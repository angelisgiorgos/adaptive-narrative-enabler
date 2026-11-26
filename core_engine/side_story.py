"""Side-story orchestration: how the director weaves optional content (side quests, dynamic hostile
encounters) into the main story, and how the training objective measures it (``side_story`` in
hyperparameters.yaml; the objective terms under ``fitness.side_story``, fitness version 2).

**The plan.** Before the story lets a new side opportunity in (a side quest offered or started, an
encounter the encounter director would start), the director looks at the whole story (``plan``):

- the main quest: its stage, how long since it last moved (``main_idle``), whether a climax waits
  or the finale has begun;
- the optional quests and open threads: side quests under way or on offer, open clues;
- the player's previous choices: side quests taken on, turned down (several in a row: the story
  offers only what bears on the quest, ``decline_backoff`` / ``backoff_relevance``);
- the current danger and recent encounters: tension, alert, how much of the last turns went to
  optional content (``max_side_share`` of ``share_window`` turns), the last introduction.

It holds new side content (a code: ``hold``) while an event is under way (a major encounter), a
climax waits, in the finale, while a clock runs (a side quest with ``deadline_turns`` turns left or
fewer), when the main quest has stood still for ``main_idle_turns`` turns (counted from
mission_settings.pursuit_start_step), when ``max_open_threads`` threads are open, when optional
content took more than ``max_side_share`` of the last turns, or sooner than ``intro_gap`` turns
after the last introduction.

**One candidate policy.** Side quests that could be offered and encounter templates that could start
are scored alike (``candidates``): how well they fit the story now (side quests: tags shared with
the place and the last turns, the mission's thread; encounters: their weight and category), plus
how much they bear on the main story (``relevance``, 0..1) x ``relevance_weight``, scaled by the
storyteller's learned appetite (its genome: ``side_quest_bias`` for side quests, ``threat_prob``
for attacks with ``encounter_appetite``). The best fitting is offered first; an attack is picked
by these weights. The storyteller's ranking pulls toward side quests under way less while the main
quest stands still (``idle_pull``) and in the climax and finale (``finale_pull``).

**Agency.** The director offers, the player decides: a side quest the story sets up (an encounter's
outcome: ``side_quest``) is always an offer the player may turn down (``agency``); nothing optional
is completed for the player (a side quest completes when its goal holds, or when the player hands
it over).

**Connections.** A side quest's ``texts.reveal`` is told when it is completed: what it turned up about
the main quest (with ``aids_mission: lead`` the way there opens too). An encounter's outcome can set
up a side quest (a rescue, an investigation: ``on_win`` / ``on_loss`` ``side_quest``). Helping
someone (``aids_mission: ally``) makes them an ally, who helps in checks and offers a hand in the
climax. Near the climax (when it is announced, or when the finale begins) the story calls back up
to ``max_callbacks`` resolved side stories: a side quest's ``texts.callback`` or an encounter
result's ``callback`` (``callbacks: authored``), or, with ``callbacks: all``, a few words for a
completed side quest that helped the mission.

**What the story keeps** (plain data: snapshots and save games carry it): ``main_progress_turns``
(turns the main quest moved), ``side_story_log`` [{turn, kind (offer | start | hook | encounter),
key, title, relevance, hold (the code the policy gave, even when it is off), interrupting}] and
``side_story_links`` [{turn, source (side_quest | encounter), key, what (reward | change | aid | ally
| reveal | callback | hook | standing | consequence | captured | follow_up | cost)}]: what each
optional thread led to.

**The objective** (fitness version 2: ``objective_terms``): every term is a share or a mean, never a
count, so neither more side content nor a longer story scores more by itself; ``docs/AI.md``
("Side-story objective terms") gives each one and the reason for its default weight. Nothing here
draws from the dice: a seeded story stays reproducible, and a world without side quests or encounter
templates tells the same story with or without it.
"""
from __future__ import annotations

import math

from utils.config_loader import config

from .context import with_state_context

