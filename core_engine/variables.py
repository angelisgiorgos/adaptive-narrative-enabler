"""Persistent variables: values that survive from one playthrough to the next.

Variables are defined under ``world_definition.variables`` (config/variables.yaml)::

    thief_caught: {type: bool, default: false, description: ...}
    reputation:   {type: int, default: 0, min: -5, max: 5}

Effects change them (``set_variable`` / ``change_variable``, or the outcome
shorthand ``set_variables`` / ``change_variables``; see effects.py), and every
``enabled_if`` can depend on them (see conditions.py), e.g. in shorthand::

    enabled_if:
      thief_caught: false          # equals
      reputation: {gte: 2}         # eq, ne, gt, gte, lt, lte, in, not_in
      playthroughs: {gte: 1}       # built-in: finished playthroughs

Values are stored per player profile in a JSON file (``persistence.path``),
with the profile's latest starting locations (``starts``, see starts.py).
Only real play writes to it; simulations and evolution never do.
"""
import copy
import datetime
import json
import os
import threading

from utils.config_loader import config

from . import conditions
from .conditions import BUILTIN_VARIABLES as BUILTINS, OPERATORS, References
from .starts import HISTORY_LIMIT

TYPES = {"bool": bool, "int": int, "float": float, "str": str}
PROFILE_PATTERN = r"^[A-Za-z0-9_.-]{1,64}$"


# ---- definitions -----------------------------------------------------------

def definitions(cfg=None):
    """The configured variable definitions (name -> spec mapping)."""
    cfg = config if cfg is None else cfg
    return dict(cfg.get("world_definition.variables") or {})


def _matches_type(value, type_name):
    if type_name == "bool":
        return isinstance(value, bool)
    if type_name in ("int", "float"):
        number = (int, float) if type_name == "float" else (int,)
        return isinstance(value, number) and not isinstance(value, bool)
    return isinstance(value, str)


def validate_definitions(specs):
    """Return a list of problems with variable definitions (empty when valid)."""
    if not isinstance(specs, dict):
        return ["`world_definition.variables` must be a mapping of variable names to definitions."]
    errors = []
    for name, spec in specs.items():
        label = f"`variables.{name}`"
        if name in BUILTINS:
            errors.append(f"{label}: `{name}` is built in and cannot be redefined.")
            continue
        if name in conditions.KEYWORDS:
            errors.append(f"{label}: `{name}` is a condition keyword; choose another name.")
            continue
        if not isinstance(spec, dict):
            errors.append(f"{label} must be a mapping with `type` and `default`.")
            continue
        type_name = spec.get("type")
        if type_name not in TYPES:
            errors.append(f"{label}.type must be one of: {', '.join(TYPES)}.")
            continue
        problems = []
        if "default" not in spec or not _matches_type(spec["default"], type_name):
            problems.append(f"{label}.default must be a {type_name}.")
        for bound in ("min", "max"):
            if bound in spec and (type_name not in ("int", "float") or not _matches_type(spec[bound], "float")):
                problems.append(f"{label}.{bound} is only allowed for numbers and must be a number.")
        if "min" in spec and "max" in spec and not problems and spec["min"] > spec["max"]:
            problems.append(f"{label}: min is greater than max.")
        errors.extend(problems)
    return errors


def defaults(specs):
    return {name: copy.deepcopy(spec.get("default")) for name, spec in specs.items()}


def clamp(name, value, specs):
    spec = specs.get(name, {})
    if spec.get("type") in ("int", "float"):
        if "min" in spec:
            value = max(spec["min"], value)
        if "max" in spec:
            value = min(spec["max"], value)
        if spec["type"] == "int":
            value = int(value)
    return value


# ---- conditions --------------------------------------------------------------

def check(condition, values):
    """Whether a condition holds for these variable values (no story in progress:
    only `variable` leaves can be read). Same evaluator as every ``enabled_if``."""
    return conditions.holds(condition, conditions.VariableFacts(values))


