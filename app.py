"""Gradio authoring, simulation, and play UI for Adaptive Narrative Enabler."""

from __future__ import annotations

import copy
import os
import random
import tempfile
import threading
import uuid
import zipfile
import math
from pathlib import Path
from numbers import Number

import gradio as gr
import yaml

import ui_authoring
import main as engine
from core_engine import GameState, Genome, Mission
from core_engine.actions import REPEAT_RULES
from core_engine.narration import DEFAULT_MESSAGES, validate_messages
from utils.config_loader import config


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_DIR = PROJECT_ROOT / "config"
ENTITY_FILES = {
    "Locations": (CONFIG_DIR / "locations.yaml", "locations", "mapping"),
    "NPCs": (CONFIG_DIR / "npcs.yaml", "characters", "list"),
    "Items": (CONFIG_DIR / "items.yaml", "objects", "list"),
    "Events": (CONFIG_DIR / "events.yaml", "events", "mapping"),
}
# Settings added after configurations were first exported; older uploads may omit them.
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
}
COMMON_TAGS = sorted(
    set(engine.ConfigEditor.COMMON_OUTCOME_TAGS)
    | {
        "urban", "maritime", "nature", "trade", "palace", "luxury",
        "discovery", "dark", "dangerous", "weapon", "defense", "social",
    }
)
SESSIONS = {}
SESSION_LOCK = threading.Lock()
CONFIG_LOCK = threading.RLock()
APP_SETTINGS = ui_authoring.load_app_settings()
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
        raise gr.Error("The weights form is out of sync; reload the app and try again.")
    path = CONFIG_DIR / "hyperparameters.yaml"
    data = _read_yaml(path)
    for dotted_path, value in zip(paths, values):
        if value is None or not math.isfinite(float(value)):
            raise gr.Error(f"`{dotted_path}` must be a finite number.")
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


def _validate_complete_config(candidate: object, reference: object, path: str = "") -> list[str]:
    """Return useful schema errors by comparing an upload with the shipped config."""
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
        return errors
    if path == "messages":
        return validate_messages(candidate)
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
            if not isinstance(entry, dict) or not isinstance(entry.get("goal"), dict) or not entry["goal"]:
                errors.append(f"Mission `{name}` needs a `goal` mapping.")
            elif unknown := sorted(set(entry["goal"]) - set(Mission.GOAL_KEYS)):
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
        for key, expected in reference.items():
            child_path = f"{path}.{key}" if path else key
            if key not in candidate:
                if child_path not in OPTIONAL_CONFIG_PATHS:
                    errors.append(f"Missing `{child_path}`.")
            else:
                errors.extend(_validate_complete_config(candidate[key], expected, child_path))
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


def upload_config(uploaded_file):
    if not uploaded_file:
        raise gr.Error("Choose a YAML configuration file first.")
    path = Path(uploaded_file)
    if path.suffix.lower() not in {".yaml", ".yml"}:
        raise gr.Error("The configuration must be a .yaml or .yml file.")
    try:
        if path.stat().st_size > MAX_CONFIG_UPLOAD_BYTES:
            raise gr.Error(f"The configuration file must be {MAX_CONFIG_UPLOAD_BYTES:,} bytes or smaller.")
        with path.open("r", encoding="utf-8") as stream:
            uploaded = yaml.safe_load(stream)
    except gr.Error:
        raise
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise gr.Error(f"Could not read the YAML configuration: {exc}") from exc

    if not isinstance(uploaded, dict):
        raise gr.Error("The YAML document must contain a mapping/object at its root.")
    errors = _validate_complete_config(uploaded, config.default_config())
    if errors:
        preview = "\n".join(f"- {error}" for error in errors[:20])
        remainder = len(errors) - 20
        if remainder:
            preview += f"\n- …and {remainder} more problem(s)."
        raise gr.Error("The configuration is incomplete or invalid:\n" + preview)

    config.use_override(uploaded)
    engine.refresh_runtime_settings()
    return (*load_settings(), "✅ Uploaded configuration validated and activated for simulations.")


def use_builtin_config():
    config.clear_override()
    engine.refresh_runtime_settings()
    return (*load_settings(), "Built-in configuration restored.")


def _entity_records(kind: str):
    path, key, shape = ENTITY_FILES[kind]
    # events.yaml is absent from config volumes created before Events existed.
    data = _read_yaml(path) if path.exists() or kind != "Events" else {}
    records = data.setdefault("world_definition", {}).setdefault(key, {} if shape == "mapping" else [])
    return path, data, records, shape


def _entity_name_values(kind: str):
    _, _, records, shape = _entity_records(kind)
    names = list(records) if shape == "mapping" else [record.get("name", "Unnamed") for record in records]
    return names, names[0] if names else None


