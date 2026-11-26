"""People and factions that remember: NPC relationships, factions, personal goals and autonomous
reactions (``relationships`` in the tuning; ``factions`` and the NPC fields below in the world).

It extends the NPCs the engine already has (their goals, tags, homes, dialogue and actions) and
the factions the encounter director already knows (``encounters.factions``); it is not a separate
character simulation. Everything runs on the world's own conditions and effects, never rolls the
story's dice, and is plain data on the GameState, so snapshots and save games carry it.

**An NPC's state** (``state.npc_relations``, made the first time something touches them)::

    {trust, hostility, disposition, memories: [{turn, kind, how, about, location}]}

Its *attitude* is ``trust - hostility + relationships.faction_weight x its faction's standing``.
Its *disposition* follows from it: ``hostile`` (hostility >= hostile_at, attitude <= -hostile_at, or
its faction hostile and trust below friendly_at), ``ally`` (a goal of theirs fulfilled), ``friendly``
(attitude >= friendly_at), ``wary`` (attitude below 0), else ``neutral``. A change of disposition is
a *transition*: logged, told when the player knows them, and a signal reactions can answer.

**The world** (all optional; worlds without them play as before)::

    factions:
      City Watch:
        standing: 0              # where the player starts with them (or `variable`: a persistent one)
        hostile_below: -2        # hostile while the standing is below this
        friendly_at: 2           # friendly from this standing up
        relations: {Thieves Guild: -0.5}   # a standing change spills over (x this) to these factions
        reacts_to: {theft: -1, crime: -1}  # deeds witnessed by a member (or against one) move the standing
        everywhere: false        # true: they hear of every such deed, witnessed or not
        persist: false           # true with `variable`: the standing is kept in that persistent variable
        public: true             # the player sees who belongs to it
        reactions: [...]         # as an NPC's, by any member (in the scene, or anywhere)
    characters:
    - name: Gate Guard
      faction: City Watch
      relationship: {trust: 0, hostility: 0, persist: true}   # persist: kept in the profile's next stories
      related: {Captain of the Watch: 0.5}   # what is done to them reaches these people (x this)
      never: [offer_quest]       # behaviours the director may never make them perform
      hostile_event: guard_arrest   # once they turn hostile, they confront the player with this event
      activities: [{text: "...", tags_any: [market], disposition: [friendly]}]
      goals:
      - goal: Maintain order     # keyword matching still works for a sentence alone (the fallback)
        id: maintain_order
        supports: [arrest]       # explicit: an action with them whose outcome has one of these tags helps
        progress_when: {...}     # explicit: an action with them helps when this holds after it
        complete_if: {...}       # explicit: fulfilled as soon as this holds (any turn)
        opposes: [theft]         # what they are against: such an outcome they suffer or see sets it back
        needed: 2
        trust: 2                 # trust when fulfilled (relationships.goal_trust)
        on_complete: [effects]
      reactions:
      - signal: quest_completed  # what it answers (SIGNALS); without one, whenever `when` holds
        quest: a_gift_for_the_chapel
        about: me                # me | faction | related | anyone: whose deed counts
        when: {npc_trust: Priest, op: gte, value: 2}
        behaviour: offer_quest   # offer_quest | request_help | warn | confront | move | praise | threaten
        side_quest: the_bell_tower   # offered (offer_quest, request_help)
        event: arrest            # started, with them behind it (confront)
        move_to: Palace          # or move_to_tags: [religion] (move)
        where: scene             # scene (they must be here) | seek (they come to the player) | anywhere
        text: "..."              # told ({npc} {location} {faction})
        trust: 1                 # and hostility, standing {faction: n}, effects [...]
        once: true               # (cooldown: turns, when not once)
        expires: 8               # turns it waits to be delivered

**Deeds.** What the player does to people is a deed: ``helped``, ``betrayed``, ``theft``, ``crime``,
``attacked`` (an outcome tagged as ``relationships.deed_tags`` says, or the ``deed`` effect), and the
story's own: ``quest_completed`` / ``quest_failed`` / ``quest_declined`` (to a side quest's giver).
A deed changes its target's trust and hostility, what the people related to them think, the
witnesses' (the NPCs in the scene; not for attacking someone already hostile), and the standing of
the factions concerned (``relationships.deeds``). Each is remembered by those who suffered, saw or
heard of it (``memories``, at most ``max_memories``).

**Conditions** read it: ``npc_trust: <npc>`` / ``npc_hostility: <npc>`` / ``faction_standing: <faction>``
(numbers), ``npc_disposition: <npc>`` (eq / in), ``faction_hostile`` / ``faction_friendly: <faction>``,
``npc_ally: <npc>``, ``npc_goal_done: <npc>/<goal id or text>``, ``npc_remembers: <npc>/<kind>``.
**Effects** change it: ``change_trust`` / ``change_hostility`` ({npc: n}, or n for the acting NPC),
``change_standing`` ({faction: n}), ``deed`` (a kind, or {kind, npc}), ``move_npc`` ({npc: place}).

**The director's guard** (``permits``): no behaviour is performed by an NPC who is gone or switched
off, who ``never`` does it, whose disposition contradicts it (a hostile NPC offers nothing, a
friendly one confronts no one) or whose goals oppose what it is about (its tags). It holds for
reactions, side quests offered by a giver, encounters behind an NPC, and a hostile NPC's friendly
actions (``hostile_refuses_tags``), unless the author's own condition on that action asks about the
relationship.
"""
from __future__ import annotations

import re

from utils.config_loader import config

from .conditions import Condition
from .context import with_state_context
from .effects import EffectList

DISPOSITIONS = ("hostile", "wary", "neutral", "friendly", "ally")
BEHAVIOURS = ("offer_quest", "request_help", "warn", "confront", "move", "praise", "threaten")
FRIENDLY_BEHAVIOURS = frozenset({"offer_quest", "request_help", "praise"})
HOSTILE_BEHAVIOURS = frozenset({"confront", "threaten", "attack"})
DEEDS = ("helped", "betrayed", "theft", "crime", "attacked")
QUEST_SIGNALS = ("quest_completed", "quest_failed", "quest_declined")
SIGNALS = DEEDS + QUEST_SIGNALS + ("goal_completed", "goal_opposed", "mission_stage", "mission_complete",
                                    "disposition", "faction", "variable", "met")
ABOUT = ("me", "faction", "related", "anyone")
WHERE = ("scene", "seek", "anywhere")
REACTION_FIELDS = ("signal", "when", "about", "quest", "variable", "faction", "to", "stage", "once", "cooldown",
                   "behaviour", "text", "side_quest", "event", "move_to", "move_to_tags", "effects", "trust",
                   "hostility", "standing", "where", "expires", "tags")
FACTION_FIELDS = ("description", "standing", "hostile_below", "friendly_at", "variable", "persist", "relations",
                  "members", "reacts_to", "everywhere", "public", "reactions")
GOAL_FIELDS = ("goal", "text", "id", "tags", "needed", "reward", "supports", "opposes", "progress_when", "complete_if",
               "trust", "on_complete")
NPC_FIELDS = ("faction", "relationship", "related", "never", "hostile_event", "activities", "reactions", "haunts")
PERSIST = ("none", "configured", "all")
# What the decision trace's relationship codes mean.
CODES = {
    "npc_gone": "The NPC is gone from the story.",
    "npc_disabled": "The NPC is switched off (enabled_if) or out of the session's cast.",
    "unknown_npc": "No such NPC in this world.",
    "forbidden": "The NPC never does this (`never`).",
    "hostile": "The NPC is hostile: they offer, ask and praise nothing.",
    "friendly": "The NPC is friendly or an ally: they confront and threaten no one.",
    "opposes_goal": "It goes against one of the NPC's goals (`opposes`).",
    "absent": "The NPC is not in the scene (`where: scene`).",
    "condition": "Its `when` does not hold now.",
    "busy": "The story is in an event, or has ended.",
    "delivered": "Done.",
    "expired": "It waited longer than `expires` turns.",
}

DEFAULTS = {
    "enabled": True,
    "friendly_at": 2,             # attitude from which an NPC is friendly
    "hostile_at": 3,              # hostility (or -attitude) from which an NPC is hostile
    "faction_weight": 0.5,        # x a faction's standing in its members' attitude
    "faction_hostile_below": -2,  # a world faction's hostile_below unless it sets one
    "limits": {"trust": [-10, 10], "hostility": [0, 10], "standing": [-10, 10]},
    "max_memories": 8,
    "kin": 0.5,                   # how far word travels between the copies of a generic NPC (1: one person)
    "haunt_weight": 2.0,          # x a generic NPC's presence here: regulars are shown before occasional visitors
    "reaction_pull": 1.0,         # the storyteller's pull toward a place where someone waits with a reaction
    "reaction_expiry": 8,         # turns a reaction waits to be delivered
    "max_reactions_per_turn": 1,
    "announce": True,             # tell transitions the player can know of
    "witnesses": True,            # the NPCs in the scene see what the player does
    "persist": "configured",      # none | configured (relationship.persist / faction persist) | all
    "goal_trust": 2,              # trust gained when one of an NPC's goals is fulfilled
    "goal_setback": {"trust": -1, "hostility": 1},   # an outcome a goal opposes
    "hostile_refuses_tags": ["social", "trade", "information", "help", "blessing"],
    "aggressive_tags": ["combat", "threat", "confrontation", "brawl", "stealth", "escape", "deception"],
    "appearance": {"ally": 1.0, "friendly": 0.5, "neutral": 0.0, "wary": -0.25, "hostile": 0.0},
    "deed_tags": {"theft": ["theft", "steal", "stolen", "pickpocket"], "crime": ["crime", "murder", "vandalism"],
                  "attacked": ["attack", "assault"], "betrayed": ["betrayal", "betray", "betrayed"],
                  "helped": ["help", "helped", "rescue", "aid"]},
    "deeds": {
        "helped": {"trust": 1, "hostility": -1, "faction": 0.5, "related": 0.5},
        "betrayed": {"trust": -3, "hostility": 2, "faction": -1, "related": 0.5, "witness_trust": -1},
        "theft": {"trust": -1, "hostility": 1, "faction": -0.5, "related": 0.5, "witness_hostility": 1},
        "crime": {"hostility": 1, "faction": -1, "related": 0.5, "witness_hostility": 1},
        "attacked": {"trust": -2, "hostility": 3, "faction": -1, "related": 0.5, "witness_hostility": 1},
        "quest_completed": {"trust": 2, "faction": 1, "related": 0.5},
        "quest_failed": {"trust": -1, "faction": -0.5},
        "quest_declined": {},
    },
}


