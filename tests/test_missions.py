import random
import unittest
from unittest.mock import patch

import app as studio
import main as engine
from core_engine import Action, Character, Event, GameState, Genome, Location, Mission, Object, Outcome, WorldGraph
from utils.config_loader import config


def _genome():
    random.seed(11)
    genome = Genome()
    genome.event_prob = 0.0
    genome.chaos_bias = 0.0
    return genome


def _world(*missions):
    world = WorldGraph()
    market = Location("Market", ["urban", "market"])
    market.actions = [
        Action("Browse the stalls", [Outcome("You browse.", tags=["neutral"], coin_change=5)]),
        Action("Pray at the shrine", [Outcome("Nothing answers.", success_prob=0.0)]),
    ]
    alley = Location("Alley", ["urban", "dark"])
    market.connect(alley)
    world.add_location(market)
    world.add_location(alley)
    world.add_character(Character(
        "Thief", ["market"], known_locations=["Market"],
        actions=[Action("(Thief) Confront the thief", [Outcome("You step in.")],
                        character_name="Thief", triggers_event="confront")],
    ))
    world.add_event(Event("confront", actions=[
        Action("Fight the thief", [Outcome("They flee.", tags=["combat", "success"], remove_npc=True)]),
    ]))
    for mission in missions:
        world.add_mission(mission)
    return world


def _full_drive():
    """Skip the story-arc pacing so steering is at full strength from step 0."""
    return patch.dict(config._config["mission_settings"], {"pursuit_start_step": -1, "pursuit_full_step": 0})


def _choose(state, name):
    names = [action.name for action in state.ranked_available_actions()]
    return state.step(names.index(name))


class MissionTests(unittest.TestCase):
    def _state(self, *missions, **kwargs):
        return GameState(_world(*missions), _genome(), start="Market", rng=random.Random(2), **kwargs)

    def test_random_choice_skips_missions_already_complete(self):
        chosen = set()
        for seed in range(20):
            state = GameState(
                _world(Mission("here", {"location": "Market"}),
                       Mission("alley", {"location": "Alley"}),
                       Mission("rich", {"coins": 50})),
                _genome(), start="Market", rng=random.Random(seed),
            )
            chosen.add(state.mission.name)
            self.assertTrue(state.history[0].startswith("[MISSION]"))
        self.assertEqual(chosen, {"alley", "rich"})

    def test_action_goal_wins_and_ends_story(self):
        state = self._state(Mission("browse", {"action": "Browse the stalls"}, title="Go shopping"))
        self.assertFalse(state.won)
        self.assertFalse(_choose(state, "Browse the stalls"))
        self.assertTrue(state.won and state.ended)
        self.assertEqual(state.end_reason, "Mission complete: Go shopping.")
        self.assertEqual(state.mission_status, "Go shopping: completed")
        self.assertIn("[MISSION COMPLETE] Go shopping", state.history)
        self.assertFalse(state.step(), "a finished story takes no further steps")

    def test_npc_action_goal_matches_without_prefix_and_failures_do_not_count(self):
        state = self._state(Mission("pray", {"action": "Pray at the shrine"}))
        _choose(state, "Pray at the shrine")
        self.assertFalse(state.won)

        state = self._state(Mission("talk", {"action": "Talk with Thief"}))
        _choose(state, "(Thief) Talk with Thief")
        self.assertTrue(state.won)

    def test_all_goal_conditions_must_hold(self):
        target = config.get("game_state.initial_coins", 5) + 5  # after one "Browse the stalls"
        state = self._state(Mission("both", {"location": "Alley", "coins": target}))
        _choose(state, "Browse the stalls")
        self.assertFalse(state.won)  # enough coins, not yet in the Alley
        _choose(state, "Move to Alley")
        self.assertTrue(state.won)

        state = self._state(Mission("both", {"location": "Alley", "coins": target + 100}))
        _choose(state, "Move to Alley")
        self.assertFalse(state.won)  # in the Alley, not enough coins

    def test_event_and_npc_removal_goals(self):
        state = self._state(Mission("thief", {"npc_removed": "Thief", "event": "confront"}))
        _choose(state, "(Thief) Confront the thief")
        self.assertFalse(state.won)
        self.assertEqual(state.ranked_available_actions()[0].name, "Fight the thief")
        _choose(state, "Fight the thief")
        self.assertTrue(state.won)

    def test_goal_actions_are_ranked_first(self):
        with _full_drive():
            state = self._state(Mission("thief", {"npc_removed": "Thief"}))
            self.assertEqual(state.ranked_available_actions()[0].name, "(Thief) Confront the thief")

    def test_goal_pursuit_is_paced_like_a_story_arc(self):
        state = self._state(Mission("thief", {"npc_removed": "Thief"}))
        start = config.get("mission_settings.pursuit_start_step")
        full = config.get("mission_settings.pursuit_full_step")
        state.actions_taken = start
        self.assertEqual(state._goal_drive(), 0.0)
        state.actions_taken = (start + full) / 2
        self.assertAlmostEqual(state._goal_drive(), state.genome.goal_bias / 2)
        state.actions_taken = full + 5
        self.assertAlmostEqual(state._goal_drive(), state.genome.goal_bias)

    def test_story_can_continue_after_the_goal(self):
        settings = {**config.get("mission_settings", {}), "end_on_goal": False}
        with patch.dict(config._config, {"mission_settings": settings}):
            state = self._state(Mission("browse", {"action": "Browse the stalls"}))
            self.assertTrue(_choose(state, "Browse the stalls"))
        self.assertTrue(state.won)
        self.assertFalse(state.ended)

    def test_win_is_rewarded_by_fitness_after_a_full_story(self):
        bonus = config.get("fitness.base.mission_win_bonus")
        min_steps = config.get("fitness.base.mission_win_min_steps")
        state = self._state(Mission("browse", {"action": "Browse the stalls"}))
        _choose(state, "Browse the stalls")  # won after 1 step
        rushed = engine._fitness_breakdown(state)["ending"]
        state.actions_taken = min_steps
        full = engine._fitness_breakdown(state)["ending"]
        self.assertAlmostEqual(full - rushed, bonus * (1 - 1 / min_steps))

    def test_invalid_goals_are_rejected(self):
        with self.assertRaises(ValueError):
            Mission("bad", {"reach": "Vault"})
        with self.assertRaises(ValueError):
            Mission("empty", {})
        with self.assertRaises(ValueError):
            self._state(Mission("a", {"coins": 99}), mission="missing")

    def test_shipped_missions_build(self):
        world = engine.build_world(_genome())
        self.assertGreaterEqual(len(world.missions), 2)
        state = GameState(world, _genome(), rng=random.Random(0))
        self.assertIn(state.mission.name, world.missions)



