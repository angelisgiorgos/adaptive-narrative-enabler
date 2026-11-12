"""The LLM judge (evaluator/game_judge.py): reading the model's judgement, the digest of a
playthrough it reads, averaging samples, and the API endpoints. The model is a fake here;
scripts that run the real Gemma 4 are described in the README."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import json
import os
import random
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
from backend import sessions
from core_engine import Genome
from evaluator.game_judge import CRITERIA, GameJudge, build_prompt, digest_turns, parse_judgement


def _reply(engagement=7, **overrides):
    scores = {name: 6 for name in CRITERIA}
    scores.update(overrides)
    return json.dumps({"scores": scores, "engagement": engagement, "best_moment": "turn 3: the ambush",
                       "weakest_moment": "turn 8: a repeat", "verdict": "Tense and varied."})


class FakeRuntime:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def generate(self, prompt, max_new_tokens=512, temperature=0.3, **kwargs):
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else ""


TURNS = [
    {"action": "Explore harbor", "location_before": "Harbor", "location_after": "Harbor", "outcome": "A sailor talks.",
     "health": 10, "coins": 10, "succeeded": True, "event": None, "event_after": None, "check": None},
    {"action": "Bribe with coins", "location_before": "Harbor", "location_after": "Market",
     "outcome": "He pockets the coins.", "health": 10, "coins": 0, "succeeded": True, "event": None,
     "event_after": None, "check": {"stat": "persuasion", "passed": True}},
    {"action": "Walk on", "location_before": "Market", "location_after": "Market", "outcome": "A bandit attacks!",
     "health": 7, "coins": 0, "succeeded": True, "event": None, "event_after": "Ambush", "check": None},
    {"action": "Make a run for it", "location_before": "Market", "location_after": "Harbor", "outcome": "You run.",
     "health": 7, "coins": 0, "succeeded": True, "event": "Ambush", "event_after": None,
     "check_json": json.dumps({"stat": "stealth", "passed": False})},
]


class ParseTests(unittest.TestCase):
    def test_a_clean_judgement(self):
        result = parse_judgement(_reply(engagement=8, surprise=9))
        self.assertEqual(result["engagement"], 8.0)
        self.assertEqual(result["scores"]["surprise"], 9.0)
        self.assertEqual(set(result["scores"]), set(CRITERIA))
        self.assertEqual(result["verdict"], "Tense and varied.")

    def test_fences_prose_trailing_commas_and_bounds(self):
        text = "Here is my review:\n```json\n" + _reply(engagement=14, pacing=0)[:-1] + ",}\n```\nThanks!"
        result = parse_judgement(text)
        self.assertEqual(result["engagement"], 10.0)   # clamped to 1-10
        self.assertEqual(result["scores"]["pacing"], 1.0)

    def test_scores_written_as_lines(self):
        text = "agency: 7\npacing: 5\nsurprise = 8\nengagement: 6"
        result = parse_judgement(text)
        self.assertEqual(result["scores"], {"agency": 7.0, "pacing": 5.0, "surprise": 8.0})
        self.assertEqual(result["engagement"], 6.0)

    def test_nothing_to_read(self):
        self.assertIsNone(parse_judgement("I cannot judge this."))
        self.assertIsNone(parse_judgement(""))


class DigestTests(unittest.TestCase):
    def test_every_turn_with_what_changed(self):
        digest = digest_turns(TURNS, {"mission": "Break into the Vault", "end_kind": "mission", "won": True})
        self.assertIn("Mission: Break into the Vault", digest)
        self.assertIn("2. [Harbor] Bribe with coins -> He pockets the coins. (persuasion check passed; moves to Market; "
                      "coins 10->0)", digest)
        self.assertIn("event begins: Ambush", digest)
        self.assertIn("stealth check failed", digest)  # a recorded turn's check_json
        self.assertIn("Ending: mission (won).", digest)
        prompt = build_prompt(digest)
        for name in CRITERIA:
            self.assertIn(f'"{name}": <1-10>', prompt)

    def test_the_beats_and_the_alternatives_a_player_saw(self):
        turn = dict(TURNS[0], options=["Bribe with coins", "Explore harbor", "Leave"],
                    text="> Explore harbor\nRESULT: A sailor talks.\n[QUEST] Find the ship: done.\n[LOCATION] Still at Harbor\n"
                         "[TWIST] The stranger was right all along.")
        digest = digest_turns([turn])
        self.assertIn("| [QUEST] Find the ship: done. | [TWIST] The stranger was right all along.", digest)
        self.assertIn("[could also: Bribe with coins; Leave]", digest)
        self.assertNotIn("[LOCATION]", digest)

    def test_long_stories_keep_their_beginning_and_end(self):
        turns = [dict(TURNS[0], action=f"Step {n}") for n in range(1, 101)]
        digest = digest_turns(turns, max_turns=30)
        self.assertIn("1. [Harbor] Step 1 ", digest)
        self.assertIn("100. [Harbor] Step 100 ", digest)
        self.assertIn("(70 turns not shown)", digest)
        self.assertNotIn("Step 50 ", digest)


class JudgeTests(unittest.TestCase):
    def test_samples_are_averaged_with_their_spread(self):
        runtime = FakeRuntime([_reply(engagement=6, agency=5), _reply(engagement=8, agency=7)])
        result = GameJudge(runtime=runtime, samples=2).judge_turns(TURNS)
        self.assertEqual(result["engagement"], 7.0)
        self.assertEqual(result["scores"]["agency"], 6.0)
        self.assertEqual(result["spread"], 1.0)
        self.assertEqual((result["samples"], result["parsed"]), (2, 2))
        self.assertIn("PLAYTHROUGH:", runtime.prompts[0])

    def test_an_unreadable_judgement_says_so(self):
        result = GameJudge(runtime=FakeRuntime(["no json here"]), samples=1).judge_turns(TURNS)
        self.assertIsNone(result["engagement"])
        self.assertEqual(result["parsed"], 0)


class BenchmarkTests(unittest.TestCase):
    def test_stories_are_played_judged_twice_and_summarised(self):
        from evaluator.judge_benchmark import benchmark, play_stories, spearman, summary

        self.assertAlmostEqual(spearman([1, 2, 3], [2, 4, 9]), 1.0)
        self.assertAlmostEqual(spearman([1, 2, 3], [3, 2, 1]), -1.0)
        stories = play_stories(2, first_seed=3)
        self.assertTrue(all(state.ended or state.actions_taken for state in stories))
        judge = GameJudge(runtime=FakeRuntime([_reply(e) for e in (6, 7, 8, 8)]), samples=1)
        rows = benchmark(stories, judge=judge, workers=1, repeats=2)
        self.assertEqual([r["judgements"] for r in rows], [[6.0, 7.0], [8.0, 8.0]])
        result = summary(rows)
        self.assertEqual(result["engagement"], 7.25)
        self.assertEqual(result["retest_difference"], 0.5)
        self.assertEqual(set(result["criteria"]), set(CRITERIA))


class JudgeEndpointTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"ANE_SAVE_DIR": tempfile.mkdtemp(prefix="ane-saves-tests-")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(api.api)
        self.token, state = sessions._start_game(Genome(rng=random.Random(4)), "judge-test", None, None)
        self.addCleanup(sessions.SESSIONS.pop, self.token, None)
        for _ in range(4):
            sessions.play_action(self.token, sessions._player_options(state)[0].name)

    def test_a_session_and_its_recorded_story_are_judged(self):
        with patch("utils.llm_model_loader.get_llm_runtime", lambda *a, **k: FakeRuntime([_reply(7)] * 4)):
            played = self.client.post(f"/v1/sessions/{self.token}/judge")
            self.assertEqual(played.status_code, 200, played.text)
            self.assertEqual(played.json()["engagement"], 7.0)
            self.assertNotIn("raw", played.json())
            story_id = sessions.SESSION_RECORDS[self.token]
            recorded = self.client.post(f"/v1/records/stories/{story_id}/judge")
            self.assertEqual(recorded.status_code, 200, recorded.text)
            self.assertEqual(set(recorded.json()["scores"]), set(CRITERIA))
        self.assertEqual(self.client.post("/v1/sessions/nope/judge").status_code, 404)

    def test_without_a_model_the_judge_is_unavailable(self):
        def missing(*args, **kwargs):
            raise RuntimeError("vLLM is required for LLM features.")

        class Broken:
            generate = staticmethod(missing)

        with patch("utils.llm_model_loader.get_llm_runtime", lambda *a, **k: Broken()):
            response = self.client.post(f"/v1/sessions/{self.token}/judge")
        self.assertEqual(response.status_code, 503)
        self.assertIn("not available", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
