"""A world's map (``world_definition.routes``) is the author's: an upload is checked against its own
places (core_engine.builder.route_errors, as the runtime reads the map), never against the built-in
city's (Harbor, Market, ...). A route holds both ways; ``send_to`` stays the one-way jump.

Salt Press (tests/fixtures/salt_press.yaml) is a reconstruction of the reported world: Typesetters Hall
and Rotary Room are declared neighbours, the press floor downstairs."""
try:  # recording and downloads go to temporary folders, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

import api
from backend import sessions, store
from core_engine.builder import route_errors, route_map
from utils.config_loader import config

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "salt_press.yaml"
CITY = ("Harbor", "Market", "Alley", "Palace", "Dungeon")
STAIRS = "Take the stairs down to the press"
CHUTE = "Slide down the paper chute"


def _salt_press():
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


def _one_way():
    """Salt Press with a bindery reached only by the paper chute (``send_to``): no route leads back."""
    world = _salt_press()
    locations = world["world_definition"]["locations"]
    locations["Rotary Room"]["actions"].append({"name": CHUTE, "outcomes": [
        {"desc": "You drop through the chute onto a heap of spoiled sheets.", "send_to": "Bindery", "tags": ["danger"]}]})
    locations["Bindery"] = {"tags": ["secluded", "workshop"], "descriptions": ["Folded signatures wait under the presses."],
                            "actions": [{"name": "Stitch a signature", "outcomes": [
                                {"desc": "The thread holds.", "tags": ["discovery"]}]}]}
    return world


def _reachable(routes, start):
    seen, todo = {start}, [start]
    while todo:
        for other in (routes or {}).get(todo.pop(), []):
            if other not in seen:
                seen.add(other)
                todo.append(other)
    return seen


def _validate(world):
    """The errors an upload of ``world`` gets (the store's own steps, without making it active)."""
    merged, errors, _ = store.complete_config(copy.deepcopy(world))
    return errors + store._validate_complete_config(merged, config.default_config())


class RouteValidationTests(unittest.TestCase):
    def test_salt_press_routes_are_checked_against_its_own_rooms(self):
        self.assertEqual(_validate(_salt_press()), [])
        self.assertEqual(route_errors(_salt_press()["world_definition"]), [])  # what the core accepts

    def test_an_unknown_destination_names_its_source_and_the_missing_room(self):
        world = _salt_press()
        world["world_definition"]["routes"]["Typesetters Hall"].append("Composing Loft")
        errors = _validate(world)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("'Typesetters Hall'", errors[0])
        self.assertIn("'Composing Loft'", errors[0])
        self.assertIn("not a location of this world", errors[0])
        self.assertFalse(any(place in "\n".join(errors) for place in CITY))   # nothing about the bundled city

    def test_an_unknown_source_and_a_wrong_shape_are_refused(self):
        world = _salt_press()
        world["world_definition"]["routes"]["Composing Loft"] = ["Typesetters Hall"]
        world["world_definition"]["routes"]["Rotary Room"] = "Loading Dock"
        errors = "\n".join(_validate(world))
        self.assertIn("'Composing Loft' is not a location of this world", errors)
        self.assertIn("routes.Rotary Room must be a list of locations", errors)
        world["world_definition"]["routes"] = ["Typesetters Hall"]
        self.assertIn("`world_definition.routes` must map each place", "\n".join(_validate(world)))

    def test_omitted_empty_and_null_maps(self):
        for routes in ("omitted", {}, None):
            with self.subTest(routes=routes):
                world = _salt_press()
                if routes == "omitted":
                    world["world_definition"].pop("routes")
                else:
                    world["world_definition"]["routes"] = routes
                self.assertEqual(_validate(world), [])
                merged, _, _ = store.complete_config(world)
                wd = merged["world_definition"]
                if routes == "omitted":
                    self.assertNotIn("routes", wd)            # never filled with the city's map
                else:
                    self.assertEqual(wd["routes"], routes)     # kept as written
                self.assertIsNone(route_map(wd))               # no authored map: every story draws its own

    def test_the_shipped_city_still_validates_and_its_errors_name_both_ends(self):
        city = config.default_config()
        self.assertEqual(_validate(city), [])
        self.assertEqual(set(route_map(city["world_definition"])), set(city["world_definition"]["locations"]))
        city["world_definition"]["routes"]["Tavern"] = ["Harbor", "Atlantis"]
        errors = _validate(city)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("from 'Tavern' to 'Atlantis'", errors[0])

    def test_routes_hold_both_ways(self):
        routes = route_map(_salt_press()["world_definition"])
        self.assertEqual(routes["Rotary Room"], ["Loading Dock", "Typesetters Hall"])  # written on the hall's side
        self.assertEqual(routes["Loading Dock"], ["Rotary Room"])


