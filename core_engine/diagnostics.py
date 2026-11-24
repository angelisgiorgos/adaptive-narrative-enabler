"""Author-only decision diagnostics: why each action is shown to the player, hidden or blocked now.

    trace = decision_trace(state)        # a JSON-ready dict; ``state`` is not changed

The trace is made on a **copy** of the story (its dice, its scene, its conversation and its save
store left out), so asking never changes the game: no turn passes, no random number is drawn from
the story's dice, no encounter or event starts, nothing is saved. On the copy, the engine builds
the player's options exactly as for the player (GameState.player_options); at each of its own
decision points it tells the trace what it decided (``_decision_trace`` hooks), and the trace
explains it with the same predicates the engine used. The engine's decisions are never taken here.

Every action gets a ``status``:

- ``shown``: offered to the player now (with why: the quest's next step, a way on, by rank, ...);
- ``hidden``: available, but not among the choices shown now (the relevance filter, a choice-count
  limit, the director's loop filter, behind "Approach <someone>", outside the conversation);
- ``blocked``: not available now (a condition, a missing item or coins, used up, an event in
  progress, a locked exit, its owner not here or not visible).

and ``reasons``: [{code, message, detail}] (``REASONS`` lists every code). The player never sees
any of this: PlayState carries only the choices offered (core_engine/choices.py).
"""
from __future__ import annotations

import copy
import math

from . import choices as choice_ids
from . import conditions