def _event_choices():
    return [("None", "")] + [(name, name) for name in (config.get("world_definition.events") or {})]


def _npc_fields(kind: str, unique, encounter_event, encounter_repeat):
    """Editor updates for the NPC-only options, hidden for other content types."""
    visible = kind == "NPCs"
    return (
        gr.Checkbox(value=bool(unique), visible=visible),
        gr.Dropdown(choices=_event_choices(), value=encounter_event or "", visible=visible),
        gr.Checkbox(value=bool(encounter_repeat), visible=visible),
    )


def entity_names(kind: str):
    names, value = _entity_name_values(kind)
    values = load_entity(kind, value) if value else ("", [], "", "", False, None, False)
    return gr.Dropdown(choices=names, value=value), *values[:4], *_npc_fields(kind, *values[4:])


def load_entity(kind: str, name: str):
    """Return name, tags, descriptions, goals, and the NPC options unique,
    encounter_event, and encounter_repeat (all None for other kinds)."""
    _, _, records, shape = _entity_records(kind)
    if shape == "mapping":
        record = records.get(name, {})
        descriptions = record.get("entered_descriptions") or record.get("descriptions", [])
        tags = record.get("tags", [])
    else:
        record = next((entry for entry in records if entry.get("name") == name), {})
        descriptions = record.get("descriptions", [])
        tags = record.get("associated_tags", [])
    npc = kind == "NPCs"
    return (
        record.get("name", name or ""),
        tags,
        "\n".join(descriptions),
        "\n".join(record.get("goals", [])),
        bool(record.get("unique", False)) if npc else None,
        (record.get("encounter_event") or None) if npc else None,
        bool(record.get("encounter_repeat", False)) if npc else None,
    )


def _load_entity_ui(kind: str, name: str):
    values = load_entity(kind, name)
    return (*values[:4], *_npc_fields(kind, *values[4:]))


def save_entity(kind: str, selected_name: str, name: str, tags, descriptions: str, goals: str,
                unique=None, encounter_event=None, encounter_repeat=None):
    """Save an entry. For NPCs, an option passed as None keeps its stored value
    (unique defaults to generic); ``encounter_event=""`` removes the encounter."""
    name = (name or "").strip()
    if not name:
        raise gr.Error("Name is required.")

    with CONFIG_LOCK:
        path, data, records, shape = _entity_records(kind)
        if shape == "mapping":
            if name != selected_name and name in records:
                raise gr.Error(f"An entry named `{name}` already exists. Choose a unique name.")
            record = copy.deepcopy(records.get(selected_name, {}))
            record["tags"] = list(tags or [])
            record["entered_descriptions"] = _lines(descriptions)
            record["goals"] = _lines(goals)
            if selected_name != name:
                records.pop(selected_name, None)
            records[name] = record
        else:
            index = next((i for i, entry in enumerate(records) if entry.get("name") == selected_name), None)
            if name != selected_name and any(entry.get("name") == name for entry in records):
                raise gr.Error(f"An entry named `{name}` already exists. Choose a unique name.")
            record = copy.deepcopy(records[index]) if index is not None else {}
            record.update(
                name=name,
                associated_tags=list(tags or []),
                descriptions=_lines(descriptions),
                goals=_lines(goals),
            )
            if kind == "NPCs":
                record["unique"] = bool(record.get("unique", False) if unique is None else unique)
                if encounter_event is not None:
                    events = config.get("world_definition.events") or {}
                    if encounter_event and encounter_event not in events:
                        raise gr.Error(f"Unknown encounter event `{encounter_event}`. Define it in events.yaml first.")
                    record["encounter_event"] = encounter_event or None
                if encounter_repeat is not None:
                    record["encounter_repeat"] = bool(encounter_repeat)
                if not record.get("encounter_event"):
                    record.pop("encounter_event", None)
                    record.pop("encounter_repeat", None)
            if index is None:
                records.append(record)
            else:
                records[index] = record
        _write_yaml(path, data)
        config.clear_override()
        config.reload()
        choices = list(records) if shape == "mapping" else [entry["name"] for entry in records]
    return gr.Dropdown(choices=choices, value=name), f"Saved **{name}** to `{path.name}`."


def new_entity():
    return gr.Dropdown(value=None), "", [], "", "", False, "", False


