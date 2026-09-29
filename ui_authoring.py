"""Persistence and validation for the studio's developer controls."""
import ast
import hashlib
import os
from pathlib import Path
import tempfile
import threading

import yaml

ROOT = Path(__file__).resolve().parent
ACTIONS_PATH = ROOT / "core_engine" / "actions.py"
SETTINGS_PATH = ROOT / "config" / "ui_settings.yml"
LOCK = threading.RLock()
DEFAULTS = {
    "max_web_sessions": 100,
    "max_simulation_work": 20000,
    "max_config_upload_bytes": 2 * 1024 * 1024,
    "max_engine_upload_bytes": 20 * 1024 * 1024,
    "candidate_limit": 50,
    "generation_limit": 25,
    "simulation_step_limit": 60,
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


def validate_settings(values):
    if not isinstance(values, dict) or set(values) != set(DEFAULTS):
        raise ValueError("App settings must contain exactly the supported parameter names.")
    for key, default in DEFAULTS.items():
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
    with LOCK:
        saved = yaml.safe_load(SETTINGS_PATH.read_text()) if SETTINGS_PATH.exists() else {}
        return validate_settings({**DEFAULTS, **(saved or {})})


def atomic_write(path, text):
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         delete=False) as stream:
            name = stream.name
            stream.write(text)
        if path.exists():
            os.chmod(name, path.stat().st_mode & 0o777)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def save_app_settings(values):
    validate_settings(values)
    with LOCK:
        atomic_write(SETTINGS_PATH, yaml.safe_dump(values, sort_keys=False))


def load_actions_source():
    with LOCK:
        source = ACTIONS_PATH.read_text(encoding="utf-8")
        return source, hashlib.sha256(source.encode()).hexdigest()


class ActionsRevisionConflict(ValueError):
    """The editor's source revision no longer matches the file on disk."""


def save_actions_source(source, revision):
    # Compile without executing author-supplied code in the running server.
    tree = ast.parse(source, filename=str(ACTIONS_PATH))
    compile(tree, str(ACTIONS_PATH), "exec")
    classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    if not {"Action", "Outcome"}.issubset(classes):
        raise ValueError("Keep both the Action and Outcome classes required by the engine.")
    with LOCK:
        previous, current_revision = load_actions_source()
        if revision != current_revision:
            raise ActionsRevisionConflict("actions.py changed since it was loaded. Reload before saving.")
        atomic_write(ACTIONS_PATH.with_suffix(".py.bak"), previous)
        atomic_write(ACTIONS_PATH, source)
        return hashlib.sha256(source.encode()).hexdigest()
