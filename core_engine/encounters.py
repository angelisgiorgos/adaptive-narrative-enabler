"""Dynamic hostile encounters: a director that starts authored encounters when the story makes
them plausible (config: ``encounters`` in hyperparameters.yaml; templates: events with an
``encounter`` block).

An encounter template is an ordinary authored event (world_definition.events): its title,
descriptions, tags and actions with their conditions, checks and outcomes are the encounter, and
the player's choices (fight, defend, evade, escape, negotiate, surrender, seek help...) are its
actions. Its ``encounter`` block says when the director may start it and what follows::

    events:
      glacier_thieves:
        title: Thieves on the glacier
        tags: [threat, ambush]
        encounter:
          category: ambush           # ambush | hostile_patrol | faction_attack | npc_assault |
                                     # location_raid | environmental_threat | defensive_encounter
          tags_any: [glacier]        # only where the place has one of these tags (empty: anywhere not safe)
          tags_none: [cloister]      # never where it has one of these
          weight: 1.0                # among the encounters that could start
          danger: 1.0                # x its chance
          difficulty: 1.0            # x the checks of its actions (1: as written)
          faction: Glacier Thieves   # who attacks (encounters.factions: standing, hostility)
          hostile_only: true         # only while that faction is hostile (default for faction_attack)
          npc: Thief                 # the NPC behind it (npc_assault: they must be in the scene)
          min_turn: 4                # not before this turn
          stages: [middle, late]     # early | middle | late: where the story is (its progress)
          min_tension: 0.0           # GameState.tension 0..1
          max_tension: 1.0
          min_alert: 0               # the watch's alert (stakes.alert)
          max_alert: null
          min_power: 0.0             # the player's Power, how able they are to fight
          max_per_story: 1           # at most this many times in a story
          cooldown: 20               # turns before it can come again (encounters.template_cooldown)
          on_win:  {text, effects, standing: {Glacier Thieves: -1}, side_quest: key, consequence: kind,
                    callback: text}   # callback: told near the climax (side_story.callbacks)
          on_loss: {text, effects, standing, side_quest, consequence, callback}
          on_escape: {...}           # stepped away (the event's fallback)
          rewards_once: true         # on_win's effects are given once per story, however often it is won

When (``encounters.director``, once a turn, after the mandatory encounter of a hostile NPC and the
random events had their chance): never while the story is in an event, in the opening turns
(``opening_turns``), sooner than ``min_separation`` turns after the last attack (longer after a
lost one: ``after_loss_separation``), beyond ``max_per_session`` attacks, in a safe place
(``safe_tags``), while the player is hurt below ``min_health``, held in a cell, talking with
someone, has a side quest on offer or waiting to be handed over, faces a climax, or is in the
finale (unless ``in_conversation`` / ``in_climax`` / ``in_finale``). Then each template is checked
(its event's own rules too: enabled_if, which can ask about missions, side quests and what was
done; repeat, max_occurrences, cooldown), and one roll decides whether an attack comes:
``rate`` x the place's danger (``danger_tags``) x (1 + tension x ``tension_weight``) x (1 + alert x
``alert_weight``) x a hostile faction's (1 + ``hostility_weight``) x the template's danger, at most
``max_chance``. Which one: weighted by its weight, its category's weight and how many of its tags
the place shares.

The roll and the pick use a random stream derived from the story's dice and the moment (their
state, the turn and the place), not the dice themselves: a fixed story state and seed give the same
encounters, and a story where no attack comes is the same as without the director.

Losing does not end the story: ``lethal: false`` (the default) leaves the player at 1 health
("spared"), the event's own outcomes carry the consequences (coins, items, being sent somewhere),
``on_loss`` adds the encounter's, and the next attack waits longer. A player an encounter leaves
locked in (an outcome that sends them to a cell) is let out after ``captive_release`` turns there at
most, whether or not the director's own ``release_after`` is on: a lost encounter cannot end the
story in a cell (0: leave it to the director and the cell's own escape). ``difficulty`` (x the
template's) makes the checks of its actions harder by ``difficulty_points`` per 1.0 above 1.

What a story knows is plain data (snapshots, save games): ``state.dynamic_encounters`` [{turn, event,
category, location, faction, npc, result}], ``state.faction_shift`` {faction: change}, and
``state.encounter_rewards`` (templates whose on_win was paid).
"""
from __future__ import annotations

import hashlib
import random

from utils.config_loader import config

