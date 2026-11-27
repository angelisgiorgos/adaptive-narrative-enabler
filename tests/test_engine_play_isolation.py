"""A game with a saved engine starts in that engine's own context and changes nothing process-wide: the
studio's defaults (what the editors show, what a default game or a training starts with) change only by
the explicit operation (POST /v1/engines/{id}/activate, or ``activate`` on a play request).

Exercised at the actual API boundary: a uvicorn server in this process, called over HTTP from several
threads at once. Two distinct engine packages (.ane.zip files of two small worlds) are started,
played, saved and restored, interleaved with reads and an unsaved preview of the editor world, while a
default game goes on."""
try:  # recording and saves go to temporary folders
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import copy
import hashlib
import json
import os
import random
import shutil
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx
import yaml

import api
from backend import sessions, store
from core_engine import Genome
from core_engine.context import EngineContext
from core_engine.model import EngineModel, world_tags
from core_engine.variables import VariableStore
from utils.config_loader import config

FIXTURES = Path(__file__).resolve().parent / "fixtures"
STAIRS = "Take the stairs down to the press"
CELLAR = "Go down to the cellar"


def _room(tags, description, actions):
    return {"tags": tags, "descriptions": [description], "actions": actions}


CELLAR_HOUSE = {
    "world_definition": {
        "routes": {"Hall": ["Cellar"]},
        "locations": {
            "Hall": {**_room(["exploration", "quiet"], "A cold hall.", [
                {"name": CELLAR, "outcomes": [{"desc": "The steps are slick.", "move_to": "Cellar",
                                               "tags": ["exploration"]}]},
                {"name": "Light the hall lamp", "outcomes": [{"desc": "Shadows shrink.", "tags": ["discovery"]}]}]),
                "start_eligible": True, "start_weight": 1.0},
            "Cellar": _room(["secluded", "danger"], "Casks line the walls.", [
                {"name": "Count the casks", "outcomes": [{"desc": "One cask is empty.", "tags": ["discovery"]}]}]),
        },
        "characters": [{"name": "Cellarer", "associated_tags": ["secluded"], "descriptions": ["A wary cellarer."]}],
        "objects": [{"name": "Iron Key", "associated_tags": ["secluded"], "descriptions": ["A rusted key."]}],
    },
    "game_state": {"arrest_location": "Cellar"},
    "start_selection": {"default_location": "Hall"},
}


def _fake_vectors(tags, seed, size=16):
    rng = random.Random(seed)
    return {tag: [rng.uniform(-1, 1) for _ in range(size)] for tag in sorted(tags)}


def _engine(world, seed, name):
    cfg, errors, _ = store.complete_config(copy.deepcopy(world))
    assert not errors, errors
    return EngineModel(cfg, Genome(rng=random.Random(seed)).traits(), tag_vectors=_fake_vectors(world_tags(cfg), seed),
                       source=name)


