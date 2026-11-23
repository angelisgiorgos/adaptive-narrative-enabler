"""Exporting a trained engine and running it offline, as a game would."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

import api
import main as engine
from backend import export, records, sessions
from core_engine import GameState, Genome, version
from core_engine.builder import build_world
from core_engine.model import EngineModel

PYTHON = sys.executable


def _run_offline(root, code, timeout=300):
    """Run ``code`` inside the export folder, with nothing from this repository on the path."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run([PYTHON, "-c", code], cwd=root, env=env, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise AssertionError(result.stderr[-3000:])
    return result.stdout


def _inside(parent, block):
    """Whether ``block`` is inside ``parent`` in the layout."""
    node = getattr(block, "parent", None)
    while node is not None:
        if node is parent:
            return True
        node = getattr(node, "parent", None)
    return False


class _Exports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.env = patch.dict(os.environ, {"ANE_SAVE_DIR": str(cls.dir / "saves")})
        cls.env.start()
        # Short stories and a one-story benchmark for the runs these tests export (simulation_steps does
        # not shorten full-length stories): what is exported does not depend on a story's length.
        world = engine.config.as_dict()
        world["evolution"].update({"full_length_stories": False, "cover_situations": False, "validation_episodes": 2})
        engine.config.use_override(world)
        cls.addClassCleanup(engine.config.clear_override)
        cls.game = sessions.run_simulation(5, 3, 5, True, episodes=1)

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.tmp.cleanup()

    def _unzip(self, result):
        target = Path(tempfile.mkdtemp(dir=self.dir))
        with zipfile.ZipFile(result["path"]) as archive:
            archive.extractall(target)
        return next(target.iterdir())


