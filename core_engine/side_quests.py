"""Side quests: optional quests besides the story's mission (``world_definition.side_quests``).

A side quest is written like a mission (title, description, ``goal`` or ``stages``, ``rewards``,
``enabled_if``; core_engine/world.py ``SideQuest``), plus who offers it, how it starts, ends and
fails, and how it is told::

    side_quests:
      a_gift_for_the_chapel:
        title: A gift for the chapel
        description: The Priest's cellar is empty before the feast.
        giver: Priest                # offered once the player has dealt with them (talked_to)
        start: {visited: Market}     # and/or any condition that must hold for it to be offered
        follows: charting_the_storm  # offered only once that side quest is completed
        enabled_if: {reputation: {gte: 0}}   # offered only while this holds
        goal: {item: Vintage Wine}   # or stages: [{title, description, goal, rewards}, ...]
        turn_in: true                # done, it is taken back to the giver to be completed
        within_turns: 12             # it fails when it is not completed this many turns after it started
        rewards: [{change_coins: 6}]
        fail_if: {npc_removed: Priest}       # fails when this holds while it is under way
        on_fail: [{change_variable: {reputation: -1}}]
        on_decline: [{change_variable: {reputation: -1}}]   # turned down, or the offer let go
        repeatable: false            # true: offered again in the profile's later stories once completed
        accept: choice               # choice: the player takes it on or turns it down; auto: it starts at once
        aids_mission: lead           # what completing it does for the mission: lead | ally | none
        texts:                       # the story's own words for its moments (else the messages)
          offer: The Priest wrings his hands: "Not a drop left for the feast..."
          complete: The Priest beams at the bottle. "{giver} will not forget this."

Unlike the mission, which ends the story when it is complete, any number of side quests run at
once (``side_quest_settings.max_active`` under way or on offer at most), and none of them ends the
story. Each one is offered once per story; a completed one is not offered again to the same profile
unless it is ``repeatable``.

**Offers.** With ``accept: choice`` (side_quest_settings.accept, the default) a side quest that can
start is put to the player, with what is in it for them (its rewards in words), whether it helps the
mission and how long it may take: the choices "Take on: <title>" and "Turn down: <title>" come
first (ids ``side-quest:<quest>:accept`` / ``:decline``; no turn passes). An offer not answered
within ``side_quest_settings.offer_turns`` turns (turns an event hides it do not count) lapses, like
one turned down (``on_decline``). With ``accept: auto`` it starts at once.

**Under way.** Its steps are done in order (each one's rewards), and the story tells the next one.
Quiet for ``side_quest_settings.remind_after`` turns, the story reminds the player what it still
needs. With ``within_turns`` the story warns when two turns are left, and the quest fails when time
is up. With ``turn_in`` the quest, once its goal holds, is taken back to the giver: the choice "Hand
over: <title>" comes first wherever the giver is in the scene (dealing with them in any other way
hands it over too); it fails if the giver leaves the story.

**The mission.** Completing a side quest helps the story's mission (``aids_mission``,
side_quest_settings.aids_mission): ``lead`` opens the way toward where the mission advances next;
``ally`` makes its giver an ally, who leads and helps (npc_goals); ``none`` does nothing for it.

**Training and the director.** A story's fitness counts its side quests (``fitness.side_quests``:
completed, steps done, those that helped the mission, failed), every side-quest moment is a
development (fitness rhythm: ``side_quest``; the director's sense of something new), and the
storyteller's ranking pulls toward a side quest's next step, harder as its time runs out
(``side_quest_settings.rank_weight`` x the storyteller's own learned ``side_quest_bias``, a genome
trait apart from the mission's ``goal_bias``), so training favours storytellers whose stories see
side quests through.

They are checked at the end of every turn (and after the UNP sets the player or reports what
happened: ``resume.apply_player`` / ``resume.sync``), never in the middle of one, in the order they
are written: first the ones under way and on offer, then new ones are offered or start. Nothing
here draws from the dice, so a seeded story stays reproducible.

What a story knows about them is plain data, so snapshots and save games carry it unchanged:

- ``state.side_quests``: {key: {status (offered | active | completed | failed | declined), stage,
  offered, started, ended, ready, deadline, progress, reminded, hurried, waited, aided, reason}}
  (turns; ``ready``: done, waiting to be handed over; ``aided``: lead | ally when it helped the
  mission; ``reason``: why it failed: condition | time | giver_gone);
- ``state.side_quest_events``: [{turn, quest, title, what (offered | started | declined | lapsed |
  stage | ready | reminded | hurry | completed | failed), stage (the title of the stage done),
  giver}], the journal's source;
- ``state.side_quests_done_before``: the side quests the profile completed in earlier stories.

Conditions can ask about them (``side_quest_active: <key>``, ``side_quest_done: <key>``), so a
mission, an event, a dialogue line or another side quest can follow from one. The player's choices
keep one option that works toward a side quest under way, or leads to someone who offers one
(``side_quest_settings.guide``; ``priority_side_quest`` in the decision trace).
"""
from __future__ import annotations

import string

from utils.config_loader import config

from . import side_story

STATUSES = ("offered", "active", "completed", "failed", "declined")
ACCEPT = ("choice", "auto")
AIDS = ("lead", "ally", "none")
# The moments a side quest may tell in its own words (``texts``), and the values they may use.
TEXTS = ("offer", "accept", "decline", "ready", "turn_in", "complete", "fail", "remind",
         # side_story: what completing it turned up about the main quest; its callback near the climax
         "reveal", "callback")