def settings():
    """``relationships`` in the tuning, its defaults filled in."""
    own = config.get("relationships") or {}
    merged = {**DEFAULTS, **{key: value for key, value in own.items() if value is not None}}
    for key in ("limits", "appearance", "deed_tags", "goal_setback"):
        merged[key] = {**DEFAULTS[key], **(own.get(key) or {})}
    merged["deeds"] = {kind: {**(DEFAULTS["deeds"].get(kind) or {}), **((own.get("deeds") or {}).get(kind) or {})}
                       for kind in set(DEFAULTS["deeds"]) | set(own.get("deeds") or {})}
    return merged


def enabled():
    return bool((config.get("relationships") or {}).get("enabled", True))


def _num(value):
    value = round(float(value), 2)
    return int(value) if value.is_integer() else value


def _clamp(kind, value, cfg):
    low, high = (cfg["limits"].get(kind) or [None, None])[:2]
    if low is not None:
        value = max(float(low), value)
    if high is not None:
        value = min(float(high), value)
    return _num(value)


def _lower(values):
    return {str(value).lower() for value in values or ()}


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")


# ---- the cast and the factions ---------------------------------------------------------------------

def split(name):
    """(NPC, place) of a name: ``Merchant@Market`` is the Merchant of the Market (one copy of a generic
    NPC); a plain name has no place."""
    base, at, place = str(name).partition("@")
    return base, (place or None) if at else None


def character(state, name):
    """The NPC a name stands for (``Merchant@Market`` too), or None."""
    if not name:
        return None
    world = state.world
    key = tuple(id(c) for c in world.characters)
    cached = world.__dict__.get("_relations_cast")
    if cached is None or cached[0] != key:
        cached = (key, {c.name: c for c in world.characters})
        world.__dict__["_relations_cast"] = cached
    return cached[1].get(split(name)[0])


def key_of(state, npc, place=None):
    """Whose record an NPC's name stands for: a unique NPC's own; for a generic NPC (one of many guards,
    merchants...) the copy of ``place`` (the name's own ``@place``, else where the player is), unless
    their kin is 1 (``relationship.kin``: one record for all of them)."""
    base, own = split(npc)
    char = character(state, base)
    if char is None or char.unique or _kin(char) >= 1.0:
        return base
    place = own or place or (state.current.name if state.current is not None else None)
    return f"{base}@{place}" if place else base


def _kin(char):
    """How far word of the player travels between the copies of a generic NPC (0: not at all, 1: they
    are one person): their ``relationship.kin``, else relationships.kin."""
    if char is None or char.unique:
        return 1.0
    own = (getattr(char, "relationship", None) or {}).get("kin")
    return max(0.0, min(1.0, float(settings()["kin"] if own is None else own)))


def faction_spec(name, raw, world_defined=True, cfg=None):
    """A faction as the engine reads it (world ``factions`` and the tuning's ``encounters.factions``)."""
    raw = dict(raw or {})
    cfg = cfg or settings()
    hostile_below = raw.get("hostile_below", cfg["faction_hostile_below"] if world_defined else 0)
    return {
        "name": name, "description": str(raw.get("description") or ""),
        "standing": float(raw.get("standing", 0) or 0), "hostile_below": float(hostile_below),
        "friendly_at": float(raw.get("friendly_at", cfg["friendly_at"])), "variable": raw.get("variable") or None,
        "persist": bool(raw.get("persist", False)),
        "relations": {str(other): float(factor) for other, factor in (raw.get("relations") or {}).items()},
        "members": [str(member) for member in raw.get("members") or ()],
        "reacts_to": {str(kind): float(delta) for kind, delta in (raw.get("reacts_to") or {}).items()},
        "everywhere": bool(raw.get("everywhere", False)), "public": bool(raw.get("public", True)),
        "reactions": list(raw.get("reactions") or ()), "world": world_defined,
    }


def factions(state):
    """{name: spec} of every faction: the world's, then those only the tuning names (encounters.factions).
    Read once per world and configuration (a cache on the world, which snapshots never carry)."""
    world = getattr(state.world, "factions", None) or {}
    key = (id(world), len(world), id(config.get("encounters")), id(config.get("relationships")))
    cached = state.world.__dict__.get("_relations_factions")
    if cached is not None and cached[0] == key:
        return cached[1]
    from .encounters import settings as encounter_settings

    cfg = settings()
    tuning = encounter_settings()["factions"]
    found = {name: faction_spec(name, spec, world_defined=False, cfg=cfg) for name, spec in tuning.items()}
    for name, spec in world.items():
        found[name] = faction_spec(name, {**(tuning.get(name) or {}), **spec}, world_defined=True, cfg=cfg)
    state.world.__dict__["_relations_factions"] = (key, found)
    return found


def faction_of(state, npc):
    """The faction an NPC belongs to: their own ``faction``, else a faction that lists them."""
    char = character(state, npc)
    own = getattr(char, "faction", None) if char is not None else None
    if own:
        return own
    for name, spec in factions(state).items():
        if npc in spec["members"]:
            return name
    return None


def members(state, faction):
    names = [c.name for c in state.world.characters if faction_of(state, c.name) == faction]
    return sorted(set(names))


@with_state_context
def standing(state, faction):
    """A faction's standing with the player: its own (or its persistent variable's), changed by this
    story (``state.faction_shift``)."""
    spec = factions(state).get(faction) or faction_spec(faction, {}, world_defined=False)
    base = spec["standing"]
    if spec["variable"] and spec["variable"] in getattr(state, "variables", {}):
        try:
            base = float(state.variables.get(spec["variable"], base))
        except (TypeError, ValueError):
            pass
    return base + float((state.__dict__.get("faction_shift") or {}).get(faction, 0))


def faction_status(state, faction):
    """hostile | friendly | neutral."""
    spec = factions(state).get(faction) or faction_spec(faction, {}, world_defined=False)
    value = standing(state, faction)
    if value < spec["hostile_below"]:
        return "hostile"
    return "friendly" if value >= spec["friendly_at"] else "neutral"


@with_state_context
def change_standing(state, faction, delta, cause=None, spill=True):
    """Move a faction's standing (and, x their relation, the factions it is related to)."""
    if not faction or not delta:
        return
    cfg = settings()
    specs = factions(state)
    spec = specs.get(faction) or faction_spec(faction, {}, world_defined=False, cfg=cfg)
    before = faction_status(state, faction)
    current = standing(state, faction)
    target = _clamp("standing", current + float(delta), cfg)
    if spec["variable"] and spec["persist"] and spec["variable"] in getattr(state, "variable_specs", {}):
        base = target - float((state.__dict__.get("faction_shift") or {}).get(faction, 0))
        state._write_variable(spec["variable"], int(round(base)) if (state.variable_specs[spec["variable"]] or {}).get(
            "type") == "int" else base)
    else:
        shift = state.__dict__.setdefault("faction_shift", {})
        shift[faction] = _num(float(shift.get(faction, 0)) + (target - current))
    _bump(state)
    after = faction_status(state, faction)
    if after != before:
        _log(state, {"what": "faction", "faction": faction, "from": before, "to": after, "cause": cause})
        if cfg["announce"]:
            state._say(f"faction_now_{after}", faction=faction)
        signal(state, "faction", faction=faction, to=after)
    for member in members(state, faction):
        settle(state, member, cause=cause or "faction")
    if spill:
        for other, factor in sorted(spec["relations"].items()):
            if other != faction:
                change_standing(state, other, float(delta) * factor, cause=cause, spill=False)


# ---- one NPC's state ------------------------------------------------------------------------------

def _bump(state):
    state.__dict__["relationship_version"] = int(state.__dict__.get("relationship_version", 0)) + 1


def _initial(char):
    spec = dict(getattr(char, "relationship", None) or {})
    return float(spec.get("trust", 0) or 0), float(spec.get("hostility", 0) or 0)


def record(state, npc, create=False):
    """The record of an NPC (of the copy ``key_of`` names): {trust, hostility, disposition, memories},
    their own (a new one when ``create``), or a view of their initial state."""
    key = key_of(state, npc)
    records = state.__dict__.setdefault("npc_relations", {}) if create else (state.__dict__.get("npc_relations") or {})
    found = records.get(key)
    if found is not None:
        return found
    trust_value, hostility_value = _initial(character(state, key))
    fresh = {"trust": _num(trust_value), "hostility": _num(hostility_value), "disposition": None, "memories": []}
    if create:
        records[key] = fresh
    return fresh


