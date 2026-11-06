"""Character stats, skill checks, and the combat system."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
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
from core_engine import Action, Character, GameState, Genome, Location, Object, Outcome, WorldGraph
from core_engine.combat import CombatSystem
from core_engine.stats import CharacterStats, validate_settings
from utils.config_loader import config

SOCIAL = {
    "stats": {"stealth": {}, "persuasion": {}, "watchfulness": {}, "intimidation": {}},
    "allocation": {"total_points": 50, "min_points": 5, "max_points": 20, "value_per_point": 5},
    "mode": "balanced",
    "tag_checks": {"stealth": "stealth"},
    "damage_reduction_stat": "intimidation",
}


def _genome():
    random.seed(66)
    genome = Genome()
    genome.event_prob = 0.0
    genome.chaos_bias = 0.0
    return genome


class _Dice:
    """A random generator whose rolls are chosen by the test."""

    def __init__(self, *rolls):
        self.rolls = list(rolls)

    def randint(self, low, high):
        return self.rolls.pop(0)

    def choice(self, options):
        return options[0]


class _SocialStory(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(config._config["world_definition"], {"character_stats": SOCIAL})
        patcher.start()
        self.addCleanup(patcher.stop)


class CharacterStatsTests(_SocialStory):
    def test_points_budget(self):
        names, budget = list(SOCIAL["stats"]), CharacterStats(mode="balanced").budget
        good = {"stealth": 20, "persuasion": 15, "watchfulness": 10, "intimidation": 5}
        self.assertEqual(CharacterStats.validate_points(good, names, budget), [])
        for bad in ({**good, "stealth": 21, "intimidation": 4}, {**good, "stealth": 19},
                    {k: v for k, v in good.items() if k != "stealth"}, {**good, "luck": 5}):
            with self.subTest(points=bad):
                self.assertTrue(CharacterStats.validate_points(bad, names, budget))
        with self.assertRaises(ValueError):
            CharacterStats(points={**good, "stealth": 30})

    def test_random_allocation_respects_the_limits_and_is_reproducible(self):
        splits = []
        for seed in range(30):
            points = CharacterStats(mode="random", rng=random.Random(seed)).points
            self.assertEqual(sum(points.values()), 50)
            self.assertTrue(all(5 <= value <= 20 for value in points.values()))
            splits.append(tuple(points.values()))
        self.assertEqual(CharacterStats(mode="random", rng=random.Random(3)).points,
                         CharacterStats(mode="random", rng=random.Random(3)).points)
        self.assertGreater(len(set(splits)), 10)

    def test_values_changes_and_bonuses(self):
        stats = CharacterStats(points={"stealth": 20, "persuasion": 15, "watchfulness": 10, "intimidation": 5})
        self.assertEqual(stats.values(), {"stealth": 100, "persuasion": 75, "watchfulness": 50, "intimidation": 25})
        self.assertEqual(stats.change("stealth", 30), (100, 100))  # capped at 100
        self.assertEqual(stats.change("stealth", -10), (100, 90))
        self.assertEqual(stats.value("intimidation", bonus=20), 45)

    def test_settings_validation(self):
        self.assertEqual(validate_settings(SOCIAL), [])
        self.assertTrue(validate_settings({**SOCIAL, "allocation": {"total_points": 90, "min_points": 5, "max_points": 20}}))
        self.assertTrue(validate_settings({**SOCIAL, "tag_checks": {"combat": "attack"}}))
        self.assertTrue(validate_settings({**SOCIAL, "mode": "lucky"}))
        self.assertTrue(validate_settings({**SOCIAL, "damage_reduction_stat": "defence"}))


class CombatSystemTests(_SocialStory):
    def _combat(self, *rolls):
        stats = CharacterStats(points={"stealth": 12, "persuasion": 12, "watchfulness": 13, "intimidation": 13})
        return CombatSystem(stats, rng=_Dice(*rolls))

    def test_roll_passes_at_or_under_the_value(self):
        combat = self._combat(60, 61)
        self.assertTrue(combat.roll("stealth").passed)  # stealth 60
        self.assertFalse(combat.roll("stealth").passed)
        self.assertEqual([r.roll for r in combat.history], [60, 61])

    def test_difficulty_can_be_harder_easier_and_change_over_time(self):
        combat = self._combat()
        self.assertEqual(combat.target("stealth"), 60)
        self.assertEqual(combat.target("stealth", modifier=-10), 50)
        with patch.dict(config._config["skill_check_resolution"], {"difficulty_penalty": 20}):
            self.assertEqual(combat.target("stealth"), 40)
        with patch.dict(config._config["skill_check_resolution"],
                        {"difficulty_rate_per_step": 1.0, "difficulty_rate_start_step": 10}):
            self.assertEqual(combat.target("stealth", step=10), 60)
            self.assertEqual(combat.target("stealth", step=30), 40)  # harder over time
        with patch.dict(config._config["skill_check_resolution"],
                        {"difficulty_rate_per_step": -1.0, "difficulty_rate_start_step": 0}):
            self.assertEqual(combat.target("stealth", step=50), 95)  # easier, capped at max_chance
        with patch.dict(config._config["skill_check_resolution"], {"difficulty_penalty": 500}):
            self.assertEqual(combat.target("stealth"), 5)  # never below min_chance

    def test_checks_mitigation_and_failures(self):
        combat = self._combat()
        checked = Action("Sneak", [Outcome("x")], skill_check={"stat": "persuasion", "modifier": 5})
        self.assertEqual(combat.check_for(checked), ("persuasion", 5))
        self.assertEqual(combat.check_for(Action("Hide", []), Outcome("y", tags=["stealth"])), ("stealth", 0))
        self.assertIsNone(combat.check_for(Action("Wave", []), Outcome("z", tags=["social"])))
        self.assertEqual(combat.mitigate(-4), -2)  # intimidation 65 // 25 = 2 prevented
        self.assertEqual(combat.mitigate(-1), 0)
        self.assertEqual(combat.failure_penalty(dangerous=True), 1)
        with patch.dict(config._config["skill_check_resolution"], {"failure_mitigation_threshold": 60}):
            self.assertEqual(combat.failure_penalty(dangerous=True), 0)


def _world():
    world = WorldGraph()
    hall = Location("Hall", ["hall"])
    hall.actions = [
        Action("Talk your way in", [
            Outcome("The doors open.", on_check="pass", tags=["social"]),
            Outcome("They laugh you out.", on_check="fail", tags=["social"], health_change=-1),
        ], skill_check={"stat": "persuasion"}, exclusive_group="entry", source="location", owner="Hall"),
        Action("Slip past", [Outcome("You slip past.", tags=["stealth"])],
               exclusive_group="entry", source="location", owner="Hall"),
        Action("Train", [Outcome("You practise.", change_stats={"persuasion": 10})],
               repeat="always", source="location", owner="Hall"),
    ]
    world.add_location(hall)
    world.add_object(Object("Badge", ["hall"], stat_bonus={"persuasion": 20}))
    return world


class GameStateCheckTests(_SocialStory):
    def _state(self, *rolls, points=None):
        points = points or {"stealth": 12, "persuasion": 12, "watchfulness": 13, "intimidation": 13}
        state = GameState(_world(), _genome(), start="Hall", rng=random.Random(0), stats=points)
        state.combat.rng = _Dice(*rolls)
        return state

    def _choose(self, state, name):
        return state.step([a.name for a in state.ranked_available_actions()].index(name))

    def test_pass_and_fail_branches(self):
        state = self._state(10)
        self._choose(state, "Talk your way in")
        self.assertIn("RESULT: The doors open.", state.history)
        self.assertIn("[CHECK] persuasion: rolled 10, needed 60 or less - passed.", state.history)
        self.assertNotIn("Slip past", [a.name for a in state.ranked_available_actions()])  # choice made

        state = self._state(99)
        self._choose(state, "Talk your way in")
        self.assertIn("RESULT: They laugh you out.", state.history)  # the authored failure
        self.assertFalse(any(line.startswith("[FAILURE]") for line in state.history))
        self.assertIn("Slip past", [a.name for a in state.ranked_available_actions()])  # failed: still open

    def test_tag_checks_fail_into_the_generic_failure(self):
        state = self._state(99)
        self._choose(state, "Slip past")
        self.assertIn("[FAILURE] Slip past failed.", state.history)

    def test_items_and_outcomes_raise_stats(self):
        state = self._state()
        self.assertEqual(state.combat.target("persuasion", inventory=state.inventory), 60)
        state.inventory.add("Badge")
        self.assertEqual(state.combat.target("persuasion", inventory=state.inventory), 80)
        self._choose(state, "Train")
        self.assertEqual(state.stats.value("persuasion"), 70)
        self.assertIn("[STAT] persuasion: 60 -> 70", state.history)

    def test_the_ai_prefers_what_its_character_is_good_at(self):
        talker = self._state(points={"stealth": 5, "persuasion": 20, "watchfulness": 13, "intimidation": 12})
        sneak = self._state(points={"stealth": 20, "persuasion": 5, "watchfulness": 13, "intimidation": 12})
        rank = lambda state: [a.name for a in state.ranked_available_actions() if a.name != "Train"]
        self.assertEqual(rank(talker)[0], "Talk your way in")
        self.assertEqual(rank(sneak)[0], "Slip past")

    def test_undefined_stats_stop_the_world_build(self):
        npcs = [{**config.get("world_definition.characters")[0],
                 "actions": [{"name": "x", "skill_check": {"stat": "luck"}, "outcomes": [{"desc": "y"}]}]}]
        with patch.dict(config._config["world_definition"], {"characters": npcs}):
            with self.assertRaises(ValueError):
                engine.build_world(_genome())


class StatsEditingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        for name in ("stats.yaml", "items.yaml", "npcs.yaml"):
            shutil.copy(store.CONFIG_DIR / name, self.dir / name)
        for patcher in (
            patch.object(content, "STATS_PATH", self.dir / "stats.yaml"),
            patch.dict(content.ENTITY_FILES, {"Items": (self.dir / "items.yaml", "objects", "list"),
                                            "NPCs": (self.dir / "npcs.yaml", "characters", "list")}),
            patch.object(store.config, "reload"),
            patch.dict(os.environ, {"ANE_SAVE_DIR": str(self.dir / "saves")}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(api.api)

    def test_stats_api(self):
        self.assertIn("persuasion", self.client.get("/v1/stats").json()["character_stats"]["stats"])
        self.assertEqual(self.client.put("/v1/stats", json={"character_stats": {**SOCIAL, "mode": "lucky"}}).status_code, 400)
        self.assertEqual(self.client.put("/v1/stats", json={"character_stats": SOCIAL}).status_code, 200)
        self.assertIn("watchfulness", yaml.safe_load((self.dir / "stats.yaml").read_text())["world_definition"]["character_stats"]["stats"])

    def test_allocate(self):
        result = self.client.post("/v1/stats/allocate", json={"mode": "random"}).json()
        self.assertEqual((result["errors"], sum(result["points"].values())), ([], 50))
        bad = self.client.post("/v1/stats/allocate", json={"points": {"attack": 50}}).json()
        self.assertTrue(bad["errors"])

    def test_play_with_own_points_and_item_bonus(self):
        own = {"attack": 20, "defence": 10, "stealth": 10, "persuasion": 10}
        body = {"candidate_count": 1, "generations": 1, "simulation_steps": 2, "deterministic": False, "stats": own}
        game = self.client.post("/v1/simulations", json=body)
        self.assertEqual(game.status_code, 200, game.text)
        self.assertEqual(game.json()["stat_points"], own)
        self.assertEqual(game.json()["stats"]["attack"], 100)
        body["stats"] = {"attack": 30, "defence": 5, "stealth": 5, "persuasion": 10}
        self.assertEqual(self.client.post("/v1/simulations", json=body).status_code, 400)

        item = self.client.get("/v1/entities/Items/Steel Dagger").json()
        self.assertEqual(item["stat_bonus"], {"attack": 15})
        item["stat_bonus"] = {"luck": 3}
        self.assertEqual(self.client.put("/v1/entities/Items/Steel Dagger", json=item).status_code, 400)

    def test_actions_editor_knows_skill_checks(self):
        ok = [{"name": "Charm", "skill_check": {"stat": "persuasion", "modifier": -5},
               "outcomes": [{"desc": "Yes.", "on_check": "pass", "change_stats": {"persuasion": 5}},
                            {"desc": "No.", "on_check": "fail"}]}]
        self.assertEqual(self.client.put("/v1/entities/NPCs/Priest/actions", json={"actions": ok}).status_code, 200)
        for bad in ({"skill_check": {"stat": "luck"}}, {"outcomes": [{"desc": "x", "on_check": "maybe"}]}):
            action = {"name": "x", "outcomes": [{"desc": "y"}], **bad}
            with self.subTest(bad=bad):
                self.assertEqual(self.client.put("/v1/entities/NPCs/Priest/actions", json={"actions": [action]}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
