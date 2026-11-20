"""Play sessions: engine bundles, games created by the evolutionary AI, turns and play state."""
from __future__ import annotations

import collections
import copy
import datetime
import json
import logging
import math
import os
import random
import shutil
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
from core_engine.game_state import MIN_STORY_LENGTH, story_length
from core_engine import variables as persistent
from core_engine.variables import VariableStore
from core_engine.evolution import LearningMemory, NarrativeEvolution, evolution_settings, story_turns
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
# The complete engines of this process (engine id -> its model file and summary): a finished run's
# engine and every loaded engine. An ANE AI is made from one (backend/export.py), also when
# recording is off.
ENGINES: "collections.OrderedDict[str, dict]" = collections.OrderedDict()

def play_state(state: GameState) -> dict:
    """Structured snapshot of a game, for clients that render their own UI."""
    # A game not started yet (no scene set: the first step sets it) has nothing in view.
    description, _, objects = state._describe_visible_scene() if hasattr(state, "location_objects") else ("", [], [])
    return {
        "location": state.current.name,
        "scene_name": state.scene_name,
        "description": description or "",
        "items_here": [{"name": name, "text": text} for name, text in objects] if not state.ended else [],
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
        "talking_to": state.conversation() if hasattr(state, "conversation") else None,
        "turn_summary": (state.journal() or [None])[-1] if hasattr(state, "journal") else None,
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
        "session_report": state.__dict__.get("session_report"),
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


LOG = logging.getLogger("ane.sessions")
KEPT_ENGINES = 20  # finished runs kept with the saves when recording is off


def kept_engines_dir() -> Path:
    """Where finished runs' engines are kept when recording is off: with the saves (ANE_SAVE_DIR,
    a volume in Docker), so they can still be exported after a restart."""
    return Path(os.environ.get("ANE_SAVE_DIR") or (Path(__file__).resolve().parent.parent / "saves")) / "engines"


def kept_engines():
    """The engines kept with the saves (newest last), whose files are still there."""
    index = kept_engines_dir() / "index.json"
    try:
        entries = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [e for e in entries if isinstance(e, dict) and Path(str(e.get("model_path", ""))).is_file()]


def _keep_engine(info, model_path):
    """Keep a finished run's engine model with the saves (the newest KEPT_ENGINES)."""
    folder = kept_engines_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{info['engine_id']}.ane.zip"
    shutil.copyfile(model_path, target)
    entries = [e for e in kept_engines() if e.get("engine_id") != info["engine_id"]] + [{**info, "model_path": str(target)}]
    for dropped in entries[:-KEPT_ENGINES]:
        Path(dropped["model_path"]).unlink(missing_ok=True)
    entries = entries[-KEPT_ENGINES:]
    temporary = folder / ".index.json.tmp"
    temporary.write_text(json.dumps(entries, indent=1), encoding="utf-8")
    os.replace(temporary, folder / "index.json")
    return str(target)


def _remember_engine(model, model_path, run_id=None):
    """Keep a complete engine (its model file) for exports: the records keep it when recording is
    on; otherwise it is kept with the saves, so a finished run can be exported after a restart."""
    learning = model.learning or {}
    info = {"engine_id": model.id, "model_path": model_path, "created_at": model.created,
            "source": model.source, "score": model.score, "run_id": run_id, "resumable": bool(model.checkpoint),
            "generations": learning.get("generations_run") or len(learning.get("history") or []) or None}
    if not records.enabled():
        try:
            info["model_path"] = _keep_engine(info, model_path)
        except OSError as exc:
            LOG.warning("Could not keep engine %s with the saves (%s): it can be exported until the API restarts.",
                        model.id, exc)
    with SESSION_LOCK:
        ENGINES[model.id] = info
        ENGINES.move_to_end(model.id)
        while len(ENGINES) > MAX_RUNS:
            ENGINES.popitem(last=False)


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
        # Its own dice (drawn from the global, seeded ones: reproducible runs stay reproducible),
        # so that its save game can carry them.
        player = GameState(world, genome, store=VariableStore(), profile=profile, rng=random.Random(random.getrandbits(64)),
                           stats=points, stats_mode=mode, mission=mission)
    except ValueError as exc:
        if mission is None:
            raise
        raise AuthoringError(str(exc)) from exc
    return _register_session(player, origin, run_id=run_id, engine_id=engine_id,
                             parameters={"profile": profile, "stat_points": points, "stats_mode": mode,
                                         "mission": mission}), player


def _register_session(player, origin, run_id=None, engine_id=None, parameters=None):
    """Make a game a play session (recorded as it is played); returns its token."""
    token = uuid.uuid4().hex
    player.session_id = token  # the session's id in its report (core_engine/progression.py)
    story_id = records.record_story(player, origin, "play", run_id=run_id, engine_id=engine_id, session_id=token,
                                    parameters=parameters or {})
    with SESSION_LOCK:
        while len(SESSIONS) >= MAX_WEB_SESSIONS:
            dropped = next(iter(SESSIONS))
            SESSIONS.pop(dropped)
            SESSION_RECORDS.pop(dropped, None)
        SESSIONS[token] = player
        SESSION_RECORDS[token] = story_id
    return token


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
    result = _game_for(model, profile, points, mode, origin,
                       f"Saved engine loaded. Best recorded score: **{model.score if model.score is not None else 'unknown'}**. "
                       f"Evolution was skipped.{note}", mission=mission)
    _remember_engine(model, result["engine_path"])
    return result


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
                     f"Recorded engine `{engine_id}` loaded. Fitness on the benchmark (the training objective): **{model.score}**.", mission=mission)


