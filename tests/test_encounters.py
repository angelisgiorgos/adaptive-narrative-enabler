"""Hostile NPCs that start an event by themselves, and generic NPC removal."""
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
from core_engine import Action, Character, Event, GameState, Genome, Location, Outcome, WorldGraph
from utils.config_loader import config


def _genome():
    random.seed(9)
    genome = Genome()
    genome.event_prob = 0.0
    genome.chaos_bias = 0.0
    return genome


def _world(repeat=False, unique=False, event="ambush"):
    world = WorldGraph()
    for name in ("Lane", "Square", "Yard"):
        location = Location(name, ["street"])
        location.actions = [Action(f"Look around the {name}", [Outcome("You look.")])]
        world.add_location(location)
    world.locations["Lane"].connect(world.locations["Square"])
    world.locations["Square"].connect(world.locations["Yard"])
    world.add_character(Character(
        "Cutthroat", ["street"], unique=unique, encounter_event=event, encounter_repeat=repeat,
        actions=[Action("(Cutthroat) Haggle", [Outcome("They laugh.")])],
    ))
    world.add_event(Event("ambush", title="Ambush", descriptions=["A blade in the dark."], actions=[
        Action("Fight", [Outcome("They flee.", tags=["combat", "success"], remove_npc=True)]),
        Action("Stand still", [Outcome("They size you up.")]),
    ]))
    return world


def _state(**kwargs):
    return GameState(_world(**kwargs), _genome(), start="Lane", rng=random.Random(4))


def _names(state):
    return [a.name for a in state.ranked_available_actions()]


def _choose(state, name):
    return state.step(_names(state).index(name))


class EncounterTests(unittest.TestCase):
    def test_hostile_npc_confronts_before_anything_else(self):
        state = _state()
        self.assertCountEqual(_names(state), ["Fight", "Stand still"])  # no travel, no looking around
        self.assertEqual(state.active_event.name, "ambush")
        self.assertEqual(state.event_source_npc, "Cutthroat")
        self.assertIn("[ENCOUNTER] Cutthroat confronts you!", state.history)
        _choose(state, "Stand still")
        self.assertIsNone(state.active_event)
        self.assertIn("Move to Square", _names(state))
        self.assertIn("(Cutthroat) Haggle", _names(state))  # still here, but no second ambush

    def test_first_encounter_only_by_default(self):
        state = _state()
        _choose(state, "Stand still")
        _choose(state, "Move to Square")
        self.assertIsNone(state.active_event)
        self.assertNotIn("Fight", _names(state))

    def test_repeat_encounters_happen_once_per_visit(self):
        state = _state(repeat=True)
        _choose(state, "Stand still")
        self.assertNotIn("Fight", _names(state))  # same visit
        _choose(state, "Move to Square")
        self.assertEqual(state.active_event.name, "ambush")

    def test_removing_a_generic_npc_only_clears_this_visit(self):
        state = _state(repeat=True)
        _choose(state, "Fight")
        self.assertIn("[NPC REMOVED] Cutthroat leaves the Lane.", state.history)
        self.assertNotIn("(Cutthroat) Haggle", _names(state))
        _choose(state, "Move to Square")
        self.assertEqual(state.active_event.name, "ambush")  # another cutthroat elsewhere
        self.assertIn("Cutthroat", state.removed_npcs)  # still counts for npc_removed goals

    def test_removing_a_unique_npc_is_permanent(self):
        world = _world(unique=True)
        world.locations["Lane"].tags = {"street", "home"}
        world.characters[0].associated_tags = {"street", "home"}
        state = GameState(world, _genome(), start="Lane", rng=random.Random(4))
        _choose(state, "Fight")
        self.assertIn("is no longer part of the story", state.history[-3] + state.history[-2] + state.history[-1]
                      or "".join(state.history))
        _choose(state, "Move to Square")
        _choose(state, "Return to Lane")
        self.assertNotIn("(Cutthroat) Haggle", _names(state))

    def test_unknown_encounter_event_is_ignored(self):
        state = _state(event="missing")
        self.assertIn("Look around the Lane", _names(state))
        self.assertIn("[EVENT] Unknown event 'missing' was ignored.", state.history)

    def test_shipped_hostile_npcs(self):
        world = engine.build_world(_genome())
        bandit = next(c for c in world.characters if c.name == "Bandit")
        thief = next(c for c in world.characters if c.name == "Memory Thief")
        self.assertEqual((bandit.encounter_event, bandit.encounter_repeat), ("bandit_ambush", True))
        self.assertEqual((thief.encounter_event, thief.encounter_repeat), ("memory_theft", False))
        state = GameState(world, _genome(), start="Alley", rng=random.Random(0))
        state.get_available_actions()
        if "Bandit" in state.visible_inhabitants:
            self.assertEqual(state.active_event.name, "bandit_ambush")


class EncounterEditingTests(unittest.TestCase):
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

    def test_editor_sets_keeps_and_clears_encounters(self):
        name, tags, descriptions, goals, *_ = studio.load_entity("NPCs", "Gate Guard")
        studio.save_entity("NPCs", name, name, tags, descriptions, goals, None, "bandit_ambush", True)
        self.assertEqual(studio.load_entity("NPCs", "Gate Guard")[5:], ("bandit_ambush", True))
        studio.save_entity("NPCs", name, name, tags, descriptions, goals)
        self.assertEqual(self._stored("Gate Guard")["encounter_event"], "bandit_ambush")
        studio.save_entity("NPCs", name, name, tags, descriptions, goals, None, "", True)
        self.assertNotIn("encounter_event", self._stored("Gate Guard"))
        self.assertNotIn("encounter_repeat", self._stored("Gate Guard"))
        with self.assertRaises(Exception):
            studio.save_entity("NPCs", name, name, tags, descriptions, goals, None, "no_such_event")

    def test_api_round_trip(self):
        bandit = self.client.get("/v1/entities/NPCs/Bandit").json()
        self.assertEqual((bandit["encounter_event"], bandit["encounter_repeat"]), ("bandit_ambush", True))
        body = {**bandit, "encounter_repeat": False}
        self.assertFalse(self.client.put("/v1/entities/NPCs/Bandit", json=body).json()["encounter_repeat"])
        self.assertEqual(self.client.put("/v1/entities/NPCs/Bandit", json={**body, "encounter_event": ""}).json()["encounter_event"], None)
        self.assertEqual(self.client.put("/v1/entities/NPCs/Bandit", json={**body, "encounter_event": "nope"}).status_code, 400)

    def test_upload_rejects_bad_encounter_types(self):
        candidate = config.default_config()
        candidate["world_definition"]["characters"][0]["encounter_repeat"] = "often"
        errors = studio._validate_complete_config(candidate, config.default_config())
        self.assertTrue(any("encounter_repeat" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
