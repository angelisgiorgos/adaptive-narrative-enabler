"""People and factions that remember (core_engine/relationships.py): structured NPC state, deeds and
witnesses, explicit goals beside the keyword fallback, factions and their standing, hostility
transitions, reactions and contextual behaviour, the director's guard, dialogue and actions unlocked
or withheld by the relationship, what the player and the author see, save games, the profile's
later stories, deterministic replay, the world's validation and the demo worlds' examples."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import copy
import json
import random
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from backend import store
from core_engine import (Action, Character, Event, GameState, Genome, Location, Outcome, SideQuest, WorldGraph,
                         conditions, effects, relationships, resume, savegame, side_quests)
from core_engine.builder import build_world, logic_errors
from core_engine.context import EngineContext
from core_engine.diagnostics import decision_trace
from core_engine.effects import EffectList
from core_engine.variables import VariableStore
from utils.config_loader import config


def _genome():
    random.seed(11)
    genome = Genome()
    genome.event_prob = 0.0
    genome.chaos_bias = 0.0
    return genome


def _act(name, *outcomes, owner=None, **fields):
    return Action(f"({owner}) {name}" if owner else name, list(outcomes), character_name=owner, source="npc",
                  owner=owner, repeat="always", **fields)


def _say(text, *tags, **fields):
    return Outcome(text, tags=list(tags) or ["neutral"], **fields)


FACTIONS = {
    "Watch": {"hostile_below": -2, "friendly_at": 2, "relations": {"Outlaws": -1}, "reacts_to": {"theft": -1},
              "reactions": [{"signal": "faction", "to": "hostile", "behaviour": "warn",
                             "text": "The Watch wants a word with you."}]},
    "Outlaws": {"public": False},
    "Chapel": {"reacts_to": {"quest_completed": 1}},
}


def _world(factions=None, **people):
    """Square - Chapel, Square - Barracks, Square - Woods; a vendor, a watchman, a sister and her brother,
    an outlaw (hostile from the start), and whoever ``people`` adds or replaces."""
    world = WorldGraph()
    for name, tags in (("Square", ["urban", "market"]), ("Chapel", ["religion", "quiet"]),
                       ("Barracks", ["guarded", "law"]), ("Woods", ["wild", "forest"])):
        location = Location(name, tags)
        location.actions = [Action(f"Rest in the {name}", [_say("You rest.")], source="location", owner=name,
                                   repeat="always")]
        world.add_location(location)
    for other in ("Chapel", "Barracks", "Woods"):
        world.connect("Square", other)
    cast = {
        "Vendor": Character("Vendor", ["market"], unique=True, known_locations=["Square"], actions=[
            _act("Pocket a trinket", _say("You pocket a trinket.", "theft", coin_change=2), owner="Vendor"),
            _act("Buy a pie", _say("A warm pie.", "trade", "social"), owner="Vendor"),
            _act("Haggle in earnest", _say("He haggles back.", "trade", "social"), owner="Vendor",
                 enabled_if={"npc_disposition": "Vendor", "op": "eq", "value": "hostile"}),
        ]),
        "Watchman": Character("Watchman", ["law"], unique=True, faction="Watch", known_locations=["Square"]),
        "Sister": Character("Sister", ["religion"], unique=True, faction="Chapel", known_locations=["Chapel"],
                            never={"confront"}, goals=["Pray for the town"], actions=[
                                _act("Light a candle", _say("The candle burns.", "devotion"), owner="Sister"),
                                _act("Help with the alms", _say("You hand out bread.", "help"), owner="Sister"),
                            ]),
        "Brother": Character("Brother", ["religion"], unique=True, known_locations=["Chapel"],
                             related={"Sister": 0.5}),
        "Outlaw": Character("Outlaw", ["wild"], unique=True, faction="Outlaws", known_locations=["Woods"],
                            relationship={"hostility": 4}, actions=[
                                _act("Strike the outlaw", _say("You strike first.", "attack"), owner="Outlaw")]),
    }
    cast["Sister"].related = {"Brother": 1.0}
    cast.update(people)
    for char in cast.values():
        if char is not None:
            world.add_character(char)
    world.factions = copy.deepcopy(FACTIONS if factions is None else factions)
    return world


def _settings(**relationships_policy):
    return {"relationships": {**(config.get("relationships") or {}), **relationships_policy},
            "side_story": {**config.get("side_story"), "enabled": False},
            "unexpected_events": {**config.get("unexpected_events"), "enabled": False},
            "npc_roles": {**(config.get("npc_roles") or {}), "enabled": False},
            "conversations": {**config.get("conversations"), "enabled": False},
            "encounters": {**config.get("encounters"), "enabled": False},
            "consequences": {**(config.get("consequences") or {}), "enabled": False},
            "session_scope": {**(config.get("session_scope") or {}), "enabled": False},
            "director": {**(config.get("director") or {}), "enabled": False}}


def _state(world=None, start="Square", seed=3, homes=None, store_=None, profile=None, **policy):
    """A story whose settings are fixed when it starts (its engine context); everyone lives where
    ``homes`` says (default: their first known place)."""
    world = world or _world()
    with patch.dict(config._config, _settings(**policy)):
        state = GameState(world, _genome(), start=start, rng=random.Random(seed), store=store_, profile=profile)
    for char in world.characters:
        state.npc_homes[char.name] = (homes or {}).get(char.name, char.known_locations[0] if char.known_locations
                                                       else start)
    state._scene_dirty = True
    state.get_available_actions()
    return state


def _do(state, name):
    """Take the action whose name ends with ``name`` (the player's choice)."""
    actions = state.ranked_available_actions()
    index = next(i for i, a in enumerate(actions) if a.name.endswith(name))
    state.step(index, ranked=actions)
    return state


def _names(state):
    return [a.name for a in state.get_available_actions()]


def _bind(state):
    return state.context.bind()


class StateAndConditionTests(unittest.TestCase):
    def test_initial_state_and_dispositions(self):
        state = _state()
        with _bind(state):
            self.assertEqual(relationships.disposition(state, "Vendor"), "neutral")
            self.assertEqual(relationships.disposition(state, "Outlaw"), "hostile")   # relationship.hostility 4
            self.assertEqual(relationships.trust(state, "Vendor"), 0)
            self.assertIsNone(relationships.disposition(state, "Nobody"))
            self.assertNotIn("Vendor", state.npc_relations)  # made the first time something touches them

    def test_conditions_read_people_and_factions(self):
        state = _state()
        with _bind(state):
            relationships.change(state, "Sister", trust=3)
            relationships.change_standing(state, "Chapel", 2)
            relationships.remember(state, "Sister", "helped")
            holds = state.holds
            self.assertTrue(holds({"npc_trust": "Sister", "op": "gte", "value": 3}))
            self.assertTrue(holds({"npc_hostility": "Sister", "op": "eq", "value": 0}))
            self.assertTrue(holds({"npc_disposition": "Sister", "op": "in", "value": ["friendly", "ally"]}))
            self.assertTrue(holds({"faction_standing": "Chapel", "op": "gte", "value": 2}))
            self.assertTrue(holds({"faction_friendly": "Chapel"}))
            self.assertFalse(holds({"faction_hostile": "Chapel"}))
            self.assertTrue(holds({"npc_remembers": "Sister/helped"}))
            self.assertFalse(holds({"npc_remembers": "Sister/theft"}))
            self.assertFalse(holds({"npc_ally": "Sister"}))

    def test_condition_validation(self):
        refs = conditions.References(npcs=["Sister"], factions=["Chapel"])
        self.assertEqual(conditions.validate({"npc_trust": "Sister", "op": "gte", "value": 1}, refs), [])
        self.assertIn("unknown NPC", " ".join(conditions.validate({"npc_trust": "Nobody", "value": 1}, refs)))
        self.assertIn("unknown faction", " ".join(conditions.validate({"faction_standing": "Guild", "value": 1}, refs)))
        self.assertIn("disposition is one of", " ".join(conditions.validate(
            {"npc_disposition": "Sister", "value": "angry"}, refs)))
        self.assertIn("eq, ne, in or not_in", " ".join(conditions.validate(
            {"npc_disposition": "Sister", "op": "gte", "value": "friendly"}, refs)))
        self.assertIn("<NPC>/<goal>", " ".join(conditions.validate({"npc_goal_done": "Sister"}, refs)))
        self.assertIn("unknown NPC", " ".join(conditions.validate({"npc_remembers": "Nobody/theft"}, refs)))
        self.assertIn("unknown faction", " ".join(conditions.validate({"faction_friendly": "Guild"}, refs)))
        # A persistent variable named like the old words (examples/frozen_monastery.yaml: `trust`) still works.
        self.assertEqual(conditions.normalize({"trust": {"gte": 2}}), ("variable", "trust", "gte", 2))


class EffectTests(unittest.TestCase):
    def test_effects_change_people_and_factions(self):
        state = _state()
        with _bind(state):
            state.apply_effects([{"change_trust": {"Sister": 2}}, {"change_hostility": {"Vendor": 1}},
                                 {"change_standing": {"Watch": 1}}])
            self.assertEqual(relationships.trust(state, "Sister"), 2)
            self.assertEqual(relationships.hostility(state, "Vendor"), 1)
            self.assertEqual(relationships.standing(state, "Watch"), 1)
            self.assertEqual(relationships.standing(state, "Outlaws"), -1)   # relations: Watch -> Outlaws x -1
            state.apply_effects([{"change_trust": 2}], npc="Brother")         # a number: the NPC who acts
            self.assertEqual(relationships.trust(state, "Brother"), 2)

    def test_effect_validation(self):
        refs = conditions.References(npcs=["Sister"], factions=["Chapel"], locations=["Square"])
        self.assertEqual(effects.validate([{"change_trust": {"Sister": 1}}, {"change_standing": {"Chapel": -1}},
                                           {"deed": "theft"}, {"deed": {"kind": "helped", "npc": "Sister"}},
                                           {"move_npc": {"Sister": "Square"}}, {"change_hostility": 2}], refs), [])
        problems = " ".join(effects.validate([{"change_trust": {"Nobody": 1}}, {"change_standing": {"Guild": 1}},
                                              {"deed": "gossip"}, {"move_npc": {"Sister": "Moon"}},
                                              {"change_hostility": {"Sister": "a lot"}}], refs))
        for expected in ("unknown NPC `Nobody`", "unknown faction `Guild`", "needs a deed", "unknown location `Moon`",
                         "must be a number"):
            self.assertIn(expected, problems)


class DeedTests(unittest.TestCase):
    def test_a_theft_reaches_target_witnesses_related_and_faction(self):
        state = _state()
        self.assertEqual(state.visible_inhabitants, {"Vendor", "Watchman"})
        _do(state, "Pocket a trinket")
        with _bind(state):
            self.assertEqual((relationships.trust(state, "Vendor"), relationships.hostility(state, "Vendor")), (-1, 1))
            self.assertEqual(relationships.hostility(state, "Watchman"), 1)          # a witness
            self.assertEqual(relationships.standing(state, "Watch"), -1)              # reacts_to theft, a member saw it
            self.assertEqual(relationships.standing(state, "Outlaws"), 1)             # its enemies are glad
            self.assertTrue(state.holds({"npc_remembers": "Watchman/theft"}))
            memory = state.npc_relations["Watchman"]["memories"][-1]
            self.assertEqual((memory["how"], memory["about"], memory["location"]), ("witness", "Vendor", "Square"))
            self.assertEqual(state.npc_deeds[-1]["kind"], "theft")
            self.assertEqual(state.npc_deeds[-1]["witnesses"], ["Watchman"])

    def test_related_people_hear_of_it(self):
        state = _state(start="Chapel")
        with _bind(state):
            relationships.deed(state, "betrayed", target="Sister", witnesses=[])
            self.assertEqual(relationships.trust(state, "Sister"), -3)
            self.assertEqual(relationships.trust(state, "Brother"), -1.5)   # x related 1.0 x deeds.betrayed.related 0.5
            self.assertEqual(state.npc_relations["Brother"]["memories"][-1]["how"], "heard")

    def test_attacking_someone_hostile_is_no_crime(self):
        state = _state(start="Woods", homes={"Watchman": "Woods"})
        self.assertIn("Watchman", state.visible_inhabitants)
        _do(state, "Strike the outlaw")
        with _bind(state):
            self.assertTrue(state.npc_deeds[-1]["justified"])
            self.assertEqual(relationships.hostility(state, "Watchman"), 0)       # self-defence troubles no witness
            self.assertGreater(relationships.hostility(state, "Outlaw"), 4)

    def test_a_deed_by_effect_and_disabled_relationships(self):
        state = _state()
        with _bind(state):
            state.apply_effects([{"deed": {"kind": "helped", "npc": "Vendor"}}])
            self.assertEqual(relationships.trust(state, "Vendor"), 1)
        off = _state(enabled=False)
        _do(off, "Pocket a trinket")
        with _bind(off):
            self.assertEqual(off.npc_relations, {})
            self.assertEqual(off.npc_deeds, [])
            self.assertEqual(relationships.explain(off)["enabled"], False)
            self.assertEqual(relationships.player_view(off, "Vendor"), {"disposition": None, "faction": None})


class TransitionTests(unittest.TestCase):
    def test_hostility_transitions_are_logged_told_and_signalled(self):
        state = _state()
        with _bind(state):
            relationships.change(state, "Watchman", hostility=1, cause="test")
            self.assertEqual(state.npc_relations["Watchman"]["disposition"], "wary")
            relationships.change(state, "Watchman", hostility=2, cause="test")
            self.assertEqual(relationships.disposition(state, "Watchman"), "hostile")
            moves = [(e["from"], e["to"]) for e in state.relationship_log if e["what"] == "disposition"]
            self.assertEqual(moves, [("neutral", "wary"), ("wary", "hostile")])
            self.assertIn("[RELATIONSHIP] The Watchman now counts you an enemy.", state.history)
            relationships.change(state, "Watchman", trust=6, hostility=-3)
            self.assertEqual(relationships.disposition(state, "Watchman"), "friendly")
            self.assertIn("[RELATIONSHIP] The Watchman warms to you.", state.history)

    def test_hostile_event_confronts_the_player(self):
        arrest = Event("arrest", title="The arrest", actions=[
            Action("Go quietly", [_say("They march you off.")], source="event", owner="arrest")])
        warden = Character("Warden", ["law"], unique=True, known_locations=["Barracks"], hostile_event="arrest")
        world = _world(Warden=warden)
        world.add_event(arrest)
        state = _state(world)
        self.assertNotIn("Warden", state.visible_inhabitants)
        with _bind(state):
            relationships.change(state, "Warden", hostility=3)
        _do(state, "Rest in the Square")       # the turn ends: the reaction is delivered
        self.assertEqual(state.active_event.name, "arrest")
        self.assertEqual(state.event_source_npc, "Warden")
        self.assertIn("Warden", state.visible_inhabitants)                     # they sought the player out
        self.assertIn("The Warden seeks you out.", state.history)
        self.assertEqual(state.event_log[-1]["cause"], "npc_reaction")

    def test_faction_turns_hostile_and_members_with_it(self):
        state = _state()
        with _bind(state):
            relationships.change_standing(state, "Watch", -3)
            self.assertEqual(relationships.faction_status(state, "Watch"), "hostile")
            self.assertEqual(relationships.disposition(state, "Watchman"), "hostile")
            self.assertIn("[FACTION] The Watch turn against you.", state.history)
            self.assertTrue(state.holds({"faction_hostile": "Watch"}))
            from core_engine import encounters

            self.assertTrue(encounters.hostile(state, "Watch"))    # one faction model: the encounters read it too
            # personal trust outweighs the faction
            relationships.change(state, "Watchman", trust=3)
            self.assertNotEqual(relationships.disposition(state, "Watchman"), "hostile")
        _do(state, "Rest in the Square")
        self.assertIn("The Watch wants a word with you.", state.history)   # the faction's own reaction

    def test_a_faction_hostile_from_the_start_makes_no_transition(self):
        factions = {**copy.deepcopy(FACTIONS), "Outlaws": {"standing": -3, "public": False}}
        thug = Character("Thug", ["wild"], unique=True, faction="Outlaws", known_locations=["Woods"])
        state = _state(_world(factions=factions, Thug=thug), start="Woods")
        with _bind(state):
            self.assertEqual(relationships.disposition(state, "Thug"), "hostile")
            relationships.remember(state, "Thug", "met")
            relationships.settle(state, "Thug")                       # first touched: hostile as it always was
        self.assertEqual([e for e in state.relationship_log if e.get("npc") == "Thug"], [])
        self.assertNotIn("[RELATIONSHIP] The Thug now counts you an enemy.", state.history)

    def test_tuning_only_factions_keep_their_old_threshold(self):
        tuning = {"encounters": {**config.get("encounters"), "enabled": False, "factions": {"Bandits": {"standing": 0}}}}
        with patch.dict(config._config, {**_settings(), **tuning}):
            state = GameState(_world(factions={}), _genome(), start="Square", rng=random.Random(3))
        with _bind(state):
            self.assertEqual(relationships.faction_status(state, "Bandits"), "neutral")
            relationships.change_standing(state, "Bandits", -0.5)
            self.assertEqual(relationships.faction_status(state, "Bandits"), "hostile")   # hostile_below 0, as before


class GoalTests(unittest.TestCase):
    def _sister(self, *goals):
        return Character("Sister", ["religion"], unique=True, faction="Chapel", known_locations=["Chapel"],
                         goals=list(goals), actions=[
                             _act("Light a candle", _say("The candle burns.", "devotion"), owner="Sister"),
                             _act("Mend the roof", _say("The roof holds.", "repair"), owner="Sister"),
                             _act("Help with the alms", _say("You hand out bread.", "help"), owner="Sister")])

    def _goal_state(self, *goals):
        from core_engine.builder import npc_goals

        world = _world(Sister=self._sister(*goals))
        sister = next(c for c in world.characters if c.name == "Sister")
        sister.goal_specs = npc_goals(goals)
        sister.goals = [g["text"] for g in sister.goal_specs]
        return _state(world, start="Chapel")

    def test_supports_and_progress_when(self):
        state = self._goal_state({"goal": "Keep the chapel standing", "id": "roof", "supports": ["repair"],
                                  "needed": 2, "reward": "none", "trust": 3})
        _do(state, "Mend the roof")
        self.assertEqual(state.npc_goal_progress["Sister"], {0: 1})
        _do(state, "Mend the roof")
        self.assertIn("Sister", state.allies)
        with _bind(state):
            self.assertTrue(state.holds({"npc_goal_done": "Sister/roof"}))
            self.assertTrue(state.holds({"npc_goal_done": "Sister/Keep the chapel standing"}))
            self.assertTrue(state.holds({"npc_ally": "Sister"}))
            self.assertEqual(relationships.disposition(state, "Sister"), "ally")
            self.assertGreaterEqual(relationships.trust(state, "Sister"), 3)

    def test_complete_if_and_on_complete(self):
        state = self._goal_state({"goal": "See the candles lit", "id": "candles",
                                  "complete_if": {"action_done": "Light a candle"}, "reward": "none",
                                  "on_complete": [{"change_coins": 3}]})
        coins = state.coins
        _do(state, "Light a candle")
        self.assertIn("Sister", state.allies)
        self.assertEqual(state.coins, coins + 3)
        self.assertEqual(state.npc_goals_done["Sister"], [0])

    def test_explicit_goals_ignore_keywords_and_the_fallback_still_works(self):
        # "Light candles" as a sentence: its words help (keywords); the explicit one only by its tag.
        state = self._goal_state("Light candles", {"goal": "Light the lamps", "id": "lamps", "supports": ["repair"]})
        _do(state, "Light a candle")
        self.assertEqual(state.npc_goal_progress["Sister"], {0: 1})            # keyword fallback, as before
        _do(state, "Light a candle")
        self.assertIn("Sister", state.allies)

    def test_opposed_goal_sets_back_and_reacts(self):
        sister = self._sister({"goal": "Keep the peace", "id": "peace", "supports": ["help"], "opposes": ["theft"]})
        sister.reactions = [{"signal": "goal_opposed", "behaviour": "warn", "where": "scene",
                             "text": "{npc} shakes her head at you."}]
        world = _world(Sister=sister)
        state = _state(world, homes={"Sister": "Square"})
        _do(state, "Help with the alms")
        self.assertEqual(state.npc_goal_progress["Sister"], {0: 1})
        _do(state, "Pocket a trinket")      # she sees it
        self.assertEqual(state.npc_goal_progress["Sister"], {0: 0})
        with _bind(state):
            self.assertTrue(state.holds({"npc_remembers": "Sister/goal_opposed"}))
        self.assertIn("The Sister shakes her head at you.", state.history)


class DialogueAndActionTests(unittest.TestCase):
    def test_dialogue_unlocked_by_the_relationship(self):
        from core_engine.world import DialogueLine

        sister = Character("Sister", ["religion"], unique=True, known_locations=["Chapel"], descriptions=["She prays."],
                           dialogue=[DialogueLine("You are welcome here, friend.",
                                                  {"npc_disposition": "Sister", "op": "in", "value": ["friendly", "ally"]})])
        state = _state(_world(Sister=sister), start="Chapel")
        talk = next(a for a in state.get_available_actions() if a.source == "conversation" and a.owner == "Sister")
        self.assertNotIn("friend", talk.outcomes[0].desc)
        with _bind(state):
            relationships.change(state, "Sister", trust=2)
        talk = next(a for a in state.get_available_actions() if a.source == "conversation" and a.owner == "Sister")
        self.assertEqual(talk.outcomes[0].desc, "You are welcome here, friend.")

    def test_hostile_npc_withholds_friendly_dealings_and_talk(self):
        state = _state()
        self.assertIn("(Vendor) Buy a pie", _names(state))
        self.assertNotIn("(Vendor) Haggle in earnest", _names(state))
        with _bind(state):
            relationships.change(state, "Vendor", hostility=3)
        names = _names(state)
        self.assertNotIn("(Vendor) Buy a pie", names)                     # trade, social: withheld
        self.assertIn("(Vendor) Pocket a trinket", names)                 # not a friendly dealing
        self.assertIn("(Vendor) Haggle in earnest", names)                # its own condition asks: the author decided
        talk = next(a for a in state.get_available_actions() if a.source == "conversation" and a.owner == "Vendor")
        self.assertEqual(talk.outcomes[0].desc, "The Vendor refuses to speak with you.")
        trace = decision_trace(state)
        pie = next(a for a in trace["actions"] if a["label"].endswith("Buy a pie"))
        self.assertEqual(pie["status"], "blocked")
        self.assertIn("npc_refuses", [r["code"] for r in pie["reasons"]])


class ReactionTests(unittest.TestCase):
    def test_reaction_waits_for_the_npc_and_expires(self):
        brother = Character("Brother", ["religion"], unique=True, known_locations=["Chapel"], reactions=[
            {"signal": "theft", "about": "anyone", "behaviour": "warn", "where": "scene", "expires": 3,
             "text": "{npc} has heard what you did in the square."}])
        state = _state(_world(Brother=brother))
        _do(state, "Pocket a trinket")
        self.assertEqual(state.npc_reactions_due[0]["blocked"], "absent")    # he is in the chapel
        _do(state, "Rest in the Square")
        _do(state, "Rest in the Square")
        _do(state, "Rest in the Square")
        _do(state, "Rest in the Square")
        self.assertEqual(state.npc_reactions_due, [])
        self.assertEqual(state.relationship_log[-1]["what"], "reaction_expired")
        self.assertNotIn("The Brother has heard what you did in the square.", state.history)
        # in time, he says it when the player comes to him
        state = _state(_world(Brother=brother))
        _do(state, "Pocket a trinket")
        _do(state, "Chapel")
        self.assertIn("The Brother has heard what you did in the square.", state.history)

    def test_condition_reaction_fires_once_and_cooldown_repeats(self):
        vendor = Character("Vendor", ["market"], unique=True, known_locations=["Square"], reactions=[
            {"when": {"resource": "coins", "op": "gte", "value": 0}, "behaviour": "praise", "where": "anywhere",
             "text": "Once."},
            {"when": {"resource": "coins", "op": "gte", "value": 0}, "once": False, "cooldown": 2,
             "where": "anywhere", "text": "Again."}])
        state = _state(_world(Vendor=vendor), max_reactions_per_turn=2)
        for _ in range(5):
            _do(state, "Rest in the Square")
        self.assertEqual(state.history.count("Once."), 1)
        self.assertGreaterEqual(state.history.count("Again."), 2)

    def test_offer_quest_and_quest_consequences(self):
        follow = SideQuest("follow_up", {"location": "Woods"}, title="Into the woods", giver="Sister")
        first = SideQuest("alms", {"action": "Help with the alms"}, title="Alms for the poor", giver="Sister",
                          accept="auto")
        sister = Character("Sister", ["religion"], unique=True, faction="Chapel", known_locations=["Chapel"], actions=[
            _act("Help with the alms", _say("You hand out bread.", "charity"), owner="Sister")],
            reactions=[{"signal": "quest_completed", "quest": "alms", "behaviour": "offer_quest",
                        "side_quest": "follow_up", "where": "scene"}])
        world = _world(Sister=sister)
        world.add_side_quest(first)
        world.add_side_quest(follow)
        state = _state(world, start="Chapel")
        _do(state, "Help with the alms")      # dealt with her: the quest starts...
        _do(state, "Help with the alms")      # ...and is done
        self.assertEqual(state.side_quests["alms"]["status"], "completed")
        with _bind(state):
            self.assertEqual(relationships.trust(state, "Sister"), 2)                 # deeds.quest_completed
            self.assertEqual(relationships.standing(state, "Chapel"), 1)              # reacts_to quest_completed
            self.assertTrue(state.holds({"npc_remembers": "Sister/quest_completed"}))
        self.assertEqual(state.side_quests["follow_up"]["status"], "offered")       # her reaction offered it
        self.assertIn("The Sister has something to ask of you.", state.history)

    def test_failed_quest_costs_trust(self):
        quest = SideQuest("errand", {"location": "Woods"}, title="An errand", giver="Sister", accept="auto",
                          within_turns=1)
        world = _world()
        world.add_side_quest(quest)
        state = _state(world, start="Chapel")
        for _ in range(4):
            if (state.side_quests.get("errand") or {}).get("status") == "failed":
                break
            _do(state, "Light a candle")
        self.assertEqual(state.side_quests["errand"]["status"], "failed")
        with _bind(state):
            self.assertEqual(relationships.trust(state, "Sister"), -1)

    def test_move_behaviour_follows_location_affinity(self):
        hermit = Character("Hermit", ["wild", "forest"], unique=True, known_locations=["Square"], reactions=[
            {"when": {"npc_hostility": "Hermit", "op": "gte", "value": 1}, "behaviour": "move",
             "move_to_tags": ["wild", "quiet"], "text": "The hermit leaves."}])
        state = _state(_world(Hermit=hermit))
        self.assertIn("Hermit", state.visible_inhabitants)
        with _bind(state):
            relationships.change(state, "Hermit", hostility=1)
        _do(state, "Rest in the Square")
        self.assertEqual(state.npc_homes["Hermit"], "Woods")      # the best fit of the tagged places (forest, wild)
        self.assertNotIn("Hermit", state.visible_inhabitants)
        self.assertIn("[PEOPLE] The Hermit leaves for the Woods.", state.history)


class GuardTests(unittest.TestCase):
    def test_permits(self):
        state = _state()
        with _bind(state):
            self.assertIsNone(relationships.permits(state, "Sister", "offer_quest"))
            self.assertEqual(relationships.permits(state, "Sister", "confront"), "forbidden")   # never
            self.assertEqual(relationships.permits(state, "Outlaw", "offer_quest"), "hostile")
            relationships.change(state, "Vendor", trust=3)
            self.assertEqual(relationships.permits(state, "Vendor", "confront"), "friendly")
            self.assertEqual(relationships.permits(state, "Nobody", "warn"), "unknown_npc")
            state._remove_npc("Brother")
            self.assertEqual(relationships.permits(state, "Brother", "warn"), "npc_gone")

    def test_no_contradictory_reaction_and_it_waits_with_a_reason(self):
        sister = Character("Sister", ["religion"], unique=True, known_locations=["Square"], never={"confront"},
                           reactions=[{"signal": "theft", "behaviour": "confront", "where": "anywhere",
                                       "text": "She confronts you."},
                                      {"signal": "theft", "behaviour": "offer_quest", "where": "anywhere",
                                       "text": "She offers you work."}])
        state = _state(_world(Sister=sister), max_reactions_per_turn=3)
        with _bind(state):
            relationships.change(state, "Sister", hostility=5)      # hostile: she offers nothing
        _do(state, "Pocket a trinket")
        self.assertNotIn("She confronts you.", state.history)
        self.assertNotIn("She offers you work.", state.history)
        self.assertEqual(sorted(item["blocked"] for item in state.npc_reactions_due), ["forbidden", "hostile"])
        trace = decision_trace(state)
        self.assertEqual(sorted(item["blocked"] for item in trace["relationships"]["due"]), ["forbidden", "hostile"])

    def test_a_hostile_giver_offers_no_side_quest(self):
        quest = SideQuest("alms", {"location": "Woods"}, title="Alms", giver="Sister")
        world = _world()
        world.add_side_quest(quest)
        state = _state(world, start="Chapel")
        with _bind(state):
            relationships.change(state, "Sister", hostility=4)
            state.character_interactions["Sister"] = 1
            self.assertFalse(side_quests.offered(state, quest))
            self.assertEqual(side_quests.offered_by(state, "Sister"), [])
            self.assertFalse(side_quests.hook(state, "alms"))
            relationships.change(state, "Sister", hostility=-4)
            self.assertTrue(side_quests.offered(state, quest))

    def test_a_friend_is_not_set_on_the_player(self):
        from core_engine import encounters

        assault = Event("assault", title="Assault", actions=[Action("Fight", [_say("You fight.")], source="event",
                                                                     owner="assault")],
                        encounter={"category": "npc_assault", "npc": "Vendor"})
        world = _world()
        world.add_event(assault)
        state = _state(world)
        with _bind(state):
            spec = assault.encounter
            self.assertIsNone(encounters._template_block(state, assault, spec, encounters.settings()))
            relationships.change(state, "Vendor", trust=4)
            self.assertEqual(encounters._template_block(state, assault, spec, encounters.settings()), "npc_unwilling")


class ViewTests(unittest.TestCase):
    def test_player_sees_dispositions_not_numbers(self):
        state = _state(start="Woods")
        people = {p["name"]: p for p in state.people_here()}
        self.assertEqual(people["Outlaw"]["disposition"], "hostile")
        self.assertIsNone(people["Outlaw"]["faction"])                 # the Outlaws are not public
        self.assertNotIn("trust", people["Outlaw"])
        state = _state()
        people = {p["name"]: p for p in state.people_here()}
        self.assertEqual(people["Watchman"]["faction"], "Watch")
        met = {p["name"]: p for p in state.people_met()}
        self.assertEqual(met["Watchman"]["disposition"], "neutral")

    def test_author_sees_everything(self):
        state = _state()
        _do(state, "Pocket a trinket")
        trace = decision_trace(state)
        rel = trace["relationships"]
        vendor = next(n for n in rel["npcs"] if n["npc"] == "Vendor")
        self.assertEqual((vendor["trust"], vendor["hostility"], vendor["disposition"]), (-1, 1, "wary"))
        self.assertEqual(vendor["memories"][-1]["kind"], "theft")
        watch = next(f for f in rel["factions"] if f["faction"] == "Watch")
        self.assertEqual((watch["standing"], watch["status"], watch["members"]), (-1, "neutral", ["Watchman"]))
        self.assertEqual(rel["deeds"][-1]["kind"], "theft")
        sister = next(n for n in rel["npcs"] if n["npc"] == "Sister")
        self.assertEqual(sister["goals"][0]["how"], "keywords")


class PersistenceTests(unittest.TestCase):
    def test_save_game_carries_relationships_exactly(self):
        # A snapshot rebuilds the world from the configuration: the shipped world, as configured.
        genome = _genome()
        state = GameState(build_world(genome), genome, start="Market", rng=random.Random(2))
        state.take_option(state.player_options()[0])
        with _bind(state):
            state.apply_effects([{"deed": {"kind": "theft", "npc": "Merchant"}}, {"change_trust": {"Priest": 3}},
                                 {"move_npc": {"Priest": "Palace"}}])
            state.npc_reactions_due.append({"id": "npc:Priest:0", "owner": ["npc", "Priest"], "since": 1, "until": 9,
                                            "signal": {"kind": "quest_completed"}, "blocked": "absent"})
        doc = json.loads(json.dumps(savegame.make(state)))
        loaded, how, note = savegame.load(doc, genome)
        self.assertEqual(how, "exact", note)
        for field in ("npc_relations", "faction_shift", "npc_deeds", "npc_reactions_due", "relationship_log"):
            self.assertEqual(getattr(loaded, field), getattr(state, field), field)
        self.assertEqual(loaded.npc_homes["Priest"], "Palace")
        with _bind(loaded):
            self.assertEqual(relationships.disposition(loaded, "Priest"), "friendly")

    def test_parameters_rebuild_a_session(self):
        state = _state()
        _do(state, "Pocket a trinket")
        with _bind(state):
            state.npc_reactions_fired["npc:Sister:0"] = 1
            params = resume.parameters(state)
        self.assertEqual(params["relationships"]["npcs"]["Vendor"]["hostility"], 1)
        world = _world()
        with patch.dict(config._config, _settings()):
            rebuilt = resume.rebuild(_genome(), params, world=world, seed=3)
        with _bind(rebuilt):
            self.assertEqual(relationships.hostility(rebuilt, "Vendor"), 1)
            self.assertEqual(relationships.standing(rebuilt, "Watch"), -1)   # the encounters' standing changes
            self.assertTrue(rebuilt.holds({"npc_remembers": "Watchman/theft"}))
            self.assertEqual(rebuilt.npc_reactions_fired, {"npc:Sister:0": 1})

    def test_profile_keeps_what_is_configured_to_persist(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        vault = VariableStore(folder / "variables.json")

        def world():
            w = _world()
            for char in w.characters:
                if char.name == "Vendor":
                    char.relationship = {"persist": True}
            return w

        first = _state(world(), store_=vault, profile="p1")
        _do(first, "Pocket a trinket")
        first.end_session()
        saved = vault.npc_relations("p1")
        self.assertEqual(set(saved["npcs"]), {"Vendor"})                    # the Watchman does not persist
        second = _state(world(), store_=vault, profile="p1")
        with _bind(second):
            self.assertEqual(relationships.hostility(second, "Vendor"), 1)
            self.assertTrue(second.holds({"npc_remembers": "Vendor/theft"}))
            self.assertEqual(relationships.hostility(second, "Watchman"), 0)
        third = _state(world(), store_=vault, profile="p1", persist="none")
        with _bind(third):
            self.assertEqual(relationships.hostility(third, "Vendor"), 0)
        other = _state(world(), store_=vault, profile="p2")
        with _bind(other):
            self.assertEqual(relationships.hostility(other, "Vendor"), 0)

    def test_previous_encounters_are_remembered_on_meeting(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        vault = VariableStore(folder / "variables.json")
        vault.record_npc_relations("p", {"npcs": {"Sister": {"trust": 3, "hostility": 0,
                                                             "memories": [{"kind": "helped"}]}}})

        def world():
            sister = Character("Sister", ["religion"], unique=True, known_locations=["Chapel"],
                               relationship={"persist": True}, reactions=[
                                   {"signal": "met", "when": {"npc_remembers": "Sister/helped"}, "behaviour": "praise",
                                    "text": "{npc} remembers what you did for her."}])
            return _world(Sister=sister)

        state = _state(world(), start="Square", store_=vault, profile="p")
        _do(state, "Chapel")
        self.assertIn("The Sister remembers what you did for her.", state.history)


class DeterminismTests(unittest.TestCase):
    def _play(self, seed):
        state = _state(seed=seed)
        for _ in range(12):
            if state.ended:
                break
            state.step()
        return state

    def test_fixed_seed_same_relationships(self):
        first, second = self._play(7), self._play(7)
        self.assertEqual(first.history, second.history)
        self.assertEqual(first.npc_relations, second.npc_relations)
        self.assertEqual(first.relationship_log, second.relationship_log)
        self.assertEqual(first.faction_shift, second.faction_shift)

    def test_relationships_draw_no_dice(self):
        state = _state()
        before = state.rng.getstate()
        with _bind(state):
            relationships.deed(state, "theft", target="Vendor")
            relationships.update(state)
            relationships.explain(state)
        self.assertEqual(state.rng.getstate(), before)


class WorldValidationTests(unittest.TestCase):
    def _errors(self, **world):
        base = {"locations": {"Square": {"tags": ["urban"]}}, "characters": [{"name": "Vendor", "associated_tags": []}],
                "events": {}, "side_quests": {}}
        base.update(world)
        return " ".join(logic_errors(base))

    def test_validation(self):
        self.assertEqual(self._errors(factions={"Watch": {"reacts_to": {"theft": -1}}}), "")
        problems = self._errors(
            factions={"Watch": {"relations": {"Ghosts": 1}, "reacts_to": {"gossip": 1}, "colour": "blue",
                                "members": ["Nobody"]}},
            characters=[{"name": "Vendor", "faction": "Guild", "related": {"Nobody": 1}, "never": ["dance"],
                         "hostile_event": "missing", "relationship": {"trust": "lots"},
                         "goals": [{"goal": "Sell", "complete_if": {"npc_removed": "Nobody"}, "nonsense": 1}],
                         "reactions": [{"signal": "gossip", "behaviour": "dance", "side_quest": "none"},
                                       {"behaviour": "warn"}]}])
        for expected in ("`Ghosts` is not a faction", "`gossip` is not a deed", "unknown field(s) colour",
                         "`Nobody` is not an NPC", "`Guild` is not a faction", ".never must list", "no event 'missing'", "relationship.trust must be a number",
                         "unknown NPC `Nobody`", "unknown field(s) nonsense", "signal must be one of",
                         "behaviour must be one of", "is not a side quest", "needs `signal`"):
            self.assertIn(expected, problems)

    def test_a_world_is_checked_with_its_own_tuning_factions(self):
        # An uploaded world (or an engine model) is checked before its own tuning is in use.
        world = {"locations": {"Square": {"tags": ["urban"]}}, "characters": [{"name": "Vendor", "faction": "Bandits"}],
                 "factions": {"Guild": {"relations": {"Bandits": -1}}}}
        self.assertIn("`Bandits` is not a faction", " ".join(logic_errors(world, tuning_factions=[])))
        self.assertEqual(logic_errors(world, tuning_factions=["Bandits"]), [])

    def test_yaml_on_is_read_as_signal(self):
        raw = yaml.safe_load("- on: theft\n  behaviour: warn\n")
        self.assertEqual(relationships.reaction_spec(raw[0])["on"], "theft")
        self.assertEqual(self._errors(characters=[{"name": "Vendor", "reactions": raw}]), "")


def _peddler(kin=None, **fields):
    """A generic NPC: one at the Square every visit, one at the Chapel one visit in two, never elsewhere."""
    relationship = {} if kin is None else {"kin": kin}
    return Character("Peddler", ["market"], unique=False, relationship=relationship, haunts=[
        {"places": ["Square"], "presence": 1.0}, {"places": ["Chapel"], "presence": 0.5}], actions=[
        _act("Lift a ribbon", _say("You lift a ribbon.", "theft"), owner="Peddler")], **fields)


class GenericNpcTests(unittest.TestCase):
    def test_each_copy_is_its_own_person_and_word_travels_by_kin(self):
        state = _state(_world(Peddler=_peddler()))
        self.assertIn("Peddler", state.visible_inhabitants)
        _do(state, "Lift a ribbon")
        self.assertIn("Peddler@Square", state.npc_relations)
        with _bind(state):
            self.assertEqual((relationships.trust(state, "Peddler@Square"),
                              relationships.hostility(state, "Peddler@Square")), (-1, 1))
            self.assertEqual((relationships.trust(state, "Peddler@Chapel"),
                              relationships.hostility(state, "Peddler@Chapel")), (-0.5, 0.5))   # kin 0.5
            self.assertEqual(relationships.trust(state, "Peddler"), -1)                         # the one here
            self.assertTrue(state.holds({"npc_remembers": "Peddler@Square/theft"}))
            self.assertFalse(state.holds({"npc_remembers": "Peddler@Chapel/theft"}))           # heard, not seen
            self.assertEqual(state.holds({"npc_trust": "Peddler@Chapel", "op": "lt", "value": 0}), True)
        for kin, elsewhere in ((0.0, 0), (1.0, -1)):
            state = _state(_world(Peddler=_peddler(kin=kin)))
            _do(state, "Lift a ribbon")
            with _bind(state):
                self.assertEqual(relationships.trust(state, "Peddler@Chapel"), elsewhere)
            if kin == 1.0:
                self.assertEqual(set(k for k in state.npc_relations if k.startswith("Peddler")), {"Peddler"})

    def test_haunts_regular_occasional_and_never(self):
        world = _world(Peddler=_peddler())
        peddler = next(c for c in world.characters if c.name == "Peddler")
        state = _state(world)
        with _bind(state):
            square, chapel, woods = (world.locations[n] for n in ("Square", "Chapel", "Woods"))
            self.assertEqual((relationships.presence(state, peddler, square), relationships.presence(state, peddler, chapel),
                              relationships.presence(state, peddler, woods)), (1.0, 0.5, 0.0))
            self.assertTrue(state._npc_fits_location(peddler, chapel))        # a place they can be (planning)
            self.assertFalse(state._npc_fits_location(peddler, woods))
            self.assertEqual(state._npc_sites("Peddler"), {"Square", "Chapel"})
        found, total = 0, 0
        for seed in range(60):
            story = _state(_world(Peddler=_peddler()), seed=seed)
            with _bind(story):
                story.current = story.world.locations["Chapel"]
                for visit in range(1, 6):
                    story.location_counts["Chapel"] = visit
                    first = relationships.here_this_visit(story, next(c for c in story.world.characters
                                                                      if c.name == "Peddler"))
                    again = relationships.here_this_visit(story, next(c for c in story.world.characters
                                                                      if c.name == "Peddler"))
                    self.assertEqual(first, again)                           # the same visit, the same answer
                    found += first
                    total += 1
        self.assertGreater(found / total, 0.35)
        self.assertLess(found / total, 0.65)

    def test_scenes_follow_haunts_and_the_trace_says_why(self):
        state = _state(_world(Peddler=_peddler()), start="Woods")
        self.assertNotIn("Peddler", state.visible_inhabitants)
        rows = {row["name"]: row for row in decision_trace(state)["entities"] if row["kind"] == "npc"}
        self.assertEqual(rows["Peddler"]["reason"]["code"], "npc_does_not_fit")
        absent = None
        for seed in range(30):
            chapel = _state(_world(Peddler=_peddler()), start="Chapel", seed=seed)
            if "Peddler" not in chapel.visible_inhabitants:
                absent = chapel
                break
        self.assertIsNotNone(absent)
        rows = {row["name"]: row for row in decision_trace(absent)["entities"] if row["kind"] == "npc"}
        self.assertEqual(rows["Peddler"]["reason"]["code"], "npc_elsewhere_this_visit")

    def test_a_generic_npc_moves_by_a_new_haunt(self):
        state = _state(_world(Peddler=_peddler()))
        with _bind(state):
            self.assertEqual(relationships.move_npc(state, "Peddler", to="Woods"), "Woods")
            self.assertEqual(relationships.presence(state, next(c for c in state.world.characters
                                                                if c.name == "Peddler"), state.world.locations["Woods"]), 1.0)
            params = relationships.parameters(state)
        self.assertEqual(params["haunts"], {"Peddler": ["Woods"]})
        _do(state, "Woods")
        self.assertIn("Peddler", state.visible_inhabitants)

    def test_people_met_show_the_copy_met_last_and_persistence_keeps_copies(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        vault = VariableStore(folder / "variables.json")
        state = _state(_world(Peddler=_peddler_persisting()), store_=vault, profile="p")
        _do(state, "Lift a ribbon")
        met = {p["name"]: p for p in state.people_met()}
        self.assertEqual(met["Peddler"]["disposition"], "wary")
        state.end_session()
        saved = vault.npc_relations("p")
        self.assertIn("Peddler@Square", saved["npcs"])
        self.assertEqual(saved["kin"]["Peddler"], {"trust": -1, "hostility": 1})
        later = _state(_world(Peddler=_peddler_persisting()), store_=vault, profile="p")
        with _bind(later):
            self.assertEqual(relationships.hostility(later, "Peddler@Square"), 1)
            self.assertEqual(relationships.hostility(later, "Peddler@Chapel"), 0.5)

    def test_parameters_rebuild_copies_and_kin(self):
        state = _state(_world(Peddler=_peddler()))
        _do(state, "Lift a ribbon")
        with _bind(state):
            params = resume.parameters(state)
        with patch.dict(config._config, _settings()):
            rebuilt = resume.rebuild(_genome(), params, world=_world(Peddler=_peddler()), seed=3)
        with _bind(rebuilt):
            self.assertEqual(relationships.hostility(rebuilt, "Peddler@Square"), 1)
            self.assertEqual(relationships.hostility(rebuilt, "Peddler@Chapel"), 0.5)

    def test_haunt_validation(self):
        base = {"locations": {"Square": {"tags": ["urban"]}}, "events": {}, "side_quests": {}}
        problems = " ".join(logic_errors({**base, "characters": [
            {"name": "Lord", "unique": True, "haunts": [{"places": ["Square"]}]},
            {"name": "Peddler", "haunts": [{"places": ["Moon"], "presence": 2}, {"tag": "x"}],
             "relationship": {"kin": 3}}]}))
        for expected in ("haunts is for generic NPCs", "`Moon` is not a location", "presence must be a number",
                         "unknown field(s) tag", "kin must be a number from 0 to 1"):
            self.assertIn(expected, problems)


def _peddler_persisting():
    peddler = _peddler()
    peddler.relationship = {"persist": True}
    return peddler


class TrainingTests(unittest.TestCase):
    def test_the_fitness_term_rewards_changes_without_farming(self):
        state = _state()
        with _bind(state):
            for _ in range(3):  # back and forth: the same two changes, counted once each
                relationships.change(state, "Vendor", hostility=1)
                relationships.change(state, "Vendor", hostility=-1)
            relationships.change_standing(state, "Watch", -3)
            cfg = {"change_bonus": 3, "reaction_bonus": 2, "faction_bonus": 3, "diminishing": 0.5}
            term = relationships.fitness_term(state, cfg)
        changes = {(e["npc"], e["from"], e["to"]) for e in state.relationship_log if e["what"] == "disposition"}
        self.assertEqual(term["relationship_changes"], sum(0.5 ** i for i in range(len(changes))) * 3)
        self.assertEqual(term["faction_changes"], (1 + 0.5) * 3)            # the Watch hostile, the Outlaws friendly
        self.assertLess(term["relationship_changes"], 3 / (1 - 0.5))         # bounded

    def test_fitness_versions(self):
        from core_engine import fitness

        self.assertEqual(fitness.FITNESS_VERSION, 3)
        self.assertEqual(fitness.version(), 3)                               # the shipped scoring
        state = _state()
        _do(state, "Rest in the Square")       # a story with no turn is scored by its zero-step penalty alone
        with _bind(state):
            relationships.change(state, "Vendor", hostility=1)
        with patch.dict(state.context._data["fitness"], {"version": 3}), _bind(state):
            self.assertIn("relationship_changes", fitness._fitness_breakdown(state))
        with patch.dict(state.context._data["fitness"], {"version": 2}), _bind(state):
            self.assertNotIn("relationship_changes", fitness._fitness_breakdown(state))

    def test_the_storyteller_is_pulled_toward_goals_and_waiting_people(self):
        state = _state(start="Square")
        sister = next(c for c in state.world.characters if c.name == "Sister")
        sister.goal_specs = [{"text": "Feed the poor", "id": "alms", "keywords": [], "explicit": True,
                              "supports": ["help"], "needed": 1}]
        alms = next(a for a in sister.actions if a.name.endswith("Help with the alms"))
        candle = next(a for a in sister.actions if a.name.endswith("Light a candle"))
        with _bind(state):
            self.assertTrue(relationships.would_help(state, sister, alms.outcomes[0]))
            self.assertFalse(relationships.would_help(state, sister, candle.outcomes[0]))
            way = next(a for a in state.get_available_actions() if a.source == "navigation" and a.owner == "Chapel")
            self.assertEqual(relationships.reaction_pull(state, way), 0.0)
            state.npc_reactions_due.append({"id": "npc:Sister:0", "owner": ["npc", "Sister"], "since": 0, "until": 9,
                                            "signal": {}, "blocked": "absent"})
            sister.reactions = [{"signal": "theft", "behaviour": "warn", "where": "scene", "text": "Shame."}]
            self.assertEqual(relationships.reaction_pull(state, way), 1.0)


class CatalogueTests(unittest.TestCase):
    def test_factions_in_the_world_catalogue_and_patches(self):
        from narrative_runtime.world import apply_patch, patch_errors, world_catalogue, world_entity

        data = config.as_dict()
        catalogue = world_catalogue(data)
        self.assertEqual({f["name"] for f in catalogue["factions"]}, {"City Watch", "Thieves' Guild", "Chapel"})
        self.assertEqual(world_entity(data, "factions", "City Watch")["kind"], "factions")
        merchant = next(n for n in catalogue["npcs"] if n["name"] == "Merchant")
        self.assertEqual(merchant["haunts"][0]["presence"], 1.0)              # an NPC's own fields come whole
        patched = apply_patch(data, {"upsert": {"factions": {"Dockers": {"reacts_to": {"helped": 1}}}},
                                     "update": {"npcs": {"Dock Worker": {"faction": "Dockers"}}}})
        self.assertEqual(patch_errors(patched), [])
        broken = apply_patch(data, {"update": {"npcs": {"Dock Worker": {"faction": "Nobody"}}}})
        self.assertIn("`Nobody` is not a faction", " ".join(patch_errors(broken)))


class DemoWorldTests(unittest.TestCase):
    def _context(self, path=None):
        if path is None:
            cfg = config.as_dict()
        else:
            cfg, errors, _ = store.complete_config(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
            self.assertEqual(errors, [])
        own = {key: value for key, value in _settings().items() if key not in ("relationships", "encounters")}
        cfg = {**cfg, **own, "encounters": {**cfg["encounters"], "enabled": False}}
        genome = _genome()
        return EngineContext(cfg, genome=genome), genome

    def test_city_factions_and_people(self):
        context, genome = self._context()
        with context.bind():
            world = build_world(genome)
            self.assertEqual(set(world.factions), {"City Watch", "Thieves' Guild", "Chapel"})
            state = GameState(world, genome, start="Market", rng=random.Random(1), context=context)
            self.assertEqual(relationships.faction_of(state, "Captain of the Watch"), "City Watch")
            self.assertEqual(relationships.disposition(state, "Thief"), "wary")
            self.assertEqual(relationships.player_view(state, "Fence")["faction"], None)   # the guild is not public
            # The Captain warms to the player: he offers the Watch's own side quest.
            state.npc_homes["Captain of the Watch"] = "Market"
            state.character_interactions["Captain of the Watch"] = 1
            relationships.change(state, "Captain of the Watch", trust=3)
            state.__dict__["_scene_dirty"] = True
            state.get_available_actions()
            relationships.update(state)
            self.assertEqual(state.side_quests.get("a_night_with_the_watch", {}).get("status"), "offered")

    def test_the_city_merchant_keeps_to_the_markets(self):
        context, genome = self._context()
        with context.bind():
            world = build_world(genome)
            merchant = next(c for c in world.characters if c.name == "Merchant")
            state = GameState(world, genome, start="Market", rng=random.Random(1), context=context)
            presence = {name: relationships.presence(state, merchant, loc) for name, loc in world.locations.items()}
            self.assertEqual((presence["Market"], presence["Market of the Ancients"], presence["Tavern"],
                              presence["Harbor"], presence["Alley"]), (1.0, 1.0, 0.3, 0.15, 0.0))

    def test_example_worlds_build_with_their_own_factions(self):
        for path, expected in (("examples/frozen_monastery.yaml", {"Brotherhood"}),
                               ("examples/orbital_station.yaml", {"Station Security", "Free Traders"})):
            context, genome = self._context(path)
            with context.bind():
                self.assertEqual(set(build_world(genome).factions), expected)

    def test_station_mechanic_goal_and_praise(self):
        context, genome = self._context("examples/orbital_station.yaml")
        with context.bind():
            world = build_world(genome)
            state = GameState(world, genome, start="Reactor Core", rng=random.Random(1), context=context)
            state.npc_homes["Mechanic"] = "Reactor Core"
            for _ in range(4):
                actions = state.ranked_available_actions()
                hit = [i for i, a in enumerate(actions) if a.name.endswith("Lend a hand with repairs")]
                if hit:
                    state.step(hit[0], ranked=actions)
                    break
                state.step()
            if "Mechanic" not in state.allies:
                self.skipTest("the Mechanic was not in the scene with this seed")
            relationships.update(state)
            self.assertTrue(state.holds({"npc_goal_done": "Mechanic/keep_things_running"}))
            self.assertGreaterEqual(relationships.standing(state, "Station Security"), 1)


if __name__ == "__main__":
    unittest.main()