DEFAULTS = {
    # Off when a configuration has no ``side_story`` section (one written before it: its stories
    # stay as they were); the built-in configuration switches it on.
    "enabled": False,
    "agency": True,
    "hold_in_event": True, "hold_in_climax": True, "hold_in_finale": True,
    "deadline_turns": 3,
    "main_idle_turns": 12,
    "max_open_threads": 4,
    "share_window": 10, "max_side_share": 0.5,
    "intro_gap": 2,
    "decline_backoff": 2, "backoff_relevance": 0.5,
    "relevance_weight": 2.0,
    "encounter_appetite": True,
    "idle_pull": 0.5, "finale_pull": 0.25,
    "callbacks": "all", "max_callbacks": 2,
    "meaningful_relevance": 0.5,
}
CALLBACKS = ("authored", "all", "none")
# Holds that break into the main story (counted as interruptions); the others keep it from crowding.
INTERRUPTS = ("event", "climax", "finale", "deadline", "main_idle")
HOLDS = INTERRUPTS + ("threads_full", "side_share", "gap", "declined")
HOLD_MEANINGS = {
    "event": "An event (a major encounter) is under way (side_story.hold_in_event).",
    "climax": "The climax waits (side_story.hold_in_climax).",
    "finale": "The finale has begun (side_story.hold_in_finale).",
    "deadline": "A clock runs: a side quest has side_story.deadline_turns turns left or fewer.",
    "main_idle": "The main quest has stood still for side_story.main_idle_turns turns.",
    "threads_full": "Enough threads are open (side_story.max_open_threads).",
    "side_share": "Optional content took more than side_story.max_side_share of the last turns.",
    "gap": "Too soon after the last side quest offered or attack begun (side_story.intro_gap).",
    "declined": "The player turned down several offers in a row: only what bears on the quest is offered "
                "(side_story.decline_backoff, backoff_relevance).",
    "not_yet": "It cannot be offered yet (its giver not met, its start condition, enabled_if, follows).",
    "eligible": "It could come now.",
}
# What an optional thread led to that the story goes on with (meaningful); reward: coins or health.
STORY_CONSEQUENCES = ("change", "aid", "ally", "reveal", "callback", "hook", "standing", "consequence", "captured",
                      "follow_up", "cost")
APPROACH_TAGS = ("combat", "stealth", "social", "persuasion", "trade", "escape", "discovery", "help", "threat",
                 "rest")
NUMBERS = ("deadline_turns", "main_idle_turns", "max_open_threads", "share_window", "max_side_share", "intro_gap",
           "decline_backoff", "backoff_relevance", "relevance_weight", "idle_pull", "finale_pull", "max_callbacks",
           "meaningful_relevance")


def settings():
    """The side-story policy (``side_story`` in the tuning), its defaults filled in."""
    own = config.get("side_story") or {}
    merged = {**DEFAULTS, **{key: value for key, value in own.items() if value is not None}}
    if merged["callbacks"] not in CALLBACKS:
        merged["callbacks"] = "all"
    return merged


def validate_settings(values):
    """Problems with a ``side_story`` section."""
    if not isinstance(values, dict):
        return ["`side_story` must be a mapping."]
    errors = []
    for key in ("enabled", "agency", "hold_in_event", "hold_in_climax", "hold_in_finale", "encounter_appetite"):
        if key in values and not isinstance(values[key], bool):
            errors.append(f"`side_story.{key}` must be true or false.")
    for key in NUMBERS:
        value = values.get(key)
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0):
            errors.append(f"`side_story.{key}` must be a number of 0 or more.")
    for key in ("max_side_share", "backoff_relevance", "idle_pull", "finale_pull", "meaningful_relevance"):
        value = values.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 1:
            errors.append(f"`side_story.{key}` must be between 0 and 1.")
    if values.get("callbacks") is not None and values["callbacks"] not in CALLBACKS:
        errors.append(f"`side_story.callbacks` must be one of {', '.join(CALLBACKS)}.")
    return errors


# ---- what the story is like now ----------------------------------------------------------------

