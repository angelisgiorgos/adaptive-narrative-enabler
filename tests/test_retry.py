"""Unique actions that can be retried after a failure (retry_on_failure)."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import pickle
import random
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from backend import content, store
from backend.errors import AuthoringError
from core_engine import Action, GameState, Genome, Location, Outcome, WorldGraph
from core_engine.actions import repetition_errors
from core_engine.builder import logic_errors
from core_engine.combat import CheckResult
from narrative_runtime.engine import FORMAT, Game
from utils.config_loader import config


def _genome():
    random.seed(5)
    genome = Genome()
    genome.event_prob = 0.0
    genome.chaos_bias = 0.0
    return genome


def _lock(repeat="once", retry=False, group=None):
    """Picking the lock rolls a stealth check: pass opens it, fail does not."""
    return Action("Pick the lock", [
        Outcome("The lock clicks open.", tags=["neutral"], on_check="pass"),
        Outcome("The pick snaps.", tags=["neutral"], on_check="fail"),
    ], skill_check={"stat": "stealth"}, repeat=repeat, retry_on_failure=retry, exclusive_group=group,
        source="location", owner="Gate")


def _world(*gate_actions):
    world = WorldGraph()
    gate, yard = Location("Gate", ["gate"]), Location("Yard", ["yard"])
    gate.actions = [*gate_actions, Action("Wait", [Outcome("You wait.", tags=["neutral"])], repeat="always",
                                          source="location", owner="Gate")]
    gate.connect(yard)
    world.add_location(gate)
    world.add_location(yard)
    return world


def _rolls(*passes):
    """Skill checks pass or fail in this order."""
    results = iter(passes)

    def roll(combat, stat, modifier=0, inventory=(), step=0):
        passed = next(results)
        return CheckResult(stat, 1 if passed else 100, combat.target(stat, modifier, inventory, step), passed, step)

    return patch("core_engine.combat.CombatSystem.roll", roll)


def _names(state):
    return [action.name for action in state.ranked_available_actions()]


def _choose(state, name):
    return state.step(_names(state).index(name))


def _go(state, place):
    """Travel, whether the place is new ("Move to") or known ("Return to")."""
    names = _names(state)
    return state.step(next(i for i, n in enumerate(names) if n in (f"Move to {place}", f"Return to {place}")))


def _saved_and_restored(state, folder):
    """Through the runtime's own save format (Game.save) and back."""
    path = Game(None, state).save(Path(folder) / "slot.game")
    data = pickle.loads(Path(path).read_bytes())
    assert data["format"] == FORMAT
    return data["state"]


