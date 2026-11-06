"""Shared conditions and effects: one condition tree and one effect list for the whole world."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import copy
import json
import os
import random
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

import main as engine
from backend import content, store
from backend.errors import AuthoringError
from core_engine import GameState, Genome, Outcome, conditions, effects
from core_engine import variables as persistent
from core_engine.builder import build_world, logic_errors
from core_engine.effects import EffectError
from utils.config_loader import config

# palace_access AND (persuasion >= 60 OR holding the Royal Pass)
GATE = {"all": [
    {"variable": "palace_access", "op": "eq", "value": True},
    {"any": [{"stat": "persuasion", "op": "gte", "value": 60}, {"has_item": "Royal Pass"}]},
]}
VARIABLES = {
    "palace_access": {"type": "bool", "default": False},
    "reputation": {"type": "int", "default": 0, "min": -5, "max": 5},
}


def _world_definition():
    """A court where GATE switches on a location, an NPC, an item, an action,
    an event, a dialogue line and a mission goal."""
    return {
        "variables": copy.deepcopy(VARIABLES),
        "character_stats": copy.deepcopy(config.get("world_definition.character_stats")),
        "locations": {
            "Court": {"tags": ["court"], "actions": [
                {"name": "Petition the court", "enabled_if": GATE,
                 "outcomes": [{"desc": "The court hears you.", "tags": ["neutral"]}]},
                {"name": "Call the herald", "triggers_event": "audience"},
                {"name": "Wait", "outcomes": [{"desc": "You wait.", "tags": ["neutral"]}]},
            ]},
            "Throne Room": {"tags": ["throne"], "enabled_if": GATE},
            "Cellar": {"tags": ["cellar"]},
        },
        "characters": [
            {"name": "Chamberlain", "unique": True, "associated_tags": ["court"], "known_locations": ["Court"],
             "enabled_if": GATE},
            {"name": "Herald", "unique": True, "associated_tags": ["court"], "known_locations": ["Court"],
             "descriptions": ["A herald in a plumed hat."],
             "dialogue": [{"enabled_if": GATE, "text": "Pass, friend."}, {"text": "Halt."}]},
        ],
        "objects": [
            {"name": "Royal Pass", "associated_tags": ["pass"]},
            {"name": "Seal", "associated_tags": ["court"], "enabled_if": GATE},
        ],
        "events": {"audience": {"title": "Audience", "enabled_if": GATE, "actions": [
            {"name": "Bow", "outcomes": [{"desc": "You bow.", "tags": ["neutral"]}]},
        ]}},
        "missions": {
            "audience": {"title": "Audience", "goal": {"condition": GATE}},
            "rich": {"title": "Rich", "goal": {"coins": 500}},
        },
    }


class _CourtWorld(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.store = persistent.VariableStore(str(self.dir / "save.json"))
        for patcher in (
            patch.dict(config._config, {"world_definition": _world_definition()}),
            patch.dict(os.environ, {"ANE_SAVE_DIR": str(self.dir)}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        random.seed(11)
        self.genome = Genome()
        self.genome.event_prob = 0.0
        self.genome.chaos_bias = 0.0

    def _state(self, palace_access=False, persuasion=30, royal_pass=False, **kwargs):
        world = build_world(self.genome)
        world.locations["Court"].connect(world.locations["Throne Room"])
        state = GameState(world, self.genome, start="Court", rng=random.Random(0), mission="rich",
                          variables={"palace_access": palace_access}, stats_mode="balanced", **kwargs)
        state.stats.change("persuasion", persuasion - state.stats.base_value("persuasion"))
        if royal_pass:
            state.inventory.add("Royal Pass")
        return state


class SameConditionSameResultTests(_CourtWorld):
    def _observed(self, state):
        """How each use of GATE behaves in play, without asking the evaluator."""
        actions = state.get_available_actions()
        names = [a.name for a in actions]
        talk = next(a for a in actions if a.character_name == "Herald" and a.source == "conversation")
        return {
            "location (travel offered)": any("Throne Room" in name for name in names),
            "npc (in the scene)": "Chamberlain" in state.visible_inhabitants,
            "item (on show)": "Seal" in state.visible_objects,
            "action (offered)": "Petition the court" in names,
            "event (trigger offered)": "Call the herald" in names,
            "dialogue (line chosen)": talk.outcomes[0].desc == "Pass, friend.",
            "mission goal (met)": state._goal_met(state.world.missions["audience"]),
            "is_enabled": state.is_enabled(state.world.locations["Throne Room"]),
        }

    def test_the_same_condition_gives_the_same_result_wherever_it_is_used(self):
        results = set()
        for palace_access in (False, True):
            for persuasion in (30, 70):
                for royal_pass in (False, True):
                    state = self._state(palace_access, persuasion, royal_pass)
                    expected = palace_access and (persuasion >= 60 or royal_pass)
                    self.assertEqual(state.holds(GATE), expected)
                    observed = self._observed(state)
                    with self.subTest(palace_access=palace_access, persuasion=persuasion, royal_pass=royal_pass):
                        self.assertEqual(observed, {use: expected for use in observed})
                    results.add(expected)
        self.assertEqual(results, {True, False})

    def test_uses_follow_changes_during_play(self):
        state = self._state(palace_access=True, persuasion=30)
        self.assertFalse(self._observed(state)["action (offered)"])
        state.apply_effects([{"give_item": "Royal Pass"}])  # the story's own state, not a variable
        self.assertEqual(set(self._observed(state).values()), {True})
        state.apply_effects([{"take_item": "Royal Pass"}])
        self.assertEqual(set(self._observed(state).values()), {False})

    def test_shorthand_and_tree_agree(self):
        shorthand = {"reputation": {"gte": 1, "lt": 4}, "palace_access": True}
        tree = {"all": [
            {"variable": "reputation", "op": "gte", "value": 1}, {"variable": "reputation", "op": "lt", "value": 4},
            {"variable": "palace_access", "value": True},
        ]}
        for reputation in range(-2, 6):
            for access in (False, True):
                values = {"reputation": reputation, "palace_access": access}
                state = self._state(palace_access=access)
                state.variables["reputation"] = reputation
                expected = access and 1 <= reputation < 4
                self.assertEqual(persistent.check(shorthand, values), expected)  # no story in progress
                self.assertEqual(persistent.check(tree, values), expected)
                self.assertEqual(state.holds(shorthand), expected)
                self.assertEqual(state.holds(tree), expected)

    def test_variable_only_contexts_refuse_story_facts(self):
        with self.assertRaises(conditions.ConditionError):
            persistent.check({"has_item": "Royal Pass"}, {})


class ValidationTests(unittest.TestCase):
    refs = conditions.References(
        variables=VARIABLES, stats=["persuasion"], items=["Royal Pass"], locations=["Court"],
        npcs=["Herald"], events=["audience"], missions=["audience"],
    )

    def _errors(self, condition):
        return conditions.validate(condition, self.refs)

    def test_valid_conditions(self):
        for condition in (None, {}, GATE, {"palace_access": True}, {"reputation": {"in": [1, 2]}},
                          {"not": {"has_item": "Royal Pass"}}, [{"visited": "Court"}, {"resource": "coins", "op": "gte", "value": 5}],
                          {"action_done": "(Herald) Anything"}, {"outcome_tag": "escape"}, {"playthroughs": {"gte": 1}}):
            with self.subTest(condition=condition):
                self.assertEqual(self._errors(condition), [])

    def test_operand_types(self):
        for condition in (
            {"palace_access": {"gte": 1}},                                   # ordering a bool
            {"palace_access": "yes"},                                         # bool compared with a string
            {"reputation": "high"},                                           # int compared with a string
            {"reputation": {"in": 3}},                                        # `in` needs a list
            {"reputation": {"in": [1, "two"]}},                               # ...of numbers
            {"stat": "persuasion", "op": "gte", "value": "a lot"},
            {"resource": "coins", "op": "lt", "value": True},
        ):
            with self.subTest(condition=condition):
                self.assertTrue(self._errors(condition))

    def test_references_and_structure(self):
        for condition in (
            {"karma": 1}, {"stat": "luck", "op": "gte", "value": 1}, {"resource": "mana", "op": "gt", "value": 0},
            {"has_item": "Crown"}, {"visited": "Moon"}, {"npc_removed": "Nobody"}, {"event_done": "party"},
            {"mission": "conquest"}, {"reputation": {"about": 1}}, {"has_item": "Royal Pass", "op": "eq"},
            {"any": []}, {"not": {}}, {"all": [GATE], "reputation": 1}, {"variable": "reputation"}, "palace",
        ):
            with self.subTest(condition=condition):
                self.assertTrue(self._errors(condition))

    def test_effects_check_references_types_and_writable_fields(self):
        valid = [{"set_variable": {"palace_access": True}}, {"change_variable": {"reputation": -1}},
                 {"set_stat": {"persuasion": 60}}, {"give_item": ["Royal Pass"]}, {"remove_npc": True},
                 {"start_event": "audience"}, {"complete_mission": "audience"}, {"end_story": "The end."}]
        self.assertEqual(effects.validate(valid, self.refs), [])
        for bad in (
            [{"set_variable": {"playthroughs": 3}}],          # built in, read-only
            [{"set_variable": {"karma": 1}}],                 # undefined
            [{"set_variable": {"palace_access": "yes"}}],     # wrong type
            [{"set_variable": {"reputation": 1.5}}],          # an int is whole
            [{"change_variable": {"palace_access": 1}}],      # deltas on numbers only
            [{"change_stat": {"luck": 1}}], [{"set_stat": {"persuasion": 400}}],
            [{"give_item": "Crown"}], [{"move_to": "Moon"}], [{"reveal_npc": "Nobody"}], [{"start_event": "party"}],
            [{"complete_mission": "conquest"}], [{"change_coins": "5"}], [{"end_story": ""}],
            [{"teleport": "Court"}], [{"give_item": "Royal Pass", "take_item": "Royal Pass"}], {"give_item": "x"},
        ):
            with self.subTest(effects=bad):
                self.assertTrue(effects.validate(bad, self.refs))

    def test_condition_keywords_cannot_name_variables(self):
        self.assertTrue(persistent.validate_definitions({"visited": {"type": "bool", "default": False}}))


class EffectTests(_CourtWorld):
    def test_every_effect(self):
        state = self._state(palace_access=False, store=self.store, profile="p")
        state.apply_effects([
            {"set_variable": {"palace_access": True}}, {"change_variable": {"reputation": 9}},  # clamped to 5
            {"set_stat": {"persuasion": 65}}, {"change_stat": {"persuasion": 5}},
            {"give_item": ["Royal Pass", "Seal"]}, {"take_item": "Seal"},
            {"change_coins": -1000}, {"change_health": -2},
            {"reveal_npc": "Herald"}, {"remove_npc": "Chamberlain"}, {"reveal_location": "Cellar"},
        ])
        self.assertEqual((state.variables["palace_access"], state.variables["reputation"]), (True, 5))
        self.assertEqual(self.store.load("p", VARIABLES)["reputation"], 5)  # saved for the next playthrough
        self.assertEqual(state.stats.base_value("persuasion"), 70)
        self.assertEqual(state.inventory, {"Royal Pass"})
        self.assertEqual(state.coins, 0)  # never below empty
        self.assertIn("Chamberlain", state.removed_npcs)
        self.assertIn("Cellar", state.current.connected)
        state.apply_effects([{"move_to": "Cellar"}])
        self.assertEqual(state.current.name, "Cellar")
        state.apply_effects([{"start_event": "audience"}])
        self.assertEqual(state.active_event.name, "audience")  # GATE holds: the event may start

    def test_mission_and_story_effects(self):
        state = self._state()
        state.apply_effects([{"complete_mission": True}])
        self.assertTrue(state.won and state.ended)
        ended = self._state()
        ended.apply_effects([{"end_story": "You sail away."}])
        self.assertEqual((ended.ended, ended.end_kind, ended.end_reason), (True, "story_end", "You sail away."))

    def test_outcome_fields_are_effect_shorthand(self):
        legacy = Outcome("x", set_variables={"palace_access": True}, change_variables={"reputation": 2},
                         change_stats={"persuasion": 5})
        explicit = Outcome("x", effects=[{"set_variable": {"palace_access": True}},
                                         {"change_variable": {"reputation": 2}}, {"change_stat": {"persuasion": 5}}])
        self.assertEqual(legacy.effects.items, explicit.effects.items)
        first, second = self._state(), self._state()
        first.apply(legacy)
        second.apply(explicit)
        self.assertEqual((first.variables, first.stats.values(), first.history),
                         (second.variables, second.stats.values(), second.history))

    def test_effects_of_a_chosen_outcome_and_a_started_event(self):
        state = self._state(palace_access=True, persuasion=70)
        wait = next(a for a in state.world.locations["Court"].actions if a.name == "Wait")
        wait.outcomes[0] = Outcome("You wait.", tags=["neutral"], effects=[
            {"give_item": "Royal Pass"}, {"start_event": "audience"}])
        names = [a.name for a in state.ranked_available_actions()]
        state.step(names.index("Wait"))
        self.assertIn("Royal Pass", state.inventory)
        self.assertEqual(state.active_event.name, "audience")  # started once the action was resolved


class NoPartialMutationTests(_CourtWorld):
    def _snapshot(self, state):
        return copy.deepcopy((
            state.variables, sorted(state.inventory), state.coins, state.health, dict(state.stats.changes),
            state.current.name, sorted(state.current.connected), sorted(state.removed_npcs), list(state.history),
            state._pending_event, state._story_end, state._mission_completed, state.ended,
            Path(self.store.path).read_bytes() if Path(self.store.path).exists() else None,
        ))

    def test_a_failed_validation_changes_nothing(self):
        state = self._state(store=self.store, profile="p")
        state.apply_effects([{"change_variable": {"reputation": 1}}])  # the profile has a save file
        before = self._snapshot(state)
        valid_prefix = [{"change_variable": {"reputation": 2}}, {"give_item": "Royal Pass"}, {"change_coins": 5},
                        {"change_health": -1}, {"change_stat": {"persuasion": 5}}, {"reveal_location": "Cellar"},
                        {"remove_npc": "Herald"}, {"start_event": "audience"}, {"end_story": "Over."}]
        for bad in ({"set_variable": {"palace_access": "yes"}}, {"move_to": "Atlantis"},
                    {"set_variable": {"playthroughs": 3}}, {"teleport": "Court"}, {"change_variable": {"palace_access": 1}}):
            with self.subTest(bad=bad):
                with self.assertRaises(EffectError):
                    state.apply_effects(valid_prefix + [bad])
                self.assertEqual(self._snapshot(state), before)
                with self.assertRaises(EffectError):  # the same list on an outcome
                    state.apply(Outcome("Something happens.", effects=valid_prefix + [bad]))
                self.assertEqual(self._snapshot(state), before)
        state.apply_effects(valid_prefix)  # without the bad effect, everything applies
        self.assertEqual((state.variables["reputation"], state.ended), (3, True))

    def test_an_invalid_world_is_not_built(self):
        world = _world_definition()
        world["missions"]["audience"]["rewards"] = [{"give_item": "Royal Pass"}, {"give_item": "Crown"}]
        world["locations"]["Court"]["actions"][2]["outcomes"][0]["effects"] = [{"set_variable": {"reputation": "x"}}]
        world["events"]["audience"]["enabled_if"] = {"stat": "luck", "op": "gte", "value": 1}
        errors = logic_errors(world)
        self.assertEqual(len(errors), 3, errors)
        with patch.dict(config._config, {"world_definition": world}), self.assertRaises(ValueError):
            build_world(self.genome)


class AuthoringTests(unittest.TestCase):
    """Studio saves and uploads: validated first, so a rejected change writes nothing."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        for name in ("npcs.yaml", "missions.yaml"):
            shutil.copy(store.CONFIG_DIR / name, self.dir / name)
        for patcher in (
            patch.dict(content.ENTITY_FILES, {"NPCs": (self.dir / "npcs.yaml", "characters", "list")}),
            patch.object(content, "MISSIONS_PATH", self.dir / "missions.yaml"),
            patch.object(store.config, "reload"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_rejected_actions_and_missions_write_nothing(self):
        npcs, missions = (self.dir / "npcs.yaml").read_bytes(), (self.dir / "missions.yaml").read_bytes()
        actions = content.load_entity_actions("NPCs", "Priest")
        actions[0]["enabled_if"] = {"any": [{"has_item": "Vintage Wine"}, {"reputation": {"gte": 1}}]}
        actions[0]["outcomes"][0]["effects"] = [{"give_item": "Vintage Wine"}, {"move_to": "Atlantis"}]
        with self.assertRaises(AuthoringError):
            content.save_entity_actions("NPCs", "Priest", actions)
        self.assertEqual((self.dir / "npcs.yaml").read_bytes(), npcs)
        bad_missions = content.load_missions()
        bad_missions["shield_bearer"]["rewards"] = [{"change_coins": 5}, {"set_variable": {"reputation": "max"}}]
        with self.assertRaises(AuthoringError):
            content.save_missions(bad_missions)
        self.assertEqual((self.dir / "missions.yaml").read_bytes(), missions)

        actions[0]["outcomes"][0]["effects"] = [{"give_item": "Vintage Wine"}]  # now valid: saved
        content.save_entity_actions("NPCs", "Priest", actions)
        saved = next(n for n in yaml.safe_load((self.dir / "npcs.yaml").read_text())["world_definition"]["characters"]
                     if n["name"] == "Priest")
        self.assertEqual(saved["actions"][0]["outcomes"][0]["effects"], [{"give_item": "Vintage Wine"}])

    def test_a_rejected_upload_keeps_the_active_configuration(self):
        active = config.as_dict()
        uploaded = config.default_config()
        uploaded["world_definition"]["missions"]["shield_bearer"]["goal"] = {"condition": {"has_item": "Crown"}}
        path = self.dir / "upload.yaml"
        path.write_text(yaml.safe_dump(uploaded), encoding="utf-8")
        with self.assertRaises(AuthoringError) as raised:
            store.upload_config(str(path))
        self.assertIn("Crown", str(raised.exception))
        self.assertEqual(config.as_dict(), active)


class ShippedContentTests(unittest.TestCase):
    def setUp(self):
        random.seed(11)
        self.genome = Genome()

    def _talk_to_priest(self, **variables):
        state = GameState(engine.build_world(self.genome), self.genome, start="Abandoned Church",
                          rng=random.Random(0), variables=variables)
        talk = next(a for a in state.get_available_actions() if a.character_name == "Priest" and a.source == "conversation")
        return talk.outcomes[0].desc

    def test_the_priest_speaks_by_condition(self):
        self.assertEqual(self._talk_to_priest(reputation=3), "The whole city speaks well of you, child. Walk in peace.")
        self.assertTrue(self._talk_to_priest(reputation=-1).startswith("I hear troubling things"))
        self.assertFalse(self._talk_to_priest(reputation=0).startswith(("The whole city", "I hear")))  # his usual words

    def test_mission_rewards(self):
        state = GameState(engine.build_world(self.genome), self.genome, start="Market", rng=random.Random(0),
                          mission="rid_the_market_of_the_thief")
        coins, reputation = state.coins, state.variables["reputation"]
        state.apply_effects([{"remove_npc": "Thief"}])
        self.assertTrue(state.won)
        self.assertEqual((state.coins, state.variables["reputation"]), (coins + 10, reputation + 1))


if __name__ == "__main__":
    unittest.main()