from . import side_story
from .context import with_state_context

CATEGORIES = ("ambush", "hostile_patrol", "faction_attack", "npc_assault", "location_raid", "environmental_threat",
              "defensive_encounter")
STAGES = ("early", "middle", "late")
SPEC_FIELDS = ("category", "tags_any", "tags_none", "weight", "danger", "difficulty", "faction", "hostile_only", "npc",
               "min_turn", "stages", "min_tension", "max_tension", "min_alert", "max_alert", "min_power",
               "max_per_story", "cooldown", "on_win", "on_loss", "on_escape", "rewards_once")
RESULT_FIELDS = ("text", "effects", "standing", "side_quest", "consequence", "callback")
DIRECTOR_CAUSE = "encounter_director"
# What explain()'s gate and candidate status codes mean (the decision trace's encounters.codes).
CODES = {
    "eligible": "It could start now (the roll decides).",
    "disabled": "The encounter director is off (encounters.enabled).",
    "ended": "The story has ended.",
    "event_in_progress": "An event is under way.",
    "no_templates": "The world has no encounter templates (events with an `encounter` block).",
    "opening": "Still the opening turns (encounters.opening_turns).",
    "cap_reached": "This story had its attacks already (encounters.max_per_session).",
    "separation": "Too soon after the last attack (encounters.min_separation).",
    "recovering": "Too soon after an attack the player lost (encounters.after_loss_separation).",
    "safe_place": "The place is safe (encounters.safe_tags).",
    "low_health": "The player is hurt (encounters.min_health).",
    "held": "The player is held (a cell).",
    "conversation": "The player is talking with someone (encounters.in_conversation).",
    "choice_pending": "A choice waits for the player (a side quest on offer or to hand over, a bargain).",
    "climax": "The climax waits (encounters.in_climax).",
    "finale": "The finale has begun (encounters.in_finale).",
    "category_off": "Its category is switched off (encounters.categories).",
    "not_here": "Not in this place (its tags_any / tags_none).",
    "too_early": "Before its min_turn.",
    "wrong_stage": "Not at this stage of the story (its stages).",
    "tension": "The story's tension is outside its min_tension / max_tension.",
    "alert": "The alert is outside its min_alert / max_alert.",
    "faction_not_hostile": "Its faction is not hostile (hostile_only).",
    "too_weak": "The player's Power is below its min_power.",
    "max_per_story": "It came as often as it may in a story (max_per_story).",
    "cooldown": "Too soon since it last came (its cooldown, encounters.template_cooldown).",
    "repeat_last": "It was the last attack (encounters.no_repeat_last).",
    "npc_absent": "Its NPC is not in the scene.",
    "npc_gone": "Its NPC is gone from the story.",
    "npc_unwilling": "Its NPC would not do it: friendly with the player, or never does (core_engine/relationships.py).",
    "event_blocked": "Its event's own rules hold it back (enabled_if, repeat, max_occurrences, cooldown).",
}

DEFAULTS = {
    "enabled": True, "rate": 0.06, "max_chance": 0.3, "opening_turns": 3, "min_separation": 6,
    "after_loss_separation": 10, "max_per_session": 3, "template_cooldown": 20, "no_repeat_last": True,
    "safe_tags": ["safe", "sanctuary", "home", "medical", "quiet", "cloister", "garden"],
    "danger_tags": {"threat": 0.6, "dark": 0.3, "wild": 0.4, "guarded": 0.2, "abandoned": 0.3},
    "tension_weight": 0.5, "alert_weight": 0.15, "hostility_weight": 0.5, "min_health": 0.4, "difficulty": 1.0,
    "difficulty_points": 10, "lethal": False, "in_finale": False, "in_climax": False, "in_conversation": False,
    "captive_release": 5, "categories": {}, "factions": {},
}


def settings():
    """The encounter policy (``encounters`` in the tuning), its defaults filled in."""
    own = config.get("encounters") or {}
    merged = {**DEFAULTS, **{key: value for key, value in own.items() if value is not None}}
    merged["categories"] = {name: {"enabled": True, "weight": 1.0, **((own.get("categories") or {}).get(name) or {})}
                            for name in CATEGORIES}
    merged["factions"] = dict(own.get("factions") or {})
    return merged


# ---- templates ------------------------------------------------------------------------------------

