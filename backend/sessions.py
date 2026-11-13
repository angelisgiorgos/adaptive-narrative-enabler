"""Play sessions: engine bundles, games created by the evolutionary AI, turns and play state."""
from __future__ import annotations

import collections
import copy
import datetime
import json
import math
import random
import tempfile
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from numbers import Number
from pathlib import Path
import yaml
import main as engine
from core_engine import GameState, Genome, Mission
from core_engine import variables as persistent
from core_engine.variables import VariableStore
from core_engine.evolution import LearningMemory, NarrativeEvolution, evolution_settings
from core_engine.model import EngineModel
from utils.config_loader import config

from . import records
from .errors import AuthoringError
from .store import APP_SETTINGS, MAX_CONFIG_UPLOAD_BYTES, MAX_ENGINE_UPLOAD_BYTES, MAX_SIMULATION_WORK, MAX_WEB_SESSIONS, _validate_complete_config
from .content import _player_stats, _profile


SESSIONS = {}
SESSION_RECORDS = {}  # session token -> id of its story in the records database

SESSION_LOCK = threading.Lock()
# Recent evolution runs (learning summaries), so any run or generation can be exported.
RUNS: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
MAX_RUNS = 20

def play_state(state: GameState) -> dict:
    """Structured snapshot of a game, for clients that render their own UI."""
    return {
        "location": state.current.name,
        "start_location": state.start_location,
        "health": state.health,
        "max_health": state.max_health,
        "coins": state.coins,
        "power": round(state.power, 3),
        "inventory": sorted(state.inventory),
        "mission": state.mission.title if state.mission else None,
        "mission_status": state.mission_status,
        "won": bool(state.won),
        "event": state.active_event.title if state.active_event else None,
        "end_reason": state.end_reason,
        "profile": state.profile if state._store is not None else None,
        "variables": {k: v for k, v in state.variables.items() if k in state.variable_specs or k == "playthroughs"},
        "stats": state.stats.values(state.combat.item_bonuses(state.inventory)),
        "stat_points": dict(state.stats.points),
        "last_check": vars(state.combat.history[-1]).copy() if state.combat.history else None,
        "quest": state.quest_log(),
        "people": state.people_here() if not state.ended else [],
        "people_met": state.people_met(),
        "map": state.map_view(),
        "choices": choice_details(state),
        "ways_on": state.ways_on() if not state.ended else [],
        "stakes": state.stakes() if hasattr(state, "stakes") else None,
        # The session: its cast, the one-time actions tried, and its report once it has ended.
        "cast": state.scope() if hasattr(state, "scope") else None,
        "unique_actions": state.unique_actions() if hasattr(state, "unique_actions") else [],
        "report": state.__dict__.get("session_report"),
    }

def _export_run_artifacts(model, candidate_count=0, generations=0, simulation_steps=0):
    """(the run's configuration as YAML, the engine model file).

    The model file (core_engine/model.py) holds the world, the storyteller, the
    training state, the memory and the tag vectors; it is also a studio bundle
    (config.yaml + best_model.yaml), so older loaders read it too."""
    exported = dict(model.config)
    exported["generated_run"] = {
        "best_score": float(model.score or 0.0),
        "candidate_count": int(candidate_count),
        "generations": int(generations),
        "simulation_steps": int(simulation_steps),
        "genome": dict(model.genome_traits),
        "engine_id": model.id,
    }
    if model.learning:
        exported["generated_run"]["learning"] = model.learning
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".yaml", prefix="narrative-config-", delete=False,
    ) as stream:
        yaml.safe_dump(exported, stream, sort_keys=False, allow_unicode=True)
        config_path = stream.name
    with tempfile.NamedTemporaryFile(suffix=".zip", prefix="narrative-engine-", delete=False) as archive_stream:
        bundle_path = archive_stream.name
    model.save(bundle_path)
    return config_path, bundle_path

def _validated_genome(values):
    if not isinstance(values, dict):
        raise AuthoringError("The artifact does not contain a valid genome mapping.")
    genome = Genome()
    expected = set(vars(genome))
    defaults = Genome.added_field_defaults()
    missing = sorted(expected - set(values) - set(defaults))
    if missing:
        raise AuthoringError("The saved genome is missing: " + ", ".join(missing))
    for key in expected:
        value = values.get(key, defaults.get(key))
        if not isinstance(value, Number) or isinstance(value, bool) or not math.isfinite(float(value)):
            raise AuthoringError(f"Genome parameter `{key}` must be a finite number.")
        setattr(genome, key, float(value))
    return genome

def _model_genome_values(model_data):
    if not isinstance(model_data, dict):
        return None
    if isinstance(model_data.get("genome"), dict):
        return model_data["genome"]
    generated_run = model_data.get("generated_run")
    if isinstance(generated_run, dict) and isinstance(generated_run.get("genome"), dict):
        return generated_run["genome"]
    expected = set(vars(Genome())) - set(Genome.added_field_defaults())
    if expected.issubset(model_data):
        return model_data
    return None