def _effective(state, npc, field):
    """Trust or hostility as the NPC holds it: their own, and for a copy of a generic NPC what their kin
    heard (relationships.kin x the changes the other copies went through: ``state.npc_kin``)."""
    key = key_of(state, npc)
    value = float(record(state, key)[field])
    base, place = split(key)
    if place is None:
        return _num(value)
    char = character(state, base)
    initial = _initial(char)[0 if field == "trust" else 1]
    told = float(((state.__dict__.get("npc_kin") or {}).get(base) or {}).get(field, 0))
    value += _kin(char) * (told - (value - initial))
    return _clamp(field, value, settings())


def trust(state, npc):
    return _effective(state, npc, "trust")


def hostility(state, npc):
    return _effective(state, npc, "hostility")


def attitude(state, npc):
    value = trust(state, npc) - hostility(state, npc)
    faction = faction_of(state, npc)
    if faction:
        value += float(settings()["faction_weight"]) * standing(state, faction)
    return _num(value)


@with_state_context
def disposition(state, npc):
    """hostile | wary | neutral | friendly | ally (module docstring)."""
    if character(state, npc) is None:
        return None
    cfg = settings()
    value = attitude(state, npc)
    faction = faction_of(state, npc)
    hostile_at, friendly_at = float(cfg["hostile_at"]), float(cfg["friendly_at"])
    if hostility(state, npc) >= hostile_at or value <= -hostile_at or (
            faction and faction_status(state, faction) == "hostile" and trust(state, npc) < friendly_at):
        return "hostile"
    if split(npc)[0] in (state.__dict__.get("allies") or ()):
        return "ally"
    if value >= friendly_at:
        return "friendly"
    return "wary" if value < 0 else "neutral"


def _known_to_player(state, npc):
    name = split(npc)[0]
    return name in (state.__dict__.get("met") or {}) or name in (state.__dict__.get("visible_inhabitants") or ())


@with_state_context
def settle(state, npc, cause=None):
    """Note the NPC's disposition (of the copy ``key_of`` names); a change is a transition (logged,
    told when the player knows them, a signal)."""
    if character(state, npc) is None:
        return None
    key = key_of(state, npc)
    rec = record(state, key, create=True)
    now = disposition(state, key)
    before = rec.get("disposition") or _base_disposition(state, key)
    rec["disposition"] = now
    if now == before:
        return now
    base, place = split(key)
    _log(state, {"what": "disposition", "npc": base, "place": place, "from": before, "to": now, "cause": cause})
    here = place is None or state.current is None or place == state.current.name
    if settings()["announce"] and now != "ally" and here and _known_to_player(state, base):
        called = state._called(base)
        state._say(f"npc_now_{now}", npc=called[:1].upper() + called[1:])
    signal(state, "disposition", npc=base, to=now)
    return now


def _base_disposition(state, npc):
    """The disposition before anything happened in this story: their initial trust and hostility and
    their faction's standing as written (a faction hostile from the start makes no transition)."""
    trust_value, hostility_value = _initial(character(state, npc))
    cfg = settings()
    value = trust_value - hostility_value
    faction = faction_of(state, npc)
    faction_hostile = False
    if faction:
        spec = factions(state).get(faction) or faction_spec(faction, {}, world_defined=False, cfg=cfg)
        value += float(cfg["faction_weight"]) * spec["standing"]
        faction_hostile = spec["standing"] < spec["hostile_below"]
    if hostility_value >= float(cfg["hostile_at"]) or value <= -float(cfg["hostile_at"]) or (
            faction_hostile and trust_value < float(cfg["friendly_at"])):
        return "hostile"
    return "friendly" if value >= float(cfg["friendly_at"]) else "wary" if value < 0 else "neutral"


@with_state_context
def change(state, npc, trust=0, hostility=0, cause=None):
    """Change an NPC's trust and hostility (clamped; for a copy of a generic NPC, their kin hear of it:
    ``npc_kin``), then settle their disposition."""
    if character(state, npc) is None or (not trust and not hostility):
        return
    cfg = settings()
    key = key_of(state, npc)
    rec = record(state, key, create=True)
    base, place = split(key)
    told = state.__dict__.setdefault("npc_kin", {}).setdefault(base, {}) if place else None
    for field, delta in (("trust", trust), ("hostility", hostility)):
        if not delta:
            continue
        old = float(rec[field])
        rec[field] = _clamp(field, old + float(delta), cfg)
        if told is not None:
            told[field] = _num(float(told.get(field, 0)) + float(rec[field]) - old)
    _bump(state)
    settle(state, key, cause=cause)


def remember(state, npc, kind, how="target", about=None):
    """The NPC remembers something the player did (``how``: target, witness, heard)."""
    if character(state, npc) is None:
        return
    rec = record(state, npc, create=True)
    rec["memories"].append({"turn": state.actions_taken, "kind": str(kind), "how": how, "about": about,
                            "location": state.current.name if state.current is not None else None})
    limit = max(1, int(settings()["max_memories"]))
    if len(rec["memories"]) > limit:
        del rec["memories"][:len(rec["memories"]) - limit]
    _bump(state)


def remembers(state, npc, kind):
    return any(m.get("kind") == kind for m in record(state, npc).get("memories") or ())


def _log(state, entry):
    state.__dict__.setdefault("relationship_log", []).append({"turn": state.actions_taken, **entry})


# ---- deeds: what the player does to people ----------------------------------------------------------

def _witnesses(state, exclude=()):
    seen = [name for name in sorted(state.__dict__.get("visible_inhabitants") or ())
            if name not in exclude and not state._is_removed(name)]
    return seen


def _enemies(state, faction, other):
    """Whether two factions are set against each other (a negative relation either way)."""
    if not faction or not other or faction == other:
        return False
    specs = factions(state)
    return (specs.get(faction) or {}).get("relations", {}).get(other, 0) < 0 or \
        (specs.get(other) or {}).get("relations", {}).get(faction, 0) < 0


@with_state_context
def deed(state, kind, target=None, witnesses=None, quest=None):
    """The player did ``kind`` to ``target`` (an NPC or None) before ``witnesses`` (default: the NPCs
    in the scene): trust, hostility, memories, the people related to the target, faction standings,
    then the reactions it calls for (module docstring)."""
    if not enabled():
        return
    cfg = settings()
    spec = cfg["deeds"].get(kind) or {}
    target = target if character(state, target) is not None else None
    seen = _witnesses(state, exclude={target}) if witnesses is None else [w for w in witnesses if w != target]
    justified = kind == "attacked" and target is not None and disposition(state, target) == "hostile"
    if target is not None:
        remember(state, target, kind, "target")
        change(state, target, spec.get("trust", 0), spec.get("hostility", 0), cause=kind)
        char = character(state, target)
        for other, factor in sorted((getattr(char, "related", None) or {}).items()):
            if character(state, other) is None or other == target or state._is_removed(other):
                continue
            factor = float(factor) * float(spec.get("related", 1.0) if spec.get("related") is not None else 1.0)
            if other not in seen:
                remember(state, other, kind, "heard", about=target)
            change(state, other, float(spec.get("trust", 0)) * factor, float(spec.get("hostility", 0)) * factor,
                   cause=kind)
    target_faction = faction_of(state, target) if target else None
    if cfg["witnesses"] and not justified:
        for witness in seen:
            if _enemies(state, faction_of(state, witness), target_faction):
                continue  # what happens to their enemies does not trouble them
            remember(state, witness, kind, "witness", about=target)
            change(state, witness, spec.get("witness_trust", 0), spec.get("witness_hostility", 0), cause=kind)
    shifts = {}
    specs = factions(state)
    if target_faction and not justified:
        delta = specs.get(target_faction, {}).get("reacts_to", {}).get(kind, spec.get("faction", 0))
        if delta:
            shifts[target_faction] = float(delta)
    witnessed_by = {faction_of(state, w) for w in seen}
    for name, faction in sorted(specs.items()):
        delta = faction["reacts_to"].get(kind)
        if delta and name not in shifts and not (justified and name == target_faction) and (
                faction["everywhere"] or name in witnessed_by):
            shifts[name] = float(delta)
    for name, delta in sorted(shifts.items()):
        change_standing(state, name, delta, cause=kind)
    state.__dict__.setdefault("npc_deeds", []).append({
        "turn": state.actions_taken, "kind": kind, "target": target, "witnesses": list(seen),
        "location": state.current.name if state.current is not None else None, "quest": quest,
        "justified": justified})
    signal(state, kind, npc=target, witnesses=seen, quest=quest)


def deed_kinds(tags, cfg=None):
    """The deeds an outcome's tags stand for (relationships.deed_tags), in DEEDS order."""
    cfg = cfg or settings()
    tags = _lower(tags)
    return [kind for kind in DEEDS if tags & _lower(cfg["deed_tags"].get(kind))]


@with_state_context
def after_outcome(state, outcome, action):
    """After an outcome is applied (GameState.apply): the deeds its tags stand for, and the goals of
    the people concerned that it sets back (``opposes``)."""
    if not enabled() or outcome is None:
        return
    cfg = settings()
    target = None
    if action is not None:
        target = action.character_name or (state.event_source_npc if action.source == "event" else None)
    if action is not None and action.source in ("navigation", "conversation"):
        return
    for kind in deed_kinds(outcome.tags, cfg):
        deed(state, kind, target=target)
    tags = _lower(outcome.tags)
    concerned = ([target] if target else []) + _witnesses(state, exclude={target})
    for npc in concerned:
        char = character(state, npc)
        if char is None:
            continue
        done = goals_done(state, npc)
        for index, goal in enumerate(state._goal_specs(char)):
            if index in done or not (tags & _lower(goal.get("opposes"))):
                continue
            progress = state.__dict__.setdefault("npc_goal_progress", {}).setdefault(npc, {})
            progress[index] = max(0, progress.get(index, 0) - 1)
            remember(state, npc, "goal_opposed", "target" if npc == target else "witness")
            change(state, npc, cfg["goal_setback"].get("trust", 0), cfg["goal_setback"].get("hostility", 0),
                   cause="goal_opposed")
            signal(state, "goal_opposed", npc=npc, goal=goal.get("id"))