def _authored(model):
    return {a["name"] for place in model.config["world_definition"]["locations"].values() for a in place.get("actions") or []}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class _LiveApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.clear_override()
        cls.folder = Path(tempfile.mkdtemp(prefix="ane-isolation-"))
        cls.env = patch.dict(os.environ, {"ANE_SAVE_DIR": str(cls.folder / "saves")})
        cls.env.start()
        cls.press = _engine(yaml.safe_load((FIXTURES / "salt_press.yaml").read_text(encoding="utf-8")), 31, "salt press")
        cls.house = _engine(CELLAR_HOUSE, 32, "cellar house")
        cls.paths = {"press": cls.press.save(cls.folder / "press.ane.zip"),
                     "house": cls.house.save(cls.folder / "house.ane.zip")}
        import uvicorn

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        cls.server = uvicorn.Server(uvicorn.Config(api.api, host="127.0.0.1", port=port, log_level="warning"))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.time() + 20
        while not cls.server.started and time.time() < deadline:
            time.sleep(0.05)
        cls.http = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"X-ANE-User": "isolation-tests"},
                                timeout=300)

    @classmethod
    def tearDownClass(cls):
        cls.http.close()
        cls.server.should_exit = True
        cls.thread.join(10)
        cls.env.stop()
        config.clear_override()
        shutil.rmtree(cls.folder, ignore_errors=True)

    def setUp(self):
        store.use_builtin_config()
        self.addCleanup(store.use_builtin_config)

    # ---- calls ------------------------------------------------------------------------------

    def ok(self, response):
        self.assertLess(response.status_code, 300, response.text)
        return response.json()

    def load(self, which, profile, **params):
        with open(self.paths[which], "rb") as stream:
            return self.ok(self.http.post("/v1/engines/load", files={"file": (f"{which}.ane.zip", stream.read())},
                                          params={"profile": profile, **params}))

    def act(self, session_id, action):
        return self.ok(self.http.post(f"/v1/sessions/{session_id}/actions", json={"action": action}))

    def revision(self):
        return self.ok(self.http.get("/v1/world/revision"))["revision"]

    def active_config(self):
        return self.ok(self.http.get("/v1/config"))

    def preview(self):
        draft = {"actions": [{"name": "Wave at the ships", "outcomes": [{"desc": "Gulls answer.", "tags": ["discovery"]}]}]}
        answer = self.ok(self.http.post("/v1/entities/Locations/Harbor/preview", json=draft))
        self.assertFalse(answer["saved"])
        return answer

    def reads(self):
        """What other authors do meanwhile: read the editor world and preview an unsaved draft."""
        return [lambda: self.revision(), lambda: self.active_config(), lambda: self.preview(),
                lambda: self.ok(self.http.get("/v1/world/catalogue"))]

    @staticmethod
    def together(*calls):
        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            futures = [pool.submit(call) for call in calls]
            return [future.result() for future in futures]

    # ---- checks -----------------------------------------------------------------------------

    def assert_started_in(self, game, model, other, start):
        self.assertEqual(game["engine"]["engine_id"], model.id)
        self.assertEqual(game["engine_id"], model.id)
        self.assertEqual(game["world_revision"], model.context().world_version)
        self.assertEqual(game["world"]["id"], model.world_id)
        self.assertFalse(game["world"]["current"])          # not the studio's world, and it did not become it
        self.assertIs(game["activated"], False)
        self.assertEqual(game["location"], start)
        self.assertTrue(set(game["actions"]) & _authored(model), game["actions"])  # its own world's choices
        self.assertFalse(set(game["actions"]) & _authored(other), game["actions"])
        with sessions.SESSION_LOCK:
            state = sessions.find_session(game["session_id"], check_owner=False)
        self.assertEqual(state.context.world_version, model.context().world_version)
        self.assertEqual(state.context.tag_space.key, model.context().tag_space.key)

    def assert_in_world(self, state, model, other):
        places = set(model.config["world_definition"]["locations"])
        self.assertIn(state["location"], places)
        self.assertFalse(set(state["actions"]) & _authored(other), state["actions"])


class InterleavedEnginesTests(_LiveApi):
    def test_two_engines_interleave_with_reads_previews_and_a_default_game(self):
        # A third, default game: the studio's defaults, started before anything else.
        default = self.ok(self.http.post("/v1/simulations", json={
            "candidate_count": 2, "generations": 1, "simulation_steps": 4, "episodes": 1, "deterministic": False,
            "warm_start": False}))
        with sessions.SESSION_LOCK:
            default_context = sessions.find_session(default["session_id"], check_owner=False).context.world_version
        before = {"revision": self.revision(), "config": _digest(self.active_config()),
                  "defaults": EngineContext.capture().world_version}
        self.assertEqual(default_context, before["defaults"])

        # 1. Both engines started at once (twice each), among reads and an unsaved preview.
        started = self.together(lambda: self.load("press", "ink"), lambda: self.load("house", "cellar"),
                                lambda: self.load("press", "ink"), lambda: self.load("house", "cellar"), *self.reads())
        press, house, press2, house2 = started[:4]
        for game in (press, press2):
            self.assert_started_in(game, self.press, self.house, "Typesetters Hall")
        for game in (house, house2):
            self.assert_started_in(game, self.house, self.press, "Hall")
        self.assertEqual(press["world_revision"], press2["world_revision"])
        self.assertNotEqual(press["world_revision"], house["world_revision"])

        # 2. Each moves downstairs while the default game and the readers go on.
        moved = self.together(lambda: self.act(press["session_id"], STAIRS), lambda: self.act(house["session_id"], CELLAR),
                              lambda: self.act(default["session_id"], default["actions"][0]), *self.reads())
        self.assertEqual(moved[0]["location"], "Rotary Room")
        self.assertEqual(moved[1]["location"], "Cellar")
        self.assert_in_world(moved[0], self.press, self.house)
        self.assert_in_world(moved[1], self.house, self.press)
        self.assertFalse(set(moved[2]["actions"]) & (_authored(self.press) | _authored(self.house)))

        # 3. Save both and continue the saves at once: exactly, each in its own engine's world.
        saves = self.together(lambda: self.ok(self.http.get(f"/v1/sessions/{press['session_id']}/savegame")),
                              lambda: self.ok(self.http.get(f"/v1/sessions/{house['session_id']}/savegame")))
        self.assertEqual([s["engine"]["engine_id"] for s in saves], [self.press.id, self.house.id])
        restored = self.together(*(lambda doc=doc: self.ok(self.http.post("/v1/sessions/load", json={"savegame": doc}))
                                   for doc in saves), *self.reads())
        self.assertEqual([(r["restored"], r["location"]) for r in restored[:2]],
                         [("exact", "Rotary Room"), ("exact", "Cellar")])
        for state, model in zip(restored[:2], (self.press, self.house)):
            with sessions.SESSION_LOCK:
                context = sessions.find_session(state["session_id"], check_owner=False).context
            self.assertEqual(context.world_version, model.context().world_version)

        # 4. The restored games and the originals go on, at once, each in its own world.
        after = self.together(lambda: self.act(restored[0]["session_id"], "Check the night's print run"),
                              lambda: self.act(restored[1]["session_id"], "Count the casks"),
                              lambda: self.act(press2["session_id"], STAIRS),
                              lambda: self.act(house2["session_id"], CELLAR), *self.reads())
        for state, (model, other) in zip(after[:4], [(self.press, self.house), (self.house, self.press)] * 2):
            self.assert_in_world(state, model, other)

        # Each profile's history holds its own engine's starts only.
        self.assertEqual(VariableStore().profile("ink")["starts"], ["Typesetters Hall", "Typesetters Hall"])
        self.assertEqual(VariableStore().profile("cellar")["starts"], ["Hall", "Hall"])

        # The editor's world, the studio's defaults and the default game's context did not move.
        self.assertEqual(self.revision(), before["revision"])
        self.assertEqual(_digest(self.active_config()), before["config"])
        self.assertFalse(self.active_config()["using_override"])
        self.assertEqual(EngineContext.capture().world_version, before["defaults"])
        with sessions.SESSION_LOCK:
            self.assertEqual(sessions.find_session(default["session_id"], check_owner=False).context.world_version,
                             default_context)
        nxt = self.act(default["session_id"], moved[2]["actions"][0])
        self.assertFalse(set(nxt["actions"]) & (_authored(self.press) | _authored(self.house)))