def play_finished_run(engine_id=None, profile=None, stats_choice=None, stats_points=None, origin="studio",
                      mission=None):
    """Play the world a finished run was trained on, with its storyteller: a finished run (a
    complete engine, in the records or kept with the saves); ``engine_id`` None or "latest": the
    newest. Its world and tuning become the active ones, as when an engine is loaded."""
    from .export import EngineRequired, complete_engine

    profile = _profile(profile)
    points, mode = _player_stats(stats_choice, stats_points)
    try:
        model = complete_engine("engine", engine_id=None if engine_id in (None, "", "latest") else engine_id)
    except EngineRequired as exc:
        if engine_id in (None, "", "latest"):
            raise EngineRequired("No finished run yet: train the AI first (🧠 Train → Evolve "
                                 "storytellers), then play its world here.") from exc
        raise
    model.activate()
    engine.refresh_runtime_settings()
    when = (model.created or "")[:16].replace("T", " ")
    return _game_for(model, profile, points, mode, origin,
                     f"Playing the finished run of {when} (engine `{model.id[:8]}`): its world, with its storyteller.",
                     mission=mission)


def session_journal(token):
    """A play session turn by turn (GameState.journal)."""
    with SESSION_LOCK:
        return _session(token).journal()


def session_transcript(token):
    """The story of a play session so far."""
    with SESSION_LOCK:
        return _session(token).get_full_story()


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

