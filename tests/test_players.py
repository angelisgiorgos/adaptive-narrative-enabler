"""Simulated players and the storyteller (core_engine/players.py): in evaluated stories the player's
turns are taken by players who choose among the choices shown, never by the storyteller, which only
directs; the same story seed always has the same player; the older evaluation stays available."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import collections
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main as engine
from core_engine import GameState, Genome, players
from core_engine.evolution import NarrativeEvolution, evaluate_task, evolution_settings, play_episode

ROOT = Path(__file__).resolve().parent.parent


class _Players(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"ANE_SAVE_DIR": tempfile.mkdtemp(prefix="ane-players-")})
        patcher.start()
        self.addCleanup(patcher.stop)
        engine.refresh_runtime_settings()

    def _story(self, seed, player, steps=40):
        genome = Genome(rng=random.Random(seed))
        state = GameState(engine.build_world(genome), genome, rng=random.Random(seed))
        return players.play(state, steps, player)


class WhoPlaysTests(_Players):
    def test_a_story_seed_always_has_the_same_player_and_story(self):
        self.assertEqual(players.player_for(7).name, players.player_for(7).name)
        first, second = self._story(3, players.player_for(3)), self._story(3, players.player_for(3))
        self.assertEqual(first.history, second.history)
        code = "from core_engine import players; print(players.player_for(7).name, players.player_for(8).name)"
        env = {**os.environ, "PYTHONHASHSEED": "123"}
        other = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(other.stdout.split(), [players.player_for(7).name, players.player_for(8).name])

    def test_the_mix_decides_how_often_each_player_plays(self):
        counts = collections.Counter(players.player_for(seed).name for seed in range(700))
        self.assertEqual(set(counts), set(players.PLAYERS))
        self.assertGreater(counts["follower"], counts["random"])  # weight 2 against 1
        only = collections.Counter(players.player_for(s, {"explorer": 1, "random": 0}).name for s in range(50))
        self.assertEqual(set(only), {"explorer"})
        self.assertEqual(players.mix_errors(players.DEFAULT_MIX), [])
        for bad in ({}, {"wizard": 1}, {"random": 0}, ["random"]):
            with self.subTest(mix=bad):
                self.assertTrue(players.mix_errors(bad))

    def test_players_choose_among_the_choices_shown(self):
        for name, cls in players.PLAYERS.items():
            with self.subTest(player=name):
                genome = Genome(rng=random.Random(1))
                state = GameState(engine.build_world(genome), genome, rng=random.Random(1))
                player = cls(random.Random(1))
                for _ in range(15):
                    options = state.player_options()
                    if state.ended or not options:
                        break
                    self.assertIn(player.choose(state, options), range(len(options)))
                    players.take_turn(state, player)
                self.assertGreater(state.actions_taken, 0)

    def test_the_storyteller_does_not_choose_for_simulated_players(self):
        """Evolution's stories: the storyteller only directs; its own pick is never taken."""
        settings = evolution_settings()
        task = (engine.build_world, engine.fitness, Genome(rng=random.Random(2)).traits(), [0, 1], 30,
                {"player": "simulated", "player_mix": settings["player_mix"]})
        with patch.object(GameState, "_storyteller_pick", side_effect=AssertionError("the storyteller chose")):
            self.assertIsInstance(evaluate_task(task), float)
        picks = []
        original = GameState._storyteller_pick

        def counted(state, actions):
            picks.append(1)
            return original(state, actions)

        with patch.object(GameState, "_storyteller_pick", counted):
            evaluate_task(task[:5] + ({"player": "storyteller"},))
        self.assertTrue(picks)  # the older evaluation: the storyteller takes the player's turns

    def test_one_ranking_per_turn_tells_the_same_story(self):
        def thrice_ranked(state, player):
            options = state.player_options()
            if not options:
                return False
            chosen = options[player.choose(state, options)]
            names = [action.name for action in state.ranked_available_actions()]
            return state.step(names.index(chosen.name))

        for seed in range(3):
            with self.subTest(seed=seed):
                genome = Genome(rng=random.Random(seed))
                a = GameState(engine.build_world(genome), genome, rng=random.Random(seed))
                b = GameState(engine.build_world(genome), genome, rng=random.Random(seed))
                pa, pb = players.player_for(seed), players.player_for(seed)
                for _ in range(40):
                    went_on = [players.take_turn(a, pa), thrice_ranked(b, pb)]  # both take the turn
                    if not all(went_on):
                        break
                self.assertEqual(a.history, b.history)
                self.assertEqual(a.rng.getstate(), b.rng.getstate())

    def test_what_players_prefer(self):
        genome = Genome(rng=random.Random(4))
        state = GameState(engine.build_world(genome), genome, rng=random.Random(4))
        options = state.player_options()
        explorer = players.Explorer(random.Random(0)).weights(state, options)
        for action, weight in zip(options, explorer):
            if action.source == "navigation" and not state.location_counts.get(action.owner):
                self.assertEqual(weight, max(explorer))  # a place not yet seen
        follower = players.Follower(random.Random(0)).weights(state, options)
        self.assertEqual(follower, sorted(follower, reverse=True))  # the first shown, the likeliest
        cautious = players.Cautious(random.Random(0)).weights(state, options)
        self.assertEqual(len(cautious), len(options))


class EvolutionTests(_Players):
    def test_evolution_evaluates_with_simulated_players_and_checks_the_setting(self):
        evolution = NarrativeEvolution(engine.build_world, engine.fitness, seed=1)
        self.assertEqual(evolution.settings["player"], "simulated")
        self.assertEqual(evolution.benchmark_task(Genome().traits())[5]["player"], "simulated")
        for bad in ({"player": "puppet"}, {"player_mix": {"wizard": 1}}):
            with self.subTest(settings=bad), self.assertRaises(ValueError):
                NarrativeEvolution(engine.build_world, engine.fitness,
                                   settings={"pop_size": 2, "generations": 1, "episodes_per_genome": 1, **bad}).run()

    def test_training_saved_before_simulated_players_goes_on_as_it_was_trained(self):
        evolution = NarrativeEvolution(engine.build_world, engine.fitness, seed=1)
        checkpoint = {"format": NarrativeEvolution.CHECKPOINT_FORMAT, "next_generation": 1, "seed": 1,
                      "settings": {k: v for k, v in evolution.settings.items() if k not in ("player", "player_mix")}}
        resumed = NarrativeEvolution.from_checkpoint(engine.build_world, engine.fitness, checkpoint)
        self.assertEqual(resumed.settings["player"], "storyteller")

    def test_play_episode_takes_a_player(self):
        genome = Genome(rng=random.Random(5))
        with patch.object(GameState, "_storyteller_pick", side_effect=AssertionError("the storyteller chose")):
            state = play_episode(engine.build_world, genome, 5, 30, None, players.player_for(5))
        self.assertGreater(state.actions_taken, 0)


if __name__ == "__main__":
    unittest.main()
