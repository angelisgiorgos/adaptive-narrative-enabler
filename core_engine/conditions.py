"""One condition language for the whole world.

Every ``enabled_if`` (locations, NPCs, items, events, actions, dialogue lines,
missions) and every mission goal is a condition, evaluated by ``holds`` here,
so the same condition gives the same answer wherever it is written.

A condition is a tree. Groups combine conditions::

    all: [<condition>, ...]     every one holds
    any: [<condition>, ...]     at least one holds
    not: <condition>            it does not hold

Leaves compare a value, or test a fact of the story::

    variable: reputation   op: gte   value: 2      persistent variable (also `playthroughs`)
    stat: persuasion       op: gte   value: 60     character stat, 1-100 with item bonuses
    resource: coins        op: gte   value: 20     coins, health or turn (actions taken)
    has_item: Royal Pass                           in the inventory
    item_collected: Royal Pass                     collected or received in this story (even if used since)
    visited: Palace                                reached at least once in this story
    at_location: Palace                            where the player is now
    npc_removed: Thief                             removed from the story
    event_done: confront                           entered and resolved (any result)
    event_succeeded: confront                      resolved by an event action that succeeded
    event_failed: confront                         resolved by an event action that failed
    action_done: Interrogate the thief             taken successfully ("(NPC) " prefix optional)
    outcome_tag: escape                            an outcome with this tag succeeded
    mission: expose_the_fence                      the story's mission

``op`` is eq (the default), ne, gt, gte, lt, lte, in or not_in.

Shorthand, as written before trees existed: a mapping of variable names means
``all`` of its comparisons, and a list means ``all`` of its items::

    enabled_if: {thief_caught: false, reputation: {gte: 2, lt: 5}}
"""
import re

OPERATORS = ("eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in")
BUILTIN_VARIABLES = {"playthroughs": "int"}  # read-only, maintained by the variable store
GROUPS = ("all", "any", "not")
COMPARISONS = ("variable", "stat", "resource")
FACTS = ("has_item", "item_collected", "visited", "at_location", "npc_removed", "event_done", "event_succeeded",
         "event_failed", "action_done", "outcome_tag", "mission", "secret_known")
KEYWORDS = frozenset(GROUPS + COMPARISONS + FACTS)  # cannot be variable names (shorthand would be ambiguous)
RESOURCES = ("coins", "health", "turn")
TRUE = ("all", ())

# Which world names a fact refers to (References attribute); None: free text.
_FACT_REFERENCES = {
    "has_item": "items", "item_collected": "items", "visited": "locations", "at_location": "locations",
    "npc_removed": "npcs", "event_done": "events", "event_succeeded": "events", "event_failed": "events",
    "action_done": None, "outcome_tag": None, "mission": "missions", "secret_known": "npcs",
}
_FACT_LABELS = {"items": "item", "locations": "location", "npcs": "NPC", "events": "event", "missions": "mission"}
_ACTION_PREFIX = re.compile(r"^\s*(\([^)]*\)|\[[^\]]*\])\s*")


class ConditionError(ValueError):
    """A condition that cannot be read or evaluated."""


def action_keys(name):
    """Match "(Priest) Seek a blessing" by its full name or as "Seek a blessing"."""
    full = str(name).strip().lower()
    return {full, _ACTION_PREFIX.sub("", full)}


# ---- reading ---------------------------------------------------------------------

def _freeze(value):
    return tuple(_freeze(item) for item in value) if isinstance(value, (list, tuple)) else value


def normalize(raw):
    """The canonical tree (nested tuples) of a condition; ``TRUE`` when empty."""
    if isinstance(raw, Condition):
        return raw.tree
    if raw is None or raw == {} or raw == []:
        return TRUE
    if isinstance(raw, list):
        return ("all", tuple(normalize(child) for child in raw))
    if not isinstance(raw, dict):
        raise ConditionError(f"a condition must be a mapping or a list, not {type(raw).__name__}")
    if set(raw) & KEYWORDS:
        return _node(raw)
    leaves = []  # shorthand: {variable: value | {op: value, ...}}
    for name, expected in raw.items():
        if isinstance(expected, dict):
            if not expected:
                raise ConditionError(f"`{name}` needs at least one comparison ({', '.join(OPERATORS)})")
            leaves.extend(("variable", name, op, _freeze(value)) for op, value in expected.items())
        else:
            leaves.append(("variable", name, "eq", _freeze(expected)))
    return leaves[0] if len(leaves) == 1 else ("all", tuple(leaves))