def _read_engine_artifact(uploaded_file, model_parameters_file=None):
    if not uploaded_file:
        raise AuthoringError("Choose a generated YAML or engine ZIP first.")
    path = Path(uploaded_file)
    if path.stat().st_size > MAX_ENGINE_UPLOAD_BYTES:
        raise AuthoringError(f"Engine artifacts must be {MAX_ENGINE_UPLOAD_BYTES:,} bytes or smaller.")
    try:
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                if "config.yaml" not in names:
                    raise AuthoringError("The engine ZIP is missing `config.yaml`.")
                info = archive.getinfo("config.yaml")
                if info.file_size > MAX_CONFIG_UPLOAD_BYTES:
                    raise AuthoringError("The archived configuration is too large.")
                config_data = yaml.safe_load(archive.read("config.yaml"))
                model_name = "best_model.yaml" if "best_model.yaml" in names else "best_genome.yaml"
                if model_name in names and archive.getinfo(model_name).file_size > 100_000:
                    raise AuthoringError("The archived genome metadata is too large.")
                genome_doc = (
                    yaml.safe_load(archive.read(model_name))
                    if model_name in names
                    else config_data.get("generated_run", {}) if isinstance(config_data, dict) else {}
                )
        elif path.suffix.lower() in {".yaml", ".yml"}:
            config_data = yaml.safe_load(path.read_text(encoding="utf-8"))
            genome_doc = config_data.get("generated_run", {}) if isinstance(config_data, dict) else {}
        else:
            raise AuthoringError("Upload a generated .yaml, .yml, or engine .zip file.")
    except (OSError, UnicodeError, zipfile.BadZipFile, KeyError, yaml.YAMLError) as exc:
        raise AuthoringError(f"Could not read the engine artifact: {exc}") from exc

    if not isinstance(config_data, dict):
        raise AuthoringError("The artifact configuration must be a mapping/object.")
    errors = _validate_complete_config(config_data, config.default_config())
    if errors:
        raise AuthoringError("The artifact configuration is invalid:\n" + "\n".join(errors[:20]))
    if model_parameters_file:
        model_path = Path(model_parameters_file)
        if model_path.suffix.lower() not in {".yaml", ".yml"}:
            raise AuthoringError("Model parameters must be a .yaml or .yml file.")
        try:
            if model_path.stat().st_size > 100_000:
                raise AuthoringError("The model parameters file must be 100 KB or smaller.")
            model_data = yaml.safe_load(model_path.read_text(encoding="utf-8"))
        except AuthoringError:
            raise
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise AuthoringError(f"Could not read the model parameters: {exc}") from exc
        genome_values = _model_genome_values(model_data)
    else:
        genome_values = _model_genome_values(genome_doc)
    if genome_values is None:
        raise AuthoringError(
            "No model parameters were found. Upload a generated bundle/configuration "
            "or provide a separate model parameters YAML file."
        )
    return config_data, _validated_genome(genome_values)

def _mission_key(world, mission):
    """The key of the mission a player asked for (its key or its title); None: drawn by the story."""
    if mission in (None, "") or str(mission).strip().lower() in ("any", "random"):
        return None
    wanted = str(mission).strip()
    for key, entry in world.missions.items():
        if wanted in (key, entry.title):
            return key
    known = ", ".join(f"{entry.title} ({key})" for key, entry in world.missions.items()) or "none"
    raise AuthoringError(f"This world has no mission '{wanted}'. Its missions: {known}.")


def failure_lint():
    """The failures the current world leaves as dead ends (story_beats.failure_lint): actions
    whose failure changes nothing but health or coins and that no complication event fits."""
    from core_engine.story_beats import failure_lint as lint

    world = engine.build_world(Genome(rng=random.Random(0)))
    return lint(world)


def playable_missions(profile=None):
    """The missions of the current world as a player may choose them: [{key, title,
    description, stages, available}] where ``available`` says whether the mission is
    open to this profile now (its enabled_if, read with the profile's stored variables)."""
    profile = _profile(profile)
    specs = persistent.definitions()
    store = VariableStore()
    values = store.load(profile, specs)
    achieved = set(store.completed_goals(profile)) if (config.get("progression") or {}).get("goals_once", True) else set()
    missions = []
    for key, entry in (config.get("world_definition.missions") or {}).items():
        entry = entry or {}
        condition = entry.get("enabled_if")
        try:
            available = condition is None or persistent.check(condition, values)
        except Exception:
            available = True  # a condition on the story itself: decided when the story starts
        missions.append({
            "key": key, "title": entry.get("title") or key, "description": entry.get("description") or "",
            "stages": [(stage or {}).get("title") or f"Stage {number}" for number, stage in
                       enumerate(entry.get("stages") or [], start=1)],
            "available": bool(available) and key not in achieved,
            "achieved": key in achieved,  # every goal once per save game (progression.goals_once)
        })
    return missions


def _start_game(genome, profile, points, mode, origin="studio", run_id=None, engine_id=None, mission=None):
    """A playable game for the genome; variable changes are saved to the profile, and
    the game is recorded (records database) as it is played. ``mission``: the mission
    the player chose (key or title), else the story draws one."""
    world = engine.build_world(genome)
    mission = _mission_key(world, mission)
    if mission is not None and not next((m["available"] for m in playable_missions(profile) if m["key"] == mission), True):
        raise AuthoringError(f"The mission '{world.missions[mission].title}' is not open to profile '{profile}' "
                             "now (it was already achieved in this save game, or its enabled_if does not hold).")
    try:
        player = GameState(world, genome, store=VariableStore(), profile=profile,
                           stats=points, stats_mode=mode, mission=mission)
    except ValueError as exc:
        if mission is None:
            raise
        raise AuthoringError(str(exc)) from exc
    token = uuid.uuid4().hex
    player.session_id = token  # the session's id in its report (core_engine/progression.py)
    story_id = records.record_story(
        player, origin, "play", run_id=run_id, engine_id=engine_id, session_id=token,
        parameters={"profile": profile, "stat_points": points, "stats_mode": mode, "mission": mission},
    )
    with SESSION_LOCK:
        while len(SESSIONS) >= MAX_WEB_SESSIONS:
            dropped = next(iter(SESSIONS))
            SESSIONS.pop(dropped)
            SESSION_RECORDS.pop(dropped, None)
        SESSIONS[token] = player
        SESSION_RECORDS[token] = story_id
    return token, player


