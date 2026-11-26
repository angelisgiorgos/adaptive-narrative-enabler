"""Simulated players: who chooses the player's actions when storytellers are evaluated.

The storyteller (a genome) is the narrative controller, the director. It decides what the world
offers and how the story answers: the world it builds (outcome odds, guards, leads), who is where,
when events and encounters come, how stakes and failures fall, and which choices the player is
shown, in what order (GameState.player_options). It does not choose for the player.

A simulated player chooses among the choices shown as a person would, with only what a person
sees: their names and their order, the kind of choice (travel, talk, an item, the place's own),
the skill check with its chance and what failing costs, the quest log and who can help with it,
the places and people already met, health. Never the storyteller's own scores. Evolution evaluates
every storyteller with the same players on the same stories (a story seed always has the same
player), so a storyteller is rewarded for what it offers and how the story answers, not for
playing well itself.

Before, the storyteller also took the player's turns (GameState._storyteller_pick: its own
favourite choice), so training could reward a storyteller partly for choosing well, which does
not transfer to people. That evaluation is kept (``evolution.player: storyteller``) to compare
with, and for training states saved before.

Players (PLAYERS): follower (mostly one of the first choices shown), explorer (new places and new
people first), quester (the quest's next step first), cautious (avoids likely failures, the more so
when hurt), random. ``evolution.player_mix`` weighs how often each plays.

Playtest strategies (STRATEGIES, evaluator/playtest.py) add side_quester (side quests first), fighter
(combat first) and conflict_avoider (cautious, and avoids fights). They are not in training's player
mix, so training and its seeds are unchanged.
"""
from __future__ import annotations

import random

from .context import with_state_context

MODES = ("simulated", "storyteller")
DEFAULT_MIX = {"follower": 2, "explorer": 1, "quester": 2, "cautious": 1, "random": 1}


def _weighted(rng, weights):
    total = sum(weights)
    if total <= 0:
        return rng.randrange(len(weights))
    draw = rng.random() * total
    for index, weight in enumerate(weights):
        draw -= weight
        if draw < 0:
            return index
    return len(weights) - 1


def _target(action):
    """What a choice is about, as the player reads it: a place to go, a person, an item."""
    return getattr(action, "owner", None) or getattr(action, "character_name", None) or ""


class Player:
    """A simulated player: ``choose`` gives the index of the choice it takes among those shown."""

    name = "player"

    def __init__(self, rng):
        self.rng = rng

    def weights(self, state, options):
        return [1.0] * len(options)

    def choose(self, state, options):
        return _weighted(self.rng, self.weights(state, options))


class RandomPlayer(Player):
    """Any choice shown, equally likely."""

    name = "random"


class Follower(Player):
    """Reads the choices top-down and mostly takes one of the first: each next one is about half
    as likely (the order is the director's: what it puts first is what people take most)."""

    name = "follower"

    def weights(self, state, options):
        return [0.5 ** index for index in range(len(options))]


class Explorer(Player):
    """New places and new people first, then anything."""

    name = "explorer"

    def weights(self, state, options):
        visited = {name for name, count in state.location_counts.items() if count}
        talked = {name for name, count in state.character_interactions.items() if count}
        weights = []
        for action in options:
            target = _target(action)
            if action.source == "navigation" and target and target not in visited:
                weights.append(6.0)
            elif action.source in ("conversation", "npc", "approach") and target and target not in talked:
                weights.append(3.0)
            else:
                weights.append(1.0)
        return weights


class Quester(Player):
    """The quest's next step first: a choice the quest log names, choices about what it says is
    still needed, or with a person who can help with it now; otherwise mostly one of the first."""

    name = "quester"

    def weights(self, state, options):
        log = state.quest_log() or {}
        todo = " ".join(log.get("todo") or []).lower()
        helpers = {person["name"] for person in state.people_here() if person.get("quest")}
        weights = []
        for index, action in enumerate(options):
            weight = 0.5 ** index
            target = _target(action)
            if target and target.lower() in todo:
                weight += 4.0
            if target in helpers:
                weight += 2.0
            said = action.name.split(") ", 1)[-1].lower()  # the choice, as the quest log may name it
            if len(said) > 3 and said in todo:
                weight += 6.0
            if action.source == "side_quest":  # a side quest helps the quest (side_quest_settings.aids_mission)
                weight += 2.0 if getattr(action, "key", None) == "accept" else 0.0
            weights.append(weight)
        return weights