class EncounterSpec:
    """An event's ``encounter`` block, read once."""

    def __init__(self, raw, event_name):
        raw = dict(raw or {})
        self.event = event_name
        self.category = raw.get("category")
        if self.category not in CATEGORIES:
            raise ValueError(f"Event '{event_name}': `encounter.category` must be one of {', '.join(CATEGORIES)}.")
        self.tags_any = {str(t).lower() for t in raw.get("tags_any") or []}
        self.tags_none = {str(t).lower() for t in raw.get("tags_none") or []}
        self.weight = float(raw.get("weight", 1.0))
        self.danger = float(raw.get("danger", 1.0))
        self.difficulty = float(raw.get("difficulty", 1.0))
        self.faction = raw.get("faction") or None
        self.hostile_only = bool(raw.get("hostile_only", self.category == "faction_attack" and bool(self.faction)))
        self.npc = raw.get("npc") or None
        self.min_turn = int(raw.get("min_turn", 0) or 0)
        self.stages = list(raw.get("stages") or STAGES)
        self.min_tension = float(raw.get("min_tension", 0.0))
        self.max_tension = float(raw.get("max_tension", 1.0))
        self.min_alert = int(raw.get("min_alert", 0) or 0)
        self.max_alert = raw.get("max_alert")
        self.min_power = float(raw.get("min_power", 0.0))
        self.max_per_story = int(raw.get("max_per_story", 1) or 1)
        self.cooldown = raw.get("cooldown")
        self.rewards_once = bool(raw.get("rewards_once", True))
        self.results = {}
        for key in ("on_win", "on_loss", "on_escape"):
            block = dict(raw.get(key) or {})
            from .effects import EffectList

            block["effects"] = EffectList.of(block.get("effects"))
            self.results[key] = block


def templates(state):
    """{event name: (event, spec)} of the world's encounter templates, in the order they are written."""
    found = {}
    for name, event in state.world.events.items():
        spec = getattr(event, "encounter", None)
        if spec is not None:
            found[name] = (event, spec)
    return found


# ---- what the story is like now ---------------------------------------------------------------

@with_state_context
def stage(state):
    """Where the story is: early, middle or late (its share of the action limit)."""
    limit = max(1, int(state._max_actions())) if hasattr(state, "_max_actions") else 100
    progress = state.actions_taken / limit
    return "early" if progress < 1 / 3 else "middle" if progress < 2 / 3 else "late"


@with_state_context
def standing(state, faction):
    """A faction's standing with the player: its own (world ``factions``, or encounters.factions in the
    tuning; or the persistent variable it names), changed by this story (faction_shift). One faction
    model for the whole engine: core_engine/relationships.py."""
    from . import relationships

    return relationships.standing(state, faction)


@with_state_context
def hostile(state, faction):
    """Whether a faction is hostile now: its standing below its hostile_below (0 for a faction only
    the tuning names; relationships.faction_hostile_below for a world faction)."""
    from . import relationships

    return relationships.faction_status(state, faction) == "hostile"


def _place_tags(state):
    return {str(tag).lower() for tag in getattr(state.current, "tags", None) or ()}


def _last_attack(state):
    """The turn of the latest attack (a dynamic encounter or a hostile NPC's), and whether it was lost."""
    # Counted from when an attack ended (a quiet spell), or began while it goes on.
    turns = [entry.get("ended_step", entry.get("step", 0)) for entry in state.event_log
             if entry.get("cause") in ("encounter", DIRECTOR_CAUSE)]
    lost = [entry.get("turn", 0) for entry in state.__dict__.get("dynamic_encounters") or ()
            if entry.get("result") == "failure"]
    return (max(turns) if turns else None), (max(lost) if lost else None)