def load_best_engine(uploaded_file, model_parameters_file=None, profile=None, stats_choice=None, stats_points=None,
                     origin="studio", mission=None):
    """Start a game with a previously evolved storyteller (no evolution): an engine
    model file, or an older bundle / configuration YAML."""
    profile = _profile(profile)
    points, mode = _player_stats(stats_choice, stats_points)
    if uploaded_file and EngineModel.is_model(uploaded_file):
        if Path(uploaded_file).stat().st_size > MAX_ENGINE_UPLOAD_BYTES:
            raise AuthoringError(f"Engine artifacts must be {MAX_ENGINE_UPLOAD_BYTES:,} bytes or smaller.")
        try:
            model = EngineModel.load(uploaded_file)
        except ValueError as exc:
            raise AuthoringError(str(exc)) from exc
        model.activate()
        note = "" if model.code_matches() else " It was trained with different engine code, so stories may differ."
    else:
        config_data, genome = _read_engine_artifact(uploaded_file, model_parameters_file)
        config.use_override(config_data)
        score = (config_data.get("generated_run") or {}).get("best_score")
        model = EngineModel.capture(genome, score=score if isinstance(score, Number) else None,
                                    learning=(config_data.get("generated_run") or {}).get("learning"),
                                    source="loaded bundle")
        note = " (an older bundle: no training state, so it can be retrained but not continued)."
    engine.refresh_runtime_settings()
    records.record_engine(model, origin=origin, source=model.source or "loaded")
    return _game_for(model, profile, points, mode, origin,
                     f"Saved engine loaded. Best recorded score: **{model.score if model.score is not None else 'unknown'}**. "
                     f"Evolution was skipped.{note}", mission=mission)


def _game_for(model, profile, points, mode, origin, report, run_id=None, candidate_count=0, generations=0,
              simulation_steps=0, mission=None):
    token, player = _start_game(model.genome, profile, points, mode, origin=origin, run_id=run_id, engine_id=model.id,
                                mission=mission)
    scene, actions = _scene(player)
    config_path, bundle_path = _export_run_artifacts(model, candidate_count, generations, simulation_steps)
    return {
        "token": token, "report": report, "scene": scene, "actions": actions, "last_turn": "",
        "config_path": config_path, "engine_path": bundle_path, "learning": model.learning,
        "engine_id": model.id, "run_id": run_id,
    }


def _recorded_model(engine_id):
    try:
        return records.load_engine(engine_id)
    except LookupError as exc:
        raise AuthoringError(str(exc)) from exc
    except ValueError as exc:
        raise AuthoringError(f"Engine `{engine_id}` could not be loaded: {exc}") from exc


def play_engine(engine_id, profile=None, stats_choice=None, stats_points=None, origin="studio", mission=None):
    """Start a game with a recorded engine: its world, tuning and storyteller."""
    profile = _profile(profile)
    points, mode = _player_stats(stats_choice, stats_points)
    model = _recorded_model(engine_id)
    model.activate()
    engine.refresh_runtime_settings()
    return _game_for(model, profile, points, mode, origin,
                     f"Recorded engine `{engine_id}` loaded. Benchmark score: **{model.score}**.", mission=mission)


def _option_limit():
    return max(1, int((config.get("player_options") or {}).get("limit", 6)))


def _player_options(state: GameState):
    """The options a player sees (GameState.player_options): the most relevant of the
    storyteller's options, at most player_options.limit, in the scene's order
    (player_options.order: world) or best first (ranked)."""
    order = (config.get("player_options") or {}).get("order", "ranked")
    return state.player_options(limit=_option_limit(), order="world" if order == "world" else "ranked")


def choice_details(state):
    """The choices offered now, each with the check it involves ({stat, chance, bonus}, or
    None): the player sees what is at stake before choosing."""
    if state.ended:
        return []
    return [{"name": action.name, "check": state.check_hint(action)} for action in _player_options(state)]


def risk_text(risk):
    """What failing a choice costs, in a few words: "fail: -2 health, -8 coins, sent to the Dungeon"."""
    if not risk:
        return ""
    parts = [f"{risk[key]} {key}" for key in ("health", "coins") if risk.get(key)]
    if risk.get("sent_to"):
        parts.append(f"sent to the {risk['sent_to']}")
    return "fail: " + ", ".join(parts) if parts else ""


def check_text(check):
    """A choice's check in a few words: "stealth 45% (fail: -2 health)"."""
    if not check:
        return ""
    risk = risk_text(check.get("risk"))
    return f"{check['stat']} {check['chance']}%" + (f" ({risk})" if risk else "")


def quest_line(quest):
    """The mission in one line of the scene: title, current stage, what is still to do."""
    stages = quest.get("stages") or []
    current = next((number for number, stage in enumerate(stages, start=1) if stage["state"] == "current"), None)
    line = f"**Quest:** {quest['title']}"
    if current:
        line += f" · stage {current}/{len(stages)}: *{stages[current - 1]['title']}*"
    if quest["status"] != "in progress":
        line += f" · {quest['status']}"
    elif quest.get("todo"):
        line += " · to do: " + "; ".join(quest["todo"])
    return line


def people_text(people):
    """The people here, one line each: who they are, what they want, how you stand with them."""
    lines = ["**People here:**"]
    for person in people:
        notes = []
        if person["hostile"]:
            notes.append("⚠ hostile")
        if person["quest"]:
            notes.append("★ can help with your quest")
        if person.get("importance", 0) >= 2:
            notes.append(f"◆ {person.get('role')}")  # someone who matters in this story (npc_roles)
        if person.get("ally"):
            notes.append("♥ ally")
        elif person.get("helped"):
            notes.append(f"helped {person['helped']}×")
        if person["talks"]:
            notes.append(f"spoken with {person['talks']}×")
        line = f"- **{person['name']}** — {person['description']}"
        if person.get("wants"):
            line += f" *Wants:* {person['wants'][:1].lower() + person['wants'][1:]}."
        if notes:
            line += " (" + " · ".join(notes) + ")"
        lines.append(line)
    return "\n".join(lines)


