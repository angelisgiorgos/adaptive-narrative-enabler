"""The evolutionary AI behind story creation, and editing engine logic through parameters."""
import json
import os
import random
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import main as engine
from backend import sessions, store
from core_engine import Genome
from core_engine.evolution import LearningMemory, NarrativeEvolution

SMALL = {"pop_size": 12, "generations": 8, "episodes_per_genome": 1, "max_steps": 1, "validation_episodes": 1}


def goal_seeking_fitness(state):
    """A fitness with a known optimum: the higher the genome's goal_bias, the better."""
    return state.genome.goal_bias


def flat_fitness(state):
    return 0.0


class _TempSaves(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        patcher = patch.dict(os.environ, {"ANE_SAVE_DIR": str(self.dir)})
        patcher.start()
        self.addCleanup(patcher.stop)


class EvolutionTests(_TempSaves):
    def test_evolution_learns_what_the_fitness_rewards(self):
        start = max(Genome(random.Random(i)).goal_bias for i in range(SMALL["pop_size"]))
        result = NarrativeEvolution(engine.build_world, goal_seeking_fitness, settings=SMALL, seed=3).run()
        self.assertGreater(result.best_genome.goal_bias, start)
        self.assertGreater(result.history[-1].mean, result.history[0].mean)  # the whole population improved
        self.assertEqual(result.generations_run, SMALL["generations"])
        tendency = next(t for t in result.learned_traits() if t["trait"] == "goal_bias")["tendency"]
        self.assertEqual(tendency, "high")

    def test_seeded_runs_are_reproducible(self):
        first = NarrativeEvolution(engine.build_world, goal_seeking_fitness, settings=SMALL, seed=5).run()
        second = NarrativeEvolution(engine.build_world, goal_seeking_fitness, settings=SMALL, seed=5).run()
        self.assertEqual(first.best_genome.traits(), second.best_genome.traits())
        self.assertEqual([h.validation for h in first.history], [h.validation for h in second.history])

    def test_adaptive_mutation_and_early_stopping(self):
        settings = {**SMALL, "stagnation_generations": 2, "early_stop_generations": 4, "mutation_step": 0.1}
        result = NarrativeEvolution(engine.build_world, flat_fitness, settings=settings, seed=1).run()
        steps = [h.mutation_step for h in result.history]
        self.assertEqual(steps[0], 0.1)
        self.assertGreater(max(steps), 0.1)  # widened while learning stalled
        self.assertTrue(result.early_stopped)
        self.assertEqual(result.generations_run, 5)

    def test_learning_memory_carries_over_between_runs(self):
        memory = LearningMemory()
        first = NarrativeEvolution(engine.build_world, goal_seeking_fitness, settings=SMALL, seed=7, memory=memory).run()
        self.assertEqual(first.warm_started, 0)
        remembered = memory.load()
        self.assertTrue(remembered)
        self.assertLessEqual(len(remembered), 12)
        self.assertEqual(max(e["score"] for e in remembered), first.best_score)
        second = NarrativeEvolution(engine.build_world, goal_seeking_fitness, settings=SMALL, seed=8, memory=memory).run()
        self.assertGreater(second.warm_started, 0)
        self.assertGreaterEqual(second.best_score, first.best_score - 1e-9)  # it starts where it left off
        memory.forget()
        self.assertEqual(memory.load(), [])

    def test_main_uses_the_shared_engine(self):
        evolution = {**engine.config.get("evolution"), **SMALL, "generations": 2, "warm_start": False}
        with patch.dict(engine.config._config, {"evolution": evolution}), patch.object(engine, "EVALUATION_THREADS", 1):
            score, genome, episode_score, state = engine.evolve()
        self.assertIsInstance(genome, Genome)
        self.assertGreater(state.actions_taken, 0)


class EngineLogicTests(_TempSaves):
    def setUp(self):
        super().setUp()
        shutil.copy(store.CONFIG_DIR / "hyperparameters.yaml", self.dir / "hyperparameters.yaml")
        for patcher in (patch.object(store, "HYPERPARAMETERS_PATH", self.dir / "hyperparameters.yaml"),
                        patch.object(store.config, "reload")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(api.api)

    def test_sections_are_listed_and_described(self):
        sections = {s["section"]: s["description"] for s in self.client.get("/v1/logic").json()["sections"]}
        self.assertIn("evolution", sections)
        self.assertIn("skill_check_resolution", sections)
        self.assertTrue(sections["fitness"])

    def test_parameters_change_the_logic_and_keep_comments(self):
        text = self.client.get("/v1/logic/evolution").json()["parameters"]
        self.assertIn("# parents are the best of this many random genomes", text)
        changed = text.replace("tournament_size: 3", "tournament_size: 5")
        self.assertEqual(self.client.put("/v1/logic/evolution", json={"parameters": changed}).status_code, 200)
        saved = (self.dir / "hyperparameters.yaml").read_text()
        self.assertIn("tournament_size: 5          # parents are the best of this many random genomes", saved)
        self.assertIn("skill_check_resolution:", saved)  # other sections untouched

    def test_invalid_parameters_are_rejected(self):
        before = (self.dir / "hyperparameters.yaml").read_text()
        text = self.client.get("/v1/logic/evolution").json()["parameters"]
        for bad in (text.replace("tournament_size: 3", "tournament_size: many"),
                    text.replace("tournament_size: 3", "tournament_sise: 3"),
                    "fitness:\n  base: {}\n"):
            with self.subTest(bad=bad[:40]):
                self.assertEqual(self.client.put("/v1/logic/evolution", json={"parameters": bad}).status_code, 400)
        self.assertEqual(self.client.get("/v1/logic/no_such_section").status_code, 404)
        self.assertEqual((self.dir / "hyperparameters.yaml").read_text(), before)


class NoPythonInTheUITests(unittest.TestCase):
    def test_python_source_editing_is_gone(self):
        client = TestClient(api.api)
        self.assertEqual(client.get("/v1/actions").status_code, 404)
        self.assertIn(client.put("/v1/actions", json={"source": "x"}).status_code, (404, 405))
        import app
        languages = {c.get("props", {}).get("language") for c in app.demo.config["components"]}
        self.assertNotIn("python", languages)

    def test_the_ui_file_holds_no_backend_logic(self):
        source = Path(__file__).resolve().parent.parent.joinpath("app.py").read_text()
        for backend_only in ("import main", "write_text(", "yaml.safe_dump(", "GameState(", "NarrativeEvolution("):
            self.assertNotIn(backend_only, source)


class LearningInPlayTests(_TempSaves):
    def test_games_come_from_evolution_and_report_learning(self):
        client = TestClient(api.api)
        body = {"candidate_count": 4, "generations": 3, "simulation_steps": 3, "episodes": 1, "deterministic": False}
        game = client.post("/v1/simulations", json=body)
        self.assertEqual(game.status_code, 200, game.text)
        learning = game.json()["learning"]
        self.assertEqual(len(learning["history"]), 3)
        self.assertTrue({"best", "mean", "validation", "diversity", "mutation_step"} <= set(learning["history"][0]))
        self.assertEqual({t["trait"] for t in learning["learned_traits"]}, set(Genome().traits()))
        self.assertGreater(client.get("/v1/learning").json()["size"], 0)
        second = client.post("/v1/simulations", json=body).json()["learning"]
        self.assertGreater(second["warm_started_from_memory"], 0)  # the next run continues learning
        self.assertEqual(client.delete("/v1/learning").json()["size"], 0)

    def test_studio_shows_the_learning_curve(self):
        import app
        outputs = app.run_simulation_ui(4, 2, 3, False, "default", "story default", "", 1, False,
                                        progress=lambda *a, **k: None)
        curve, traits = outputs[7], outputs[8]
        self.assertEqual(set(curve["series"]), {"benchmark (champion)", "best this generation", "population average"})
        self.assertIn("goal_bias", set(traits["trait"]))


if __name__ == "__main__":
    unittest.main()