TEXT_FIELDS = ("title", "giver", "reward", "todo", "turns")
# Journal events that bring something new (the director's rhythm, fitness developments).
MOMENTS = ("offered", "started", "stage", "ready", "completed", "failed", "reacted", "withdrawn")


IMPOSSIBLE = ("fail", "keep")


def settings():
    """side_quest_settings in the tuning: {enabled, max_active, guide, accept, offer_turns,
    aids_mission, rank_weight, remind_after} and when the story lets a new side quest in: offer_after
    (not in the opening turns), offer_cooldown (turns between two new ones), offer_max_tension (not in
    a tenser moment: GameState.tension), finale_offers (in the finale or not); how continuity is kept:
    impossible (fail | keep: a side quest that can no longer be done), pause_when_held (a deadline does
    not run while the player is held), epilogue (the ending recalls them), fit (the one that fits the
    story best is offered first)."""
    cfg = config.get("side_quest_settings") or {}
    accept = str(cfg.get("accept", "choice"))
    aids = str(cfg.get("aids_mission", "lead"))
    impossible = str(cfg.get("impossible", "fail"))
    return {"enabled": bool(cfg.get("enabled", True)), "max_active": max(0, int(cfg.get("max_active", 3))),
            "guide": bool(cfg.get("guide", True)), "accept": accept if accept in ACCEPT else "choice",
            "offer_turns": max(1, int(cfg.get("offer_turns", 3))), "aids_mission": aids if aids in AIDS else "lead",
            "rank_weight": float(cfg.get("rank_weight", 2.0)),
            "remind_after": max(0, int(cfg.get("remind_after", 6))),
            "offer_after": max(0, int(cfg.get("offer_after", 1))),
            "offer_cooldown": max(0, int(cfg.get("offer_cooldown", 1))),
            "offer_max_tension": float(cfg.get("offer_max_tension", 0.75)),
            "finale_offers": bool(cfg.get("finale_offers", False)),
            "impossible": impossible if impossible in IMPOSSIBLE else "fail",
            "pause_when_held": bool(cfg.get("pause_when_held", True)),
            "epilogue": bool(cfg.get("epilogue", True)),
            "fit": bool(cfg.get("fit", True))}


def accept_mode(quest):
    """How the quest starts: ``choice`` (the player takes it on or turns it down) or ``auto``."""
    return quest.accept if getattr(quest, "accept", None) in ACCEPT else settings()["accept"]


def aid_mode(quest):
    """How completing it helps the mission: ``lead``, ``ally`` or ``none``."""
    return quest.aids_mission if getattr(quest, "aids_mission", None) in AIDS else settings()["aids_mission"]


def quests(state):
    """The world's side quests, in the order they are written ({key: SideQuest})."""
    return getattr(state.world, "side_quests", None) or {}


def records(state):
    return state.__dict__.setdefault("side_quests", {})


def _log(state, quest, what, stage=None):
    state.__dict__.setdefault("side_quest_events", []).append({
        "turn": state.actions_taken, "quest": quest.name, "title": quest.title, "what": what, "stage": stage,
        "giver": quest.giver})


def _stage_goal(quest, stage):
    """(goal, goal conditions) of the step ``stage`` (the goal of a quest without stages)."""
    if quest.stages:
        step = quest.stages[min(stage, len(quest.stages) - 1)]
        return step["goal"], step["goal_conditions"]
    return quest.goal, quest.goal_conditions


def pending(state, key):
    """The goal keys of the side quest ``key``'s current step that do not hold yet ({} when it is
    not under way, or done and waiting to be handed over)."""
    record = records(state).get(key)
    quest = quests(state).get(key)
    if quest is None or not record or record.get("status") != "active" or record.get("ready"):
        return {}
    goal, goal_conditions = _stage_goal(quest, int(record.get("stage", 0)))
    return {k: v for k, v in goal.items() if not goal_conditions[k].holds(state.facts)}


def _goal_met(state, quest, stage):
    return all(condition.holds(state.facts) for condition in _stage_goal(quest, stage)[1].values())


def _done_before(state, key):
    return key in (state.__dict__.get("side_quests_done_before") or ())


def offered(state, quest):
    """Whether ``quest`` can be offered (or start) now: not in this story yet, not completed by the
    profile before (unless repeatable), switched on (enabled_if), the side quest it follows
    completed, its giver dealt with, its start condition holding."""
    if quest.name in records(state):
        return False
    if not quest.repeatable and _done_before(state, quest.name):
        return False
    if not state.is_enabled(quest) or not _follows_done(state, quest):
        return False
    if quest.giver and (state.character_interactions.get(quest.giver, 0) <= 0
                        or quest.giver in state.removed_npcs):
        return False
    return quest.start is None or quest.start.holds(state.facts)


def hook(state, key):
    """The story sets a side quest up (an encounter's outcome: core_engine/encounters.py): it is offered
    (accept: choice) or starts now, whoever gives it and whatever its start condition; not when it is
    unknown, already in this story, completed by the profile before (unless repeatable) or switched
    off (enabled_if). Returns whether it was set up."""
    quest = quests(state).get(key)
    if quest is None or quest.name in records(state) or not settings()["enabled"]:
        return False
    if (not quest.repeatable and _done_before(state, quest.name)) or not state.is_enabled(quest):
        return False
    story = side_story.settings()
    side_story.note(state, "hook", quest.name, quest.title, side_story.quest_relevance(state, quest))
    # The director offers, the player decides (side_story.agency): what the story sets up is an offer.
    if accept_mode(quest) == "choice" or (story["enabled"] and story["agency"]):
        _offer(state, quest)
    else:
        _start(state, quest)
        _progress(state, quest)
    return True


