"""Unique vs generic NPC placement, and the unique flag in the editor and API."""
import random
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

import api
import app as studio
import main as engine
from core_engine import Action, Character, GameState, Genome, Location, Outcome, WorldGraph
from utils.config_loader import config


def _genome():
    random.seed(5)
    genome = Genome()
    genome.event_prob = 0.0
    genome.chaos_bias = 0.0
    return genome


def _world():
    world = WorldGraph()
    harbor = Location("Dockside", ["port", "sea"])
    tavern = Location("Taproom", ["port", "ale"])
    crypt = Location("Crypt", ["spectral", "underground"])
    for location in (harbor, tavern, crypt):
        location.actions = [Action(f"Look around the {location.name}", [Outcome("You look.")])]
        world.add_location(location)
    harbor.connect(tavern)
    harbor.connect(crypt)
    # Known at the Dockside, but the Crypt matches the ghost's tags far better.
    world.add_character(Character("Ghost", ["spectral", "underground"],
                                  known_locations=["Dockside", "Crypt"], unique=True))
    world.add_character(Character("Sailor", ["port"], unique=False,
                                  actions=[Action("(Sailor) Swap stories", [Outcome("Tall tales.")])]))
    return world


def _state(world=None):
    return GameState(world or _world(), _genome(), start="Dockside", rng=random.Random(3))


def _present(state):
    state.get_available_actions()
    return {c.name for c in state.current_inhabitants if c.name in state.visible_inhabitants}


class NPCPlacementTests(unittest.TestCase):
    def test_unique_npc_lives_only_at_its_best_fitting_home(self):
        state = _state()
        self.assertEqual(state.npc_homes, {"Ghost": "Crypt"})
        self.assertNotIn("Ghost", _present(state))  # known at the Dockside, but lives in the Crypt
        state._move_to("Crypt")
        self.assertIn("Ghost", _present(state))
        state._move_to("Dockside")
        self.assertNotIn("Ghost", _present(state))

    def test_generic_npc_appears_everywhere_it_fits(self):
        state = _state()
        self.assertIn("Sailor", _present(state))
        state._move_to("Taproom")
        self.assertIn("Sailor", _present(state))
        state._move_to("Crypt")
        self.assertNotIn("Sailor", _present(state))

    def test_unique_resident_is_always_present(self):
        world = _world()
        for index in range(4):
            world.add_character(Character(f"Shade {index}", ["spectral", "underground"]))
        with patch.dict(config._config["scene"], {"max_visible_npcs": 1}):
            state = _state(world)
            state._move_to("Crypt")
            self.assertEqual(_present(state), {"Ghost"})

    def test_reveal_respects_unique_homes_and_adds_generic_npcs(self):
        state = _state()
        _present(state)
        state.apply(Outcome("A chill passes.", reveal_npc="Ghost"))
        self.assertNotIn("Ghost", _present(state))

        state._move_to("Crypt")
        _present(state)
        state.apply(Outcome("Someone waves from the dark.", reveal_npc="Sailor"))
        self.assertIn("Sailor", _present(state))
        self.assertIn("(Sailor) Swap stories", [a.name for a in state.get_available_actions()])

    def test_shipped_npcs_declare_unique_and_have_consistent_homes(self):
        characters = config.get("world_definition.characters")
        self.assertTrue(all(isinstance(c.get("unique"), bool) for c in characters))
        state = GameState(engine.build_world(_genome()), _genome())
        self.assertEqual(state.npc_homes["Pale Conductor"], "Ghostline Station")
        self.assertEqual(state.npc_homes["Thief"], "Market")
        self.assertNotIn("Gate Guard", state.npc_homes)

    def test_upload_rejects_non_boolean_unique(self):
        candidate = config.default_config()
        candidate["world_definition"]["characters"][0]["unique"] = "yes"
        errors = studio._validate_complete_config(candidate, config.default_config())
        self.assertTrue(any("unique" in error for error in errors))


class UniqueFlagEditingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "npcs.yaml"
        shutil.copy(studio.CONFIG_DIR / "npcs.yaml", self.path)
        patcher = patch.dict(studio.ENTITY_FILES, {"NPCs": (self.path, "characters", "list")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(api.api)

    def _stored(self, name):
        records = yaml.safe_load(self.path.read_text())["world_definition"]["characters"]
        return next(r for r in records if r["name"] == name)

    def test_editor_round_trip(self):
        name, tags, descriptions, goals, unique, *_ = studio.load_entity("NPCs", "Gate Guard")
        self.assertFalse(unique)
        studio.save_entity("NPCs", name, name, tags, descriptions, goals, True)
        self.assertTrue(self._stored("Gate Guard")["unique"])
        studio.save_entity("NPCs", name, name, tags, descriptions, goals)  # None keeps it
        self.assertTrue(self._stored("Gate Guard")["unique"])
        self.assertIsNone(studio.load_entity("Locations", "Harbor")[4])

    def test_api_get_and_put(self):
        response = self.client.get("/v1/entities/NPCs/Pale Conductor")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["unique"])
        body = {**response.json(), "unique": False}
        self.assertFalse(self.client.put("/v1/entities/NPCs/Pale Conductor", json=body).json()["unique"])
        self.assertFalse(self._stored("Pale Conductor")["unique"])
        self.assertIsNone(self.client.get("/v1/entities/Locations/Harbor").json()["unique"])


if __name__ == "__main__":
    unittest.main()
