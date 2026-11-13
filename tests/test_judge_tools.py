"""Tools around the LLM judge: pairwise judging (evaluator/game_judge.py, judge_benchmark.py),
fitting the fitness to the judge (evaluator/calibrate_fitness.py) and the judge in the loop
of the evolution (core_engine/evolution.py). The model is a fake here."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import json
import os
import random
import tempfile
import unittest
from dataclasses import asdict
from unittest.mock import patch

import yaml

import main as engine
from core_engine import Genome
from core_engine.evolution import NarrativeEvolution
from evaluator import calibrate_fitness
from evaluator.game_judge import CRITERIA, GameJudge, combine_orders, parse_pairwise
from evaluator.judge_benchmark import _apply_variants, _wilson, pairwise, pairwise_summary, play_stories
from utils.config_loader import config


class FakeRuntime:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def generate(self, prompt, max_new_tokens=512, temperature=0.3, **kwargs):
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else ""


def _duel(winner, **criteria):
    return json.dumps({"criteria": criteria, "winner": winner, "reason": f"{winner} reads better."})


class PairwiseParseTests(unittest.TestCase):
    def test_fences_trailing_commas_and_lines(self):
        text = 'Verdict:\n```json\n{"criteria": {"agency": "A", "pacing": "b", "surprise": "Tie",}, "winner": "B",}\n```'
        self.assertEqual(parse_pairwise(text), {"winner": "B", "criteria": {"agency": "A", "pacing": "B", "surprise": "tie"},
                                                "reason": ""})
        lines = parse_pairwise("agency: A\npacing = B\nwinner: A")
        self.assertEqual((lines["winner"], lines["criteria"]), ("A", {"agency": "A", "pacing": "B"}))
        self.assertIsNone(parse_pairwise("Both are fine stories."))
        self.assertIsNone(parse_pairwise(""))

    def test_both_orders_must_agree(self):
        first = {"winner": "B", "criteria": {"agency": "A", "pacing": "B"}, "reason": "x"}
        # The second judgement saw B first, so its "A" means the story B.
        agreeing = {"winner": "A", "criteria": {"agency": "B", "pacing": "B"}, "reason": "y"}
        combined = combine_orders(first, agreeing)
        self.assertEqual((combined["winner"], combined["agreed"]), ("B", True))
        self.assertEqual(combined["criteria"], {"agency": "A", "pacing": "tie"})
        position_bias = combine_orders(first, {"winner": "B", "criteria": {}, "reason": ""})  # "the first one" twice
        self.assertEqual((position_bias["winner"], position_bias["agreed"]), ("tie", False))
        self.assertEqual(combine_orders(first, None)["winner"], "tie")  # one order alone decides nothing
        self.assertIsNone(combine_orders(None, None))

    def test_the_judge_reads_both_orders(self):
        runtime = FakeRuntime([_duel("A", agency="A"), _duel("B", agency="B")])
        result = GameJudge(runtime=runtime, samples=1).compare_digests("STORY ONE", "STORY TWO")
        self.assertEqual((result["winner"], result["criteria"], result["agreed"]), ("A", {"agency": "A"}, True))
        self.assertLess(runtime.prompts[0].index("STORY ONE"), runtime.prompts[0].index("STORY TWO"))
        self.assertLess(runtime.prompts[1].index("STORY TWO"), runtime.prompts[1].index("STORY ONE"))
        self.assertEqual([order["swapped"] for order in result["orders"]], [False, True])
        unreadable = GameJudge(runtime=FakeRuntime(["?", "?"]), samples=1).compare_digests("a", "b")
        self.assertIsNone(unreadable["winner"])


class PairwiseBenchmarkTests(unittest.TestCase):
    def test_win_rate_with_its_interval(self):
        rows = [{"winner": w, "criteria": c} for w, c in
                (("B", {"agency": "B"}), ("B", {"agency": "B"}), ("A", {"agency": "A"}), ("tie", {}))]
        result = pairwise_summary(rows)
        self.assertEqual((result["pairs"], result["a_wins"], result["b_wins"], result["ties"]), (4, 1, 2, 1))
        self.assertAlmostEqual(result["b_win_rate"], 2 / 3)
        low, high = result["wilson_95"]
        self.assertTrue(0.0 <= low < 2 / 3 < high <= 1.0)
        self.assertAlmostEqual(result["criteria"]["agency"], 2 / 3)
        self.assertIsNone(result["criteria"]["pacing"])
        self.assertEqual(_wilson(0, 0), [0.0, 1.0])
        narrow, wide = _wilson(80, 100), _wilson(8, 10)
        self.assertLess(narrow[1] - narrow[0], wide[1] - wide[0])  # more pairs, a surer win rate

    def test_stories_compared_pair_by_pair(self):
        states = play_stories(2, first_seed=3)
        judge = GameJudge(runtime=FakeRuntime([_duel("B"), _duel("A"), _duel("A"), _duel("A")]), samples=1)
        rows, result = pairwise(states[:1] + states[1:], states[1:] + states[:1], judge=judge, workers=1)
        self.assertEqual([row["winner"] for row in rows], ["B", "tie"])
        self.assertEqual((result["b_wins"], result["ties"]), (1, 1))

    def test_a_variant_is_undone(self):
        before = config.get("evolution.judge_weight")
        undo = _apply_variants(["evolution.judge_weight=0.5", "director.new_key=[1, 2]"])
        try:
            self.assertEqual(config.get("evolution.judge_weight"), 0.5)
            self.assertEqual(config.get("director.new_key"), [1, 2])
        finally:
            undo()
        self.assertEqual(config.get("evolution.judge_weight"), before)
        self.assertIsNone(config.get("director.new_key"))


class CalibrationTests(unittest.TestCase):
    @staticmethod
    def _samples(n=60, seed=1):
        rng = random.Random(seed)
        samples = []
        for _ in range(n):
            terms = {"rewarded": rng.random(), "punished": rng.random(), "noise": rng.random(), "constant": 1.0}
            engagement = 5 + 3 * terms["rewarded"] - 2 * terms["punished"] + rng.gauss(0, 0.2)
            samples.append((terms, engagement))
        return samples

    def test_terms_that_run_with_the_judge_grow(self):
        result = calibrate_fitness.fit(self._samples(), alpha=1.0)
        terms = result["terms"]
        self.assertNotIn("constant", terms)  # never varies: says nothing
        self.assertGreater(terms["rewarded"]["coef"], 0)
        self.assertLess(terms["punished"]["coef"], 0)
        self.assertEqual(terms["rewarded"]["multiplier"], 2.0)  # the strongest term, gain 1
        self.assertLess(terms["punished"]["multiplier"], 1.0)
        self.assertLess(abs(terms["noise"]["multiplier"] - 1.0), abs(terms["punished"]["multiplier"] - 1.0))
        for term in terms.values():
            self.assertTrue(calibrate_fitness.LOW <= term["multiplier"] <= calibrate_fitness.HIGH)
        self.assertGreater(result["spearman"], 0.8)
        self.assertGreater(result["cv_spearman"], 0.8)  # it predicts stories it was not fitted on
        strong = calibrate_fitness.fit(self._samples(), gain=10.0)  # alpha chosen by cross-validation
        self.assertIn(strong["alpha"], calibrate_fitness.ALPHAS)
        self.assertEqual((strong["terms"]["rewarded"]["multiplier"], strong["terms"]["punished"]["multiplier"]),
                         (3.0, 0.25))  # clipped
        with self.assertRaises(ValueError):
            calibrate_fitness.fit(self._samples(n=2))

    def test_stories_and_judgements_from_separate_files(self):
        folder = tempfile.mkdtemp(prefix="ane-calibrate-")
        stories, judged, out = (os.path.join(folder, name) for name in ("stories.jsonl", "judged.jsonl", "w.yaml"))
        samples = self._samples(n=12)
        with open(stories, "w") as stream:
            for seed, (terms, _) in enumerate(samples):
                stream.write(json.dumps({"world": "w", "kind": "engine", "seed": seed, "breakdown": terms}) + "\n")
        with open(judged, "w") as stream:
            for seed, (_, engagement) in enumerate(samples):
                for shift in (-0.5, 0.5):  # two judgements of one story are averaged
                    stream.write(json.dumps({"world": "w", "kind": "engine", "seed": seed,
                                             "judgement": {"engagement": engagement + shift}}) + "\n")
                stream.write(json.dumps({"world": "w", "kind": "engine", "seed": seed, "group": "ablation",
                                         "judgement": {"engagement": 1.0}}) + "\n")
        loaded = calibrate_fitness.load_separate([stories], [judged])
        self.assertEqual(len(loaded), 12)
        self.assertAlmostEqual(sorted(e for _, e in loaded)[0], sorted(e for _, e in samples)[0])
        with patch("builtins.print"):
            calibrate_fitness.main(["--stories", stories, "--judged", judged, "--write", out])
        with open(out) as stream:
            weights = yaml.safe_load(stream)["fitness"]["term_weights"]
        self.assertEqual(set(weights), {"rewarded", "punished", "noise"})
        self.assertGreater(weights["rewarded"], 1.0)


class FakeJudge:
    """Engagement by call order (judge_workers 1 keeps that order)."""

    def __init__(self, engagements):
        self.engagements, self.digests = list(engagements), []

    def judge_digest(self, digest):
        self.digests.append(digest)
        value = self.engagements.pop(0) if self.engagements else 5.0
        return {"engagement": value}


class NeverCalled:
    def judge_digest(self, digest):
        raise AssertionError("judge_weight 0 must not call the judge")


QUICK = {"pop_size": 6, "generations": 3, "episodes_per_genome": 1, "max_steps": 2, "validation_episodes": 1,
         "full_length_stories": False, "early_stop_generations": 0, "judge_workers": 1}


def goal_seeking_fitness(state):
    return state.genome.goal_bias


class JudgeInTheLoopTests(unittest.TestCase):
    def _evolution(self, judge=None, **settings):
        return NarrativeEvolution(engine.build_world, goal_seeking_fitness, settings={**QUICK, **settings}, seed=4,
                                  judge=judge)

    def test_the_judge_moves_the_best_few(self):
        evolution = self._evolution(FakeJudge([1.0, 9.0]), judge_weight=0.1, judge_top_k=2)
        genomes = [Genome(random.Random(n)) for n in range(3)]
        scored = [(1.0, genomes[0]), (0.9, genomes[1]), (0.8, genomes[2])]
        rescored, engagement = evolution._judged(scored, seed=11, situation=None, max_steps=2)
        # 1.0 - 0.4 and 0.9 + 0.4: the judge's favourite leads, the third is untouched.
        self.assertEqual([genome for _, genome in rescored], [genomes[1], genomes[2], genomes[0]])
        self.assertEqual([round(score, 6) for score, _ in rescored], [1.3, 0.8, 0.6])
        self.assertEqual(engagement, 5.0)
        self.assertEqual(len(evolution.judge.digests), 2)
        self.assertTrue(all("1." in digest for digest in evolution.judge.digests))  # a played story

    def test_a_run_with_the_judge_reports_its_engagement(self):
        judge = FakeJudge([8.0] * 6)
        result = self._evolution(judge, judge_weight=0.5, judge_top_k=2).run()
        self.assertEqual(len(judge.digests), 2 * QUICK["generations"])
        self.assertEqual([stats.judge_engagement for stats in result.history], [8.0] * QUICK["generations"])

    def test_weight_zero_is_the_plain_evolution(self):
        plain = self._evolution().run()
        off = self._evolution(NeverCalled(), judge_weight=0.0).run()
        self.assertEqual([asdict(s) for s in plain.history], [asdict(s) for s in off.history])
        self.assertTrue(all(stats.judge_engagement is None for stats in plain.history))

    def test_without_a_model_the_evolution_goes_on(self):
        plain = self._evolution().run()

        def missing(*args, **kwargs):
            raise RuntimeError("vLLM is required for LLM features.")

        with patch("utils.llm_model_loader.get_llm_runtime", missing), \
                self.assertLogs("core_engine.evolution", level="WARNING") as logs:
            without = self._evolution(judge_weight=1.0).run()
        self.assertEqual(len(logs.records), 1)  # said once, not every generation
        self.assertEqual([asdict(s) for s in plain.history], [asdict(s) for s in without.history])

        class GoneMidRun:
            def judge_digest(self, digest):
                raise ConnectionError("the vLLM server stopped")

        with self.assertLogs("core_engine.evolution", level="WARNING") as logs:
            gone = self._evolution(GoneMidRun(), judge_weight=1.0).run()
        self.assertEqual(len(logs.records), 1)
        self.assertEqual([asdict(s) for s in plain.history], [asdict(s) for s in gone.history])

    def test_unreadable_judgements_leave_the_scores(self):
        evolution = self._evolution(FakeJudge([None, None]), judge_weight=1.0)
        scored = [(1.0, Genome(random.Random(1))), (0.5, Genome(random.Random(2)))]
        with self.assertLogs("core_engine.evolution", level="WARNING"):
            rescored, engagement = evolution._judged(scored, seed=3, situation=None, max_steps=2)
        self.assertEqual(rescored, scored)
        self.assertIsNone(engagement)


if __name__ == "__main__":
    unittest.main()