def _follows_done(state, quest):
    follows = getattr(quest, "follows", None)
    if not follows:
        return True
    return (records(state).get(follows) or {}).get("status") == "completed" or _done_before(state, follows)


def offered_by(state, npc):
    """The titles of the side quests ``npc`` offers now: offered but for dealing with them."""
    titles = []
    for quest in quests(state).values():
        if quest.giver != npc or quest.name in records(state):
            continue
        if not quest.repeatable and _done_before(state, quest.name):
            continue
        if state.is_enabled(quest) and _follows_done(state, quest) and (
                quest.start is None or quest.start.holds(state.facts)):
            titles.append(quest.title)
    return titles


# ---- the story's say: when a new one may come, which fits, what the story does to them -----------

def fit(state, quest):
    """How well ``quest`` fits the story now: its ``tags`` shared with the place (1 each) and with what
    the last turns brought (0.5 each); the mission's thread (2: a place it names is where the mission
    advances next, or it wants what the mission wants). Higher: offered first."""
    score = 0.0
    tags = set(getattr(quest, "tags", None) or ())
    if tags:
        here = {str(tag).lower() for tag in getattr(state.current, "tags", None) or ()}
        recent = {str(tag).lower() for step in (getattr(state, "tags_seen", None) or [])[-3:]
                  for tag in (step if isinstance(step, (list, tuple, set)) else [step])}
        score += len(tags & here) + 0.5 * len(tags & recent)
    if state.mission and not state.won:
        goal = _stage_goal(quest, 0)[0]
        mission_goal = state._pending_goal() if hasattr(state, "_pending_goal") else {}
        if any(mission_goal.get(key) == value for key, value in goal.items()):
            score += 2.0
        elif any(state._quest_pull(place) > 0 for place in _goal_places(goal) if place in state.world.locations):
            score += 1.0
    return score


def story_allows_new(state, cfg=None):
    """Whether the story lets a new side quest in now: past the opening (offer_after), not right after
    another (offer_cooldown), no event in progress, not a tense moment (offer_max_tension), not the
    finale (unless finale_offers)."""
    cfg = cfg or settings()
    if state.actions_taken < cfg["offer_after"] or state.active_event:
        return False
    last = state.__dict__.get("_side_quest_last_new")
    if last is not None and state.actions_taken - last < cfg["offer_cooldown"]:
        return False
    if cfg["offer_max_tension"] < 1.0 and state.tension() > cfg["offer_max_tension"]:
        return False
    return cfg["finale_offers"] or not state.in_finale()


def _react(state, quest, record):
    """The story's reactions to the quest (``reactions``): each whose ``when`` holds while the quest is
    in its state (``while``) happens once: its text, its effects, then what it does to the quest."""
    for number, reaction in enumerate(getattr(quest, "reactions", None) or ()):
        if number in (record.get("reacted") or ()):
            continue
        status = record.get("status")
        wanted = reaction["while"]
        if status not in ("active", "offered") or (wanted != "any" and wanted != status):
            continue
        if not reaction["when"].holds(state.facts):
            continue
        record.setdefault("reacted", []).append(number)
        _log(state, quest, "reacted", stage=reaction["then"])
        if reaction.get("text"):
            line = string.Formatter().vformat(str(reaction["text"]), (), _Blank(_values(state, quest)))
            if line.strip():
                state.history.append(line)
        state._run_effects(reaction["effects"])
        then = reaction["then"]
        if then == "withdraw" or (status == "offered" and then in ("fail", "complete")):
            record.update(status="declined", ended=state.actions_taken, reason="story")
            _log(state, quest, "withdrawn")
            return "stop"
        if then == "fail":
            _finish(state, quest, "failed", "story")
            return "stop"
        if then == "complete" and status == "active":
            _finish(state, quest, "completed")
            return "stop"
        if then == "advance" and status == "active":
            _advance(state, quest, record)
            if record.get("status") != "active":
                return "stop"
    return None


def _advance(state, quest, record):
    """The story moves the quest one step on (a reaction): to its next step, else it is done."""
    stages = quest.stages
    if stages and record["stage"] < len(stages) - 1:
        done = stages[record["stage"]]
        record["stage"] += 1
        record["progress"] = state.actions_taken
        following = stages[record["stage"]]
        _log(state, quest, "stage", stage=done["title"])
        state._run_effects(done["rewards"])
        _say(state, "side_quest_stage", title=quest.title, done=done["title"], next=following["title"],
             description=following["description"])
        return
    if getattr(quest, "turn_in", False) and quest.giver:
        record.update(ready=True, progress=state.actions_taken,
                      ready_talks=state.character_interactions.get(quest.giver, 0))
        _log(state, quest, "ready")
        _tell(state, quest, "ready", "side_quest_ready", title=quest.title, giver=state._called(quest.giver))
        return
    _finish(state, quest, "completed")


def _impossible(state, quest, record):
    """Why the quest can no longer be done, in words (None: it can): its giver gone from the story, or
    a unique item its current step needs gone for good."""
    if quest.giver and quest.giver in state.removed_npcs:
        return "giver_gone", None
    for key, value in pending(state, quest.name).items():
        if key == "item" and value not in state.inventory and hasattr(state, "_item_obtainable") \
                and value in (state.__dict__.get("item_instances") or {}) and not state._item_obtainable(value):
            return "item_gone", value
    return None