def run_simulation(candidate_count=None, generations=None, simulation_steps=None, deterministic=None,
                   profile=None, stats_choice=None, stats_points=None, episodes=None, warm_start=None,
                   progress=None, origin="studio", mission=None, user_id=None, seed=None, start_game=True):
    """Evolve storytellers with the shared evolutionary AI and start a game with the best
    (``start_game`` False: train only, no game: nothing is written to the player's profile).

    The run's settings are the saved engine defaults with what it sets itself (None: the default),
    resolved by resolve_training and executed exactly. ``progress(generation, total,
    GenerationStats)`` reports each generation. Returns the game (token, scene, actions), the
    report, the exported bundle, the learning summary (history per generation, what the AI
    learned) and ``training`` (the overrides, where each value came from, the resolved settings
    and those the evolution ran with).
    """
    profile = _profile(profile)
    points, mode = _player_stats(stats_choice, stats_points)
    config.reload()
    engine.refresh_runtime_settings()
    plan = resolve_training({"candidate_count": candidate_count, "generations": generations,
                             "simulation_steps": simulation_steps, "episodes": episodes, "deterministic": deterministic,
                             "seed": seed, "warm_start": warm_start})
    _refuse_unless_ready(plan)
    run = plan["effective"]
    if run["seed"] is not None:
        engine.apply_random_seed(force=True, seed=run["seed"])
    evolution = NarrativeEvolution(
        engine.build_world, engine.fitness,
        settings={
            "pop_size": run["population"], "generations": run["generations"], "max_steps": run["requested_steps"],
            "episodes_per_genome": run["stories_per_storyteller"],
            "warm_start": run["warm_start"],
        },
        seed=run["seed"],
        memory=LearningMemory(),
    )
    candidate_count, episodes, simulation_steps = run["population"], run["stories_per_storyteller"], run["requested_steps"]
    parameters = {"candidate_count": candidate_count, "generations": run["generations"],
                  "simulation_steps": simulation_steps, "episodes": episodes, "deterministic": run["deterministic"],
                  "warm_start": run["warm_start"], "seed": run["seed"], "overrides": plan["overrides"],
                  "profile": profile, "stat_points": points, "stats_mode": mode}
    seed = run["seed"]
    run_id = uuid.uuid4().hex
    records.start_run(origin, settings=evolution.settings, parameters=parameters, seed=seed, run_id=run_id,
                      user_id=user_id)
    try:
        result = _train(evolution, progress)
    except Exception as exc:
        records.finish_run(run_id, None, error=exc)
        raise
    game = _finish_training(evolution, result, run_id, origin, profile, points, mode, candidate_count, episodes,
                            simulation_steps, title="Evolution complete.", mission=mission, start_game=start_game)
    game["training"] = _training_record(plan, evolution, result.warm_started)
    return game


def _training_record(plan, evolution, warm_started=0):
    """What a run returns of its settings: its overrides, where each value came from, the values it
    was resolved to, and those its evolution ran with (read back from it), with the number of
    storytellers it really started from (``warm_started``: the summary before it only counted the
    memory, ``remembered``)."""
    return {"overrides": plan["overrides"], "sources": plan["sources"], "resolved": plan["effective"],
            "remembered": plan.get("remembered"),
            "executed": {**executed_training(evolution), "warm_started": int(warm_started or 0)}}


def _train(evolution, progress=None, resume=None):
    """Run ``evolution`` (its world active): a generation's stories, and its champions'
    benchmark, are played by a pool of processes at once (engine.story_pool), with the same
    scores as one process would give."""
    settings = evolution.settings
    stories = max(int(settings["pop_size"]) * int(settings["episodes_per_genome"]),
                  int(settings.get("benchmark_top_k", 1)) * int(settings.get("validation_episodes", 1)))
    serial = evolution.map_fn
    with engine.story_pool(stories) as map_fn:
        evolution.map_fn = map_fn or serial
        try:
            return evolution.run(progress=progress, resume=resume)
        finally:
            evolution.map_fn = serial  # the pool closes with the run