class Cautious(Player):
    """Avoids the choices it will probably fail, the more so when hurt; likes choices without a check."""

    name = "cautious"

    def weights(self, state, options):
        hurt = state.health < state.max_health / 2
        weights = []
        for action in options:
            hint = state.check_hint(action)
            if hint is None:
                weights.append(2.0)
                continue
            weight = max(0.05, hint["chance"] / 100) ** (3.0 if hurt else 1.5)
            if hint.get("risk"):
                weight *= 0.6 if hurt else 0.85
            weights.append(weight)
        return weights


PLAYERS = {cls.name: cls for cls in (Follower, Explorer, Quester, Cautious, RandomPlayer)}


# ---- playtest strategies (evaluator/playtest.py): not part of training's player mix -------------

COMBAT_TAGS = frozenset({"combat", "fight", "attack", "ambush", "assault", "raid", "violence", "duel", "battle"})
COMBAT_WORDS = ("fight", "attack", "strike", "ambush", "duel", "charge", "battle", "hold the", "defend")
YIELD_WORDS = ("surrender", "flee", "hide", "retreat", "run", "sneak", "give in", "talk", "negotiate", "bribe",
               "avoid", "leave", "slip away")


def is_combat(state, action):
    """Whether a choice looks like a fight to a player: its outcomes are tagged as combat, its check is
    the combat stat (character_stats.tag_checks: combat), or its name says so."""
    tags = {str(tag).lower() for outcome in getattr(action, "outcomes", None) or () for tag in outcome.tags}
    if tags & COMBAT_TAGS:
        return True
    hint = state.check_hint(action) if getattr(action, "outcomes", None) else None
    if hint and str(hint.get("stat", "")).lower() in ("attack", "combat"):
        return True
    said = action.name.split(") ", 1)[-1].lower()
    return any(word in said for word in COMBAT_WORDS)


def is_side_quest_step(state, action):
    """Whether a choice works toward a side quest: an offer's answer, or what an active side quest's
    current step names (its place or person)."""
    if action.source == "side_quest":
        return True
    from . import side_quests  # (imported here: side_quests depends on the story, the story on players)

    wanted = set()
    for key, record in side_quests.records(state).items():
        quest = side_quests.quests(state).get(key)
        if quest is None or record.get("status") != "active":
            continue
        if record.get("ready") and quest.giver:  # done: back to who asked
            wanted.add(quest.giver.lower())
            continue
        goal = side_quests._stage_goal(quest, int(record.get("stage", 0)))[0]
        wanted |= {place.lower() for place in side_quests._goal_places(goal)}
        wanted |= {str(value).lower() for value in (goal or {}).values() if isinstance(value, str)}
    target = _target(action).lower()
    if target and target in wanted:
        return True
    places = [name for name in state.world.locations if name.lower() in wanted]
    return any(side_quests._on_the_way(state, action, place) for place in places)  # the next step there


class SideQuester(Player):
    """Side quests first: takes on every side quest offered, then follows its steps; meets new people
    (who may offer one); otherwise mostly one of the first choices."""

    name = "side_quester"

    def weights(self, state, options):
        talked = {name for name, count in state.character_interactions.items() if count}
        weights = []
        for index, action in enumerate(options):
            weight = 0.5 ** index
            if action.source == "side_quest":
                weight += 8.0 if getattr(action, "key", None) == "accept" else 0.0
            elif is_side_quest_step(state, action):
                weight += 6.0
            elif action.source in ("approach", "conversation", "npc") and _target(action) not in talked:
                weight += 2.0
            weights.append(weight)
        return weights


class Fighter(Player):
    """Combat first: takes the fights it is offered (attacks, ambushes, duels) whatever its health."""

    name = "fighter"

    def weights(self, state, options):
        return [6.0 if is_combat(state, action) else 0.5 ** index + 0.2 for index, action in enumerate(options)]


class ConflictAvoider(Cautious):
    """Cautious, and avoids conflict: fights are its last choice; giving way, hiding or talking first."""

    name = "conflict_avoider"

    def weights(self, state, options):
        weights = []
        for action, weight in zip(options, super().weights(state, options)):
            said = action.name.split(") ", 1)[-1].lower()
            if is_combat(state, action):
                weight *= 0.05
            elif any(word in said for word in YIELD_WORDS):
                weight *= 3.0
            weights.append(weight)
        return weights


