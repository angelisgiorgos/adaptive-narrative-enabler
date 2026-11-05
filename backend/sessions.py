"""Play sessions: engine bundles, games created by the evolutionary AI, turns and play state."""
from __future__ import annotations

import collections
import copy
import math
import random
import tempfile
import threading
import uuid
import zipfile
from numbers import Number
from pathlib import Path
import yaml
import main as engine
from core_engine import GameState, Genome, Mission
from core_engine.variables import VariableStore
from core_engine.evolution import LearningMemory, NarrativeEvolution
from utils.config_loader import config

from .errors import AuthoringError
from .store import APP_SETTINGS, MAX_CONFIG_UPLOAD_BYTES, MAX_ENGINE_UPLOAD_BYTES, MAX_SIMULATION_WORK, MAX_WEB_SESSIONS, _validate_complete_config
from .content import _player_stats, _profile


SESSIONS = {}

SESSION_LOCK = threading.Lock()
# Recent evolution runs (learning summaries), so any run or generation can be exported.
RUNS: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
MAX_RUNS = 20

def play_state(state: GameState) -> dict:
    """Structured snapshot of a game, for clients that render their own UI."""
    return {
        "location": state.current.name,
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
    }

def _export_run_artifacts(genome, score, candidate_count, generations, simulation_steps, learning=None):
    exported = config.as_dict()
    exported["generated_run"] = {
        "best_score": float(score),
        "candidate_count": int(candidate_count),
        "generations": int(generations),
        "simulation_steps": int(simulation_steps),
        "genome": {key: float(value) for key, value in vars(genome).items()},
    }
    if learning:
        exported["generated_run"]["learning"] = learning
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".yaml",
        prefix="narrative-config-",
        delete=False,
    ) as stream:
        yaml.safe_dump(exported, stream, sort_keys=False, allow_unicode=True)
        config_path = stream.name

    with tempfile.NamedTemporaryFile(
        suffix=".zip", prefix="narrative-engine-", delete=False
    ) as archive_stream:
        bundle_path = archive_stream.name
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(config_path, "config.yaml")
        archive.writestr(
            "best_model.yaml",
            yaml.safe_dump(exported["generated_run"], sort_keys=False),
        )
        archive.writestr(
            "README.txt",
            "Adaptive Narrative model bundle containing the active configuration and "
            "winning evolved parameters. Upload it in the app to play without evolving again.\n",
        )
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

def _start_game(genome, profile, points, mode):
    """A playable game for the genome; variable changes are saved to the profile."""
    player = GameState(engine.build_world(genome), genome, store=VariableStore(), profile=profile,
                       stats=points, stats_mode=mode)
    token = uuid.uuid4().hex
    with SESSION_LOCK:
        while len(SESSIONS) >= MAX_WEB_SESSIONS:
            SESSIONS.pop(next(iter(SESSIONS)))
        SESSIONS[token] = player
    return token, player


def load_best_engine(uploaded_file, model_parameters_file=None, profile=None, stats_choice=None, stats_points=None):
    """Start a game with a previously evolved storyteller (no evolution)."""
    profile = _profile(profile)
    points, mode = _player_stats(stats_choice, stats_points)
    config_data, genome = _read_engine_artifact(uploaded_file, model_parameters_file)
    config.use_override(config_data)
    engine.refresh_runtime_settings()
    token, player = _start_game(genome, profile, points, mode)
    scene, actions = _scene(player)
    run_data = config_data.get("generated_run", {})
    score = run_data.get("best_score", "unknown")
    config_path, bundle_path = _export_run_artifacts(
        genome,
        float(score) if isinstance(score, Number) else 0.0,
        run_data.get("candidate_count", 0),
        run_data.get("generations", 0),
        run_data.get("simulation_steps", 0),
        learning=run_data.get("learning"),
    )
    report = f"Saved engine loaded. Best recorded score: **{score}**. Evolution was skipped."
    return {
        "token": token, "report": report, "scene": scene, "actions": actions, "last_turn": "",
        "config_path": config_path, "engine_path": bundle_path, "learning": run_data.get("learning"),
    }


