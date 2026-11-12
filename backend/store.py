"""Configuration storage and validation: settings, weights, hyperparameters, story rules."""
from __future__ import annotations

import math
import os
import re
import tempfile
import threading
from numbers import Number
from pathlib import Path
import yaml
import main as engine
from core_engine import Mission
from core_engine.actions import REPEAT_RULES
from core_engine import rules, starts
from core_engine.evolution import AGGREGATES
from core_engine.builder import logic_errors
from core_engine.narration import validate_messages
from core_engine.variables import PROFILE_PATTERN, validate_definitions
from utils.config_loader import config

from .errors import AuthoringError


PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_DIR = PROJECT_ROOT / "config"

OPTIONAL_CONFIG_PATHS = {
    "world_definition.events",
    "authored_events",
    "action_selection.event_trigger_weight",
    "world_definition.missions",
    "mission_settings",
    "mission_settings.route_weight",
    "mission_settings.discovery_route_cost",
    "mission_settings.pursuit_start_step",
    "mission_settings.pursuit_full_step",
    "action_selection.mission_goal_weight",
    "fitness.base.mission_win_bonus",
    "fitness.base.mission_win_min_steps",
    "genome_bounds.goal_bias",
    "game_state.max_health",
    "game_state.arrest_location",
    "location_constraints.Dungeon.lock_when",
    "game_state.auto_travel_on_spawn",
    "game_state.auto_travel_on_spawn_threshold",
    "genome_bounds.novelty_bias",
    "action_selection.repetition_penalty",
    "outcome_objective.repetition_penalty",
    "fitness.variety",
    "action_rules",
    "messages",
    "world_definition.variables",
    "persistence",
    "world_definition.character_stats",
    "skill_check_resolution",
    "power_model.stat_weight",
    "action_selection.skill_fit_weight",
    "evolution.tournament_size",
    "evolution.max_mutation_step",
    "evolution.stagnation_generations",
    "evolution.early_stop_generations",
    "evolution.warm_start",
    "evolution.hall_of_fame_size",
    "evolution.validation_episodes",
    "fitness.npc_interaction_cap",
    "scene.visibility",
    "game_state.commit_discovery_weight",
    "game_state.commit_revisit_penalty",
    "transition_objective.story_phases",
    "spawn_selection.story_phases",
    "spawn_selection.fallback_similarity_threshold",
    "spawn_selection.fallback_candidates",
    "discovery_nodes",
    "npc_leads",
    "unexpected_events.objective",
    "outcome_resolution.probability_floor",
    "outcome_resolution.probability_ceiling",
    "start_selection",
    "story_rules",
    "unexpected_events.coin_budget",
    "unexpected_events.effect_repeat_penalty",
    "unexpected_events.name_location",
    "fitness.base.unfinished_penalty",
    "fitness.repetition",
    "fitness.pacing",
    "fitness.length_normalization",
    "evolution.full_length_stories",
    "evolution.stratify_episodes",
    "evolution.score_aggregate",
    "evolution.consistency_weight",
    # Added with the director and the engagement-based score; older configurations use the defaults.
    "evolution.min_diversity",
    "evolution.max_immigrant_rate",
    "evolution.cover_situations",
    "evolution.benchmark_top_k",
    "fitness.base.action_limit_penalty",
    "fitness.base.stage_bonus",
    "fitness.length_normalization.mode",
    "fitness.length_normalization.floor_steps",
    "fitness.engagement",
    "skill_check_resolution.failure_damage",
    "skill_check_resolution.dangerous_failure_damage",
    "director",
    "world_definition.story_map",
    "mission_selection",
    "player_options",
    # Added with maps and relevant choices.
    "world_definition.routes",
    "action_selection.heading_weight",
    "action_selection.heading_turns",
    "location_constraints.Dungeon.entry",
    "player_options.min",
    "player_options.relevance",
    "player_options.focus_relevance",
    "player_options.travel_limit",
    "player_options.talk_limit",
    "player_options.focus_limit",
    "player_options.tense_tags",
    "player_options.relevance_filter",
}

CONFIG_LOCK = threading.RLock()

# ---- the studio's app parameters (config/ui_settings.yml; GET|PUT /v1/app-settings) ----------

