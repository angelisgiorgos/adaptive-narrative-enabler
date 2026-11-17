"""The ANE AI a UNP launches, drives and stops (narrative_runtime/, core_engine/resume.py,
savegame.py, content_rules.py; packaging/; backend/export.py platforms):

- a session rebuilt from the UNP's parameters stands exactly at them, quietly (nothing told or
  paid twice); the UNP sets the player and reports what happened in its game;
- save games: exact, with the UNP's changes applied; rebuilt when the snapshot is missing,
  damaged or made by another engine; an ended session is not rebuilt;
- world content from the UNP is checked like the studio checks it;
- the catalogue holds every entity and field;
- the server only serves local, authorised JSON requests;
- the whole protocol end to end, with Python (packaging/smoke_test.py), like CI does with the
  standalone build;
- exports by platform: a standalone runtime is used only when its code version is the API's; the
  Unity export is a .unitypackage with the C# library, the engine package and those runtimes;
- the command line (info, play, evolve, serve and its options), as a UNP or a person uses it;
- the Unity library agrees with the ANE AI: its routes, request fields, platforms, launch
  arguments, and the package it ships runs.
"""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import copy
import http.client
import io
import json
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import main as engine
from backend import export
from core_engine import GameState, Genome, content_rules, resume, savegame, version
from core_engine.model import EngineModel
from core_engine.variables import VariableStore
from narrative_runtime.world import PatchFormatError, apply_patch, patch_errors, world_catalogue
from utils.config_loader import config

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def _play(state, policy, turns):
    for _ in range(turns):
        if state.ended or not state.ranked_available_actions():
            return
        state.step(policy.randrange(len(state.ranked_available_actions())))


class _Runtime(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="ane-unp-"))
        patcher = patch.dict(os.environ, {"ANE_SAVE_DIR": str(self.folder)})
        patcher.start()
        self.addCleanup(patcher.stop)
        config.reload()
        engine.refresh_runtime_settings()
        self.store = VariableStore(str(self.folder / "variables.json"))
        self.genome = Genome(rng=random.Random(4))

    def _session(self, turns=10, seed=4):
        state = GameState(engine.build_world(self.genome), self.genome, rng=random.Random(seed), store=self.store,
                          profile="unp")
        _play(state, random.Random(seed), turns)
        state.ranked_available_actions()
        return state


class ResumeTests(_Runtime):
    def test_a_rebuilt_session_stands_at_the_parameters_quietly(self):
        state = self._session(12)
        params = resume.parameters(state)
        params["inventory"] = sorted(set(params["inventory"]) | {"Rusty Sword"})
        params["coins"], params["health"] = 23, 4
        rebuilt = resume.rebuild(self.genome, params, store=self.store, profile="unp")
        again = resume.parameters(rebuilt)
        for field in ("current_location", "location_counts", "turns", "coins", "health", "max_health", "stat_points",
                      "inventory", "variables", "removed_npcs", "allies", "alert", "used_items"):
            with self.subTest(field=field):
                self.assertEqual(again[field], params[field])
        self.assertEqual(again["mission"]["key"], params["mission"]["key"])
        self.assertEqual(again["mission"]["stage"], params["mission"]["stage"])
        self.assertEqual([u["action"] for u in again["unique_actions"]], [u["action"] for u in params["unique_actions"]])
        self.assertEqual({p["name"] for p in again["people_met"]}, {p["name"] for p in params["people_met"]})
        told = "\n".join(rebuilt.history)
        self.assertNotIn("You collected", told)  # set, not played
        self.assertIn("[RESUMED]", told)
        self.assertTrue(rebuilt.player_directed)  # real play
        self.assertTrue(rebuilt.ranked_available_actions())
        with self.assertRaises(resume.ParameterError):
            resume.rebuild(self.genome, {**params, "current_location": "Atlantis"})
        with self.assertRaises(resume.ParameterError):
            resume.rebuild(self.genome, {**params, "ended": True})

    def test_the_unp_sets_the_player_and_reports_its_game(self):
        state = self._session(3)
        applied = resume.apply_player(state, {"coins": 50, "add_items": ["Rusty Sword"], "health": 2,
                                              "variables": {"reputation": 2}})
        self.assertEqual(set(applied), {"coins", "add_items", "health", "variables"})
        self.assertEqual((state.coins, state.health, state.variables["reputation"]), (50, 2, 2))
        self.assertIn("Rusty Sword", state.inventory)
        resume.apply_player(state, {"inventory": ["Ancient Map"]})
        self.assertEqual(state.inventory, {"Ancient Map"})
        for bad in ({"stat_points": {"attack": 99}}, {"inventory": ["Unicorn"]}, {"wings": True},
                    {"variables": {"nope": 1}}):
            with self.subTest(bad=bad), self.assertRaises(resume.ParameterError):
                resume.apply_player(state, bad)
        resume.sync(state, {"type": "item_lost", "item": "Ancient Map"})
        self.assertNotIn("Ancient Map", state.inventory)
        resume.sync(state, {"type": "coins_changed", "amount": -10})
        self.assertEqual(state.coins, 40)
        with self.assertRaises(resume.ParameterError):
            resume.sync(state, {"type": "teleport"})

    def test_an_action_done_in_the_unp_can_reach_the_goal(self):
        genome = self.genome
        state = GameState(engine.build_world(genome), genome, rng=random.Random(1), mission="shield_bearer",
                          start="Market")
        self.assertFalse(state.won)
        resume.sync(state, {"type": "item_gained", "item": "Heavy Shield"})
        self.assertTrue(state.won)  # the goal: hold a heavy shield
        state = GameState(engine.build_world(genome), genome, rng=random.Random(1), start="Market")
        resume.sync(state, {"type": "action_done", "action": "(Guard) Report the market thief", "where": "Market"})
        self.assertIn("report the market thief", state.succeeded_actions)
        self.assertEqual(state.unique_actions()[-1]["action"], "(Guard) Report the market thief")  # used up


