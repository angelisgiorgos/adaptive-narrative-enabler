"""Persistent variables: values that survive from one playthrough to the next.

Variables are defined under ``world_definition.variables`` (config/variables.yaml)::

    thief_caught: {type: bool, default: false, description: ...}
    reputation:   {type: int, default: 0, min: -5, max: 5}

Outcomes change them (``set_variables`` / ``change_variables``) and locations,
NPCs, items and missions can depend on them with ``enabled_if``::

    enabled_if:
      thief_caught: false          # equals
      reputation: {gte: 2}         # eq, ne, gt, gte, lt, lte, in, not_in
      playthroughs: {gte: 1}       # built-in: finished playthroughs

Values are stored per player profile in a JSON file (``persistence.path``).
Only real play writes to it; simulations and evolution never do.
"""
import copy
import datetime
import json
import os
import threading

from utils.config_loader import config

TYPES = {"bool": bool, "int": int, "float": float, "str": str}
OPERATORS = ("eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in")
BUILTINS = {"playthroughs": "int"}  # read-only, maintained by the store
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

def _compare(value, operator, expected):
    try:
        return {
            "eq": lambda: value == expected,
            "ne": lambda: value != expected,
            "gt": lambda: value > expected,
            "gte": lambda: value >= expected,
            "lt": lambda: value < expected,
            "lte": lambda: value <= expected,
            "in": lambda: value in expected,
            "not_in": lambda: value not in expected,
        }[operator]()
    except TypeError:
        return False


def check(conditions, values):
    """Whether every condition in an ``enabled_if``-style mapping holds."""
    for name, expected in (conditions or {}).items():
        value = values.get(name)
        if isinstance(expected, dict):
            if not all(_compare(value, op, target) for op, target in expected.items()):
                return False
        elif value != expected:
            return False
    return True


def validate_conditions(conditions, specs, where="enabled_if"):
    """Return problems with a condition mapping (unknown variables/operators)."""
    if conditions is None:
        return []
    if not isinstance(conditions, dict):
        return [f"`{where}` must be a mapping of variable names to values or comparisons."]
    known = set(specs) | set(BUILTINS)
    errors = []
    for name, expected in conditions.items():
        if name not in known:
            errors.append(f"`{where}` uses undefined variable `{name}`.")
        elif isinstance(expected, dict):
            unknown = sorted(set(expected) - set(OPERATORS))
            if unknown or not expected:
                errors.append(
                    f"`{where}.{name}` needs comparisons from: {', '.join(OPERATORS)}"
                    + (f" (unknown: {', '.join(unknown)})." if unknown else ".")
                )
    return errors


def validate_changes(set_variables, change_variables, specs, where="outcome"):
    """Return problems with an outcome's variable changes."""
    errors = []
    for field, changes in (("set_variables", set_variables), ("change_variables", change_variables)):
        if changes is None:
            continue
        if not isinstance(changes, dict):
            errors.append(f"`{where}.{field}` must be a mapping of variable names to values.")
            continue
        for name, value in changes.items():
            spec = specs.get(name)
            if spec is None:
                errors.append(f"`{where}.{field}` changes undefined variable `{name}`.")
            elif field == "set_variables" and not _matches_type(value, spec.get("type")):
                errors.append(f"`{where}.set_variables.{name}` must be a {spec.get('type')}.")
            elif field == "change_variables" and (
                spec.get("type") not in ("int", "float") or not _matches_type(value, "float")
            ):
                errors.append(f"`{where}.change_variables.{name}` needs a numeric variable and a number.")
    return errors


# ---- storage -----------------------------------------------------------------

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
        """Stored record {values, playthroughs, updated} of a profile."""
        record = self._read().get("profiles", {}).get(name, {})
        return {
            "values": dict(record.get("values", {})),
            "playthroughs": int(record.get("playthroughs", 0)),
            "updated": record.get("updated"),
        }

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
