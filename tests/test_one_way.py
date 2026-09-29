"""Walking into a prison by a normal route vs being sent there one way."""
import random
import unittest
from unittest.mock import patch

import main as engine
from core_engine import Action, Character, GameState, Genome, Location, Outcome, WorldGraph
from utils.config_loader import config


def _genome():
    random.seed(44)
    genome = Genome()
    genome.event_prob = 0.0
    genome.chaos_bias = 0.0
    return genome


def _world():
    world = WorldGraph()
    for name in ("Road", "Gate", "Cell"):
        world.add_location(Location(name, [name.lower()]))
    road, gate, cell = (world.locations[n] for n in ("Road", "Gate", "Cell"))
    road.connect(gate)
    road.actions = [
        Action("Wait on the road", [Outcome("You wait.")], repeat="always", source="location", owner="Road"),
        Action("Get thrown in the cell", [Outcome("Seized!", send_to="Cell")], source="location", owner="Road"),
    ]
    cell.actions = [
        Action("Pick the lock", [Outcome("Click.", tags=["escape"])], source="location", owner="Cell"),
        Action("Sit in the dark", [Outcome("Time passes.")], repeat="always", source="location", owner="Cell"),
    ]
    world.add_character(Character("Warden", ["road"], known_locations=["Road"], actions=[
        Action("(Warden) Fight the warden", [Outcome("You swing.", tags=["combat"], success_prob=0.0)],
               character_name="Warden", source="npc", owner="Warden"),
    ]))
    return world


def _names(state):
    return [a.name for a in state.ranked_available_actions()]


def _choose(state, name):
    return state.step(_names(state).index(name))


class OneWayTests(unittest.TestCase):
    def setUp(self):
        constraints = {"Cell": {"requires_escape": True, "lock_when": "sent", "escape_tags": ["escape"]}}
        for patcher in (
            patch.dict(config._config, {"location_constraints": constraints}),
            patch.dict(config._config["game_state"], {"arrest_location": "Cell"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.state = GameState(_world(), _genome(), start="Road", rng=random.Random(0))

    def test_a_normal_route_can_be_walked_back(self):
        self.state.world.locations["Road"].connect(self.state.world.locations["Cell"])
        _choose(self.state, "Move to Cell")
        self.assertFalse(self.state._location_is_locked())
        self.assertIn("Return to Road", _names(self.state))

    def test_being_sent_is_one_way_and_confines_until_escape(self):
        _choose(self.state, "Get thrown in the cell")
        self.assertEqual(self.state.current.name, "Cell")
        self.assertTrue(self.state._location_is_locked())
        self.assertIn("Cell", self.state.world.locations["Road"].connected)
        self.assertNotIn("Road", self.state.world.locations["Cell"].connected)
        self.assertTrue(any(line.startswith("[SENT] You are taken to the Cell") for line in self.state.history))
        self.assertFalse(any(name.startswith(("Move to", "Return to")) for name in _names(self.state)))

        _choose(self.state, "Pick the lock")
        self.assertIsNone(self.state.confined_in)
        self.assertFalse(self.state._location_is_locked("Cell"))
        self.assertNotIn("Return to Road", _names(self.state))  # still no way back that way

    def test_losing_to_a_guard_sends_the_player_one_way(self):
        with patch.object(GameState, "_is_guard_failure", lambda self, action, outcome: action.character_name == "Warden"):
            _choose(self.state, "(Warden) Fight the warden")
        self.assertEqual((self.state.current.name, self.state.confined_in), ("Cell", "Cell"))
        self.assertNotIn("Road", self.state.world.locations["Cell"].connected)

    def test_every_arrest_needs_a_new_escape(self):
        self.state._move_to("Cell", one_way=True)
        _choose(self.state, "Pick the lock")
        self.state.current = self.state.world.locations["Road"]
        self.state._move_to("Cell", one_way=True)
        self.assertTrue(self.state._location_is_locked())

    def test_lock_when_always_keeps_the_old_rule(self):
        config._config["location_constraints"]["Cell"]["lock_when"] = "always"
        self.state.world.locations["Road"].connect(self.state.world.locations["Cell"])
        _choose(self.state, "Move to Cell")
        self.assertTrue(self.state._location_is_locked())

    def test_send_to_is_read_from_yaml(self):
        outcome = engine._build_outcome({"desc": "Seized!", "send_to": "Dungeon"}, _genome())
        self.assertEqual(outcome.send_to, "Dungeon")


class ShippedDungeonTests(unittest.TestCase):
    def test_dungeon_found_by_a_normal_route_can_be_left(self):
        genome = _genome()
        state = GameState(engine.build_world(genome), genome, start="Palace", rng=random.Random(0))
        palace, dungeon = state.world.locations["Palace"], state.world.locations["Dungeon"]
        palace.connect(dungeon)  # e.g. "Whispers of a prisoner in the Dungeon are heard."
        state._move_to("Dungeon")
        self.assertFalse(state._location_is_locked())
        self.assertIn("Return to Palace", _names(state))

    def test_arrest_into_the_dungeon_cannot_be_walked_back(self):
        genome = _genome()
        state = GameState(engine.build_world(genome), genome, start="Palace", rng=random.Random(0))
        state._move_to(config.get("game_state.arrest_location"), one_way=True)
        self.assertTrue(state._location_is_locked())
        self.assertNotIn("Palace", state.world.locations["Dungeon"].connected)


if __name__ == "__main__":
    unittest.main()