def _finish_training(evolution, result, run_id, origin, profile, points, mode, candidate_count, episodes,
                     simulation_steps, title, parent_id=None, source=None, mission=None, start_game=True):
    """Record a finished run (its generations, champions, best engine with its model
    file, and its preview story) and, unless ``start_game`` is False, start a game with the best
    storyteller. Without a game nothing is played: no session, no start or mission in the
    player's profile, no play story in the records."""
    learning = result.summary()
    genome, score, simulated = result.best_genome, result.best_score, result.best_state
    model = EngineModel.from_training(evolution, result, source=source or f"{origin} run {run_id}", parent_id=parent_id)
    records.finish_run(run_id, result, model=model, origin=origin)
    records.record_story(simulated, origin, "preview", run_id=run_id, engine_id=model.id,
                         parameters={"benchmark": True})

    token = player = scene = None
    actions = []
    if start_game:
        token, player = _start_game(genome, profile, points, mode, origin=origin, run_id=run_id, engine_id=model.id,
                                    mission=mission)
        scene, actions = _scene(player)
    history = result.history
    report = (
        f"{title} Best fitness on the benchmark: **{score:.2f}** (the training objective, not a measure of "
        "quality: judge the stories in *⚖ Evaluate*) "
        f"({learning['improvement']:+.2f} since generation 0)  \n"
        + ("Stories played by **simulated players** (they choose among the choices shown; the storyteller "
           "only directs).  \n" if evolution.settings.get("player") == "simulated"
           else "Stories played by the **storyteller itself** (evolution.player: storyteller).  \n")
        + f"Generations: **{result.generations_run}** · Storytellers per generation: **{candidate_count}** · "
        f"Stories per storyteller: **{episodes}**"
        + (f"  \nStarted from **{result.warm_started}** storyteller(s) remembered from earlier runs." if result.warm_started else "")
        + f"  \nBenchmark per generation: {' → '.join(f'{h.validation:.0f}' for h in history)}  \n"
        f"Preview path: {' → '.join(simulated.path)}  \n"
        f"Preview mission: {simulated.mission_status}  \n"
        + (f"Player profile: **{profile}** (playthroughs finished: {player.variables.get('playthroughs', 0)})  \n"
           if player is not None else "")
        + f"Engine: `{model.id}`" + (" (recorded)" if records.enabled() else "")
    )
    config_path, bundle_path = _export_run_artifacts(model, candidate_count, result.generations_run, simulation_steps)
    _remember_engine(model, bundle_path, run_id)
    with SESSION_LOCK:
        RUNS[run_id] = {"learning": learning, "token": token, "engine_id": model.id,
                        "checkpoint": evolution.last_checkpoint}
        while len(RUNS) > MAX_RUNS:
            RUNS.popitem(last=False)
    return {
        "token": token, "run_id": run_id, "report": report, "scene": scene, "actions": actions, "last_turn": "",
        "config_path": config_path, "engine_path": bundle_path, "learning": learning, "engine_id": model.id,
        "game_started": token is not None,
    }


# ---- training settings: the saved engine defaults, and one run's own overrides ---------------------

# What a run may set for itself; anything it leaves out (None) is the saved engine default.
TRAINING_OVERRIDES = ("candidate_count", "generations", "episodes", "simulation_steps", "deterministic", "seed",
                      "warm_start")
MAX_STORIES_PER_STORYTELLER = 10
# A resolved field -> the override that sets it.
RESOLVED_FROM = {"population": "candidate_count", "generations": "generations", "stories_per_storyteller": "episodes",
                 "requested_steps": "simulation_steps", "deterministic": "deterministic", "seed": "seed",
                 "warm_start": "warm_start"}


def _setting_getter(source):
    """config.get, or the same over a configuration ``source`` (a dict)."""
    if source is None:
        return config.get

    def get(key, default=None):
        node = source
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node
    return get


def training_defaults(source=None) -> dict:
    """The saved engine defaults a run uses for whatever it does not set itself (⚙ Advanced →
    Simulation and the engine logic: evolution.*, reproducibility.*, narrative_end.max_actions),
    from the active configuration or ``source`` (a configuration, e.g. a recorded engine's)."""
    get = _setting_getter(source)
    evolution = evolution_settings(base=get("evolution") or {})
    seed = get("reproducibility.seed")
    configured = get("narrative_end.max_actions", MIN_STORY_LENGTH)
    return {"candidate_count": int(evolution["pop_size"]), "generations": int(evolution["generations"]),
            "episodes": int(evolution["episodes_per_genome"]), "simulation_steps": int(evolution["max_steps"]),
            "deterministic": bool(get("reproducibility.deterministic", True)),
            "seed": None if seed is None else int(seed), "warm_start": bool(evolution["warm_start"]),
            "full_length_stories": bool(evolution["full_length_stories"]),
            "story_length": max(MIN_STORY_LENGTH, int(configured)), "story_length_setting": configured}


