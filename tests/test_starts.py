"""Starting locations: eligibility, seeded variation, recent-start history and mission viability."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import os
import random
import shutil
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

import api
import main as engine
from backend import content, sessions, store
from backend.errors import AuthoringError
from core_engine import Action, GameState, Genome, Location, Mission, Outcome, WorldGraph, starts
from core_engine import variables as persistent
from utils.config_loader import config

SHIPPED_STARTS = {"Harbor": 1.5, "Market": 1.0, "Tavern": 1.0, "Festival of Masks": 0.5}


def _genome():
    random.seed(11)
    return Genome()


def _world(*locations, missions=()):
    """``locations``: (name, eligible, weight) triples; no routes between them."""
    world = WorldGraph()
    for name, eligible, weight in locations:
        world.add_location(Location(name, [name.lower()], start_eligible=eligible, start_weight=weight))
    for mission in missions:
        world.add_mission(mission)
    return world


def _shares(counts):
    total = sum(counts.values())
    return {name: count / total for name, count in counts.items()}


class _TempStore(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.store = persistent.VariableStore(str(self.dir / "save.json"))
        env = patch.dict(os.environ, {"ANE_SAVE_DIR": str(self.dir)})
        env.start()
        self.addCleanup(env.stop)


class EligibilityTests(unittest.TestCase):
    def test_shipped_world_marks_its_starting_locations(self):
        world = engine.build_world(_genome())
        self.assertEqual({loc.name: loc.start_weight for loc in world.start_locations()}, SHIPPED_STARTS)
        self.assertFalse(world.locations["Dungeon"].start_eligible)

    def test_ineligible_locations_never_start_a_session(self):
        genome = _genome()
        histories = ([], ["Harbor"], ["Harbor", "Market", "Tavern"], ["Festival of Masks"] * 3)
        for seed in range(300):
            state = GameState(engine.build_world(genome), genome, rng=random.Random(seed),
                              recent_starts=histories[seed % len(histories)])
            self.assertIn(state.start_location, SHIPPED_STARTS, f"seed {seed}")
            self.assertEqual((state.current.name, state.path), (state.start_location, [state.start_location]))

    def test_starting_locations_switched_off_by_variables_never_start(self):
        genome = _genome()
        drawn = {
            GameState(engine.build_world(genome), genome, rng=random.Random(seed), variables={"reputation": -3}).start_location
            for seed in range(200)
        }
        self.assertEqual(drawn, {"Harbor", "Market", "Tavern"})  # the masquerade turns the traveler away

    def test_no_enabled_starting_location_is_an_error(self):
        world = _world(("Hall", False, 1.0))
        world.add_location(Location("Gate", ["gate"], start_eligible=True, enabled_if={"reputation": {"gte": 5}}))
        with self.assertRaises(ValueError):
            GameState(world, _genome(), rng=random.Random(0), variables={"reputation": 0})

    def test_worlds_without_starting_locations_keep_their_single_start(self):
        for names, expected in ((("Hall", "Harbor"), "Harbor"), (("Hall", "Crypt"), "Hall")):
            world = _world(*((name, False, 1.0) for name in names))
            state = GameState(world, _genome(), rng=random.Random(5))
            self.assertEqual(state.start_location, expected)
            forced = GameState(world, _genome(), rng=random.Random(5), start=expected)
            self.assertEqual(state.rng.getstate(), forced.rng.getstate())  # no draw for the start: replays as before

    def test_requested_start(self):
        genome = _genome()
        self.assertEqual(GameState(engine.build_world(genome), genome, start="Tavern").start_location, "Tavern")
        with self.assertRaises(ValueError):
            GameState(engine.build_world(genome), genome, start="Atlantis")
        self.assertEqual(sessions.play_state(GameState(engine.build_world(genome), genome, start="Market"))["start_location"],
                         "Market")


class VariationTests(unittest.TestCase):
    def test_several_starting_locations_are_drawn_across_seeded_sessions(self):
        genome = _genome()
        counts = Counter(GameState(engine.build_world(genome), genome, rng=random.Random(seed)).start_location
                         for seed in range(400))
        self.assertEqual(set(counts), set(SHIPPED_STARTS))
        total_weight = sum(SHIPPED_STARTS.values())
        for name, share in _shares(counts).items():
            self.assertAlmostEqual(share, SHIPPED_STARTS[name] / total_weight, delta=0.06, msg=name)

    def test_the_same_seed_gives_the_same_start(self):
        genome = _genome()
        for seed in range(10):
            first = GameState(engine.build_world(genome), genome, rng=random.Random(seed))
            again = GameState(engine.build_world(genome), genome, rng=random.Random(seed))
            self.assertEqual((first.start_location, first.mission.name), (again.start_location, again.mission.name))


class RecentStartTests(unittest.TestCase):
    def test_recent_starts_get_the_configured_weight(self):
        a, b, c = (Location(name, [], start_eligible=True, start_weight=w) for name, w in (("A", 1), ("B", 1), ("C", 2)))
        values = {"default_location": "A", "recent_starts": 3, "repeat_factor": 0.25}
        self.assertEqual(starts.weights([a, b, c], [], values), {"A": 1, "B": 1, "C": 2})
        self.assertEqual(starts.weights([a, b, c], ["A"], values), {"A": 0.25, "B": 1, "C": 2})
        self.assertEqual(starts.weights([a, b, c], ["A", "A"], values)["A"], 0.0625)  # each recent start counts
        self.assertEqual(starts.weights([a, b, c], ["A", "B", "C", "A"], values), {"A": 0.25, "B": 0.25, "C": 0.5})
        self.assertAlmostEqual(starts.probabilities([a, b, c], ["A"], values)["A"], 0.25 / 3.25)
        self.assertEqual(starts.weights([a, b, c], ["A"], {**values, "recent_starts": 0})["A"], 1)
        self.assertEqual(starts.weights([a, b, c], ["A"], {**values, "repeat_factor": 1})["A"], 1)

    def test_a_recent_start_is_drawn_with_the_reduced_probability(self):
        world = _world(("A", True, 1.0), ("B", True, 1.0))
        fresh = Counter(GameState(world, _genome(), rng=random.Random(seed)).start_location for seed in range(1000))
        repeat = Counter(GameState(world, _genome(), rng=random.Random(seed), recent_starts=["A"]).start_location
                         for seed in range(1000))
        self.assertAlmostEqual(_shares(fresh)["A"], 0.5, delta=0.04)
        self.assertAlmostEqual(_shares(repeat)["A"], 0.25 / 1.25, delta=0.04)  # discouraged...
        self.assertGreater(repeat["A"], 0)  # ...never forbidden

    def test_shipped_harbor_is_drawn_less_often_after_starting_there(self):
        genome = _genome()
        counts = Counter(GameState(engine.build_world(genome), genome, rng=random.Random(seed),
                                   recent_starts=["Harbor"]).start_location for seed in range(600))
        expected = 1.5 * 0.25 / (1.5 * 0.25 + 1.0 + 1.0 + 0.5)
        self.assertAlmostEqual(_shares(counts)["Harbor"], expected, delta=0.04)


class HistoryTests(_TempStore):
    def test_real_play_records_each_profiles_starts(self):
        genome = _genome()
        alice = [GameState(engine.build_world(genome), genome, rng=random.Random(seed), store=self.store, profile="alice")
                 for seed in range(4)]
        self.assertEqual(self.store.recent_starts("alice"), [state.start_location for state in alice])
        self.assertEqual(self.store.profile("alice")["starts"], self.store.recent_starts("alice"))
        self.assertEqual(self.store.recent_starts("bob"), [])
        nxt = GameState(engine.build_world(genome), genome, store=self.store, profile="alice")
        self.assertEqual(nxt.recent_starts, [state.start_location for state in alice])  # what the draw discouraged
        self.store.reset("alice")
        self.assertEqual(self.store.recent_starts("alice"), [])

    def test_history_is_capped_and_keeps_variables(self):
        self.store.save("carol", {"reputation": 2})
        for index in range(starts.HISTORY_LIMIT + 5):
            self.store.record_start("carol", f"L{index}")
        self.assertEqual(self.store.recent_starts("carol"),
                         [f"L{index}" for index in range(5, starts.HISTORY_LIMIT + 5)])
        self.assertEqual(self.store.profile("carol")["values"], {"reputation": 2})

    def test_simulations_never_record_starts(self):
        genome = _genome()
        GameState(engine.build_world(genome), genome, rng=random.Random(0), profile="alice")
        self.assertFalse(any(self.dir.iterdir()))

    def test_a_profiles_next_session_rarely_repeats_its_last_start(self):
        world = _world(("A", True, 1.0), ("B", True, 1.0))
        repeats = 0
        for player in range(300):
            first = GameState(world, _genome(), rng=random.Random(player), store=self.store, profile=f"p{player}")
            second = GameState(world, _genome(), rng=random.Random(10_000 + player), store=self.store, profile=f"p{player}")
            repeats += first.start_location == second.start_location
        self.assertAlmostEqual(repeats / 300, 0.25 / 1.25, delta=0.06)


class MissionViabilityTests(unittest.TestCase):
    """Dock can discover the way to the Keep; the Cellar has no way out; the Keep is the goal."""

    def _world(self, *missions):
        world = _world(("Dock", True, 1.0), ("Cellar", True, 1.0), ("Keep", True, 1.0), missions=missions)
        world.locations["Dock"].actions = [Action("Scout the shore", [Outcome("A path appears.", spawn=True)])]
        world.allow_spawn("Dock", "Keep")
        return world

    def test_stories_only_start_where_the_mission_can_be_reached(self):
        reach = Mission("reach_keep", {"location": "Keep"})
        for seed in range(100):
            state = GameState(self._world(reach), _genome(), rng=random.Random(seed))
            self.assertEqual((state.mission.name, state.start_location), ("reach_keep", "Dock"))
        requested = GameState(self._world(reach, Mission("rich", {"coins": 500})), _genome(),
                              rng=random.Random(0), mission="reach_keep")
        self.assertEqual(requested.start_location, "Dock")

    def test_each_mission_gets_a_start_it_can_be_reached_from(self):
        pairs = {
            (state.mission.name, state.start_location)
            for state in (GameState(self._world(Mission("reach_keep", {"location": "Keep"}), Mission("rich", {"coins": 500})),
                                    _genome(), rng=random.Random(seed)) for seed in range(200))
        }
        self.assertEqual(pairs, {("reach_keep", "Dock"), ("rich", "Dock"), ("rich", "Cellar"), ("rich", "Keep")})

    def test_missions_no_start_can_reach_are_left_out(self):
        world = self._world(Mission("rich", {"coins": 500}))
        world.add_location(Location("Island", ["island"]))
        world.add_mission(Mission("island", {"location": "Island"}))
        self.assertEqual({GameState(world, _genome(), rng=random.Random(seed)).mission.name for seed in range(50)}, {"rich"})


class ValidationTests(unittest.TestCase):
    def test_bad_location_fields_are_rejected(self):
        for field, value in (("start_eligible", "yes"), ("start_weight", 0), ("start_weight", -1), ("start_weight", "x")):
            with self.subTest(field=field, value=value):
                with patch.dict(config._config["world_definition"]["locations"]["Harbor"], {field: value}):
                    with self.assertRaises(ValueError):
                        engine.build_world(_genome())

    def test_bad_start_selection_is_rejected(self):
        for key, value in (("repeat_factor", 0), ("repeat_factor", 1.5), ("recent_starts", -1),
                           ("recent_starts", starts.HISTORY_LIMIT + 1), ("default_location", 3)):
            with self.subTest(key=key, value=value):
                with patch.dict(config._config["start_selection"], {key: value}):
                    with self.assertRaises(ValueError):
                        engine.build_world(_genome())

    def test_uploaded_configurations(self):
        reference = config.default_config()
        older = config.default_config()
        older.pop("start_selection")
        self.assertEqual(store._validate_complete_config(older, reference), [])  # saved before start variation
        bad = config.default_config()
        bad["world_definition"]["locations"]["Market"]["start_weight"] = 0
        bad["start_selection"]["repeat_factor"] = 0
        errors = "\n".join(store._validate_complete_config(bad, reference))
        self.assertIn("start_weight", errors)
        self.assertIn("repeat_factor", errors)


class AuthoringTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        for name in ("locations.yaml", "hyperparameters.yaml"):
            shutil.copy(store.CONFIG_DIR / name, self.dir / name)
        for patcher in (
            patch.dict(content.ENTITY_FILES, {"Locations": (self.dir / "locations.yaml", "locations", "mapping")}),
            patch.object(store, "HYPERPARAMETERS_PATH", self.dir / "hyperparameters.yaml"),
            patch.object(store.config, "reload"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(api.api)

    def _stored(self, name):
        return yaml.safe_load((self.dir / "locations.yaml").read_text())["world_definition"]["locations"][name]

    def test_locations_expose_their_start_settings(self):
        harbor = self.client.get("/v1/entities/Locations/Harbor").json()
        self.assertEqual((harbor["start_eligible"], harbor["start_weight"]), (True, 1.5))
        alley = self.client.get("/v1/entities/Locations/Alley").json()
        self.assertEqual((alley["start_eligible"], alley["start_weight"]), (False, 1.0))
        self.assertIsNone(self.client.get("/v1/entities/NPCs/Priest").json()["start_eligible"])

        response = self.client.put("/v1/entities/Locations/Alley", json={**alley, "start_eligible": True, "start_weight": 3})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((self._stored("Alley")["start_eligible"], self._stored("Alley")["start_weight"]), (True, 3))
        kept = self.client.put("/v1/entities/Locations/Alley", json={**alley, "start_eligible": None, "start_weight": None})
        self.assertEqual((kept.json()["start_eligible"], kept.json()["start_weight"]), (True, 3))  # omitted: kept
        self.assertEqual(self.client.put("/v1/entities/Locations/Alley", json={**alley, "start_weight": 0}).status_code, 422)
        name, tags, descriptions, goals, *_ = content.load_entity("Locations", "Alley")
        with self.assertRaises(AuthoringError):
            content.save_entity("Locations", name, name, tags, descriptions, goals, start_weight=-2)

    def test_start_selection_is_engine_logic(self):
        sections = {s["section"] for s in self.client.get("/v1/logic").json()["sections"]}
        self.assertIn("start_selection", sections)
        text = self.client.get("/v1/logic/start_selection").json()["parameters"]
        self.assertEqual(self.client.put("/v1/logic/start_selection",
                                         json={"parameters": text.replace("repeat_factor: 0.25", "repeat_factor: 0")}).status_code, 400)
        changed = text.replace("repeat_factor: 0.25", "repeat_factor: 0.5")
        self.assertEqual(self.client.put("/v1/logic/start_selection", json={"parameters": changed}).status_code, 200)
        saved = yaml.safe_load((self.dir / "hyperparameters.yaml").read_text())["start_selection"]
        self.assertEqual(saved["repeat_factor"], 0.5)


if __name__ == "__main__":
    unittest.main()
