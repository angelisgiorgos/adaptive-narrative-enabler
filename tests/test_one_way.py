"""Walking into a prison by a normal route vs being sent there one way."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import random
import unittest
from unittest.mock import patch

import main as engine
from core_engine import Action, Character, GameState, Genome, Location, Outcome, WorldGraph
from core_engine.builder import _build_outcome
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
        outcome = _build_outcome({"desc": "Seized!", "send_to": "Dungeon"}, _genome())
        self.assertEqual(outcome.send_to, "Dungeon")


class ShippedDungeonTests(unittest.TestCase):
    def test_the_shipped_dungeon_is_only_reached_by_being_sent(self):
        # location_constraints.Dungeon.entry: sent; nor does the Palace border the cells.
        genome = _genome()
        state = GameState(engine.build_world(genome), genome, start="Palace", rng=random.Random(0))
        self.assertFalse(state._move_to("Dungeon"))
        state.world.locations["Palace"].connect(state.world.locations["Dungeon"])  # even a known way
        self.assertFalse(state._move_to("Dungeon"))
        self.assertEqual(state.current.name, "Palace")
        self.assertFalse(any(name.endswith("Dungeon") for name in _names(state)))

        state = GameState(engine.build_world(genome), genome, start="Palace", rng=random.Random(0))
        self.assertTrue(state._move_to("Dungeon", one_way=True))  # an arrest
        self.assertTrue(state._location_is_locked())
        self.assertEqual(state.neighbours("Dungeon"), {"Alley", "Sewers"})
        state.confined_in = None  # escaped: the Palace is reached through the Sewers' basement passage
        self.assertTrue(state._move_to("Palace"))
        self.assertEqual(state.current.name, "Sewers")
        self.assertTrue(any(line.startswith("[ROUTE] No road runs straight from the Dungeon to the Palace")
                            for line in state.history))

    def test_arrest_into_the_dungeon_cannot_be_walked_back(self):
        genome = _genome()
        state = GameState(engine.build_world(genome), genome, start="Palace", rng=random.Random(0))
        state._move_to(config.get("game_state.arrest_location"), one_way=True)
        self.assertTrue(state._location_is_locked())
        self.assertNotIn("Palace", state.world.locations["Dungeon"].connected)



class PrisonReleaseTests(unittest.TestCase):
    """A prison never ends the story as a stall when the director lets the player out (LIMITATIONS #4):
    the release also happens on a turn that ends in a plain failure, and once an event in the cell is
    over. Without a director or an encounter's captive_release, a stall still ends the story."""

    def setUp(self):
        constraints = {"Cell": {"requires_escape": True, "lock_when": "sent", "escape_tags": ["escape"]}}
        for patcher in (
            patch.dict(config._config, {"location_constraints": constraints}),
            patch.dict(config._config["game_state"], {"arrest_location": "Cell"}),
            patch.dict(config._config["narrative_end"], {"max_consecutive_steps_in_location": 5}),
            patch.dict(config._config, {"director": {**(config._config.get("director") or {}), "enabled": True,
                                                     "release_after": 5}}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        world = _world()
        world.locations["Cell"].actions = [  # the lock never gives: every turn is a plain failure
            Action("Pick the lock", [Outcome("Click.", tags=["escape"], success_prob=0.0)], repeat="always",
                   source="location", owner="Cell"),
        ]
        self.state = GameState(world, _genome(), start="Road", rng=random.Random(0))
        self.state._move_to("Cell", one_way=True)

    def test_failing_every_escape_is_released_not_stalled(self):
        for _ in range(8):
            if self.state.ended or self.state.confined_in is None:
                break
            _choose(self.state, "Pick the lock")
        self.assertFalse(self.state.ended, self.state.end_reason)
        self.assertIsNone(self.state.confined_in)
        self.assertNotEqual(self.state.end_kind, "stalled")

    def test_an_event_in_the_cell_defers_the_release(self):
        self.state.consecutive_steps_in_location = 5
        self.state.active_event = object()           # the guards come running
        self.assertFalse(self.state._check_end_conditions())
        self.assertEqual(self.state.confined_in, "Cell")  # held while the event lasts, never ended as a stall
        self.state.active_event = None
        self.assertFalse(self.state._check_end_conditions())
        self.assertIsNone(self.state.confined_in)          # let out once it is over

    def test_without_a_director_a_stall_still_ends_the_story(self):
        config._config["director"]["enabled"] = False
        self.state.consecutive_steps_in_location = 5
        self.assertTrue(self.state._check_end_conditions())
        self.assertEqual(self.state.end_kind, "stalled")


class SmallTalkTests(unittest.TestCase):
    """Small talk (no authored line) would be told word for word again: once per visit (LIMITATIONS #8).
    Authored dialogue can change from one turn to the next and stays on offer."""

    def _talk(self, state, name):
        return next(a for a in state.ranked_available_actions() if a.name == f"({name}) Talk with {name}")

    def test_small_talk_is_once_per_visit_and_dialogue_stays(self):
        from core_engine.world import DialogueLine

        world = _world()
        world.add_character(Character("Herald", ["road"], known_locations=["Road"], descriptions=["A herald."],
                                      dialogue=[DialogueLine("Hear ye!")]))
        state = GameState(world, _genome(), start="Road", rng=random.Random(0))
        self.assertEqual(self._talk(state, "Warden").repeat, "per_visit")
        self.assertEqual(self._talk(state, "Herald").repeat, "always")


if __name__ == "__main__":
    unittest.main()