def resolve_training(overrides=None, source=None, warm_start_from=None) -> dict:
    """One run's training settings, as it will run: the saved engine defaults (training_defaults,
    from the active configuration or ``source``) with the run's own ``overrides``
    (TRAINING_OVERRIDES; None or left out: the default), held to the app's limits, with the seed of
    a deterministic run, how long a story is, and the work against max_simulation_work.
    ``warm_start_from``: an engine id when the run starts from a recorded engine (retraining) rather
    than from the learning memory. A run executes exactly ``effective``; the studio shows it first.
    Returns {defaults, overrides, sources, effective, work, notes, errors, ready, remembered}."""
    given = {key: value for key, value in (overrides or {}).items() if key in TRAINING_OVERRIDES and value is not None}
    defaults = training_defaults(source)
    asked = {**{key: defaults[key] for key in TRAINING_OVERRIDES}, **given}
    notes, errors = [], []

    def held(field, label, limit, why):
        value = max(1, int(asked[field]))
        if value > limit:
            notes.append(f"{label}: {value} asked, held to {limit} ({why}).")
            return limit
        return value

    population = held("candidate_count", "Population", APP_SETTINGS["candidate_limit"], "app parameter candidate_limit")
    generations = held("generations", "Generations", APP_SETTINGS["generation_limit"], "app parameter generation_limit")
    steps = held("simulation_steps", "Steps per simulation", APP_SETTINGS["simulation_step_limit"],
                 "app parameter simulation_step_limit")
    episodes = held("episodes", "Stories per storyteller", MAX_STORIES_PER_STORYTELLER, "the most a storyteller plays")
    deterministic, seed = bool(asked["deterministic"]), None
    if deterministic:
        if asked["seed"] is None:
            errors.append("A deterministic run needs a seed: set one for this run, save a default seed "
                          "(⚙ Advanced → Simulation), or make the run random.")
        else:
            seed = int(asked["seed"])
    elif "seed" in given:
        notes.append(f"Seed {given['seed']} is not used: this run is random, not deterministic.")
    remembered = None
    if warm_start_from:
        warm_start, warm_mode = True, "engine"
    else:
        warm_start = bool(asked["warm_start"])
        warm_mode = "memory" if warm_start else "off"
        if warm_start:
            remembered = len(LearningMemory().load())
    work = training_work(population, generations, episodes, steps, full_length=defaults["full_length_stories"],
                         length=defaults["story_length"], configured=defaults["story_length_setting"])
    effective = {"population": population, "generations": generations, "stories_per_storyteller": episodes,
                 "requested_steps": steps, "story_turns": work["effective_max_turns"],
                 "full_length_stories": work["full_length_stories"], "story_length": work["story_length"],
                 "deterministic": deterministic, "seed": seed, "warm_start": warm_start, "warm_start_mode": warm_mode}
    sources = {name: ("override" if field in given else "default") for name, field in RESOLVED_FROM.items()}
    if warm_start_from:
        sources["warm_start"] = f"engine {warm_start_from}"
    return {"defaults": defaults, "overrides": given, "sources": sources, "effective": effective, "work": work,
            "notes": notes, "errors": errors, "ready": not errors and work["allowed"], "remembered": remembered}


def _refuse_unless_ready(plan):
    """Refuse a run its resolution says cannot start (no seed for a deterministic run, too much work)."""
    if plan["errors"]:
        raise AuthoringError(" ".join(plan["errors"]))
    if not plan["work"]["allowed"]:
        raise AuthoringError(_work_refusal(plan["work"]))


def executed_training(evolution) -> dict:
    """The configuration an evolution ran with, read back from it (the same fields as
    resolve_training's ``effective``, to compare)."""
    settings = evolution.settings
    turns, _ = story_turns(settings["max_steps"], settings["full_length_stories"])
    engine_memory = type(evolution.memory).__name__ == "ModelMemory"
    return {"population": int(settings["pop_size"]), "generations": int(settings["generations"]),
            "stories_per_storyteller": int(settings["episodes_per_genome"]), "requested_steps": int(settings["max_steps"]),
            "story_turns": turns, "full_length_stories": bool(settings["full_length_stories"]),
            "story_length": story_length(), "deterministic": evolution.seed is not None, "seed": evolution.seed,
            "warm_start": bool(settings["warm_start"]),
            "warm_start_mode": "engine" if engine_memory else ("memory" if settings["warm_start"] else "off")}