def _scene(state: GameState) -> tuple[str, list[str]]:
    actions = _player_options(state) if not state.ended else []
    description, npcs, objects = state._describe_visible_scene()
    inventory = ", ".join(sorted(state.inventory)) or "empty"
    details = [
        f"## {state.scene_name}",
        description or "",
        f"**Health:** {state.health_player.status()} · **Coins:** {state.coins} · **Power:** {state.power:.2f}",
        f"**Inventory:** {inventory}",
    ]
    quest = state.quest_log()
    if quest:
        details.insert(1, quest_line(quest))
    if npcs and not state.ended:
        details.append(people_text(state.people_here()))
    if state.stats.names:
        bonuses = state.combat.item_bonuses(state.inventory)
        details.append("**Stats:** " + " · ".join(
            f"{name} {value}" + (f" (+{bonuses[name]})" if bonuses.get(name) else "")
            for name, value in state.stats.values(bonuses).items()
        ))
    checks = [f"{c['name']} — {check_text(c['check'])}" for c in choice_details(state) if c["check"]]
    if checks:
        details.append("**Checks:** " + " · ".join(checks))
    stakes = state.stakes() if hasattr(state, "stakes") else None
    if stakes and stakes["alert"]:
        details.append(f"**Alert:** {stakes['alert']}/{stakes['max']}" + (" — the watch is looking for you"
                                                                         if stakes["alert"] >= stakes["warn_at"] else ""))
    if state.combat.history:
        last = state.combat.history[-1]
        details.append(f"**Last check:** {last.stat} rolled {last.roll} vs {last.target} — {'passed' if last.passed else 'failed'}")
    shown = {k: v for k, v in state.variables.items() if k in state.variable_specs}
    if shown:
        details.append("**Remembered across playthroughs:** " + " · ".join(f"{k} {v}" for k, v in shown.items()))
    if objects:
        details.append("**Visible items:** " + ", ".join(name for name, _ in objects))
    if state.won and state.ended:
        details.append(f"### Mission complete\n{state.end_reason}")
    elif state.ended:
        details.append(f"### Game over\n{state.end_reason}")
    return "\n\n".join(details), [action.name for action in actions]

def run_simulation(candidate_count: int, generations: int, simulation_steps: int, deterministic: bool,
                   profile=None, stats_choice=None, stats_points=None, episodes=None, warm_start=None,
                   progress=None, origin="studio", mission=None):
    """Evolve storytellers with the shared evolutionary AI and start a game with the best.

    ``progress(generation, total, GenerationStats)`` reports each generation.
    Returns the game (token, scene, actions), the report, the exported bundle and
    the learning summary (history per generation, what the AI learned).
    """
    profile = _profile(profile)
    points, mode = _player_stats(stats_choice, stats_points)
    config.reload()
    engine.refresh_runtime_settings()
    candidate_count = max(1, min(int(candidate_count), APP_SETTINGS["candidate_limit"]))
    generations = max(1, min(int(generations), APP_SETTINGS["generation_limit"]))
    simulation_steps = max(1, min(int(simulation_steps), APP_SETTINGS["simulation_step_limit"]))
    episodes = max(1, min(int(episodes or config.get("evolution.episodes_per_genome", 3)), 10))
    # Whole stories (evolution.full_length_stories) can last up to narrative_end.max_actions.
    story_steps = simulation_steps
    if evolution_settings().get("full_length_stories"):
        story_steps = max(simulation_steps, max(100, int(config.get("narrative_end.max_actions", 100))))
    requested_work = candidate_count * generations * story_steps * episodes
    if requested_work > MAX_SIMULATION_WORK:
        raise AuthoringError(
            f"This run requests {requested_work:,} simulated turns, above the "
            f"public-app safety limit of {MAX_SIMULATION_WORK:,}. Reduce candidates, "
            "generations, steps, or episodes per genome."
        )
    seed = None
    if deterministic:
        seed = config.get("reproducibility.seed")
        if seed is None:
            raise AuthoringError("Set a reproducibility seed before running deterministically.")
        engine.apply_random_seed(force=True)
        seed = int(seed)

    warm_start = config.get("evolution.warm_start", True) if warm_start is None else bool(warm_start)
    evolution = NarrativeEvolution(
        engine.build_world, engine.fitness,
        settings={
            "pop_size": candidate_count, "generations": generations, "max_steps": simulation_steps,
            "episodes_per_genome": episodes, "crossover_rate": APP_SETTINGS["crossover_probability"],
            "warm_start": warm_start,
        },
        seed=seed,
        memory=LearningMemory(),
    )
    parameters = {"candidate_count": candidate_count, "generations": generations, "simulation_steps": simulation_steps,
                  "episodes": episodes, "deterministic": bool(deterministic), "warm_start": warm_start,
                  "profile": profile, "stat_points": points, "stats_mode": mode}
    run_id = uuid.uuid4().hex
    records.start_run(origin, settings=evolution.settings, parameters=parameters, seed=seed, run_id=run_id)
    try:
        result = evolution.run(progress=progress)
    except Exception as exc:
        records.finish_run(run_id, None, error=exc)
        raise
    return _finish_training(evolution, result, run_id, origin, profile, points, mode, candidate_count, episodes,
                            simulation_steps, title="Evolution complete.", mission=mission)