def _gate(state, cfg):
    """Why the director cannot start an encounter now (a code), or None."""
    if not cfg["enabled"]:
        return "disabled"
    if state.ended:
        return "ended"
    if state.active_event:
        return "event_in_progress"
    if not templates(state):
        return "no_templates"
    if state.actions_taken < int(cfg["opening_turns"]):
        return "opening"
    attacks = state.__dict__.get("dynamic_encounters") or []
    if len(attacks) >= int(cfg["max_per_session"]):
        return "cap_reached"
    last, lost = _last_attack(state)
    if last is not None and state.actions_taken - last < int(cfg["min_separation"]):
        return "separation"
    if lost is not None and state.actions_taken - lost < int(cfg["after_loss_separation"]):
        return "recovering"
    if _place_tags(state) & {str(tag).lower() for tag in cfg["safe_tags"]}:
        return "safe_place"
    if state.max_health and state.health < float(cfg["min_health"]) * state.max_health:
        return "low_health"
    if hasattr(state, "_location_is_locked") and state._location_is_locked():
        return "held"
    if not cfg["in_conversation"] and (getattr(state, "talking_to", None) or getattr(state, "focus_place", None)):
        return "conversation"
    if _choice_pending(state):
        return "choice_pending"
    if not cfg["in_climax"] and (state.__dict__.get("climax_state") or {}).get("armed"):
        return "climax"
    if not cfg["in_finale"] and hasattr(state, "in_finale") and state.in_finale():
        return "finale"
    # The side-story policy (core_engine/side_story.py): no new side content while the main story
    # needs the room (a clock running, the main quest standing still, too many threads open, ...).
    # An author who lets attacks into the finale or a waiting climax (in_finale, in_climax) is obeyed.
    return side_story.hold(state, "encounter", allow=tuple(
        moment for moment in ("finale", "climax") if cfg[f"in_{moment}"]))


def _choice_pending(state):
    """An unresolved choice the story put to the player: a side quest on offer or waiting to be
    handed over, a bargain to take or refuse."""
    for record in (state.__dict__.get("side_quests") or {}).values():
        if record.get("status") == "offered" or (record.get("status") == "active" and record.get("ready")):
            return True
    return bool(state.__dict__.get("pending_bargain") or state.__dict__.get("_pending_bargain"))


def _template_block(state, event, spec, cfg):
    """Why a template cannot start now (a code), or None."""
    category = cfg["categories"].get(spec.category) or {}
    if not category.get("enabled", True):
        return "category_off"
    tags = _place_tags(state)
    if spec.tags_any and not (tags & spec.tags_any):
        return "not_here"
    if tags & spec.tags_none:
        return "not_here"
    if state.actions_taken < spec.min_turn:
        return "too_early"
    if stage(state) not in spec.stages:
        return "wrong_stage"
    tension = state.tension() if hasattr(state, "tension") else 0.0
    if not spec.min_tension <= tension <= spec.max_tension:
        return "tension"
    alert = int(state.__dict__.get("alert", 0) or 0)
    if alert < spec.min_alert or (spec.max_alert is not None and alert > int(spec.max_alert)):
        return "alert"
    if spec.faction and spec.hostile_only and not hostile(state, spec.faction):
        return "faction_not_hostile"
    if float(getattr(state, "power", 0.0)) < spec.min_power:
        return "too_weak"
    past = [entry for entry in state.__dict__.get("dynamic_encounters") or () if entry.get("event") == spec.event]
    if len(past) >= spec.max_per_story:
        return "max_per_story"
    cooldown = int(spec.cooldown if spec.cooldown is not None else cfg["template_cooldown"])
    if past and state.actions_taken - past[-1]["turn"] < cooldown:
        return "cooldown"
    attacks = state.__dict__.get("dynamic_encounters") or []
    if cfg["no_repeat_last"] and attacks and attacks[-1].get("event") == spec.event:
        return "repeat_last"
    if spec.npc and spec.category == "npc_assault" and spec.npc not in (state.__dict__.get("visible_inhabitants") or ()):
        return "npc_absent"
    if spec.npc and state._is_removed(spec.npc):
        return "npc_gone"
    if spec.npc and spec.category == "npc_assault":
        from . import relationships

        if relationships.permits(state, spec.npc, "attack") in ("friendly", "forbidden"):
            return "npc_unwilling"  # the director cannot set a friend (or someone who never would) on them
    if state._event_blocked(event) is not None:
        return "event_blocked"
    return None


def _chance(state, spec, cfg):
    tags = _place_tags(state)
    danger = 1.0 + sum(float(weight) for tag, weight in (cfg["danger_tags"] or {}).items() if tag in tags)
    tension = state.tension() if hasattr(state, "tension") else 0.0
    alert = int(state.__dict__.get("alert", 0) or 0)
    chance = float(cfg["rate"]) * danger * (1 + tension * float(cfg["tension_weight"])) \
        * (1 + alert * float(cfg["alert_weight"])) * spec.danger
    if spec.faction and hostile(state, spec.faction):
        chance *= 1 + float(cfg["hostility_weight"])
    chance *= side_story.encounter_chance_factor(state)  # the storyteller's appetite for attacks (side_story)
    return min(float(cfg["max_chance"]), max(0.0, chance))