def _chain_world(goal):
    """Start - Road - Bridge - Keep in a line, plus an unconnected Tower that the
    Start can only reach by discovering it."""
    world = WorldGraph()
    names = ["Start", "Road", "Bridge", "Keep", "Tower", "Cell"]
    for name in names:
        world.add_location(Location(name, [name.lower()]))
    for first, second in (("Start", "Road"), ("Road", "Bridge"), ("Bridge", "Keep"), ("Start", "Cell")):
        world.locations[first].connect(world.locations[second])
    world.locations["Start"].actions = [
        Action("Sing a song", [Outcome("You sing.", tags=["social"], coin_change=1)]),
        Action("Scout the hills", [Outcome("A path appears.", spawn=True)]),
    ]
    world.allow_spawn("Start", "Tower")
    world.add_mission(Mission("goal", goal))
    return world


class RoutePlanningTests(unittest.TestCase):
    def setUp(self):
        patcher = _full_drive()
        patcher.start()
        self.addCleanup(patcher.stop)

    def _state(self, goal):
        genome = _genome()
        genome.goal_bias = 1.0
        return GameState(_chain_world(goal), genome, start="Start", rng=random.Random(0))

    def test_travel_follows_the_shortest_known_route(self):
        state = self._state({"location": "Keep"})
        self.assertEqual(state._goal_distances()["Start"], 3)
        # Discovery is worth nothing here: the Tower is not on the way.
        self.assertEqual(state.ranked_available_actions()[0].name, "Move to Road")
        _choose(state, "Move to Road")
        self.assertEqual(state.ranked_available_actions()[0].name, "Move to Bridge")

    def test_undiscovered_goal_is_reached_by_discovering_a_path(self):
        state = self._state({"location": "Tower"})
        self.assertEqual(state._goal_distances()["Start"], config.get("mission_settings.discovery_route_cost", 2))
        self.assertEqual(state.ranked_available_actions()[0].name, "Scout the hills")
        self.assertIs(state.world.spawn(state.current, state=state), state.world.locations["Tower"])

    def test_locked_locations_are_dead_ends(self):
        with patch.dict(config._config, {"location_constraints": {"Cell": {"requires_escape": True}}}):
            state = self._state({"location": "Keep"})
            state.world.locations["Cell"].connect(state.world.locations["Keep"])
            # Nobody was sent to the Cell, so it is an ordinary shortcut.
            self.assertEqual(state._goal_distances()["Start"], 2)
        constraints = {"Cell": {"requires_escape": True, "lock_when": "always"}}
        with patch.dict(config._config, {"location_constraints": constraints}):
            state = self._state({"location": "Keep"})
            state.world.locations["Cell"].connect(state.world.locations["Keep"])
            self.assertEqual(state._goal_distances()["Start"], 3)  # not 2 through the locked Cell

    def test_item_goals_route_to_where_the_item_is_visible(self):
        state = self._state({"item": "Lantern"})
        state.world.add_object(Object("Lantern", ["keep"], collectible=True, actions=[
            Action("[Lantern] Take the lantern", [Outcome("Got it.")], collects_object="Lantern"),
        ]))
        self.assertEqual(state._goal_sites(), {"Keep"})
        self.assertEqual(state.ranked_available_actions()[0].name, "Move to Road")


class GoalBiasCompatibilityTests(unittest.TestCase):
    def test_bundles_saved_before_goal_bias_still_load(self):
        values = {key: 0.5 for key in vars(Genome()) if key != "goal_bias"}
        genome = studio._validated_genome(values)
        low, high = config.get("genome_bounds.goal_bias")
        self.assertAlmostEqual(genome.goal_bias, (low + high) / 2)
        self.assertIsNotNone(studio._model_genome_values(values))

    def test_other_missing_parameters_are_still_rejected(self):
        values = {key: 0.5 for key in vars(Genome()) if key != "voyage_bias"}
        with self.assertRaises(Exception):
            studio._validated_genome(values)


if __name__ == "__main__":
    unittest.main()