def _finish_training(evolution, result, run_id, origin, profile, points, mode, candidate_count, episodes,
                     simulation_steps, title, parent_id=None, source=None, mission=None):
    """Record a finished run (its generations, champions, best engine with its model
    file, and its preview story) and start a game with the best storyteller."""
    learning = result.summary()
    genome, score, simulated = result.best_genome, result.best_score, result.best_state
    model = EngineModel.from_training(evolution, result, source=source or f"{origin} run {run_id}", parent_id=parent_id)
    records.finish_run(run_id, result, model=model, origin=origin)
    records.record_story(simulated, origin, "preview", run_id=run_id, engine_id=model.id,
                         parameters={"benchmark": True})

    token, player = _start_game(genome, profile, points, mode, origin=origin, run_id=run_id, engine_id=model.id,
                                mission=mission)
    scene, actions = _scene(player)
    history = result.history
    report = (
        f"{title} Best benchmark score: **{score:.2f}** "
        f"({learning['improvement']:+.2f} since generation 0)  \n"
        f"Generations: **{result.generations_run}** · Storytellers per generation: **{candidate_count}** · "
        f"Stories per storyteller: **{episodes}**"
        + (f"  \nStarted from **{result.warm_started}** storyteller(s) remembered from earlier runs." if result.warm_started else "")
        + f"  \nBenchmark per generation: {' → '.join(f'{h.validation:.0f}' for h in history)}  \n"
        f"Preview path: {' → '.join(simulated.path)}  \n"
        f"Preview mission: {simulated.mission_status}  \n"
        f"Player profile: **{profile}** (playthroughs finished: {player.variables.get('playthroughs', 0)})  \n"
        f"Engine: `{model.id}`" + (" (recorded)" if records.enabled() else "")
    )
    config_path, bundle_path = _export_run_artifacts(model, candidate_count, result.generations_run, simulation_steps)
    with SESSION_LOCK:
        RUNS[run_id] = {"learning": learning, "token": token, "engine_id": model.id,
                        "checkpoint": evolution.last_checkpoint}
        while len(RUNS) > MAX_RUNS:
            RUNS.popitem(last=False)
    return {
        "token": token, "run_id": run_id, "report": report, "scene": scene, "actions": actions, "last_turn": "",
        "config_path": config_path, "engine_path": bundle_path, "learning": learning, "engine_id": model.id,
    }


def _training_guard(pop_size, generations, episodes, steps):
    story_steps = steps
    if evolution_settings().get("full_length_stories"):
        story_steps = max(steps, max(100, int(config.get("narrative_end.max_actions", 100))))
    requested = int(pop_size) * int(generations) * int(episodes) * int(story_steps)
    if requested > MAX_SIMULATION_WORK:
        raise AuthoringError(f"This training requests {requested:,} simulated turns, above the public-app safety "
                             f"limit of {MAX_SIMULATION_WORK:,}. Ask for fewer generations.")


def continue_engine(engine_id, generations=1, profile=None, stats_choice=None, stats_points=None, origin="studio",
                    progress=None, mission=None):
    """Continue a recorded engine's training exactly where it stopped, for
    ``generations`` more generations; records the run and starts a game with the result."""
    profile = _profile(profile)
    points, mode = _player_stats(stats_choice, stats_points)
    model = _recorded_model(engine_id)
    if not model.checkpoint:
        raise AuthoringError(f"Engine `{engine_id}` has no saved training state to continue; retrain it instead.")
    generations = max(1, min(int(generations), APP_SETTINGS["generation_limit"]))
    settings = model.checkpoint["settings"]
    _training_guard(settings["pop_size"], generations, settings["episodes_per_genome"], settings["max_steps"])
    model.activate()
    engine.refresh_runtime_settings()
    run_id = uuid.uuid4().hex
    records.start_run(origin, kind="continue", settings={**settings, "generations": generations},
                      parameters={"generations": generations, "from_generation": model.checkpoint["next_generation"],
                                  "profile": profile}, seed=model.checkpoint.get("seed"), parent_engine_id=engine_id,
                      run_id=run_id)
    try:
        from core_engine.evolution import ModelMemory

        evolution = NarrativeEvolution.from_checkpoint(
            engine.build_world, engine.fitness, model.checkpoint, generations=generations,
            memory=ModelMemory(model.memory) if model.memory or settings.get("warm_start") else None,
        )
        result = evolution.run(progress=progress, resume=model.checkpoint)
    except Exception as exc:
        records.finish_run(run_id, None, error=exc)
        raise
    return _finish_training(evolution, result, run_id, origin, profile, points, mode, settings["pop_size"],
                            settings["episodes_per_genome"], settings["max_steps"],
                            title=f"Training continued for {generations} generation(s) from engine `{engine_id}`.",
                            parent_id=engine_id, source=f"continued from {engine_id}", mission=mission)


def retrain_engine(engine_id, candidate_count=8, generations=5, simulation_steps=16, episodes=None, deterministic=True,
                   profile=None, stats_choice=None, stats_points=None, origin="studio", progress=None, mission=None):
    """A new run whose first population starts from a recorded engine's storyteller
    and memory, on that engine's world; records it and starts a game with the result."""
    profile = _profile(profile)
    points, mode = _player_stats(stats_choice, stats_points)
    model = _recorded_model(engine_id)
    model.activate()
    engine.refresh_runtime_settings()
    candidate_count = max(1, min(int(candidate_count), APP_SETTINGS["candidate_limit"]))
    generations = max(1, min(int(generations), APP_SETTINGS["generation_limit"]))
    simulation_steps = max(1, min(int(simulation_steps), APP_SETTINGS["simulation_step_limit"]))
    episodes = max(1, min(int(episodes or config.get("evolution.episodes_per_genome", 3)), 10))
    _training_guard(candidate_count, generations, episodes, simulation_steps)
    seed = int(config.get("reproducibility.seed")) if deterministic and config.get("reproducibility.seed") is not None else None
    settings = {"pop_size": candidate_count, "generations": generations, "max_steps": simulation_steps,
                "episodes_per_genome": episodes, "crossover_rate": APP_SETTINGS["crossover_probability"]}
    run_id = uuid.uuid4().hex
    records.start_run(origin, kind="retrain", settings=settings, parameters={**settings, "profile": profile},
                      seed=seed, parent_engine_id=engine_id, run_id=run_id)
    try:
        from core_engine.evolution import ModelMemory

        memory = ModelMemory(model.memory)
        memory.remember([(model.score if model.score is not None else 0.0, model.genome)], size=max(len(model.memory) + 1, 12))
        evolution = NarrativeEvolution(engine.build_world, engine.fitness, settings={**settings, "warm_start": True},
                                       seed=seed, memory=memory)
        result = evolution.run(progress=progress)
    except Exception as exc:
        records.finish_run(run_id, None, error=exc)
        raise
    return _finish_training(evolution, result, run_id, origin, profile, points, mode, candidate_count, episodes,
                            simulation_steps, title=f"Retrained from engine `{engine_id}`.", parent_id=engine_id,
                            source=f"retrained from {engine_id}", mission=mission)


