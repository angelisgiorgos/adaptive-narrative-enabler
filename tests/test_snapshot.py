"""Exact snapshots of a story (core_engine/snapshot.py): JSON only, and a restored story goes on
exactly as the original would (the same scenes, text, dice and state), in this process and in a
fresh one with another hash seed; edge states (a climax under way, a bargain pending, the player
locked up, the story ended); every attribute is either saved or deliberately rebuilt; a snapshot
made with other code, another world or another storyteller is refused."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main as engine
from core_engine import GameState, Genome, snapshot, version
from utils.config_loader import config

ROOT = Path(__file__).resolve().parent.parent


def _play(state, policy, turns):
    for _ in range(turns):
        if state.ended:
            return
        options = state.ranked_available_actions()
        if not options:
            return
        state.step(policy.randrange(len(options)))


def _story(seed, turns):
    genome = Genome(rng=random.Random(seed))
    state = GameState(engine.build_world(genome), genome, rng=random.Random(seed))
    _play(state, random.Random(1000 + seed), turns)
    state.ranked_available_actions()  # the scene as a player sees it
    return state, genome


class _Snapshots(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"ANE_SAVE_DIR": tempfile.mkdtemp(prefix="ane-snapshot-")})
        patcher.start()
        self.addCleanup(patcher.stop)
        config.reload()
        engine.refresh_runtime_settings()

    def assertContinuesIdentically(self, state, genome, turns=25):
        data = json.loads(json.dumps(snapshot.dump(state)))  # through JSON, as a UNP keeps it
        restored = snapshot.load(data, genome=genome)
        self.assertEqual(snapshot.canonical(snapshot.dump(restored)), snapshot.canonical(data))
        _play(state, random.Random(7), turns)
        _play(restored, random.Random(7), turns)
        self.assertEqual(restored.history, state.history)
        self.assertEqual(restored.rng.getstate(), state.rng.getstate())
        self.assertEqual(snapshot.canonical(snapshot.dump(restored)), snapshot.canonical(snapshot.dump(state)))
        return restored


class RoundTripTests(_Snapshots):
    def test_stories_go_on_exactly_after_a_restore(self):
        for seed in range(4):
            for cut in (0, 5, 18, 40):
                with self.subTest(seed=seed, cut=cut):
                    state, genome = _story(seed, cut)
                    self.assertContinuesIdentically(state, genome)

    def test_edge_states(self):
        found = {}
        for seed in range(40):
            genome = Genome(rng=random.Random(seed))
            state = GameState(engine.build_world(genome), genome, rng=random.Random(seed))
            policy = random.Random(seed)
            for _ in range(120):
                if state.ended or not state.ranked_available_actions():
                    break
                state.ranked_available_actions()
                if (state.__dict__.get("climax_state") or {}).get("armed") and state.active_event is not None:
                    found.setdefault("climax", (snapshot.dump(state), genome))
                if any(name.startswith("_bargain_") for name in state.world.events) and "bargain" not in found:
                    found["bargain"] = (snapshot.dump(state), genome)
                if state.__dict__.get("confined_in"):
                    found.setdefault("confined", (snapshot.dump(state), genome))
                state.step(policy.randrange(len(state.ranked_available_actions())))
            if state.ended:
                found.setdefault("ended", (snapshot.dump(state), genome))
            if len(found) == 4:
                break
        self.assertTrue({"climax", "ended"} <= set(found), set(found))
        for kind, (data, genome) in found.items():
            with self.subTest(kind=kind):
                restored = snapshot.load(json.loads(json.dumps(data)), genome=genome)
                self.assertEqual(snapshot.canonical(snapshot.dump(restored)), snapshot.canonical(data))
                if kind == "climax":
                    self.assertTrue(restored.active_event.name.startswith("_climax_"))
                    self.assertTrue(restored.ranked_available_actions())
                if kind == "ended":
                    self.assertTrue(restored.ended)

    def test_every_attribute_is_saved_or_rebuilt(self):
        state, genome = _story(2, 60)
        data = snapshot.dump(state)
        saved = set(data["attributes"])
        rebuilt = snapshot.SKIP
        self.assertEqual(set(vars(state)) - saved - rebuilt, set())
        self.assertNotIn("_site_index", saved)
        json.dumps(data)  # JSON only

    def test_a_snapshot_must_fit_this_engine(self):
        state, genome = _story(1, 6)
        data = snapshot.dump(state)
        other = Genome(rng=random.Random(99))
        with self.assertRaises(snapshot.SnapshotMismatch):
            snapshot.load(data, genome=other)  # another storyteller
        with self.assertRaises(snapshot.SnapshotMismatch):
            snapshot.load({**data, "code_version": "other"}, genome=genome)
        with self.assertRaises(snapshot.SnapshotMismatch):
            snapshot.load({**data, "config_hash": "other"}, genome=genome)
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.load({**data, "format": "pickle"}, genome=genome)
        with self.assertRaises(snapshot.SnapshotError):  # an unknown class is never built
            snapshot.load({**data, "attributes": {**data["attributes"], "x": {"$o": "Popen", "a": {}}}}, genome=genome)
        state._turn = {"action": "x"}
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.dump(state)  # not in the middle of a turn


class FreshProcessTests(_Snapshots):
    def test_a_snapshot_goes_on_identically_in_another_process(self):
        state, genome = _story(3, 15)
        folder = Path(tempfile.mkdtemp(prefix="ane-snapshot-proc-"))
        (folder / "snap.json").write_text(json.dumps(snapshot.dump(state)))
        _play(state, random.Random(7), 20)
        script = (
            "import json, random, sys\n"
            "import main as engine\n"
            "from core_engine import snapshot\n"
            f"data = json.load(open({str(folder / 'snap.json')!r}))\n"
            "state = snapshot.load(data)\n"
            "policy = random.Random(7)\n"
            "for _ in range(20):\n"
            "    if state.ended: break\n"
            "    options = state.ranked_available_actions()\n"
            "    if not options: break\n"
            "    state.step(policy.randrange(len(options)))\n"
            "print(json.dumps(state.history))\n"
        )
        for hash_seed in ("0", "1", "random"):
            with self.subTest(PYTHONHASHSEED=hash_seed):
                env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONPATH": str(ROOT)}
                out = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True,
                                     timeout=600)
                self.assertEqual(out.returncode, 0, out.stderr[-2000:])
                self.assertEqual(json.loads(out.stdout.strip().splitlines()[-1]), state.history)

    def test_the_code_version_is_stable(self):
        self.assertEqual(version.code_version(), version.source_version())
        self.assertRegex(version.code_version(), r"^[0-9a-f]{16}$")


if __name__ == "__main__":
    unittest.main()