def note_main_progress(state):
    """The main quest moved this turn (a stage done, the quest won)."""
    turns = state.__dict__.setdefault("main_progress_turns", [])
    if not turns or turns[-1] != state.actions_taken:
        turns.append(state.actions_taken)


def main_idle(state):
    """Turns since the main quest last moved, counted from mission_settings.pursuit_start_step (the
    opening is for finding one's feet); 0 without an open mission."""
    if not getattr(state, "mission", None) or state.won:
        return 0
    start = int(config.get("mission_settings.pursuit_start_step", 0) or 0)
    last = max(int(state.__dict__.get("last_quest_progress", 0) or 0), start)
    return max(0, state.actions_taken - last)


def open_threads(state):
    """{side_quests (under way or on offer), clues (open), count}."""
    from . import side_quests

    keys = [key for key, record in side_quests.records(state).items() if record.get("status") in ("active", "offered")]
    clues = len(state._open_clues()) if hasattr(state, "_open_clues") else 0
    return {"side_quests": keys, "clues": clues, "count": len(keys) + clues}


def _encounter_turns(state):
    turns = set()
    for record in state.__dict__.get("dynamic_encounters") or ():
        turns.add(int(record.get("turn", 0)))
    for entry in getattr(state, "event_log", None) or ():
        if entry.get("cause") == "encounter_director" and entry.get("ended_step") is not None:
            turns.add(int(entry["ended_step"]))
    return turns


def side_share(state, cfg=None):
    """The share of the last ``share_window`` turns that went to optional content (a side quest
    offered, started, advanced, done or failed; an attack begun or fought)."""
    cfg = cfg or settings()
    window = max(1, int(cfg["share_window"]))
    start = state.actions_taken - window
    turns = {int(e.get("turn", 0)) for e in state.__dict__.get("side_quest_events") or ()
             if e.get("what") in ("offered", "started", "stage", "ready", "completed", "failed")}
    turns |= _encounter_turns(state)
    recent = {turn for turn in turns if turn >= start}
    return len(recent) / window  # of the whole window: one offer on the first turn is not "all of it"


def declined_in_a_row(state):
    """Side quests turned down (or let lapse) since the player last took one on."""
    count = 0
    for entry in reversed(state.__dict__.get("side_quest_events") or ()):
        if entry.get("what") in ("declined", "lapsed"):
            count += 1
        elif entry.get("what") == "started":
            break
    return count


def _deadline_near(state, cfg):
    from . import side_quests

    for record in side_quests.records(state).values():
        if record.get("status") == "active" and record.get("deadline") is not None and \
                record["deadline"] - state.actions_taken <= int(cfg["deadline_turns"]):
            return True
    return False


def last_introduction(state):
    log = state.__dict__.get("side_story_log") or []
    return int(log[-1]["turn"]) if log else None


def hold(state, kind="offer", relevance=None, cfg=None, force=False, allow=()):
    """Why no new side content of ``kind`` (offer | encounter | hook) may come now (a code of
    HOLDS), or None. ``force``: what the policy would say even when it is off (for the record);
    ``allow``: holds the caller's own, more specific settings lift (encounters.in_finale: finale)."""
    cfg = cfg or settings()
    if not cfg["enabled"] and not force:
        return None
    if cfg["hold_in_event"] and state.active_event:
        return "event"
    if cfg["hold_in_climax"] and "climax" not in allow and (state.__dict__.get("climax_state") or {}).get("armed"):
        return "climax"
    if cfg["hold_in_finale"] and "finale" not in allow and state.in_finale():
        return "finale"
    if _deadline_near(state, cfg):
        return "deadline"
    if kind == "hook":  # a consequence of what just happened, not a new introduction: only those moments
        return None
    if int(cfg["main_idle_turns"]) > 0 and main_idle(state) >= int(cfg["main_idle_turns"]):
        return "main_idle"
    if open_threads(state)["count"] >= int(cfg["max_open_threads"]):
        return "threads_full"
    if float(cfg["max_side_share"]) < 1.0 and side_share(state, cfg) > float(cfg["max_side_share"]):
        return "side_share"
    last = last_introduction(state)
    if last is not None and state.actions_taken - last < int(cfg["intro_gap"]):
        return "gap"
    if kind == "offer" and relevance is not None and int(cfg["decline_backoff"]) and \
            declined_in_a_row(state) >= int(cfg["decline_backoff"]) and relevance < float(cfg["backoff_relevance"]):
        return "declined"
    return None