# ---- the records database, for the studio ------------------------------------------------

def recorded_engines(limit=200) -> list[tuple[str, str]]:
    """(label, id) of the recorded engines, newest first (empty when there are none)."""
    try:
        engines = records.list_engines(limit=limit)
    except LookupError:
        return []
    return [(f"{e['created_at'][:16]} · {e['source']}" + (f" · gen {e['generation']}" if e['generation'] is not None else "")
             + (f" · score {e['score']:.1f}" if e['score'] is not None else "") + (" · resumable" if e['resumable'] else "")
             + f" · {e['id'][:8]}", e["id"]) for e in engines]


def recorded_stories(limit=200) -> list[tuple[str, str]]:
    try:
        stories = records.list_stories(limit=limit)
    except LookupError:
        return []
    return [(f"{s['created_at'][:16]} · {s['origin']} {s['kind']} · {s['turns']} turns · {s['status']}"
             + (f" · {s['mission_status']}" if s['mission_status'] else "") + f" · {s['id'][:8]}", s["id"])
            for s in stories]


def recorded_runs(limit=50) -> list[dict]:
    try:
        runs = records.list_runs(limit=limit)
    except LookupError:
        return []
    return [{"run": r["id"][:8], "created": r["created_at"][:16], "origin": r["origin"], "kind": r["kind"],
             "status": r["status"], "generations": r["generations_run"], "best score": r["best_score"],
             "engine": (r["best_engine_id"] or "")[:8]} for r in runs]


def recorded_story(story_id) -> dict:
    try:
        return records.get_story(story_id)
    except LookupError as exc:
        raise AuthoringError(str(exc)) from exc


def recorded_engine_files(engine_id) -> tuple[str, str]:
    """(engine model file, world-and-tuning YAML) of a recorded engine, for download."""
    model = _recorded_model(engine_id)
    model_path = tempfile.NamedTemporaryFile(prefix=f"engine-{engine_id[:8]}-", suffix=".ane.zip", delete=False).name
    model.save(model_path)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=f"world-{engine_id[:8]}-", suffix=".yaml",
                                     delete=False) as stream:
        stream.write(model.world_yaml())
        world_path = stream.name
    return model_path, world_path


def learning_memory() -> dict:
    """The storytellers the AI remembers from earlier runs (hall of fame)."""
    entries = LearningMemory().load()
    return {"hall_of_fame": entries, "size": len(entries), "path": LearningMemory().path}


def forget_learning() -> dict:
    LearningMemory().forget()
    return learning_memory()


def end_session(token: str):
    """End a session where it stands (the player stops): the progression is saved and the
    session reported, as when a story ends by itself. Returns its report."""
    with SESSION_LOCK:
        state = SESSIONS.get(token)
        if state is None:
            raise AuthoringError("Play session not found.")
        state.end_session()
        records.update_story(SESSION_RECORDS.get(token), state)
        return state.__dict__.get("session_report")


def session_report(token: str):
    """The report of an ended session (None while it goes on)."""
    with SESSION_LOCK:
        state = SESSIONS.get(token)
        if state is None:
            raise AuthoringError("Play session not found.")
        return state.__dict__.get("session_report") if state.ended else None


def progression(profile=None):
    """A player's save game (core_engine/progression.py): goals achieved, playthroughs, sessions."""
    return VariableStore().progression(_profile(profile))


def resend_report(profile, report_id):
    """Send a kept session report to the UNP again (here and now); returns the delivery."""
    from core_engine import progression as reports

    store = VariableStore()
    report = next((r for r in store.sessions(_profile(profile)) if r.get("report_id") == report_id), None)
    if report is None:
        raise AuthoringError(f"No session report `{report_id}` in profile `{_profile(profile)}`.")
    report = {k: v for k, v in report.items() if k != "delivery"}
    delivery = reports.send(report)
    store.note_delivery(_profile(profile), report_id, delivery)
    return delivery


def play_action(token: str, selected_action: str):
    """Take one action; returns (scene, next action names, text of the turn)."""
    with SESSION_LOCK:
        state = SESSIONS.get(token)
        if state is None:
            raise AuthoringError("Run a simulation first to start a game.")
        offered = [action.name for action in _player_options(state)]
        if selected_action not in offered:
            raise AuthoringError("That choice is no longer available. Select a current action.")
        names = [action.name for action in state.ranked_available_actions()]  # step() indexes the ranked order
        before = len(state.history)
        state.step(names.index(selected_action))
        turn_text = "\n\n".join(state.history[before:]).strip()
        scene, next_actions = _scene(state)
        records.update_story(SESSION_RECORDS.get(token), state)
    return scene, next_actions, turn_text