def close(state):
    """The story ends: the epilogue recalls the side quests (side_quest_settings.epilogue): those
    completed, failed, turned down and left unfinished."""
    if not settings()["epilogue"] or not records(state):
        return
    groups = {"completed": [], "failed": [], "unfinished": []}
    for key, record in records(state).items():
        quest = quests(state).get(key)
        if quest is None:
            continue
        status = record.get("status")
        if status in ("completed", "failed"):
            groups[status].append(quest.title)
        elif status == "active":
            groups["unfinished"].append(quest.title)
    parts = []
    for status, words in (("completed", "saw {} through"), ("failed", "failed at {}"),
                          ("unfinished", "left {} unfinished")):
        if groups[status]:
            titles = groups[status]
            listed = titles[0] if len(titles) == 1 else ", ".join(titles[:-1]) + " and " + titles[-1]
            parts.append(words.format(listed))
    if parts:
        _say(state, "side_quest_epilogue", summary="; ".join(parts))


# ---- telling it ---------------------------------------------------------------------------------

def reward_words(state, effect_list):
    """The rewards of an effect list in a few words: "6 coins, the Ancient Map, reputation +1"
    (what a player can tell from it; other effects are left out)."""
    words = []
    for kind, argument in getattr(effect_list, "items", None) or ():
        if kind == "change_coins" and isinstance(argument, (int, float)) and argument > 0:
            words.append(f"{argument:g} coins")
        elif kind == "change_health" and isinstance(argument, (int, float)) and argument > 0:
            words.append(f"{argument:g} health")
        elif kind == "give_item":
            words += [state._called(name) for name in (argument if isinstance(argument, tuple) else (argument,))]
        elif kind == "change_variable" and isinstance(argument, tuple):
            words += [f"{name} {value:+g}" if isinstance(value, (int, float)) and not isinstance(value, bool)
                      else str(name) for name, value in argument]
        elif kind == "reveal_location":
            words.append(f"the way to the {argument}")
    return ", ".join(words)


class _Blank(dict):
    def __missing__(self, key):
        return ""


def _values(state, quest, **more):
    record = records(state).get(quest.name) or {}
    deadline = record.get("deadline")
    values = {"title": quest.title, "giver": state._called(quest.giver) if quest.giver else "",
              "reward": reward_words(state, quest.rewards),
              "todo": "; ".join(state._goal_words(k, v) for k, v in pending(state, quest.name).items()),
              "turns": max(0, deadline - state.actions_taken) if deadline is not None else ""}
    values.update(more)
    return values


def format_text(state, quest, text, **values):
    """One of the quest's own texts with its values ({title}, {giver}, ...) filled in."""
    return string.Formatter().vformat(str(text), (), _Blank(_values(state, quest, **values))).strip()


def _tell(state, quest, moment, message, **values):
    """Tell a moment of the quest: its own words (``texts``) when it has them, else the message."""
    own = (getattr(quest, "texts", None) or {}).get(moment)
    if own:
        line = format_text(state, quest, own, **values)
        if line:
            state.history.append(line)
        return
    if message:
        state._say(message, **values)


def _announce(state, quest):
    """The quest as it is put to the player: its giver asks (or what the player learns tells it),
    then what is in it for them, whether it helps the mission, and how long it may take."""
    description = quest.stages[0]["title"] if quest.stages else quest.description
    description = description or quest.description or "; ".join(  # nothing written: what it needs, in words
        state._goal_words(key, value) for key, value in _stage_goal(quest, 0)[0].items())
    if quest.giver:
        _tell(state, quest, "offer", "side_quest_given", giver=state._called(quest.giver), title=quest.title,
              description=description)
    else:
        _tell(state, quest, "offer", "side_quest_start", title=quest.title, description=description)
    reward = reward_words(state, quest.rewards)
    if reward:
        state._say("side_quest_stakes", title=quest.title, reward=reward)
    if aid_mode(quest) != "none" and state.mission and not state.won:
        state._say("side_quest_helps", title=quest.title, mission=state.mission.title)
    if getattr(quest, "within_turns", None):
        state._say("side_quest_deadline", title=quest.title, turns=quest.within_turns)


# ---- its course ---------------------------------------------------------------------------------

def _start(state, quest, announce=True):
    record = records(state).setdefault(quest.name, {})
    within = getattr(quest, "within_turns", None)
    record.update(status="active", stage=0, started=state.actions_taken, ended=None, ready=False,
                  progress=state.actions_taken, deadline=state.actions_taken + within if within else None)
    _log(state, quest, "started")
    if announce:
        _announce(state, quest)
    _point_the_way(state, quest, _goal_places(_stage_goal(quest, 0)[0]))


def _offer(state, quest):
    """Put the quest to the player: they take it on or turn it down (answer), as a choice."""
    records(state)[quest.name] = {"status": "offered", "stage": 0, "started": None, "ended": None,
                                  "offered": state.actions_taken, "waited": 0}
    _log(state, quest, "offered")
    _announce(state, quest)


def _decline(state, quest, what):
    record = records(state)[quest.name]
    record.update(status="declined", ended=state.actions_taken)
    _log(state, quest, what)
    if what == "declined":
        _tell(state, quest, "decline", "side_quest_declined", title=quest.title)
    state._run_effects(getattr(quest, "on_decline", None) or ())