# ---- personal goals ---------------------------------------------------------------------------------

def goals_done(state, npc):
    return set((state.__dict__.get("npc_goals_done") or {}).get(npc) or ())


def goal_index(state, npc, goal):
    """The index of an NPC's goal named by id or text (case does not count), or None."""
    char = character(state, npc)
    if char is None:
        return None
    wanted = str(goal).strip().lower()
    for index, spec in enumerate(state._goal_specs(char)):
        if wanted in (str(spec.get("id") or "").lower(), str(spec.get("text") or "").lower(), slug(spec.get("text"))):
            return index
    return None


def explicit_progress(state, char, outcome, action):
    """The explicit goals of ``char`` (supports, progress_when) an action with them advances: indexes."""
    tags = _lower(outcome.tags)
    done = goals_done(state, char.name)
    hits = []
    for index, goal in enumerate(state._goal_specs(char)):
        if not goal.get("explicit") or index in done:
            continue
        condition = goal.get("progress_when")
        if (tags & _lower(goal.get("supports"))) or (condition is not None and Condition.of(condition).holds(state.facts)):
            hits.append(index)
    return hits


def goal_fulfilled(state, char, goal, index=None):
    """One of an NPC's goals is fulfilled (GameState._fulfil_npc_goal): trust, a memory, its own
    effects (on_complete) and the signal."""
    if index is None:
        index = next((i for i, g in enumerate(state._goal_specs(char)) if g is goal), None)
    if index is not None:
        done = state.__dict__.setdefault("npc_goals_done", {}).setdefault(char.name, [])
        if index not in done:
            done.append(index)
    if not enabled():
        return
    remember(state, char.name, "goal_completed", "target", about=goal.get("id"))
    gained = goal.get("trust")
    change(state, char.name, float(settings()["goal_trust"] if gained is None else gained), 0, cause="goal_completed")
    settle(state, char.name, cause="goal_completed")
    if goal.get("on_complete"):
        state._run_effects(goal["on_complete"], npc=char.name)
    signal(state, "goal_completed", npc=char.name, goal=goal.get("id"))


def _check_goal_conditions(state):
    """Explicit goals whose ``complete_if`` holds now are fulfilled."""
    for char in state.world.characters:
        specs = state._goal_specs(char)
        if not specs or not any(g.get("complete_if") is not None for g in specs):
            continue
        if state._is_removed(char.name):
            continue
        done = goals_done(state, char.name)
        for index, goal in enumerate(specs):
            if index in done or goal.get("complete_if") is None:
                continue
            if Condition.of(goal["complete_if"]).holds(state.facts):
                state._fulfil_npc_goal(char, goal, index=index)


# ---- training: what the storyteller is pulled toward, and what a story is worth -----------------------

def would_help(state, char, outcome):
    """Whether ``outcome`` would advance one of ``char``'s explicit goals by its tags (``supports``): the
    storyteller's pull toward helping someone (GameState._rank_actions, npc_goals.action_weight), as
    the keyword goals have."""
    tags = _lower(outcome.tags)
    if not tags:
        return False
    done = goals_done(state, char.name)
    return any(goal.get("explicit") and index not in done and tags & _lower(goal.get("supports"))
               for index, goal in enumerate(state._goal_specs(char)))


def reaction_pull(state, action):
    """How much a way to a place pulls the storyteller because someone waits there with a reaction that
    needs them in the scene (relationships.reaction_pull; 0 for anything else)."""
    due = state.__dict__.get("npc_reactions_due") or ()
    if not due or action.source != "navigation" or not enabled():
        return 0.0
    place = state.world.locations.get(action.owner)
    if place is None:
        return 0.0
    for item in due:
        kind, owner = item["owner"]
        rx = _lookup(state, item)
        if rx is None or rx["where"] != "scene":
            continue
        for name in ([owner] if kind == "npc" else members(state, owner)):
            char = character(state, name)
            if char is None:
                continue
            if (state.npc_homes.get(name) == place.name) if char.unique else bool(presence(state, char, place)):
                return float(settings()["reaction_pull"])
    return 0.0


def fitness_term(state, cfg):
    """The relationships' share of a story's fitness (fitness version 3; ``cfg``: fitness.relationships):
    people whose feelings about the player changed (each distinct transition: an NPC, or a copy of one,
    from one disposition to another), reactions delivered, and factions whose status changed. Each one
    more is worth ``diminishing`` x the one before, so the term is bounded however many a story holds
    and going back and forth earns nothing: {relationship_changes, npc_reactions, faction_changes}."""
    decay = min(0.95, max(0.0, float(cfg.get("diminishing", 0.6))))
    log = state.__dict__.get("relationship_log") or ()
    changes = {(e.get("npc"), e.get("place"), e.get("from"), e.get("to")) for e in log if e.get("what") == "disposition"}
    reactions = {e.get("id") for e in log if e.get("what") == "reaction"}
    factions_changed = {(e.get("faction"), e.get("to")) for e in log if e.get("what") == "faction"}

    def series(count):
        return sum(decay ** index for index in range(count))

    return {"relationship_changes": series(len(changes)) * float(cfg.get("change_bonus", 3)),
            "npc_reactions": series(len(reactions)) * float(cfg.get("reaction_bonus", 2)),
            "faction_changes": series(len(factions_changed)) * float(cfg.get("faction_bonus", 3))}


# ---- the director's guard ----------------------------------------------------------------------------

@with_state_context
def permits(state, npc, behaviour, tags=()):
    """Why ``npc`` may not perform ``behaviour`` now (a code of CODES), or None when they may."""
    char = character(state, npc)
    if char is None:
        return "unknown_npc"
    if npc in state.removed_npcs or (not char.unique and state._is_removed(npc)):
        return "npc_gone"
    if not state.is_enabled(char):
        return "npc_disabled"
    if behaviour in set(getattr(char, "never", None) or ()):
        return "forbidden"
    if not enabled():
        return None
    now = disposition(state, npc)
    if behaviour in FRIENDLY_BEHAVIOURS and now == "hostile":
        return "hostile"
    if behaviour in HOSTILE_BEHAVIOURS and now in ("friendly", "ally"):
        return "friendly"
    tags = _lower(tags)
    if tags:
        done = goals_done(state, npc)
        for index, goal in enumerate(state._goal_specs(char)):
            if index not in done and tags & _lower(goal.get("opposes")):
                return "opposes_goal"
    return None


_RELATIONSHIP_READS = {"npc_trust", "npc_hostility", "npc_disposition", "faction_standing", "faction_hostile",
                       "faction_friendly", "npc_ally", "npc_remembers"}


@with_state_context
def refuses(state, char, action):
    """Whether a hostile NPC withholds this action of theirs (its outcomes are friendly dealings:
    relationships.hostile_refuses_tags, and nothing aggressive), unless its own condition asks
    about the relationship (the author decided)."""
    if not enabled() or action.source not in ("npc",) or disposition(state, char.name) != "hostile":
        return False
    condition = getattr(action, "condition", None)
    if condition is not None and any(kind in _RELATIONSHIP_READS for kind, _ in condition.references()):
        return False
    cfg = settings()
    tags = set()
    for outcome in action.outcomes:
        tags |= _lower(outcome.tags)
    return bool(tags & _lower(cfg["hostile_refuses_tags"])) and not (tags & _lower(cfg["aggressive_tags"]))


def hostile_talk(state, char):
    """What a hostile NPC says instead of their usual words, or None."""
    if not enabled() or disposition(state, char.name) != "hostile":
        return None
    called = state._called(char.name)
    return state.narrate("npc_hostile_talk", stable=char.name, npc=called[:1].upper() + called[1:])


# ---- where people are, and what they are doing ------------------------------------------------------

def visibility_bonus(state, char):
    """How much more (or less) likely a generic NPC is to show up, by their disposition."""
    if not enabled() or not state.__dict__.get("npc_relations"):
        return 0.0
    return float(settings()["appearance"].get(disposition(state, char.name), 0.0))


def seeking(state, char):
    """Whether the NPC came to the player in this visit (a reaction that seeks them out)."""
    wanted = (state.__dict__.get("npc_seeking") or {}).get(char.name)
    return wanted is not None and list(wanted) == list(state._visit_id())


def _bring(state, npc):
    """The NPC comes to the player now (into the scene, for this visit)."""
    char = character(state, npc)
    state.__dict__.setdefault("npc_seeking", {})[npc] = list(state._visit_id())
    if npc not in state.visible_inhabitants:
        called = state._called(npc)
        state._say("npc_seeks", npc=called[:1].upper() + called[1:])
    state.visible_inhabitants.add(npc)
    if char not in state.current_inhabitants:
        state.current_inhabitants.append(char)
    state._note_met(npc)