# ---- importing artifacts into the records ---------------------------------------------------
# Generated files (the API's downloads in artifacts/, saved engines, exports, story
# configurations) become recorded engines and games: each is read, completed, checked,
# recorded once (duplicates are linked to the engine already recorded) and moved into the
# records folder (imports/<kind>/).

IMPORT_KINDS = ("engine_model", "offline_export", "engine_bundle", "story_config")


def _literal(value):
    """Values some older artifacts stored as Python text ("{'threat_prob': 0.3, ...}")."""
    if isinstance(value, str) and value.strip()[:1] in "{[":
        import ast

        try:
            return ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return value
    return value


def _completed_config(cfg):
    """An artifact's configuration completed as an upload is (store.complete_config)."""
    from .store import complete_config

    completed, errors, filled = complete_config(cfg)
    if errors:
        raise AuthoringError("; ".join(errors))
    return completed, filled


def _read_import(path):
    """(kind, data) of an artifact; data has config, genome and what the file carried."""
    path = Path(path)
    if path.suffix.lower() == ".zip":
        if EngineModel.is_model(path):
            model = EngineModel.load(path)
            return "engine_model", {"model": model, "learning": model.learning, "created": model.created}
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            engine_json = next((n for n in sorted(names) if n.endswith("engine.json") and n.count("/") <= 1), None)
            if engine_json:  # an offline engine export
                root = engine_json[: -len("engine.json")]
                meta = json.loads(archive.read(engine_json))
                if root + "config/engine.yaml" not in names:
                    raise AuthoringError("The engine export has no config/engine.yaml.")

                def optional(name, default):
                    return json.loads(archive.read(root + name)) if root + name in names else default

                memory = optional("saves/evolution_memory.json", {})
                return "offline_export", {
                    "config": yaml.safe_load(archive.read(root + "config/engine.yaml")),
                    "genome": _literal(meta.get("genome")), "score": meta.get("score"),
                    "learning": _literal(meta.get("learning")), "created": meta.get("created"),
                    "source": meta.get("source"), "tag_vectors": optional("data/tag_vectors.json", {}),
                    "memory": memory.get("hall_of_fame", []) if isinstance(memory, dict) else [],
                }
            if "config.yaml" in names:  # an older engine bundle
                cfg = yaml.safe_load(archive.read("config.yaml"))
                model_name = next((n for n in ("best_model.yaml", "best_genome.yaml") if n in names), None)
                doc = yaml.safe_load(archive.read(model_name)) if model_name else (cfg or {}).get("generated_run") or {}
                return "engine_bundle", {"config": cfg, "genome": _model_genome_values(_literal_doc(doc)),
                                         "score": (doc or {}).get("best_score"), "learning": _literal((doc or {}).get("learning"))}
        raise AuthoringError("Not an engine model, engine bundle or engine export.")
    if path.suffix.lower() in (".yaml", ".yml"):
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict) or "world_definition" not in cfg:
            raise AuthoringError("Not a story configuration (no world_definition).")
        run = _literal_doc(cfg.get("generated_run") or {})
        return "story_config", {"config": cfg, "genome": _model_genome_values(run), "score": run.get("best_score"),
                                "learning": _literal(run.get("learning")), "engine_id": run.get("engine_id")}
    raise AuthoringError("Not an engine or story artifact (.zip, .yaml).")


def _literal_doc(doc):
    return {k: _literal(v) for k, v in doc.items()} if isinstance(doc, dict) else doc


def _benchmark(model):
    """The benchmark score of a storyteller on its own world (the evolution's validation stories)."""
    from core_engine.evolution import evaluate_task

    model.activate()
    engine.refresh_runtime_settings()
    evolution = NarrativeEvolution(engine.build_world, engine.fitness, seed=0)
    return round(float(evaluate_task(evolution.benchmark_task(model.genome_traits))), 3)


def _preview_story(model, engine_id, seed=0):
    """One whole story told by the engine on its world, recorded as the engine's preview game."""
    state = model.new_game(seed=seed)
    engine.refresh_runtime_settings()
    while not state.ended and state.health > 0 and state.step():
        pass
    return records.record_story(state, "import", "preview", engine_id=engine_id, seed=seed,
                                parameters={"preview": True, "seed": seed})


