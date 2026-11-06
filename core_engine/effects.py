"""Effect lists: one way to change a story, shared by outcomes, NPC dialogue
lines and mission rewards (GameState.apply_effects runs them).

    effects:
      - set_variable: {palace_access: true}    # persistent variables, clamped to min/max
      - change_variable: {reputation: 1}       # numeric variables only
      - set_stat: {persuasion: 60}             # character stats for this story (1-100)
      - change_stat: {persuasion: 5}
      - give_item: Royal Pass                  # an item name or a list of names
      - take_item: Bribe Purse
      - change_coins: -5                       # never below 0
      - change_health: 1                       # between 0 and max_health
      - remove_npc: Thief                      # true: the NPC who acts
      - reveal_npc: Priest                     # into the current scene
      - reveal_item: Fence's Ledger
      - reveal_location: Vault                 # opens a route from the current location
      - unlock_location: Dungeon               # a confined player may leave it
      - move_to: Palace
      - send_to: Dungeon                       # one way
      - start_event: bandit_ambush             # once the current action is resolved
      - complete_mission: true                 # the story's mission (or `complete_mission: <id>`) is won
      - end_story: You sail into the sunrise.  # the story ends with this text

Effects run in order. A list is validated as a whole (references, value types,
writable fields) before its first effect runs, so a bad effect changes nothing.
The outcome fields written before effect lists existed (`set_variables`,
`change_variables`, `change_stats`) are shorthand for the matching effects.
"""
from .conditions import BUILTIN_VARIABLES, References, matches_type

# effect -> shape of its argument
EFFECTS = {
    "set_variable": "assignments", "change_variable": "assignments",
    "set_stat": "assignments", "change_stat": "assignments",
    "give_item": "items", "take_item": "items",
    "change_coins": "number", "change_health": "number",
    "remove_npc": "actor", "reveal_npc": "npc", "reveal_item": "item",
    "reveal_location": "location", "unlock_location": "location", "move_to": "location", "send_to": "location",
    "start_event": "event", "complete_mission": "mission", "end_story": "text",
}
# what each effect writes, for route planning toward goals (GameState._advances)
_WRITES = {
    "set_variable": "variable", "change_variable": "variable", "set_stat": "stat", "change_stat": "stat",
    "give_item": "item", "take_item": "item", "reveal_item": "item", "remove_npc": "npc", "reveal_npc": "npc_seen",
    "reveal_location": "location", "unlock_location": "location", "move_to": "location", "send_to": "location",
    "start_event": "event", "complete_mission": "mission", "end_story": "end",
}
_REFERENCES = {"items": "items", "npc": "npcs", "item": "items", "location": "locations", "event": "events"}
STAT_RANGE = (1, 100)


class EffectError(ValueError):
    """An effect list that cannot be read or applied."""


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_whole(value):
    return isinstance(value, int) and not isinstance(value, bool)


def normalize(raw):
    """``((effect, argument), ...)`` of an effect list; raises EffectError on bad structure."""
    if isinstance(raw, EffectList):
        return raw.items
    if raw is None:
        return ()
    if isinstance(raw, tuple) and all(isinstance(item, tuple) and len(item) == 2 for item in raw):
        return raw  # already normalized
    if not isinstance(raw, list):
        raise EffectError("effects must be a list of one-effect mappings")
    items = []
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict) or len(entry) != 1:
            raise EffectError(f"effect {index} must be a mapping with exactly one effect")
        (kind, argument), = entry.items()
        if kind not in EFFECTS:
            raise EffectError(f"effect {index}: unknown effect `{kind}` (use {', '.join(EFFECTS)})")
        if EFFECTS[kind] == "assignments":
            if not isinstance(argument, dict) or not argument:
                raise EffectError(f"effect {index}: `{kind}` needs a mapping of names to values")
            argument = tuple(argument.items())
        elif EFFECTS[kind] == "items":
            argument = tuple(argument) if isinstance(argument, list) else (argument,)
        items.append((kind, argument))
    return tuple(items)


def from_outcome_fields(set_variables=None, change_variables=None, change_stats=None):
    """The effects that an outcome's older fields stand for."""
    items = []
    if set_variables:
        items.append(("set_variable", tuple(dict(set_variables).items())))
    if change_variables:
        items.append(("change_variable", tuple(dict(change_variables).items())))
    if change_stats:
        items.append(("change_stat", tuple(dict(change_stats).items())))
    return tuple(items)