def _node(raw):
    keys = set(raw)
    group = keys & set(GROUPS)
    if group:
        kind = group.pop()
        if len(keys) != 1:
            raise ConditionError(f"`{kind}` must be the only key of its mapping")
        body = raw[kind]
        if kind == "not":
            if body is None or body == {} or body == []:
                raise ConditionError("`not` needs a condition")
            return ("not", normalize(body))
        if not isinstance(body, list) or not body:
            raise ConditionError(f"`{kind}` needs a non-empty list of conditions")
        return (kind, tuple(normalize(child) for child in body))
    subjects = keys & set(COMPARISONS + FACTS)
    if len(subjects) != 1:
        raise ConditionError(f"a condition names exactly one of: {', '.join(COMPARISONS + FACTS)}")
    kind = subjects.pop()
    if kind in FACTS:
        if keys != {kind}:
            raise ConditionError(f"`{kind}` takes no other keys (use `not` to negate it)")
        return ("fact", kind, raw[kind])
    if keys - {kind, "op", "value"}:
        raise ConditionError(f"`{kind}` takes only `op` and `value`, not {', '.join(sorted(keys - {kind, 'op', 'value'}))}")
    if "value" not in raw:
        raise ConditionError(f"`{kind}: {raw[kind]}` needs a `value`")
    return (kind, raw[kind], raw.get("op", "eq"), _freeze(raw["value"]))


class Condition:
    """A condition read once (``normalize``) and evaluated many times."""

    __slots__ = ("source", "tree")

    def __init__(self, raw=None):
        self.source = raw.source if isinstance(raw, Condition) else raw
        self.tree = normalize(raw)

    @classmethod
    def of(cls, raw):
        return raw if isinstance(raw, Condition) else cls(raw)

    def holds(self, facts):
        return evaluate(self.tree, facts)

    def references(self):
        return references(self.tree)

    @property
    def dynamic(self):
        """Whether it reads more than persistent variables (the story's own state)."""
        return any(kind != "variable" for kind, _ in self.references())

    def __eq__(self, other):
        return isinstance(other, Condition) and other.tree == self.tree

    def __hash__(self):
        return hash(self.tree)

    def __repr__(self):
        return f"Condition({self.source!r})"


    def __copy__(self):
        return self  # immutable

    def __deepcopy__(self, memo):
        return self


class Conditional:
    """Mixin: ``enabled_if`` (any condition form) switches the object on; its
    ``condition`` is read once here and evaluated by GameState.is_enabled."""

    condition = None
    revision = 0  # counts every enabled_if set, so cached summaries know when to look again

    @property
    def enabled_if(self):
        return self.__dict__.get("_enabled_if")

    @enabled_if.setter
    def enabled_if(self, raw):
        self._enabled_if = raw or None
        self.condition = Condition(raw) if raw else None
        Conditional.revision += 1


# ---- evaluating ------------------------------------------------------------------

def compare(value, op, expected):
    try:
        if op == "eq":
            return value == expected
        if op == "ne":
            return value != expected
        if op == "gt":
            return value > expected
        if op == "gte":
            return value >= expected
        if op == "lt":
            return value < expected
        if op == "lte":
            return value <= expected
        if op == "in":
            return value in expected
        if op == "not_in":
            return value not in expected
    except TypeError:
        return False
    return False  # unknown operator (rejected by validate)


def evaluate(tree, facts):
    kind = tree[0]
    if kind == "all":
        return all(evaluate(child, facts) for child in tree[1])
    if kind == "any":
        return any(evaluate(child, facts) for child in tree[1])
    if kind == "not":
        return not evaluate(tree[1], facts)
    if kind == "fact":
        return facts.fact(tree[1], tree[2])
    return compare(facts.quantity(kind, tree[1]), tree[2], tree[3])