def import_artifacts(folder, move=True, preview=True, progress=print):
    """Import every engine and game in ``folder`` into the records database.

    Each file is read (engine models, offline exports, older bundles, story
    configurations), its world completed with the tuning it lacks and checked, and
    recorded with what it carried: storyteller, score, learning memory, tag vectors
    and training run. What is missing is filled in: tag vectors (computed for its
    world), a benchmark score, the run and its generations (from its learning
    summary), and a preview story. A file whose world and storyteller are already
    recorded is linked to that engine. The file is then moved (``move``) or copied
    into the records folder (imports/<kind>/). Files that are not engines or games,
    or whose world is invalid, stay where they are. Returns a report."""
    import shutil
    import datetime as _datetime

    from core_engine.builder import logic_errors
    from core_engine.model import config_id, world_tags
    from utils.tag_similarity import export_tag_vectors

    if not records.enabled():
        raise AuthoringError(f"Recording is off: {records.directory()} cannot be used.")
    folder = Path(folder)
    files = sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in (".zip", ".yaml", ".yml")),
                   key=lambda p: p.stat().st_mtime)
    report = {"folder": str(folder), "files": len(files), "engines": 0, "linked": 0, "runs": set(), "previews": 0,
              "scored": 0, "completed": 0, "failed": [], "by_kind": collections.Counter()}
    restore = config.as_dict() if config.using_override else None
    index = records.engine_index()
    target_root = records.directory() / "imports"
    # Read everything first: engine models go first, so the richest record of an engine
    # (with its training state) is the one kept and the other files link to it.
    readable = []
    for path in files:
        try:
            readable.append((path, *_read_import(path)))
        except Exception as exc:  # noqa: BLE001 - reported; the file stays where it is
            report["failed"].append({"file": path.name, "error": str(exc)[:300]})
    readable.sort(key=lambda item: (IMPORT_KINDS.index(item[1]), item[0].stat().st_mtime))
    try:
        for number, (path, kind, data) in enumerate(readable, 1):
            created = _datetime.datetime.fromtimestamp(path.stat().st_mtime, _datetime.timezone.utc).isoformat(timespec="seconds")
            try:
                if kind == "engine_model":
                    model = data["model"]
                    filled = []
                else:
                    cfg, filled = _completed_config(data["config"])
                    errors = logic_errors(cfg.get("world_definition"))
                    if errors:
                        raise AuthoringError("Its world is invalid: " + "; ".join(errors[:3]))
                    if data.get("genome") is None:
                        raise AuthoringError("It carries no storyteller (genome).")
                    genome = _validated_genome(data["genome"])
                    key = (config_id({k: v for k, v in cfg.items() if k != "generated_run"}), records.genome_key(genome.traits()))
                    score = data.get("score")
                    model = EngineModel(
                        cfg, genome.traits(), score=score if isinstance(score, Number) else None,
                        memory=data.get("memory"), tag_vectors=data.get("tag_vectors") or export_tag_vectors(world_tags(cfg)),
                        model_id=data.get("engine_id") or uuid.uuid5(uuid.NAMESPACE_URL, "ane-imported-engine:" + "|".join(key)).hex,
                        source=f"imported {kind}" + (f" ({data['source']})" if data.get("source") else ""),
                        created=data.get("created") or created,
                        learning=data["learning"] if isinstance(data.get("learning"), dict) else None,
                    )
                key = (model.world_id, records.genome_key(model.genome_traits))
                existing = index.get(key)
                if existing is None and model.score is None:
                    model.score = _benchmark(model)
                    report["scored"] += 1
                target = target_root / kind / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                (shutil.move if move else shutil.copy2)(str(path), str(target))
                learning = data.get("learning") if isinstance(data.get("learning"), dict) else None
                engine_id, run_id, new = records.import_engine(model, kind, target, created_at=model.created or created,
                                                               learning=learning, existing_id=existing)
                index[key] = engine_id
                report["by_kind"][kind] += 1
                report["completed"] += bool(filled)
                if run_id:
                    report["runs"].add(run_id)
                if new:
                    report["engines"] += 1
                    if preview and _preview_story(model, engine_id):
                        report["previews"] += 1
                else:
                    report["linked"] += 1
            except Exception as exc:  # noqa: BLE001 - reported per file; the file stays where it is
                report["failed"].append({"file": path.name, "error": str(exc)[:300]})
            if progress and (number % 25 == 0 or number == len(readable)):
                progress(f"[IMPORT] {number}/{len(readable)}: {report['engines']} new engines, {report['linked']} linked, "
                         f"{len(report['failed'])} not imported")
    finally:
        if restore is not None:
            config.use_override(restore)
        else:
            config.clear_override()
        engine.refresh_runtime_settings()
    report["runs"] = len(report["runs"])
    report["by_kind"] = dict(report["by_kind"])
    return report


# ---- background jobs: long work (evolution runs, continued or retrained training) that ------
# reports its progress while it runs, so clients poll it (POST /v1/jobs/..., GET /v1/jobs/{id})
# instead of waiting on one long request. Jobs run one at a time, in the order they were
# submitted (one training uses all the evaluation it is given); the latest MAX_JOBS are kept.

MAX_JOBS = 100
_JOB_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ane-job")
JOBS: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
_JOBS_LOCK = threading.Lock()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def submit_job(kind: str, work, description: str = "") -> dict:
    """Queue ``work(progress)``; ``progress(generation, total, stats)`` records how far
    it got and ``work`` returns the job's result (JSON data). Returns the job."""
    job = {"id": uuid.uuid4().hex, "kind": kind, "description": description, "status": "queued",
           "created_at": _now(), "started_at": None, "finished_at": None, "progress": None,
           "result": None, "error": None}
    with _JOBS_LOCK:
        JOBS[job["id"]] = job
        while len(JOBS) > MAX_JOBS:
            oldest = next(iter(JOBS))
            if JOBS[oldest]["status"] in ("queued", "running"):
                break
            JOBS.pop(oldest)

    def progress(generation, total, stats):
        with _JOBS_LOCK:
            if isinstance(stats, str):  # a job counting its own steps (stories judged): step n of total
                job["progress"] = {"generation": generation, "total": total, "label": stats}
                return
            job["progress"] = {"generation": generation + 1, "total": total, "best": stats.best, "mean": stats.mean,
                               "validation": stats.validation, "improved": bool(stats.improved)}

    def run():
        with _JOBS_LOCK:
            job.update(status="running", started_at=_now())
        try:
            result = work(progress)
            with _JOBS_LOCK:
                job.update(status="finished", result=result)
        except Exception as exc:  # noqa: BLE001 - reported to the client as the job's error
            with _JOBS_LOCK:
                job.update(status="failed", error=getattr(exc, "detail", None) or getattr(exc, "message", None) or str(exc))
        finally:
            with _JOBS_LOCK:
                job["finished_at"] = _now()

    _JOB_EXECUTOR.submit(run)
    return get_job(job["id"])


def get_job(job_id: str) -> dict | None:
    with _JOBS_LOCK:
        job = JOBS.get(job_id)
        return copy.deepcopy(job) if job is not None else None


def list_jobs() -> list[dict]:
    """The kept jobs, newest first, without their results."""
    with _JOBS_LOCK:
        return [{k: v for k, v in job.items() if k != "result"} for job in reversed(JOBS.values())]