@with_state_context
def move_npc(state, npc, to=None, tags=None):
    """Move an NPC to ``to`` (a place) or to the place their tags fit best among those with one of ``tags``
    (not where they are). A unique NPC lives there from now on; a generic one (a merchant, a guard) is
    found there every visit from now on, besides their usual places (``state.npc_haunts``). The new
    place, or None."""
    char = character(state, npc)
    npc = split(npc)[0]
    if char is None or npc in state.removed_npcs:
        return None
    here = state.npc_homes.get(npc) if char.unique else None
    extra = (state.__dict__.get("npc_haunts") or {}).get(npc) or []
    places = [loc for loc in state.world.locations.values() if loc.name != here and loc.name not in extra
              and state.is_enabled(loc) and not state._left_out(loc)]
    if to:
        target = next((loc for loc in places if loc.name == to), None)
    else:
        wanted = _lower(tags)
        fitting = [loc for loc in places if not wanted or _lower(loc.tags) & wanted]
        target = max(fitting, key=lambda loc: (state._npc_location_affinity(char, loc), loc.name), default=None) \
            if fitting else None
    if target is None:
        return None
    was_here = npc in state.visible_inhabitants and (not char.unique or here == state.current.name)
    if char.unique:
        state.npc_homes[npc] = target.name
    else:
        state.__dict__.setdefault("npc_haunts", {}).setdefault(npc, []).append(target.name)
    (state.__dict__.get("npc_seeking") or {}).pop(npc, None)
    state.__dict__.setdefault("npc_moves", []).append({"turn": state.actions_taken, "npc": npc, "from": here,
                                                       "to": target.name})
    if was_here or target.name == state.current.name:
        called = state._called(npc)
        state._say("npc_moves", npc=called[:1].upper() + called[1:], location=target.name)
    if was_here and char.unique:
        state.visible_inhabitants.discard(npc)
        state.current_inhabitants = [c for c in state.current_inhabitants if c.name != npc]
    state._scene_dirty = True
    _bump(state)
    return target.name


# ---- where generic NPCs are found: their haunts -----------------------------------------------------------

def haunts(char):
    """A generic NPC's ``haunts``, read once: [{places, tags_any, presence}] (in the order written)."""
    raw = list(getattr(char, "haunts", None) or ())
    cached = char.__dict__.get("_haunts")
    if cached is not None and cached[0] == tuple(id(entry) for entry in raw):
        return cached[1]
    found = [{"places": set((entry or {}).get("places") or ()), "tags_any": _lower((entry or {}).get("tags_any")),
              "presence": max(0.0, min(1.0, float((entry or {}).get("presence", 1.0))))}
             for entry in raw if isinstance(entry, dict)]
    char.__dict__["_haunts"] = (tuple(id(entry) for entry in raw), found)
    return found


def presence(state, char, location):
    """How often a generic NPC is found at ``location``: 1.0 every visit, 0.25 one visit in four, 0.0
    never; None for an NPC whose places their tags decide (no ``haunts``: as before). The first haunt
    that matches decides: one naming the place (``places``), sharing a tag with it (``tags_any``), or
    naming neither (anywhere their tags fit). A place they moved to (move_npc) is 1.0."""
    if char.unique:
        return None
    if location.name in ((state.__dict__.get("npc_haunts") or {}).get(char.name) or ()):
        return 1.0
    entries = haunts(char)
    if not entries:
        return None
    tags = _lower(location.tags)
    for entry in entries:
        if entry["places"] or entry["tags_any"]:
            if location.name in entry["places"] or tags & entry["tags_any"]:
                return entry["presence"]
        elif state._npc_fits_by_tags(char, location):
            return entry["presence"]
    return 0.0


def here_this_visit(state, char):
    """Whether a generic NPC with haunts is found here in this visit: always at presence 1, never at 0,
    else a draw fixed for this story, NPC, place and visit (the story's own randomness, never its dice):
    the same visit gives the same answer, and a fixed seed the same story. None: no haunts."""
    value = presence(state, char, state.current)
    if value is None:
        return None
    if value >= 1.0 or value <= 0.0:
        return value >= 1.0
    visit = state.location_counts.get(state.current.name, 0)
    return state._story_draw("haunt", char.name, state.current.name, visit) < value


def haunt_bonus(state, char):
    """A regular is shown before an occasional visitor when the scene has few places for people
    (relationships.haunt_weight x their presence here)."""
    value = presence(state, char, state.current) if state.current is not None else None
    return 0.0 if value is None else float(settings()["haunt_weight"]) * value


def activity(state, char):
    """What the NPC is doing here: the first of their ``activities`` that fits the place (tags_any),
    their disposition and its condition (``when``), or None."""
    entries = getattr(char, "activities", None) or ()
    if not entries:
        return None
    place = _lower(state.current.tags) if state.current is not None else set()
    now = disposition(state, char.name) if enabled() else None
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("text"):
            continue
        wanted = _lower(entry.get("tags_any"))
        if wanted and not (wanted & place):
            continue
        dispositions = entry.get("disposition")
        if dispositions and now not in ([dispositions] if isinstance(dispositions, str) else dispositions):
            continue
        if entry.get("when") is not None and not Condition.of(entry["when"]).holds(state.facts):
            continue
        return str(entry["text"]).format_map({"npc": state._called(char.name)})
    return None


# ---- reactions: what people do about it --------------------------------------------------------------

def _default_where(behaviour):
    if behaviour in ("confront",):
        return "seek"
    if behaviour in ("warn", "move", None):
        return "anywhere"
    return "scene"


def _signal_of(raw):
    """A reaction's ``signal`` (``on``, which YAML reads as true, is taken too)."""
    for key in ("signal", "on", True):
        if raw.get(key) not in (None, True, False):
            return raw[key]
    return None


def reaction_spec(raw, index=None):
    """A reaction as the engine reads it (normalized once)."""
    raw = dict(raw or {})
    behaviour = raw.get("behaviour") or None
    tags = raw.get("tags")
    return {
        "index": index, "on": _signal_of(raw), "about": raw.get("about") or None,
        "when": Condition(raw["when"]) if raw.get("when") else None, "quest": raw.get("quest"),
        "variable": raw.get("variable"), "faction": raw.get("faction"), "to": raw.get("to"), "stage": raw.get("stage"),
        "once": bool(raw.get("once", True)), "cooldown": int(raw.get("cooldown", 0) or 0), "behaviour": behaviour,
        "text": raw.get("text"), "side_quest": raw.get("side_quest"), "event": raw.get("event"),
        "move_to": raw.get("move_to"), "move_to_tags": list(raw.get("move_to_tags") or ()),
        "effects": EffectList.of(raw.get("effects")), "trust": float(raw.get("trust", 0) or 0),
        "hostility": float(raw.get("hostility", 0) or 0),
        "standing": {str(k): float(v) for k, v in (raw.get("standing") or {}).items()},
        "where": raw.get("where") or _default_where(behaviour), "expires": raw.get("expires"),
        "tags": [tags] if isinstance(tags, str) else list(tags or ()),
    }


def _reactions_of(owner):
    """The normalized reactions of an NPC (with the confrontation of its ``hostile_event``) or a faction spec."""
    if isinstance(owner, dict):
        raw = owner.get("reactions") or ()
        cache_key = ("faction", id(raw), len(raw))
        cached = owner.get("_rx")
        if cached is None or cached[0] != cache_key:
            owner["_rx"] = cached = (cache_key, [reaction_spec(r, i) for i, r in enumerate(raw)])
        return cached[1]
    raw = list(getattr(owner, "reactions", None) or ())
    hostile_event = getattr(owner, "hostile_event", None)
    cache_key = (tuple(id(r) for r in raw), hostile_event)
    cached = owner.__dict__.get("_rx")
    if cached is None or cached[0] != cache_key:
        found = [reaction_spec(r, i) for i, r in enumerate(raw)]
        if hostile_event:
            found.append(reaction_spec({"signal": "disposition", "to": "hostile", "behaviour": "confront",
                                        "event": hostile_event, "where": "seek"}, "hostile"))
        owner.__dict__["_rx"] = cached = (cache_key, found)
    return cached[1]


def _all_reactions(state):
    """[(owner kind, owner name, reaction)] of every NPC and faction, in the world's order."""
    found = []
    for char in state.world.characters:
        for rx in _reactions_of(char):
            found.append(("npc", char.name, rx))
    world_factions = getattr(state.world, "factions", None) or {}
    for name in world_factions:
        spec = world_factions[name]
        for rx in _reactions_of(spec):
            found.append(("faction", name, rx))
    return found


def _rid(kind, owner, rx):
    return f"{kind}:{owner}:{rx['index']}"


def _about(state, kind, owner, rx, npc, witnesses):
    """Whether a deed (or what befell ``npc``) concerns the reaction's owner."""
    about = rx["about"] or ("faction" if kind == "faction" else "me")
    if about == "anyone":
        return True
    if kind == "faction":
        return (npc and faction_of(state, npc) == owner) or any(faction_of(state, w) == owner for w in witnesses)
    if npc == owner or owner in witnesses:
        return True
    if about == "faction":
        mine = faction_of(state, owner)
        return bool(mine) and npc is not None and faction_of(state, npc) == mine
    if about == "related":
        char = character(state, owner)
        return npc in (getattr(char, "related", None) or {})
    return False


def _matches(state, kind, owner, rx, signal_kind, info):
    npc, witnesses = info.get("npc"), info.get("witnesses") or ()
    if signal_kind in QUEST_SIGNALS:
        if rx["quest"]:
            return info.get("quest") == rx["quest"]
        return _about(state, kind, owner, rx, npc, ())
    if signal_kind in DEEDS or signal_kind in ("goal_completed", "goal_opposed"):
        if rx["quest"] and info.get("quest") != rx["quest"]:
            return False
        return _about(state, kind, owner, rx, npc, witnesses)
    if signal_kind == "mission_stage":
        stage = rx["stage"]
        return stage is None or stage in (info.get("stage"), info.get("title"))
    if signal_kind == "mission_complete":
        return True
    if signal_kind == "variable":
        return not rx["variable"] or rx["variable"] == info.get("variable")
    if signal_kind == "disposition":
        if rx["to"] and rx["to"] != info.get("to"):
            return False
        return npc == owner if kind == "npc" else faction_of(state, npc) == owner
    if signal_kind == "faction":
        if rx["to"] and rx["to"] != info.get("to"):
            return False
        wanted = rx["faction"] or (owner if kind == "faction" else faction_of(state, owner))
        return info.get("faction") == wanted
    if signal_kind == "met":
        return npc == owner if kind == "npc" else faction_of(state, npc) == owner
    return False