class SaveGameTests(_Runtime):
    def test_exact_with_changes_or_rebuilt(self):
        state = self._session(10)
        doc = json.loads(json.dumps(savegame.make(state, {"package": "test"})))
        self.assertEqual(doc["format"], "ane.savegame/1")
        loaded, how, note = savegame.load(doc, self.genome, store=self.store)
        self.assertEqual((how, note), ("exact", None))
        self.assertEqual(loaded.history, state.history)
        changed = copy.deepcopy(doc)
        changed["parameters"]["coins"] = state.coins + 9
        loaded, how, note = savegame.load(changed, self.genome, store=self.store)
        self.assertEqual((how, loaded.coins), ("exact", state.coins + 9))
        self.assertIn("coins", note)
        damaged = copy.deepcopy(doc)
        damaged["snapshot"]["attributes"]["coins"] = 999
        loaded, how, note = savegame.load(damaged, self.genome, store=self.store)
        self.assertEqual(how, "parameters")
        self.assertIn("checksum", note)
        self.assertEqual(loaded.coins, state.coins)  # from the parameters
        loaded, how, note = savegame.load(doc, Genome(rng=random.Random(77)), store=self.store)
        self.assertEqual(how, "parameters")
        self.assertIn("storyteller", note)
        no_snapshot = {k: v for k, v in doc.items() if k not in ("snapshot", "checksum")}
        self.assertEqual(savegame.load(no_snapshot, self.genome)[1], "parameters")
        with self.assertRaises(savegame.SaveGameError):
            savegame.load({"format": "other"}, self.genome)


class ContentTests(_Runtime):
    def test_world_patches_are_checked_like_the_studio_checks(self):
        current = config.as_dict()
        self.assertEqual(patch_errors(current), [])
        patched = apply_patch(current, {
            "upsert": {"items": [{"name": "Lantern", "associated_tags": ["light"]}],
                       "locations": {"Lighthouse": {"tags": ["sea"], "descriptions": ["A tall tower."]}}},
            "update": {"npcs": {"Merchant": {"goals": ["Sell lanterns"], "tags": ["trade"]}}},
            "add_actions": {"locations": {"Harbor": [{"name": "Climb the lighthouse", "outcomes": [
                {"desc": "The sea is wide.", "move_to": "Lighthouse"}]}]}},
            "tags": {"npcs": {"Merchant": {"add": ["smuggler"]}}},
            "delete": {"items": ["Antique Statue"]},
        })
        self.assertEqual(patch_errors(patched), [])
        world = patched["world_definition"]
        merchant = next(c for c in world["characters"] if c["name"] == "Merchant")
        self.assertEqual((merchant["goals"], merchant["associated_tags"]), (["Sell lanterns"], ["trade", "smuggler"]))
        self.assertIn("Lighthouse", world["locations"])
        self.assertFalse(any(o["name"] == "Antique Statue" for o in world["objects"]))
        bad = apply_patch(current, {"upsert": {"npcs": [{"name": "Ghost", "role": "overlord", "secret": {"talks": -1},
                                                         "encounter_event": "nope"}]}})
        errors = patch_errors(bad)
        self.assertTrue(any("role" in e for e in errors) and any("secret" in e for e in errors)
                        and any("encounter" in e for e in errors), errors)
        for malformed in ({"teleport": {}}, {"update": {"npcs": {"Nobody": {"goals": []}}}}, {"upsert": {"dragons": []}},
                          {"delete": {"items": ["Unicorn"]}}):
            with self.subTest(patch=malformed), self.assertRaises(PatchFormatError):
                apply_patch(current, malformed)

    def test_the_catalogue_holds_every_entity_and_field(self):
        data = config.as_dict()
        world = data["world_definition"]
        catalogue = world_catalogue(data, ["Harbor"])
        self.assertEqual(catalogue["format"], "ane.world/1")
        self.assertEqual(len(catalogue["locations"]), len(world["locations"]))
        self.assertEqual(len(catalogue["npcs"]), len(world["characters"]))
        self.assertEqual(len(catalogue["items"]), len(world["objects"]))
        self.assertEqual({e["name"] for e in catalogue["events"]}, set(world["events"]))
        self.assertEqual({m["name"] for m in catalogue["missions"]}, set(world["missions"]))
        merchant = next(n for n in catalogue["npcs"] if n["name"] == "Merchant")
        for field in ("associated_tags", "descriptions", "actions", "secret", "known_locations"):
            self.assertIn(field, merchant)
        self.assertTrue(all(e.get("title") for e in catalogue["events"]))
        self.assertEqual(set(catalogue["stats"]["names"]), set(world["character_stats"]["stats"]))
        self.assertEqual(content_rules.world_errors(world), [])