def answer(state, key, choice):
    """The player's answer to a side quest: ``accept`` (take the offer on), ``decline`` (turn it
    down) or ``turn_in`` (hand it over to its giver). No turn passes. Returns whether it applied."""
    quest, record = quests(state).get(key), records(state).get(key)
    if quest is None or not record:
        return False
    if choice == "turn_in":
        if record.get("status") != "active" or not record.get("ready"):
            return False
        _hand_over(state, quest)
        return True
    if record.get("status") != "offered":
        return False
    if choice == "accept":
        _start(state, quest, announce=False)
        _tell(state, quest, "accept", "side_quest_accepted", title=quest.title)
        if getattr(quest, "within_turns", None):
            state._say("side_quest_deadline", title=quest.title, turns=quest.within_turns)
        _progress(state, quest)  # its goal may hold already
    else:
        _decline(state, quest, "declined")
    return True


def _hand_over(state, quest):
    _tell(state, quest, "turn_in", "side_quest_turned_in", giver=state._called(quest.giver), title=quest.title)
    _finish(state, quest, "completed")


def _finish(state, quest, status, reason=None, what=None):
    record = records(state)[quest.name]
    record.update(status=status, ended=state.actions_taken, ready=False)
    _log(state, quest, status)
    if status == "completed":
        state._run_effects(quest.rewards)
        _tell(state, quest, "complete", "side_quest_complete", title=quest.title)
        record["aided"] = _aid_mission(state, quest)
        side_story.completed(state, quest, record["aided"])  # what it led to; its reveal (texts.reveal)
        return
    record["reason"] = reason
    message = {"time": "side_quest_expired", "giver_gone": "side_quest_giver_gone",
               "item_gone": "side_quest_item_gone"}.get(reason, "side_quest_failed")
    _tell(state, quest, "fail", message, title=quest.title, giver=state._called(quest.giver) if quest.giver else "",
          item=state._called(what) if what else "")
    state._run_effects(quest.on_fail)
    side_story.failed(state, quest)


def _aid_mission(state, quest):
    """What completing it does for the story's mission (aid_mode): ``lead``, the way toward where the
    mission advances next opens; ``ally``, its giver becomes an ally (who leads and helps); None when
    it changes nothing (no mission, won, or the way is known already)."""
    how = aid_mode(quest)
    if how == "none":
        return None
    if how == "ally" and quest.giver and quest.giver not in state.__dict__.get("allies", set()):
        state.__dict__.setdefault("allies", set()).add(quest.giver)
        _say(state, "side_quest_ally", npc=state._called(quest.giver), title=quest.title)
        return "ally"
    if not state.mission or state.won:
        return None
    here = state.current.name
    pulls = [(state._quest_pull(name), name) for name, location in state.world.locations.items()
             if name != here and name not in state.current.connected and state.is_enabled(location)]
    pulls = [(pull, name) for pull, name in pulls if pull > 0]
    if not pulls:
        return None
    target = sorted(pulls, key=lambda item: (-item[0], state.location_counts.get(item[1], 0), item[1]))[0][1]
    opened = state._open_route(target)
    if opened is None:
        return None
    _say(state, "side_quest_aid", title=quest.title, location=opened.name)
    return "lead"


def _say(state, key, **values):
    state._say(key, **values)


def _goal_places(goal):
    """The places a step's goal names (``location``; ``visited`` / ``at_location`` in a condition)."""
    places = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("visited", "at_location") and isinstance(value, str):
                    places.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for key, value in (goal or {}).items():
        if key == "location" and isinstance(value, str):
            places.append(value)
        elif key == "condition":
            walk(value)
    return places


def _point_the_way(state, quest, places):
    """Whoever set the player on it (the giver, else what the player heard) points the way to the
    places it now needs that no known way leads to: the way opens from here, along the map."""
    for place in places:
        if place not in state.world.locations or place == state.current.name or place in state.current.connected:
            continue
        if place in state.location_counts:  # been there: the player knows the way back
            continue
        if not state._route_path(state.current.name, place):
            continue
        if state._open_route(place) is not None:
            _say(state, "side_quest_way", title=quest.title, location=place,
                 source=state._called(quest.giver) if quest.giver else "What you heard")


def _progress(state, quest):
    """A side quest under way: failed (fail_if, its time up, its giver gone while it waits for them),
    its steps done in order (each one's rewards), then completed (the quest's rewards), or, with
    ``turn_in``, ready to be handed over to its giver."""
    record = records(state)[quest.name]
    if quest.fail_if is not None and quest.fail_if.holds(state.facts):
        return _finish(state, quest, "failed", "condition")
    impossible = _impossible(state, quest, record) if settings()["impossible"] == "fail" else None
    if impossible:
        return _finish(state, quest, "failed", impossible[0], impossible[1])
    deadline = record.get("deadline")
    if deadline is not None and state.actions_taken >= deadline and not (
            record.get("ready") or (not quest.turn_in and _last_step(quest, record) and _goal_met(state, quest, record["stage"]))):
        return _finish(state, quest, "failed", "time")
    if record.get("ready"):
        if quest.giver in state.removed_npcs:
            return _finish(state, quest, "failed", "giver_gone")
        if not _goal_met(state, quest, record["stage"]):  # what was brought is gone again
            record["ready"] = False
        elif state.character_interactions.get(quest.giver, 0) > int(record.get("ready_talks", 0)):
            return _hand_over(state, quest)  # dealt with the giver since: handed over
        else:
            return None
    stages = quest.stages
    while stages and record["stage"] < len(stages) - 1 and _goal_met(state, quest, record["stage"]):
        done = stages[record["stage"]]
        record["stage"] += 1
        record["progress"] = state.actions_taken
        following = stages[record["stage"]]
        _log(state, quest, "stage", stage=done["title"])
        state._run_effects(done["rewards"])
        _say(state, "side_quest_stage", title=quest.title, done=done["title"], next=following["title"],
             description=following["description"])
        _point_the_way(state, quest, _goal_places(following["goal"]))
    if _goal_met(state, quest, record["stage"]):
        if getattr(quest, "turn_in", False) and quest.giver:
            if not record.get("ready"):
                record.update(ready=True, progress=state.actions_taken,
                              ready_talks=state.character_interactions.get(quest.giver, 0))
                _log(state, quest, "ready")
                _tell(state, quest, "ready", "side_quest_ready", title=quest.title,
                      giver=state._called(quest.giver))
                _point_the_way(state, quest, [state.npc_homes.get(quest.giver)] if quest.giver in state.npc_homes else [])
            return None
        _finish(state, quest, "completed")
    return None