APP_SETTINGS_PATH = CONFIG_DIR / "ui_settings.yml"
APP_SETTINGS_LOCK = threading.RLock()
# The largest run the REST API accepts (POST /v1/simulations, the jobs, continue and
# retrain); the studio's slider limits below may be set up to these.
RUN_LIMITS = {"candidate_limit": 400, "generation_limit": 1000, "simulation_step_limit": 400}
APP_DEFAULTS = {
    "max_web_sessions": 100,
    "max_simulation_work": 60000,  # an upper bound: whole stories are counted at narrative_end.max_actions
    "max_config_upload_bytes": 2 * 1024 * 1024,
    "max_engine_upload_bytes": 20 * 1024 * 1024,
    "candidate_limit": RUN_LIMITS["candidate_limit"],
    "generation_limit": RUN_LIMITS["generation_limit"],
    "simulation_step_limit": RUN_LIMITS["simulation_step_limit"],
    "default_candidates": 8,
    "default_generations": 5,
    "default_simulation_steps": 16,
    "crossover_probability": 0.75,
    "queue_concurrency": 2,
    "queue_max_size": 24,
    "server_host": "0.0.0.0",
    "server_port": 7860,
    "share": False,
}


def validate_app_settings(values):
    if not isinstance(values, dict) or set(values) != set(APP_DEFAULTS):
        raise ValueError("App settings must contain exactly the supported parameter names.")
    for key, default in APP_DEFAULTS.items():
        value = values[key]
        if isinstance(default, bool):
            valid = isinstance(value, bool)
        elif isinstance(default, int):
            valid = type(value) is int and value > 0
        elif isinstance(default, float):
            valid = type(value) in (int, float) and 0 <= value <= 1
        else:
            valid = isinstance(value, str) and bool(value.strip())
        if not valid:
            raise ValueError(f"Invalid value for {key}.")
    if values["server_port"] > 65535:
        raise ValueError("Server port must be between 1 and 65535.")
    for limit, maximum in RUN_LIMITS.items():
        if values[limit] > maximum:
            raise ValueError(f"{limit} must not exceed {maximum}, the largest run the API accepts.")
    for default, limit in (("default_candidates", "candidate_limit"),
                           ("default_generations", "generation_limit"),
                           ("default_simulation_steps", "simulation_step_limit")):
        if values[default] > values[limit]:
            raise ValueError(f"{default} must not exceed {limit}.")
    work = values["default_candidates"] * values["default_generations"] * values["default_simulation_steps"]
    if work > values["max_simulation_work"]:
        raise ValueError("Default simulation exceeds max_simulation_work.")
    return values


def load_app_settings():
    with APP_SETTINGS_LOCK:
        saved = yaml.safe_load(APP_SETTINGS_PATH.read_text()) if APP_SETTINGS_PATH.exists() else {}
        return validate_app_settings({**APP_DEFAULTS, **(saved or {})})


def save_app_settings(values):
    """Validate and save the app parameters; they apply when the studio and the API restart."""
    validate_app_settings(values)
    with APP_SETTINGS_LOCK:
        _write_yaml(APP_SETTINGS_PATH, values)


APP_SETTINGS = load_app_settings()

MAX_WEB_SESSIONS = APP_SETTINGS["max_web_sessions"]

MAX_SIMULATION_WORK = APP_SETTINGS["max_simulation_work"]

MAX_CONFIG_UPLOAD_BYTES = APP_SETTINGS["max_config_upload_bytes"]

MAX_ENGINE_UPLOAD_BYTES = APP_SETTINGS["max_engine_upload_bytes"]

WEIGHT_KEYWORDS = (
    "weight", "bonus", "penalt", "threshold", "chance", "rate",
    "mitigation", "floor", "power",
)

def _read_yaml(path: Path) -> dict:
    with CONFIG_LOCK:
        with path.open("r", encoding="utf-8") as stream:
            return yaml.safe_load(stream) or {}

def _write_yaml(path: Path, data: dict) -> None:
    with CONFIG_LOCK:
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temp_name = stream.name
                yaml.safe_dump(data, stream, sort_keys=False, allow_unicode=True)
            os.replace(temp_name, path)
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)

def _lines(value) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]