class ExportContentTests(_Exports):
    def test_sources(self):
        best = export.export_engine("run", run_id=self.game["run_id"])["manifest"]
        self.assertEqual(best["source"], "best of the run")
        self.assertEqual(best["genome"], self.game["learning"]["best_genome"])
        first = export.export_engine("run", run_id=self.game["run_id"], generation=0)["manifest"]
        self.assertEqual(first["source"], "champion of generation 0")
        self.assertEqual(len(first["learning"]["history"]), 1)
        self.assertEqual(export.export_engine("memory")["manifest"]["source"], "best remembered storyteller")
        from_game = export.export_engine("game", token=self.game["token"])["manifest"]
        self.assertEqual(from_game["genome"], sessions.SESSIONS[self.game["token"]].genome.traits())
        for kwargs in ({"source": "run", "run_id": self.game["run_id"], "generation": 99},
                       {"source": "game", "token": "nope"}, {"source": "cloud"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(Exception):
                export.export_engine(**kwargs)
        with patch.dict(sessions.RUNS, {}, clear=True), self.assertRaises(Exception):
            export.export_engine("run")

    def test_package_contents(self):
        result = export.export_engine("run", run_id=self.game["run_id"], name="my game engine!")
        with zipfile.ZipFile(result["path"]) as archive:
            names = set(archive.namelist())
            vectors = json.loads(archive.read("my-game-engine/data/tag_vectors.json"))
        self.assertEqual(result["manifest"]["name"], "my-game-engine")
        for required in ("engine.json", "config/engine.yaml", "data/tag_vectors.json", "saves/evolution_memory.json",
                         "core_engine/game_state.py", "core_engine/builder.py", "core_engine/evolution.py",
                         "narrative_runtime/engine.py", "narrative_runtime/server.py", "utils/tag_similarity.py",
                         "run.py", "README.md", "requirements.txt"):
            self.assertIn(f"my-game-engine/{required}", names)
        for studio_only in ("app.py", "api.py", "main.py", "backend/", "core_engine/augmenter.py", "evaluator/"):
            self.assertFalse(any(name.startswith(f"my-game-engine/{studio_only}") for name in names), studio_only)
        from utils.tag_similarity import _normalize_tag_text
        expected = {_normalize_tag_text(tag) for tag in export.world_tags(engine.config.as_dict())}
        self.assertEqual(expected - set(vectors), set())  # every world tag has its training vector
        without = export.export_engine("run", run_id=self.game["run_id"], include_memory=False)
        with zipfile.ZipFile(without["path"]) as archive:
            self.assertFalse(any(n.endswith("evolution_memory.json") for n in archive.namelist()))


class OfflineRuntimeTests(_Exports):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        result = export.export_engine("run", run_id=cls.game["run_id"])
        cls.genome = Genome.from_traits(result["manifest"]["genome"])
        cls.root = cls._unzip(cls, result)

    def test_plays_offline_without_the_studio_or_ml_libraries(self):
        out = _run_offline(self.root, """
import json, sys
from narrative_runtime import NarrativeEngine
engine = NarrativeEngine()
game = engine.new_game(seed=11, persist=False)
view = game.view()
while not view["ended"] and view["turn_number"] < 100:
    view = game.choose(0)
saved = engine.new_game(profile="p1", seed=12)
saved.choose(0)
again = engine.load_game(saved.save("slot.game"))
print(json.dumps({
    "transcript": game.transcript(), "ended": view["ended"],
    "resumed": again.transcript() == saved.transcript() and again.actions() == saved.actions(),
    "loaded": sorted(m for m in ("torch", "sentence_transformers", "sklearn", "gradio", "fastapi", "main", "app", "backend") if m in sys.modules),
    "from_export": __import__("core_engine").__file__.startswith(__import__("os").getcwd()),
}))
""")
        data = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(data["ended"])
        self.assertTrue(data["resumed"])
        self.assertEqual(data["loaded"], [])
        self.assertTrue(data["from_export"])
        state = GameState(build_world(self.genome), self.genome, rng=random.Random(11))
        while not state.ended and state.actions_taken < 100:  # the first choice a player sees, as choose(0)
            options = state.player_options()
            if not options or not state.take_option(options[0]):
                break
        self.assertEqual(data["transcript"], state.get_full_story())  # identical behaviour offline

    def test_http_server_for_game_engines(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["ANE_RUNTIME_TOKEN"] = "game-token"  # every request needs it (Authorization: Bearer)
        server = subprocess.Popen([PYTHON, "run.py", "serve", "--port", str(port)], cwd=self.root, env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(server.kill)
        base = f"http://127.0.0.1:{port}"

        def call(path, body=None, method=None):
            data = json.dumps(body).encode() if body is not None else None
            request = urllib.request.Request(base + path, data=data, method=method or ("POST" if data else "GET"),
                                             headers={"Content-Type": "application/json",
                                                      "Authorization": "Bearer game-token"})
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read())

        for _ in range(60):
            try:
                if call("/engine")[0] == 200:
                    break
            except OSError:
                time.sleep(0.5)
        status, view = call("/games", {"profile": "unity", "stats_mode": "balanced", "seed": 3})
        self.assertEqual(status, 201)
        self.assertTrue(view["actions"])
        status, turn = call(f"/games/{view['game_id']}/choose", {"action": 0})
        self.assertEqual((status, turn["turn_number"]), (200, 1))
        self.assertIn(f"> {view['actions'][0]}", turn["turn"])  # an unexpected event may come first
        self.assertEqual(call(f"/games/{view['game_id']}/choose", {"action": "fly away"})[0], 400)
        self.assertEqual(call("/games/nope")[0], 404)
        self.assertIn("transcript", call(f"/games/{view['game_id']}/transcript")[1])
        self.assertEqual(call(f"/games/{view['game_id']}", method="DELETE")[0], 200)
        status, chosen = call("/games", {"start": "Tavern", "persist": False})
        self.assertEqual((status, chosen["location"], chosen["start_location"]), (201, "Tavern", "Tavern"))
        self.assertEqual(call("/games", {"start": "Dungeon", "persist": False})[0], 400)  # not a starting location

    def test_starting_locations_offline(self):
        out = _run_offline(self.root, """
import json
from narrative_runtime import NarrativeEngine
engine = NarrativeEngine()
chosen = engine.new_game(start="Market", persist=False).view()
drawn = sorted({engine.new_game(seed=seed, persist=False).view()["start_location"] for seed in range(40)})
try:
    engine.new_game(start="Vault", persist=False)
    refused = False
except ValueError:
    refused = True
played = [engine.new_game(profile="returning", seed=seed).state.start_location for seed in range(3)]
print(json.dumps({"listed": engine.info()["starting_locations"], "chosen": [chosen["location"], chosen["start_location"]],
                  "drawn": drawn, "refused": refused, "played": played, "history": engine.store.recent_starts("returning")}))
""")
        data = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(data["listed"], ["Harbor", "Market", "Tavern", "Festival of Masks"])
        self.assertEqual(data["chosen"], ["Market", "Market"])
        self.assertTrue(set(data["drawn"]) <= set(data["listed"]) and len(data["drawn"]) > 1, data["drawn"])
        self.assertTrue(data["refused"])  # the Vault is not a starting location
        self.assertEqual(data["history"], data["played"])  # the profile remembers where its stories began

    def test_keeps_training_offline(self):
        # A copy: adopting a better storyteller rewrites engine.json, which the other tests play with.
        root = Path(tempfile.mkdtemp(dir=self.dir)) / self.root.name
        shutil.copytree(self.root, root)
        out = _run_offline(root, """
from narrative_runtime import NarrativeEngine
summary = NarrativeEngine().evolve(generations=2, pop_size=4, episodes=1, max_steps=5, seed=1)
print(summary["warm_started_from_memory"], summary["adopted"], summary["new_storyteller_score"] >= 0)
""")
        warm, adopted, scored = out.split()[-3:]
        self.assertGreater(int(warm), 0)  # continued from the shipped learning memory
        self.assertEqual(scored, "True")


class ExportEndpointsTests(_Exports):
    def test_api(self):
        client = TestClient(api.api, headers={"X-ANE-User": "api-tests"})  # a backend calling the API, for its user
        runs = client.get("/v1/runs").json()["runs"]
        self.assertIn(self.game["run_id"], [run["run_id"] for run in runs])
        response = client.post("/v1/exports", json={"source": "run", "run_id": self.game["run_id"], "generation": 1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["source"], "champion of generation 1")
        download = client.get(response.json()["download_url"])
        self.assertEqual(download.status_code, 200)
        archive = zipfile.ZipFile(__import__("io").BytesIO(download.content))
        self.assertTrue(any(name.endswith("engine.json") for name in archive.namelist()))
        self.assertEqual(client.post("/v1/exports", json={"source": "game", "session_id": "nope"}).status_code, 400)
        simulated = client.post("/v1/simulations", json={"candidate_count": 3, "generations": 1, "simulation_steps": 3,
                                                         "episodes": 1, "deterministic": False}).json()
        self.assertIn(simulated["run_id"], [run["run_id"] for run in client.get("/v1/runs").json()["runs"]])

    def test_studio(self):
        import app
        paths, status = app.export_engine_ui("A generation of this run", self.game["run_id"], "2", None, True, "")
        self.assertTrue(zipfile.is_zipfile(paths[0]))
        self.assertIn("champion of generation 2", status)
        path, status = app.export_engine_ui("This game's storyteller", None, None, self.game["token"], False, "mine")
        self.assertIn("storyteller of the current game", status)

    def test_the_export_steps_refuse_invalid_combinations_and_say_how_to_resolve_them(self):
        import gradio as gr

        import app

        listed = app.export_platform_list()
        python = next(p for p in listed if p["platform"] == "python")
        self.assertTrue(python["available"])
        missing = [p for p in listed if not p["available"]]
        for entry in missing:  # each unavailable system says why (the API's reason) in its label and note
            with self.subTest(platform=entry["platform"]):
                self.assertIn("(not available here)", dict((v, l) for l, v in app.export_platform_choices(listed))[
                    entry["platform"]])
                self.assertIn(entry["reason"], app.export_platform_note(entry["platform"], listed))
        # The ANE AI from a storyteller without an engine: refused, with both ways out.
        problems = app.export_problems("linux-x64", "This game's storyteller", token=self.game["token"], listed=[
            python, {"platform": "linux-x64", "available": True}])
        self.assertEqual(len(problems), 1)
        self.assertIn("Choose *finished runs* in step 1, or *Python* in step 2", problems[0][1])
        # Python from each source: what is missing is named.
        self.assertIn("No game is open", app.export_problems("python", "This game's storyteller")[0][0])
        self.assertIn("No run was trained", app.export_problems("python", app.GENERATION_SOURCE)[0][0])
        self.assertIn("No generation is chosen",
                      app.export_problems("python", app.GENERATION_SOURCE, run_id=self.game["run_id"])[0][0])
        self.assertEqual(app.export_problems("python", app.GENERATION_SOURCE, generation="1",
                                             run_id=self.game["run_id"]), [])
        self.assertIn("No finished run is chosen", app.export_problems("python", app.FINISHED_RUNS, [])[0][0])
        self.assertIn("no longer offered", app.export_problems("python", app.FINISHED_RUNS, ["0" * 32])[0][0])
        self.assertEqual(app.export_problems("python", app.FINISHED_RUNS, [self.game["engine_id"]]), [])
        # The form: Export disabled for an invalid combination, with the summary saying why and how.
        form = app.export_form_ui("python", "This game's storyteller")
        self.assertFalse(form[4].interactive)
        self.assertIn("⛔", form[-1])
        self.assertFalse(form[2].visible)                       # finished runs: only for that source
        form = app.export_form_ui("python", "This game's storyteller", token=self.game["token"])
        self.assertTrue(form[4].interactive)
        self.assertIn("✅ **Ready:** 1 download for Python", form[-1])
        button, options, check = app.export_check_ui("python", app.FINISHED_RUNS, [], None, None, None, "")
        self.assertFalse(button.interactive)
        self.assertIn("Choose one or more finished runs", check)
        _, options, _ = app.export_check_ui("python", app.FINISHED_RUNS, ["a", "b"], name="pack")
        self.assertIn("`pack-<engine id>`", options)
        # A refused export asks nothing of the API.
        with self.assertRaises(gr.Error) as refused:
            app.export_engine_ui("This game's storyteller", None, None, None, True, "", "python")
        self.assertIn("No game is open", str(refused.exception))
        # Usage after the download, per platform.
        self.assertIn("python run.py play", app.export_usage_markdown("python"))
        self.assertIn("Import Package", app.export_usage_markdown("unity"))
        self.assertIn("ane-ai.exe serve", app.export_usage_markdown("windows-x64"))
        self.assertIn("com.apple.quarantine", app.export_usage_markdown("macos-arm64"))

    def test_the_export_destination_is_laid_out_in_steps_and_wired(self):
        import gradio as gr

        import app

        demo = app.build_app()
        by = {b.elem_id: b for b in demo.blocks.values() if getattr(b, "elem_id", None)}
        export = by["dest-export"]
        headings = [b.value for b in demo.blocks.values() if isinstance(b, gr.Markdown) and str(b.value).startswith(
            "#### ") and _inside(export, b)]
        self.assertEqual([h.split(" · ")[0] for h in headings], ["#### 1", "#### 2", "#### 3", "#### 4", "#### 5"])

        def listeners(component, event):
            return [fn for fn in demo.fns.values() if (component._id, event) in fn.targets]

        for elem, event, name in (("export-platform", "input", "export_form_ui"), ("export-source", "input",
                                  "export_form_ui"), ("export-engines", "change", "export_check_ui"),
                                  ("export-name", "input", "export_check_ui")):
            with self.subTest(elem=elem):
                self.assertIn(name, [fn.fn.__name__ for fn in listeners(by[elem], event)])
        # Nothing re-lays the form when a value is set by code (Export this result's chosen engine stays).
        self.assertEqual(listeners(by["export-platform"], "change"), [])
        self.assertEqual(listeners(by["export-source"], "change"), [])


    def test_the_ane_ai_is_made_from_a_complete_engine_only(self):
        runtimes = Path(tempfile.mkdtemp(dir=self.dir))
        with zipfile.ZipFile(runtimes / "ane-runtime-linux-x64.zip", "w") as zipped:  # a stand-in runtime
            zipped.writestr("runtime.json", json.dumps({"format": "ane.runtime/1", "target": "linux-x64",
                                                       "code_version": version.code_version(), "protocol": 1,
                                                       "executable": "ane-ai"}))
            zipped.writestr("ane-ai", "#!/bin/sh\n")
        client = TestClient(api.api, headers={"X-ANE-User": "api-tests"})  # a backend calling the API, for its user
        engine_id = self.game["engine_id"]
        with patch.dict(os.environ, {"ANE_RUNTIMES_DIR": str(runtimes)}):
            listed = client.get("/v1/exports/engines").json()["engines"]
            self.assertIn(engine_id, [e["engine_id"] for e in listed])  # the finished run's engine
            refused = [{"source": "memory"}, {"source": "game", "session_id": self.game["token"]},
                       {"source": "run", "run_id": self.game["run_id"], "generation": 1}]
            if records.enabled():  # a generation's champion is recorded, but it is no complete engine
                champion = next(e for e in records.list_engines(run_id=self.game["run_id"])
                                if e["source"] == "generation_champion")
                refused.append({"source": "engine", "engine_id": champion["id"]})
            for body in refused:
                with self.subTest(body=body):
                    response = client.post("/v1/exports", json={**body, "platform": "linux-x64"})
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertIn("complete engine", response.json()["detail"])

            for body in ({"source": "run", "run_id": self.game["run_id"]}, {"source": "engine", "engine_id": engine_id}):
                with self.subTest(body=body):
                    response = client.post("/v1/exports", json={**body, "platform": "linux-x64", "name": "ane-ai"})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["engine_id"], engine_id)
                    with zipfile.ZipFile(__import__("io").BytesIO(client.get(response.json()["download_url"]).content)) as zipped:
                        manifest = json.loads(zipped.read("ane-ai/engine.json"))
                        exported_config = yaml.safe_load(zipped.read("ane-ai/config/engine.yaml"))
                        vectors = json.loads(zipped.read("ane-ai/data/tag_vectors.json"))
                        names = set(zipped.namelist())
                    model = EngineModel.load(sessions.ENGINES[engine_id]["model_path"])
                    # The engine itself: its storyteller, world, tag vectors and training state.
                    self.assertEqual(manifest["genome"], model.genome.traits())
                    self.assertEqual(exported_config, model.config)
                    self.assertEqual(vectors, model.tag_vectors)
                    self.assertTrue(manifest["resumable"] and "ane-ai/data/training.json" in names)
                    self.assertIn("ane-ai/ane-ai", names)

        # The Python version is still made from any storyteller.
        python = client.post("/v1/exports", json={"source": "run", "run_id": self.game["run_id"], "generation": 1})
        self.assertEqual(python.status_code, 200, python.text)

    def test_the_studio_exports_the_finished_runs(self):
        import app

        second = sessions.run_simulation(3, 1, 3, True, episodes=1)  # another finished run, the latest
        ids = [engine for _, engine in app.finished_run_choices()]
        self.assertEqual(ids[0], second["engine_id"])  # newest first
        self.assertIn(self.game["engine_id"], ids)
        _, generation, chosen, note, button, platform_note, _, check = app.export_form_ui("linux-x64")
        self.assertEqual(chosen.value, [second["engine_id"]])        # the newest finished run
        self.assertFalse(generation.visible)
        self.assertFalse(button.interactive)                          # no linux-x64 runtime here: explained
        self.assertIn("not available here", platform_note)
        self.assertIn("**To resolve:**", check)

        runtimes = Path(tempfile.mkdtemp(dir=self.dir))
        with zipfile.ZipFile(runtimes / "ane-runtime-linux-x64.zip", "w") as zipped:
            zipped.writestr("runtime.json", json.dumps({"format": "ane.runtime/1", "target": "linux-x64",
                                                       "code_version": version.code_version(), "protocol": 1,
                                                       "executable": "ane-ai"}))
            zipped.writestr("ane-ai", "#!/bin/sh\n")
        both = [self.game["engine_id"], second["engine_id"]]
        with patch.dict(os.environ, {"ANE_RUNTIMES_DIR": str(runtimes)}):
            form = app.export_form_ui("linux-x64", app.FINISHED_RUNS, both)
            self.assertTrue(form[4].interactive)                      # available now, two runs chosen
            self.assertEqual(form[2].value, both)
            self.assertIn("2 downloads", form[-1])
            files, status = app.export_engine_ui(app.FINISHED_RUNS, None, None, None, False, "both", "linux-x64", both)
        self.assertEqual(len(files), 2)  # one download per finished run
        exported = set()
        for path in files:
            with zipfile.ZipFile(path) as zipped:
                manifest = json.loads(next(zipped.read(n) for n in zipped.namelist() if n.endswith("/engine.json")))
            exported.add(manifest["engine_id"])
        self.assertEqual(exported, set(both))
        files, status = app.export_engine_ui(app.FINISHED_RUNS, None, None, None, False, "", "python", second["engine_id"])
        self.assertEqual(len(files), 1)
        self.assertIn(second["engine_id"][:8], status)

    def test_terminal_run_exports_automatically(self):
        run_dir = Path(tempfile.mkdtemp(dir=self.dir))
        target = engine.export_offline_engine(Genome(random.Random(1)), 12.5, str(run_dir), "20260101_000000")
        self.assertTrue(zipfile.is_zipfile(target))
        self.assertEqual(Path(target).name, "offline_engine.zip")


if __name__ == "__main__":
    unittest.main()