def writes(items):
    """(kind, name) of everything an effect list changes."""
    written = set()
    for kind, argument in items:
        target = _WRITES.get(kind)
        if EFFECTS[kind] == "assignments":
            written |= {(target, name) for name, _ in argument}
        elif EFFECTS[kind] == "items":
            written |= {(target, name) for name in argument}
        elif kind in ("change_coins", "change_health"):
            written.add(("resource", kind.split("_", 1)[1]))
            if kind == "change_coins" and _is_number(argument) and argument > 0:
                written.add(("coins_gain", None))
        else:
            written.add((target, argument))
    return frozenset(written)


class EffectList:
    """An effect list read once. ``checked``: validated against the world it runs in."""

    __slots__ = ("items", "checked", "writes")

    def __init__(self, raw=None, checked=False):
        self.items = normalize(raw)
        self.checked = checked
        self.writes = writes(self.items)

    @classmethod
    def of(cls, raw):
        """``raw`` itself if it is already an EffectList (keeps ``checked``)."""
        return raw if isinstance(raw, cls) else cls(raw)

    def __bool__(self):
        return bool(self.items)

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)

    def __repr__(self):
        return f"EffectList({list(self.items)!r})"

    def __copy__(self):
        return self  # shared: the items never change

    def __deepcopy__(self, memo):
        return self


# ---- validating --------------------------------------------------------------------

def validate(raw, refs=None, where="effects"):
    """Problems with an effect list: structure, references, value types and
    writable fields (empty when valid)."""
    refs = refs or References()
    try:
        items = normalize(raw)
    except EffectError as exc:
        return [f"`{where}`: {exc}."]
    errors = []
    for index, (kind, argument) in enumerate(items, start=1):
        errors.extend(_check(kind, argument, refs, f"`{where}` effect {index} (`{kind}`)"))
    return errors


def _check(kind, argument, refs, label):
    shape = EFFECTS[kind]
    errors = []
    if kind in ("set_variable", "change_variable"):
        for name, value in argument:
            type_name = refs.variable_type(name)
            if name in BUILTIN_VARIABLES:
                errors.append(f"{label}: `{name}` is built in and read-only.")
            elif refs.variables is not None and name not in refs.variables:
                errors.append(f"{label}: undefined variable `{name}`.")
            elif kind == "set_variable" and type_name and not matches_type(value, type_name, strict=True):
                errors.append(f"{label}: `{name}` is a {type_name}, so it cannot be set to {value!r}.")
            elif kind == "change_variable" and type_name and type_name not in ("int", "float"):
                errors.append(f"{label}: `{name}` is a {type_name}; only numbers change by a delta (use set_variable).")
            elif kind == "change_variable" and not _is_number(value):
                errors.append(f"{label}: the change of `{name}` must be a number.")
        return errors
    if kind in ("set_stat", "change_stat"):
        for name, value in argument:
            if refs.stats is not None and name not in refs.stats:
                errors.append(f"{label}: undefined stat `{name}`.")
            elif not _is_whole(value):
                errors.append(f"{label}: `{name}` needs a whole number.")
            elif kind == "set_stat" and not STAT_RANGE[0] <= value <= STAT_RANGE[1]:
                errors.append(f"{label}: `{name}` must be set between {STAT_RANGE[0]} and {STAT_RANGE[1]}.")
        return errors
    if shape == "number":
        return [] if _is_whole(argument) else [f"{label} needs a whole number."]
    if shape == "text":
        return [] if isinstance(argument, str) and argument.strip() else [f"{label} needs the story's closing text."]
    if shape == "mission":
        if argument is True or (isinstance(argument, str) and (refs.missions is None or argument in refs.missions)):
            return []
        return [f"{label} needs `true` (the story's mission) or a mission id, not {argument!r}."]
    if shape == "actor" and argument is True:
        return []
    names = argument if shape == "items" else (argument,)
    if not names:
        return [f"{label} needs a name."]
    known = getattr(refs, _REFERENCES["npc" if shape == "actor" else shape])
    for name in names:
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{label} needs a name" + (" (or `true` for the acting NPC)." if shape == "actor" else "."))
        elif known is not None and name not in known:
            errors.append(f"{label}: unknown {'NPC' if shape in ('npc', 'actor') else shape.rstrip('s')} `{name}`.")
    return errors
