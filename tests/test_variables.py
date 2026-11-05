"""Persistent variables: definitions, outcomes, enabled_if, storage across playthroughs."""
import json
import os
import random
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

import api
from backend import content, store
import main as engine
from core_engine import Action, Character, GameState, Genome, Location, Mission, Object, Outcome, WorldGraph
from core_engine import variables as persistent
from utils.config_loader import config

SPECS = {
    "met_ghost": {"type": "bool", "default": False},
    "fame": {"type": "int", "default": 0, "min": -2, "max": 3},
}


def _genome():
    random.seed(55)
    genome = Genome()
    genome.event_prob = 0.0
    genome.chaos_bias = 0.0
    return genome


class _TempStore(unittest.TestCase):
    """Isolates each test from the real saves/ directory and the shipped variables."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.store = persistent.VariableStore(str(self.dir / "save.json"))
        env = patch.dict(os.environ, {"ANE_SAVE_DIR": str(self.dir)})
        env.start()
        self.addCleanup(env.stop)


class ModuleTests(_TempStore):
    def test_conditions(self):
        values = {"met_ghost": True, "fame": 2, "playthroughs": 1}
        self.assertTrue(persistent.check({"met_ghost": True, "fame": {"gte": 2, "lt": 3}}, values))
        self.assertFalse(persistent.check({"fame": {"gt": 2}}, values))
        self.assertTrue(persistent.check({"fame": {"in": [1, 2]}, "playthroughs": {"ne": 0}}, values))
        self.assertTrue(persistent.check(None, values))

    def test_validation(self):
        self.assertEqual(persistent.validate_definitions(SPECS), [])
        self.assertTrue(persistent.validate_definitions({"x": {"type": "int", "default": "no"}}))
        self.assertTrue(persistent.validate_definitions({"x": {"type": "int", "default": 0, "min": 5, "max": 1}}))
        self.assertTrue(persistent.validate_definitions({"playthroughs": {"type": "int", "default": 0}}))
        self.assertEqual(persistent.validate_conditions({"fame": {"gte": 1}, "playthroughs": 2}, SPECS), [])
        self.assertTrue(persistent.validate_conditions({"fortune": 1}, SPECS))
        self.assertTrue(persistent.validate_conditions({"fame": {"about": 1}}, SPECS))
        self.assertTrue(persistent.validate_changes({"fame": "high"}, None, SPECS))
        self.assertTrue(persistent.validate_changes(None, {"met_ghost": 1}, SPECS))

    def test_store_round_trip(self):
        self.assertEqual(self.store.load("p1", SPECS), {"met_ghost": False, "fame": 0, "playthroughs": 0})
        self.store.save("p1", {"met_ghost": True, "fame": 3, "playthroughs": 0}, finished=True)
        self.assertEqual(self.store.load("p1", SPECS), {"met_ghost": True, "fame": 3, "playthroughs": 1})
        self.assertEqual(self.store.load("p2", SPECS)["met_ghost"], False)  # profiles are separate
        data = json.loads((self.dir / "save.json").read_text())
        data["profiles"]["p1"]["values"].update(fame="bad", removed_variable=1)
        (self.dir / "save.json").write_text(json.dumps(data))
        self.assertEqual(self.store.load("p1", SPECS)["fame"], 0)  # wrong type falls back to the default
        self.store.reset("p1")
        self.assertEqual(self.store.load("p1", SPECS)["playthroughs"], 0)


def _world():
    world = WorldGraph()
    for name, enabled_if in (("Hall", None), ("Crypt", {"met_ghost": True}), ("Gallery", {"fame": {"gte": 1}})):
        world.add_location(Location(name, [name.lower()], enabled_if=enabled_if))
    hall = world.locations["Hall"]
    hall.connect(world.locations["Crypt"])
    hall.connect(world.locations["Gallery"])
    hall.actions = [
        Action("Call out", [Outcome("A ghost answers.", set_variables={"met_ghost": True})],
               repeat="always", source="location", owner="Hall"),
        Action("Perform", [Outcome("Applause!", change_variables={"fame": 5})],
               repeat="always", source="location", owner="Hall"),
        Action("Sit", [Outcome("You sit.")], repeat="always", source="location", owner="Hall"),
    ]
    world.add_character(Character("Ghost", ["hall"], known_locations=["Hall"], enabled_if={"met_ghost": True},
                                  actions=[Action("(Ghost) Listen", [Outcome("Whispers.")], character_name="Ghost",
                                                  source="npc", owner="Ghost")]))
    world.add_object(Object("Candle", ["hall"], enabled_if={"playthroughs": {"gte": 1}},
                            actions=[Action("[Candle] Light it", [Outcome("Light.")], source="item", owner="Candle")]))
    return world


def _names(state):
    return [a.name for a in state.ranked_available_actions()]


def _choose(state, name):
    return state.step(_names(state).index(name))


class GameStateTests(_TempStore):
    def setUp(self):
        super().setUp()
        specs = patch.dict(config._config["world_definition"], {"variables": SPECS})
        specs.start()
        self.addCleanup(specs.stop)

    def _state(self, **kwargs):
        return GameState(_world(), _genome(), start="Hall", rng=random.Random(0), **kwargs)

    def test_outcomes_change_variables_and_switch_things_on_mid_visit(self):
        state = self._state()
        names = _names(state)
        self.assertNotIn("Move to Crypt", names)
        self.assertNotIn("(Ghost) Listen", names)
        _choose(state, "Call out")
        self.assertTrue(state.variables["met_ghost"])
        self.assertIn("[PERSISTENT] met_ghost: False -> True", state.history)
        names = _names(state)
        self.assertIn("Move to Crypt", names)
        self.assertIn("(Ghost) Listen", names)  # the ghost appears without leaving the Hall

    def test_changes_can_be_hidden_from_the_story(self):
        with patch.dict(config._config, {"messages": {"variable_changed": ""}}):
            state = self._state()
            _choose(state, "Call out")
        self.assertTrue(state.variables["met_ghost"])
        self.assertFalse(any("met_ghost" in line for line in state.history))

    def test_numbers_are_clamped(self):
        state = self._state()
        _choose(state, "Perform")
        self.assertEqual(state.variables["fame"], 3)
        self.assertIn("Move to Gallery", _names(state))

    def test_disabled_locations_cannot_be_reached(self):
        state = self._state()
        self.assertFalse(state._move_to("Crypt"))
        self.assertIsNone(state.world.spawn(state.current, force_target="Crypt", state=state))
        self.assertEqual(GameState(_world(), _genome(), start="Crypt", rng=random.Random(0)).current.name, "Hall")

    def test_simulations_never_write(self):
        state = self._state()
        _choose(state, "Call out")
        state._end("stalled", "x")
        self.assertFalse(any(self.dir.iterdir()))

    def test_real_play_saves_changes_and_counts_playthroughs(self):
        state = self._state(store=self.store, profile="alice")
        self.assertNotIn("[Candle] Light it", _names(state))  # first playthrough
        _choose(state, "Call out")
        self.assertTrue(self.store.load("alice", SPECS)["met_ghost"])  # saved on change
        state._finalize_if_ended() or state._end("stalled", "x")
        second = self._state(store=self.store, profile="alice")
        self.assertEqual(second.variables, {"met_ghost": True, "fame": 0, "playthroughs": 1})
        names = _names(second)
        self.assertIn("(Ghost) Listen", names)
        self.assertIn("[Candle] Light it", names)
        self.assertIn("Move to Crypt", names)

    def test_missions_can_be_gated_and_can_target_variables(self):
        world = _world()
        world.add_mission(Mission("fame", {"variables": {"fame": {"gte": 2}}}, title="Become famous"))
        world.add_mission(Mission("sequel", {"location": "Crypt"}, enabled_if={"playthroughs": {"gte": 1}}))
        with patch.dict(config._config["mission_settings"], {"pursuit_start_step": -1, "pursuit_full_step": 0}):
            state = GameState(world, _genome(), start="Hall", rng=random.Random(0))
            self.assertEqual(state.mission.name, "fame")  # the sequel is not offered yet
            self.assertEqual(state._goal_sites(), {"Hall"})  # where fame can change
            self.assertEqual(state.ranked_available_actions()[0].name, "Perform")
            _choose(state, "Perform")
        self.assertTrue(state.won)


class WorldBuildTests(_TempStore):
    def test_undefined_variables_are_rejected(self):
        bad_npcs = [{**config.get("world_definition.characters")[0], "enabled_if": {"no_such_variable": True}}]
        with patch.dict(config._config["world_definition"], {"characters": bad_npcs}):
            with self.assertRaises(ValueError):
                engine.build_world(_genome())

    def test_shipped_content_across_two_playthroughs(self):
        genome = _genome()
        first = GameState(engine.build_world(genome), genome, start="Market", rng=random.Random(0),
                          store=self.store, mission="rid_the_market_of_the_thief")
        self.assertNotIn("Memory Thief", {c.name for c in first.world.characters if first.is_enabled(c)})
        first.apply(Outcome("You win the fight.", set_variables={"thief_caught": True}))
        first._end("mission", "done")

        second = GameState(engine.build_world(genome), genome, start="Market", rng=random.Random(0), store=self.store)
        enabled = {c.name for c in second.world.characters if second.is_enabled(c)}
        self.assertNotIn("Thief", enabled)
        self.assertIn("Memory Thief", enabled)  # appears from the second playthrough
        self.assertNotIn(second.mission.name, {"rid_the_market_of_the_thief", "expose_the_fence"})

    def test_evolution_does_not_touch_the_store(self):
        engine.evaluate_genome(_genome(), rng_seed=1)
        self.assertFalse((self.dir / "persistent_variables.json").exists())


class EditingTests(_TempStore):
    def setUp(self):
        super().setUp()
        self.npcs = self.dir / "npcs.yaml"
        shutil.copy(store.CONFIG_DIR / "npcs.yaml", self.npcs)
        self.variables_file = self.dir / "variables.yaml"
        self.variables_file.write_text((store.CONFIG_DIR / "variables.yaml").read_text())
        for patcher in (
            patch.dict(content.ENTITY_FILES, {"NPCs": (self.npcs, "characters", "list")}),
            patch.object(content, "VARIABLES_PATH", self.variables_file),
            patch.object(store.config, "reload"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _stored_npc(self, name):
        return next(r for r in yaml.safe_load(self.npcs.read_text())["world_definition"]["characters"] if r["name"] == name)

    def test_enabled_if_in_the_entity_editor(self):
        name, tags, descriptions, goals, *_ = content.load_entity("NPCs", "Priest")
        content.save_entity("NPCs", name, name, tags, descriptions, goals, enabled_if="{reputation: {gte: 1}}")
        self.assertEqual(self._stored_npc("Priest")["enabled_if"], {"reputation": {"gte": 1}})
        self.assertEqual(content.load_entity("NPCs", "Priest")[7], {"reputation": {"gte": 1}})
        content.save_entity("NPCs", name, name, tags, descriptions, goals)  # None keeps it
        self.assertIn("enabled_if", self._stored_npc("Priest"))
        with self.assertRaises(Exception):
            content.save_entity("NPCs", name, name, tags, descriptions, goals, enabled_if="{karma: 1}")
        content.save_entity("NPCs", name, name, tags, descriptions, goals, enabled_if="")
        self.assertNotIn("enabled_if", self._stored_npc("Priest"))

    def test_definitions_editor_validates(self):
        good = "# mine\nworld_definition:\n  variables:\n    luck: {type: int, default: 1}\n"
        self.assertEqual(content.save_variables_yaml(good)[0], good)
        self.assertEqual(self.variables_file.read_text(), good)
        with self.assertRaises(Exception):
            content.save_variables_yaml("world_definition:\n  variables:\n    luck: {type: int, default: lots}\n")
        self.assertEqual(self.variables_file.read_text(), good)

    def test_api(self):
        client = TestClient(api.api)
        self.assertIn("reputation", client.get("/v1/variables").json()["definitions"])
        self.assertEqual(client.put("/v1/variables", json={"variables": {"x": {"type": "bool", "default": 3}}}).status_code, 400)
        self.assertEqual(client.put("/v1/variables", json={"variables": {"x": {"type": "bool", "default": True}}}).status_code, 200)
        persistent.VariableStore().save("default", {"reputation": 2}, finished=True)
        values = client.get("/v1/variables/values").json()
        self.assertEqual(values["playthroughs"], 1)
        self.assertEqual(client.post("/v1/variables/reset").json()["playthroughs"], 0)
        body = client.get("/v1/entities/NPCs/Thief").json()
        self.assertEqual(body["enabled_if"], {"thief_caught": False})


if __name__ == "__main__":
    unittest.main()