def validate_conditions(condition, specs, where="enabled_if"):
    """Problems with a condition that may only use these variables (see
    conditions.validate for full world references)."""
    return conditions.validate(condition, References(variables=specs), where=where)


def validate_changes(set_variables, change_variables, specs, where="outcome"):
    """Problems with an outcome's ``set_variables`` / ``change_variables`` (shorthand
    for effects; validated by effects.validate)."""
    from . import effects

    errors = []
    for field, changes in (("set_variables", set_variables), ("change_variables", change_variables)):
        if changes is not None and not isinstance(changes, dict):
            errors.append(f"`{where}.{field}` must be a mapping of variable names to values.")
    if errors:
        return errors
    legacy = effects.from_outcome_fields(set_variables, change_variables)
    return effects.validate(legacy, References(variables=specs), where=where)


# ---- storage -----------------------------------------------------------------

def _start_names(record, key="starts"):
    starts = record.get(key) if isinstance(record, dict) else None
    return [name for name in starts if isinstance(name, str)] if isinstance(starts, list) else []


def _default_path():
    base = os.environ.get("ANE_SAVE_DIR") or os.path.join(os.path.dirname(os.path.dirname(__file__)), "saves")
    return os.path.join(base, "persistent_variables.json")


class VariableStore:
    """Persistent values per player profile, in one JSON file (atomic writes)."""

    _lock = threading.Lock()

    def __init__(self, path=None):
        if path is None and os.environ.get("ANE_SAVE_DIR"):
            path = os.path.join(os.environ["ANE_SAVE_DIR"], "persistent_variables.json")
        self.path = path or config.get("persistence.path") or _default_path()
        if not os.path.isabs(self.path):
            self.path = os.path.join(os.path.dirname(os.path.dirname(__file__)), self.path)

    def _read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                data = json.load(stream)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        temporary = f"{self.path}.tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
        os.replace(temporary, self.path)

    def profile(self, name="default"):
        """Stored record {values, playthroughs, starts, updated} of a profile."""
        record = self._read().get("profiles", {}).get(name, {})
        return {
            "values": dict(record.get("values", {})),
            "playthroughs": int(record.get("playthroughs", 0)),
            "starts": _start_names(record),
            "missions": _start_names(record, "missions"),
            "updated": record.get("updated"),
        }

    def recent_starts(self, name="default"):
        """The profile's latest starting locations, oldest first."""
        return _start_names(self._read().get("profiles", {}).get(name, {}))

    def record_start(self, name, location, limit=None):
        """Remember where one of the profile's stories started (latest ``limit`` kept)."""
        self._remember(name, "starts", location, limit)

    def recent_missions(self, name="default"):
        """The profile's latest missions (their names), oldest first."""
        return _start_names(self._read().get("profiles", {}).get(name, {}), "missions")

    def record_mission(self, name, mission, limit=None):
        """Remember the mission of one of the profile's stories (latest ``limit`` kept)."""
        self._remember(name, "missions", mission, limit)

    def recent_memories(self, name="default"):
        """What the profile's last stories were, in a few words each, oldest first (memory)."""
        return _start_names(self._read().get("profiles", {}).get(name, {}), "memories")

    def record_memory(self, name, text, limit=None):
        """Remember what one of the profile's stories was (story_beats: "Last time you ...")."""
        self._remember(name, "memories", text, limit)

    def remembered_people(self, name="default"):
        """{friends, foes}: the NPCs the profile's stories made friends of, and beat."""
        record = self._read().get("profiles", {}).get(name, {})
        return {"friends": _start_names(record, "friends"), "foes": _start_names(record, "foes")}

    def record_people(self, name, friends=(), foes=(), limit=None):
        """Remember the friends made and the foes beaten in one of the profile's stories."""
        if not friends and not foes:
            return
        with self._lock:
            data = self._read()
            record = data.setdefault("profiles", {}).setdefault(name, {"values": {}, "playthroughs": 0})
            for key, names in (("friends", friends), ("foes", foes)):
                kept = [n for n in _start_names(record, key) if n not in set(names)] + [str(n) for n in names]
                record[key] = kept[-(limit or HISTORY_LIMIT):]
            record["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            data["version"] = 1
            self._write(data)

    # ---- progression: the goals a save game achieved, and its sessions (core_engine/progression.py)

    def completed_goals(self, name="default"):
        """The goals (mission keys) this save game has achieved, in order; each only once."""
        return _start_names(self._read().get("profiles", {}).get(name, {}), "goals_achieved")

    def complete_goal(self, name, goal):
        """Add a goal to the save game's achieved goals (once)."""
        with self._lock:
            data = self._read()
            record = data.setdefault("profiles", {}).setdefault(name, {"values": {}, "playthroughs": 0})
            achieved = _start_names(record, "goals_achieved")
            if str(goal) not in achieved:
                record["goals_achieved"] = achieved + [str(goal)]
            record["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            data["version"] = 1
            self._write(data)

    def record_session(self, name, report, limit=50):
        """Keep a session's report in the save game (the latest ``limit``)."""
        with self._lock:
            data = self._read()
            record = data.setdefault("profiles", {}).setdefault(name, {"values": {}, "playthroughs": 0})
            sessions = [s for s in record.get("sessions") or [] if isinstance(s, dict)]
            record["sessions"] = (sessions + [report])[-max(1, int(limit)):]
            record["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            data["version"] = 1
            self._write(data)

    def note_delivery(self, name, report_id, delivery):
        """Note whether a session's report reached the UNP."""
        with self._lock:
            data = self._read()
            record = data.get("profiles", {}).get(name)
            for report in (record or {}).get("sessions") or []:
                if isinstance(report, dict) and report.get("report_id") == report_id:
                    report["delivery"] = delivery
                    self._write(data)
                    return True
        return False

    def sessions(self, name="default"):
        """The save game's session reports, oldest first."""
        record = self._read().get("profiles", {}).get(name, {})
        return [s for s in record.get("sessions") or [] if isinstance(s, dict)]

    def progression(self, name="default"):
        """The save game: {profile, playthroughs, goals_achieved, sessions, values, updated}."""
        stored = self.profile(name)
        return {"profile": name, "playthroughs": stored["playthroughs"], "goals_achieved": self.completed_goals(name),
                "sessions": self.sessions(name), "values": stored["values"], "updated": stored["updated"]}

    def _remember(self, name, key, value, limit=None):
        with self._lock:
            data = self._read()
            record = data.setdefault("profiles", {}).setdefault(name, {"values": {}, "playthroughs": 0})
            record[key] = (_start_names(record, key) + [str(value)])[-(limit or HISTORY_LIMIT):]
            record["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            data["version"] = 1
            self._write(data)

    def load(self, name="default", specs=None):
        """Current values: stored ones for defined variables, defaults otherwise."""
        specs = definitions() if specs is None else specs
        stored = self.profile(name)
        values = defaults(specs)
        for key, value in stored["values"].items():
            if key in specs and _matches_type(value, specs[key].get("type")):
                values[key] = value
        values["playthroughs"] = stored["playthroughs"]
        return values

    def profiles(self):
        return sorted(self._read().get("profiles", {}))

    def save(self, name, values, finished=False, playthroughs=None):
        """Store a profile's values; ``finished`` counts a completed playthrough,
        ``playthroughs`` sets the count directly (editing a profile)."""
        with self._lock:
            data = self._read()
            profiles = data.setdefault("profiles", {})
            record = profiles.setdefault(name, {"values": {}, "playthroughs": 0})
            record["values"] = {k: v for k, v in values.items() if k not in BUILTINS}
            if playthroughs is not None:
                record["playthroughs"] = max(0, int(playthroughs))
            if finished:
                record["playthroughs"] = int(record.get("playthroughs", 0)) + 1
            record["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            data["version"] = 1
            self._write(data)

    def reset(self, name="default"):
        with self._lock:
            data = self._read()
            data.get("profiles", {}).pop(name, None)
            data["version"] = 1
            self._write(data)