def _stream(state, salt):
    """A random stream for this moment, derived from the story's dice (read, not drawn), the turn
    and the place: deterministic for a fixed story state and seed."""
    text = f"{salt}|{state.rng.getstate()!r}|{state.actions_taken}|{state.current.name}"
    return random.Random(hashlib.sha256(text.encode("utf-8")).hexdigest())


@with_state_context
def candidates(state, cfg=None):
    """[(event, spec, chance)] of the templates that could start now (the gate aside)."""
    cfg = cfg or settings()
    return [(event, spec, _chance(state, spec, cfg)) for event, spec in templates(state).values()
            if _template_block(state, event, spec, cfg) is None]


@with_state_context
def maybe_start(state):
    """Once a turn: perhaps start a dynamic encounter (module docstring). Returns whether one started."""
    if state.__dict__.get("_encounter_checked") == state.actions_taken:
        return False
    state.__dict__["_encounter_checked"] = state.actions_taken
    cfg = settings()
    if _gate(state, cfg) is not None:
        return False
    found = candidates(state, cfg)
    if not found:
        return False
    stream = _stream(state, "encounter")
    if stream.random() >= max(chance for _, _, chance in found):
        return False
    tags = _place_tags(state)
    weights = [spec.weight * float(cfg["categories"][spec.category]["weight"]) * (1 + len(tags & spec.tags_any))
               * side_story.encounter_weight(state, event, spec) for event, spec, _ in found]
    if not any(weight > 0 for weight in weights):
        return False
    event, spec, _ = stream.choices(found, weights=weights, k=1)[0]
    return start(state, event, spec)


def start(state, event, spec):
    """Start a template's encounter (its intro, then its event)."""
    npc = spec.npc if spec.npc and not state._is_removed(spec.npc) else None
    side_story.note(state, "encounter", event.name, event.title, side_story.encounter_relevance(state, event, spec))
    state._say(f"encounter_{spec.category}", title=event.title, location=state.current.name,
               faction=spec.faction or "", npc=state._called(npc) if npc else "")
    if not state._enter_event(event.name, source_npc=npc, cause=DIRECTOR_CAUSE):
        state.side_story_log.pop()  # it did not come after all
        return False
    state.__dict__.setdefault("dynamic_encounters", []).append({
        "turn": state.actions_taken, "event": event.name, "category": spec.category, "location": state.current.name,
        "faction": spec.faction, "npc": npc, "result": None})
    return True


# ---- the outcome ----------------------------------------------------------------------------------

@with_state_context
def check_penalty(state, action):
    """How much harder the checks of an encounter's actions are (encounters.difficulty x the template's
    difficulty, encounters.difficulty_points per 1.0 above 1; easier below)."""
    event = state.active_event
    spec = getattr(event, "encounter", None) if event is not None else None
    if spec is None or action is None or action.source != "event":
        return 0
    cfg = settings()
    return int(round((float(cfg["difficulty"]) * spec.difficulty - 1.0) * float(cfg["difficulty_points"])))