def _weight_entries():
    entries = []

    def visit(value, parts=()):
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, parts + (str(key),))
        elif (
            isinstance(value, Number)
            and not isinstance(value, bool)
            and any(keyword in part.lower() for part in parts for keyword in WEIGHT_KEYWORDS)
        ):
            entries.append((".".join(parts), value))

    visit(_read_yaml(CONFIG_DIR / "hyperparameters.yaml"))
    return entries

def save_weights(paths, *values):
    if len(paths) != len(values):
        raise AuthoringError("The weights form is out of sync; reload the app and try again.")
    path = CONFIG_DIR / "hyperparameters.yaml"
    data = _read_yaml(path)
    for dotted_path, value in zip(paths, values):
        if value is None or not math.isfinite(float(value)):
            raise AuthoringError(f"`{dotted_path}` must be a finite number.")
        keys = dotted_path.split(".")
        target = data
        for key in keys[:-1]:
            target = target[key]
        old_value = target[keys[-1]]
        target[keys[-1]] = int(value) if isinstance(old_value, int) else float(value)
    _write_yaml(path, data)
    config.clear_override()
    engine.refresh_runtime_settings()
    return f"Saved {len(paths)} configured weights and scoring controls."

# Mappings whose keys are chosen by the writer (location names, stat names, ...).
FREE_FORM_PATHS = {
    "location_constraints", "scene.location_npc_tags", "action_selection.location_penalties",
    "genome_bounds", "action_rules.default_repeat",
}


def _validate_complete_config(candidate: object, reference: object, path: str = "", strict: bool = False) -> list[str]:
    """Return useful schema errors by comparing an upload with the shipped config.

    ``strict`` (the engine-logic editor) also rejects missing and unknown keys,
    except in mappings whose keys the writer chooses (FREE_FORM_PATHS)."""
    errors = []
    label = path or "configuration"
    if path == "reproducibility.seed" and candidate is None:
        return []
    # These collections contain user-authored story content, so their entry names
    # must not be compared with the example world bundled with the project.
    if path == "world_definition.locations":
        if not isinstance(candidate, dict) or not candidate:
            return ["`world_definition.locations` must be a non-empty mapping."]
        for name, entry in candidate.items():
            if not str(name).strip() or not isinstance(entry, dict):
                errors.append("Every location needs a name and a mapping/object value.")
            else:
                errors.extend(starts.location_errors(entry, f"Location `{name}`"))
        return errors
    if path == "start_selection" and isinstance(candidate, dict):
        errors.extend(starts.validate_settings(starts.settings({"start_selection": candidate})))
    if path == "story_rules" and isinstance(candidate, dict):
        errors.extend(rules.validate_settings(rules.settings({"story_rules": candidate})))
    if path == "evolution.score_aggregate" and candidate not in AGGREGATES:
        errors.append(f"`evolution.score_aggregate` must be one of {', '.join(AGGREGATES)}.")
    if path == "messages":
        return validate_messages(candidate)
    if path == "world_definition.variables":
        return validate_definitions(candidate)
    if path == "action_rules.default_repeat":
        if not isinstance(candidate, dict):
            return ["`action_rules.default_repeat` must be a mapping of action source to repeat rule."]
        return [
            f"`action_rules.default_repeat.{source}` must be one of: once, per_visit, always."
            for source, rule in candidate.items() if rule not in REPEAT_RULES
        ]
    if path == "world_definition.missions":
        if not isinstance(candidate, dict):
            return ["`world_definition.missions` must be a mapping."]
        for name, entry in candidate.items():
            stages = entry.get("stages") if isinstance(entry, dict) else None
            goals = [stage.get("goal") if isinstance(stage, dict) else None for stage in stages or []]
            if not isinstance(entry, dict) or (not goals and (not isinstance(entry.get("goal"), dict) or not entry["goal"])):
                errors.append(f"Mission `{name}` needs a `goal` mapping (or `stages`).")
                continue
            for goal in [entry.get("goal")] * ("goal" in entry) + goals:
                if not isinstance(goal, dict) or not goal:
                    errors.append(f"Mission `{name}`: every stage needs a `goal` mapping.")
                elif unknown := sorted(set(goal) - set(Mission.GOAL_KEYS)):
                    errors.append(f"Mission `{name}` has unknown goal keys: {', '.join(unknown)}.")
        return errors
    if path == "world_definition.events":
        if not isinstance(candidate, dict):
            return ["`world_definition.events` must be a mapping."]
        for name, entry in candidate.items():
            if not str(name).strip() or not isinstance(entry, dict):
                errors.append("Every event needs a name and a mapping/object value.")
        return errors
    if path in {"world_definition.characters", "world_definition.objects"}:
        if not isinstance(candidate, list) or not candidate:
            return [f"`{path}` must be a non-empty list."]
        for index, entry in enumerate(candidate):
            if not isinstance(entry, dict):
                errors.append(f"`{path}[{index}]` must be a mapping/object.")
            elif not str(entry.get("name", "")).strip():
                errors.append(f"`{path}[{index}].name` is required.")
            elif "unique" in entry and not isinstance(entry["unique"], bool):
                errors.append(f"`{path}[{index}].unique` must be true or false.")
            elif "encounter_repeat" in entry and not isinstance(entry["encounter_repeat"], bool):
                errors.append(f"`{path}[{index}].encounter_repeat` must be true or false.")
            elif entry.get("encounter_event") is not None and not isinstance(entry["encounter_event"], str):
                errors.append(f"`{path}[{index}].encounter_event` must be an event name.")
        return errors
    if isinstance(reference, dict):
        if not isinstance(candidate, dict):
            return [f"`{label}` must be a mapping/object."]
        free_form = path in FREE_FORM_PATHS
        for key, expected in reference.items():
            child_path = f"{path}.{key}" if path else key
            if key not in candidate:
                if not free_form and (strict or child_path not in OPTIONAL_CONFIG_PATHS):
                    errors.append(f"Missing `{child_path}`.")
            else:
                errors.extend(_validate_complete_config(candidate[key], expected, child_path, strict))
        if strict and not free_form:
            for key in candidate:
                if key not in reference:
                    errors.append(f"Unknown setting `{path}.{key}`." if path else f"Unknown setting `{key}`.")
    elif isinstance(reference, list):
        if not isinstance(candidate, list):
            errors.append(f"`{label}` must be a list.")
    elif reference is not None:
        valid = (
            isinstance(candidate, Number) and not isinstance(candidate, bool)
            if isinstance(reference, Number) and not isinstance(reference, bool)
            else isinstance(candidate, type(reference))
        )
        if not valid:
            errors.append(f"`{label}` must be {type(reference).__name__}, not {type(candidate).__name__}.")
    return errors