def training_work(candidates, generations, episodes, steps, full_length=None, length=None, configured=None):
    """What a run asks for, before it starts: the steps asked for and the turns a story can really
    take (core_engine.evolution.story_turns: with full-length stories every story is played to its
    end, narrative_end.max_actions, whatever the steps; otherwise the steps, though no story outlasts
    its end), why they differ, and the work: storytellers x generations x stories per storyteller x
    turns per story, at most (many stories end sooner), against the app parameter
    max_simulation_work."""
    candidates, generations, episodes, steps = (int(candidates), int(generations), int(episodes), int(steps))
    full_length = bool(evolution_settings().get("full_length_stories")) if full_length is None else bool(full_length)
    length = story_length() if length is None else int(length)
    configured = config.get("narrative_end.max_actions", length) if configured is None else configured
    turns, steps_decide = story_turns(steps, full_length, end=length)
    override = None
    if full_length:
        override = {"setting": "evolution.full_length_stories",
                    "reason": f"Full-length stories are on (evolution.full_length_stories): every story is played to "
                              f"its end, {turns} turns (narrative_end.max_actions), so the {steps} steps asked for "
                              "do not set how long a story is."}
    elif not steps_decide:
        override = {"setting": "narrative_end.max_actions",
                    "reason": f"A story ends by itself after {length} turns (narrative_end.max_actions), so no story "
                              f"takes the {steps} steps asked for: {turns} at most."}
    if override and configured != length:
        override["reason"] += f" (narrative_end.max_actions is {configured}; a story lasts at least {length} turns.)"
    work = candidates * generations * episodes * turns
    return {"work": work, "limit": MAX_SIMULATION_WORK, "allowed": work <= MAX_SIMULATION_WORK,
            "over_by": max(0, work - MAX_SIMULATION_WORK),
            "requested_steps": steps, "effective_max_turns": turns, "story_turns": turns,
            "full_length_stories": full_length, "story_length": length, "steps_decide": steps_decide,
            "override": override,
            "factors": {"storytellers": candidates, "generations": generations,
                        "stories_per_storyteller": episodes, "turns_per_story": turns}}


def _work_refusal(asked):
    """Why a run asking for ``asked`` (training_work) is refused: the multiplication, the reason for
    its story length, and what to change."""
    f = asked["factors"]
    why = f" {asked['override']['reason']}" if asked["override"] else ""
    return (f"This run requests up to {asked['work']:,} simulated turns ({f['storytellers']} storytellers x "
            f"{f['generations']} generations x {f['stories_per_storyteller']} stories x {f['turns_per_story']} turns), "
            f"{asked['over_by']:,} above the limit of {asked['limit']:,}.{why} Use fewer storytellers, generations "
            "or stories per storyteller, or raise `max_simulation_work` (⚙ Advanced → App & jobs → App parameters).")


def _training_guard(pop_size, generations, episodes, steps):
    asked = training_work(pop_size, generations, episodes, steps)
    if not asked["allowed"]:
        raise AuthoringError(_work_refusal(asked))


def continue_engine(engine_id, generations=1, profile=None, stats_choice=None, stats_points=None, origin="studio",
                    progress=None, mission=None, user_id=None, start_game=True):
    """Continue a recorded engine's training exactly where it stopped, for
    ``generations`` more generations; records the run and (unless ``start_game`` is False)
    starts a game with the result."""
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
                      run_id=run_id, user_id=user_id)
    try:
        from core_engine.evolution import ModelMemory

        evolution = NarrativeEvolution.from_checkpoint(
            engine.build_world, engine.fitness, model.checkpoint, generations=generations,
            memory=ModelMemory(model.memory) if model.memory or settings.get("warm_start") else None,
        )
        result = _train(evolution, progress, resume=model.checkpoint)
    except Exception as exc:
        records.finish_run(run_id, None, error=exc)
        raise
    return _finish_training(evolution, result, run_id, origin, profile, points, mode, settings["pop_size"],
                            settings["episodes_per_genome"], settings["max_steps"],
                            title=f"Training continued for {generations} generation(s) from engine `{engine_id}`.",
                            parent_id=engine_id, source=f"continued from {engine_id}", mission=mission,
                            start_game=start_game)