def _last_step(quest, record):
    return not quest.stages or record.get("stage", 0) >= len(quest.stages) - 1


def _pace(state, quest, record):
    """Remind the player of a quiet side quest; warn when its time runs short (once)."""
    cfg = settings()
    deadline = record.get("deadline")
    if deadline is not None and not record.get("hurried") and 0 < deadline - state.actions_taken <= 2:
        record["hurried"] = True
        _log(state, quest, "hurry")
        _say(state, "side_quest_hurry", title=quest.title, turns=deadline - state.actions_taken)
        return
    after = cfg["remind_after"]
    quiet_since = max(int(record.get("progress") or 0), int(record.get("reminded") or 0))
    if after and state.actions_taken - quiet_since >= after:
        record["reminded"] = state.actions_taken
        _log(state, quest, "reminded")
        if record.get("ready"):
            _tell(state, quest, "remind", "side_quest_remind_ready", title=quest.title,
                  giver=state._called(quest.giver))
        else:
            todo = "; ".join(state._goal_words(k, v) for k, v in pending(state, quest.name).items())
            _tell(state, quest, "remind", "side_quest_reminder", title=quest.title, todo=todo or quest.description)


def update(state):
    """Offer, start, advance, complete and fail the side quests (end of a turn; never mid-turn).
    Returns whether anything changed."""
    cfg = settings()
    every = quests(state)
    if not cfg["enabled"] or not every or state.ended:
        return False
    before = len(state.__dict__.get("side_quest_events") or [])
    held = cfg["pause_when_held"] and state._location_is_locked()
    for key, quest in every.items():  # the ones under way and on offer first
        record = records(state).get(key) or {}
        if record.get("status") in ("active", "offered") and _react(state, quest, record) == "stop":
            continue
        if record.get("status") == "active":
            if held and record.get("deadline") is not None:  # held: its time does not run
                record["deadline"] += 1
            _progress(state, quest)
            if record.get("status") == "active":
                _pace(state, quest, record)
        elif record.get("status") == "offered":
            # Turns it stood unanswered among the choices (an event in progress hides offers: not counted).
            if not state.active_event and state.actions_taken > int(record.get("offered") or 0):
                record["waited"] = int(record.get("waited", 0)) + 1
            if quest.giver and quest.giver in state.removed_npcs:  # who offered it is gone: so is the offer
                record.update(status="declined", ended=state.actions_taken, reason="giver_gone")
                _log(state, quest, "withdrawn")
            elif int(record.get("waited", 0)) >= cfg["offer_turns"] or not state.is_enabled(quest):
                _decline(state, quest, "lapsed")  # not answered in time: it lapses
    taken = sum(1 for record in records(state).values() if record.get("status") in ("active", "offered"))
    candidates = [quest for quest in every.values() if offered(state, quest)]
    if candidates and taken < cfg["max_active"] and story_allows_new(state, cfg):
        order = {key: number for number, key in enumerate(every)}
        story = side_story.settings()
        if story["enabled"]:
            # The side-story policy: what fits the story and bears on the main quest first, and nothing
            # while the main story needs the room (side_story.hold).
            candidates.sort(key=lambda quest: (-side_story.priority(state, quest, story), order[quest.name]))
        elif cfg["fit"]:  # what fits the story now first; written order among equals
            candidates.sort(key=lambda quest: (-fit(state, quest), order[quest.name]))
        room = cfg["max_active"] - taken
        for quest in candidates[:1 if cfg["offer_cooldown"] else room]:
            if story["enabled"] and side_story.hold(state, "offer", side_story.quest_relevance(state, quest),
                                                    story) is not None:
                continue  # not now (or the one before took the room)
            state.__dict__["_side_quest_last_new"] = state.actions_taken
            side_story.note(state, "offer" if accept_mode(quest) == "choice" else "start", quest.name, quest.title,
                            side_story.quest_relevance(state, quest))
            if getattr(quest, "follows", None):
                side_story.link(state, "side_quest", quest.follows, "follow_up", quest.name)
            if accept_mode(quest) == "choice":
                _offer(state, quest)
                continue
            _start(state, quest)
            _progress(state, quest)  # its goal may hold already (the item in hand)
    return len(state.__dict__.get("side_quest_events") or []) != before


# ---- the player's choices ---------------------------------------------------------------------

def offers(state):
    """The side quests on offer now, in the order they were offered."""
    return [quests(state)[key] for key, record in sorted(records(state).items(),
                                                         key=lambda item: item[1].get("offered") or 0)
            if record.get("status") == "offered" and key in quests(state)]