# Every strategy a playtest can name (core_engine/players.py's players and the playtest ones), with the
# names the playtest documents use for the training players.
STRATEGIES = {**PLAYERS, **{cls.name: cls for cls in (SideQuester, Fighter, ConflictAvoider)}}
STRATEGY_ALIASES = {"main_quest": "quester", "explorer": "explorer", "side_quest": "side_quester",
                    "combat": "fighter", "cautious": "cautious", "conflict_avoiding": "conflict_avoider",
                    "random": "random", "follower": "follower"}


def strategy(name, seed):
    """The playtest strategy ``name`` (a STRATEGIES name or a STRATEGY_ALIASES one) with its own dice for
    the story seed ``seed`` (the same seed and name: the same choices, in every process)."""
    key = STRATEGY_ALIASES.get(name, name)
    if key not in STRATEGIES:
        raise ValueError(f"Unknown player strategy `{name}`; use {', '.join(sorted(set(STRATEGIES) | set(STRATEGY_ALIASES)))}.")
    return STRATEGIES[key](random.Random(f"ane-playtest:{key}:{seed}"))


def mix_errors(mix):
    """Problems of an ``evolution.player_mix`` (empty when it can be used)."""
    if not isinstance(mix, dict) or not mix:
        return ["evolution.player_mix must map players to weights."]
    errors = [f"evolution.player_mix: unknown player `{name}`; use {', '.join(PLAYERS)}." for name in mix
              if name not in PLAYERS]
    if not errors and not any(isinstance(w, (int, float)) and not isinstance(w, bool) and w > 0 for w in mix.values()):
        errors.append("evolution.player_mix needs a weight above 0.")
    return errors


def player_for(seed, mix=None):
    """The simulated player of the story seed ``seed``: the same seed has the same player and the
    same choices, in every process."""
    mix = {name: float(weight) for name, weight in (mix or DEFAULT_MIX).items()
           if name in PLAYERS and float(weight) > 0} or dict(DEFAULT_MIX)
    rng = random.Random(f"ane-player:{seed}")  # a text seed: the same in every process
    names = sorted(mix)
    return PLAYERS[names[_weighted(rng, [mix[name] for name in names])]](rng)


@with_state_context
def take_turn(state, player):
    """One choice: ``player`` chooses among the choices shown; False when the story cannot go on.
    The options are ranked once and shared by the choices shown and the step. Approaching
    someone or leaving a conversation is a choice too, but no turn (GameState.take_option)."""
    if state.ended:
        return False
    # After a choice that took no turn, nothing but the conversation changed: the turn's ranking holds.
    kept = state.__dict__.pop("_ranking_after_free_step", None)
    if kept is not None and kept[0] == (state.actions_taken, len(state.history)):
        actions, pairs = kept[1], kept[2]
    else:
        actions = state._actions_now()
        pairs = state._rank_actions(actions)
    ranked = [action for _, action in pairs]  # what step() indexes (ranked_available_actions)
    options = state.player_options(ranked=(actions, pairs))
    if not ranked or not options:
        return False
    chosen = options[player.choose(state, options)]
    went_on = state.take_option(chosen, ranked=ranked)
    if chosen.source in ("approach", "leave"):  # (a side quest taken on changes the ranking: not kept)
        state._ranking_after_free_step = ((state.actions_taken, len(state.history)), actions, pairs)
    return went_on


@with_state_context
def play(state, max_steps, player=None):
    """Play ``state`` for up to ``max_steps`` turns: ``player`` chooses, or, with None, the
    storyteller takes its own favourite choice (the older evaluation, ``evolution.player:
    storyteller``). Choices that take no turn (approaching someone, leaving a conversation)
    do not count."""
    start, free = state.actions_taken, 0
    while state.actions_taken - start < max_steps and free < 50:
        if state.health <= 0:
            break
        before = state.actions_taken
        if not (take_turn(state, player) if player is not None else state.step()):
            break
        free = free + 1 if state.actions_taken == before else 0
    return state


def for_settings(seed, settings):
    """The player of a story seed under evolution ``settings`` (None: the storyteller plays)."""
    if (settings or {}).get("player", "simulated") != "simulated":
        return None
    return player_for(seed, (settings or {}).get("player_mix"))