@with_state_context
def resolved(state, event, entry, outcome=None):
    """A dynamic encounter has been resolved (GameState._finish_event, before death is looked at):
    won, lost (a failed check, or an outcome told as a failure: giving in, being caught) or escaped
    (the event's fallback); the player is spared at 1 health unless encounters.lethal; its on_win /
    on_loss / on_escape follow (on_win's effects once per story when rewards_once)."""
    spec = getattr(event, "encounter", None)
    if spec is None or entry.get("cause") != DIRECTOR_CAUSE:
        return
    result = entry.get("result")
    if result == "success" and outcome is not None:
        from . import rules

        if rules.is_failure(outcome, set(rules.settings()["failure_tags"])):
            result = "failure"  # no check, but what happened is a loss (giving in, being caught): it counts so
    records = [r for r in state.__dict__.get("dynamic_encounters") or () if r.get("event") == event.name
               and r.get("result") is None]
    if records:
        records[-1]["result"] = result
    cfg = settings()
    if state.health_player.is_dead and not cfg["lethal"]:
        state.health_player.health = 1
        state.update_power()
        state._say("encounter_spared", title=event.title)
        result = "failure"
        if records:
            records[-1]["result"] = "failure"
    if records and hasattr(state, "_location_is_locked") and state._location_is_locked():
        records[-1]["held_in"] = list(state._visit_id())  # where the encounter left them locked in (this visit)
    thread = f"{event.name}@{records[-1]['turn']}" if records else event.name  # side_story_links key
    if records and records[-1].get("held_in"):
        side_story.link(state, "encounter", thread, "captured")
    key = {"success": "on_win", "failure": "on_loss", "abandoned": "on_escape"}.get(result)
    block = spec.results.get(key) if key else None
    if not block:
        return
    paid = state.__dict__.setdefault("encounter_rewards", [])
    once = key == "on_win" and spec.rewards_once and event.name in paid
    if block.get("text"):
        state.history.append(str(block["text"]))
    if not once:
        state._run_effects(block["effects"])
        if key == "on_win":
            paid.append(event.name)
        if side_story.effect_kind(block["effects"]):
            side_story.link(state, "encounter", thread, side_story.effect_kind(block["effects"]))
    for faction, delta in (block.get("standing") or {}).items():
        from . import relationships

        relationships.change_standing(state, faction, float(delta), cause="encounter")
        side_story.link(state, "encounter", thread, "standing", faction)
    if block.get("side_quest"):
        from . import side_quests

        if side_quests.hook(state, block["side_quest"]):  # a rescue, an investigation: offered to the player
            side_story.link(state, "encounter", thread, "hook", block["side_quest"])
    if block.get("consequence") and hasattr(state, "_queue_consequence"):
        state._queue_consequence(block["consequence"], spec.npc or spec.faction)
        side_story.link(state, "encounter", thread, "consequence", block["consequence"])
    if block.get("callback") and records:
        records[-1]["callback"] = str(block["callback"])  # told near the climax (side_story.callbacks)


@with_state_context
def captive_release(state):
    """Turns after which a player an encounter left locked in is let out (``captive_release``),
    or None: not held by an encounter (still in that visit to that cell), or the rule is off."""
    turns = int(settings()["captive_release"] or 0)
    attacks = state.__dict__.get("dynamic_encounters") or []
    if turns <= 0 or not attacks or not attacks[-1].get("held_in"):
        return None
    if list(state._visit_id()) != list(attacks[-1]["held_in"]) or not state._location_is_locked():
        return None
    return turns


def freed(state):
    """The player is out of the cell an encounter put them in (released, or escaped)."""
    attacks = state.__dict__.get("dynamic_encounters") or []
    if attacks and attacks[-1].get("held_in"):
        attacks[-1]["held_in"] = None


def parameters(state):
    """The encounters as the session's parameters (ane.session_state/1 ``encounters``): {attacks
    [{turn, event, category, location, faction, npc, result}], factions {faction: standing change},
    rewards_paid [templates whose on_win was paid]}."""
    keys = ("turn", "event", "category", "location", "faction", "npc", "result")
    return {"attacks": [{key: record.get(key) for key in keys} for record in state.__dict__.get("dynamic_encounters") or ()],
            "factions": dict(state.__dict__.get("faction_shift") or {}),
            "rewards_paid": list(state.__dict__.get("encounter_rewards") or ())}


def restore(state, saved):
    """Set the encounters from ``saved`` (parameters()), quietly: attacks of templates this world has,
    standing changes as numbers, rewards paid. Nothing is told or paid again."""
    if not isinstance(saved, dict):
        return
    known = templates(state)
    attacks = []
    for entry in saved.get("attacks") or ():
        if isinstance(entry, dict) and entry.get("event") in known and isinstance(entry.get("turn"), int):
            attacks.append({"turn": entry["turn"], "event": entry["event"],
                            "category": entry.get("category") or known[entry["event"]][1].category,
                            "location": entry.get("location"), "faction": entry.get("faction"),
                            "npc": entry.get("npc"), "result": entry.get("result")})
    state.__dict__["dynamic_encounters"] = attacks
    state.__dict__["faction_shift"] = {str(name): float(value) for name, value in (saved.get("factions") or {}).items()
                                       if isinstance(value, (int, float)) and not isinstance(value, bool)}
    state.__dict__["encounter_rewards"] = [name for name in saved.get("rewards_paid") or () if name in known]


# ---- what a player and an author can see ----------------------------------------------------------

def at_turn(state, turn):
    """The dynamic encounter that started at turn ``turn`` (the journal's ``encounter``), or None."""
    for entry in state.__dict__.get("dynamic_encounters") or ():
        if entry.get("turn") == turn:
            return {key: entry.get(key) for key in ("event", "category", "faction", "npc", "result")}
    return None


