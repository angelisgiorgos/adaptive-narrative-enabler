"""The evolutionary AI behind story creation, and editing engine logic through parameters."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
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
        # app.py formats YAML for its code editors (yaml.safe_dump) but uses no backend code:
        # tests/test_studio_api.py checks that it imports none of it.
        source = Path(__file__).resolve().parent.parent.joinpath("app.py").read_text()
        for backend_only in ("import main", "write_text(", "GameState(", "NarrativeEvolution("):
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


def _turn(action, outcome, location="Gate", health=8, coins=0, event_after=None, check=None, succeeded=True, source="location"):
    return {"action": action, "outcome": outcome, "location_before": location, "location_after": location,
            "event": None, "event_after": event_after, "health": health, "coins": coins, "check": check,
            "succeeded": succeeded, "source": source, "owner": location}


class EngagementScoreTests(unittest.TestCase):
    """The score counts what a player feels turn by turn (core_engine/fitness.py)."""

    CFG = {"filler_penalty": 60, "loop_window": 6, "loop_penalty": 4, "novelty_fade_penalty": 50, "escalation_bonus": 40}

    def _terms(self, log):
        from types import SimpleNamespace

        from core_engine.fitness import _engagement

        return _engagement(SimpleNamespace(turn_log=log), self.CFG)

    def test_loops_and_filler_cost(self):
        looping = [_turn("Pray", "Nothing answers."), _turn("Wait", "You wait.")] * 6
        varied = [_turn(f"Act {i}", f"Something new {i}.") for i in range(12)]
        bad, good = self._terms(looping), self._terms(varied)
        self.assertEqual(good["loops"], 0)
        self.assertEqual(bad["loops"], -10 * 4)                    # every turn after the first two repeats
        self.assertEqual(good["filler"], 0)
        self.assertAlmostEqual(bad["filler"], -60 * 10 / 12)          # 10 of 12 turns brought nothing new
        self.assertLess(bad["novelty_fade"], 0)                    # new content dried up

    def test_rising_tension_is_rewarded(self):
        calm = [_turn(f"Act {i}", f"Calm {i}.") for i in range(6)]
        rising = calm[:3] + [_turn(f"Fight {i}", f"Hit {i}.", health=8 - i - 1, event_after="Ambush") for i in range(3)]
        self.assertEqual(self._terms(calm)["escalation"], 0)
        self.assertGreater(self._terms(rising)["escalation"], 0)

    def test_an_ending_beats_the_action_limit(self):
        from core_engine.fitness import _fitness_breakdown

        state = engine.GameState(engine.build_world(Genome(random.Random(3))), Genome(random.Random(3)), rng=random.Random(3))
        state.step()
        state.ended, state.end_kind = True, "action_limit"
        cut = _fitness_breakdown(state)["ending"]
        state.end_kind = "epilogue"
        self.assertEqual(_fitness_breakdown(state)["ending"] - cut, engine.config.get("fitness.base.action_limit_penalty"))

    def test_growing_terms_count_per_turn(self):
        from core_engine.fitness import _fitness_breakdown

        genome = Genome(random.Random(5))
        state = engine.GameState(engine.build_world(genome), genome, rng=random.Random(5))
        for _ in range(60):
            if state.ended or not state.step():
                break
        normalized = _fitness_breakdown(state)
        with patch.dict(engine.config._config["fitness"]["length_normalization"], {"enabled": False}):
            raw = _fitness_breakdown(state)
        scale = 35 / max(state.actions_taken, 20)
        for term in ("discovery", "character_interactions", "tone_variety"):
            self.assertAlmostEqual(normalized[term], raw[term] * scale)


class ReliableLearningTests(unittest.TestCase):
    """Champions are chosen on the benchmark; the population stays diverse."""

    def _evolution(self, **settings):
        base = {"pop_size": 6, "generations": 1, "episodes_per_genome": 1, "validation_episodes": 2, "max_steps": 4,
                "full_length_stories": False, "warm_start": False, "cover_situations": False}

        def scores(function, tasks):  # the training stories favour threat, the benchmark the opposite
            return [(-1 if task[3][0] >= 10 ** 6 else 1) * task[2]["threat_prob"] for task in tasks]

        return NarrativeEvolution(engine.build_world, engine.fitness, settings={**base, **settings}, seed=1, map_fn=scores)

    def test_the_champion_is_the_best_on_the_benchmark_among_the_top_k(self):
        for top_k in (1, 3):
            with self.subTest(top_k=top_k):
                evolution = self._evolution(benchmark_top_k=top_k)
                result = evolution.run()
                ranked = sorted(evolution.last_checkpoint["scored"], key=lambda pair: pair[0], reverse=True)
                top = [traits["threat_prob"] for _, traits in ranked[:top_k]]
                self.assertEqual(result.best_genome.traits()["threat_prob"], min(top))
                self.assertEqual(result.history[0].best_traits, result.best_genome.traits())

    def test_the_benchmark_covers_every_situation(self):
        evolution = self._evolution(cover_situations=True)
        self.assertEqual(len(evolution._validation_seeds()), max(2, len(evolution._situations())))

    def test_a_collapsing_population_gets_newcomers(self):
        evolution = self._evolution(min_diversity=1.0, max_immigrant_rate=0.5)
        clone = Genome(random.Random(2))
        scored = [(1.0, Genome.from_traits(clone.traits())) for _ in range(6)]
        population = evolution._next_population(scored, 0.14, diversity=0.0)
        keys = {evolution._genome_key(genome) for genome in population}
        self.assertEqual(len(keys), len(population))                # no two storytellers alike


if __name__ == "__main__":
    unittest.main()