class _Api(unittest.TestCase):
    def setUp(self):
        self.addCleanup(store.use_builtin_config)
        self.client = TestClient(api.api, headers={"X-ANE-User": "api-tests"})

    def upload(self, world, name="salt_press.yaml"):
        return self.client.post("/v1/config/upload", files={"file": (name, yaml.safe_dump(world, sort_keys=False).encode())})

    def train(self):
        game = self.client.post("/v1/simulations", json={"candidate_count": 2, "generations": 1, "simulation_steps": 4,
                                                         "episodes": 1, "deterministic": True, "seed": 7,
                                                         "warm_start": False})
        self.assertEqual(game.status_code, 200, game.text)
        return game.json()

    def act(self, session_id, action):
        response = self.client.post(f"/v1/sessions/{session_id}/actions", json={"action": action})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class RouteUploadTests(_Api):
    def test_the_original_file_uploads_unchanged(self):
        response = self.client.post("/v1/config/upload", files={"file": ("salt_press.yaml", FIXTURE.read_bytes())})
        self.assertEqual(response.status_code, 200, response.text)
        active = self.client.get("/v1/config").json()["config"]["world_definition"]
        self.assertEqual(active["routes"], _salt_press()["world_definition"]["routes"])

    def test_an_unknown_destination_is_a_400_that_names_both_ends(self):
        before = self.client.get("/v1/world/revision").json()["revision"]
        world = _salt_press()
        world["world_definition"]["routes"]["Rotary Room"].append("Composing Loft")
        response = self.upload(world)
        self.assertEqual(response.status_code, 400)
        detail = response.json()["detail"]
        self.assertIn("from 'Rotary Room' to 'Composing Loft'", detail)
        self.assertFalse(any(f"routes.{place}`" in detail for place in CITY), detail)
        self.assertEqual(self.client.get("/v1/world/revision").json()["revision"], before)

    def test_the_shipped_city_uploads(self):
        response = self.upload(config.default_config(), "city.yaml")
        self.assertEqual(response.status_code, 200, response.text)

    def test_a_one_way_chute_has_no_route_back(self):
        world = _one_way()
        self.assertEqual(_validate(world), [])
        self.assertNotIn("Bindery", route_map(world["world_definition"]))   # on no route
        self.assertEqual(self.upload(world).status_code, 200)
        game = self.train()
        self.assertEqual(game["location"], "Typesetters Hall")
        self.assertEqual(self.act(game["session_id"], STAIRS)["location"], "Rotary Room")
        state = self.act(game["session_id"], CHUTE)
        self.assertEqual(state["location"], "Bindery")
        with sessions.SESSION_LOCK:
            locations = sessions.find_session(game["session_id"], check_owner=False).world.locations
        self.assertIn("Bindery", locations["Rotary Room"].connected)
        self.assertNotIn("Rotary Room", locations["Bindery"].connected)     # one way: no route back


OFFLINE = """
import json
from narrative_runtime import NarrativeEngine
from core_engine.builder import route_map
from utils.config_loader import config

engine = NarrativeEngine.load(".")
game = engine.new_game(start="Typesetters Hall", seed=3, persist=False)
before = game.view()["location"]
game.choose(%r)
print(json.dumps({"routes": route_map(config.get("world_definition")), "before": before,
                  "after": game.view()["location"], "name": engine.manifest["name"]}))
""" % STAIRS


class SaltPressEndToEndTests(_Api):
    """Upload Salt Press unchanged, train, move downstairs, name (publish) and export the engine, then
    replay from the export offline and compare the rooms reachable there with the studio's."""

    def test_upload_train_move_publish_export_replay(self):
        response = self.client.post("/v1/config/upload", files={"file": ("salt_press.yaml", FIXTURE.read_bytes())})
        self.assertEqual(response.status_code, 200, response.text)
        game = self.train()
        self.assertEqual(game["location"], "Typesetters Hall")
        self.assertEqual(self.act(game["session_id"], STAIRS)["location"], "Rotary Room")  # downstairs

        named = self.client.put(f"/v1/engines/{game['engine_id']}/name", json={"name": "Salt Press"})
        self.assertEqual(named.status_code, 200, named.text)
        exported = self.client.post("/v1/exports", json={"source": "engine", "engine_id": game["engine_id"]})
        self.assertEqual(exported.status_code, 200, exported.text)
        download = self.client.get(exported.json()["download_url"])
        self.assertEqual(download.status_code, 200)

        studio_routes = route_map(self.client.get("/v1/config").json()["config"]["world_definition"])
        with tempfile.TemporaryDirectory() as directory:
            with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
                archive.extractall(directory)
            (root,) = [p for p in Path(directory).iterdir() if (p / "engine.json").exists()]
            self.assertEqual(json.loads((root / "engine.json").read_text())["world"]["locations"], 3)
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}   # nothing from this repository
            result = subprocess.run([sys.executable, "-c", OFFLINE], cwd=root, env=env, capture_output=True,
                                    text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        replay = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(replay["name"], "Salt-Press")
        self.assertEqual((replay["before"], replay["after"]), ("Typesetters Hall", "Rotary Room"))
        self.assertEqual(replay["routes"], studio_routes)
        expected = {"Typesetters Hall", "Rotary Room", "Loading Dock"}
        self.assertEqual(_reachable(studio_routes, "Typesetters Hall"), expected)
        self.assertEqual(_reachable(replay["routes"], "Typesetters Hall"), expected)


if __name__ == "__main__":
    unittest.main()