@with_state_context
def explain(state):
    """Why an attack could or could not come now, for authors (the decision trace): {enabled, gate
    (a code or None), stage, attacks (this story's), candidates: [{event, category, status
    (eligible | its code), chance}]}. Reads only: no dice, no change."""
    cfg = settings()
    rows = []
    for event, spec in templates(state).values():
        block = _template_block(state, event, spec, cfg)
        rows.append({"event": event.name, "category": spec.category, "status": block or "eligible",
                     "chance": round(_chance(state, spec, cfg), 4) if block is None else 0.0})
    return {"enabled": bool(cfg["enabled"]), "gate": _gate(state, cfg), "stage": stage(state),
            "attacks": [dict(entry) for entry in state.__dict__.get("dynamic_encounters") or ()],
            "factions": {name: {"standing": standing(state, name), "hostile": hostile(state, name)}
                         for name in cfg["factions"]},
            "candidates": rows,
            "codes": {**CODES, **{code: meaning for code, meaning in side_story.HOLD_MEANINGS.items()
                                  if code not in CODES}}}


def spec_errors(raw, where, world=None):
    """Problems with an event's ``encounter`` block (world: the world_definition, for references)."""
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return [f"{where}: `encounter` must be a mapping."]
    errors = []
    unknown = sorted(set(raw) - set(SPEC_FIELDS))
    if unknown:
        errors.append(f"{where}: unknown encounter field(s) {', '.join(unknown)}; use {', '.join(SPEC_FIELDS)}.")
    if raw.get("category") not in CATEGORIES:
        errors.append(f"{where}: `encounter.category` must be one of {', '.join(CATEGORIES)}.")
    for field in ("tags_any", "tags_none", "stages"):
        value = raw.get(field)
        if value is not None and (not isinstance(value, list) or not all(isinstance(v, str) for v in value)):
            errors.append(f"{where}: `encounter.{field}` must be a list of names.")
    if any(stage_name not in STAGES for stage_name in raw.get("stages") or [] if isinstance(raw.get("stages"), list)):
        errors.append(f"{where}: `encounter.stages` takes {', '.join(STAGES)}.")
    for field in ("weight", "danger", "difficulty", "min_tension", "max_tension", "min_power"):
        value = raw.get(field)
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0):
            errors.append(f"{where}: `encounter.{field}` must be a number of 0 or more.")
    for field in ("min_turn", "min_alert", "max_alert", "cooldown"):
        value = raw.get(field)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            errors.append(f"{where}: `encounter.{field}` must be a whole number of 0 or more.")
    value = raw.get("max_per_story")
    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
        errors.append(f"{where}: `encounter.max_per_story` must be a whole number of 1 or more.")
    for field in ("hostile_only", "rewards_once"):
        if field in raw and not isinstance(raw[field], bool):
            errors.append(f"{where}: `encounter.{field}` must be true or false.")
    npcs = {entry.get("name") for entry in (world or {}).get("characters") or [] if isinstance(entry, dict)}
    if raw.get("npc") and world is not None and raw["npc"] not in npcs:
        errors.append(f"{where}: `encounter.npc` `{raw['npc']}` is not an NPC of this world.")
    quests = set((world or {}).get("side_quests") or {})
    for key in ("on_win", "on_loss", "on_escape"):
        block = raw.get(key)
        if block is None:
            continue
        if not isinstance(block, dict):
            errors.append(f"{where}: `encounter.{key}` must be a mapping of {', '.join(RESULT_FIELDS)}.")
            continue
        unknown = sorted(set(block) - set(RESULT_FIELDS))
        if unknown:
            errors.append(f"{where}: unknown field(s) {', '.join(unknown)} in `encounter.{key}`; "
                          f"use {', '.join(RESULT_FIELDS)}.")
        if block.get("side_quest") and world is not None and block["side_quest"] not in quests:
            errors.append(f"{where}: `encounter.{key}.side_quest` `{block['side_quest']}` is not a side quest.")
        standing_change = block.get("standing")
        if standing_change is not None and (not isinstance(standing_change, dict) or not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in standing_change.values())):
            errors.append(f"{where}: `encounter.{key}.standing` must map factions to numbers.")
        for field in ("text", "callback"):
            if block.get(field) is not None and not isinstance(block[field], str):
                errors.append(f"{where}: `encounter.{key}.{field}` must be text.")
    return errors