def _choice(quest, key, label, told):
    from .actions import Action, Outcome

    action = Action(f"{label}: {quest.title}", [Outcome(told, tags=["social"])], character_name=quest.giver,
                    source="side_quest", owner=quest.name, repeat="always")
    action.key = key
    return action


def offer_options(state):
    """The free choices of side quests (none during an event): "Take on: <title>" and "Turn down:
    <title>" for each one on offer; "Hand over: <title>" for each one done and waiting for its giver,
    when the giver is in the scene. Ids: side-quest:<quest>:accept | decline | turn-in."""
    if state.active_event or state.ended:
        return []
    options = []
    for quest in offers(state):
        options.append(_choice(quest, "accept", "Take on", "You take it on."))
        options.append(_choice(quest, "decline", "Turn down", "You let it go."))
    here = set(state.__dict__.get("visible_inhabitants") or ())
    for key, record in records(state).items():
        quest = quests(state).get(key)
        if quest is not None and record.get("status") == "active" and record.get("ready") and quest.giver in here:
            options.append(_choice(quest, "turn_in", "Hand over", f"You hand it over to the {quest.giver}."))
    return options


# ---- what the player follows ------------------------------------------------------------------

def log(state):
    """The side quests as the player follows them, in the order they were offered or started:
    [{key, title, description, giver, status, reason, stages: [{title, description, state:
    done|current|todo}], todo (what the current step still needs, in words), ready (done, to hand
    over to the giver), rewards (in words), deadline, turns_left, turn_in, aids_mission,
    aided_mission, follows, offered, started, ended}]."""
    entries = []
    for key, record in sorted(records(state).items(),
                              key=lambda item: (item[1].get("offered") or item[1].get("started") or 0)):
        quest = quests(state).get(key)
        if quest is None:
            continue
        status, index = record.get("status"), int(record.get("stage", 0))
        stages = [{"title": stage["title"], "description": stage.get("description") or "",
                   "state": "done" if status == "completed" or number < index or record.get("ready") else
                   "current" if number == index and status == "active" else "todo"}
                  for number, stage in enumerate(quest.stages)]
        deadline = record.get("deadline")
        entries.append({
            "key": key, "title": quest.title, "description": quest.description or "", "giver": quest.giver,
            "status": status, "reason": record.get("reason"), "stages": stages,
            "todo": [state._goal_words(k, v) for k, v in pending(state, key).items()] if status == "active" else [],
            "ready": bool(record.get("ready")), "rewards": reward_words(state, quest.rewards),
            "deadline": deadline,
            "turns_left": max(0, deadline - state.actions_taken) if deadline is not None and status == "active" else None,
            "turn_in": bool(getattr(quest, "turn_in", False)), "aids_mission": aid_mode(quest),
            "aided_mission": record.get("aided"), "follows": getattr(quest, "follows", None),
            "offered": record.get("offered"), "started": record.get("started"), "ended": record.get("ended"),
        })
    return entries


def events_at(state, turn):
    """What happened to side quests at the end of turn ``turn`` (the journal's ``side_quests``)."""
    return [{k: entry[k] for k in ("quest", "title", "what", "stage")}
            for entry in state.__dict__.get("side_quest_events") or [] if entry.get("turn") == turn]


def moments_at(state, turn):
    """Whether turn ``turn`` brought a side-quest moment (offered, started, a step, ready, completed,
    failed): something new for the director's rhythm and the fitness's developments."""
    return any(entry.get("what") in MOMENTS for entry in events_at(state, turn))


# ---- the storyteller's pull ---------------------------------------------------------------------

def _on_the_way(state, action, place):
    """Whether a travel option is the next step on the map toward ``place`` (not next door)."""
    if action.source != "navigation" or not place or place == state.current.name:
        return False
    path = state._route_path(state.current.name, place)
    return bool(path) and len(path) > 1 and action.owner == path[0]


def affinity(state, action):
    """How much an action works toward the side quests: their current steps' goals (1; a step on the
    way to a place a goal names, 0.75), more as time runs out; the way back to a giver waiting for
    what was asked (1.5; a step on the way, 0.75); or someone who offers one and has not been met
    (0.5)."""
    score = 0.0
    for key, record in records(state).items():
        if record.get("status") != "active":
            continue
        quest = quests(state).get(key)
        if quest is None:
            continue
        urgency = 1.0
        if record.get("deadline") is not None:
            urgency += max(0, 3 - (record["deadline"] - state.actions_taken)) * 0.5
        if record.get("ready"):
            home = state.npc_homes.get(quest.giver)
            with_giver = action.character_name == quest.giver or (
                action.source == "approach" and action.owner == quest.giver)
            if with_giver or (action.source == "navigation" and home and action.owner == home):
                score += 1.5 * urgency
            elif _on_the_way(state, action, home):
                score += 0.75 * urgency
            continue
        for goal_key, value in pending(state, key).items():
            if state._advances(action, goal_key, value):
                score += urgency
            elif goal_key == "location" and _on_the_way(state, action, value):
                score += 0.75 * urgency
    person = (action.character_name or action.owner) if action.source in ("conversation", "approach", "npc") else None
    if person and offered_by(state, person) and state.character_interactions.get(person, 0) <= 0:
        score += 0.5
    return score


