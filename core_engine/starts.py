"""Where a story starts.

Locations opt in as starting points (config/locations.yaml)::

    Harbor:
      start_eligible: true    # a story may begin here (default: false)
      start_weight: 1.5       # relative chance among the eligible locations (default: 1)

Each new story draws its start with the session's random generator from the
eligible locations that are enabled (``enabled_if``) and from which the story's
mission can be reached (GameState._choose_mission_and_start). The draw is
weighted by ``start_weight``. Every time a location started one of the
profile's ``start_selection.recent_starts`` latest stories, its weight is
multiplied by ``start_selection.repeat_factor``: repeating a recent start is
discouraged, never forbidden.

A world in which no location is ``start_eligible`` keeps a single start:
``start_selection.default_location`` (or the first enabled location).
"""
import math

from utils.config_loader import config

HISTORY_LIMIT = 20  # starts remembered per profile; recent_starts may not exceed it
DEFAULTS = {"default_location": "Harbor", "recent_starts": 3, "repeat_factor": 0.25}


def settings(cfg=None):
    """``start_selection`` with defaults for the keys a configuration leaves out."""
    cfg = config if cfg is None else cfg
    return {**DEFAULTS, **(cfg.get("start_selection") or {})}


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_settings(values):
    """Problems with ``start_selection`` (empty when valid)."""
    errors = []
    window = values.get("recent_starts")
    if isinstance(window, bool) or not isinstance(window, int) or not 0 <= window <= HISTORY_LIMIT:
        errors.append(f"`start_selection.recent_starts` must be a whole number from 0 to {HISTORY_LIMIT}.")
    factor = values.get("repeat_factor")
    if not _is_number(factor) or not 0 < factor <= 1:
        errors.append("`start_selection.repeat_factor` must be above 0 (0 would forbid repeats) and at most 1.")
    if not isinstance(values.get("default_location"), str):
        errors.append("`start_selection.default_location` must be a location name.")
    return errors


def location_errors(data, where):
    """Problems with a location's ``start_eligible`` / ``start_weight``."""
    errors = []
    if not isinstance(data.get("start_eligible", False), bool):
        errors.append(f"{where}: `start_eligible` must be true or false.")
    weight = data.get("start_weight", 1.0)
    if not _is_number(weight) or weight <= 0:
        errors.append(f"{where}: `start_weight` must be a number above 0 (use `start_eligible: false` to exclude it).")
    return errors


def weights(candidates, recent=(), values=None):
    """Location name -> draw weight: ``start_weight`` x ``repeat_factor`` per recent start there."""
    values = settings() if values is None else values
    window = int(values["recent_starts"])
    recent = list(recent)[-window:] if window > 0 else []
    factor = float(values["repeat_factor"])
    return {loc.name: loc.start_weight * factor ** recent.count(loc.name) for loc in candidates}


def probabilities(candidates, recent=(), values=None):
    """Location name -> chance of being drawn as the start."""
    drawn = weights(candidates, recent, values)
    total = sum(drawn.values())
    return {name: weight / total for name, weight in drawn.items()}


def draw(candidates, recent, rng, values=None):
    """Draw one start. A single candidate is returned without using ``rng``,
    so worlds with one start replay exactly as before start variation existed."""
    if len(candidates) == 1:
        return candidates[0]
    drawn = weights(candidates, recent, values)
    return rng.choices(candidates, weights=[drawn[loc.name] for loc in candidates], k=1)[0]