WORLD_CONTENT = ("locations", "characters", "objects", "events", "missions", "variables", "routes")
LOCATION_KEYED = (("location_constraints",), ("action_selection", "location_penalties"))


def complete_config(cfg):
    """(configuration, errors, filled): ``cfg`` with the tuning it lacks taken from the
    built-in configuration (its own values win), so a file may hold only a world. Its
    world content is kept as written; tuning keyed by places of the built-in world that
    this world lacks is dropped; places the tuning must name (the arrest location, the
    default start) have to exist in this world."""
    import copy

    def merge(default, own):
        if isinstance(default, dict) and isinstance(own, dict):
            merged = copy.deepcopy(default)
            merged.update({key: merge(default.get(key), value) for key, value in own.items()})
            return merged
        return copy.deepcopy(own)

    own = {key: value for key, value in dict(cfg).items() if key != "generated_run"}
    defaults = config.default_config()
    default_world = defaults.get("world_definition") or {}
    defaults["world_definition"] = {k: v for k, v in default_world.items() if k not in WORLD_CONTENT}
    merged = merge(defaults, own)
    filled = sorted(set(merged) - set(own))
    places = set(((merged.get("world_definition") or {}).get("locations") or {}))
    errors = []
    if places and places != set(default_world.get("locations") or {}):
        for path in LOCATION_KEYED:
            parent = merged
            for key in path[:-1]:
                parent = parent.get(key) if isinstance(parent.get(key), dict) else {}
            mapping = parent.get(path[-1])
            if isinstance(mapping, dict):
                parent[path[-1]] = {name: rule for name, rule in mapping.items() if name in places}
        for label, value in (("game_state.arrest_location", (merged.get("game_state") or {}).get("arrest_location")),
                             ("start_selection.default_location", (merged.get("start_selection") or {}).get("default_location"))):
            if value is not None and value not in places:
                errors.append(f"`{label}` is `{value}`, which is not a place of this world: set it to one of its locations.")
    return merged, errors, filled


