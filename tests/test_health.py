"""HealthPlayer bounds, combat rules, power, and its use by GameState."""
import random
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import app as studio
import main as engine
from core_engine import Action, GameState, Genome, HealthPlayer, Location, Outcome, WorldGraph
from utils.config_loader import config


def _genome():
    random.seed(21)
    genome = Genome()
    genome.event_prob = 0.0
    genome.chaos_bias = 0.0
    return genome


class HealthPlayerTests(unittest.TestCase):
    def test_healing_never_exceeds_the_maximum(self):
        player = HealthPlayer(initial=8, maximum=10)
        self.assertEqual(player.heal(5), 2)
        self.assertEqual(player.health, 10)
        self.assertTrue(player.is_full)
        self.assertEqual(player.heal(3), 0)
        self.assertEqual((player.healing_received, player.healing_wasted), (2, 6))

    def test_damage_never_goes_below_zero(self):
        player = HealthPlayer(initial=3, maximum=10)
        self.assertEqual(player.damage(5), 3)
        self.assertEqual(player.health, 0)
        self.assertTrue(player.is_dead)
        self.assertEqual((player.damage_taken, player.lowest_health), (3, 0))

    def test_change_set_and_effective_change(self):
        player = HealthPlayer(initial=8, maximum=10)
        self.assertEqual(player.effective_change(+4), 2)  # preview only
        self.assertEqual(player.health, 8)
        self.assertEqual(player.change(-3), -3)
        self.assertEqual(player.set(99), 5)
        self.assertEqual(player.health, 10)
        player.set(-4)
        self.assertEqual(player.health, 0)
        self.assertEqual(player.restore_full(), 10)

    def test_state_properties(self):
        player = HealthPlayer(initial=4, maximum=10, low_threshold=4)
        self.assertTrue(player.is_low and player.is_alive)
        self.assertEqual((player.missing, player.ratio, player.status()), (6, 0.4, "4/10"))

    def test_starting_health_is_capped_and_maximum_validated(self):
        self.assertEqual(HealthPlayer(initial=50, maximum=10).health, 10)
        with self.assertRaises(ValueError):
            HealthPlayer(maximum=0)

    def test_power_is_clamped(self):
        max_power = config.get("power_model.max_power")
        player = HealthPlayer(initial=10, maximum=10)
        self.assertEqual(player.calculate_power(attack_bonus=100, defense_bonus=100), max_power)
        player.set(0)
        self.assertEqual(player.calculate_power(), config.get("power_model.min_power"))

    def test_combat_rules(self):
        # Mitigation softens damage but never turns it into healing.
        self.assertEqual(HealthPlayer.mitigated_combat_change(-4, 0, 0), -4)
        self.assertEqual(HealthPlayer.mitigated_combat_change(-1, 10, 10), 0)
        self.assertEqual(HealthPlayer.mitigated_combat_change(+2, 10, 10), 2)
        self.assertEqual(HealthPlayer.failure_penalty(0, dangerous=True), 1)
        self.assertEqual(HealthPlayer.failure_penalty(10, dangerous=True), 0)
        self.assertEqual(HealthPlayer.failure_penalty(10, dangerous=False), 1)


class GameStateHealthTests(unittest.TestCase):
    def _state(self):
        world = WorldGraph()
        shrine = Location("Shrine", ["holy"])
        shrine.actions = [
            Action("Pray", [Outcome("You feel restored.", health_change=5)]),
            Action("Pray again", [Outcome("Warmth fills you.", health_change=5)]),
        ]
        world.add_location(shrine)
        return GameState(world, _genome(), start="Shrine", rng=random.Random(0))

    def test_repeated_healing_is_capped(self):
        state = self._state()
        state.step([a.name for a in state.ranked_available_actions()].index("Pray"))
        state.step([a.name for a in state.ranked_available_actions()].index("Pray again"))
        self.assertEqual(state.health, state.max_health)
        self.assertTrue(any(line.startswith("[HEALTH] Already at") for line in state.history))

    def test_assigning_health_is_clamped(self):
        state = self._state()
        state.health = 1000
        self.assertEqual(state.health, state.max_health)
        state.health -= 1000
        self.assertEqual(state.health, 0)

    def test_healing_at_full_health_is_not_valued(self):
        state = self._state()
        heal = Outcome("Heal.", health_change=5)
        action = Action("x", [heal])
        with patch.dict(config._config["outcome_objective"], {"health_weight": 1000}):
            state.health = state.max_health - 5
            hurt_score = state._outcome_objective_score(heal, action)
            state.health = state.max_health
            full_score = state._outcome_objective_score(heal, action)
        self.assertGreater(hurt_score, 4000)
        self.assertLess(full_score, 100)

    def test_health_stays_within_bounds_in_full_games(self):
        for seed in range(150):
            random.seed(seed)
            genome = Genome()
            state = GameState(engine.build_world(genome), genome, rng=random.Random(seed))
            for _ in range(100):
                self.assertTrue(0 <= state.health <= state.max_health, f"seed {seed}: {state.health}")
                self.assertLessEqual(state.power, config.get("power_model.max_power"))
                if not state.step():
                    break
            self.assertLessEqual(state.health_player.highest_health, state.max_health)


class HealthSettingsTests(unittest.TestCase):
    def test_starting_health_cannot_exceed_maximum(self):
        values = list(studio.load_settings())
        with self.assertRaises(Exception):
            studio.save_settings(*values[:4], 20, *values[5:9], max_health=10)

    def test_api_exposes_maximum_health(self):
        response = TestClient(api.api).get("/v1/settings")
        self.assertEqual(response.json()["max_health"], config.get("game_state.max_health"))


if __name__ == "__main__":
    unittest.main()