def delete_entity(kind: str, selected_name: str):
    if not selected_name:
        raise gr.Error("Select an entry to delete first.")

    with CONFIG_LOCK:
        path, data, records, shape = _entity_records(kind)
        if kind != "Events" and len(records) <= 1:
            raise gr.Error(f"At least one {kind.lower()[:-1]} must remain in the configuration.")
        if shape == "mapping":
            if selected_name not in records:
                raise gr.Error(f"`{selected_name}` no longer exists. Refresh the entry list.")
            del records[selected_name]
            choices = list(records)
        else:
            index = next((i for i, entry in enumerate(records) if entry.get("name") == selected_name), None)
            if index is None:
                raise gr.Error(f"`{selected_name}` no longer exists. Refresh the entry list.")
            records.pop(index)
            choices = [entry["name"] for entry in records]
        _write_yaml(path, data)
        config.clear_override()
        config.reload()

    next_name = choices[0]
    values = load_entity(kind, next_name)
    return (
        gr.Dropdown(choices=choices, value=next_name),
        *values[:4], *_npc_fields(kind, *values[4:]),
        f"Deleted **{selected_name}** from `{path.name}`.",
    )


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
        raise gr.Error("Every simulation setting needs a numeric value.")
    if max_health is None:
        max_health = config.get("game_state.max_health", 10)
    if int(health) > int(max_health):
        raise gr.Error(f"Starting health ({int(health)}) cannot exceed maximum health ({int(max_health)}).")
    if deterministic and seed is None:
        raise gr.Error("A numeric seed is required when deterministic reproducibility is enabled.")
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


MESSAGES_PATH = CONFIG_DIR / "messages.yaml"


def effective_messages() -> dict:
    """Every message key with its current text (customised or default)."""
    configured = config.get("messages") or {}
    return {key: configured.get(key, default) for key, default in DEFAULT_MESSAGES.items()}


def load_messages_yaml() -> str:
    if MESSAGES_PATH.exists():
        return MESSAGES_PATH.read_text(encoding="utf-8")
    return yaml.safe_dump({"messages": DEFAULT_MESSAGES}, sort_keys=False, allow_unicode=True)


def _store_messages(text: str) -> None:
    with CONFIG_LOCK:
        MESSAGES_PATH.write_text(text, encoding="utf-8")
        config.clear_override()
        config.reload()


def save_messages(messages: dict) -> str:
    """Validate and save story messages given as a mapping (used by the API)."""
    errors = validate_messages(messages)
    if errors:
        raise gr.Error("The story messages are invalid:\n" + "\n".join(f"- {e}" for e in errors))
    _store_messages(yaml.safe_dump({"messages": messages}, sort_keys=False, allow_unicode=True))
    return f"Saved {len(messages)} story message(s) to `messages.yaml`."


def save_messages_yaml(yaml_text: str):
    """Validate and save the story messages editor, keeping its comments."""
    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as exc:
        raise gr.Error(f"Could not read the YAML: {exc}") from exc
    if not isinstance(data, dict) or set(data) - {"messages"}:
        raise gr.Error("The document must contain a single top-level `messages:` mapping.")
    messages = data.get("messages") or {}
    errors = validate_messages(messages)
    if errors:
        raise gr.Error("The story messages are invalid:\n" + "\n".join(f"- {e}" for e in errors))
    _store_messages(yaml_text)
    return yaml_text, f"Saved {len(messages)} story message(s). New stories use them straight away."


def load_hyperparameters_yaml():
    return (CONFIG_DIR / "hyperparameters.yaml").read_text(encoding="utf-8")