# code -> (status, what it means). The message of a reason says it for the case at hand.
REASONS = {
    # shown
    "priority_escape": ("shown", "A way out of the place that holds the player comes first."),
    "priority_quest": ("shown", "The step that best advances the quest comes first."),
    "priority_side_quest": ("shown", "It works toward a side quest under way, or leads to someone who offers one."),
    "side_quest_offer": ("shown", "It answers a side quest on offer (take it on or turn it down); no turn passes."),
    "priority_payoff": ("shown", "The payoff of an open clue comes first."),
    "priority_way_on": ("shown", "A way on (travel) is always among the choices."),
    "priority_way_back": ("shown", "The way back where the player came from stays a choice."),
    "rank": ("shown", "Shown by the storyteller's ranking, within the limits."),
    "conversation_member": ("shown", "A choice of the person the player is talking with."),
    "event_choice": ("shown", "A choice of the event in progress."),
    "focus_member": ("shown", "An activity of the place that drew the player in."),
    "unfiltered": ("shown", "Shown by rank (player_options.relevance_filter is off)."),
    # hidden (available, not shown now)
    "below_relevance": ("hidden", "Ranked among the least relevant options now (player_options.relevance)."),
    "kind_limit": ("hidden", "Enough choices of its kind are shown already (travel_limit, talk_limit, approach_limit)."),
    "choice_limit": ("hidden", "The number of choices shown now is reached (player_options.limit, focus_limit)."),
    "would_loop": ("hidden", "It could only repeat what was just told (director.loop_window)."),
    "behind_approach": ("hidden", "Offered through \"Approach <person>\" (conversations): it appears once the "
                                  "player approaches them."),
    "outside_conversation": ("hidden", "The player is talking with someone: only their choices and leaving are "
                                       "offered."),
    "outside_focus_place": ("hidden", "A place drew the player in: its own activities and \"Look around\" are "
                                      "offered first."),
    "just_left_conversation": ("hidden", "The player just left this person: they are not offered again before "
                                         "something happens."),
    # blocked: the action itself
    "condition_false": ("blocked", "Its enabled_if does not hold."),
    "until_reached": ("blocked", "Its until condition holds: its time is over."),
    "outcomes_stale": ("blocked", "Each of its outcomes tells what the story has moved past."),
    "event_unavailable": ("blocked", "The event it starts cannot start now."),
    "used_once": ("blocked", "A one-time action (repeat: once) already taken."),
    "exclusive_choice_made": ("blocked", "Another choice of its exclusive group was already made."),
    "done_this_visit": ("blocked", "Already taken in this visit (repeat: per_visit)."),
    "question_answered": ("blocked", "A question already answered (action_rules.questions_once)."),
    "escape_not_needed": ("blocked", "A way out offered only to a player held here."),
    "already_here": ("blocked", "It would only bring the player where they are."),
    "out_of_reach": ("blocked", "It leads to a place that does not border this one."),
    "outcomes_waiting": ("blocked", "Each of its outcomes waits for a setup (a payoff before its clue)."),
    "missing_item": ("blocked", "It needs an item the player does not carry (required_object)."),
    "missing_item_tag": ("blocked", "It needs an item with a tag the player does not carry (required_tag)."),
    "insufficient_coins": ("blocked", "It costs more coins than the player has (required_coins)."),
    "already_has_item": ("blocked", "It gives an item the player already carries (collects_object)."),
    "item_unobtainable": ("blocked", "The item it gives cannot be had now (collects_object)."),
    "unexplained": ("blocked", "Not offered; none of the known reasons applies (please report it)."),
    # blocked: the moment
    "event_in_progress": ("blocked", "An event is in progress: only its choices are offered."),
    "encounter": ("blocked", "A hostile person confronts the player: the encounter's choices come first."),
    "no_small_talk": ("blocked", "A hostile person (an encounter) does not chat unless dialogue is written for them."),
    # blocked: the source entity
    "npc_absent": ("blocked", "The person is not in this scene."),
    "npc_not_visible": ("blocked", "The person is here but not among those the scene shows (scene.max_visible_npcs, "
                                   "visibility)."),
    "npc_removed": ("blocked", "The person is gone from the story."),
    "npc_disabled": ("blocked", "The person's enabled_if does not hold."),
    "npc_left_out": ("blocked", "The person is not in this session's cast (session_scope)."),
    "npc_not_revealed": ("blocked", "Someone who matters (npc_roles) not found yet."),
    "npc_appearances_used": ("blocked", "Already seen in as many visits as they appear in (npc_behaviour.appearances)."),
    "npc_lives_elsewhere": ("blocked", "A unique person lives elsewhere now."),
    "npc_does_not_fit": ("blocked", "A generic person who does not fit this place's tags."),
    "npc_not_placed": ("blocked", "Could be here, but the scene placed others (its slots are taken)."),
    "item_not_here": ("blocked", "The item is not in this place (or is carried: carried items offer no actions)."),
    "item_not_visible": ("blocked", "The item is here but not among those the scene shows."),
    "item_disabled": ("blocked", "The item's enabled_if does not hold, or it is not in this session's cast."),
    # blocked: travel
    "exit_locked": ("blocked", "The player cannot leave this place until they escape or its exit opens."),
    "location_disabled": ("blocked", "The place's enabled_if does not hold, or it is not in this session's cast."),
    "not_adjacent": ("blocked", "The place does not border this one on the map."),
    "sent_only": ("blocked", "One only arrives there by being sent (location_constraints.entry: sent)."),
    "visits_used": ("blocked", "The place was visited as often as its tags allow (location_behaviour.visits)."),
    "unknown_location": ("blocked", "A connection to a place this world does not have."),
}
STATUS_ORDER = {"shown": 0, "hidden": 1, "blocked": 2}


def _reason(code, message=None, **detail):
    return {"code": code, "message": message or REASONS[code][1], "detail": detail or None}


def _number(value):
    return None if value is None or (isinstance(value, float) and math.isinf(value)) else round(float(value), 4)