def _fired(state):
    return state.__dict__.setdefault("npc_reactions_fired", {})


def _due(state):
    return state.__dict__.setdefault("npc_reactions_due", [])


def _may_queue(state, rid, rx):
    if any(item["id"] == rid for item in _due(state)):
        return False
    last = _fired(state).get(rid)
    if last is None:
        return True
    return not rx["once"] and state.actions_taken - int(last) >= max(1, rx["cooldown"])


def _queue(state, kind, owner, rx, info):
    rid = _rid(kind, owner, rx)
    if not _may_queue(state, rid, rx):
        return
    expires = rx["expires"] if rx["expires"] is not None else settings()["reaction_expiry"]
    _due(state).append({"id": rid, "owner": [kind, owner], "since": state.actions_taken,
                        "until": state.actions_taken + int(expires), "signal": {
                            key: value for key, value in info.items() if key != "witnesses"}, "blocked": None})


@with_state_context
def signal(state, kind, **info):
    """Something happened (SIGNALS): the reactions it calls for wait to be delivered (update)."""
    if not enabled():
        return
    for owner_kind, owner, rx in _all_reactions(state):
        if rx["on"] == kind and _matches(state, owner_kind, owner, rx, kind, info):
            _queue(state, owner_kind, owner, rx, {"kind": kind, **info})


def _lookup(state, item):
    kind, owner = item["owner"]
    for owner_kind, name, rx in _all_reactions(state):
        if owner_kind == kind and name == owner and _rid(kind, owner, rx) == item["id"]:
            return rx
    return None


def _actor(state, kind, owner, rx):
    """Who performs a reaction: the NPC, or for a faction a member who is here (else the first one
    who may act, for one that seeks the player out or happens anywhere)."""
    if kind == "npc":
        return owner
    names = members(state, owner)
    here = [name for name in names if name in state.visible_inhabitants]
    able = [name for name in here + names if permits(state, name, rx["behaviour"], rx["tags"]) is None]
    return able[0] if able else (here[0] if here else (names[0] if names else None))


def _deliver(state, item, rx, actor):
    kind, owner = item["owner"]
    called = state._called(actor) if actor else owner
    called = called[:1].upper() + called[1:]
    faction = owner if kind == "faction" else (faction_of(state, actor) or "")
    if rx["where"] == "seek" and actor and actor not in state.visible_inhabitants:
        _bring(state, actor)
    text = rx["text"]
    if text:
        state._say("npc_reaction", text=str(text).format_map(
            {"npc": called, "location": state.current.name, "faction": faction}), npc=called)
    elif rx["behaviour"] and rx["behaviour"] != "move":
        state._say(f"npc_{rx['behaviour']}", npc=called, location=state.current.name, faction=faction)
    if actor:
        change(state, actor, rx["trust"], rx["hostility"], cause="reaction")
    for name, delta in sorted(rx["standing"].items()):
        change_standing(state, name, delta, cause="reaction")
    if rx["effects"]:
        state._run_effects(rx["effects"], npc=actor)
    if rx["side_quest"]:
        from . import side_quests

        side_quests.hook(state, rx["side_quest"])
    if rx["event"] and state.active_event is None:
        state._enter_event(rx["event"], source_npc=actor, cause="npc_reaction")
    if rx["behaviour"] == "move" or rx["move_to"] or rx["move_to_tags"]:
        if actor:
            move_npc(state, actor, to=rx["move_to"], tags=rx["move_to_tags"])
    _fired(state)[item["id"]] = state.actions_taken
    _log(state, {"what": "reaction", "id": item["id"], "npc": actor, "behaviour": rx["behaviour"],
                 "signal": (item.get("signal") or {}).get("kind")})


@with_state_context
def update(state):
    """End of a turn (GameState._check_end_conditions): explicit goals completed by their condition,
    dispositions settled, reactions whose condition now holds queued, and those due delivered (at
    most max_reactions_per_turn; never inside an event)."""
    if not enabled() or state.current is None:
        return
    _check_goal_conditions(state)
    for npc in sorted(state.__dict__.get("npc_relations") or ()):
        settle(state, npc)
    if state.ended:
        return
    for owner_kind, owner, rx in _all_reactions(state):
        if rx["on"] is None and rx["when"] is not None and _may_queue(state, _rid(owner_kind, owner, rx), rx):
            if rx["when"].holds(state.facts):
                _queue(state, owner_kind, owner, rx, {"kind": None})
    deliver(state)


@with_state_context
def deliver(state):
    """Deliver the reactions that are due and may be delivered now (their condition, the guard, the NPC
    where it must be), at most max_reactions_per_turn a turn: at the end of a turn (update), and once
    the next scene is set (GameState._step), so someone met there reacts at once."""
    due = _due(state) if enabled() else None
    if not due or state.ended or state.current is None:
        return
    limit = max(1, int(settings()["max_reactions_per_turn"]))
    counted = state.__dict__.get("_reactions_delivered") or [None, 0]
    delivered = counted[1] if counted[0] == state.actions_taken else 0
    for item in list(due):
        if state.actions_taken > item["until"]:
            due.remove(item)
            _log(state, {"what": "reaction_expired", "id": item["id"], "blocked": item.get("blocked")})
            continue
        if state.active_event is not None or delivered >= limit:
            item["blocked"] = "busy"
            continue
        rx = _lookup(state, item)
        if rx is None:
            due.remove(item)
            continue
        if rx["when"] is not None and not rx["when"].holds(state.facts):
            item["blocked"] = "condition"
            continue
        kind, owner = item["owner"]
        actor = _actor(state, kind, owner, rx)
        reason = permits(state, actor, rx["behaviour"], rx["tags"]) if actor else ("unknown_npc" if kind == "npc" else None)
        if reason is not None and (actor or kind == "npc"):
            item["blocked"] = reason
            continue
        if rx["where"] == "scene" and (actor is None or actor not in state.visible_inhabitants):
            item["blocked"] = "absent"
            continue
        due.remove(item)
        _deliver(state, item, rx, actor)
        delivered += 1
    state.__dict__["_reactions_delivered"] = [state.actions_taken, delivered]


# ---- what the player and the author see ---------------------------------------------------------------

@with_state_context
def player_view(state, npc, place=None):
    """What the player may know of someone (the copy at ``place`` for a generic NPC; default: where the
    player is): {disposition, faction (when it is public)}."""
    if not enabled():
        return {"disposition": None, "faction": None}
    faction = faction_of(state, npc)
    spec = factions(state).get(faction) if faction else None
    return {"disposition": disposition(state, key_of(state, npc, place)),
            "faction": faction if spec is None or spec["public"] else None}


def _goal_rows(state, char):
    progress = state.__dict__.get("npc_goal_progress") or {}
    needed = int((config.get("npc_goals") or {}).get("progress_needed", 2))
    done = goals_done(state, char.name)
    rows = []
    for index, goal in enumerate(state._goal_specs(char)):
        how = "conditions" if goal.get("complete_if") is not None or goal.get("progress_when") is not None \
            else "tags" if goal.get("supports") else "keywords"
        rows.append({"id": goal.get("id"), "text": goal.get("text"), "how": how,
                     "progress": int((progress.get(char.name) or {}).get(index, 0)),
                     "needed": int(goal.get("needed") or needed), "done": index in done
                     or (how == "keywords" and char.name in (state.__dict__.get("allies") or ())),
                     "supports": list(goal.get("supports") or ()), "opposes": list(goal.get("opposes") or ())})
    return rows


@with_state_context
def explain(state):
    """Everything about people and factions, for authors (the decision trace's ``relationships``):
    {enabled, npcs [...], factions [...], due, log, deeds, codes}. A generic NPC has a row for each copy
    the story touched (``place``), else one for the copy here. Reads only."""
    if not enabled():
        return {"enabled": False, "npcs": [], "factions": [], "due": [], "log": [], "deeds": [], "codes": CODES}
    npcs = []
    records = state.__dict__.get("npc_relations") or {}
    for char in state.world.characters:
        keys = sorted(key for key in records if split(key)[0] == char.name and split(key)[1]) or [
            key_of(state, char.name)]
        goals = _goal_rows(state, char)
        for key in keys:
            rec = record(state, key)
            place = split(key)[1]
            npcs.append({
                "npc": char.name, "place": place, "faction": faction_of(state, char.name),
                "trust": trust(state, key), "hostility": hostility(state, key), "attitude": attitude(state, key),
                "disposition": disposition(state, key), "goals": goals, "memories": list(rec.get("memories") or ()),
                "related": dict(getattr(char, "related", None) or {}), "never": sorted(getattr(char, "never", None) or ()),
                "home": state.npc_homes.get(char.name), "seeking": seeking(state, char),
                "kin": None if char.unique else _kin(char),
                "haunts": None if char.unique or presence(state, char, state.current) is None else {
                    loc.name: presence(state, char, loc) for loc in state.world.locations.values()
                    if presence(state, char, loc)},
                "persist": bool((getattr(char, "relationship", None) or {}).get("persist", False)),
            })
    rows = []
    for name, spec in factions(state).items():
        rows.append({"faction": name, "standing": _num(standing(state, name)), "status": faction_status(state, name),
                     "base": spec["standing"], "shift": (state.__dict__.get("faction_shift") or {}).get(name, 0),
                     "hostile_below": spec["hostile_below"], "friendly_at": spec["friendly_at"],
                     "relations": dict(spec["relations"]), "members": members(state, name),
                     "reacts_to": dict(spec["reacts_to"]), "world": spec["world"]})
    return {"enabled": True, "npcs": npcs, "factions": rows, "due": [dict(item) for item in _due(state)],
            "fired": dict(_fired(state)), "log": list(state.__dict__.get("relationship_log") or ())[-20:],
            "deeds": list(state.__dict__.get("npc_deeds") or ())[-20:], "codes": CODES}