# ---- how much an opportunity bears on the main story -----------------------------------------

def quest_relevance(state, quest):
    """0..1: how much a side quest bears on the main story: it helps the mission (aids_mission lead
    or ally: 0.3), reveals something about it (texts.reveal: 0.3) or calls back near the climax
    (texts.callback: 0.1); it wants what the mission wants (0.3) or leads where the mission advances
    (0.2); its giver matters to the story (importance 2+: 0.1)."""
    from . import side_quests

    score = 0.0
    if side_quests.aid_mode(quest) in ("lead", "ally"):
        score += 0.3
    texts = getattr(quest, "texts", None) or {}
    if texts.get("reveal"):
        score += 0.3
    if texts.get("callback"):
        score += 0.1
    if getattr(state, "mission", None) and not state.won:
        goal = side_quests._stage_goal(quest, 0)[0] or {}
        mission_goal = state._pending_goal() if hasattr(state, "_pending_goal") else {}
        if any(mission_goal.get(key) == value for key, value in goal.items()):
            score += 0.3
        elif any(state._quest_pull(place) > 0 for place in side_quests._goal_places(goal)
                 if place in state.world.locations):
            score += 0.2
    if quest.giver and hasattr(state, "importance") and state.importance(quest.giver) >= 2:
        score += 0.1
    return round(min(1.0, score), 3)


def encounter_relevance(state, event, spec):
    """0..1: how much an attack bears on the main story: someone who matters is behind it (its NPC,
    importance 2+: 0.3); it can set up a side quest (a rescue, an investigation: 0.3), calls back
    near the climax (0.1) or changes a faction's standing or queues a consequence (0.1); it comes on
    the mission's way (the place pulls toward the mission: 0.2)."""
    score = 0.0
    if spec.npc and hasattr(state, "importance") and state.importance(spec.npc) >= 2:
        score += 0.3
    blocks = [block for block in spec.results.values() if block]
    if any(block.get("side_quest") for block in blocks):
        score += 0.3
    if any(block.get("callback") for block in blocks):
        score += 0.1
    if any(block.get("standing") or block.get("consequence") for block in blocks):
        score += 0.1
    if getattr(state, "mission", None) and not state.won and state._quest_pull(state.current.name) > 0:
        score += 0.2
    return round(min(1.0, score), 3)


def appetite(state, kind):
    """The storyteller's learned appetite for side content of ``kind``: its side_quest_bias for side
    quests; for attacks (encounter_appetite) 0.5 + its threat_prob; 1 otherwise."""
    if kind == "side_quest":
        return float(getattr(state.genome, "side_quest_bias", 0.6))
    if kind == "encounter" and settings()["encounter_appetite"]:
        return 0.5 + float(getattr(state.genome, "threat_prob", 0.5))
    return 1.0


def priority(state, quest, cfg=None):
    """A side quest's place among those that could be offered now: fit + relevance x
    relevance_weight (x the storyteller's appetite, which orders nothing among side quests but
    is reported)."""
    from . import side_quests

    cfg = cfg or settings()
    return side_quests.fit(state, quest) + quest_relevance(state, quest) * float(cfg["relevance_weight"])


def encounter_weight(state, event, spec):
    """x an attack's weight in the encounter director's pick (1 + relevance x relevance_weight)."""
    cfg = settings()
    if not cfg["enabled"]:
        return 1.0
    return 1.0 + encounter_relevance(state, event, spec) * float(cfg["relevance_weight"])


def encounter_chance_factor(state):
    """x the chance of an attack: the storyteller's appetite for attacks, when the policy is on."""
    cfg = settings()
    if not cfg["enabled"] or not cfg["encounter_appetite"]:
        return 1.0
    return appetite(state, "encounter")


