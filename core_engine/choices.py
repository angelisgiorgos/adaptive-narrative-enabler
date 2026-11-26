"""The player's choices as structured objects: a stable identity, where each comes from, and what it
involves (requirements, the skill check, what failing costs).

**Identity.** A choice's ``id`` does not depend on the text it is shown with (an owner's prefix, an
item named in place of ``{...}``, a message the author rewrote) nor on its position among the
choices. It is made of what offers it:

==============================  =============================================================
``location:<place>:<key>``      a place's own action (``key``: its authored ``id``, else its
``npc:<person>:<key>``          authored name, as a slug); a person's; an item's; an event's
``item:<item>:<key>``
``event:<event>:<key>``
``talk:<person>``               talking with someone present (conversation)
``approach:<person>``           approaching someone (conversations.enabled): their choices
``leave:<person>``              leaving a conversation
``look:<place>``                looking around again after a place drew the player in
``go:<place>``                  travelling to a neighbouring place
``travel:<place>``              fast travel along a known way
``fallback:<event>``            stepping away from an event with nothing else to do
``side-quest:<quest>:accept``   taking an offered side quest on (``:decline``: turning it down)
==============================  =============================================================

Two characters (or places, items) with identically named actions have different ids (the owner
is part of it). Two offered choices that would still share an id (an entry with two actions of the
same name and no ``id``) are told apart by a short digest of where each is written
(``content_ref``), never by their order.

**Provenance.** ``provenance(action, state)``: its owner, the entity offering it ({kind, name}),
the place where it is offered, its category, its authored key and its content reference (where it
is written in the configuration, e.g. ``world_definition.characters[Guard].actions[2]``).

**Choosing by id.** ``select(state, offered, action_id, state_version)``: the offered action with
that id. With ``state_version`` (``GameState.state_version`` when the choices were shown), a choice
made from options the game has moved past is refused (``StaleChoice``); an id not offered now is
refused (``UnavailableChoice``). Both carry the current version and the ids offered now; nothing is
done. The studio API (backend/sessions.py) and the exported ANE AI (narrative_runtime/) both
choose this way.

This module only reads a story; it never changes one.
"""
from __future__ import annotations

import hashlib
import re

# What offers an action (Action.source) -> the entity kind in its id and provenance.
AUTHORED = {"location": "location", "npc": "npc", "item": "item", "event": "event"}
CATEGORIES = {
    "location": "place", "npc": "person", "item": "item", "event": "event", "conversation": "talk",
    "approach": "approach", "leave": "leave", "event_fallback": "event", "side_quest": "side_quest",
}
_PREFIX = re.compile(r"^\s*(\([^)]*\)|\[[^\]]*\])\s*")


def slug(text):
    """A readable, URL-safe form of a name: "Gate Guard" -> "gate-guard"."""
    value = re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower()).strip("-")
    return value or "x"


def _key(action):
    """The authored key of an action (its id, else its authored name); for actions made before keys
    existed (older save games): the displayed name without its owner's prefix."""
    key = getattr(action, "key", None)
    return key if key else _PREFIX.sub("", str(action.name or ""))


def category(action):
    """What kind of choice it is: place, person, item, event, talk, approach, leave, look, travel,
    fast_travel."""
    if action.source == "navigation":
        return "fast_travel" if getattr(action, "fast_travel", None) else "travel"
    if action.source == "leave" and action.character_name is None:
        return "look"
    return CATEGORIES.get(action.source, action.source or "other")


def base_id(action, state=None):
    """The identity of an action, from what offers it (see the module docstring)."""
    kind = category(action)
    if action.source in AUTHORED:
        return f"{AUTHORED[action.source]}:{slug(action.owner)}:{slug(_key(action))}"
    if kind in ("talk", "approach", "leave"):
        return f"{kind}:{slug(action.character_name or action.owner)}"
    if kind == "look":
        return f"look:{slug(action.owner)}"
    if kind == "travel":
        return f"go:{slug(action.owner)}"
    if kind == "fast_travel":
        return f"travel:{slug(action.owner)}"
    if action.source == "side_quest":
        return f"side-quest:{slug(action.owner)}:{slug(_key(action))}"
    if action.source == "event_fallback":
        event = getattr(getattr(state, "active_event", None), "name", None) if state is not None else None
        return f"fallback:{slug(event or 'event')}"
    return f"{slug(action.source or 'action')}:{slug(action.owner)}:{slug(_key(action))}"


def same_choice(a, b):
    """Whether two action objects are the same choice (an option and its copy in a new ranking): the
    same identity, text and content reference."""
    return (base_id(a) == base_id(b) and a.name == b.name
            and getattr(a, "content_ref", None) == getattr(b, "content_ref", None))


def assign_ids(actions, state=None):
    """Stable, unique ids for ``actions`` (in their order). A shared id is told apart by a digest of
    each one's content reference (where it is written) and displayed name, not by position; only
    actions identical in all of that are numbered."""
    bases = [base_id(action, state) for action in actions]
    counts = {}
    for base in bases:
        counts[base] = counts.get(base, 0) + 1
    ids, taken = [], set()
    for action, base in zip(actions, bases):
        value = base
        if counts[base] > 1:
            where = f"{getattr(action, 'content_ref', None) or ''}|{action.name}"
            value = f"{base}~{hashlib.sha256(where.encode('utf-8')).hexdigest()[:6]}"
        number, unique = 2, value
        while unique in taken:
            unique, number = f"{value}~{number}", number + 1
        taken.add(unique)
        ids.append(unique)
    return ids