def retrain_engine(engine_id, candidate_count=None, generations=None, simulation_steps=None, episodes=None,
                   deterministic=None, profile=None, stats_choice=None, stats_points=None, origin="studio",
                   progress=None, mission=None, user_id=None, seed=None, start_game=True):
    """A new run whose first population starts from a recorded engine's storyteller
    and memory, on that engine's world; records it and (unless ``start_game`` is False) starts
    a game with the result. Its
    settings are that engine's saved defaults with what the run sets itself (resolve_training)."""
    profile = _profile(profile)
    points, mode = _player_stats(stats_choice, stats_points)
    model = _recorded_model(engine_id)
    model.activate()
    engine.refresh_runtime_settings()
    plan = resolve_training({"candidate_count": candidate_count, "generations": generations,
                             "simulation_steps": simulation_steps, "episodes": episodes, "deterministic": deterministic,
                             "seed": seed}, warm_start_from=engine_id)
    _refuse_unless_ready(plan)
    run = plan["effective"]
    candidate_count, generations, simulation_steps = run["population"], run["generations"], run["requested_steps"]
    episodes, seed = run["stories_per_storyteller"], run["seed"]
    if seed is not None:
        engine.apply_random_seed(force=True, seed=seed)
    settings = {"pop_size": candidate_count, "generations": generations, "max_steps": simulation_steps,
                "episodes_per_genome": episodes}
    run_id = uuid.uuid4().hex
    records.start_run(origin, kind="retrain", settings=settings, parameters={**settings, "profile": profile},
                      seed=seed, parent_engine_id=engine_id, run_id=run_id, user_id=user_id)
    try:
        from core_engine.evolution import ModelMemory

        memory = ModelMemory(model.memory)
        memory.remember([(model.score if model.score is not None else 0.0, model.genome)], size=max(len(model.memory) + 1, 12))
        evolution = NarrativeEvolution(engine.build_world, engine.fitness, settings={**settings, "warm_start": True},
                                       seed=seed, memory=memory)
        result = _train(evolution, progress)
    except Exception as exc:
        records.finish_run(run_id, None, error=exc)
        raise
    game = _finish_training(evolution, result, run_id, origin, profile, points, mode, candidate_count, episodes,
                            simulation_steps, title=f"Retrained from engine `{engine_id}`.", parent_id=engine_id,
                            source=f"retrained from {engine_id}", mission=mission, start_game=start_game)
    game["training"] = _training_record(plan, evolution, result.warm_started)
    return game


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
        offered = {action.name: action for action in _player_options(state)}
        if selected_action not in offered:
            raise AuthoringError("That choice is no longer available. Select a current action.")
        before = len(state.history)
        state.take_option(offered[selected_action])  # approaching someone or leaving a conversation: no turn
        turn_text = "\n\n".join(state.history[before:]).strip()
        scene, next_actions = _scene(state)
        records.update_story(SESSION_RECORDS.get(token), state)
    return scene, next_actions, turn_text


# ---- a session as the UNP sees it (the ANE AI's protocol, for play sessions) ----------------------
# The session's parameters, the player's state and what happened in the game set from outside, and
# save games (ane.savegame/1): the same as the exported ANE AI does (core_engine/resume.py,
# savegame.py), so a UNP integration can be tried in the studio and the API.

def _session(token):
    state = SESSIONS.get(token)
    if state is None:
        raise LookupError("Play session not found.")
    return state