def upload_config(uploaded_file):
    if not uploaded_file:
        raise AuthoringError("Choose a YAML configuration file first.")
    path = Path(uploaded_file)
    if path.suffix.lower() not in {".yaml", ".yml"}:
        raise AuthoringError("The configuration must be a .yaml or .yml file.")
    try:
        if path.stat().st_size > MAX_CONFIG_UPLOAD_BYTES:
            raise AuthoringError(f"The configuration file must be {MAX_CONFIG_UPLOAD_BYTES:,} bytes or smaller.")
        with path.open("r", encoding="utf-8") as stream:
            uploaded = yaml.safe_load(stream)
    except AuthoringError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise AuthoringError(f"Could not read the YAML configuration: {exc}") from exc

    if not isinstance(uploaded, dict):
        raise AuthoringError("The YAML document must contain a mapping/object at its root.")
    if "world_definition" not in uploaded:
        raise AuthoringError("The configuration needs a `world_definition`.")
    uploaded, errors, filled = complete_config(uploaded)  # a world-only file gets the built-in tuning
    errors += _validate_complete_config(uploaded, config.default_config())
    if not errors:  # every condition and effect list, against the uploaded world itself
        errors = logic_errors(uploaded.get("world_definition"))
    if errors:
        preview = "\n".join(f"- {error}" for error in errors[:20])
        remainder = len(errors) - 20
        if remainder > 0:
            preview += f"\n- …and {remainder} more problem(s)."
        raise AuthoringError("The configuration is incomplete or invalid:\n" + preview)

    config.use_override(uploaded)
    engine.refresh_runtime_settings()
    note = f" Taken from the built-in configuration: {', '.join(filled)}." if filled else ""
    return (*load_settings(), "✅ Uploaded configuration validated and activated for simulations." + note)

def use_builtin_config():
    config.clear_override()
    engine.refresh_runtime_settings()
    return (*load_settings(), "Built-in configuration restored.")

def load_settings():
    config.reload()
    return (
        config.get("evolution.pop_size", 20),
        config.get("evolution.generations", 20),
        config.get("evolution.episodes_per_genome", 3),
        config.get("evolution.max_steps", 60),
        config.get("game_state.initial_health", 8),
        config.get("game_state.initial_coins", 5),
        max(100, config.get("narrative_end.max_actions", 100)),
        config.get("reproducibility.deterministic", True),
        config.get("reproducibility.seed", 42),
        config.get("game_state.max_health", 10),
    )

def save_settings(pop_size, generations, episodes, max_steps, health, coins, max_actions, deterministic, seed,
                  max_health=None):
    if any(value is None for value in (pop_size, generations, episodes, max_steps, health, coins, max_actions)):
        raise AuthoringError("Every simulation setting needs a numeric value.")
    if max_health is None:
        max_health = config.get("game_state.max_health", 10)
    if int(health) > int(max_health):
        raise AuthoringError(f"Starting health ({int(health)}) cannot exceed maximum health ({int(max_health)}).")
    if deterministic and seed is None:
        raise AuthoringError("A numeric seed is required when deterministic reproducibility is enabled.")
    path = CONFIG_DIR / "hyperparameters.yaml"
    with CONFIG_LOCK:
        data = _read_yaml(path)
        data.setdefault("evolution", {}).update(
            pop_size=max(1, int(pop_size)),
            generations=max(1, int(generations)),
            episodes_per_genome=max(1, int(episodes)),
            max_steps=max(1, int(max_steps)),
        )
        data.setdefault("game_state", {}).update(
            initial_health=max(1, int(health)),
            max_health=max(1, int(max_health)),
            initial_coins=max(0, int(coins)),
        )
        data.setdefault("narrative_end", {})["max_actions"] = max(100, int(max_actions))
        data.setdefault("reproducibility", {}).update(
            deterministic=bool(deterministic),
            seed=int(seed) if deterministic else None,
        )
        _write_yaml(path, data)
        config.clear_override()
        config.reload()
        engine.refresh_runtime_settings()
    return "Simulation settings saved to `hyperparameters.yaml`."

HYPERPARAMETERS_PATH = CONFIG_DIR / "hyperparameters.yaml"