def save_all_hyperparameters(yaml_text: str):
    path = CONFIG_DIR / "hyperparameters.yaml"
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise gr.Error(f"Invalid hyperparameter YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise gr.Error("Hyperparameters must be a YAML mapping/object.")
    errors = _validate_complete_config(data, _read_yaml(path))
    if errors:
        preview = "\n".join(f"- {error}" for error in errors[:20])
        raise gr.Error("Hyperparameters are incomplete or invalid:\n" + preview)
    data.setdefault("narrative_end", {})["max_actions"] = max(
        100, int(data["narrative_end"]["max_actions"])
    )
    reproducibility = data.setdefault("reproducibility", {})
    if reproducibility.get("deterministic", True) and reproducibility.get("seed") is None:
        raise gr.Error("`reproducibility.seed` is required in deterministic mode.")
    with CONFIG_LOCK:
        _write_yaml(path, data)
        config.clear_override()
        engine.refresh_runtime_settings()
    return (*load_settings(), load_hyperparameters_yaml(), "All hyperparameters validated and saved.")


def _export_run_artifacts(genome, score, candidate_count, generations, simulation_steps):
    exported = config.as_dict()
    exported["generated_run"] = {
        "best_score": float(score),
        "candidate_count": int(candidate_count),
        "generations": int(generations),
        "simulation_steps": int(simulation_steps),
        "genome": {key: float(value) for key, value in vars(genome).items()},
    }
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
        raise gr.Error("The artifact does not contain a valid genome mapping.")
    genome = Genome()
    expected = set(vars(genome))
    defaults = Genome.added_field_defaults()
    missing = sorted(expected - set(values) - set(defaults))
    if missing:
        raise gr.Error("The saved genome is missing: " + ", ".join(missing))
    for key in expected:
        value = values.get(key, defaults.get(key))
        if not isinstance(value, Number) or isinstance(value, bool) or not math.isfinite(float(value)):
            raise gr.Error(f"Genome parameter `{key}` must be a finite number.")
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
        raise gr.Error("Choose a generated YAML or engine ZIP first.")
    path = Path(uploaded_file)
    if path.stat().st_size > MAX_ENGINE_UPLOAD_BYTES:
        raise gr.Error(f"Engine artifacts must be {MAX_ENGINE_UPLOAD_BYTES:,} bytes or smaller.")
    try:
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                if "config.yaml" not in names:
                    raise gr.Error("The engine ZIP is missing `config.yaml`.")
                info = archive.getinfo("config.yaml")
                if info.file_size > MAX_CONFIG_UPLOAD_BYTES:
                    raise gr.Error("The archived configuration is too large.")
                config_data = yaml.safe_load(archive.read("config.yaml"))
                model_name = "best_model.yaml" if "best_model.yaml" in names else "best_genome.yaml"
                if model_name in names and archive.getinfo(model_name).file_size > 100_000:
                    raise gr.Error("The archived genome metadata is too large.")
                genome_doc = (
                    yaml.safe_load(archive.read(model_name))
                    if model_name in names
                    else config_data.get("generated_run", {}) if isinstance(config_data, dict) else {}
                )
        elif path.suffix.lower() in {".yaml", ".yml"}:
            config_data = yaml.safe_load(path.read_text(encoding="utf-8"))
            genome_doc = config_data.get("generated_run", {}) if isinstance(config_data, dict) else {}
        else:
            raise gr.Error("Upload a generated .yaml, .yml, or engine .zip file.")
    except (OSError, UnicodeError, zipfile.BadZipFile, KeyError, yaml.YAMLError) as exc:
        raise gr.Error(f"Could not read the engine artifact: {exc}") from exc

    if not isinstance(config_data, dict):
        raise gr.Error("The artifact configuration must be a mapping/object.")
    errors = _validate_complete_config(config_data, config.default_config())
    if errors:
        raise gr.Error("The artifact configuration is invalid:\n" + "\n".join(errors[:20]))
    if model_parameters_file:
        model_path = Path(model_parameters_file)
        if model_path.suffix.lower() not in {".yaml", ".yml"}:
            raise gr.Error("Model parameters must be a .yaml or .yml file.")
        try:
            if model_path.stat().st_size > 100_000:
                raise gr.Error("The model parameters file must be 100 KB or smaller.")
            model_data = yaml.safe_load(model_path.read_text(encoding="utf-8"))
        except gr.Error:
            raise
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise gr.Error(f"Could not read the model parameters: {exc}") from exc
        genome_values = _model_genome_values(model_data)
    else:
        genome_values = _model_genome_values(genome_doc)
    if genome_values is None:
        raise gr.Error(
            "No model parameters were found. Upload a generated bundle/configuration "
            "or provide a separate model parameters YAML file."
        )
    return config_data, _validated_genome(genome_values)


def load_best_engine(uploaded_file, model_parameters_file=None):
    config_data, genome = _read_engine_artifact(uploaded_file, model_parameters_file)
    config.use_override(config_data)
    engine.refresh_runtime_settings()
    player = GameState(engine.build_world(genome), genome)
    token = uuid.uuid4().hex
    with SESSION_LOCK:
        while len(SESSIONS) >= MAX_WEB_SESSIONS:
            SESSIONS.pop(next(iter(SESSIONS)))
        SESSIONS[token] = player
    scene, actions = _scene(player)
    run_data = config_data.get("generated_run", {})
    score = run_data.get("best_score", "unknown")
    config_path, bundle_path = _export_run_artifacts(
        genome,
        float(score) if isinstance(score, Number) else 0.0,
        run_data.get("candidate_count", 0),
        run_data.get("generations", 0),
        run_data.get("simulation_steps", 0),
    )
    report = f"Saved engine loaded. Best recorded score: **{score}**. Evolution was skipped."
    return token, report, scene, gr.Radio(choices=actions, value=actions[0] if actions else None), "", config_path, bundle_path


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
    if objects:
        details.append("**Visible items:** " + ", ".join(name for name, _ in objects))
    if state.won and state.ended:
        details.append(f"### Mission complete\n{state.end_reason}")
    elif state.ended:
        details.append(f"### Game over\n{state.end_reason}")
    return "\n\n".join(details), [action.name for action in actions]


def _score_candidate(genome, simulation_steps):
    simulated = GameState(engine.build_world(genome), genome)
    for _ in range(simulation_steps):
        if not simulated.step():
            break
    return engine.fitness(simulated), genome, simulated


def run_simulation(candidate_count: int, generations: int, simulation_steps: int, deterministic: bool, progress=gr.Progress()):
    config.reload()
    engine.refresh_runtime_settings()
    candidate_count = max(1, min(int(candidate_count), APP_SETTINGS["candidate_limit"]))
    generations = max(1, min(int(generations), APP_SETTINGS["generation_limit"]))
    simulation_steps = max(1, min(int(simulation_steps), APP_SETTINGS["simulation_step_limit"]))
    requested_work = candidate_count * generations * simulation_steps
    if requested_work > MAX_SIMULATION_WORK:
        raise gr.Error(
            f"This run requests {requested_work:,} simulated turns, above the "
            f"public-app safety limit of {MAX_SIMULATION_WORK:,}. Reduce candidates, "
            "generations, or steps."
        )
    if deterministic:
        if config.get("reproducibility.seed") is None:
            raise gr.Error("Set a reproducibility seed before running deterministically.")
        engine.apply_random_seed(force=True)
    else:
        random.seed(int(uuid.uuid4().hex[:16], 16))

    population = [Genome() for _ in range(candidate_count)]
    best = None
    generation_scores = []
    for generation in progress.tqdm(range(generations), desc="Evolving narrative"):
        scored = sorted(
            (_score_candidate(genome, simulation_steps) for genome in population),
            key=lambda result: result[0],
            reverse=True,
        )
        generation_scores.append(scored[0][0])
        if best is None or scored[0][0] > best[0]:
            best = scored[0]

        survivor_count = max(2 if candidate_count > 1 else 1, candidate_count // 4)
        survivors = [result[1] for result in scored[:survivor_count]]
        next_population = [copy.deepcopy(scored[0][1])]
        while len(next_population) < candidate_count:
            if len(survivors) > 1 and random.random() < APP_SETTINGS["crossover_probability"]:
                child = Genome.crossover(random.choice(survivors), random.choice(survivors))
            else:
                child = copy.deepcopy(random.choice(survivors))
            child.mutate()
            next_population.append(child)
        population = next_population

    score, genome, simulated = best
    player = GameState(engine.build_world(genome), genome)
    token = uuid.uuid4().hex
    with SESSION_LOCK:
        while len(SESSIONS) >= MAX_WEB_SESSIONS:
            SESSIONS.pop(next(iter(SESSIONS)))
        SESSIONS[token] = player
    scene, actions = _scene(player)
    report = (
        f"Simulation complete. Best score: **{score:.2f}**  \n"
        f"Generations: **{generations}** · Candidate genomes: **{candidate_count}**  \n"
        f"Score progression: {' → '.join(f'{value:.1f}' for value in generation_scores)}  \n"
        f"Preview path: {' → '.join(simulated.path)}  \n"
        f"Preview mission: {simulated.mission_status}  \n"
        f"Unexpected events: **{len(simulated.unexpected_events)}**"
    )
    config_path, bundle_path = _export_run_artifacts(
        genome, score, candidate_count, generations, simulation_steps
    )
    return token, report, scene, gr.Radio(choices=actions, value=actions[0] if actions else None), "", config_path, bundle_path


def play_action(token: str, selected_action: str):
    with SESSION_LOCK:
        state = SESSIONS.get(token)
        if state is None:
            raise gr.Error("Run a simulation first to start a game.")
        actions = state.ranked_available_actions(limit=8)
        names = [action.name for action in actions]
        if selected_action not in names:
            raise gr.Error("That choice is no longer available. Select a current action.")
        before = len(state.history)
        state.step(names.index(selected_action))
        turn_text = "\n\n".join(state.history[before:]).strip()
        scene, next_actions = _scene(state)
    return scene, gr.Radio(
        choices=next_actions,
        value=next_actions[0] if next_actions else None,
        interactive=bool(next_actions),
    ), turn_text


CSS = """
.hero {padding: 1.25rem; border-radius: 18px; background: linear-gradient(120deg,#17233f,#4a244f); color:#fff}
.status {border-left: 4px solid #8b5cf6; padding-left: 1rem}
"""


def load_actions_editor():
    try:
        source, revision = ui_authoring.load_actions_source()
    except (OSError, UnicodeError) as exc:
        raise gr.Error(str(exc)) from exc
    return source, revision, "Loaded the latest actions.py from disk."


def save_actions_editor(source, revision):
    try:
        revision = ui_authoring.save_actions_source(source, revision)
    except (ValueError, SyntaxError, OSError) as exc:
        raise gr.Error(str(exc)) from exc
    return revision, "Saved actions.py; previous version is actions.py.bak. Restart the engine to apply. Syntax checks do not verify runtime behavior."


def save_app_parameters(*values):
    settings = dict(zip(ui_authoring.DEFAULTS, values))
    try:
        ui_authoring.save_app_settings(settings)
    except (ValueError, OSError) as exc:
        raise gr.Error(str(exc)) from exc
    return "App parameters saved. Restart the UI to apply them. ANE_UI_HOST, ANE_UI_PORT and ANE_UI_SHARE environment variables override launch settings."


def build_app():
    settings = load_settings()
    names, selected_name = _entity_name_values("Locations")
    initial_name, initial_tags, initial_descriptions, initial_goals, *_ = load_entity("Locations", selected_name)

    with gr.Blocks(title="Adaptive Narrative Studio", css=CSS) as demo:
        session_token = gr.State("")
        gr.Markdown(
            "# Adaptive Narrative Studio\n"
            "Author the world, test its narrative behavior, then play the generated game.",
            elem_classes="hero",
        )

        with gr.Tabs():
            with gr.Tab("1 · World editor"):
                with gr.Row():
                    kind = gr.Radio(list(ENTITY_FILES), value="Locations", label="Content type")
                    entity = gr.Dropdown(label="Entry", choices=names, value=selected_name)
                    add_entry = gr.Button("New entry")
                name = gr.Textbox(label="Name", value=initial_name)
                tags = gr.Dropdown(
                    COMMON_TAGS,
                    value=initial_tags,
                    multiselect=True,
                    allow_custom_value=True,
                    label="Tags",
                    info="Type a new tag and press Enter to add it.",
                )
                descriptions = gr.Textbox(
                    label="Descriptions", value=initial_descriptions, lines=5,
                    info="One description per line.",
                )
                goals = gr.Textbox(label="Goals", value=initial_goals, lines=3, info="One goal per line.")
                unique = gr.Checkbox(
                    label="Unique NPC",
                    visible=False,
                    info="Unique: lives in one location for the whole game. "
                    "Unchecked (generic, e.g. guards): may appear in every location that fits its tags.",
                )
                with gr.Row():
                    encounter_event = gr.Dropdown(
                        _event_choices(),
                        value="",
                        label="Encounter event",
                        visible=False,
                        info="Hostile NPCs: this event starts by itself when the NPC is in the scene, "
                        "before the player can act or leave.",
                    )
                    encounter_repeat = gr.Checkbox(
                        label="Repeat on every visit",
                        visible=False,
                        info="Unchecked: only the first encounter in a game.",
                    )
                with gr.Row():
                    save_entry = gr.Button("Save entry", variant="primary")
                    delete_entry = gr.Button("Delete entry", variant="stop")
                editor_status = gr.Markdown()

                npc_fields = [unique, encounter_event, encounter_repeat]
                entry_fields = [name, tags, descriptions, goals, *npc_fields]
                kind.change(entity_names, kind, [entity, *entry_fields])
                entity.change(_load_entity_ui, [kind, entity], entry_fields)
                add_entry.click(new_entity, None, [entity, *entry_fields])
                save_entry.click(
                    save_entity,
                    [kind, entity, *entry_fields],
                    [entity, editor_status],
                )
                delete_entry.click(
                    delete_entity,
                    [kind, entity],
                    [entity, *entry_fields, editor_status],
                )

            with gr.Tab("2 · Simulation settings"):
                gr.Markdown(
                    "### Complete configuration file\n"
                    "Upload one combined YAML file containing all sections from the four files in `config/`. "
                    "It is checked completely before it can replace the built-in runtime configuration."
                )
                with gr.Row():
                    config_upload = gr.File(
                        label="Combined YAML configuration",
                        file_types=[".yaml", ".yml"],
                        type="filepath",
                    )
                    with gr.Column():
                        activate_config = gr.Button("Validate & use uploaded config", variant="primary")
                        restore_config = gr.Button("Use built-in config")
                with gr.Row():
                    pop_size = gr.Number(settings[0], label="Evolution population", precision=0, minimum=1)
                    generations = gr.Number(settings[1], label="Generations", precision=0, minimum=1)
                    episodes = gr.Number(settings[2], label="Episodes / genome", precision=0, minimum=1)
                    max_steps = gr.Number(settings[3], label="Maximum simulation steps", precision=0, minimum=1)
                with gr.Row():
                    health = gr.Number(settings[4], label="Starting health", precision=0, minimum=1)
                    max_health = gr.Number(
                        settings[9], label="Maximum health", precision=0, minimum=1,
                        info="Healing never raises health above this value.",
                    )
                    coins = gr.Number(settings[5], label="Starting coins", precision=0, minimum=0)
                    max_actions = gr.Number(
                        settings[6],
                        label="Game action limit",
                        precision=0,
                        minimum=100,
                        info="The action-limit cutoff cannot be set below 100; other ending conditions still apply.",
                    )
                with gr.Row():
                    deterministic = gr.Checkbox(
                        value=settings[7],
                        label="Deterministic reproducibility",
                        info="Repeat the same evolution when the configuration and seed are unchanged.",
                    )
                    seed = gr.Number(settings[8], label="Reproducibility seed", precision=0)
                save_config = gr.Button("Save settings", variant="primary")
                config_status = gr.Markdown()
                setting_outputs = [
                    pop_size, generations, episodes, max_steps, health, coins, max_actions,
                    deterministic, seed, max_health, config_status,
                ]
                activate_config.click(upload_config, config_upload, setting_outputs)
                restore_config.click(use_builtin_config, None, setting_outputs)
                save_config.click(
                    save_settings,
                    [
                        pop_size, generations, episodes, max_steps, health, coins,
                        max_actions, deterministic, seed, max_health,
                    ],
                    config_status,
                )
                with gr.Accordion("Story messages", open=False):
                    gr.Markdown(
                        "Rewrite any sentence the engine adds to a story, e.g. `voyage: \"You head towards "
                        "the {location}.\"`. Each entry lists its `{placeholders}`; a list of texts is used "
                        "in turn. Unknown messages or placeholders are rejected."
                    )
                    messages_editor = gr.Code(value=load_messages_yaml(), language="yaml", label="messages.yaml")
                    with gr.Row():
                        save_messages_button = gr.Button("Validate & save messages", variant="primary")
                        reload_messages_button = gr.Button("Reload from disk")
                    messages_status = gr.Markdown()
                    save_messages_button.click(save_messages_yaml, messages_editor, [messages_editor, messages_status])
                    reload_messages_button.click(load_messages_yaml, None, messages_editor)
                with gr.Accordion("Advanced · all hyperparameters", open=False):
                    gr.Markdown(
                        "Edit every hyperparameter as YAML. The complete structure and value types "
                        "are validated before saving."
                    )
                    hyperparameters_editor = gr.Code(
                        value=load_hyperparameters_yaml(),
                        language="yaml",
                        label="hyperparameters.yaml",
                    )
                    save_all_config = gr.Button("Validate & save all hyperparameters")
                    advanced_status = gr.Markdown()
                    save_all_config.click(
                        save_all_hyperparameters,
                        hyperparameters_editor,
                        [
                            pop_size, generations, episodes, max_steps, health, coins,
                            max_actions, deterministic, seed, max_health, hyperparameters_editor,
                            advanced_status,
                        ],
                    )

            with gr.Tab("3 · Weights"):
                gr.Markdown(
                    "Configure every numeric weight, bonus, penalty, threshold, chance, rate, "
                    "mitigation, floor, and power control used by scoring and the narrative engine."
                )
                weight_entries = _weight_entries()
                weight_paths = [path for path, _ in weight_entries]
                weight_components = []
                grouped_weights = {}
                for weight_path, value in weight_entries:
                    grouped_weights.setdefault(weight_path.split(".")[0], []).append((weight_path, value))
                for section, section_entries in grouped_weights.items():
                    with gr.Accordion(section.replace("_", " ").title(), open=False):
                        for index in range(0, len(section_entries), 3):
                            with gr.Row():
                                for weight_path, value in section_entries[index:index + 3]:
                                    weight_components.append(
                                        gr.Number(value=value, label=weight_path)
                                    )
                save_weights_button = gr.Button("Save all weights", variant="primary")
                weights_status = gr.Markdown()
                save_weights_button.click(
                    save_weights,
                    [gr.State(weight_paths), *weight_components],
                    weights_status,
                )

            with gr.Tab("4 · Simulate & play"):
                gr.Markdown("Run a quick tournament of narrative genomes. The strongest result becomes your playable game.")
                with gr.Accordion("Load a previous best engine", open=False):
                    gr.Markdown(
                        "Upload a generated engine ZIP, or upload an existing complete configuration YAML "
                        "with its model-parameters YAML. If the configuration already contains a "
                        "`generated_run.genome`, the separate parameters file is optional."
                    )
                    with gr.Row():
                        engine_upload = gr.File(
                            label="Engine bundle or configuration",
                            file_types=[".zip", ".yaml", ".yml"],
                            type="filepath",
                        )
                        model_parameters_upload = gr.File(
                            label="Model parameters (optional)",
                            file_types=[".yaml", ".yml"],
                            type="filepath",
                        )
                        load_engine_button = gr.Button("Load best engine & start game", variant="primary")
                with gr.Row():
                    candidates = gr.Slider(1, APP_SETTINGS["candidate_limit"], value=APP_SETTINGS["default_candidates"], step=1, label="Candidate genomes")
                    generations = gr.Slider(1, APP_SETTINGS["generation_limit"], value=APP_SETTINGS["default_generations"], step=1, label="Evolution generations")
                    sim_steps = gr.Slider(1, APP_SETTINGS["simulation_step_limit"], value=APP_SETTINGS["default_simulation_steps"], step=1, label="Steps per simulation")
                deterministic_run = gr.Checkbox(
                    value=settings[7],
                    label="Run deterministically",
                    info="Uses the reproducibility seed saved in Simulation settings.",
                )
                run_button = gr.Button("Run simulation & start game", variant="primary")
                simulation_report = gr.Markdown(elem_classes="status")
                scene = gr.Markdown("Run a simulation to begin.")
                choices = gr.Radio([], label="What do you do?", interactive=True)
                choose = gr.Button("Take action", variant="primary")
                turn_result = gr.Textbox(label="Last turn", lines=7, interactive=False)
                generated_config = gr.File(
                    label="Generated story configuration",
                    interactive=False,
                    visible=True,
                )
                generated_engine = gr.File(
                    label="Best model bundle (config + evolved parameters)",
                    interactive=False,
                    visible=True,
                )

                play_outputs = [
                    session_token, simulation_report, scene, choices, turn_result,
                    generated_config, generated_engine,
                ]

                run_button.click(
                    run_simulation,
                    [candidates, generations, sim_steps, deterministic_run],
                    play_outputs,
                    concurrency_limit=1,
                    concurrency_id="narrative_evolution",
                )
                load_engine_button.click(
                    load_best_engine,
                    [engine_upload, model_parameters_upload],
                    play_outputs,
                    concurrency_limit=1,
                )
                choose.click(
                    play_action,
                    [session_token, choices],
                    [scene, choices, turn_result],
                    concurrency_limit=1,
                )

            with gr.Tab("5 · Actions & app parameters"):
                gr.Markdown(
                    "Edit action logic and app parameters, then restart to apply changes. "
                    "Python edits run with the engine's permissions; this editor is for trusted authors."
                )
                source, revision = ui_authoring.load_actions_source()
                source_revision = gr.State(revision)
                with gr.Accordion("Action logic · actions.py", open=True):
                    actions_editor = gr.Code(value=source, language="python", label="core_engine/actions.py", interactive=True)
                    with gr.Row():
                        reload_actions = gr.Button("Reload actions.py from disk")
                        save_actions = gr.Button("Validate & save actions.py", variant="primary")
                    actions_status = gr.Markdown()
                    editor_outputs = [actions_editor, source_revision, actions_status]
                    reload_actions.click(load_actions_editor, None, editor_outputs)
                    demo.load(load_actions_editor, None, editor_outputs)
                    save_actions.click(save_actions_editor, [actions_editor, source_revision], [source_revision, actions_status])
                with gr.Accordion("App parameters", open=True):
                    gr.Markdown("Saved in config/ui_settings.yml. Upload limits are in bytes; crossover probability is between 0 and 1.")
                    parameter_inputs = []
                    for key, value in ui_authoring.load_app_settings().items():
                        label = key.replace("_", " ").title()
                        if isinstance(value, bool):
                            component = gr.Checkbox(value=value, label=label)
                        elif isinstance(value, str):
                            component = gr.Textbox(value=value, label=label)
                        else:
                            component = gr.Number(value=value, label=label, precision=0 if isinstance(ui_authoring.DEFAULTS[key], int) else None)
                        parameter_inputs.append(component)
                    save_parameters = gr.Button("Save app parameters", variant="primary")
                    parameters_status = gr.Markdown()
                    save_parameters.click(save_app_parameters, parameter_inputs, parameters_status)

    return demo


demo = build_app()


if __name__ == "__main__":
    demo.queue(default_concurrency_limit=APP_SETTINGS["queue_concurrency"], max_size=APP_SETTINGS["queue_max_size"]).launch(
        server_name=os.getenv("ANE_UI_HOST", APP_SETTINGS["server_host"]),
        server_port=int(os.getenv("ANE_UI_PORT", str(APP_SETTINGS["server_port"]))),
        share=os.getenv("ANE_UI_SHARE", str(APP_SETTINGS["share"])).lower() in {"1", "true", "yes"},
    )