def _parameter_errors(work):
    from core_engine import resume, savegame

    try:
        return work()
    except (resume.ParameterError, savegame.SaveGameError) as exc:
        raise AuthoringError(str(exc)) from exc


def session_parameters(token):
    """The session's parameters (ane.session_state/1): place, path, items, stats, mission stage, ..."""
    from core_engine import resume

    with SESSION_LOCK:
        state = _session(token)
        _scene(state)  # the scene as the player sees it
        return resume.parameters(state)


def update_player(token, player):
    """Set the player's stats, items, coins, health or variables, as a UNP does; returns the fields set."""
    from core_engine import resume

    with SESSION_LOCK:
        state = _session(token)
        applied = _parameter_errors(lambda: resume.apply_player(state, dict(player or {})))
        records.update_story(SESSION_RECORDS.get(token), state)
        return applied


def sync_session(token, events):
    """Take what happened in the UNP's game into the session: one event or {"events": [...]}."""
    from core_engine import resume

    events = events.get("events") if isinstance(events, dict) and "events" in events else events
    events = events if isinstance(events, list) else [events]
    with SESSION_LOCK:
        state = _session(token)
        taken = _parameter_errors(lambda: [resume.sync(state, dict(event or {})) for event in events])
        records.update_story(SESSION_RECORDS.get(token), state)
        return taken


def session_savegame(token):
    """The session's save game (ane.savegame/1): its parameters and an exact snapshot."""
    from core_engine import savegame

    with SESSION_LOCK:
        state = _session(token)
        _scene(state)
        return savegame.make(state, {"package": "studio", "created": None})


def load_savegame(doc, profile=None, session_id=None, origin="studio"):
    """Continue a save game as a new play session: exactly when its snapshot fits this engine and
    world, else rebuilt from its parameters. Its storyteller comes from the snapshot, or (a save
    game of parameters only) from the play session ``session_id``. Returns (token, how, note)."""
    from core_engine import savegame

    if not isinstance(doc, dict) or doc.get("format") != savegame.FORMAT:
        raise AuthoringError(f"Not a save game: send an {savegame.FORMAT} document.")
    traits = (doc.get("snapshot") or {}).get("genome")
    if traits:
        genome = Genome.from_traits(traits)
    else:
        with SESSION_LOCK:
            if session_id not in SESSIONS:
                raise AuthoringError("This save game has no snapshot, so no storyteller: give the `session_id` of a "
                                     "play session whose storyteller continues it.")
            genome = SESSIONS[session_id].genome
    store = VariableStore()
    state, how, note = _parameter_errors(lambda: savegame.load(doc, genome, store=store, profile=profile or None))
    if state.ended:
        raise AuthoringError("The save game's session has ended: start a new one.")
    token = _register_session(state, origin, parameters={"profile": state.profile, "savegame": how})
    return token, how, note


def world_catalogue():
    """The world as the ANE AI sends it to a UNP (ane.world/1): every entity with all its fields."""
    from narrative_runtime.world import starting_locations, world_catalogue as catalogue

    active = config.as_dict()
    return catalogue(active, starting_locations(active), revision=0, engine="studio")


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
    from core_engine import players

    state = model.new_game(seed=seed)
    engine.refresh_runtime_settings()
    players.play(state, 10 ** 6, players.for_settings(seed, evolution_settings()))  # a simulated player
    return records.record_story(state, "import", "preview", engine_id=engine_id, seed=seed,
                                parameters={"preview": True, "seed": seed})


def import_folder() -> dict:
    """The server's folder of earlier engines and games to import (ANE_ARTIFACT_DIR, else the
    project's artifacts/): {folder, files (the names waiting there)}."""
    folder = Path(os.getenv("ANE_ARTIFACT_DIR") or Path(__file__).resolve().parent.parent / "artifacts")
    files = sorted(p.name for p in folder.iterdir()
                   if p.is_file() and p.suffix.lower() in (".zip", ".yaml", ".yml")) if folder.is_dir() else []
    return {"folder": str(folder), "files": files}


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