def load_hyperparameters_yaml():
    return (CONFIG_DIR / "hyperparameters.yaml").read_text(encoding="utf-8")

def save_all_hyperparameters(yaml_text: str):
    path = CONFIG_DIR / "hyperparameters.yaml"
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise AuthoringError(f"Invalid hyperparameter YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise AuthoringError("Hyperparameters must be a YAML mapping/object.")
    errors = _validate_complete_config(data, _read_yaml(path))
    if errors:
        preview = "\n".join(f"- {error}" for error in errors[:20])
        raise AuthoringError("Hyperparameters are incomplete or invalid:\n" + preview)
    data.setdefault("narrative_end", {})["max_actions"] = max(
        100, int(data["narrative_end"]["max_actions"])
    )
    reproducibility = data.setdefault("reproducibility", {})
    if reproducibility.get("deterministic", True) and reproducibility.get("seed") is None:
        raise AuthoringError("`reproducibility.seed` is required in deterministic mode.")
    with CONFIG_LOCK:
        _write_yaml(path, data)
        config.clear_override()
        engine.refresh_runtime_settings()
    return (*load_settings(), load_hyperparameters_yaml(), "All hyperparameters validated and saved.")

RULE_CHOICES = {
    "repeat": list(REPEAT_RULES),
    "arrest_lock_when": ["sent", "always", "never"],
    "save_on": ["change", "end"],
    "simulate_with": ["defaults", "stored"],
}

def load_rules() -> dict:
    """The story rules spread over hyperparameters.yaml, as one flat mapping."""
    arrest = config.get("game_state.arrest_location", "Dungeon")
    constraint = (config.get("location_constraints") or {}).get(arrest, {})
    repeat = {"location": "per_visit", "npc": "per_visit", "item": "once", "event": "always",
              **(config.get("action_rules.default_repeat") or {})}
    return {
        "missions_enabled": bool(config.get("mission_settings.enabled", True)),
        "end_on_goal": bool(config.get("mission_settings.end_on_goal", True)),
        "pursuit_start_step": int(config.get("mission_settings.pursuit_start_step", 12)),
        "pursuit_full_step": int(config.get("mission_settings.pursuit_full_step", 30)),
        "route_weight": float(config.get("mission_settings.route_weight", 6.0)),
        "repeat_location": repeat["location"],
        "repeat_npc": repeat["npc"],
        "repeat_item": repeat["item"],
        "repeat_event": repeat["event"],
        "arrest_location": arrest,
        "arrest_lock_when": constraint.get("lock_when", "sent") if constraint.get("requires_escape") else "never",
        "auto_travel_on_spawn": bool(config.get("game_state.auto_travel_on_spawn", True)),
        "auto_travel_on_spawn_threshold": float(config.get("game_state.auto_travel_on_spawn_threshold", 1.5)),
        "profile": config.get("persistence.profile", "default"),
        "save_on": config.get("persistence.save_on", "change"),
        "simulate_with": config.get("persistence.simulate_with", "defaults"),
        "fallback_action": config.get("authored_events.fallback_action", "Step away from the situation"),
        "fallback_description": config.get("authored_events.fallback_description", "You step back and let the moment pass."),
    }

def save_rules(rules: dict) -> str:
    rules = {**load_rules(), **{k: v for k, v in rules.items() if v is not None}}
    errors = []
    for field in ("repeat_location", "repeat_npc", "repeat_item", "repeat_event"):
        if rules[field] not in RULE_CHOICES["repeat"]:
            errors.append(f"`{field}` must be one of {', '.join(REPEAT_RULES)}.")
    for field in ("arrest_lock_when", "save_on", "simulate_with"):
        if rules[field] not in RULE_CHOICES[field]:
            errors.append(f"`{field}` must be one of {', '.join(RULE_CHOICES[field])}.")
    if rules["arrest_location"] not in (config.get("world_definition.locations") or {}):
        errors.append(f"Arrest location `{rules['arrest_location']}` does not exist.")
    try:
        start, full = int(rules["pursuit_start_step"]), int(rules["pursuit_full_step"])
        if start < 0 or full <= start:
            errors.append("Goal pursuit must start at step 0 or later and reach full strength after it starts.")
        float(rules["route_weight"]), float(rules["auto_travel_on_spawn_threshold"])
    except (TypeError, ValueError):
        errors.append("Pacing steps must be whole numbers and weights must be numbers.")
    if not re.match(PROFILE_PATTERN, str(rules["profile"])):
        errors.append("The default player profile may use letters, digits, `_`, `-` and `.` (up to 64).")
    if not str(rules["fallback_action"]).strip():
        errors.append("The fallback action needs a name.")
    if errors:
        raise AuthoringError("The story rules are invalid:\n" + "\n".join(f"- {e}" for e in errors))

    path = HYPERPARAMETERS_PATH
    with CONFIG_LOCK:
        data = _read_yaml(path)
        data.setdefault("mission_settings", {}).update(
            enabled=bool(rules["missions_enabled"]), end_on_goal=bool(rules["end_on_goal"]),
            pursuit_start_step=int(rules["pursuit_start_step"]), pursuit_full_step=int(rules["pursuit_full_step"]),
            route_weight=float(rules["route_weight"]),
        )
        data.setdefault("action_rules", {})["default_repeat"] = {
            "location": rules["repeat_location"], "npc": rules["repeat_npc"],
            "item": rules["repeat_item"], "event": rules["repeat_event"],
        }
        data.setdefault("game_state", {}).update(
            arrest_location=rules["arrest_location"],
            auto_travel_on_spawn=bool(rules["auto_travel_on_spawn"]),
            auto_travel_on_spawn_threshold=float(rules["auto_travel_on_spawn_threshold"]),
        )
        constraint = data.setdefault("location_constraints", {}).setdefault(rules["arrest_location"], {})
        constraint.setdefault("escape_tags", ["escape"])
        constraint["requires_escape"] = rules["arrest_lock_when"] != "never"
        if constraint["requires_escape"]:
            constraint["lock_when"] = rules["arrest_lock_when"]
        data.setdefault("persistence", {}).update(
            profile=rules["profile"], save_on=rules["save_on"], simulate_with=rules["simulate_with"],
        )
        data.setdefault("authored_events", {}).update(
            fallback_action=str(rules["fallback_action"]).strip(),
            fallback_description=str(rules["fallback_description"]).strip(),
        )
        _write_yaml(path, data)
        config.clear_override()
        config.reload()
        engine.refresh_runtime_settings()
    return "Story rules saved to `hyperparameters.yaml`. New stories use them."

RULE_FIELDS = [
    "missions_enabled", "end_on_goal", "pursuit_start_step", "pursuit_full_step", "route_weight",
    "repeat_location", "repeat_npc", "repeat_item", "repeat_event",
    "arrest_location", "arrest_lock_when", "auto_travel_on_spawn", "auto_travel_on_spawn_threshold",
    "profile", "save_on", "simulate_with", "fallback_action", "fallback_description",
]

def load_rules_ui():
    rules = load_rules()
    return tuple(rules[field] for field in RULE_FIELDS)

def save_rules_ui(*values):
    return save_rules(dict(zip(RULE_FIELDS, values)))


# ---- engine logic: every parameter of the logic scripts, section by section ------

LOGIC_SECTIONS = {
    "evolution": "How the evolutionary AI searches: population, generations, stories per storyteller, selection, crossover, mutation, adaptive mutation, early stopping and the learning memory.",
    "genome_bounds": "The range each evolvable storyteller trait may take.",
    "fitness": "What makes a story engaging: the scores that evolution maximises (dramatic arc, contrast, setup/payoff, variety, endings), counted per turn, and the engagement terms: filler turns, loops, fading novelty and escalation.",
    "action_selection": "How the storyteller ranks the actions it can take (threat, discovery, movement, goals, novelty, skill fit, location penalties).",
    "outcome_objective": "How the storyteller picks an outcome of an action (health, coins, success chance, discovery, repetition).",
    "outcome_resolution": "How likely unchecked outcomes are to succeed, and the probability range.",
    "outcome_scoring": "Bonuses the outcome scoring gives to spawns, movement, discovery, leads and payoffs.",
    "transition_objective": "Which destinations the storyteller prefers, by story phase.",
    "spawn_selection": "Which new places are discovered, by story phase and contrast.",
    "scene": "Which NPCs and items appear in a scene.",
    "skill_check_resolution": "How hard skill checks are, over time, and how the damage-reduction stat softens combat.",
    "mission_settings": "Missions: pacing of goal pursuit and route planning.",
    "story_rules": "Rules that keep stories coherent, paced and varied: failure outcomes, text-consistent routes, holding back from the mission early, moving on instead of stalling, repetition and backtracking costs, tie-breaking, items and clue payoffs.",
    "start_selection": "Where stories start: how strongly a player's recent starts are discouraged, and the single start of worlds without starting locations.",
    "action_rules": "How often actions are offered by default (once, per visit, always).",
    "game_state": "Starting health and coins, maximum health, arrests and following new paths.",
    "power_model": "How the power indicator is derived from health and skills.",
    "narrative_end": "When a story ends (action limit, stalling).",
    "director": "How a story builds over its course: escalation of events, threats and damage, the finale before the action limit with an epilogue ending, and options that would only loop.",
    "mission_selection": "How a story's mission is drawn: a player's recent missions are drawn less often.",
    "player_options": "The choices a player sees: how many, and in which order (the scene's order, or the storyteller's ranking).",
    "location_constraints": "Prisons: which locations confine a player until an escape.",
    "unexpected_events": "Procedural unexpected events: how often, and their parts.",
    "authored_events": "Authored events: the fallback option.",
    "discovery_nodes": "What an NPC's hint reveals: a place, a person or an item.",
    "npc_leads": "When and how NPCs lead the player to places they know.",
    "persistence": "Persistent variables: player profile, when to save, what evolution plays with.",
    "reproducibility": "Seeds for reproducible runs.",
    "simulation": "Console output of the terminal story (verbose, interactive).",
    "llm_evaluator": "The optional language model that judges the final story.",
    "llm_fallback": "The public language model used when the configured one is unavailable.",
    "config_editor": "The terminal editor.",
    "editor_suggester": "Language-model suggestions in the terminal editor.",
    "world_definition": "How locations connect when no spawn rules are written (spawn strategy, similarity).",
    "augmentation": "Optional language-model expansion of the world.",
}


def _section_span(text, name):
    """(start, end) of a top-level ``name:`` block in a YAML text, or None."""
    match = re.search(rf"^{re.escape(name)}:.*$", text, flags=re.M)
    if not match:
        return None
    following = re.search(r"^[A-Za-z_][\w-]*:", text[match.end():], flags=re.M)
    return match.start(), (match.end() + following.start()) if following else len(text)


def logic_sections() -> list[dict]:
    data = _read_yaml(HYPERPARAMETERS_PATH)
    return [{"section": name, "description": LOGIC_SECTIONS.get(name, "")} for name in data]


def load_logic_section(name: str) -> str:
    """The section's text as written in hyperparameters.yaml (comments included)."""
    text = HYPERPARAMETERS_PATH.read_text(encoding="utf-8")
    span = _section_span(text, name)
    if span is None:
        raise AuthoringError(f"Unknown engine logic section `{name}`.")
    return text[span[0]:span[1]].rstrip() + "\n"


def save_logic_section(name: str, section_text: str) -> str:
    """Validate a section against the current one (same keys and value types) and
    replace it in hyperparameters.yaml, keeping every other section untouched."""
    try:
        parsed = yaml.safe_load(section_text) or {}
    except yaml.YAMLError as exc:
        raise AuthoringError(f"Could not read the YAML: {exc}") from exc
    if not isinstance(parsed, dict) or list(parsed) != [name]:
        raise AuthoringError(f"The text must contain exactly one top-level key: `{name}:`.")
    with CONFIG_LOCK:
        text = HYPERPARAMETERS_PATH.read_text(encoding="utf-8")
        span = _section_span(text, name)
        if span is None:
            raise AuthoringError(f"Unknown engine logic section `{name}`.")
        reference = (yaml.safe_load(text) or {}).get(name)
        errors = _validate_complete_config(parsed[name], reference, name, strict=True)
        if errors:
            raise AuthoringError("The engine logic is invalid:\n" + "\n".join(f"- {e}" for e in errors[:20]))
        updated = text[:span[0]] + section_text.rstrip() + "\n" + text[span[1]:]
        HYPERPARAMETERS_PATH.write_text(updated, encoding="utf-8")
        config.clear_override()
        config.reload()
        engine.refresh_runtime_settings()
    return f"Saved the `{name}` engine logic. New simulations and games use it."