def holds(raw, facts):
    """Whether a condition (any form) holds for ``facts`` (StoryFacts or VariableFacts)."""
    return evaluate(normalize(raw), facts)


class StoryFacts:
    """What conditions read from a story in progress (a GameState)."""

    def __init__(self, state):
        self.state = state

    def quantity(self, kind, name):
        state = self.state
        if kind == "variable":
            return state.variables.get(name)
        if kind == "stat":
            if name not in state.stats.names:
                return None
            return state.stats.value(name, state.combat.item_bonuses(state.inventory).get(name, 0))
        if kind == "resource":
            return {"coins": state.coins, "health": state.health, "turn": state.actions_taken}.get(name)
        return None

    def fact(self, kind, name):
        state = self.state
        if kind == "has_item":
            return name in state.inventory
        if kind == "visited":
            return name in state.location_counts
        if kind == "at_location":
            return state.current is not None and state.current.name == name
        if kind == "npc_removed":
            return name in state.removed_npcs
        if kind == "item_collected":
            return any(entry["item"] == name and entry["what"] in ("collected", "received") for entry in state.item_log)
        if kind == "event_done":
            return any(entry["event"] == name and "action" in entry for entry in state.event_log)
        if kind in ("event_succeeded", "event_failed"):
            result = "success" if kind == "event_succeeded" else "failure"
            return any(entry["event"] == name and entry.get("result") == result for entry in state.event_log)
        if kind == "action_done":
            return bool(action_keys(name) & state.succeeded_actions)
        if kind == "outcome_tag":
            return str(name).lower() in state.succeeded_outcome_tags
        if kind == "mission":
            return state.mission is not None and state.mission.name == name
        if kind == "secret_known":  # the NPC's secret came out in this story (story_beats)
            return name in state.__dict__.get("revealed_secrets", {})
        return False


class VariableFacts:
    """Persistent variables alone (no story in progress): only `variable` leaves can be read."""

    def __init__(self, values):
        self.values = values

    def quantity(self, kind, name):
        if kind != "variable":
            raise ConditionError(f"`{kind}` conditions need a story in progress")
        return self.values.get(name)

    def fact(self, kind, name):
        raise ConditionError(f"`{kind}` conditions need a story in progress")


def references(tree):
    """(kind, name) of everything a condition reads."""
    kind = tree[0]
    if kind in ("all", "any"):
        return set().union(*(references(child) for child in tree[1]))
    if kind == "not":
        return references(tree[1])
    if kind == "fact":
        return {(tree[1], tree[2])}
    return {(kind, tree[1])}


# ---- mission goals -----------------------------------------------------------------

_GOAL_FACTS = {"action": "action_done", "location": "visited", "item": "has_item", "event": "event_done",
               "event_succeeded": "event_succeeded", "npc_removed": "npc_removed", "outcome_tag": "outcome_tag"}


def goal_condition(key, value):
    """The condition a mission goal key stands for (goal keys are shorthand)."""
    if key in _GOAL_FACTS:
        return {_GOAL_FACTS[key]: value}
    if key == "coins":
        return {"resource": "coins", "op": "gte", "value": value}
    if key in ("variables", "condition"):
        return value
    raise ConditionError(f"unknown goal key `{key}`")


# ---- validating --------------------------------------------------------------------