class DecisionTrace:
    """Collects what the engine decides while building the options (GameState hooks) and why."""

    def __init__(self, state):
        self.state = state
        self.begin()
        self.limits = {}
        self.ranks = {}
        self.shown_actions = []

    def begin(self):
        self.records = {}
        self.order = []
        self.notes = []
        self.entities = {}
        self.mode = "scene"
        self.event = None

    # ---- recording ----------------------------------------------------------------------------

    def _record(self, action, status=None, stage=None, reason=None):
        key = id(action)
        record = self.records.get(key)
        if record is None:
            record = self.records[key] = {"action": action, "status": status, "stage": stage, "reasons": []}
            self.order.append(key)
        current = record["status"]
        # Shown wins; otherwise the more severe of hidden and blocked (a blocked action stays blocked).
        if status is not None and (current is None or status == "shown"
                                   or (current != "shown" and STATUS_ORDER[status] > STATUS_ORDER[current])):
            record["status"], record["stage"] = status, stage or record["stage"]
        if reason is not None and all(r["code"] != reason["code"] for r in record["reasons"]):
            record["reasons"].append(reason)
        return record

    def note(self, code, **detail):
        self.notes.append({"code": code, "detail": detail or None})

    def event_mode(self, event, started):
        self.mode = "event"
        npc = self.state.__dict__.get("event_source_npc")
        log = [entry for entry in self.state.event_log if entry.get("event") == event.name]
        cause = log[-1].get("cause") if log else None
        self.event = {"name": event.name, "title": event.title, "started_now": bool(started), "npc": npc,
                      "cause": cause}
        code = "encounter" if cause == "encounter" else "event_in_progress"
        detail = {"event": event.name, "npc": npc} if npc else {"event": event.name}
        # What the scene would offer otherwise waits: the place's, the people's and the items' actions.
        scene = list(self.state.current.actions)
        for char in self.state.__dict__.get("current_inhabitants") or []:
            scene.extend(char.actions)
        for obj in self.state.__dict__.get("location_objects") or []:
            scene.extend(obj.actions)
        for action in scene:
            self._record(action, "blocked", "moment", _reason(code, **detail))

    def entity_hidden(self, entity, code):
        self.entities[(type(entity).__name__, entity.name)] = code
        for action in entity.actions:
            self._record(action, "blocked", "source", _reason(code, entity=entity.name))

    def no_small_talk(self, char):
        talk = choice_ids_action("conversation", char.name, self.state)
        self._record(talk, "blocked", "source", _reason("no_small_talk", npc=char.name))

    def blocked(self, action, stage):
        reasons = (_requirement_reasons if stage == "requirements" else _filter_reasons)(self.state, action)
        for reason in reasons or [_reason("unexplained", stage=stage)]:
            self._record(action, "blocked", stage, reason)

    def navigation(self, offered):
        state = self.state
        locked = state._location_is_locked()
        for name in sorted(state.current.connected):
            if name in offered:
                continue
            travel = choice_ids_action("navigation", name, state)
            for reason in _travel_reasons(state, name, locked, offered):
                self._record(travel, "blocked", "travel", reason)

    def loop_filtered(self, actions, window):
        for action in actions:
            self._record(action, "hidden", "director", _reason("would_loop", window=window))

    def available(self, actions):
        for action in actions:
            self._record(action, None, "available")

    def ranked(self, pairs, limit, order):
        self.limits.update(limit=limit, order=order)
        for rank, (score, action) in enumerate(pairs, start=1):
            self.ranks[id(action)] = (rank, score)
            self._record(action, None, "ranked")

    def conversation(self, person, others):
        self.mode = "conversation"
        for action in others:
            self._record(action, "hidden", "conversation", _reason("outside_conversation", person=person))

    def focus_place(self, place, others):
        self.mode = "focus_place"
        for action in others:
            self._record(action, "hidden", "conversation", _reason("outside_focus_place", place=place))

    def just_left(self, person, members):
        for action in members:
            self._record(action, "hidden", "conversation", _reason("just_left_conversation", person=person))

    def behind_approach(self, option, members):
        for action in members:
            self._record(action, "hidden", "conversation",
                         _reason("behind_approach", person=option.character_name, approach=option))

    def relevance(self, **settings):
        self.limits.update({key: _number(value) if isinstance(value, float) else value
                            for key, value in settings.items()})

    def chosen(self, action, why):
        self._record(action, None, "presentation", _reason(why))

    def hidden(self, action, code, **detail):
        self._record(action, "hidden", "presentation", _reason(code, **{k: _number(v) if isinstance(v, float) else v
                                                                        for k, v in detail.items()}))

    def over_limit(self, actions, limit, where):
        for action in actions:
            self._record(action, "hidden", "presentation", _reason("choice_limit", limit=limit, where=where))

    def shown(self, chosen):
        self.shown_actions = list(chosen)

    # ---- the result ---------------------------------------------------------------------------

    def result(self, session_id=None):
        state = self.state
        shown_ids = choice_ids.assign_ids(self.shown_actions, state)
        shown_by_key = {id(action): action_id for action, action_id in zip(self.shown_actions, shown_ids)}
        for action in self.shown_actions:
            record = self._record(action, "shown", "shown")
            if not any(REASONS.get(r["code"], ("",))[0] == "shown" for r in record["reasons"]):
                code = {"approach": "rank", "leave": "rank"}.get(action.source)
                if self.mode == "event":
                    code = "event_choice"
                elif self.mode == "conversation":
                    code = "conversation_member" if action.source != "leave" else "rank"
                elif self.mode == "focus_place":
                    code = "focus_member" if action.source != "leave" else "rank"
                elif not self.limits.get("cap"):
                    code = "unfiltered"
                record["reasons"].insert(0, _reason(code or "rank"))
            # Shown: whatever hid it on the way (a pass that was superseded) is not a reason now.
            record["reasons"] = [r for r in record["reasons"] if REASONS.get(r["code"], ("",))[0] == "shown"]
        records = [self.records[key] for key in self.order]
        for record in records:  # available, ranked, and neither shown nor hidden: left by the limits
            if record["status"] is None:
                record["status"], record["stage"] = "hidden", "presentation"
                record["reasons"].append(_reason("choice_limit", limit=self.limits.get("limit"), where="not chosen"))
        others = [r["action"] for r in records if id(r["action"]) not in shown_by_key]
        other_ids = dict(zip(map(id, others), choice_ids.assign_ids(others, state)))
        ids = {**other_ids, **shown_by_key}
        actions = []
        for record in records:
            action = record["action"]
            origin = choice_ids.provenance(action, state)
            rank, score = self.ranks.get(id(action), (None, None))
            reasons = []
            for reason in record["reasons"]:
                detail = dict(reason["detail"] or {})
                if hasattr(detail.get("approach"), "source"):  # the "Approach <person>" option, by its id
                    detail["approach"] = ids.get(id(detail["approach"]), choice_ids.base_id(detail["approach"], state))
                reasons.append({**reason, "detail": detail or None})
            actions.append({
                "id": ids[id(action)], "label": action.name, "category": origin["category"], "owner": action.owner,
                "source": origin["entity"], "location": origin["location"], "key": origin["key"],
                "content_ref": origin["content_ref"], "status": record["status"], "stage": record["stage"],
                "rank": rank, "score": _number(score), "reasons": reasons,
            })
        actions.sort(key=lambda entry: (STATUS_ORDER[entry["status"]],
                                        shown_ids.index(entry["id"]) if entry["id"] in shown_ids else 0,
                                        entry["rank"] or 10 ** 6))
        return actions, shown_ids