def pull_factor(state):
    """x the pull of side quests under way in the storyteller's ranking: finale_pull in the climax and
    the finale, idle_pull while the main quest stands still, 1 otherwise (and when the policy is off)."""
    cfg = settings()
    if not cfg["enabled"]:
        return 1.0
    if (state.__dict__.get("climax_state") or {}).get("armed") or state.in_finale():
        return float(cfg["finale_pull"])
    if int(cfg["main_idle_turns"]) > 0 and main_idle(state) >= int(cfg["main_idle_turns"]):
        return float(cfg["idle_pull"])
    return 1.0


# ---- what happens: introductions, links, reveals and callbacks ------------------------------------

def note(state, kind, key, title=None, relevance=None):
    """Record a side-content introduction (an offer, a start, a hook, an attack) with what the policy
    says about the moment (``hold``, computed even when it is off) and whether it broke into the main
    story (``interrupting``)."""
    code = hold(state, "hook" if kind == "hook" else ("encounter" if kind == "encounter" else "offer"),
                force=True)
    state.__dict__.setdefault("side_story_log", []).append({
        "turn": state.actions_taken, "kind": kind, "key": key, "title": title,
        "relevance": relevance, "hold": code, "interrupting": code in INTERRUPTS})


def link(state, source, key, what, detail=None):
    """An optional thread led to something (``what``): STORY_CONSEQUENCES, or reward."""
    entry = {"turn": state.actions_taken, "source": source, "key": key, "what": what}
    if detail is not None:
        entry["detail"] = detail
    state.__dict__.setdefault("side_story_links", []).append(entry)


def effect_kind(effect_list):
    """``change`` when an effect list changes the story (a variable, an item, a flag, a place),
    ``reward`` when only coins or health, None when it is empty."""
    writes = getattr(effect_list, "writes", None) or frozenset()
    if not writes:
        return None
    return "reward" if all(kind in ("resource", "coins_gain") for kind, _ in writes) else "change"


def completed(state, quest, aided):
    """A side quest was completed: what it led to, and its reveal (texts.reveal) is told."""
    kind = effect_kind(getattr(quest, "rewards", None))
    if kind:
        link(state, "side_quest", quest.name, kind)
    if aided:
        link(state, "side_quest", quest.name, "ally" if aided == "ally" else "aid")
    text = (getattr(quest, "texts", None) or {}).get("reveal")
    if text:
        from . import side_quests

        line = side_quests.format_text(state, quest, text)
        if line:
            state._say("side_story_reveal", title=quest.title, text=line)
            link(state, "side_quest", quest.name, "reveal")


def failed(state, quest):
    if effect_kind(getattr(quest, "on_fail", None)) == "change":
        link(state, "side_quest", quest.name, "cost")


def callbacks(state, moment):
    """Near the climax (``moment``: climax | finale), once a story: call back up to max_callbacks
    resolved side stories. Returns how many were told."""
    cfg = settings()
    if not cfg["enabled"] or cfg["callbacks"] == "none" or state.__dict__.get("side_story_called_back"):
        return 0
    from . import side_quests

    found = []  # (authored first, -relevance, turn, line, source, key)
    for key, record in side_quests.records(state).items():
        quest = side_quests.quests(state).get(key)
        if quest is None or record.get("status") != "completed":
            continue
        own = (getattr(quest, "texts", None) or {}).get("callback")
        if own:
            line = side_quests.format_text(state, quest, own)
            if line:
                found.append((0, -quest_relevance(state, quest), record.get("ended") or 0,
                              state.narrate("side_story_callback", title=quest.title, text=line), "side_quest", key))
        elif cfg["callbacks"] == "all" and record.get("aided"):
            message = "side_story_callback_ally" if record["aided"] == "ally" else "side_story_callback_lead"
            found.append((1, -quest_relevance(state, quest), record.get("ended") or 0,
                          state.narrate(message, title=quest.title,
                                        giver=state._called(quest.giver) if quest.giver else ""), "side_quest", key))
    for record in state.__dict__.get("dynamic_encounters") or ():
        if record.get("callback") and record.get("result"):
            event = state.world.events.get(record["event"])
            found.append((0, 0.0, record.get("turn") or 0,
                          state.narrate("side_story_callback", title=event.title if event else record["event"],
                                        text=record["callback"]), "encounter", f"{record['event']}@{record.get('turn')}"))
    if not found:
        return 0
    state.__dict__["side_story_called_back"] = moment
    told = 0
    for _, _, _, line, source, key in sorted(found)[:max(0, int(cfg["max_callbacks"]))]:
        state.history.append(line)
        link(state, source, key, "callback")
        told += 1
    return told