class References:
    """Names a condition or an effect may refer to. A field left as None is not checked."""

    def __init__(self, variables=None, stats=None, items=None, locations=None, npcs=None, events=None, missions=None):
        self.variables = variables  # name -> {type, ...} (persistent variable definitions)
        self.stats = None if stats is None else set(stats)
        self.items = None if items is None else set(items)
        self.locations = None if locations is None else set(locations)
        self.npcs = None if npcs is None else set(npcs)
        self.events = None if events is None else set(events)
        self.missions = None if missions is None else set(missions)

    @classmethod
    def from_world(cls, world):
        """From a ``world_definition`` mapping (configuration)."""
        world = world or {}
        return cls(
            variables=dict(world.get("variables") or {}),
            stats=list(((world.get("character_stats") or {}).get("stats")) or {}),
            items=[entry.get("name") for entry in world.get("objects") or [] if isinstance(entry, dict)],
            locations=list(world.get("locations") or {}),
            npcs=[entry.get("name") for entry in world.get("characters") or [] if isinstance(entry, dict)],
            events=list(world.get("events") or {}),
            missions=list(world.get("missions") or {}),
        )

    @classmethod
    def from_graph(cls, world, variables, stats):
        """From a built WorldGraph, its variable definitions and stat names."""
        return cls(
            variables=dict(variables), stats=list(stats),
            items=[obj.name for obj in world.objects], locations=list(world.locations),
            npcs=[char.name for char in world.characters], events=list(world.events), missions=list(world.missions),
        )

    def variable_type(self, name):
        """The type of a variable, or None if it is not defined (or not checked)."""
        if name in BUILTIN_VARIABLES:
            return BUILTIN_VARIABLES[name]
        spec = (self.variables or {}).get(name)
        return spec.get("type") if isinstance(spec, dict) else None


def matches_type(value, type_name, strict=False):
    """Whether ``value`` fits a variable type; ``strict``: an int must be whole (assignments)."""
    if type_name == "bool":
        return isinstance(value, bool)
    if type_name == "int" and strict:
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name in ("int", "float"):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "str":
        return isinstance(value, str)
    return True


def _operand_errors(label, type_name, op, value):
    if type_name in ("bool", "str") and op in ("gt", "gte", "lt", "lte"):
        return [f"{label} is a {type_name}; `{op}` only compares numbers (use eq, ne, in or not_in)."]
    if op in ("in", "not_in"):
        if not isinstance(value, tuple) or not value:
            return [f"{label}: `{op}` needs a non-empty list of values."]
        if not all(matches_type(item, type_name) for item in value):
            return [f"{label}: every value in `{op}` must be a {type_name}."]
        return []
    if not matches_type(value, type_name):
        return [f"{label}: `{op}` needs a {type_name} value, not {value!r}."]
    return []


def validate(raw, refs=None, where="enabled_if"):
    """Problems with a condition: structure, operators, references and operand
    types (empty when valid)."""
    refs = refs or References()
    try:
        tree = normalize(raw)
    except ConditionError as exc:
        return [f"`{where}`: {exc}."]
    errors = []
    _check(tree, refs, where, errors)
    return errors


def _check(tree, refs, where, errors):
    kind = tree[0]
    if kind in ("all", "any"):
        for child in tree[1]:
            _check(child, refs, where, errors)
        return
    if kind == "not":
        _check(tree[1], refs, where, errors)
        return
    if kind == "fact":
        _, fact, name = tree
        if not isinstance(name, str) or not name.strip():
            errors.append(f"`{where}`: `{fact}` needs a name.")
            return
        field = _FACT_REFERENCES[fact]
        known = getattr(refs, field) if field else None
        if known is not None and name not in known:
            errors.append(f"`{where}`: unknown {_FACT_LABELS[field]} `{name}` in `{fact}`.")
        return
    _, name, op, value = tree
    label = f"`{where}`: `{kind}: {name}`"
    if op not in OPERATORS:
        errors.append(f"{label} uses unknown operator `{op}` (use {', '.join(OPERATORS)}).")
        return
    if kind == "variable":
        if refs.variables is not None and name not in refs.variables and name not in BUILTIN_VARIABLES:
            errors.append(f"`{where}` uses undefined variable `{name}`.")
            return
        type_name = refs.variable_type(name)
    elif kind == "stat":
        if refs.stats is not None and name not in refs.stats:
            errors.append(f"`{where}` uses undefined stat `{name}`.")
            return
        type_name = "int"
    else:
        if name not in RESOURCES:
            errors.append(f"`{where}`: `resource` must be one of {', '.join(RESOURCES)}, not `{name}`.")
            return
        type_name = "int"
    if type_name:
        errors.extend(_operand_errors(label, type_name, op, value))