class RetryOnFailureTests(unittest.TestCase):
    def setUp(self):
        for patcher in (
            patch.dict(config._config["unexpected_events"], {"enabled": False}),
            patch.dict(config._config["narrative_end"], {"max_consecutive_steps_in_location": 100}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = directory.name

    def _state(self, *actions):
        return GameState(_world(*actions), _genome(), start="Gate", rng=random.Random(0))

    def _offered_everywhere(self, state):
        """Whether the lock is offered now, after leaving and coming back, and after a save/restore."""
        now = "Pick the lock" in _names(state)
        restored = "Pick the lock" in _names(_saved_and_restored(state, self.folder))
        _go(state, "Yard")
        _go(state, "Gate")
        revisited = "Pick the lock" in _names(state)
        return now, revisited, restored

    def test_once_actions_under_both_retry_settings(self):
        for retry in (False, True):
            for passes in (True, False):
                with self.subTest(retry=retry, first_attempt="success" if passes else "failure"):
                    state = self._state(_lock(retry=retry))
                    with _rolls(passes):
                        _choose(state, "Pick the lock")
                    expected = retry and not passes  # only a failure with retry keeps it
                    self.assertEqual(self._offered_everywhere(state), (expected,) * 3)

    def test_retry_until_success_then_gone(self):
        state = self._state(_lock(retry=True))
        with _rolls(False, False, True):
            _choose(state, "Pick the lock")
            _choose(state, "Pick the lock")                     # again, in the same visit
            state = _saved_and_restored(state, self.folder)      # a saved game keeps the attempts
            _go(state, "Yard")
            _go(state, "Gate")
            _choose(state, "Pick the lock")                      # succeeds on the third try
        self.assertEqual(self._offered_everywhere(state), (False, False, False))
        # (told the second time with a line that knows it happened before: story_beats.variation)
        self.assertEqual(sum(line.startswith("RESULT: The pick snaps.") for line in state.history), 2)
        self.assertEqual(state.history.count("RESULT: The lock clicks open."), 1)

    def test_per_visit_actions(self):
        for retry in (False, True):
            with self.subTest(retry=retry):
                state = self._state(_lock(repeat="per_visit", retry=retry))
                with _rolls(False, True):
                    _choose(state, "Pick the lock")
                    self.assertEqual("Pick the lock" in _names(state), retry)  # retry in this visit
                    if retry:
                        _choose(state, "Pick the lock")
                        self.assertNotIn("Pick the lock", _names(state))     # done for this visit
                    _go(state, "Yard")
                    _go(state, "Gate")
                    self.assertIn("Pick the lock", _names(state))            # a new visit

    def test_exclusive_groups_stay_open_after_a_retried_failure(self):
        state = self._state(_lock(retry=True, group="way_in"),
                            Action("Climb the wall", [Outcome("Up you go.", tags=["neutral"])], repeat="once",
                                   exclusive_group="way_in", source="location", owner="Gate"))
        with _rolls(False):
            _choose(state, "Pick the lock")
        self.assertTrue({"Pick the lock", "Climb the wall"} <= set(_names(state)))
        _choose(state, "Climb the wall")
        self.assertFalse({"Pick the lock", "Climb the wall"} & set(_names(state)))

    def test_games_saved_before_the_field_existed_still_load(self):
        state = self._state(_lock())
        del state.world.locations["Gate"].actions[0].__dict__["retry_on_failure"]  # as in an older save
        restored = _saved_and_restored(state, self.folder)
        self.assertFalse(restored.world.locations["Gate"].actions[0].retry_on_failure)
        with _rolls(False):
            _choose(restored, "Pick the lock")
        self.assertNotIn("Pick the lock", _names(restored))


class RetryAuthoringTests(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(repetition_errors("once", True), [])
        self.assertEqual(repetition_errors(None, True), [])            # the source's default decides
        self.assertTrue(repetition_errors("always", True))              # it would never run out anyway
        self.assertTrue(repetition_errors("once", "yes"))
        self.assertTrue(repetition_errors("twice", None))
        world = config.default_config()["world_definition"]
        objects = [{**world["objects"][0], "actions": [{**world["objects"][0]["actions"][0], "retry_on_failure": 1}]}]
        self.assertTrue(logic_errors({**world, "objects": objects + world["objects"][1:]}))

    def test_the_editor_checkbox_writes_the_field(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "items.yaml"
        shutil.copy(store.CONFIG_DIR / "items.yaml", path)
        for patcher in (patch.dict(content.ENTITY_FILES, {"Items": (path, "objects", "list")}),
                        patch.object(store.config, "reload")):
            patcher.start()
            self.addCleanup(patcher.stop)
        choices = content.entity_action_choices("Items", "Ancient Map")
        self.assertEqual(choices[0], ("1. Study the map", 0))
        self.assertEqual(content.load_action_repetition("Items", "Ancient Map", 0), ("once", False))
        content.save_action_repetition("Items", "Ancient Map", 0, "once", True)
        stored = yaml.safe_load(path.read_text())["world_definition"]["objects"][0]["actions"][0]
        self.assertEqual((stored["repeat"], stored["retry_on_failure"]), ("once", True))
        self.assertEqual(content.load_action_repetition("Items", "Ancient Map", 0), ("once", True))
        before = path.read_bytes()
        with self.assertRaises(AuthoringError):
            content.save_action_repetition("Items", "Ancient Map", 0, "always", True)
        self.assertEqual(path.read_bytes(), before)                     # rejected: nothing written
        content.save_action_repetition("Items", "Ancient Map", 0, content.REPEAT_DEFAULT, False)
        stored = yaml.safe_load(path.read_text())["world_definition"]["objects"][0]["actions"][0]
        self.assertNotIn("repeat", stored)
        self.assertNotIn("retry_on_failure", stored)


if __name__ == "__main__":
    unittest.main()