def choice_ids_action(source, name, state):
    """An action the engine would make (talk with someone, travel somewhere), for the trace."""
    from .actions import Action

    if source == "conversation":
        return Action(f"({name}) {state.narrate('talk_action', stable=name, npc=name)}", [], character_name=name,
                      source="conversation", owner=name, repeat="always")
    return Action(state.narrate("travel_new", stable=name, location=name), [], source="navigation", owner=name,
                  repeat="always")


# ---- why an action is not offered (the engine's own predicates) --------------------------------

def _condition_reason(state, code, raw):
    try:
        failing = conditions.explain(raw, state.facts)
    except Exception:  # noqa: BLE001 - an explanation must never break the trace
        failing = []
    return _reason(code, condition=_plain(raw), failing=_plain(failing))


def _plain(value):
    if hasattr(value, "tree"):
        value = getattr(value, "source", None) or str(value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    return value if isinstance(value, (str, int, float, bool)) or value is None else str(value)


def _filter_reasons(state, a):
    """Why the composite filter (offered, enabled, not arriving here, not out of reach) refused ``a``."""
    reasons = []
    scope = state._action_scope(a)
    if state._repeat_rule(a) == "once" and (scope, a.name) in state.spent_actions:
        reasons.append(_reason("used_once", repeat="once"))
    if a.exclusive_group and (scope, a.exclusive_group) in state.closed_groups:
        reasons.append(_reason("exclusive_choice_made", group=a.exclusive_group))
    if not reasons:
        if state._escape_action(a) and not state._location_is_locked():
            reasons.append(_reason("escape_not_needed"))
        elif a.name in state.visit_actions_performed and not state._escape_action(a):
            if state._is_question(a):
                reasons.append(_reason("question_answered"))
            elif state._repeat_rule(a) == "per_visit":
                reasons.append(_reason("done_this_visit", repeat="per_visit"))
    if getattr(a, "until", None) and state.holds(a.until):
        reasons.append(_reason("until_reached", condition=_plain(a.until)))
    if a.outcomes and all(state._stale(o) for o in a.outcomes):
        reasons.append(_reason("outcomes_stale"))
    if state._left_out(a):
        reasons.append(_reason("condition_false", "Not in this session's cast (session_scope).", scope="session"))
    elif not state.is_enabled(a):
        reasons.append(_condition_reason(state, "condition_false", getattr(a, "enabled_if", None)))
    if a.triggers_event and not state._event_available(a.triggers_event):
        event = state.world.events.get(a.triggers_event)
        why = state._event_blocked(event) if event is not None else None
        reasons.append(_reason("event_unavailable", f"The event it starts ({a.triggers_event}) cannot start now: "
                                                    f"{why}." if why else None, event=a.triggers_event))
    if state._only_arrives_here(a):
        reasons.append(_reason("already_here", location=state.current.name))
    if state._out_of_reach(a):
        reasons.append(_reason("out_of_reach"))
    return reasons


def _requirement_reasons(state, a):
    """Why _prepare_action refused ``a`` (its outcomes, items, coins)."""
    reasons = []
    if a.outcomes and a.source not in ("navigation", "conversation") and not any(
            state._outcome_open(o) for o in a.outcomes):
        reasons.append(_reason("outcomes_waiting", needs=sorted({str(o.requires_setup) for o in a.outcomes
                                                                 if getattr(o, "requires_setup", None)}) or None))
    if a.required_tag and not any(a.required_tag in obj.associated_tags and obj.name in state.inventory
                                  for obj in state.world.objects):
        reasons.append(_reason("missing_item_tag", tag=a.required_tag))
    if a.required_object and a.required_object not in state.inventory:
        reasons.append(_reason("missing_item", item=a.required_object))
    if a.required_coins and a.required_coins > state.coins:
        reasons.append(_reason("insufficient_coins", f"It costs {a.required_coins} coins; the player has "
                                                     f"{state.coins}.", need=a.required_coins, have=state.coins))
    if a.collects_object:
        if a.collects_object in state.inventory:
            reasons.append(_reason("already_has_item", item=a.collects_object))
        elif not state._item_obtainable(a.collects_object):
            reasons.append(_reason("item_unobtainable", item=a.collects_object))
    return reasons


def _travel_reasons(state, name, locked, offered):
    if locked:
        return [_reason("exit_locked", location=state.current.name)]
    if name not in state.world.locations:
        return [_reason("unknown_location", location=name)]
    reasons = []
    if not state.is_enabled(state.world.locations[name]):
        reasons.append(_reason("location_disabled", location=name))
    if not state._adjacent(state.current.name, name):
        reasons.append(_reason("not_adjacent", location=name))
    if state.sent_only(name):
        reasons.append(_reason("sent_only", location=name))
    if not reasons and state._visits_used(name):
        reasons.append(_reason("visits_used", location=name))
    return reasons or [_reason("unexplained", stage="travel")]


# ---- the people and items of the world, and where they are ------------------------------------

def _npc_reason(state, char):
    if state._is_removed(char.name):
        return "npc_removed"
    if state._left_out(char):
        return "npc_left_out"
    if not state.is_enabled(char):
        return "npc_disabled"
    if not state._role_revealed(char):
        return "npc_not_revealed"
    if state._appearances_used(char):
        return "npc_appearances_used"
    if char.unique and state.npc_homes.get(char.name) != state.current.name:
        return "npc_lives_elsewhere"
    if not char.unique and not state._npc_fits_location(char):
        return "npc_does_not_fit"
    return "npc_not_placed"


def _entities(trace):
    state = trace.state
    here = {c.name for c in state.__dict__.get("current_inhabitants") or []}
    visible = set(state.__dict__.get("visible_inhabitants") or [])
    rows = []
    for char in state.world.characters:
        if char.name in visible:
            status, code = "in_scene", None
        elif char.name in here:
            status, code = "present_not_visible", "npc_not_visible"
        else:
            status, code = "absent", _npc_reason(state, char)
        if code is not None:
            for action in char.actions:
                trace._record(action, "blocked", "source", _reason(code, entity=char.name))
        rows.append({"kind": "npc", "name": char.name, "status": status,
                     "reason": _reason(code, entity=char.name) if code else None,
                     "hostile": bool(char.encounter_event), "unique": bool(char.unique)})
    in_place = {o.name for o in state.__dict__.get("location_objects") or []}
    shown = set(state.__dict__.get("visible_objects") or [])
    for obj in state.world.objects:
        if obj.name in shown:
            status, code = "in_scene", None
        elif obj.name in in_place:
            status, code = "present_not_visible", "item_not_visible"
        elif not state.is_enabled(obj):
            status, code = "absent", "item_disabled"
        else:
            status, code = ("carried" if obj.name in state.inventory else "absent"), "item_not_here"
        if code is not None and obj.actions:
            for action in obj.actions:
                trace._record(action, "blocked", "source", _reason(code, entity=obj.name))
        rows.append({"kind": "item", "name": obj.name, "status": status,
                     "reason": _reason(code, entity=obj.name) if code else None})
    for event in state.world.events.values():
        if state.active_event is not None and state.active_event.name == event.name:
            status, why = "active", None
        else:
            why = state._event_blocked(event)
            status = "blocked" if why else "can_start"
        rows.append({"kind": "event", "name": event.name, "status": status, "reason": why})
    return rows


# ---- the trace ---------------------------------------------------------------------------------

def decision_trace(state, limit=None, order=None, session_id=None):
    """Why each action is shown, hidden or blocked now: a JSON-ready dict (see the module docstring).
    ``limit`` / ``order``: as the player's options are asked for (sessions._player_options). Made on a
    copy of ``state``: the game, its dice and its save store are left as they are."""
    from utils.config_loader import config

    if state.ended:
        return {"session_id": session_id, "state_version": state.state_version, "ended": True, "mode": "ended",
                "shown": [], "actions": [], "entities": [], "notes": [], "limits": {}, "codes": _codes()}
    probe = copy.deepcopy(state)          # its own dice (a copy of the story's), scene and conversation
    probe.__dict__["_store"] = None       # nothing it does is saved to the player's profile
    probe.__dict__["interactive"] = probe.__dict__["verbose"] = False
    trace = probe.__dict__["_decision_trace"] = DecisionTrace(probe)
    cfg = config.get("player_options") or {}
    limit = limit if limit is not None else max(1, int(cfg.get("limit", 6)))
    order = order or ("world" if cfg.get("order", "ranked") == "world" else "ranked")
    probe.player_options(limit=limit, order=order)
    entities = _entities(trace)
    actions, shown = trace.result(session_id)
    talking = probe.conversation()
    notes = [{**note, "message": _NOTES.get(note["code"], note["code"])} for note in trace.notes]
    if trace.event is not None and trace.event["started_now"]:
        notes.append({"code": "event_would_start", "message": _NOTES["event_would_start"],
                      "detail": {"event": trace.event["name"], "npc": trace.event["npc"]}})
    return {
        "session_id": session_id,
        "state_version": state.state_version,
        "ended": False,
        "turn": state.actions_taken,
        "location": probe.current.name,
        "mode": trace.mode,
        "event": trace.event,
        "talking_to": talking,
        "tension": _number(probe.tension()),
        "limits": {**trace.limits, "limit": limit, "order": order},
        "shown": shown,
        "counts": {status: sum(1 for a in actions if a["status"] == status) for status in STATUS_ORDER},
        "actions": actions,
        "entities": entities,
        "notes": notes,
        "codes": _codes(),
    }


_NOTES = {
    "loop_filter_skipped": "The director's loop filter was not applied: it would have left too few choices "
                           "(director.min_options).",
    "event_fallback": "Every choice of the event is unavailable: the player is offered to step away "
                      "(authored_events.fallback_action).",
    "conversations_paused": "Conversations are set aside: the player approached and left people too often without "
                            "acting (conversations.max_free_steps), so everything is on the scene.",
    "event_would_start": "Building the options would start an event or an encounter now (this copy shows it): "
                         "the player meets it when the scene is shown.",
}


def _codes():
    return {code: {"status": status, "meaning": meaning} for code, (status, meaning) in REASONS.items()}
