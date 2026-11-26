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
import os
from functools import lru_cache
from pathlib import Path

import yaml


METADATA_PATH = Path(__file__).with_name("tuning_parameters.yaml")
BASIC_PATH = Path(__file__).with_name("tuning_basic.yaml")  # the Basic view's controls (one parameter each)
FIELDS = ("label", "explanation", "unit", "type", "minimum", "maximum", "default", "source", "requires")
# Parameters the engine no longer has (an older config folder may still hold them: they have no effect
# and are not offered). `*`: any one path segment.
RETIRED = ("outcome_scoring.*", "simulation.mutation_rate", "fitness.setup_payoff.early_setup_bonus",
           "transition_objective.character_goal_weight", "transition_objective.known_location_bonus",
           "spawn_selection.character_alignment_weight")
TYPES = ("integer", "float")
# The sections of hyperparameters.yaml, as the Weights tab titles them (others: from their name).
SECTION_TITLES = {
    "skill_check_resolution": "Skill checks", "outcome_resolution": "Outcome chances",
    "outcome_objective": "Outcome objective (storyteller)",
    "transition_objective": "Travel choice (storyteller)", "action_selection": "Action choice (storyteller)",
    "spawn_selection": "New places (storyteller)", "unexpected_events": "Unexpected events",
    "power_model": "Player power", "game_state": "Game state", "story_rules": "Story rules", "scene": "Scenes",
    "director": "Director", "npc_goals": "People's goals", "relationships": "Relationships", "npc_leads": "People's leads", "evolution": "Training",
    "simulation": "Simulation", "fitness": "Story score (fitness)", "session_scope": "Session cast",
    "discovery_nodes": "Discovery", "mission_settings": "Missions", "side_quest_settings": "Side quests", "side_story": "Side stories", "encounters": "Encounters", "world_definition": "World", "climax": "Climax",
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


@lru_cache(maxsize=1)
def basic_areas() -> tuple:
    """The Basic view's areas (``tuning_basic.yaml``), checked: each control is one described
    parameter (no pattern), listed once, with an ``effect``. A broken file is an error of this release."""
    if not BASIC_PATH.exists():
        return ()
    data = yaml.safe_load(BASIC_PATH.read_text(encoding="utf-8")) or {}
    known, seen, areas = metadata(), set(), []
    for area in data.get("areas") or []:
        if set(area) - {"key", "title", "summary", "controls"} or not area.get("key") or not area.get("title"):
            raise ValueError(f"{BASIC_PATH.name}: an area needs a key and a title (and nothing else but a summary "
                             f"and controls): {area}")
        controls = []
        for control in area.get("controls") or []:
            path = control.get("path")
            if set(control) - {"path", "effect", "scaled_by"} or not control.get("effect"):
                raise ValueError(f"{BASIC_PATH.name}: {path} needs an effect (and nothing else but its path and "
                                 "scaled_by).")
            scaled = control.get("scaled_by")
            if scaled is not None and (not isinstance(scaled, dict) or scaled.get("factor") not in FACTORS
                                       or not scaled.get("trait")):
                raise ValueError(f"{BASIC_PATH.name}: {path}'s scaled_by needs a trait and a factor in {FACTORS}.")
            if path not in known or "*" in path:
                raise ValueError(f"{BASIC_PATH.name}: {path} is not a described parameter.")
            if path in seen:
                raise ValueError(f"{BASIC_PATH.name}: {path} is listed twice.")
            seen.add(path)
            controls.append({"path": path, "effect": " ".join(str(control["effect"]).split()),
                             **({"scaled_by": dict(control["scaled_by"])} if control.get("scaled_by") else {})})
        areas.append({"key": area["key"], "title": area["title"],
                      "summary": " ".join(str(area.get("summary") or "").split()), "controls": controls})
    return tuple(areas)


FACTORS = ("trait", "half_plus", "drive", "adds")


def _trait_range(trait):
    """(low, high, meaning) of a storyteller trait: the bounds training keeps it in (genome_bounds)."""
    from core_engine.evolution import TRAIT_MEANINGS
    from core_engine.genome import TRAIT_BOUNDS
    from utils.config_loader import config

    bounds = (config.get("genome_bounds") or {}).get(trait) or TRAIT_BOUNDS.get(trait) or (0.0, 1.0)
    return float(bounds[0]), float(bounds[1]), TRAIT_MEANINGS.get(trait, trait.replace("_", " "))


def scaling_text(scaled):
    """How far each storyteller moves a Basic control's value (``scaled_by``), from the trait bounds
    training keeps (genome_bounds) and the parameters it names: e.g. *Each storyteller multiplies it by
    its trait `novelty_bias` (avoiding repetition; learned between 0.2 and 0.9): it counts ×0.2 to ×0.9.*"""
    from utils.config_loader import config

    trait, factor = scaled["trait"], scaled["factor"]
    low, high, meaning = _trait_range(trait)

    def n(value):
        return f"{value:.2f}".rstrip("0").rstrip(".")

    named = f"its trait `{trait}` ({meaning}; learned between {n(low)} and {n(high)})"
    if factor == "trait":
        return f"Each storyteller multiplies it by {named}: it counts ×{n(low)} to ×{n(high)}."
    if factor == "half_plus":
        return f"Each storyteller multiplies it by 0.5 + {named}: it counts ×{n(0.5 + low)} to ×{n(0.5 + high)}."
    if factor == "drive":
        start = config.get("mission_settings.pursuit_start_step", 12)
        full = config.get("mission_settings.pursuit_full_step", 30)
        return (f"Each storyteller multiplies it by {named}, times how far the quest is pursued: 0 until turn "
                f"{start}, full from turn {full} (or once the quest has stalled): it counts ×0 early, up to "
                f"×{n(low)} to ×{n(high)}.")
    parts, least, most = [], 0.0, 0.0
    for name, weight_path in (scaled.get("with") or {}).items():
        weight = float(config.get(weight_path, 0.0) or 0.0)
        t_low, t_high, t_meaning = _trait_range(name)
        parts.append(f"its trait `{name}` ({t_meaning}; {n(t_low)}–{n(t_high)}) × {n(weight)} (`{weight_path}`)")
        least, most = least + t_low * weight, most + t_high * weight
    return (f"Each storyteller adds its own pressure to it: {' and '.join(parts)}, so {n(least)} to {n(most)} "
            "more.")


def basic_view(present) -> list[dict]:
    """The Basic view for a configuration holding the parameters ``present``: [{key, title, summary,
    controls: [{path, effect, scaled_by?, scaling?}]}] (a control whose parameter the configuration
    lacks is left out; ``scaling``: how far the storyteller's learned traits move its value)."""
    present = set(present)
    view = []
    for area in basic_areas():
        controls = []
        for control in area["controls"]:
            if control["path"] in present:
                shown = dict(control)
                if control.get("scaled_by"):
                    shown["scaling"] = scaling_text(control["scaled_by"])
                controls.append(shown)
        if controls:
            view.append({**area, "controls": controls})
    return view


def defaults_dir() -> Path:
    """The release's own configuration folder: ANE_CONFIG_DEFAULTS, else the image's pristine copy next
    to the config folder (config.defaults, Docker), else the project's config folder."""
    folder = Path(__file__).resolve().parent.parent / "config"
    for candidate in (os.environ.get("ANE_CONFIG_DEFAULTS"), folder.with_name("config.defaults")):
        if candidate and (Path(candidate) / "default_history.json").is_file():
            return Path(candidate)
    return folder


@lru_cache(maxsize=4)
def _shipped(path, modified):
    import json

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return dict(((data.get("shipped") or {}).get("hyperparameters.yaml")) or {})


def shipped_values() -> dict:
    """{path: value} of hyperparameters.yaml as this release ships it (default_history.json, written by
    packaging/default_history.py from the configuration being released): what "changed from the
    shipped value" compares with. Empty when the file has no such record (an older one)."""
    path = defaults_dir() / "default_history.json"
    try:
        return _shipped(str(path), path.stat().st_mtime_ns)
    except (OSError, ValueError):
        return {}


def _matches(pattern: str, path: str) -> bool:
    wanted, parts = pattern.split("."), path.split(".")
    return len(wanted) == len(parts) and all(w == "*" or w == p for w, p in zip(wanted, parts))


def retired(path: str) -> bool:
    """A parameter the engine no longer has (kept by an older config folder)."""
    return any(_matches(pattern, path) for pattern in RETIRED)


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
    maximum, default, source, described, requires, inactive} (``requires``: the true/false setting it
    needs; ``inactive``: why it has no effect now, when that setting is off). Without metadata (``described`` false): a label made from its
    name (with its parent's when that says more), the type it is written with, nothing invented."""
    meta = find(path)
    if meta is None:
        parts = path.split(".")
        label = _readable(parts[-1]) if len(parts) <= 2 else f"{_readable(parts[-1])} ({parts[-2].replace('_', ' ')})"
        return {"path": path, "value": value, "section": section_title(path), "label": label, "explanation": None,
                "unit": None, "type": written_type(value), "minimum": None, "maximum": None, "default": None,
                "source": [], "described": False, "requires": None, "inactive": None}
    requires = meta.get("requires")
    return {"path": path, "value": value, "section": section_title(path), "label": meta["label"],
            "explanation": meta.get("explanation"), "unit": meta.get("unit"), "type": meta["type"],
            "minimum": meta.get("minimum"), "maximum": meta.get("maximum"), "default": meta.get("default"),
            "source": list(meta.get("source") or []), "described": True, "requires": requires,
            "inactive": (f"inactive: `{requires}` is off" if requires and not _switch_on(requires) else None)}


def _switch_on(path: str) -> bool:
    """Whether the true/false setting a parameter needs is on, in the active configuration."""
    from utils.config_loader import config

    return bool(config.get(path, False))


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
