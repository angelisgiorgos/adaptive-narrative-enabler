"""Session-scoped engine configuration (core_engine/context.py): every game, training run and
evaluation reads its own frozen EngineContext, whatever happens to the process-wide configuration
while it runs (another engine activated for another session, an editor saving the files, an upload),
and tag vectors never leak from one engine or world into another."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import copy
import os
import pickle
import random
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import yaml

import main as engine
from backend import content, sessions, store
from core_engine import Genome, GameState
from core_engine import builder, snapshot
from core_engine.context import EngineContext, current_context, propagate
from core_engine.evolution import NarrativeEvolution
from core_engine.fitness import fitness
from core_engine.model import EngineModel, world_tags
from utils import tag_similarity
from utils.config_loader import config

QUICK = {"pop_size": 3, "episodes_per_genome": 1, "validation_episodes": 2, "max_steps": 6,
         "full_length_stories": False, "warm_start": False, "generations": 2}
STEPS = 25


def _changed(cfg):
    """A configuration whose tuning makes a storyteller choose differently (and a few settings a
    game reads as it goes)."""
    cfg = copy.deepcopy(cfg)
    selection = cfg.setdefault("action_selection", {})
    selection.update(base_weight=40.0, repetition_penalty=0.0, movement_weight=30.0, discovery_weight=0.0,
                     threat_weight=25.0, event_trigger_weight=0.0, mission_goal_weight=0.0)
    cfg.setdefault("game_state", {})["initial_coins"] = 77
    cfg.setdefault("scene", {})["max_visible_npcs"] = 0
    cfg.setdefault("player_options", {})["limit"] = 2
    return cfg


def _play(state, steps=STEPS):
    for _ in range(steps):
        if state.ended or not state.step():
            break
    return list(state.history)


def _fake_vectors(tags, seed, size=16):
    rng = random.Random(seed)
    return {tag: [rng.uniform(-1, 1) for _ in range(size)] for tag in sorted(tags)}


class _Clean(unittest.TestCase):
    """Every test leaves the process-wide configuration and tag vectors as the files make them."""

    def setUp(self):
        config.clear_override()
        self.addCleanup(config.clear_override)
        self.assertIsNone(current_context())


class EngineContextTests(_Clean):
    def test_frozen_snapshot_of_the_configuration(self):
        context = EngineContext.capture(seed=3)
        self.assertEqual(context.get("game_state.initial_coins"), config.get("game_state.initial_coins"))
        self.assertEqual(context.world_version, EngineModel(config.as_dict(), {}).world_id)  # the same digest
        config.use_override(_changed(config.as_dict()))
        self.assertEqual(config.get("game_state.initial_coins"), 77)
        self.assertNotEqual(context.get("game_state.initial_coins"), 77)   # taken before: unchanged
        self.assertTrue(context.unchanged())
        with self.assertRaises(AttributeError):
            context.seed = 4
        with self.assertRaises(TypeError):
            context.hyperparameters["pop_size"] = 1                          # read-only views
        with self.assertRaises(TypeError):
            context.world["locations"]["Nowhere"] = {}
        mine = context.as_dict()
        mine["game_state"]["initial_coins"] = -1                             # a copy: the context keeps its own
        self.assertNotEqual(context.get("game_state.initial_coins"), -1)

    def test_binding_routes_the_legacy_loader(self):
        context = EngineContext(_changed(config.as_dict()), seed=1)
        self.assertNotEqual(config.get("game_state.initial_coins"), 77)
        with context.bind():
            self.assertIs(current_context(), context)
            self.assertEqual(config.get("game_state.initial_coins"), 77)
            self.assertEqual(config.as_dict()["player_options"]["limit"], 2)
            self.assertNotEqual(config.process_get("game_state.initial_coins"), 77)  # the process-wide value
            with EngineContext.capture().bind():                                     # nested
                self.assertNotEqual(config.get("game_state.initial_coins"), 77)
            self.assertEqual(config.get("game_state.initial_coins"), 77)
            # Threads do not inherit it unless the work is propagated.
            with ThreadPoolExecutor(1) as pool:
                bare = pool.submit(config.get, "game_state.initial_coins").result()
                carried = pool.submit(propagate(config.get), "game_state.initial_coins").result()
            self.assertNotEqual(bare, 77)
            self.assertEqual(carried, 77)
            self.assertIs(pickle.loads(pickle.dumps(propagate(fitness))), fitness)  # processes: the bare function
        self.assertIsNone(current_context())

    def test_copy_and_pickle(self):
        context = EngineContext(config.as_dict(), genome=Genome(rng=random.Random(0)), seed=8,
                                tag_vectors=_fake_vectors({"harbor", "storm"}, 1))
        self.assertIs(copy.deepcopy(context), context)
        restored = pickle.loads(pickle.dumps(context))
        self.assertEqual((restored.world_version, restored.seed, dict(restored.genome_traits)),
                         (context.world_version, context.seed, dict(context.genome_traits)))
        self.assertEqual(restored.tag_space.key, context.tag_space.key)

    def test_a_state_keeps_the_context_it_started_with(self):
        genome = Genome(rng=random.Random(2))
        state = GameState(builder.build_world(genome), genome, rng=random.Random(4))
        self.assertEqual(state.context.world_version, EngineContext.capture().world_version)
        self.assertEqual(dict(state.context.genome_traits), genome.traits())
        self.assertIs(copy.deepcopy(state).context, state.context)
        data = snapshot.dump(state)
        self.assertNotIn("_context", data["attributes"])
        self.assertEqual(snapshot.load(data).context.world_version, state.context.world_version)


class ActiveGameTests(_Clean):
    """A game in progress keeps its configuration, whatever the process-wide one becomes."""

    def _twins(self, context=None, seed=11):
        genome = Genome(rng=random.Random(seed))

        def new():
            with (context or EngineContext.capture()).bind():
                return GameState(builder.build_world(genome), genome, rng=random.Random(seed))

        return new

    def test_global_changes_mid_game_change_nothing(self):
        new = self._twins()
        reference = _play(new())
        game = new()
        first = _play(game, 5)
        config.use_override(_changed(config.as_dict()))                  # an upload / another engine...
        self.assertEqual(config.get("game_state.initial_coins"), 77)
        _play(game, STEPS - 5)
        self.assertEqual(game.history, reference)
        self.assertEqual(first, reference[:len(first)])
        self.assertTrue(game.context.unchanged())                        # nothing wrote into it either
        # ...while a game started now runs with the new configuration, and it does change the story.
        changed = new()
        self.assertEqual(changed.coins, 77)
        self.assertNotEqual(_play(changed), reference)
        config.clear_override()

    def test_editor_saves_do_not_reach_a_running_game(self):
        folder = Path(tempfile.mkdtemp(prefix="ane-config-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        for path in store.CONFIG_DIR.glob("*.yaml"):
            shutil.copy(path, folder / path.name)
        for patcher in (patch.object(config, "_config_dir", str(folder)), patch.object(store, "CONFIG_DIR", folder),
                        patch.dict(content.ENTITY_FILES, {"Locations": (folder / "locations.yaml", "locations",
                                                                        "mapping")})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(config.reload)
        config.reload()
        new = self._twins(seed=12)
        reference = _play(new())
        game = new()
        _play(game, 4)
        # The editor saves tuning (hyperparameters.yaml) and the world (locations.yaml) while it runs.
        tuning = yaml.safe_load((folder / "hyperparameters.yaml").read_text(encoding="utf-8"))
        store.save_all_hyperparameters(yaml.safe_dump(_changed(tuning), sort_keys=False))
        place = next(iter(config.get("world_definition.locations")))
        entry = dict(config.get(f"world_definition.locations.{place}"))
        entry["description"] = "Rewritten by the editor while a game was running."
        content.save_entity_yaml("Locations", place, yaml.safe_dump(entry, sort_keys=False))
        self.assertEqual(config.get("game_state.initial_coins"), 77)
        self.assertEqual(config.get(f"world_definition.locations.{place}.description"), entry["description"])
        _play(game, STEPS - 4)
        self.assertEqual(game.history, reference)
        self.assertNotEqual(game.context.get(f"world_definition.locations.{place}.description"), entry["description"])
        later = new()                                                  # what starts now uses the saves
        self.assertEqual(later.context.get(f"world_definition.locations.{place}.description"), entry["description"])
        self.assertEqual(later.coins, 77)

    def test_runtime_settings_takes_effect_note(self):
        from backend import activation

        self.assertIn("keeps the configuration it started with", activation.CONFIG_NOTE)
        self.assertIn("keeps the configuration it started with", activation.config_saved("x", "y.yaml"))


class TwoEnginesTests(_Clean):
    """Two saved engines played at once, their turns alternating, each activation in between."""

    @classmethod
    def setUpClass(cls):
        config.clear_override()
        base = config.as_dict()
        space = tag_similarity.shared_space(None)
        cls.first = EngineModel(base, Genome(rng=random.Random(21)).traits(),
                                tag_vectors=space.export(world_tags(base)), source="first")
        other = _changed(base)
        cls.second = EngineModel(other, Genome(rng=random.Random(22)).traits(),
                                 tag_vectors=_fake_vectors(world_tags(other), 5), source="second")
        cls.folder = Path(tempfile.mkdtemp(prefix="ane-engines-"))
        cls.paths = [cls.first.save(cls.folder / "first.ane.zip"), cls.second.save(cls.folder / "second.ane.zip")]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.folder, ignore_errors=True)
        config.clear_override()

    def test_models_play_alternately_as_alone(self):
        alone = [_play(self.first.new_game(seed=5)), _play(self.second.new_game(seed=6))]
        self.assertNotEqual(alone[0], alone[1])
        games = [self.first.new_game(seed=5), self.second.new_game(seed=6)]
        models = [self.first, self.second]
        for turn in range(STEPS):
            for index, game in enumerate(games):
                models[1 - index].activate()            # the other session's engine becomes the active one
                if turn % 7 == 3:
                    config.clear_override()             # ...or an editor saves
                if not game.ended:
                    game.step()
        self.assertEqual(games[0].history, alone[0])
        self.assertEqual(games[1].history, alone[1])
        self.assertEqual([g.context.world_version for g in games], [m.world_id for m in models])

    def test_saved_and_reloaded_engines_behave_the_same(self):
        for model, path in zip((self.first, self.second), self.paths):
            loaded = EngineModel.load(path)
            self.assertEqual(loaded.context().world_version, model.context().world_version)
            self.assertEqual(loaded.context().tag_space.key, model.context().tag_space.key)
            self.assertEqual(_play(loaded.new_game(seed=9)), _play(model.new_game(seed=9)))
            self.assertEqual(sorted(loaded.build_world().locations), sorted(model.build_world().locations))

    def test_play_sessions_alternate_through_the_backend(self):
        def start(path, seed):
            random.seed(seed)  # a session's dice are drawn from the global ones
            game = sessions.load_best_engine(path, profile=f"player{seed}")  # its own profile: its own start history
            return game["token"], game["actions"]

        def fresh_saves():  # each phase starts from the same (empty) save games
            folder = tempfile.mkdtemp(prefix="ane-saves-")
            self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
            patcher = patch.dict(os.environ, {"ANE_SAVE_DIR": folder})
            patcher.start()
            self.addCleanup(patcher.stop)

        def play(token, actions, turns):
            texts = []
            for _ in range(turns):
                if not actions:
                    break
                _, actions, text = sessions.play_action(token, actions[0])
                texts.append(text)
            return texts, actions

        fresh_saves()
        first = play(*start(self.paths[0], 1), 8)[0]
        fresh_saves()
        alone = [first, play(*start(self.paths[1], 2), 8)[0]]
        self.assertNotEqual(alone[0], alone[1])
        fresh_saves()
        a, b = start(self.paths[0], 1), start(self.paths[1], 2)   # loading the second activates it process-wide
        self.assertEqual(sessions.SESSIONS[a[0]].context.world_version, self.first.world_id)
        self.assertEqual(sessions.SESSIONS[b[0]].context.world_version, self.second.world_id)
        played = {a[0]: ([], a[1]), b[0]: ([], b[1])}
        for _ in range(8):
            for token in (a[0], b[0]):
                texts, actions = played[token]
                if actions:
                    more, actions = play(token, actions, 1)
                    played[token] = (texts + more, actions)
        self.assertEqual(played[a[0]][0], alone[0])
        self.assertEqual(played[b[0]][0], alone[1])
        self.assertLessEqual(len(sessions._scene(sessions.SESSIONS[b[0]])[1]), 2)   # its own player_options.limit
        for token in (a[0], b[0]):
            sessions.end_session(token)


class TrainingRunTests(_Clean):
    def _run(self, progress=None, before=None):
        evolution = NarrativeEvolution(engine.build_world, fitness, settings=QUICK, seed=4)
        if before:
            before()
        result = evolution.run(progress=progress)
        return [h.validation for h in result.history], result.best_genome.traits()

    def test_a_run_reads_the_configuration_it_started_with(self):
        reference = self._run()
        changed = _changed(config.as_dict())
        swaps = []

        def progress(generation, total, stats):  # mid-run: another upload, then the files again
            swaps.append(generation)
            config.use_override(changed) if generation % 2 == 0 else config.clear_override()

        self.assertEqual(self._run(progress=progress, before=lambda: config.use_override(changed)), reference)
        self.assertTrue(swaps)
        config.use_override(changed)
        self.assertNotEqual(self._run(), reference)   # started under the change: it does read it

    def test_thread_pools_read_the_runs_context(self):
        reference = self._run()
        with ThreadPoolExecutor(2) as pool:
            def map_fn(function, tasks):
                return list(pool.map(function, tasks))

            evolution = NarrativeEvolution(engine.build_world, fitness, settings=QUICK, seed=4, map_fn=map_fn)
            config.use_override(_changed(config.as_dict()))
            result = evolution.run()
        self.assertEqual(([h.validation for h in result.history], result.best_genome.traits()), reference)


class TagVectorTests(_Clean):
    TAGS = (["harbor", "sea"], ["storm", "danger"])

    def test_engines_never_share_trained_vectors(self):
        plain = EngineContext.capture()
        with plain.bind():
            expected = tag_similarity.semantic_tag_similarity(*self.TAGS)
        trained = EngineContext(config.as_dict(), tag_vectors=_fake_vectors({"harbor", "sea", "storm"}, 3))
        with trained.bind():
            theirs = tag_similarity.semantic_tag_similarity(*self.TAGS)
            self.assertEqual(tag_similarity.export_tag_vectors(["danger"])["danger"], [0.0] * 16)  # unknown: neutral
        self.assertNotAlmostEqual(theirs, expected)
        self.assertNotEqual(trained.tag_space.key, plain.tag_space.key)
        # The process-wide vectors (an older caller) reach neither context...
        tag_similarity.load_tag_vectors(_fake_vectors({"harbor", "sea", "storm", "danger"}, 4))
        with plain.bind():
            self.assertEqual(tag_similarity.semantic_tag_similarity(*self.TAGS), expected)
        with trained.bind():
            self.assertEqual(tag_similarity.semantic_tag_similarity(*self.TAGS), theirs)
        # ...and go when the override goes: the files' world is not compared with another's vectors.
        self.assertTrue(tag_similarity.default_space().preloaded)
        config.clear_override()
        self.assertFalse(tag_similarity.default_space().preloaded)
        self.assertAlmostEqual(tag_similarity.semantic_tag_similarity(*self.TAGS), expected)

    def test_activation_replaces_the_process_wide_vectors(self):
        first = EngineModel(config.as_dict(), {}, tag_vectors=_fake_vectors({"harbor", "sea"}, 1))
        second = EngineModel(config.as_dict(), {}, tag_vectors=_fake_vectors({"storm"}, 2))
        first.activate()
        second.activate()
        self.assertEqual(set(tag_similarity.default_space().preloaded), {"storm"})   # not merged with the first's

    def test_spawn_rules_cache_is_per_tag_space(self):
        world = copy.deepcopy(config.get("world_definition"))
        world["spawn_strategy"] = "similarity"
        world.pop("routes", None)
        world.pop("story_map", None)
        cfg = {**config.as_dict(), "world_definition": world}
        locations = {name: type("L", (), {"tags": set(data.get("tags") or [])})()
                     for name, data in world["locations"].items()}
        rules = []
        for vectors in (None, _fake_vectors(world_tags(cfg), 6)):
            with EngineContext(cfg, tag_vectors=vectors).bind():
                rules.append(builder._build_spawn_rules(locations, world["locations"], world))
        keys = [key for key in builder._SPAWN_RULES_CACHE if key[0] == "similarity"]
        self.assertGreaterEqual(len({key[-1] for key in keys}), 2)   # one entry per tag space

    def test_concurrent_contexts_in_threads(self):
        contexts = [EngineContext(config.as_dict(), tag_vectors=_fake_vectors({"harbor", "sea", "storm", "danger"},
                                                                                seed)) for seed in (1, 2)]
        expected = []
        for context in contexts:
            with context.bind():
                expected.append(tag_similarity.semantic_tag_similarity(*self.TAGS))
        results, barrier = {}, threading.Barrier(2)

        def work(index):
            with contexts[index].bind():
                barrier.wait()
                results[index] = [tag_similarity.semantic_tag_similarity(*self.TAGS) for _ in range(50)]

        threads = [threading.Thread(target=work, args=(index,)) for index in (0, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results, {0: [expected[0]] * 50, 1: [expected[1]] * 50})


if __name__ == "__main__":
    unittest.main()