# ---- the unified view (decision trace) ------------------------------------------------------------

@with_state_context
def candidates(state):
    """Every side opportunity of the world as the director weighs it now: [{kind (side_quest |
    encounter), key, title, eligible, status, relevance, fit, appetite, score}]. Reads only."""
    from . import encounters, side_quests

    cfg = settings()
    rows = []
    for quest in side_quests.quests(state).values():
        if quest.name in side_quests.records(state):
            continue
        eligible = side_quests.offered(state, quest)
        relevance = quest_relevance(state, quest)
        fit = side_quests.fit(state, quest)
        rows.append({"kind": "side_quest", "key": quest.name, "title": quest.title, "eligible": eligible,
                     "status": (hold(state, "offer", relevance, cfg) or "eligible") if eligible else "not_yet",
                     "relevance": relevance, "fit": round(fit, 3), "appetite": round(appetite(state, "side_quest"), 3),
                     "score": round(fit + relevance * float(cfg["relevance_weight"]), 3)})
    enc_cfg = encounters.settings()
    for name, (event, spec) in encounters.templates(state).items():
        block = encounters._template_block(state, event, spec, enc_cfg)
        relevance = encounter_relevance(state, event, spec)
        rows.append({"kind": "encounter", "key": name, "title": event.title, "eligible": block is None,
                     "status": block or hold(state, "encounter", cfg=cfg) or "eligible", "relevance": relevance,
                     "fit": round(spec.weight * float(enc_cfg["categories"][spec.category]["weight"]), 3),
                     "appetite": round(appetite(state, "encounter"), 3),
                     "score": round(spec.weight * (1.0 + relevance * float(cfg["relevance_weight"])), 3)})
    return rows


@with_state_context
def plan(state):
    """What the director weighs before letting side content in (the decision trace's ``side_story``):
    {enabled, hold, main {mission, stage, idle, climax, finale}, threads, player {taken, declined,
    declined_in_a_row}, danger {tension, alert, health, recent_encounters, side_share}, pull,
    candidates, introductions (the last five), links (the last five)}. Reads only: no dice, no change."""
    from . import side_quests

    cfg = settings()
    events = state.__dict__.get("side_quest_events") or []
    stakes = state.stakes() if hasattr(state, "stakes") else None
    window = max(1, int(cfg["share_window"]))
    recent = [r for r in state.__dict__.get("dynamic_encounters") or ()
              if state.actions_taken - int(r.get("turn", 0)) <= window]
    return {
        "enabled": bool(cfg["enabled"]),
        "hold": hold(state, "offer", cfg=cfg, force=True),
        "main": {"mission": state.mission.title if getattr(state, "mission", None) else None,
                 "stage": int(getattr(state, "mission_stage", 0) or 0), "won": bool(state.won),
                 "idle": main_idle(state),
                 "climax": bool((state.__dict__.get("climax_state") or {}).get("armed")),
                 "finale": bool(state.in_finale())},
        "threads": open_threads(state),
        "player": {"taken": sum(1 for e in events if e.get("what") == "started"),
                   "declined": sum(1 for e in events if e.get("what") in ("declined", "lapsed")),
                   "declined_in_a_row": declined_in_a_row(state)},
        "danger": {"tension": round(float(state.tension()), 3), "alert": stakes["alert"] if stakes else 0,
                   "health": state.health, "recent_encounters": len(recent),
                   "side_share": round(side_share(state, cfg), 3)},
        "pull": pull_factor(state),
        "candidates": candidates(state),
        "introductions": [dict(entry) for entry in (state.__dict__.get("side_story_log") or [])[-5:]],
        "links": [dict(entry) for entry in (state.__dict__.get("side_story_links") or [])[-5:]],
        "summary": {"side_quests": side_quests.summary(state)},
        "codes": dict(HOLD_MEANINGS),
    }


