"""How the tuning parameters (the numeric weights, chances, thresholds, ... of hyperparameters.yaml that
⚙ Advanced → Weights edits) are presented and checked.

Each parameter the engine's code has been read for has its metadata in ``tuning_parameters.yaml``
(next to this file): a readable label, an explanation of what the engine does with it, its unit (each
chance on its own scale: a percentage of a 1–100 roll is not a probability), the engine's default (the
value it uses when the parameter is left out), its numeric type, and bounds where the engine itself
clamps or rejects values; ``source`` names the code that reads it. A key may hold ``*`` for one path
segment (a world's own names, e.g. ``outcome_resolution.tag_weights.*``).

A parameter without metadata keeps a fallback: a label made from its name, its path, and the numeric
type it is written with. Saving keeps a value that did not change exactly as it was written (40 stays
40, not 40.0), stores an integer parameter as an integer and refuses a fraction for it (rather than
cutting it), and refuses a value outside bounds the engine defines.
"""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

import yaml


METADATA_PATH = Path(__file__).with_name("tuning_parameters.yaml")
FIELDS = ("label", "explanation", "unit", "type", "minimum", "maximum", "default", "source")
TYPES = ("integer", "float")
# The sections of hyperparameters.yaml, as the Weights tab titles them (others: from their name).
SECTION_TITLES = {
    "skill_check_resolution": "Skill checks", "outcome_resolution": "Outcome chances",
    "outcome_scoring": "Outcome choice (storyteller)", "outcome_objective": "Outcome objective (storyteller)",
    "transition_objective": "Travel choice (storyteller)", "action_selection": "Action choice (storyteller)",
    "spawn_selection": "New places (storyteller)", "unexpected_events": "Unexpected events",
    "power_model": "Player power", "game_state": "Game state", "story_rules": "Story rules", "scene": "Scenes",
    "director": "Director", "npc_goals": "People's goals", "npc_leads": "People's leads", "evolution": "Training",
    "simulation": "Simulation", "fitness": "Story score (fitness)", "session_scope": "Session cast",
    "discovery_nodes": "Discovery", "mission_settings": "Missions", "world_definition": "World", "climax": "Climax",
    "variation": "Variation", "travel": "Travel", "stakes": "Stakes",
}


@lru_cache(maxsize=1)
def metadata() -> dict:
    """The parameters' metadata (``tuning_parameters.yaml``), checked: known fields, a known type, bounds
    in order. A broken file is an error of this release, not of the user's configuration."""
    if not METADATA_PATH.exists():
        return {}
    data = yaml.safe_load(METADATA_PATH.read_text(encoding="utf-8")) or {}
    for path, meta in data.items():
        unknown = set(meta) - set(FIELDS)
        if unknown:
            raise ValueError(f"{METADATA_PATH.name}: {path} has unknown fields {sorted(unknown)}.")
        if meta.get("type") not in TYPES:
            raise ValueError(f"{METADATA_PATH.name}: {path} needs a type, one of {TYPES}.")
        low, high = meta.get("minimum"), meta.get("maximum")
        if low is not None and high is not None and low > high:
            raise ValueError(f"{METADATA_PATH.name}: {path} has its minimum above its maximum.")
    return data


def _matches(pattern: str, path: str) -> bool:
    wanted, parts = pattern.split("."), path.split(".")
    return len(wanted) == len(parts) and all(w == "*" or w == p for w, p in zip(wanted, parts))


def find(path: str) -> dict | None:
    """The metadata of ``path``: its own, else the first pattern that matches it, else None."""
    known = metadata()
    if path in known:
        return known[path]
    return next((meta for pattern, meta in known.items() if "*" in pattern and _matches(pattern, path)), None)


def _readable(name: str) -> str:
    words = str(name).replace("_", " ").strip()
    return words[:1].upper() + words[1:]


def section_title(path: str) -> str:
    section = path.split(".")[0]
    return SECTION_TITLES.get(section, _readable(section))


def written_type(value) -> str:
    return "integer" if isinstance(value, int) and not isinstance(value, bool) else "float"


def describe(path: str, value) -> dict:
    """How a parameter is presented: {path, value, section, label, explanation, unit, type, minimum,
    maximum, default, source, described}. Without metadata (``described`` false): a label made from its
    name (with its parent's when that says more), the type it is written with, nothing invented."""
    meta = find(path)
    if meta is None:
        parts = path.split(".")
        label = _readable(parts[-1]) if len(parts) <= 2 else f"{_readable(parts[-1])} ({parts[-2].replace('_', ' ')})"
        return {"path": path, "value": value, "section": section_title(path), "label": label, "explanation": None,
                "unit": None, "type": written_type(value), "minimum": None, "maximum": None, "default": None,
                "source": [], "described": False}
    return {"path": path, "value": value, "section": section_title(path), "label": meta["label"],
            "explanation": meta.get("explanation"), "unit": meta.get("unit"), "type": meta["type"],
            "minimum": meta.get("minimum"), "maximum": meta.get("maximum"), "default": meta.get("default"),
            "source": list(meta.get("source") or []), "described": True}


def problems(path: str, value, stored) -> list[str]:
    """What is wrong with ``value`` for ``path`` (stored now as ``stored``): not a finite number, a
    fraction for an integer parameter, outside the engine's bounds."""
    info = describe(path, stored)
    name = f"{info['label']} (`{path}`)"
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return [f"{name} must be a finite number."]
    found = []
    if info["type"] == "integer" and float(value) != int(value):
        found.append(f"{name} is a whole number" + ("" if info["described"] else " (as it is written)")
                     + f": {value} is not one, and is not cut down.")
    if info["minimum"] is not None and value < info["minimum"]:
        found.append(f"{name} must be at least {info['minimum']} (the engine's bound); got {value}.")
    if info["maximum"] is not None and value > info["maximum"]:
        found.append(f"{name} must be at most {info['maximum']} (the engine's bound); got {value}.")
    return found


def stored_value(path: str, value, stored):
    """``value`` as it is written back: ``stored`` itself when the number did not change (40 stays 40,
    0.5 stays 0.5); an integer parameter as an integer; a decimal one as a decimal, kept a whole number
    when it is written as one and stays whole."""
    if not isinstance(value, bool) and isinstance(value, (int, float)) and float(value) == float(stored):
        return stored
    if describe(path, stored)["type"] == "integer":
        return int(value)
    if float(value).is_integer() and written_type(stored) == "integer":
        return int(value)
    return float(value)