def _package(folder, name="ane-test"):
    """A Python engine package (packaging/make_package.py + the runtime code + run.py)."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    subprocess.run([PYTHON, str(ROOT / "packaging" / "make_package.py"), "--out", str(folder), "--name", name,
                    "--with-code"], check=True, cwd=ROOT, env=env, capture_output=True)
    return folder


class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package = _package(Path(tempfile.mkdtemp(prefix="ane-unp-pkg-")) / "pkg")

    def test_the_whole_protocol_like_a_unp(self):
        out = subprocess.run([PYTHON, str(ROOT / "packaging" / "smoke_test.py"), "--folder", str(self.package),
                              "--command", f"{PYTHON} run.py"], capture_output=True, text=True, timeout=900)
        self.assertEqual(out.returncode, 0, out.stdout[-3000:] + out.stderr[-3000:])
        self.assertIn("Smoke test passed.", out.stdout)

    def test_only_local_authorised_json_requests(self):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["ANE_RUNTIME_TOKEN"] = "t"
        process = subprocess.Popen([PYTHON, "run.py", "--data-dir", tempfile.mkdtemp(), "serve", "--port", "0",
                                    "--exit-on-stdin-eof"], cwd=self.package, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(process.kill)
        info = json.loads(process.stdout.readline())
        port = info["port"]
        self.assertNotIn("token", info)  # given by the UNP, never echoed

        def send(method, path, body=b"", headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
            connection.request(method, path, body=body, headers={"Host": f"127.0.0.1:{port}",
                                                                 "Authorization": "Bearer t", **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b"{}")

        self.assertEqual(send("GET", "/health")[0], 200)
        self.assertEqual(send("GET", "/health", headers={"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(send("GET", "/health", headers={"Host": "evil.example:80"})[0], 403)  # DNS rebinding
        self.assertEqual(send("GET", "/health", headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(send("POST", "/games", b'{"profile": "x"}', {"Content-Type": "text/plain"})[0], 415)
        # Refused before it is read (the declared length is enough).
        self.assertEqual(send("POST", "/games", b"{}", {"Content-Type": "application/json",
                                                        "Content-Length": str(9 * 1024 * 1024)})[0], 413)
        self.assertEqual(send("POST", "/evolve", b"{}", {"Content-Type": "application/json"})[0], 403)
        self.assertEqual(send("GET", "/nowhere")[0], 404)
        self.assertEqual(send("DELETE", "/world")[0], 405)
        self.assertEqual(send("GET", "/games/nope")[0], 404)
        self.assertEqual(send("OPTIONS", "/games", headers={"Origin": "https://evil.example"})[0], 403)
        first = send("POST", "/shutdown", b"{}", {"Content-Type": "application/json"})
        self.assertEqual(first[0], 200)
        process.stdin.close()
        self.assertEqual(process.wait(timeout=60), 0)

    def test_the_cli_says_why_it_did_not_start(self):
        out = subprocess.run([PYTHON, "run.py", "--package", "/nowhere", "serve", "--port", "0"], cwd=self.package,
                             capture_output=True, text=True, timeout=120)
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(json.loads(out.stdout.strip().splitlines()[-1])["event"], "error")


def _run(package, *args, stdin="", timeout=600, env=None):
    """``python run.py <args>`` in a package, as a person or a UNP runs it."""
    environment = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    environment.update(env or {})
    return subprocess.run([PYTHON, "run.py", *args], cwd=package, input=stdin, capture_output=True, text=True,
                          timeout=timeout, env=environment)


class _Server:
    """``run.py serve`` running: its ready line, requests to it, and how it stopped."""

    def __init__(self, package, *serve_args, token="t", data_dir=None, extra=()):
        environment = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        if token:
            environment["ANE_RUNTIME_TOKEN"] = token
        self.token = token
        self.process = subprocess.Popen(
            [PYTHON, "run.py", *extra, "--data-dir", data_dir or tempfile.mkdtemp(), "serve", "--port", "0",
             *serve_args], cwd=package, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True)
        self.ready = json.loads(self.process.stdout.readline())
        self.port = self.ready.get("port")

    def send(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=300)
        sent = {"Host": f"127.0.0.1:{self.port}", **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
                **({"Content-Type": "application/json"} if body is not None else {}), **(headers or {})}
        connection.request(method, path, body=None if body is None else json.dumps(body), headers=sent)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), json.loads(response.read() or b"{}")

    def stop(self):
        if self.process.poll() is None:
            self.send("POST", "/shutdown", {"save": True})
        self.process.stdin.close()
        code = self.process.wait(timeout=120)
        rest = [json.loads(line) for line in self.process.stdout.read().splitlines() if line.strip()]
        return code, rest


class CommandLineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package = _package(Path(tempfile.mkdtemp(prefix="ane-cli-pkg-")) / "pkg", name="ane-cli")

    def test_info_says_what_is_inside(self):
        data = tempfile.mkdtemp()
        out = _run(self.package, "--data-dir", data, "info")
        self.assertEqual(out.returncode, 0, out.stderr)
        info = json.loads(out.stdout)
        self.assertEqual((info["code_version"], info["protocol"], Path(info["data_dir"]).resolve()),
                         (version.code_version(), version.PROTOCOL, Path(data).resolve()))
        self.assertEqual(info["games"], [])  # no session yet

    def test_play_in_a_terminal(self):
        out = _run(self.package, "--data-dir", tempfile.mkdtemp(), "play", "--seed", "3", "--profile", "cli",
                   stdin="x\n1\nq\n")
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertIn("[1] ", out.stdout)  # the choices
        self.assertIn("Choose a number from the list", out.stdout)  # "x" is not one
        self.assertGreaterEqual(out.stdout.count("health "), 2)  # a turn was played
        ended = _run(self.package, "--data-dir", tempfile.mkdtemp(), "play", "--seed", "3", stdin="")
        self.assertEqual(ended.returncode, 0, ended.stderr[-2000:])  # the input ended: it stops quietly
        self.assertNotIn("Traceback", ended.stderr)

    def test_evolve_offline_and_resume_the_training(self):
        package = Path(tempfile.mkdtemp()) / "pkg"
        shutil.copytree(self.package, package)
        out = _run(package, "--data-dir", tempfile.mkdtemp(), "evolve", "--generations", "1", "--pop-size", "2",
                   "--episodes", "1", "--seed", "1")
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertIn("Generation 1/1", out.stdout)
        self.assertRegex(out.stdout, r"(adopted the new|kept the current) storyteller")
        again = _run(package, "--data-dir", tempfile.mkdtemp(), "evolve", "--generations", "1", "--pop-size", "2",
                     "--episodes", "1", "--resume")
        self.assertEqual(again.returncode, 0, again.stderr[-2000:])

    def test_serve_with_a_ready_file_and_without_a_token(self):
        ready_file = Path(tempfile.mkdtemp()) / "ready.json"
        server = _Server(self.package, "--ready-file", str(ready_file), "--no-token", token=None)
        try:
            self.assertEqual(json.loads(ready_file.read_text()), server.ready)
            if os.name == "posix":
                self.assertEqual(ready_file.stat().st_mode & 0o777, 0o600)  # only the user reads it
            self.assertEqual(server.send("GET", "/health")[0], 200)  # no token needed
        finally:
            code, rest = server.stop()
        self.assertEqual(code, 0)
        self.assertEqual(rest[-1]["event"], "stopped")

    def test_serve_one_web_origin_and_offline_training(self):
        package = Path(tempfile.mkdtemp()) / "pkg"
        shutil.copytree(self.package, package)
        origin = "http://localhost:3000"
        server = _Server(package, "--allow-origin", origin, "--allow-evolve")
        try:
            status, headers, _ = server.send("OPTIONS", "/games", headers={"Origin": origin})
            self.assertEqual((status, headers.get("Access-Control-Allow-Origin")), (204, origin))
            self.assertIn("Authorization", headers.get("Access-Control-Allow-Headers", ""))
            status, headers, _ = server.send("GET", "/health", headers={"Origin": origin})
            self.assertEqual((status, headers.get("Access-Control-Allow-Origin")), (200, origin))
            self.assertEqual(server.send("GET", "/health", headers={"Origin": "http://evil.example"})[0], 403)
            status, _, summary = server.send("POST", "/evolve", {"generations": 1, "pop_size": 2, "episodes": 1})
            self.assertEqual(status, 200, summary)
            self.assertIn("adopted", summary)
        finally:
            self.assertEqual(server.stop()[0], 0)

    def test_serve_says_why_it_cannot_listen(self):
        taken = socket.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        self.addCleanup(taken.close)
        port = taken.getsockname()[1]
        out = _run(self.package, "--data-dir", tempfile.mkdtemp(), "serve", "--port", str(port), timeout=120)
        self.assertEqual(out.returncode, 1)
        self.assertEqual(json.loads(out.stdout.strip().splitlines()[-1])["event"], "error")

    @unittest.skipUnless(os.name == "posix", "signals")
    def test_sigterm_saves_every_session_and_stops(self):
        data = tempfile.mkdtemp()
        server = _Server(self.package, data_dir=data)
        status, _, game = server.send("POST", "/games", {"profile": "cli", "seed": 2})
        self.assertEqual(status, 201, game)
        server.process.send_signal(signal.SIGTERM)
        code = server.process.wait(timeout=120)
        rest = [json.loads(line) for line in server.process.stdout.read().splitlines() if line.strip()]
        self.assertEqual(code, 0)
        self.assertEqual(rest[-1]["event"], "stopped")
        saved = Path(data) / "suspended" / f"{game['game_id']}.json"
        self.assertEqual(json.loads(saved.read_text())["format"], "ane.savegame/1")

    def test_the_unity_librarys_launch_arguments(self):
        """AneAiProcess.StartAsync: --package, --data-dir, then serve --port 0 --exit-on-stdin-eof
        --parent-pid <the game> --unp-url <callback>; the token in ANE_RUNTIME_TOKEN."""
        server = _Server(self.package, "--exit-on-stdin-eof", "--parent-pid", str(os.getpid()), "--unp-url",
                         "http://127.0.0.1:9/ane-events", extra=("--package", str(self.package)))
        self.assertEqual(server.ready["event"], "ready")
        self.assertNotIn("token", server.ready)  # given by the game, never echoed
        server.process.stdin.close()  # the game went away
        self.assertEqual(server.process.wait(timeout=120), 0)


class UnityLibraryTests(unittest.TestCase):
    LIBRARY = ROOT / "unity" / "com.ane.ai"

    def _client_calls(self):
        """(method, path) of every request the C# client makes, {...} parts as "x"."""
        source = (self.LIBRARY / "Runtime" / "AneAiClient.cs").read_text(encoding="utf-8")
        calls = re.findall(r'SendAsync\("(\w+)",\s*\$?"([^"]*)"', source)
        return {(method, "/" + re.sub(r"\{[^}]*\}", "x", path)) for method, path in calls}

    def test_the_client_calls_every_route_of_the_ane_ai_and_only_those(self):
        from narrative_runtime.server import _routes

        routes = [(method, pattern) for method, pattern, _ in _routes(types.SimpleNamespace(engine=None))]
        calls = self._client_calls()
        for method, path in sorted(calls):
            with self.subTest(call=f"{method} {path}"):
                self.assertTrue(any(m == method and re.fullmatch(p, path) for m, p in routes), "no such route")
        unused = [f"{m} {p}" for m, p in routes if not any(c == m and re.fullmatch(p, path) for c, path in calls)]
        self.assertEqual(unused, ["POST /evolve"])  # offline training: off unless --allow-evolve

    def test_the_client_sends_what_the_ane_ai_takes(self):
        source = (self.LIBRARY / "Runtime" / "AneAiClient.cs").read_text(encoding="utf-8")
        to_json = source[source.index("public Dictionary<string, object> ToJson()"):]
        to_json = to_json[:to_json.index("return body;")]
        sent = set(re.findall(r'\["(\w+)"\]', to_json))
        server = (ROOT / "narrative_runtime" / "server.py").read_text(encoding="utf-8")
        accepted = set(re.search(r'for k in \(([^)]*)\)', server).group(1).replace('"', "").replace(" ", "").split(","))
        self.assertTrue(sent and sent <= accepted, sent - accepted)
        self.assertIn('["savegame"]', source)  # LoadGameAsync

    def test_platforms_and_programs_agree_with_the_exports(self):
        process = (self.LIBRARY / "Runtime" / "AneAiProcess.cs").read_text(encoding="utf-8")
        named = set(re.findall(r'"((?:windows|linux|macos)-(?:x64|arm64))"', process))
        self.assertEqual(named, set(export.STANDALONE))
        self.assertIn('"ane-ai.exe" : "ane-ai"', process)
        build = (ROOT / "packaging" / "build_runtime.py").read_text(encoding="utf-8")
        self.assertIn('"ane-ai.exe" if os.name == "nt" else "ane-ai"', build)
        self.assertIn('runtimes/ane-runtime-', (self.LIBRARY / "Runtime" / "AneAiProcess.cs").read_text(encoding="utf-8")
                      + (self.LIBRARY / "Runtime" / "AneAiBehaviour.cs").read_text(encoding="utf-8")
                      + (self.LIBRARY / "Runtime" / "AneAiExportImport.cs").read_text(encoding="utf-8"))

    def test_the_package_and_its_assemblies(self):
        package = json.loads((self.LIBRARY / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["name"], "com.ane.ai")
        self.assertRegex(package["version"], r"^\d+\.\d+\.\d+")
        self.assertEqual(package.get("dependencies", {}), {})
        for sample in package.get("samples", []):
            self.assertTrue((self.LIBRARY / sample["path"]).is_dir(), sample["path"])
        runtime = json.loads((self.LIBRARY / "Runtime" / "Ane.AI.Runtime.asmdef").read_text(encoding="utf-8"))
        self.assertEqual(set(runtime["includePlatforms"]),  # it starts a process: desktop players and the Editor
                         {"Editor", "LinuxStandalone64", "macOSStandalone", "WindowsStandalone64"})
        editor = json.loads((self.LIBRARY / "Editor" / "Ane.AI.Editor.asmdef").read_text(encoding="utf-8"))
        self.assertEqual(editor["includePlatforms"], ["Editor"])
        self.assertIn(runtime["name"], editor["references"])
        sample = next((self.LIBRARY / "Samples~").rglob("*.cs")).read_text(encoding="utf-8")
        self.assertIn("#if UNITY_EDITOR || UNITY_STANDALONE", sample)  # mobile and WebGL builds still compile

    def test_the_project_and_package_files_parse(self):
        """CI builds the C# with dotnet; this catches a broken project file without it (an XML
        comment holding `--` made MSBuild refuse the protocol test project)."""
        import xml.dom.minidom

        library = ROOT / "unity" / "com.ane.ai"
        projects = sorted(library.rglob("*.csproj"))
        self.assertEqual(len(projects), 2)
        for path in projects:
            with self.subTest(path=path.name):
                xml.dom.minidom.parse(str(path))
        for path in [library / "package.json", *library.rglob("*.asmdef")]:
            with self.subTest(path=path.name):
                json.loads(path.read_text(encoding="utf-8"))


class ExportPlatformTests(unittest.TestCase):
    def _runtime_zip(self, folder, platform, code_version):
        archive = Path(folder) / f"ane-runtime-{platform}.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("runtime.json", json.dumps({"format": "ane.runtime/1", "target": platform,
                                                       "code_version": code_version, "protocol": 1,
                                                       "executable": "ane-ai", "python": "3.10"}))
            info = zipfile.ZipInfo("ane-ai")
            info.external_attr = 0o100755 << 16
            zipped.writestr(info, "#!/bin/sh\necho ane-ai\n")
            zipped.writestr("_internal/base_library.zip", "x")
        return archive

    def test_standalone_exports_use_a_matching_runtime(self):
        folder = tempfile.mkdtemp(prefix="ane-runtimes-")
        self._runtime_zip(folder, "linux-x64", version.code_version())
        self._runtime_zip(folder, "windows-x64", "0000000000000000")
        with patch.dict(os.environ, {"ANE_RUNTIMES_DIR": folder}):
            listed = {p["platform"]: p for p in export.platforms()}
            self.assertTrue(listed["python"]["available"] and listed["linux-x64"]["available"])
            self.assertFalse(listed["windows-x64"]["available"])
            self.assertIn("other engine code", listed["windows-x64"]["reason"])
            self.assertFalse(listed["macos-arm64"]["available"])
            result = export.export_engine(model=EngineModel.capture(Genome(rng=random.Random(2))), name="ane-linux",
                                          platform="linux-x64", include_memory=False)
            self.assertEqual((result["manifest"]["platform"], result["manifest"]["code_version"]),
                             ("linux-x64", version.code_version()))
            with zipfile.ZipFile(result["path"]) as archive:
                names = set(archive.namelist())
                mode = archive.getinfo("ane-linux/ane-ai").external_attr >> 16
                readme = archive.read("ane-linux/README.md").decode()
            self.assertEqual(mode & 0o111, 0o111)  # still executable
            for required in ("engine.json", "config/engine.yaml", "data/tag_vectors.json", "runtime.json", "ane-ai"):
                self.assertIn(f"ane-linux/{required}", names)
            self.assertFalse(any("/core_engine/" in n or n.endswith("run.py") for n in names))  # inside the program
            self.assertIn("ane-ai serve --port 0", readme)
            for platform in ("windows-x64", "macos-arm64"):
                with self.subTest(platform=platform), self.assertRaises(export.RuntimeUnavailable):
                    export.export_engine(model=EngineModel.capture(Genome(rng=random.Random(2))), platform=platform)
            with self.assertRaises(Exception):
                export.export_engine(genome=Genome(rng=random.Random(2)), platform="amiga")

    def test_the_unity_export_is_an_importable_package(self):
        folder = tempfile.mkdtemp(prefix="ane-runtimes-")
        with patch.dict(os.environ, {"ANE_RUNTIMES_DIR": folder}):
            unity = next(p for p in export.platforms() if p["platform"] == "unity")
            self.assertFalse(unity["available"])  # nothing to run it with yet
            with self.assertRaises(export.RuntimeUnavailable):
                export.export_engine(model=EngineModel.capture(Genome(rng=random.Random(2))), platform="unity")
            self._runtime_zip(folder, "linux-x64", version.code_version())
            self._runtime_zip(folder, "macos-arm64", version.code_version())
            self._runtime_zip(folder, "windows-x64", "0000000000000000")  # other code: left out
            unity = next(p for p in export.platforms() if p["platform"] == "unity")
            self.assertEqual((unity["available"], unity["includes"]), (True, ["linux-x64", "macos-arm64"]))
            result = export.export_engine(model=EngineModel.capture(Genome(rng=random.Random(2))), name="ane-unity",
                                          platform="unity", include_memory=False)
        self.assertTrue(result["path"].endswith(".unitypackage"))
        self.assertEqual(result["manifest"]["unity_platforms"], ["linux-x64", "macos-arm64"])

        assets, metas, folders = {}, {}, set()
        with tarfile.open(result["path"], "r:gz") as tar:
            entries = {}
            for member in tar.getmembers():
                guid, part = member.name.split("/")
                entries.setdefault(guid, {})[part] = tar.extractfile(member).read()
        for guid, parts in entries.items():
            path = parts["pathname"].decode()
            meta = parts["asset.meta"].decode()
            self.assertIn(f"guid: {guid}", meta)  # the folder name is the asset's GUID
            self.assertEqual(guid, export._unity_guid(path))  # fixed per path: a newer export updates it
            if "asset" in parts:
                assets[path], metas[path] = parts["asset"], meta
            else:
                self.assertIn("folderAsset: yes", meta)
                folders.add(path)
        self.assertEqual(len(set(entries)), len(entries))
        for path in assets:  # every folder of every asset has its own meta
            parent = path.rsplit("/", 1)[0]
            while parent != "Assets":
                self.assertIn(parent, folders)
                parent = parent.rsplit("/", 1)[0]

        library = {p for p in assets if p.startswith("Assets/ANE-AI/")}
        for name in ("Runtime/AneAiClient.cs", "Runtime/AneAiProcess.cs", "Runtime/AneAiBehaviour.cs",
                     "Runtime/AneJson.cs", "Runtime/Ane.AI.Runtime.asmdef", "Editor/AneAiEditorMenu.cs",
                     "Editor/Ane.AI.Editor.asmdef", "Samples/TextAdventure/AneAiTextAdventure.cs", "README.md"):
            self.assertIn(f"Assets/ANE-AI/{name}", library)
        self.assertEqual(assets["Assets/ANE-AI/Runtime/AneAiClient.cs"],
                         (export.UNITY_PACKAGE / "Runtime" / "AneAiClient.cs").read_bytes())
        self.assertIn("MonoImporter", metas["Assets/ANE-AI/Runtime/AneAiClient.cs"])
        self.assertIn("AssemblyDefinitionImporter", metas["Assets/ANE-AI/Runtime/Ane.AI.Runtime.asmdef"])

        streaming = "Assets/StreamingAssets/ANE-AI/"
        self.assertEqual({p for p in assets if p.startswith(streaming + "runtimes/")},
                         {streaming + "runtimes/ane-runtime-linux-x64.zip", streaming + "runtimes/ane-runtime-macos-arm64.zip"})
        info = json.loads(assets[streaming + "ane-ai.json"])
        self.assertEqual((info["name"], info["code_version"], info["platforms"]),
                         ("ane-unity", version.code_version(), ["linux-x64", "macos-arm64"]))
        with zipfile.ZipFile(io.BytesIO(assets[streaming + "package.zip"])) as package:
            names = set(package.namelist())
        for required in ("engine.json", "config/engine.yaml", "data/tag_vectors.json"):
            self.assertIn(required, names)  # at the root: the library unpacks it as the package folder
        self.assertFalse(any(n.endswith(".py") or n == "runtime.json" or n == "README.md" for n in names))
        with zipfile.ZipFile(io.BytesIO(assets[streaming + "runtimes/ane-runtime-linux-x64.zip"])) as runtime:
            self.assertEqual(runtime.getinfo("ane-ai").external_attr >> 16 & 0o111, 0o111)

    def test_a_newer_unity_export_updates_the_same_assets_and_its_engine_runs(self):
        folder = tempfile.mkdtemp(prefix="ane-runtimes-")
        self._runtime_zip(folder, "linux-x64", version.code_version())

        def exported(seed):
            with patch.dict(os.environ, {"ANE_RUNTIMES_DIR": folder}):
                result = export.export_engine(model=EngineModel.capture(Genome(rng=random.Random(seed))),
                                              name="ane-unity", platform="unity", include_memory=False)
            assets = {}
            with tarfile.open(result["path"], "r:gz") as tar:
                for member in tar.getmembers():
                    guid, part = member.name.split("/")
                    assets.setdefault(guid, {})[part] = tar.extractfile(member).read()
            return {parts["pathname"].decode(): (guid, parts.get("asset")) for guid, parts in assets.items()}

        first, second = exported(2), exported(5)  # another storyteller, the same paths
        self.assertEqual({path: guid for path, (guid, _) in first.items()},
                         {path: guid for path, (guid, _) in second.items()})
        self.assertNotEqual(first["Assets/StreamingAssets/ANE-AI/package.zip"][1],
                            second["Assets/StreamingAssets/ANE-AI/package.zip"][1])

        # The engine package it ships is a whole one: the ANE AI's code runs it.
        package = Path(tempfile.mkdtemp()) / "package"
        with zipfile.ZipFile(io.BytesIO(second["Assets/StreamingAssets/ANE-AI/package.zip"][1])) as zipped:
            zipped.extractall(package)
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        out = subprocess.run([PYTHON, "-m", "narrative_runtime", "--package", str(package), "--data-dir",
                              tempfile.mkdtemp(), "info"], cwd=ROOT, env=env, capture_output=True, text=True,
                             timeout=300)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        info = json.loads(out.stdout)
        self.assertEqual(info["name"], "ane-unity")

    def test_no_ane_ai_before_a_complete_engine_exists(self):
        folder = tempfile.mkdtemp(prefix="ane-runtimes-")
        self._runtime_zip(folder, "linux-x64", version.code_version())
        with patch.dict(os.environ, {"ANE_RUNTIMES_DIR": folder, "ANE_RECORDS": "off"}), \
                patch.dict(export.RUNS, {}, clear=True), patch.dict(export.ENGINES, {}, clear=True):
            self.assertEqual(export.complete_engines(), [])
            for source in ("run", "engine", "memory", "game"):
                with self.subTest(source=source), self.assertRaises(export.EngineRequired) as refused:
                    export.export_engine(source=source, platform="linux-x64")
                self.assertIn("complete engine", str(refused.exception))
            with self.assertRaises(export.EngineRequired):  # a storyteller alone is not an engine
                export.export_engine(genome=Genome(rng=random.Random(2)), platform="linux-x64")
            python = export.export_engine(genome=Genome(rng=random.Random(2)), include_memory=False)  # Python: as before
            self.assertEqual(python["manifest"]["platform"], "python")

            # A finished run's engine (here: one this process keeps): the ANE AI can be made from it.
            model = EngineModel.capture(Genome(rng=random.Random(3)), source="test run")
            path = Path(folder) / "engine.ane.zip"
            model.save(str(path))
            from backend import sessions

            sessions._remember_engine(model, str(path), run_id="r1")
            export.RUNS["r1"] = {"engine_id": model.id, "learning": {}}
            self.assertEqual([e["engine_id"] for e in export.complete_engines()], [model.id])
            for source in ("run", "engine"):  # the latest run's engine; the newest engine
                with self.subTest(source=source):
                    made = export.export_engine(source=source, platform="linux-x64", include_memory=False)
                    self.assertEqual((made["manifest"]["engine_id"], made["manifest"]["source"]), (model.id, "test run"))


if __name__ == "__main__":
    unittest.main()