class ActivationTests(_LiveApi):
    def test_defaults_change_only_by_the_explicit_operation(self):
        revision = self.revision()
        self.load("press", "ink")                                    # recorded: playable by its id from here
        house = self.load("house", "cellar")
        self.assertEqual(self.revision(), revision)                 # isolated by default

        # Legacy callers: the same requests, no body or an empty one: isolated, and said so.
        for body in (None, {}):
            game = self.ok(self.http.post(f"/v1/records/engines/{self.press.id}/play", json=body))
            self.assertIs(game["activated"], False)
            self.assertEqual(game["world_revision"], self.press.context().world_version)
        self.assertEqual(self.revision(), revision)

        # The explicit operation: the press becomes the studio's defaults; the house game goes on in its own.
        activated = self.ok(self.http.post(f"/v1/engines/{self.press.id}/activate"))
        self.assertEqual((activated["engine_id"], activated["activated"]), (self.press.id, True))
        self.assertNotEqual(activated["revision"], revision)
        self.assertEqual(activated["revision"], self.revision())
        active = self.active_config()["config"]["world_definition"]
        self.assertEqual(set(active["locations"]), set(self.press.config["world_definition"]["locations"]))
        self.assert_in_world(self.act(house["session_id"], CELLAR), self.house, self.press)
        self.assertEqual(self.http.post(f"/v1/engines/{'0' * 32}/activate").status_code, 404)

        # Opting in on a play request (the earlier behaviour) does the same.
        store.use_builtin_config()
        game = self.load("house", "cellar", activate="true")
        self.assertIs(game["activated"], True)
        self.assertTrue(game["world"]["current"])                   # the studio's world now
        self.assertEqual(set(self.active_config()["config"]["world_definition"]["locations"]), {"Hall", "Cellar"})
        store.use_builtin_config()
        game = self.ok(self.http.post(f"/v1/records/engines/{self.press.id}/play", json={"activate": True}))
        self.assertIs(game["activated"], True)
        self.assertIn("Typesetters Hall", self.active_config()["config"]["world_definition"]["locations"])

    def test_an_older_bundle_plays_in_its_own_context(self):
        revision = self.revision()
        bundle = {**self.house.config, "generated_run": {"genome": dict(self.house.genome_traits), "best_score": 1.0}}
        game = self.ok(self.http.post("/v1/engines/load", files={
            "file": ("house.yaml", yaml.safe_dump(bundle, sort_keys=False).encode())}, params={"profile": "bundle"}))
        self.assertEqual(game["location"], "Hall")
        self.assertIs(game["activated"], False)
        self.assertEqual(self.revision(), revision)
        self.assertFalse(self.active_config()["using_override"])
        self.assert_in_world(self.act(game["session_id"], CELLAR), self.house, self.press)

    def test_own_stat_points_are_checked_against_the_engine_s_stats(self):
        own = self.press.config["world_definition"].get("character_stats") or {}
        points = dict((own.get("defined_points") or {}))
        if not points:
            self.skipTest("the engine's world defines no stat points")
        self.load("press", "ink")
        game = self.ok(self.http.post(f"/v1/records/engines/{self.press.id}/play", json={"stats": points}))
        self.assertEqual(game["engine_id"], self.press.id)


if __name__ == "__main__":
    unittest.main()