# ---- what it amounted to: meaningful or filler -------------------------------------------------------

@with_state_context
def classify(state, cfg=None):
    """Every optional thread of the story, labelled: [{kind (side_quest | encounter), key, title,
    status, relevance, consequences, engaged (taken on or fought), label}]. label: meaningful (it led
    to something the story goes on with, STORY_CONSEQUENCES, or bears on the main story:
    relevance >= meaningful_relevance), filler (resolved, and neither), open (still under way or on
    offer), declined (the player's choice: neither good nor bad)."""
    from . import side_quests

    cfg = cfg or settings()
    threshold = float(cfg["meaningful_relevance"])
    introduced = {}
    for entry in state.__dict__.get("side_story_log") or ():
        introduced.setdefault((entry["kind"] == "encounter", entry["key"], entry["turn"] if entry["kind"] == "encounter"
                               else None), entry)
    led = {}
    for entry in state.__dict__.get("side_story_links") or ():
        led.setdefault((entry["source"], entry["key"]), set()).add(entry["what"])
    items = []
    for key, record in side_quests.records(state).items():
        quest = side_quests.quests(state).get(key)
        if quest is None:
            continue
        status = record.get("status")
        noted = introduced.get((False, key, None))
        relevance = noted["relevance"] if noted and noted.get("relevance") is not None else quest_relevance(state, quest)
        consequences = sorted(led.get(("side_quest", key), set()))
        if status in ("offered", "declined"):
            label, engaged = ("declined" if status == "declined" else "open"), False
        elif status == "active":
            label, engaged = "open", True
        else:
            meaningful = set(consequences) & set(STORY_CONSEQUENCES) or relevance >= threshold
            label, engaged = ("meaningful" if meaningful else "filler"), True
        items.append({"kind": "side_quest", "key": key, "title": quest.title, "status": status,
                      "relevance": relevance, "consequences": consequences, "engaged": engaged, "label": label})
    for record in state.__dict__.get("dynamic_encounters") or ():
        event = state.world.events.get(record.get("event"))
        noted = introduced.get((True, record.get("event"), record.get("turn")))
        relevance = (noted or {}).get("relevance")
        if relevance is None:
            relevance = encounter_relevance(state, event, event.encounter) if event is not None and \
                getattr(event, "encounter", None) else 0.0
        consequences = sorted(led.get(("encounter", f"{record.get('event')}@{record.get('turn')}"), set()))
        if record.get("result") is None:
            label = "open"
        else:
            meaningful = set(consequences) & set(STORY_CONSEQUENCES) or relevance >= threshold
            label = "meaningful" if meaningful else "filler"
        items.append({"kind": "encounter", "key": record.get("event"), "title": event.title if event else None,
                      "status": record.get("result") or "under_way", "relevance": relevance,
                      "consequences": consequences, "engaged": True, "label": label, "turn": record.get("turn")})
    return items


def decisions(state):
    """The meaningful decisions the player made, each as the approach it took: a check's stat, else
    the kind of choice and its tag (combat, stealth, social, ...); taking a side quest on or turning
    it down. Travel, small talk and turns with nothing else to choose are not counted."""
    labels = []
    for turn in getattr(state, "turn_log", None) or ():
        if turn.get("source") in ("navigation", "conversation", None) or not turn.get("options"):
            continue
        check = turn.get("check") or {}
        if isinstance(check, dict) and check.get("stat"):
            labels.append(f"check:{check['stat']}")
            continue
        tags = {str(tag).lower() for tag in turn.get("tags") or ()} & set(APPROACH_TAGS)
        labels.append(f"{turn.get('source')}:{min(tags) if tags else 'other'}")
    offered = set()
    for entry in state.__dict__.get("side_quest_events") or ():
        if entry.get("what") == "offered":
            offered.add(entry.get("quest"))
        elif entry.get("what") == "started" and entry.get("quest") in offered:
            labels.append("side_quest:accept")
        elif entry.get("what") == "declined":
            labels.append("side_quest:decline")
    return labels