def best_option(state, pairs):
    """The option (of the ranked ``pairs``) that works most toward a side quest, or None."""
    if not settings()["guide"] or not quests(state):
        return None
    scored = [(affinity(state, a), -index, a) for index, (_, a) in enumerate(pairs)]
    best = max(scored, key=lambda item: (item[0], item[1]), default=(0.0, 0, None))
    return best[2] if best[0] > 0 else None


def ranking_weight(state):
    """What side quests add to the storyteller's ranking of an option: side_quest_settings.rank_weight
    x the storyteller's side_quest_bias (its own trait, learned in training apart from the mission's
    goal_bias), x the option's affinity; 0 when none is under way or on offer from someone (nothing
    to work toward)."""
    if not settings()["enabled"] or not quests(state):
        return 0.0
    if not any(record.get("status") == "active" for record in records(state).values()) and not any(
            quest.giver and quest.name not in records(state) for quest in quests(state).values()):
        return 0.0
    return settings()["rank_weight"] * float(getattr(state.genome, "side_quest_bias", 0.6))


# ---- the session: report, fitness, parameters ----------------------------------------------------

def summary(state):
    """{completed, failed, declined, unfinished, aided_mission}: the side quests' keys by how they
    stand (the session report; unfinished: under way or still on offer when it ended)."""
    found = {"completed": [], "failed": [], "declined": [], "unfinished": [], "aided_mission": []}
    for key, record in records(state).items():
        status = record.get("status")
        found[status if status in ("completed", "failed", "declined") else "unfinished"].append(key)
        if record.get("aided"):
            found["aided_mission"].append(key)
    return found


def fitness_term(state, cfg, version=1):
    """The side quests' share of a story's fitness (fitness.side_quests).

    Version 1: completed x complete_bonus + steps done x step_bonus + those that helped the mission x
    mission_aid_bonus - failed x fail_penalty - (once the story has ended) those left under way x
    unfinished_penalty. Version 2 (fitness.version): each one more is worth ``diminishing`` times the
    one before (a side quest like one done already, the same giver and goal, ``repeat_factor`` times
    that again), so the term is bounded however many side quests a story holds: at most
    (complete_bonus + step_bonus + mission_aid_bonus) / (1 - diminishing); threads left open count in
    fitness.side_story (side_unresolved) instead."""
    done = summary(state)
    events = state.__dict__.get("side_quest_events") or []
    steps = sum(1 for entry in events if entry.get("what") == "stage")
    if version < 2:
        unfinished = sum(1 for key in done["unfinished"] if records(state)[key].get("status") == "active")
        return (len(done["completed"]) * float(cfg.get("complete_bonus", 10))
                + steps * float(cfg.get("step_bonus", 3))
                + len(done["aided_mission"]) * float(cfg.get("mission_aid_bonus", 6))
                - len(done["failed"]) * float(cfg.get("fail_penalty", 4))
                - (unfinished * float(cfg.get("unfinished_penalty", 2)) if state.ended else 0.0))
    decay = min(0.95, max(0.0, float(cfg.get("diminishing", 0.6))))
    repeat = min(1.0, max(0.0, float(cfg.get("repeat_factor", 0.25))))

    def series(count):
        return sum(decay ** index for index in range(count))

    seen, completed_value = {}, 0.0
    order = sorted(done["completed"], key=lambda key: (records(state)[key].get("ended") or 0, key))
    for index, key in enumerate(order):
        quest = quests(state).get(key)
        signature = (getattr(quest, "giver", None), tuple(sorted(_stage_goal(quest, 0)[0] or {}))) if quest else key
        completed_value += decay ** index * repeat ** seen.get(signature, 0)
        seen[signature] = seen.get(signature, 0) + 1
    return (completed_value * float(cfg.get("complete_bonus", 10))
            + series(steps) * float(cfg.get("step_bonus", 3))
            + series(len(done["aided_mission"])) * float(cfg.get("mission_aid_bonus", 6))
            - len(done["failed"]) * float(cfg.get("fail_penalty", 4)))


def restore(state, saved):
    """Set the side quests from ``saved`` ({key: {status, stage, ready}}: the session's parameters),
    quietly: nothing is announced or paid again."""
    for key, entry in (saved or {}).items():
        quest = quests(state).get(key)
        if quest is None or not isinstance(entry, dict) or entry.get("status") not in STATUSES:
            continue
        stage = max(0, min(int(entry.get("stage", 0) or 0), max(0, len(quest.stages) - 1)))
        status = entry["status"]
        record = {"status": status, "stage": stage,
                  "started": None if status in ("offered", "declined") else 0,
                  "ended": None if status in ("active", "offered") else 0,
                  "progress": state.actions_taken, "ready": bool(entry.get("ready")) and status == "active"}
        if status == "offered":
            record.update(offered=state.actions_taken, waited=0)
        if record["ready"] and quest.giver:
            record["ready_talks"] = state.character_interactions.get(quest.giver, 0)
        within = getattr(quest, "within_turns", None)
        if status == "active" and within:
            record["deadline"] = state.actions_taken + max(1, int(entry.get("turns_left") or within))
        records(state)[key] = record


def parameters(state):
    """{key: {status, stage, ready, turns_left}}: the side quests as the session's parameters
    (ane.session_state/1)."""
    found = {}
    for key, record in records(state).items():
        entry = {"status": record.get("status"), "stage": int(record.get("stage", 0)), "ready": bool(record.get("ready"))}
        if record.get("deadline") is not None and record.get("status") == "active":
            entry["turns_left"] = max(0, record["deadline"] - state.actions_taken)
        found[key] = entry
    return found