def provenance(action, state=None):
    """Where a choice comes from: {owner, entity: {kind, name}, location, category, key, content_ref}."""
    kind = category(action)
    if action.source in AUTHORED:
        entity = {"kind": AUTHORED[action.source], "name": action.owner}
    elif kind in ("talk", "approach", "leave"):
        entity = {"kind": "npc", "name": action.character_name or action.owner}
    elif kind in ("look", "travel", "fast_travel"):
        entity = {"kind": "location", "name": action.owner}
    elif action.source == "event_fallback" and state is not None and getattr(state, "active_event", None):
        entity = {"kind": "event", "name": state.active_event.name}
    else:
        entity = {"kind": action.source, "name": action.owner}
    return {
        "owner": action.owner,
        "entity": entity,
        "location": state.current.name if state is not None and getattr(state, "current", None) else None,
        "category": kind,
        "key": getattr(action, "key", None),
        "content_ref": getattr(action, "content_ref", None),
        "via": list(getattr(action, "fast_travel", None) or []) or None,
    }


def requirements(action):
    """What taking it needs or uses: {item, item_tag, item_used, coins, consumes_item, collects_item,
    triggers_event}; only what applies."""
    found = {
        "item": action.required_object,
        "item_tag": action.required_tag,
        "item_used": getattr(action, "active_item", None),
        "coins": action.required_coins or None,
        "consumes_item": bool(action.consumes_object) or None,
        "collects_item": action.collects_object,
        "triggers_event": action.triggers_event,
    }
    return {key: value for key, value in found.items() if value not in (None, False)}


class ChoiceRefused(Exception):
    """A choice that was not taken (nothing was done): ``code``, the game's ``state_version`` now and
    the ids ``offered`` now."""

    code = "choice_refused"

    def __init__(self, message, state_version, offered=None):
        super().__init__(message)
        self.state_version = state_version
        self.offered = list(offered or [])

    def details(self):
        """The refusal as JSON: {error, code, state_version, offered}."""
        return {"error": str(self), "code": self.code, "state_version": self.state_version, "offered": self.offered}


class StaleChoice(ChoiceRefused):
    """Made from the options of an older state of the game (another window, an update from the UNP)."""

    code = "stale_state"


class UnavailableChoice(ChoiceRefused):
    """An ``action_id`` that is not among the options offered now."""

    code = "action_unavailable"


def state_version_of(value):
    """A client's ``state_version`` as an int (None: not sent); anything else is a ValueError."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"`state_version` must be a whole number >= 0 (it is {value!r}).")
    return value


def select(state, offered, action_id=None, state_version=None):
    """The offered action ``action_id`` names (None when no ``action_id`` is given: the caller chooses
    by name). ``offered``: a function giving (the options offered now, their ids: ``assign_ids``),
    called only when needed. A ``state_version`` other than the game's raises ``StaleChoice`` (also
    when choosing by name); an id not offered raises ``UnavailableChoice``. Changes nothing."""
    state_version = state_version_of(state_version)
    current = state.state_version
    if state_version is not None and state_version != current:
        raise StaleChoice(
            f"The game has moved on since that choice was offered (state {state_version}; it is at {current} "
            "now). Nothing was done: choose from the current options.", current, offered()[1])
    if action_id is None:
        return None
    options, ids = offered()
    chosen = next((action for action, offered_id in zip(options, ids) if offered_id == action_id), None)
    if chosen is None:
        raise UnavailableChoice(f"`{action_id}` is not among the choices offered now. Nothing was done: choose "
                                "from the current options.", current, ids)
    return chosen


def describe(action, state, action_id):
    """A choice as the player sees it (only what is offered: never why anything else is not):
    {id, name (the label, as before), label, category, owner, source: {kind, name}, location,
    requirements, check (as before), skill_check: {stat, chance (0-100), bonus} or None,
    failure_risk: {health, coins, sent_to} or None, available, availability, repeat, one_time,
    members (approaching someone: the ids of their choices)}."""
    hint = state.check_hint(action)  # as choice_details always gave it
    origin = provenance(action, state)
    risk = hint.get("risk") if hint else (state.failure_cost(action) if action.outcomes else None)
    free = action.source in ("approach", "leave", "side_quest")  # choices that take no turn
    repeat = state._repeat_rule(action) if not free else "always"
    members = getattr(action, "members", None)
    return {
        "id": action_id,
        "name": action.name,
        "label": action.name,
        "category": origin["category"],
        "owner": action.owner,
        "source": origin["entity"],
        "location": origin["location"],
        "requirements": requirements(action),
        "check": hint,
        "skill_check": {key: hint[key] for key in ("stat", "chance", "bonus")} if hint else None,
        "failure_risk": risk,
        "available": True,
        "availability": "offered",
        "repeat": repeat,
        "one_time": repeat == "once",
        "takes_turn": not free,
        "members": assign_ids(members, state) if members else None,
    }