def _drift(state):
    """The longest stretch of turns without main-quest progress (from the start, between steps, to
    the win or the last turn), or None without a mission."""
    if not getattr(state, "mission", None):
        return None
    marks = sorted(set(state.__dict__.get("main_progress_turns") or ()))
    if not marks and state.__dict__.get("last_quest_progress"):  # a story saved before the list
        marks = [int(state.last_quest_progress)]
    end = marks[-1] if state.won and marks else state.actions_taken
    points = [0] + [turn for turn in marks if turn <= end] + [end]
    return max(b - a for a, b in zip(points, points[1:]))


@with_state_context
def objective_terms(state, cfg):
    """The side-story terms of a story's fitness (fitness version 2; ``cfg``: fitness.side_story).
    Every key is always there (0 when it does not apply), so stories can be compared term by term."""
    def weight(key, default):
        return float(cfg.get(key, default))

    terms = {key: 0.0 for key in ("side_relevance", "side_payoff", "side_completion", "side_filler",
                                  "side_unresolved", "encounter_repetition", "side_interruptions", "main_drift",
                                  "decision_diversity")}
    if not state.actions_taken:
        return terms
    items = classify(state)
    engaged = [item for item in items if item["engaged"]]
    if engaged:
        terms["side_relevance"] = weight("relevance_weight", 8.0) * sum(i["relevance"] for i in engaged) / len(engaged)
        resolved = [item for item in engaged if item["label"] in ("meaningful", "filler")]
        if resolved:
            meaningful = sum(1 for item in resolved if item["label"] == "meaningful")
            terms["side_payoff"] = weight("payoff_weight", 12.0) * meaningful / len(resolved)
            terms["side_filler"] = -weight("filler_penalty", 6.0) * (len(resolved) - meaningful) / len(resolved)
        taken = [item for item in engaged if item["kind"] == "side_quest"]
        if taken:
            terms["side_completion"] = weight("completion_weight", 6.0) * sum(
                1 for item in taken if item["status"] == "completed") / len(taken)
        if state.ended and state.end_kind not in (None, "death"):
            terms["side_unresolved"] = -weight("unresolved_penalty", 6.0) * sum(
                1 for item in engaged if item["label"] == "open") / len(engaged)
    attacks = list(state.__dict__.get("dynamic_encounters") or ())
    if attacks:
        repeats = len(attacks) - len({record.get("event") for record in attacks})
        density = len(attacks) / max(1, state.actions_taken)
        terms["encounter_repetition"] = -(weight("repeat_penalty", 10.0) * repeats / len(attacks)
                                          + weight("density_penalty", 40.0) * max(0.0, density - weight("density_target", 0.15)))
    introductions = state.__dict__.get("side_story_log") or []
    if introductions:
        terms["side_interruptions"] = -weight("interruption_penalty", 8.0) * sum(
            1 for entry in introductions if entry.get("interrupting")) / len(introductions)
    longest = _drift(state)
    if longest is not None:
        terms["main_drift"] = -weight("drift_penalty", 20.0) * max(0, longest - int(weight("drift_allowed", 20))) / max(
            1, state.actions_taken)
    labels = decisions(state)
    if labels:
        counts = {}
        for label in labels:
            counts[label] = counts.get(label, 0) + 1
        entropy = -sum((n / len(labels)) * math.log(n / len(labels)) for n in counts.values())
        kinds = max(2, int(weight("diversity_kinds", 12)))
        terms["decision_diversity"] = weight("diversity_weight", 6.0) * min(1.0, entropy / math.log(kinds)) * min(
            1.0, len(labels) / max(1.0, weight("diversity_min_decisions", 4)))
    return terms