# ---- save games and the player's progression -------------------------------------------------------------

@with_state_context
def parameters(state):
    """The relationships as the session's parameters (ane.session_state/1 ``relationships``): {npcs
    {npc or npc@place: {trust, hostility, disposition, memories}}, kin {npc: {trust, hostility}}, goals
    {npc: {progress, done}}, moved, haunts, fired, due}."""
    npcs = {key: {"trust": rec["trust"], "hostility": rec["hostility"], "disposition": rec.get("disposition"),
                  "memories": [dict(m) for m in rec.get("memories") or ()]}
            for key, rec in sorted((state.__dict__.get("npc_relations") or {}).items())}
    progress = state.__dict__.get("npc_goal_progress") or {}
    goals = {name: {"progress": {str(index): count for index, count in (progress.get(name) or {}).items()},
                    "done": sorted(goals_done(state, name))}
             for name in sorted(set(progress) | set(state.__dict__.get("npc_goals_done") or {}))}
    moved = {m["npc"]: m["to"] for m in state.__dict__.get("npc_moves") or ()
             if getattr(character(state, m["npc"]), "unique", False)}  # generic NPCs' moves: haunts
    return {"npcs": npcs, "kin": {k: dict(v) for k, v in (state.__dict__.get("npc_kin") or {}).items()},
            "goals": goals, "moved": moved,
            "haunts": {k: list(v) for k, v in (state.__dict__.get("npc_haunts") or {}).items()},
            "fired": dict(_fired(state)), "due": [dict(item) for item in _due(state)]}


def _numbers(entry, fields=("trust", "hostility")):
    return {field: _num(entry[field]) for field in fields
            if isinstance(entry.get(field), (int, float)) and not isinstance(entry.get(field), bool)}


def restore(state, saved):
    """Set the relationships from ``saved`` (parameters()), quietly: nothing is told again."""
    if not isinstance(saved, dict):
        return
    known = {c.name for c in state.world.characters}
    records = state.__dict__.setdefault("npc_relations", {})
    for key, entry in (saved.get("npcs") or {}).items():
        if split(key)[0] not in known or not isinstance(entry, dict):
            continue
        rec = {**record(state, key), **_numbers(entry), "memories": [
            dict(m) for m in entry.get("memories") or () if isinstance(m, dict)]}
        rec["disposition"] = entry.get("disposition") if entry.get("disposition") in DISPOSITIONS else None
        records[key] = rec
    state.__dict__["npc_kin"] = {name: _numbers(entry) for name, entry in (saved.get("kin") or {}).items()
                                 if name in known and isinstance(entry, dict)}
    for name, entry in (saved.get("goals") or {}).items():
        if name not in known or not isinstance(entry, dict):
            continue
        if entry.get("progress"):
            state.__dict__.setdefault("npc_goal_progress", {})[name] = {
                int(index): int(count) for index, count in entry["progress"].items()}
        if entry.get("done"):
            state.__dict__.setdefault("npc_goals_done", {})[name] = [int(i) for i in entry["done"]]
    for name, place in (saved.get("moved") or {}).items():
        if name in known and place in state.world.locations:
            state.npc_homes[name] = place
    state.__dict__["npc_haunts"] = {name: [p for p in places if p in state.world.locations]
                                    for name, places in (saved.get("haunts") or {}).items() if name in known}
    state.__dict__["npc_reactions_fired"] = {str(k): int(v) for k, v in (saved.get("fired") or {}).items()}
    state.__dict__["npc_reactions_due"] = [dict(item) for item in saved.get("due") or () if isinstance(item, dict)
                                           and "id" in item and "owner" in item]
    _bump(state)


def _persisted(state, cfg):
    """The NPCs whose relationships the profile keeps (relationships.persist)."""
    mode = cfg["persist"] if cfg["persist"] in PERSIST else "configured"
    if mode == "none":
        return set()
    return {c.name for c in state.world.characters
            if mode == "all" or (getattr(c, "relationship", None) or {}).get("persist")}


@with_state_context
def start(state):
    """At the start of a story: what the profile's earlier stories left (relationships.persist; real
    play, or simulations that read the stored profile)."""
    if not enabled():
        return
    store = state._store
    if store is None and config.get("persistence.simulate_with", "defaults") == "stored":
        from .variables import VariableStore

        store = VariableStore()
    if store is None or not hasattr(store, "npc_relations"):
        return
    cfg = settings()
    keep = _persisted(state, cfg)
    saved = store.npc_relations(state.profile) or {}
    records = state.__dict__.setdefault("npc_relations", {})
    for key, entry in (saved.get("npcs") or {}).items():
        if split(key)[0] not in keep or character(state, key) is None or not isinstance(entry, dict):
            continue
        rec = {**record(state, key), **{field: _clamp(field, float(value), cfg)
                                         for field, value in _numbers(entry).items()}}
        rec["memories"] = [{**m, "turn": -1} for m in entry.get("memories") or () if isinstance(m, dict)]
        records[key] = rec
    for name, entry in (saved.get("kin") or {}).items():
        if name in keep and isinstance(entry, dict):
            state.__dict__.setdefault("npc_kin", {})[name] = _numbers(entry)
    for name, goals in (saved.get("goals_done") or {}).items():
        if name not in keep:
            continue
        done = [goal_index(state, name, goal) for goal in goals or ()]
        done = sorted({index for index in done if index is not None})
        if done:
            state.__dict__.setdefault("npc_goals_done", {})[name] = done
    for key in list(records):
        if split(key)[0] in keep:
            records[key]["disposition"] = disposition(state, key)
    _bump(state)


@with_state_context
def remember_story(state):
    """At the end of a story (real play): keep the relationships configured to persist in the profile:
    {npcs {npc or npc@place: {trust, hostility, memories}}, kin, goals_done {npc: [goal ids]}}."""
    store = state._store
    if not enabled() or store is None or not hasattr(store, "record_npc_relations"):
        return
    cfg = settings()
    keep = _persisted(state, cfg)
    npcs = {}
    for key, rec in sorted((state.__dict__.get("npc_relations") or {}).items()):
        if split(key)[0] in keep:
            npcs[key] = {"trust": rec["trust"], "hostility": rec["hostility"],
                         "memories": [{field: m.get(field) for field in ("kind", "how", "about", "location")}
                                      for m in (rec.get("memories") or ())[-int(cfg["max_memories"]):]]}
    kin = {name: dict(values) for name, values in (state.__dict__.get("npc_kin") or {}).items() if name in keep}
    goals = {}
    for name in sorted(keep):
        char = character(state, name)
        specs = state._goal_specs(char) if char is not None else []
        done = [specs[i].get("id") for i in sorted(goals_done(state, name)) if i < len(specs)]
        if done:
            goals[name] = done
    if npcs or kin or goals:
        store.record_npc_relations(state.profile, {"npcs": npcs, "kin": kin, "goals_done": goals})


# ---- the world as written ------------------------------------------------------------------------------

def world_factions(config_wd):
    """The world's ``factions`` as stored on the WorldGraph (raw specs: read through faction_spec)."""
    return {str(name): dict(spec or {}) for name, spec in ((config_wd or {}).get("factions") or {}).items()}


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _reaction_errors(raw, where, world, condition, effect_list, known_factions):
    errors = []
    if not isinstance(raw, list):
        return [f"{where}: `reactions` must be a list."]
    events = set(world.get("events") or {})
    quests = set(world.get("side_quests") or {})
    locations = set(world.get("locations") or {})
    for number, entry in enumerate(raw, start=1):
        label = f"{where}.reactions[{number}]"
        if not isinstance(entry, dict):
            errors.append(f"{label} must be a mapping.")
            continue
        unknown = sorted(str(key) for key in set(entry) - set(REACTION_FIELDS) - {"on", True})
        if unknown:
            errors.append(f"{label}: unknown field(s) {', '.join(unknown)}; use {', '.join(REACTION_FIELDS)}.")
        wanted = _signal_of(entry)
        if wanted is not None and wanted not in SIGNALS:
            errors.append(f"{label}.signal must be one of {', '.join(SIGNALS)}.")
        if wanted is None and not entry.get("when"):
            errors.append(f"{label} needs `signal` (what it answers) or `when` (a condition).")
        if entry.get("behaviour") is not None and entry["behaviour"] not in BEHAVIOURS:
            errors.append(f"{label}.behaviour must be one of {', '.join(BEHAVIOURS)}.")
        if entry.get("about") is not None and entry["about"] not in ABOUT:
            errors.append(f"{label}.about must be one of {', '.join(ABOUT)}.")
        if entry.get("where") is not None and entry["where"] not in WHERE:
            errors.append(f"{label}.where must be one of {', '.join(WHERE)}.")
        if entry.get("to") is not None and entry["to"] not in DISPOSITIONS + ("neutral",):
            errors.append(f"{label}.to must be one of {', '.join(DISPOSITIONS)}.")
        if entry.get("side_quest") and entry["side_quest"] not in quests:
            errors.append(f"{label}.side_quest `{entry['side_quest']}` is not a side quest.")
        if entry.get("quest") and entry["quest"] not in quests:
            errors.append(f"{label}.quest `{entry['quest']}` is not a side quest.")
        if entry.get("event") and entry["event"] not in events:
            errors.append(f"{label}.event `{entry['event']}` is not an event.")
        if entry.get("move_to") and entry["move_to"] not in locations:
            errors.append(f"{label}.move_to `{entry['move_to']}` is not a location.")
        if entry.get("faction") and known_factions and entry["faction"] not in known_factions:
            errors.append(f"{label}.faction `{entry['faction']}` is not a faction.")
        for field in ("trust", "hostility"):
            if entry.get(field) is not None and not _is_number(entry[field]):
                errors.append(f"{label}.{field} must be a number.")
        for field in ("cooldown", "expires"):
            if entry.get(field) is not None and (not isinstance(entry[field], int) or isinstance(entry[field], bool)
                                                 or entry[field] < 0):
                errors.append(f"{label}.{field} must be a whole number of 0 or more.")
        standing_change = entry.get("standing")
        if standing_change is not None and (not isinstance(standing_change, dict)
                                            or not all(_is_number(v) for v in standing_change.values())):
            errors.append(f"{label}.standing must map factions to numbers.")
        if entry.get("when"):
            condition(entry["when"], f"{label}.when")
        effect_list(entry.get("effects"), f"{label}.effects")
    return errors