def _scene(state: GameState) -> tuple[str, list[str]]:
    actions = state.ranked_available_actions(limit=8) if not state.ended else []
    description, npcs, objects = state._describe_visible_scene()
    inventory = ", ".join(sorted(state.inventory)) or "empty"
    details = [
        f"## {state.scene_name}",
        description or "",
        f"**Health:** {state.health_player.status()} · **Coins:** {state.coins} · **Power:** {state.power:.2f}",
        f"**Inventory:** {inventory}",
    ]
    if state.mission:
        details.insert(1, f"**Mission:** {state.mission.title}" + (
            f" — {state.mission.description}" if state.mission.description else ""
        ))
    if npcs:
        details.append("**People here:** " + ", ".join(name for name, _ in npcs))
    if state.stats.names:
        bonuses = state.combat.item_bonuses(state.inventory)
        details.append("**Stats:** " + " · ".join(
            f"{name} {value}" + (f" (+{bonuses[name]})" if bonuses.get(name) else "")
            for name, value in state.stats.values(bonuses).items()
        ))
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
                   progress=None):
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
    requested_work = candidate_count * generations * simulation_steps * episodes
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
    result = evolution.run(progress=progress)
    learning = result.summary()
    genome, score, simulated = result.best_genome, result.best_score, result.best_state

    token, player = _start_game(genome, profile, points, mode)
    scene, actions = _scene(player)
    history = result.history
    report = (
        f"Evolution complete. Best benchmark score: **{score:.2f}** "
        f"({learning['improvement']:+.2f} since generation 0)  \n"
        f"Generations: **{result.generations_run}** · Storytellers per generation: **{candidate_count}** · "
        f"Stories per storyteller: **{episodes}**"
        + (f"  \nStarted from **{result.warm_started}** storyteller(s) remembered from earlier runs." if result.warm_started else "")
        + f"  \nBenchmark per generation: {' → '.join(f'{h.validation:.0f}' for h in history)}  \n"
        f"Preview path: {' → '.join(simulated.path)}  \n"
        f"Preview mission: {simulated.mission_status}  \n"
        f"Player profile: **{profile}** (playthroughs finished: {player.variables.get('playthroughs', 0)})"
    )
    config_path, bundle_path = _export_run_artifacts(
        genome, score, candidate_count, generations, simulation_steps, learning=learning,
    )
    run_id = uuid.uuid4().hex
    with SESSION_LOCK:
        RUNS[run_id] = {"learning": learning, "token": token}
        while len(RUNS) > MAX_RUNS:
            RUNS.popitem(last=False)
    return {
        "token": token, "run_id": run_id, "report": report, "scene": scene, "actions": actions, "last_turn": "",
        "config_path": config_path, "engine_path": bundle_path, "learning": learning,
    }


def learning_memory() -> dict:
    """The storytellers the AI remembers from earlier runs (hall of fame)."""
    entries = LearningMemory().load()
    return {"hall_of_fame": entries, "size": len(entries), "path": LearningMemory().path}


def forget_learning() -> dict:
    LearningMemory().forget()
    return learning_memory()


def play_action(token: str, selected_action: str):
    """Take one action; returns (scene, next action names, text of the turn)."""
    with SESSION_LOCK:
        state = SESSIONS.get(token)
        if state is None:
            raise AuthoringError("Run a simulation first to start a game.")
        actions = state.ranked_available_actions(limit=8)
        names = [action.name for action in actions]
        if selected_action not in names:
            raise AuthoringError("That choice is no longer available. Select a current action.")
        before = len(state.history)
        state.step(names.index(selected_action))
        turn_text = "\n\n".join(state.history[before:]).strip()
        scene, next_actions = _scene(state)
    return scene, next_actions, turn_text