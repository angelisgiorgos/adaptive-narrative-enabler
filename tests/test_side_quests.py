"""Side quests (core_engine/side_quests.py, world_definition.side_quests): optional quests besides the
story's mission. Offered by someone (giver) or when a condition holds, several under way at once,
advanced in steps with rewards, completed or failed (fail_if, on_fail), never the end of the story;
once per profile unless repeatable; guided among the player's choices; carried by snapshots, save
games and the UNP's parameters; validated like missions; shown in the API, the runtime and the studio."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import copy
import json
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import main as engine
from backend import content, sessions
from core_engine import (Action, Character, GameState, Genome, Location, Mission, Object, Outcome, SideQuest, WorldGraph,
                         choices, content_rules, resume, savegame, side_quests, snapshot)
from core_engine.diagnostics import REASONS, decision_trace
from core_engine.variables import VariableStore
from utils.config_loader import config


def _genome():
    random.seed(9)
    genome = Genome()
    genome.event_prob = 0.0
    genome.chaos_bias = 0.0
    return genome


def _action(label, text, owner, npc=None, **kwargs):
    outcome = kwargs.pop("outcome", None) or Outcome(text, tags=["social" if npc else "neutral"])
    action = Action(f"({npc}) {label}" if npc else label, [outcome], character_name=npc,
                    source="npc" if npc else "location", owner=npc or owner, repeat="always", **kwargs)
    action.key = label
    return action


def _world(*quests, mission=None):
    """A gate with the Priest, a cellar with wine, a yard; the side quests given."""
    world = WorldGraph()
    for name in ("Gate", "Cellar", "Yard"):
        world.add_location(Location(name, [name.lower()]))
    world.connect("Gate", "Cellar")
    world.connect("Gate", "Yard")
    world.locations["Gate"].actions = [_action("Wait", "You wait.", "Gate")]
    world.locations["Cellar"].actions = [_action("Take a bottle", "You take a bottle.", "Cellar",
                                                 outcome=Outcome("You take a bottle.", tags=["success"],
                                                                 effects=[{"give_item": "Vintage Wine"}]))]
    world.locations["Yard"].actions = [_action("Rest", "You rest.", "Yard")]
    world.add_object(Object("Vintage Wine", ["drink"]))
    world.add_character(Character("Priest", ["gate"], unique=True, actions=[
        _action("Greet", "The Priest nods.", "Gate", npc="Priest")]))
    for quest in quests:
        world.add_side_quest(quest)
    if mission:
        world.add_mission(mission)
    return world


def _state(world, **kwargs):
    state = GameState(world, _genome(), start="Gate", rng=random.Random(3), **kwargs)
    for char in world.characters:
        state.npc_homes[char.name] = "Gate"
    state._scene_dirty = True
    return state


def _take(state, prefix):
    """Take the offered choice whose id starts with ``prefix`` (a turn)."""
    options = state.player_options()
    ids = choices.assign_ids(options, state)
    chosen = next(option for option, action_id in zip(options, ids) if action_id.startswith(prefix))
    state.take_option(chosen)
    return state


def _turn(state):
    """Take the first choice that is a turn (not a side quest's free answer)."""
    state.take_option(next(option for option in state.player_options() if option.source != "side_quest"))
    return state


def _wine(**kwargs):
    return SideQuest("gift", {"item": "Vintage Wine"}, title="A gift for the chapel", giver="Priest",
                     rewards=[{"change_coins": 6}], **kwargs)


class _Quiet(unittest.TestCase):
    """Side quests start at once here (accept: auto); OfferTests take them on as a choice."""

    ACCEPT = "auto"

    def setUp(self):
        for patcher in (
            patch.dict(config._config, {"side_quest_settings": {**config.get("side_quest_settings"),
                                                                "accept": self.ACCEPT}}),
            patch.dict(config._config["unexpected_events"], {"enabled": False}),
            patch.dict(config._config, {"npc_roles": {**(config.get("npc_roles") or {}), "enabled": False}}),
            patch.dict(config._config["conversations"], {"enabled": False}),
            # The side quests' own rules (the side-story policy on top of them: tests/test_side_story.py).
            patch.dict(config._config, {"side_story": {**config.get("side_story"), "enabled": False}}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


class LifecycleTests(_Quiet):
    def test_a_giver_offers_it_and_it_is_completed_with_its_rewards(self):
        state = _state(_world(_wine()))
        self.assertEqual(state.side_quest_log(), [])
        state.player_options()  # the scene is set
        self.assertEqual(next(p for p in state.people_here() if p["name"] == "Priest")["side_quests"],
                         ["A gift for the chapel"])                                       # offered, not started
        _take(state, "go:yard")
        self.assertEqual(state.side_quests, {})                                          # not without the giver
        _take(state, "go:gate")
        _take(state, "npc:priest:greet")
        self.assertEqual(state.side_quests["gift"]["status"], "active")
        self.assertIn("[SIDE QUEST] the Priest asks a favour of you: A gift for the chapel.", "\n".join(state.history))
        self.assertEqual(next(p for p in state.people_here() if p["name"] == "Priest")["side_quests"], [])
        coins = state.coins
        _take(state, "go:cellar")
        _take(state, "location:cellar:take-a-bottle")
        self.assertEqual(state.side_quests["gift"]["status"], "completed")
        self.assertEqual(state.coins, coins + 6)                                          # the reward
        self.assertIn("[SIDE QUEST COMPLETE] A gift for the chapel", state.history)
        self.assertFalse(state.ended)                                                     # never the end
        entry = state.side_quest_log()[0]
        self.assertEqual((entry["status"], entry["giver"], entry["started"]), ("completed", "Priest", 3))
        events = [event for turn in state.journal() for event in turn["side_quests"]]
        self.assertEqual([e["what"] for e in events], ["started", "completed"])

    def test_steps_their_rewards_and_failure(self):
        steps = SideQuest("errand", None, title="An errand", start={"visited": "Yard"}, stages=[
            {"title": "Fetch the wine", "goal": {"item": "Vintage Wine"}, "rewards": [{"change_coins": 2}]},
            {"title": "Back to the gate", "goal": {"condition": {"at_location": "Gate"}}}],
            fail_if={"resource": "coins", "op": "lt", "value": 0}, on_fail=[{"change_health": -1}])
        state = _state(_world(steps))
        _take(state, "go:yard")
        self.assertLessEqual({"status": "active", "stage": 0, "started": 1, "ended": None}.items(),
                             state.side_quests["errand"].items())
        _take(state, "go:gate")
        _take(state, "go:cellar")
        coins = state.coins
        _take(state, "location:cellar:take-a-bottle")
        self.assertEqual((state.side_quests["errand"]["stage"], state.coins), (1, coins + 2))  # a step and its reward
        self.assertEqual(side_quests.pending(state, "errand"), {"condition": {"at_location": "Gate"}})
        self.assertEqual([s["state"] for s in state.side_quest_log()[0]["stages"]], ["done", "current"])
        _take(state, "go:gate")
        self.assertEqual(state.side_quests["errand"]["status"], "completed")

        failing = SideQuest("debt", {"coins": 999}, title="A debt", fail_if={"visited": "Yard"},
                            on_fail=[{"change_health": -2}])
        state = _state(_world(failing))
        _take(state, "location:gate:wait")
        self.assertEqual(state.side_quests["debt"]["status"], "active")
        health = state.health
        _take(state, "go:yard")
        self.assertEqual(state.side_quests["debt"]["status"], "failed")
        self.assertLess(state.health, health)                                        # on_fail (damage as scaled)
        self.assertIn("[SIDE QUEST FAILED] A debt", state.history)
        self.assertEqual(side_quests.summary(state), {"completed": [], "failed": ["debt"], "declined": [],
                                                      "unfinished": [], "aided_mission": []})

    def test_several_at_once_up_to_the_limit_and_beside_the_mission(self):
        quests = [SideQuest(f"q{n}", {"location": "Yard"}, title=f"Quest {n}") for n in range(3)]
        mission = Mission("main", {"location": "Cellar"}, title="The cellar")
        with patch.dict(config._config, {"side_quest_settings": {"enabled": True, "max_active": 2, "guide": True, "accept": "auto",
                                                         "offer_cooldown": 0}}):
            state = _state(_world(*quests, mission=mission), mission="main")
            _take(state, "location:gate:wait")
            self.assertEqual({k: v["status"] for k, v in state.side_quests.items()}, {"q0": "active", "q1": "active"})
            _take(state, "go:yard")              # both done: q2 starts, and its goal holds already
            self.assertEqual([state.side_quests[k]["status"] for k in ("q0", "q1", "q2")], ["completed"] * 3)
            self.assertEqual([(e["quest"], e["what"]) for e in state.side_quest_events if e["quest"] == "q2"],
                             [("q2", "started"), ("q2", "completed")])
            self.assertFalse(state.won)                                                 # the mission is its own
        with patch.dict(config._config, {"side_quest_settings": {"enabled": False}}):
            state = _state(_world(*quests))
            _take(state, "go:yard")
            self.assertEqual(state.side_quests, {})                                     # switched off

    def test_offered_only_when_enabled_and_once_per_profile_unless_repeatable(self):
        gated = SideQuest("gated", {"location": "Yard"}, title="Gated", enabled_if={"visited": "Cellar"})
        state = _state(_world(gated))
        _take(state, "go:yard")
        self.assertNotIn("gated", state.side_quests)
        once, again = _wine(), SideQuest("again", {"location": "Yard"}, title="Again", repeatable=True)
        state = _state(_world(once, again))
        state.side_quests_done_before = ["gift", "again"]                               # the profile did both
        _take(state, "npc:priest:greet")
        self.assertNotIn("gift", state.side_quests)
        self.assertEqual(state.side_quests["again"]["status"], "active")

    def test_conditions_can_follow_side_quests(self):
        state = _state(_world(_wine()))
        self.assertFalse(state.holds({"talked_to": "Priest"}))
        _take(state, "npc:priest:greet")
        self.assertTrue(state.holds({"talked_to": "Priest"}))
        self.assertTrue(state.holds({"side_quest_active": "gift"}))
        self.assertFalse(state.holds({"side_quest_done": "gift"}))
        state.inventory.add("Vintage Wine")
        _take(state, "location:gate:wait")
        self.assertTrue(state.holds({"side_quest_done": "gift"}))
        refs = content_rules.conditions.References.from_world({"side_quests": {"gift": {}}, "characters": [{"name": "Priest"}]})
        self.assertEqual(content_rules.conditions.validate({"side_quest_done": "gift"}, refs), [])
        self.assertTrue(content_rules.conditions.validate({"side_quest_done": "nope"}, refs))

    def test_the_choices_keep_a_way_toward_a_side_quest(self):
        quest = SideQuest("cellar", {"location": "Cellar"}, title="The cellar")

        def reasons(guide):
            # A game reads the settings it started with (its engine context): set them first.
            with patch.dict(config._config, {"side_quest_settings": {"enabled": True, "max_active": 3, "guide": guide, "accept": "auto"}}):
                state = _state(_world(quest))
            _take(state, "location:gate:wait")
            self.assertEqual(state.side_quests["cellar"]["status"], "active")
            trace = decision_trace(state, limit=1)
            return trace["shown"], {a["id"]: [r["code"] for r in a["reasons"]] for a in trace["actions"]}

        shown, codes = reasons(guide=True)
        self.assertEqual(shown, ["go:cellar"])                                          # the way to the cellar
        self.assertIn("priority_side_quest", codes["go:cellar"])
        self.assertIn("priority_side_quest", REASONS)
        _, codes = reasons(guide=False)
        self.assertNotIn("priority_side_quest", [code for found in codes.values() for code in found])

    def test_nothing_is_drawn_from_the_dice(self):
        state = _state(_world(_wine()))
        _take(state, "npc:priest:greet")
        state.inventory.add("Vintage Wine")
        dice, world_dice = state.rng.getstate(), random.getstate()
        self.assertTrue(side_quests.update(state))
        self.assertEqual((state.rng.getstate(), random.getstate()), (dice, world_dice))
        self.assertFalse(side_quests.update(state))                                     # nothing more to do


class OfferTests(_Quiet):
    """accept: choice (the default): the player takes a side quest on or turns it down, as a choice
    that takes no turn; an offer left unanswered lapses."""

    ACCEPT = "choice"

    def _offered(self):
        state = _state(_world(_wine()))
        _take(state, "npc:priest:greet")
        self.assertEqual(state.side_quests["gift"]["status"], "offered")
        self.assertIn("[SIDE QUEST] the Priest asks a favour of you: A gift for the chapel.", "\n".join(state.history))
        return state

    def test_taking_it_on_is_a_choice_that_takes_no_turn(self):
        state = self._offered()
        options = state.player_options()
        ids = choices.assign_ids(options, state)
        self.assertEqual(ids[:2], ["side-quest:gift:accept", "side-quest:gift:decline"])  # first
        offer = choices.describe(options[0], state, ids[0])
        self.assertEqual((offer["label"], offer["category"], offer["takes_turn"]),
                         ("Take on: A gift for the chapel", "side_quest", False))
        turn, version = state.actions_taken, state.state_version
        _take(state, "side-quest:gift:accept")
        self.assertEqual(state.side_quests["gift"]["status"], "active")
        self.assertEqual(state.actions_taken, turn)                                   # no turn passed
        self.assertGreater(state.state_version, version)
        self.assertIn("[SIDE QUEST] You take it on: A gift for the chapel.", state.history)
        self.assertNotIn("side-quest:gift:accept", choices.assign_ids(state.player_options(), state))
        self.assertEqual([e["what"] for e in state.side_quest_events], ["offered", "started"])

    def test_turning_it_down_or_letting_it_lapse(self):
        state = self._offered()
        _take(state, "side-quest:gift:decline")
        self.assertEqual(state.side_quests["gift"]["status"], "declined")
        self.assertIn("[SIDE QUEST] You turn it down: A gift for the chapel.", state.history)
        _take(state, "go:yard")
        _take(state, "go:gate")
        self.assertEqual(state.side_quests["gift"]["status"], "declined")             # not offered again
        self.assertEqual(side_quests.summary(state)["declined"], ["gift"])
        state = self._offered()
        for step in ("go:yard", "go:gate", "go:yard"):                                # side_quest_settings.offer_turns
            _take(state, step)
        self.assertEqual(state.side_quests["gift"]["status"], "declined")
        self.assertEqual(state.side_quest_events[-1]["what"], "lapsed")

    def test_through_the_api_and_the_decision_trace(self):
        state = self._offered()
        trace = decision_trace(state)
        entry = next(a for a in trace["actions"] if a["id"] == "side-quest:gift:accept")
        self.assertIn("side_quest_offer", [r["code"] for r in entry["reasons"]])
        token = sessions._register_session(state, "api")
        self.addCleanup(sessions.SESSIONS.pop, token, None)
        client = TestClient(api.api)
        play = client.get(f"/v1/sessions/{token}").json()
        turn = play["turn_summary"]["turn"]
        taken = client.post(f"/v1/sessions/{token}/actions",
                            json={"action_id": "side-quest:gift:accept", "state_version": play["state_version"]})
        self.assertEqual(taken.status_code, 200, taken.text)
        self.assertEqual(([q["status"] for q in taken.json()["side_quests"]], taken.json()["turn_summary"]["turn"]),
                         (["active"], turn))


class MissionAidTests(_Quiet):
    """A completed side quest helps the story's mission (aids_mission)."""

    def _world(self, quest):
        world = _world(quest, mission=Mission("tower", {"location": "Tower"}, title="Reach the tower"))
        world.add_location(Location("Tower", ["tower"]))
        world.connect("Yard", "Tower")
        return world

    def test_lead_opens_the_way_toward_the_mission(self):
        quest = SideQuest("wine", {"item": "Vintage Wine"}, title="Wine")                # aids_mission: lead
        state = _state(self._world(quest), mission="tower")
        _take(state, "go:cellar")
        self.assertNotIn("Tower", state.current.connected)
        _take(state, "location:cellar:take-a-bottle")
        self.assertEqual(state.side_quests["wine"]["aided"], "lead")
        self.assertIn("Tower", state.current.connected)                                # the way to the goal
        self.assertIn("[LEAD] Wine pays off for your quest: the way to the Tower is open.", state.history)
        self.assertEqual(side_quests.summary(state)["aided_mission"], ["wine"])
        self.assertEqual(state.side_quest_log()[0]["aided_mission"], "lead")

    def test_ally_and_none(self):
        state = _state(_world(_wine(aids_mission="ally")))   # (with a mission, the Priest would lead the way)
        _take(state, "npc:priest:greet")
        state.inventory.add("Vintage Wine")
        _take(state, "go:yard")
        self.assertEqual(state.side_quests["gift"]["aided"], "ally")
        self.assertIn("Priest", state.allies)
        self.assertIn("[ALLY] the Priest owes you for A gift for the chapel, and will help with your quest.",
                      state.history)
        state = _state(self._world(SideQuest("wine", {"item": "Vintage Wine"}, title="Wine", aids_mission="none")),
                       mission="tower")
        _take(state, "go:cellar")
        _take(state, "location:cellar:take-a-bottle")
        self.assertIsNone(state.side_quests["wine"]["aided"])
        self.assertNotIn("Tower", state.current.connected)


class TrainingObjectiveTests(_Quiet):
    """Side quests count in a story's fitness, and the storyteller's ranking pulls toward them by its
    learned goal_bias: training favours storytellers whose stories see them through."""

    def test_fitness_counts_them(self):
        from core_engine import fitness

        staged = SideQuest("errand", None, title="An errand", stages=[
            {"title": "Fetch", "goal": {"item": "Vintage Wine"}}, {"title": "Rest", "goal": {"location": "Yard"}}])
        failing = SideQuest("debt", {"coins": 999}, title="A debt", fail_if={"visited": "Yard"})
        mission = Mission("tower", {"location": "Tower"}, title="Reach the tower")
        world = _world(staged, failing, mission=mission)
        world.add_location(Location("Tower", ["tower"]))
        world.connect("Cellar", "Tower")
        state = _state(world, mission="tower")
        for step in ("go:cellar", "location:cellar:take-a-bottle", "go:gate", "go:yard"):
            _take(state, step)
        done = side_quests.summary(state)
        self.assertEqual((done["completed"], done["failed"]), (["errand"], ["debt"]))
        cfg = config.get("fitness.side_quests")
        expected = (cfg["complete_bonus"] + cfg["step_bonus"] + cfg["mission_aid_bonus"] * len(done["aided_mission"])
                    - cfg["fail_penalty"])
        self.assertEqual(fitness._fitness_breakdown(state)["side_quests"], expected)
        self.assertEqual(side_quests.fitness_term(state, {"complete_bonus": 1, "step_bonus": 0, "mission_aid_bonus": 0,
                                                          "fail_penalty": 0}), 1)
        with patch.dict(config._config["fitness"], {"term_weights": {"side_quests": 2.0}}):
            weighted = _state(world, mission="tower")  # a story reads the settings it started with
        weighted.__dict__.update({k: copy.deepcopy(v) for k, v in state.__dict__.items()
                                  if k in ("side_quests", "side_quest_events", "actions_taken", "tags_seen")})
        self.assertEqual(fitness._fitness_breakdown(weighted)["side_quests"], 2.0 * expected)

    def test_the_ranking_pulls_toward_a_side_quest_by_its_own_learned_trait(self):
        state = _state(_world(SideQuest("cellar", {"location": "Cellar"}, title="The cellar")))
        _take(state, "location:gate:wait")
        self.assertEqual(state.side_quests["cellar"]["status"], "active")

        settings = side_quests.settings()

        def gap(bias, rank_weight, goal_bias=0.6):
            """How much the way to the cellar (the side quest's goal) outranks the way to the yard."""
            state.genome.side_quest_bias, state.genome.goal_bias = bias, goal_bias
            with patch.object(side_quests, "settings", lambda: {**settings, "rank_weight": rank_weight}):
                scores = {a.owner: score for score, a in state._rank_actions(state._actions_now())
                          if a.source == "navigation"}
            return scores["Cellar"] - scores["Yard"]

        # The side quest's pull is rank_weight x the learned side_quest_bias (x the ranking's own multiplier
        # for leaving a place, the same for both ways): it grows with side_quest_bias, is gone at
        # rank_weight 0, and the mission's goal_bias does not change it (two traits, learned apart).
        low, high = (gap(bias, 2.0) - gap(bias, 0.0) for bias in (0.3, 0.9))
        self.assertGreater(low, 0)
        self.assertAlmostEqual(high / low, 0.9 / 0.3, places=6)
        self.assertAlmostEqual(gap(0.9, 2.0, goal_bias=0.3) - gap(0.9, 0.0, goal_bias=0.3),
                               gap(0.9, 2.0, goal_bias=1.0) - gap(0.9, 0.0, goal_bias=1.0), places=6)


class UnpPatchTests(unittest.TestCase):
    def test_a_unp_adds_changes_and_removes_side_quests(self):
        from narrative_runtime.world import PatchFormatError, apply_patch, patch_errors, world_catalogue, world_entity

        base = config.as_dict()
        patched = apply_patch(base, {"upsert": {"side_quests": {"lantern": {
            "title": "Light the lantern", "giver": "Dock Worker", "goal": {"location": "Harbor"},
            "aids_mission": "ally", "accept": "choice"}}}})
        self.assertEqual(patch_errors(patched), [])
        self.assertEqual(world_entity(patched, "side_quests", "lantern")["giver"], "Dock Worker")
        self.assertIn("lantern", [e["name"] for e in world_catalogue(patched)["side_quests"]])
        changed = apply_patch(patched, {"update": {"side_quests": {"lantern": {"giver": "Nobody At All"}}}})
        self.assertTrue(any("is not an NPC" in e for e in patch_errors(changed)))            # checked like the studio
        removed = apply_patch(patched, {"delete": {"side_quests": ["lantern", "a_full_purse"]}})
        self.assertNotIn("lantern", removed["world_definition"]["side_quests"])
        self.assertEqual(patch_errors(removed), [])
        with self.assertRaises(PatchFormatError):
            apply_patch(base, {"tags": {"side_quests": {"a_full_purse": ["x"]}}})            # no tags


class RicherQuestTests(_Quiet):
    """Handing over to the giver, deadlines, chains, the cost of turning one down, the story's own
    words, reminders, and what an offer tells (rewards, the mission, the time it may take)."""

    def test_done_it_is_handed_over_to_the_giver(self):
        state = _state(_world(_wine(turn_in=True)))
        _take(state, "npc:priest:greet")
        _take(state, "go:cellar")
        coins = state.coins
        _take(state, "location:cellar:take-a-bottle")
        record = state.side_quests["gift"]
        self.assertEqual((record["status"], record["ready"]), ("active", True))       # done, not complete yet
        self.assertEqual(state.coins, coins)                                          # nothing paid yet
        self.assertIn("[SIDE QUEST] A gift for the chapel: done. Now take it back to the Priest.", state.history)
        self.assertTrue(state.side_quest_log()[0]["ready"])
        self.assertNotIn("side-quest:gift:turn-in", choices.assign_ids(state.player_options(), state))  # not here
        _take(state, "go:gate")
        ids = choices.assign_ids(state.player_options(), state)
        self.assertEqual(ids[0], "side-quest:gift:turn-in")                           # where the giver is
        turn = state.actions_taken
        _take(state, "side-quest:gift:turn-in")
        self.assertEqual((state.side_quests["gift"]["status"], state.actions_taken), ("completed", turn))
        self.assertEqual(state.coins, coins + 6)
        self.assertIn("[SIDE QUEST] You hand it over to the Priest.", state.history)
        # Dealing with the giver in any other way hands it over too.
        state = _state(_world(_wine(turn_in=True)))
        _take(state, "npc:priest:greet")
        state.inventory.add("Vintage Wine")
        _take(state, "go:yard")
        self.assertTrue(state.side_quests["gift"]["ready"])
        _take(state, "go:gate")
        _take(state, "npc:priest:greet")
        self.assertEqual(state.side_quests["gift"]["status"], "completed")
        # The giver gone while it waits for them: it fails.
        state = _state(_world(_wine(turn_in=True)))
        _take(state, "npc:priest:greet")
        state.inventory.add("Vintage Wine")
        _take(state, "go:yard")
        state.removed_npcs.add("Priest")
        _take(state, "go:gate")
        self.assertEqual((state.side_quests["gift"]["status"], state.side_quests["gift"]["reason"]),
                         ("failed", "giver_gone"))
        self.assertIn("[SIDE QUEST FAILED] A gift for the chapel: the Priest is gone from the story.", state.history)

    def test_a_deadline_warns_then_fails(self):
        quest = SideQuest("debt", {"coins": 999}, title="A debt", within_turns=3, on_fail=[{"change_coins": -1}])
        state = _state(_world(quest))
        _take(state, "go:yard")                                                        # starts at turn 1
        self.assertEqual(state.side_quests["debt"]["deadline"], 4)
        self.assertIn("[SIDE QUEST] It has to be done within 3 turns.", state.history)
        _take(state, "go:gate")
        self.assertIn("[SIDE QUEST] Time is running out for A debt: 2 turn(s) left.", state.history)
        self.assertEqual(state.side_quest_log()[0]["turns_left"], 2)
        _take(state, "go:yard")
        self.assertEqual(state.side_quests["debt"]["status"], "active")
        _take(state, "go:gate")
        self.assertEqual((state.side_quests["debt"]["status"], state.side_quests["debt"]["reason"]), ("failed", "time"))
        self.assertIn("[SIDE QUEST FAILED] A debt: too late.", state.history)

    def test_one_follows_another(self):
        first = SideQuest("first", {"location": "Yard"}, title="First")
        second = SideQuest("second", {"location": "Cellar"}, title="Second", follows="first")
        state = _state(_world(second, first))   # written before the one it follows
        _take(state, "location:gate:wait")
        self.assertNotIn("second", state.side_quests)
        _take(state, "go:yard")
        self.assertEqual(state.side_quests["first"]["status"], "completed")
        _take(state, "go:gate")
        self.assertEqual(state.side_quests["second"]["status"], "active")                # now it is offered
        state = _state(_world(second))
        state.side_quests_done_before = ["first"]                                        # the profile did it before
        _take(state, "location:gate:wait")
        self.assertEqual(state.side_quests["second"]["status"], "active")

    def test_the_story_s_own_words_reminders_and_what_an_offer_tells(self):
        quest = SideQuest("cellar", {"location": "Cellar"}, title="The cellar", rewards=[{"change_coins": 4}],
                          within_turns=9, texts={"offer": "A voice from below: \"Come down, {title}!\" ({reward})",
                                                 "complete": "Down at last. ({title})"})
        mission = Mission("tower", {"location": "Tower"}, title="Reach the tower")
        world = _world(quest, mission=mission)
        world.add_location(Location("Tower", ["tower"]))
        world.connect("Yard", "Tower")
        with patch.dict(config._config, {"side_quest_settings": {**config.get("side_quest_settings"),
                                                                 "accept": "auto", "remind_after": 2}}):
            state = _state(world, mission="tower")
        _take(state, "go:yard")
        text = "\n".join(state.history)
        self.assertIn('A voice from below: "Come down, The cellar!" (4 coins)', text)    # its own words
        self.assertIn("[SIDE QUEST] In it for you: 4 coins.", text)
        self.assertIn("[SIDE QUEST] It could help with your quest too: Reach the tower.", text)
        self.assertIn("[SIDE QUEST] It has to be done within 9 turns.", text)
        _take(state, "go:gate")
        _take(state, "go:yard")
        self.assertIn("[SIDE QUEST] Still open: The cellar - Reach the Cellar.", state.history)  # quiet 2 turns
        self.assertEqual(sum("Still open" in line for line in state.history), 1)        # once per quiet spell
        _take(state, "go:gate")
        _take(state, "go:cellar")
        self.assertIn("Down at last. (The cellar)", state.history)
        self.assertEqual(state.side_quest_log()[0]["rewards"], "4 coins")

    def test_the_way_to_a_place_it_needs_is_pointed_out(self):
        quest = SideQuest("tower", {"location": "Tower"}, title="The tower", start={"visited": "Yard"})
        world = _world(quest)
        world.add_location(Location("Tower", ["tower"]))
        state = _state(world)
        state.player_directed = True  # real play: the player goes; a lead does not take them there
        _take(state, "go:yard")
        self.assertEqual(state.side_quests["tower"]["status"], "active")
        self.assertIn("Tower", state.current.connected)                                # the way is open now
        self.assertIn("[LEAD] What you heard points you the way to the Tower.", state.history)
        _take(state, "go:tower")
        self.assertEqual(state.side_quests["tower"]["status"], "completed")

    def test_side_quest_moments_are_something_new(self):
        from core_engine import fitness

        state = _state(_world(SideQuest("yard", {"location": "Yard"}, title="The yard")))
        _take(state, "location:gate:wait")                                              # offered/started: new
        self.assertEqual(state.calm_streak, 0)
        self.assertIn("side_quest", dict(fitness._developments(state))[1])
        self.assertTrue(side_quests.moments_at(state, 1))
        self.assertFalse(side_quests.moments_at(state, 99))


class StoryTests(_Quiet):
    """The story has its say: what happens in it changes side quests (reactions), it lets new ones in
    at fitting moments only, offers first what fits it, ends those that can no longer be done, pauses a
    deadline while the player is held, and recalls them at the end."""

    def _settings(self, **changes):
        return patch.dict(config._config, {"side_quest_settings": {**config.get("side_quest_settings"),
                                                                   "accept": "auto", **changes}})

    def test_the_story_reacts_on_side_quests(self):
        staged = SideQuest("errand", None, title="An errand", stages=[
            {"title": "Fetch", "goal": {"item": "Vintage Wine"}}, {"title": "Rest", "goal": {"coins": 999}}],
            reactions=[{"when": {"visited": "Yard"}, "then": "advance", "text": "Word comes: {title} moves on.",
                        "effects": [{"change_coins": 1}]},
                       {"when": {"visited": "Cellar"}, "then": "complete"}])
        doomed = SideQuest("doomed", {"coins": 999}, title="Doomed", reactions=[
            {"when": {"visited": "Yard"}, "then": "fail", "text": "The yard settles it: {title} is over."}])
        state = _state(_world(staged, doomed))
        with self._settings(offer_cooldown=0):
            state = _state(_world(staged, doomed))
        _take(state, "location:gate:wait")
        coins = state.coins
        _take(state, "go:yard")
        self.assertEqual(state.side_quests["errand"]["stage"], 1)                       # the story moved it on
        self.assertEqual(state.coins, coins + 1)
        self.assertIn("Word comes: An errand moves on.", state.history)
        self.assertEqual((state.side_quests["doomed"]["status"], state.side_quests["doomed"]["reason"]),
                         ("failed", "story"))
        self.assertIn("The yard settles it: Doomed is over.", state.history)
        _take(state, "go:gate")
        _take(state, "go:yard")
        self.assertEqual(sum("Word comes" in line for line in state.history), 1)       # each reaction once
        _take(state, "go:gate")
        _take(state, "go:cellar")
        self.assertEqual(state.side_quests["errand"]["status"], "completed")           # the story completed it
        self.assertTrue(state.holds({"side_quest_failed": "doomed"}))
        self.assertTrue(state.holds({"side_quest_done": "errand"}))

    def test_an_offer_the_story_overtakes_is_withdrawn(self):
        quest = _wine(reactions=[{"when": {"visited": "Yard"}, "then": "withdraw", "while": "offered",
                                  "text": "{giver} waves it away: \"Never mind.\""}])
        with self._settings(accept="choice"):
            state = _state(_world(quest))
        _take(state, "npc:priest:greet")
        self.assertEqual(state.side_quests["gift"]["status"], "offered")
        _take(state, "go:yard")
        self.assertEqual((state.side_quests["gift"]["status"], state.side_quests["gift"]["reason"]),
                         ("declined", "story"))
        self.assertIn('the Priest waves it away: "Never mind."', state.history)
        self.assertTrue(state.holds({"side_quest_declined": "gift"}))

    def test_new_ones_come_at_fitting_moments_one_at_a_time(self):
        quests = [SideQuest(f"q{n}", {"coins": 999}, title=f"Quest {n}") for n in range(2)]
        with self._settings(offer_after=2):
            state = _state(_world(*quests))
        _take(state, "location:gate:wait")
        self.assertEqual(state.side_quests, {})                                         # the opening
        _take(state, "go:yard")
        self.assertEqual(list(state.side_quests), ["q0"])                               # one...
        _take(state, "go:gate")
        self.assertEqual(list(state.side_quests), ["q0", "q1"])                         # ...then the next
        with self._settings():
            tense = _state(_world(*quests))
        tense.tension = lambda: 1.0                                                     # danger a moment ago
        _take(tense, "go:yard")
        self.assertEqual(tense.side_quests, {})
        tense.tension = lambda: 0.0
        tense.in_finale = lambda: True                                                  # the finale
        _take(tense, "go:gate")
        self.assertEqual(tense.side_quests, {})
        tense.in_finale = lambda: False
        _take(tense, "go:yard")
        self.assertEqual(list(tense.side_quests), ["q0"])

    def test_what_fits_the_story_comes_first(self):
        far = SideQuest("far", {"coins": 999}, title="Far", tags=["sea"])
        near = SideQuest("near", {"coins": 999}, title="Near", tags=["yard"])
        with self._settings(offer_after=1):
            state = _state(_world(far, near))
        _take(state, "go:yard")                                                         # the Yard: tags yard
        self.assertEqual(list(state.side_quests), ["near"])                             # written second, fits
        self.assertGreater(side_quests.fit(state, near), side_quests.fit(state, far))
        mission = Mission("tower", {"location": "Yard"}, title="The yard")
        for_mission = SideQuest("help", {"location": "Yard"}, title="Help")
        with self._settings():
            state = _state(_world(far, for_mission, mission=mission), mission="tower")
        self.assertGreater(side_quests.fit(state, for_mission), side_quests.fit(state, far))  # the mission's thread

    def test_what_can_no_longer_be_done_ends_with_its_reason(self):
        with self._settings():
            state = _state(_world(_wine()))
        _take(state, "npc:priest:greet")
        state.removed_npcs.add("Priest")
        _take(state, "go:yard")
        self.assertEqual((state.side_quests["gift"]["status"], state.side_quests["gift"]["reason"]),
                         ("failed", "giver_gone"))
        quest = SideQuest("wine", {"item": "Vintage Wine"}, title="Wine")
        with self._settings():
            state = _state(_world(quest))
        _take(state, "location:gate:wait")
        from core_engine.world import ItemInstance

        gone = ItemInstance(next(o for o in state.world.objects if o.name == "Vintage Wine"), None)
        gone.state = "used"                                                             # used up elsewhere
        state.item_instances["Vintage Wine"] = gone
        _take(state, "go:yard")
        self.assertEqual(state.side_quests["wine"]["reason"], "item_gone")
        self.assertIn("[SIDE QUEST FAILED] Wine: a Vintage Wine is gone for good.", state.history)
        self.assertIn("[SIDE QUEST] Wine: Get the Vintage Wine", "\n".join(state.history))  # no description: its goal
        with self._settings(impossible="keep"):
            state = _state(_world(_wine()))
        _take(state, "npc:priest:greet")
        state.removed_npcs.add("Priest")
        _take(state, "go:yard")
        self.assertEqual(state.side_quests["gift"]["status"], "active")                 # kept, as configured

    def test_a_deadline_waits_while_the_player_is_held(self):
        quest = SideQuest("debt", {"coins": 999}, title="A debt", within_turns=3)
        with self._settings():
            state = _state(_world(quest))
        _take(state, "go:yard")
        deadline = state.side_quests["debt"]["deadline"]
        state._location_is_locked = lambda *args: True                                  # held in a cell
        _take(state, "location:yard:rest")
        _take(state, "location:yard:rest")
        self.assertEqual(state.side_quests["debt"]["deadline"], deadline + 2)
        self.assertEqual(state.side_quests["debt"]["status"], "active")

    def test_the_ending_recalls_them_and_threads_left_open_cost(self):
        done = SideQuest("done", {"location": "Yard"}, title="Seen through")
        left = SideQuest("left", {"coins": 999}, title="Left open")
        with self._settings(offer_cooldown=0):
            state = _state(_world(done, left))
        _take(state, "go:yard")
        open_before = side_quests.fitness_term(state, {"unfinished_penalty": 5})
        state.end_session()
        self.assertIn("[SIDE QUESTS] Along the way you saw Seen through through; left Left open unfinished.",
                      state.history)
        self.assertEqual(side_quests.fitness_term(state, {"unfinished_penalty": 5}), open_before - 5)
        with self._settings(epilogue=False, offer_cooldown=0):
            quiet = _state(_world(done, left))
        _take(quiet, "go:yard")
        quiet.end_session()
        self.assertFalse(any("[SIDE QUESTS]" in line for line in quiet.history))

    def test_reactions_and_tags_are_checked(self):
        world = ValidationTests.WORLD
        good = {"goal": {"location": "Gate"}, "tags": ["quiet"], "reactions": [
            {"when": {"npc_removed": "Priest"}, "then": "fail", "text": "{giver} is gone."}]}
        self.assertEqual(content_rules.side_quest_errors(world, {"q": good}), [])
        for reactions, words in (([{"then": "fail"}], "needs a `when`"),
                                 ([{"when": {"visited": "Gate"}, "then": "explode"}], "`then` must be"),
                                 ([{"when": {"visited": "Gate"}, "then": "fail", "while": "later"}], "`while` must be"),
                                 ([{"when": {"visited": "Moon"}, "then": "fail"}], "Moon"),
                                 ([{"when": {"visited": "Gate"}, "text": "{mood}"}], "unknown placeholder"),
                                 ([{"when": {"visited": "Gate"}}], "does nothing"),
                                 ("often", "must be a list")):
            with self.subTest(reactions=reactions):
                errors = content_rules.side_quest_errors(world, {"q": {**good, "reactions": reactions}})
                self.assertTrue(any(words in error for error in errors), errors)
        errors = content_rules.side_quest_errors(world, {"q": {**good, "tags": "quiet"}})
        self.assertTrue(any("`tags` must be a list" in error for error in errors), errors)


class DeclineCostTests(_Quiet):
    ACCEPT = "choice"

    def test_turning_it_down_or_letting_it_go_costs_what_on_decline_says(self):
        state = _state(_world(_wine(on_decline=[{"change_coins": -2}])))
        _take(state, "npc:priest:greet")
        coins = state.coins
        _take(state, "side-quest:gift:decline")
        self.assertEqual(state.coins, coins - 2)
        state = _state(_world(_wine(on_decline=[{"change_coins": -2}])))
        _take(state, "npc:priest:greet")
        coins = state.coins
        for step in ("go:yard", "go:gate", "go:yard"):
            _take(state, step)
        self.assertEqual(state.side_quest_events[-1]["what"], "lapsed")
        self.assertEqual(state.coins, coins - 2)


class GenomeTraitTests(unittest.TestCase):
    """side_quest_bias: the storyteller's own trait for side quests, added without changing any other
    trait or draw (a stream of its own), and given its default to storytellers saved before it."""

    def test_existing_traits_and_draws_are_unchanged(self):
        from core_engine import genome as genomes

        for seed in range(10):
            mine, plain = random.Random(seed), random.Random(seed)
            genome = Genome(mine)
            drawn = {name: plain.uniform(*genomes.trait_bounds(name)) for name in genomes.DRAWN_TRAITS}
            self.assertEqual({name: genome.traits()[name] for name in drawn}, drawn)   # the same values...
            self.assertEqual(mine.getstate(), plain.getstate())                         # ...and the same draws
            low, high = genomes.trait_bounds("side_quest_bias")
            self.assertTrue(low <= genome.side_quest_bias <= high)
            other = Genome(mine)
            before = mine.getstate()
            child = Genome.crossover(genome, other, mine)
            legacy = copy.deepcopy(child)
            del legacy.side_quest_bias                                                   # as before the trait
            child.mutate(random.Random(seed), rate=0.5)
            legacy.mutate(random.Random(seed), rate=0.5)
            self.assertEqual({k: v for k, v in child.traits().items() if k != "side_quest_bias"}, legacy.traits())
            self.assertNotEqual(before, mine.getstate())

    def test_storytellers_saved_before_the_trait(self):
        old = {k: v for k, v in Genome(random.Random(3)).traits().items() if k != "side_quest_bias"}
        restored = Genome.from_traits(old)
        self.assertEqual(restored.side_quest_bias, Genome.added_field_defaults()["side_quest_bias"])
        self.assertTrue(Genome.same(old, restored.traits()))
        self.assertFalse(Genome.same({**old, "goal_bias": 0.99}, restored.traits()))
        genome = Genome.from_traits(old)
        state = GameState(engine.build_world(genome), genome, rng=random.Random(2))
        state.take_option(state.player_options()[0])
        data = json.loads(json.dumps(snapshot.dump(state)))
        data["genome"].pop("side_quest_bias")                                          # a snapshot from before
        self.assertEqual(snapshot.load(data).side_quests, state.side_quests)           # still exact


class SavedTests(unittest.TestCase):
    """The shipped world's side quests, through snapshots, save games, the UNP's parameters and a profile."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ane-side-quests-")
        patcher = patch.dict(os.environ, {"ANE_SAVE_DIR": self.dir, "ANE_SESSIONS_DIR": os.path.join(self.dir, "s")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.genome = Genome(rng=random.Random(4))

    def _under_way(self, store=None, profile="sq"):
        """A story of the shipped world with "A full purse" under way."""
        with patch.dict(config._config, {"side_quest_settings": {**config.get("side_quest_settings"),
                                                                 "offer_cooldown": 0},   # several at once
                                         "side_story": {**config.get("side_story"), "intro_gap": 0}}):
            state = GameState(engine.build_world(self.genome), self.genome, rng=random.Random(5), store=store,
                              profile=profile, start="Tavern")
        state.coins = 1
        options = state.player_options()
        ids = choices.assign_ids(options, state)
        # A turn in the Tavern that starts no event (no offer comes while one is under way); who is in the
        # Tavern depends on the seed (the Merchant only drops by now and then: their haunts).
        state.take_option(next(option for option, action_id in zip(options, ids)
                               if not option.triggers_event and not action_id.startswith("leave")))
        self.assertEqual(state.side_quests.get("a_full_purse", {}).get("status"), "offered", state.side_quests)
        _take(state, "side-quest:a-full-purse:accept")                                # taken on, as a choice
        self.assertEqual(state.side_quests["a_full_purse"]["status"], "active")
        return state

    def test_the_shipped_side_quests_are_valid(self):
        world = config.get("world_definition") or {}
        self.assertEqual(set(world.get("side_quests") or {}), {"a_gift_for_the_chapel", "rumours_from_the_archive",
                                                                 "a_blessing_for_the_road", "charting_the_storm",
                                                                 "a_full_purse",
                                                                 # offered by the Captain once he is a friend
                                                                 # (core_engine/relationships.py, tests/test_relationships.py)
                                                                 "a_night_with_the_watch"})
        self.assertEqual(content_rules.side_quest_errors(world, world["side_quests"]), [])
        self.assertEqual(set(engine.build_world(self.genome).side_quests), set(world["side_quests"]))

    def test_snapshots_and_save_games_carry_them(self):
        state = self._under_way()
        with state.context.bind():  # under the configuration it was made with (as a restore does)
            restored = snapshot.load(json.loads(json.dumps(snapshot.dump(state))), genome=self.genome)
        self.assertEqual((restored.side_quests, restored.side_quest_events), (state.side_quests, state.side_quest_events))
        self.assertEqual(restored.side_quest_log(), state.side_quest_log())
        doc = savegame.make(state)
        self.assertEqual(doc["parameters"]["side_quests"]["a_full_purse"], {"status": "active", "stage": 0,
                                                                              "ready": False})

    def test_a_rebuilt_session_stands_where_they_stood_quietly(self):
        state = self._under_way()
        state.coins = 40
        _turn(state)
        self.assertEqual(state.side_quests["a_full_purse"]["status"], "completed")
        params = resume.parameters(state)
        rebuilt = resume.rebuild(self.genome, params)
        self.assertEqual(resume.parameters(rebuilt)["side_quests"], params["side_quests"])
        coins = rebuilt.coins
        _turn(rebuilt)
        self.assertFalse(any("A full purse" in line for line in rebuilt.history))     # not told or paid again
        self.assertEqual(rebuilt.side_quests["a_full_purse"]["status"], "completed")
        self.assertLessEqual(abs(rebuilt.coins - coins), 50)

    def test_a_completed_side_quest_stays_with_the_profile(self):
        store = VariableStore(os.path.join(self.dir, "values.json"))
        state = self._under_way(store=store, profile="keeper")
        state.coins = 40
        _turn(state)
        state.end_session()
        report = state.session_report
        self.assertEqual(report["side_quests"]["completed"], ["a_full_purse"])
        self.assertEqual(report["progression"]["side_quests_completed"], ["a_full_purse"])
        self.assertEqual(store.completed_side_quests("keeper"), ["a_full_purse"])
        self.assertEqual(store.progression("keeper")["side_quests_completed"], ["a_full_purse"])
        again = self._under_way(store=store, profile="keeper")                         # repeatable: offered again
        self.assertEqual(again.side_quests_done_before, ["a_full_purse"])


class ValidationTests(unittest.TestCase):
    WORLD = {"characters": [{"name": "Priest"}, {"name": "Bandit", "encounter_event": "ambush"}],
             "objects": [{"name": "Vintage Wine"}], "locations": {"Gate": {}}, "events": {"ambush": {}},
             "variables": {"reputation": {"type": "int", "default": 0}}}

    def _errors(self, quest):
        return content_rules.side_quest_errors(self.WORLD, {"q": quest})

    def test_what_is_refused(self):
        self.assertEqual(self._errors({"title": "Fine", "giver": "Priest", "goal": {"item": "Vintage Wine"},
                                       "start": {"visited": "Gate"}, "fail_if": {"npc_removed": "Priest"},
                                       "on_fail": [{"change_coins": -1}], "repeatable": True}), [])
        for quest, words in (
                ({"goal": {"item": "Vintage Wine"}, "weight": 2}, "unknown field(s) weight"),
                ({"goal": {"item": "Vintage Wine"}, "reward": 1}, "unknown field(s) reward"),
                ({"goal": {"item": "Nothing Here"}}, "does not exist"),
                ({"goal": {"flying": True}}, "unknown goal key"),
                ({}, "needs a `goal`"),
                ({"goal": {"location": "Gate"}, "giver": "Nobody"}, "is not an NPC"),
                ({"goal": {"location": "Gate"}, "giver": "Bandit"}, "is hostile"),
                ({"goal": {"location": "Gate"}, "start": {"visited": "Moon"}}, "Moon"),
                ({"goal": {"location": "Gate"}, "fail_if": {"spell": 1}}, "fail_if"),
                ({"goal": {"location": "Gate"}, "on_fail": [{"explode": 1}]}, "on_fail"),
                ({"goal": {"location": "Gate"}, "repeatable": "yes"}, "repeatable")):
            with self.subTest(quest=quest):
                errors = self._errors(quest)
                self.assertTrue(any(words in error for error in errors), errors)
        self.assertEqual(content_rules.side_quest_errors(self.WORLD, ["q"]),
                         ["Side quests must be a mapping of side quest ids to side quests."])
        self.assertEqual(content_rules.mission_errors(self.WORLD, ["m"]),
                         ["Missions must be a mapping of mission ids to missions."])  # as before


class ApiAndStudioTests(_Quiet):
    def setUp(self):
        super().setUp()
        self.client = TestClient(api.api)
        self.path = Path(tempfile.mkdtemp(prefix="ane-sq-doc-")) / "side_quests.yaml"
        self.path.write_text(content.load_side_quests_yaml(), encoding="utf-8")
        patcher = patch.object(content, "SIDE_QUESTS_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(config.reload)

    def test_the_document_is_checked_before_it_is_saved(self):
        doc = self.client.get("/v1/documents/side_quests").json()
        self.assertIn("a_full_purse", doc["yaml"])
        before = self.path.read_text(encoding="utf-8")
        bad = self.client.put("/v1/documents/side_quests", json={"yaml": "world_definition:\n  side_quests:\n"
                                                                            "    q: {goal: {item: Nothing Here}}\n"})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("does not exist", bad.json()["detail"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)                 # nothing written
        good = self.client.put("/v1/documents/side_quests", json={"yaml": "world_definition:\n  side_quests:\n"
                                                                             "    q: {title: Q, goal: {location: Harbor}}\n"})
        self.assertEqual(good.status_code, 200, good.text)
        self.assertIn("q: {title: Q", self.path.read_text(encoding="utf-8"))
        self.assertIn("side_quests", self.client.get("/v1/documents").text)

    def test_play_state_runtime_view_and_catalogue(self):
        from narrative_runtime.engine import Game

        state = _state(_world(_wine()))
        token = sessions._register_session(state, "api")
        self.addCleanup(sessions.SESSIONS.pop, token, None)
        play = self.client.get(f"/v1/sessions/{token}").json()
        self.assertEqual(play["side_quests"], [])
        self.assertEqual(next(p for p in play["people"] if p["name"] == "Priest")["side_quests"], ["A gift for the chapel"])
        greet = next(c["id"] for c in play["choices"] if c["id"] == "npc:priest:greet")
        play = self.client.post(f"/v1/sessions/{token}/actions", json={"action_id": greet}).json()
        self.assertEqual([(q["key"], q["status"]) for q in play["side_quests"]], [("gift", "active")])
        self.assertEqual(play["turn_summary"]["side_quests"][0]["what"], "started")
        view = Game(None, state).view()
        self.assertEqual(view["side_quests"], play["side_quests"])
        catalogue = self.client.get("/v1/world/catalogue").json()
        self.assertIn("a_full_purse", [entry["name"] for entry in catalogue["side_quests"]])

    def test_the_studio_shows_and_edits_them(self):
        import app

        quests = [{"key": "gift", "title": "A gift", "giver": "Priest", "status": "active", "todo": ["Get the wine"],
                   "stages": [{"title": "Find it", "state": "current"}]},
                  {"key": "debt", "title": "A debt", "giver": None, "status": "failed", "stages": [], "todo": []}]
        shown = app.side_quests_markdown(quests)
        self.assertIn("▶️ **A gift** · for Priest", shown)
        self.assertIn("**To do:** Get the wine", shown)
        self.assertIn("✗ **A debt** · failed", shown)
        self.assertEqual(app.side_quests_markdown([]), "")
        chips = app._changes({"side_quests": [{"title": "A gift", "what": "started", "stage": None, "quest": "gift"},
                                              {"title": "A debt", "what": "failed", "stage": None, "quest": "debt"}]})
        self.assertEqual(chips, ["🧭 new side quest: A gift", "✗ side quest failed: A debt"])
        text = app.load_side_quests_yaml()
        self.assertIn("side_quests", text)
        saved, message = app.save_side_quests_yaml(text)
        self.assertIn("side quest(s)", message)


if __name__ == "__main__":
    unittest.main()