def _haunt_errors(entry, where, world):
    raw = entry.get("haunts")
    if raw is None:
        return []
    if entry.get("unique"):
        return [f"{where}.haunts is for generic NPCs (a unique NPC has one home: move them with a `move` reaction)."]
    if not isinstance(raw, list):
        return [f"{where}.haunts must be a list of {{places, tags_any, presence}}."]
    errors = []
    locations = set(world.get("locations") or {})
    for number, item in enumerate(raw, start=1):
        label = f"{where}.haunts[{number}]"
        if not isinstance(item, dict):
            errors.append(f"{label} must be a mapping of places, tags_any and presence.")
            continue
        unknown = sorted(set(item) - {"places", "tags_any", "presence"})
        if unknown:
            errors.append(f"{label}: unknown field(s) {', '.join(unknown)}; use places, tags_any, presence.")
        for field in ("places", "tags_any"):
            value = item.get(field)
            if value is not None and (not isinstance(value, list) or not all(isinstance(v, str) for v in value)):
                errors.append(f"{label}.{field} must be a list of names.")
        errors.extend(f"{label}.places: `{place}` is not a location." for place in item.get("places") or ()
                      if isinstance(item.get("places"), list) and locations and place not in locations)
        value = item.get("presence", 1.0)
        if not _is_number(value) or not 0 <= value <= 1:
            errors.append(f"{label}.presence must be a number from 0 (never) to 1 (every visit).")
    return errors


def world_errors(world, condition, effect_list, tuning_factions=None):
    """Problems with factions and the relationship fields of NPCs (builder.logic_errors).
    ``tuning_factions``: the faction names of the world's own tuning (default: the configuration in use)."""
    world = world or {}
    errors = []
    npcs = {entry.get("name") for entry in world.get("characters") or [] if isinstance(entry, dict)}
    if tuning_factions is None:
        tuning_factions = (config.get("encounters") or {}).get("factions") or {}
    tuning_factions = set(tuning_factions)
    raw_factions = world.get("factions")
    if raw_factions is not None and not isinstance(raw_factions, dict):
        return ["`factions` must be a mapping of faction names to their settings."]
    known_factions = set(raw_factions or {}) | tuning_factions
    for name, spec in (raw_factions or {}).items():
        where = f"Faction '{name}'"
        if not isinstance(spec, dict):
            errors.append(f"{where} must be a mapping.")
            continue
        unknown = sorted(set(spec) - set(FACTION_FIELDS))
        if unknown:
            errors.append(f"{where}: unknown field(s) {', '.join(unknown)}; use {', '.join(FACTION_FIELDS)}.")
        for field in ("standing", "hostile_below", "friendly_at"):
            if spec.get(field) is not None and not _is_number(spec[field]):
                errors.append(f"{where}.{field} must be a number.")
        for field in ("relations", "reacts_to"):
            value = spec.get(field)
            if value is not None and (not isinstance(value, dict) or not all(_is_number(v) for v in value.values())):
                errors.append(f"{where}.{field} must map names to numbers.")
        for other in (spec.get("relations") or {}) if isinstance(spec.get("relations"), dict) else ():
            if other not in known_factions:
                errors.append(f"{where}.relations: `{other}` is not a faction.")
        for kind in (spec.get("reacts_to") or {}) if isinstance(spec.get("reacts_to"), dict) else ():
            if kind not in DEEDS + QUEST_SIGNALS:
                errors.append(f"{where}.reacts_to: `{kind}` is not a deed ({', '.join(DEEDS + QUEST_SIGNALS)}).")
        for member in spec.get("members") or ():
            if member not in npcs:
                errors.append(f"{where}.members: `{member}` is not an NPC.")
        variable = spec.get("variable")
        if variable and variable not in (world.get("variables") or {}):
            errors.append(f"{where}.variable `{variable}` is not a persistent variable.")
        for field in ("everywhere", "persist", "public"):
            if field in spec and not isinstance(spec[field], bool):
                errors.append(f"{where}.{field} must be true or false.")
        if spec.get("reactions") is not None:
            errors.extend(_reaction_errors(spec["reactions"], where, world, condition, effect_list, known_factions))
    for entry in world.get("characters") or []:
        if not isinstance(entry, dict):
            continue
        where = f"NPC '{entry.get('name')}'"
        faction = entry.get("faction")
        if faction is not None and (not isinstance(faction, str) or faction not in known_factions):
            errors.append(f"{where}.faction `{faction}` is not a faction (world `factions` or encounters.factions).")
        relationship = entry.get("relationship")
        if relationship is not None:
            if not isinstance(relationship, dict):
                errors.append(f"{where}.relationship must be a mapping of trust, hostility and persist.")
            else:
                unknown = sorted(set(relationship) - {"trust", "hostility", "persist", "kin"})
                if unknown:
                    errors.append(f"{where}.relationship: unknown field(s) {', '.join(unknown)}.")
                kin = relationship.get("kin")
                if kin is not None and (not _is_number(kin) or not 0 <= kin <= 1):
                    errors.append(f"{where}.relationship.kin must be a number from 0 to 1.")
                for field in ("trust", "hostility"):
                    if relationship.get(field) is not None and not _is_number(relationship[field]):
                        errors.append(f"{where}.relationship.{field} must be a number.")
                if "persist" in relationship and not isinstance(relationship["persist"], bool):
                    errors.append(f"{where}.relationship.persist must be true or false.")
        related = entry.get("related")
        if related is not None:
            if not isinstance(related, dict) or not all(_is_number(v) for v in related.values()):
                errors.append(f"{where}.related must map NPCs to numbers.")
            else:
                errors.extend(f"{where}.related: `{other}` is not an NPC." for other in related if other not in npcs)
        errors.extend(_haunt_errors(entry, where, world))
        never = entry.get("never")
        if never is not None and (not isinstance(never, list) or any(b not in BEHAVIOURS for b in never)):
            errors.append(f"{where}.never must list behaviours ({', '.join(BEHAVIOURS)}).")
        hostile_event = entry.get("hostile_event")
        if hostile_event and hostile_event not in (world.get("events") or {}):
            errors.append(f"{where}.hostile_event: no event '{hostile_event}'.")
        activities = entry.get("activities")
        if activities is not None:
            if not isinstance(activities, list):
                errors.append(f"{where}.activities must be a list.")
            else:
                for number, item in enumerate(activities, start=1):
                    if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                        errors.append(f"{where}.activities[{number}] needs a `text`.")
                        continue
                    if item.get("when"):
                        condition(item["when"], f"{where}.activities[{number}].when")
                    wanted = item.get("disposition")
                    if wanted is not None and any(d not in DISPOSITIONS for d in ([wanted] if isinstance(wanted, str)
                                                                                    else wanted)):
                        errors.append(f"{where}.activities[{number}].disposition takes {', '.join(DISPOSITIONS)}.")
        if entry.get("reactions") is not None:
            errors.extend(_reaction_errors(entry["reactions"], where, world, condition, effect_list, known_factions))
        for number, goal in enumerate(entry.get("goals") or [], start=1) if isinstance(entry.get("goals"), list) else ():
            if not isinstance(goal, dict):
                continue
            label = f"{where}.goals[{number}]"
            unknown = sorted(set(goal) - set(GOAL_FIELDS))
            if unknown:
                errors.append(f"{label}: unknown field(s) {', '.join(unknown)}; use {', '.join(GOAL_FIELDS)}.")
            for field in ("progress_when", "complete_if"):
                if goal.get(field):
                    condition(goal[field], f"{label}.{field}")
            for field in ("supports", "opposes"):
                if goal.get(field) is not None and (not isinstance(goal[field], list)
                                                    or not all(isinstance(t, str) for t in goal[field])):
                    errors.append(f"{label}.{field} must be a list of tags.")
            if goal.get("trust") is not None and not _is_number(goal["trust"]):
                errors.append(f"{label}.trust must be a number.")
            effect_list(goal.get("on_complete"), f"{label}.on_complete")
    return errors
