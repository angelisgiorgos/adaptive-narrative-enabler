"""Studio and REST API coverage: actions, missions, rules, profiles, play state, the studio's
services and jobs, app parameters, and the studio as a client of the API (in-process and HTTP)."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import ast
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

import api
from backend import content, records, sessions, store
from backend.errors import AuthoringError
from core_engine import variables as persistent

STUDIO = {"X-ANE-Client": "studio"}


def _finished(client, job_id, timeout=300):
    """Poll a job until it ends."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/v1/jobs/{job_id}").json()
        if job["status"] in ("finished", "failed"):
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish")


class _Sandbox(unittest.TestCase):
    """Copies the config files a test writes to, and redirects the save store."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        for name in ("npcs.yaml", "items.yaml", "events.yaml", "missions.yaml", "hyperparameters.yaml"):
            shutil.copy(store.CONFIG_DIR / name, self.dir / name)
        entity_files = {
            "NPCs": (self.dir / "npcs.yaml", "characters", "list"),
            "Items": (self.dir / "items.yaml", "objects", "list"),
            "Events": (self.dir / "events.yaml", "events", "mapping"),
        }
        for patcher in (
            patch.dict(content.ENTITY_FILES, entity_files),
            patch.object(content, "MISSIONS_PATH", self.dir / "missions.yaml"),
            patch.object(store, "HYPERPARAMETERS_PATH", self.dir / "hyperparameters.yaml"),
            patch.object(store.config, "reload"),
            patch.dict(os.environ, {"ANE_SAVE_DIR": str(self.dir / "saves")}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(api.api)

    def _yaml(self, name):
        return yaml.safe_load((self.dir / name).read_text())


class EntityActionsTests(_Sandbox):
    def test_get_and_put_actions(self):
        actions = self.client.get("/v1/entities/Items/Ancient Map/actions").json()["actions"]
        self.assertEqual(actions[0]["repeat"], "once")
        actions[0]["exclusive_group"] = "read_the_map"
        actions.append({"name": "Burn the map", "repeat": "once", "outcomes": [
            {"desc": "The map curls into ash.", "send_to": "Dungeon", "change_variables": {"reputation": -1}},
        ]})
        response = self.client.put("/v1/entities/Items/Ancient Map/actions", json={"actions": actions})
        self.assertEqual(response.status_code, 200, response.text)
        stored = next(o for o in self._yaml("items.yaml")["world_definition"]["objects"] if o["name"] == "Ancient Map")
        self.assertEqual([a["name"] for a in stored["actions"]][-1], "Burn the map")
        self.assertEqual(stored["actions"][0]["exclusive_group"], "read_the_map")

    def test_invalid_actions_are_rejected_and_nothing_is_written(self):
        before = (self.dir / "items.yaml").read_text()
        bad = [
            [{"name": "x", "outcomes": [{"desc": "y"}], "repeat": "twice"}],
            [{"name": "x", "outcomes": [{"desc": "y", "send_to": "Atlantis"}]}],
            [{"name": "x", "triggers_event": "no_such_event"}],
            [{"name": "x", "outcomes": [{"desc": "y", "set_variables": {"reputation": "high"}}]}],
            [{"name": "x", "outcomes": [{"desc": "y", "telport": "Vault"}]}],
            [{"name": "x", "outcomes": [{"tags": ["neutral"]}]}],
        ]
        for actions in bad:
            with self.subTest(actions=actions):
                response = self.client.put("/v1/entities/Items/Ancient Map/actions", json={"actions": actions})
                self.assertEqual(response.status_code, 400)
        self.assertEqual((self.dir / "items.yaml").read_text(), before)

    def test_studio_editor_round_trip(self):
        text = content.load_entity_actions_yaml("NPCs", "Priest")
        data = yaml.safe_load(text)
        data[0]["repeat"] = "always"
        saved, message = content.save_entity_actions_ui("NPCs", "Priest", yaml.safe_dump(data))
        self.assertEqual(yaml.safe_load(saved)[0]["repeat"], "always")
        self.assertIn("Saved", message)

    def test_event_title_and_delete(self):
        body = self.client.get("/v1/entities/Events/confront_thief").json()
        body["title"] = "Face the thief"
        self.assertEqual(self.client.put("/v1/entities/Events/confront_thief", json=body).json()["title"], "Face the thief")
        self.assertEqual(self._yaml("events.yaml")["world_definition"]["events"]["confront_thief"]["title"], "Face the thief")
        self.assertEqual(self.client.delete("/v1/entities/Items/Rusty Sword").status_code, 200)
        self.assertNotIn("Rusty Sword", [o["name"] for o in self._yaml("items.yaml")["world_definition"]["objects"]])
        self.assertEqual(self.client.delete("/v1/entities/Items/Rusty Sword").status_code, 404)


class MissionTests(_Sandbox):
    def test_get_and_put(self):
        missions = self.client.get("/v1/missions").json()["missions"]
        missions["find_the_orchard"] = {"title": "Find the orchard", "goal": {"location": "Bone Orchard"}, "weight": 2}
        self.assertEqual(self.client.put("/v1/missions", json={"missions": missions}).status_code, 200)
        self.assertIn("find_the_orchard", self._yaml("missions.yaml")["world_definition"]["missions"])

    def test_invalid_missions(self):
        for mission in (
            {"goal": {"location": "Atlantis"}},
            {"goal": {"reach": "Vault"}},
            {"goal": {"variables": {"karma": 1}}},
            {"goal": {"location": "Vault"}, "enabled_if": {"reputation": {"about": 1}}},
            {"goal": {"location": "Vault"}, "weight": -1},
        ):
            with self.subTest(mission=mission):
                response = self.client.put("/v1/missions", json={"missions": {"m": mission}})
                self.assertEqual(response.status_code, 400)

    def test_editor_keeps_comments(self):
        text = "# my missions\nworld_definition:\n  missions:\n    m: {title: M, goal: {location: Vault}}\n"
        self.assertEqual(content.save_missions_yaml(text)[0], text)
        self.assertEqual((self.dir / "missions.yaml").read_text(), text)


class RulesTests(_Sandbox):
    def test_partial_update(self):
        rules = self.client.get("/v1/rules").json()
        self.assertEqual(rules["arrest_location"], "Dungeon")
        response = self.client.put("/v1/rules", json={
            "pursuit_start_step": 5, "pursuit_full_step": 20, "repeat_item": "per_visit", "arrest_lock_when": "always",
        })
        self.assertEqual(response.status_code, 200, response.text)
        data = self._yaml("hyperparameters.yaml")
        self.assertEqual((data["mission_settings"]["pursuit_start_step"], data["mission_settings"]["pursuit_full_step"]), (5, 20))
        self.assertEqual(data["action_rules"]["default_repeat"]["item"], "per_visit")
        self.assertEqual(data["location_constraints"]["Dungeon"]["lock_when"], "always")
        self.assertEqual(data["persistence"]["simulate_with"], rules["simulate_with"])  # untouched

    def test_invalid_rules(self):
        before = (self.dir / "hyperparameters.yaml").read_text()
        for body in ({"pursuit_start_step": 20, "pursuit_full_step": 10}, {"arrest_location": "Atlantis"},
                     {"profile": "no spaces allowed"}, {"repeat_npc": "sometimes"}):
            with self.subTest(body=body):
                self.assertIn(self.client.put("/v1/rules", json=body).status_code, (400, 422))
        self.assertEqual((self.dir / "hyperparameters.yaml").read_text(), before)

    def test_studio_form(self):
        values = list(store.load_rules_ui())
        values[store.RULE_FIELDS.index("arrest_lock_when")] = "never"
        store.save_rules_ui(*values)
        self.assertFalse(self._yaml("hyperparameters.yaml")["location_constraints"]["Dungeon"]["requires_escape"])


class ProfileAndPlayTests(_Sandbox):
    def test_stored_values_of_profiles(self):
        response = self.client.put("/v1/variables/values?profile=bob", json={"values": {"reputation": -3}, "playthroughs": 2})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["values"]["reputation"], response.json()["playthroughs"]), (-3, 2))
        self.assertIn("bob", self.client.get("/v1/variables/profiles").json()["profiles"])
        self.assertEqual(self.client.put("/v1/variables/values?profile=bob", json={"values": {"reputation": "bad"}}).status_code, 400)
        self.assertEqual(self.client.put("/v1/variables/values?profile=bob", json={"values": {"karma": 1}}).status_code, 400)
        self.assertEqual(self.client.get("/v1/variables/values?profile=no%20spaces").status_code, 400)
        self.assertEqual(self.client.post("/v1/variables/reset?profile=bob").json()["playthroughs"], 0)

    def test_studio_stored_values_editor(self):
        text, _ = content.save_stored_values_yaml("carol", "playthroughs: 1\nvalues:\n  thief_caught: true\n")
        self.assertEqual(yaml.safe_load(text), {"playthroughs": 1, "values": {"thief_caught": True, "fence_exposed": False, "reputation": 0}})
        text, _ = content.reset_stored_values_ui("carol")
        self.assertEqual(yaml.safe_load(text)["playthroughs"], 0)

    def test_play_uses_the_profile_and_reports_structured_state(self):
        persistent.VariableStore().save("dave", {"thief_caught": True}, playthroughs=1)
        body = {"candidate_count": 1, "generations": 1, "simulation_steps": 2, "deterministic": False, "profile": "dave"}
        game = self.client.post("/v1/simulations", json=body)
        self.assertEqual(game.status_code, 200, game.text)
        state = game.json()
        self.assertEqual(state["profile"], "dave")
        self.assertEqual(state["variables"]["thief_caught"], True)
        self.assertEqual(state["variables"]["playthroughs"], 1)
        for field in ("location", "health", "max_health", "coins", "power", "inventory", "mission_status"):
            self.assertIn(field, state)
        self.assertLessEqual(state["health"], state["max_health"])
        after = self.client.post(f"/v1/sessions/{state['session_id']}/actions", json={"action": state["actions"][0]})
        self.assertEqual(after.status_code, 200, after.text)
        self.assertEqual(after.json()["profile"], "dave")
        self.assertEqual(self.client.post(f"/v1/sessions/{state['session_id']}/actions",
                                          json={"action": "Fly to the moon"}).status_code, 400)
        for request in (self.client.get("/v1/sessions/nothing"),
                        self.client.post("/v1/sessions/nothing/actions", json={"action": "Look around"})):
            self.assertEqual((request.status_code, request.json()["detail"]), (404, "Play session not found."))


class StudioServiceTests(_Sandbox):
    """The endpoints the studio is built on: every studio tab works through the API."""

    def test_reference_and_app_settings(self):
        reference = self.client.get("/v1/reference").json()
        self.assertEqual(set(reference["entity_kinds"]), {"Locations", "NPCs", "Items", "Events"})
        self.assertEqual(set(reference["rule_fields"]), set(api.StoryRules.model_fields))
        self.assertIn(reference["repeat_default"], ("default",))
        self.assertTrue({"once", "per_visit", "always"} <= set(reference["repeat_rules"]))
        self.assertIn("story default", reference["player_stat_choices"])
        self.assertTrue(reference["action_reference"] and reference["common_tags"] and reference["goal_keys"])
        with patch.object(store, "APP_SETTINGS_PATH", self.dir / "ui.yml"):
            settings = self.client.get("/v1/app-settings").json()
            self.assertEqual(set(settings["settings"]), set(settings["defaults"]))
            response = self.client.put("/v1/app-settings", json={"settings": {"candidate_limit": 30}})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["settings"]["candidate_limit"], 30)
            self.assertEqual(yaml.safe_load((self.dir / "ui.yml").read_text())["candidate_limit"], 30)
            self.assertEqual(self.client.put("/v1/app-settings", json={"settings": {"colour": "red"}}).status_code, 400)
            self.assertEqual(self.client.put("/v1/app-settings", json={"settings": {"candidate_limit": 1}}).status_code, 400)
            for limit, maximum in (("candidate_limit", 400), ("generation_limit", 1000), ("simulation_step_limit", 400)):
                with self.subTest(limit=limit):
                    self.assertEqual(settings["defaults"][limit], maximum)
                    self.assertEqual(self.client.put("/v1/app-settings",
                                                     json={"settings": {limit: maximum + 1}}).status_code, 400)

    def test_runs_up_to_400_candidates_1000_generations_and_400_steps(self):
        largest = {"candidate_count": 400, "generations": 1000, "simulation_steps": 400, "episodes": 1,
                   "deterministic": False}
        for field in ("candidate_count", "generations", "simulation_steps"):
            with self.subTest(field=field):
                too_large = {**largest, field: largest[field] + 1}
                self.assertEqual(self.client.post("/v1/simulations", json=too_large).status_code, 422)
                self.assertEqual(self.client.post("/v1/jobs/simulations", json=too_large).status_code, 422)
        # The largest run passes the API and reaches the backend unclamped; the total-work limit refuses it.
        response = self.client.post("/v1/simulations", json=largest)
        self.assertEqual(response.status_code, 400, response.text)
        requested = int(re.search(r"requests ([\d,]+) simulated turns", response.json()["detail"]).group(1).replace(",", ""))
        self.assertGreaterEqual(requested, 400 * 1000 * 400)

    def test_documents(self):
        names = [d["name"] for d in self.client.get("/v1/documents").json()["documents"]]
        self.assertEqual(names, ["missions", "messages", "variables", "stats", "stored_values"])
        for name in names:
            with self.subTest(document=name):
                response = self.client.get(f"/v1/documents/{name}", params={"profile": "erin"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIsInstance(yaml.safe_load(response.json()["yaml"]), dict)
        self.assertEqual(self.client.get("/v1/documents/nothing").status_code, 404)
        text = "# kept\nworld_definition:\n  missions:\n    m: {title: M, goal: {location: Vault}}\n"
        saved = self.client.put("/v1/documents/missions", json={"yaml": text})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual((self.dir / "missions.yaml").read_text(), text)  # comments kept
        for bad in ("world_definition: [", "world_definition:\n  missions:\n    m: {goal: {location: Atlantis}}\n"):
            with self.subTest(yaml=bad):
                self.assertEqual(self.client.put("/v1/documents/missions", json={"yaml": bad}).status_code, 400)
        self.assertEqual((self.dir / "missions.yaml").read_text(), text)  # nothing written
        values = self.client.put("/v1/documents/stored_values", params={"profile": "erin"},
                                 json={"yaml": "playthroughs: 2\nvalues:\n  reputation: 3\n"})
        self.assertEqual(values.status_code, 200, values.text)
        self.assertEqual(yaml.safe_load(values.json()["yaml"])["values"]["reputation"], 3)
        reset = self.client.post("/v1/variables/reset/yaml", params={"profile": "erin"})
        self.assertEqual(yaml.safe_load(reset.json()["yaml"])["playthroughs"], 0)

    def _stored_item(self, name):
        return next(o for o in self._yaml("items.yaml")["world_definition"]["objects"] if o["name"] == name)

    def test_entity_editor_services(self):
        names = self.client.get("/v1/entities/Items", params={"fields": "names"}).json()
        self.assertIn("Ancient Map", names["names"])
        self.assertNotIn("entities", names)
        data = yaml.safe_load(self.client.get("/v1/entities/Items/Ancient Map/actions/yaml").json()["yaml"])
        data[0]["exclusive_group"] = "read_the_map"
        saved = self.client.put("/v1/entities/Items/Ancient Map/actions/yaml", json={"yaml": yaml.safe_dump(data)})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(self._stored_item("Ancient Map")["actions"][0]["exclusive_group"], "read_the_map")
        bad = self.client.put("/v1/entities/Items/Ancient Map/actions/yaml", json={"yaml": "- name: x\n  repeat: twice\n"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.client.get("/v1/entities/Items/Nothing/actions/yaml").status_code, 404)

        path = "/v1/entities/Items/Ancient Map/actions/0/repetition"
        self.assertEqual({k: v for k, v in self.client.get(path).json().items() if k != "label"},
                         {"index": 0, "repeat": "once", "retry_on_failure": False})
        changed = self.client.put(path, json={"repeat": "per_visit", "retry_on_failure": True})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual((changed.json()["repeat"], changed.json()["retry_on_failure"]), ("per_visit", True))
        stored = self._stored_item("Ancient Map")["actions"][0]
        self.assertEqual((stored["repeat"], stored["retry_on_failure"]), ("per_visit", True))
        self.assertEqual(self.client.get("/v1/entities/Items/Ancient Map/actions/99/repetition").status_code, 404)
        self.assertEqual(self.client.put(path, json={"repeat": "twice"}).status_code, 422)

        policy = yaml.safe_load(self.client.get("/v1/entities/Events/confront_thief/policy/yaml").json()["yaml"]) or {}
        policy["cooldown"] = 3
        saved = self.client.put("/v1/entities/Events/confront_thief/policy/yaml", json={"yaml": yaml.safe_dump(policy)})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(self._yaml("events.yaml")["world_definition"]["events"]["confront_thief"]["cooldown"], 3)
        self.assertEqual(self.client.put("/v1/entities/Events/confront_thief/policy/yaml",
                                         json={"yaml": "colour: red\n"}).status_code, 400)
        self.assertEqual(self.client.put("/v1/entities/NPCs/Priest/policy/yaml", json={"yaml": ""}).status_code, 422)

    def test_jobs_report_progress_and_record_who_asked(self):
        body = {"candidate_count": 2, "generations": 2, "simulation_steps": 3, "episodes": 1, "deterministic": False}
        submitted = self.client.post("/v1/jobs/simulations", json=body, headers=STUDIO)
        self.assertEqual(submitted.status_code, 202, submitted.text)
        self.assertIn(submitted.json()["status"], ("queued", "running", "finished"))
        job = _finished(self.client, submitted.json()["id"])
        self.assertEqual(job["status"], "finished", job["error"])
        self.assertEqual((job["progress"]["generation"], job["progress"]["total"]), (2, 2))
        game = job["result"]
        self.assertTrue(game["session_id"] and game["scene"] and game["engine_download_url"])
        self.assertEqual(records.get_run(game["run_id"])["origin"], "studio")
        plain = self.client.post("/v1/simulations", json={**body, "generations": 1}).json()
        self.assertEqual(records.get_run(plain["run_id"])["origin"], "api")
        listed = self.client.get("/v1/jobs").json()["jobs"]
        self.assertIn(job["id"], [j["id"] for j in listed])
        self.assertNotIn("result", listed[0])
        self.assertEqual(self.client.get("/v1/jobs/nothing").status_code, 404)
        for path, request in (("/v1/jobs/engines/nothing/continue", {"generations": 1}),
                              ("/v1/jobs/engines/nothing/retrain", {}),
                              ("/v1/records/engines/nothing/play", {}),
                              ("/v1/records/engines/nothing/continue", {"generations": 1}),
                              ("/v1/records/engines/nothing/retrain", {})):
            with self.subTest(path=path):  # an unknown engine is refused before any work starts
                self.assertEqual(self.client.post(path, json=request).status_code, 404)
        with patch.object(sessions, "run_simulation", side_effect=AuthoringError("The run could not start.")):
            failed = _finished(self.client, self.client.post("/v1/jobs/simulations", json=body).json()["id"])
        self.assertEqual((failed["status"], failed["error"]), ("failed", "The run could not start."))

    def test_loading_an_engine_with_the_players_own_points(self):
        game = self.client.post("/v1/simulations", json={"candidate_count": 1, "generations": 1,
                                                         "simulation_steps": 2, "deterministic": False}).json()
        model = self.client.get(game["engine_download_url"]).content
        points = self.client.post("/v1/stats/allocate", json={"mode": "balanced"}).json()["points"]
        loaded = self.client.post("/v1/engines/load", files={"file": ("engine.ane.zip", model)},
                                  params={"stats": json.dumps(points)}, headers=STUDIO)
        self.assertEqual(loaded.status_code, 200, loaded.text)
        self.assertEqual(loaded.json()["stat_points"], points)
        for params, files in (({"stats": "[1, 2]"}, {}), ({}, {"parameters": ("genome.txt", b"x")})):
            with self.subTest(params=params, files=list(files)):
                response = self.client.post("/v1/engines/load", params=params,
                                            files={"file": ("engine.ane.zip", model), **files})
                self.assertEqual(response.status_code, 400, response.text)


class AppSettingsTests(unittest.TestCase):
    """The studio's app parameters (config/ui_settings.yml), in backend/store.py."""

    def test_settings_round_trip_and_invalid_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(store, "APP_SETTINGS_PATH", Path(directory) / "ui.yml"):
                settings = store.load_app_settings()
                settings["default_candidates"] = 4
                store.save_app_settings(settings)
                self.assertEqual(store.load_app_settings(), settings)
                for key, value in (("server_port", 70000), ("max_web_sessions", 0),
                                   ("crossover_probability", float("nan")),
                                   ("default_candidates", 401), ("candidate_limit", 401),
                                   ("generation_limit", 1001), ("simulation_step_limit", 401),
                                   ("queue_max_size", True)):
                    with self.subTest(key=key), self.assertRaises(ValueError):
                        store.save_app_settings({**settings, key: value})
                self.assertEqual(store.load_app_settings(), settings)


class StudioIsAnAPIClientTests(_Sandbox):
    def test_the_studio_uses_no_backend_code(self):
        """app.py imports nothing of the project at module level; only its in-process
        mode imports the API (api.py) to serve it, and nothing else."""
        root = Path(__file__).resolve().parent.parent
        project = {p.stem for p in root.glob("*.py")} | {p.name for p in root.iterdir() if (p / "__init__.py").exists()}

        def imported(nodes):
            names = {a.name.split(".")[0] for n in nodes if isinstance(n, ast.Import) for a in n.names}
            return names | {n.module.split(".")[0] for n in nodes if isinstance(n, ast.ImportFrom) and n.module}

        tree = ast.parse((root / "app.py").read_text())
        self.assertEqual(imported(tree.body) & project, set())
        self.assertEqual(imported(ast.walk(tree)) & project, {"api"})

    def test_studio_handlers_go_through_the_api(self):
        import app
        with patch.object(app.API, "request", wraps=app.API.request) as spy:
            app.load_entity_ui("Items", "Ancient Map")
            app.load_action_repetition("Items", "Ancient Map", 0)
            app.load_document("missions")
            app.learning_memory_text()
            app.check_points_text("{}")
            app.load_rules_ui()
        paths = {call.args[1] for call in spy.call_args_list}
        self.assertTrue({"/v1/entities/Items/Ancient%20Map", "/v1/entities/Items/Ancient%20Map/actions/0/repetition",
                         "/v1/documents/missions", "/v1/learning", "/v1/stats/allocate", "/v1/rules"} <= paths, paths)

    def test_refusals_are_shown_in_the_studio_and_write_nothing(self):
        import app
        import gradio as gr
        before = (self.dir / "items.yaml").read_text()
        with self.assertRaises(gr.Error):
            app.save_entity_actions_ui("Items", "Ancient Map", "- name: x\n  repeat: twice\n")
        with self.assertRaises(gr.Error):
            app.save_entity_ui("Items", "Ancient Map", "Ancient Map", [], "", "", False, "", False, "{any: [", "", "",
                               False, 1.0)
        self.assertEqual((self.dir / "items.yaml").read_text(), before)


class StudioOverHTTPTests(unittest.TestCase):
    """With ANE_API_URL set, the studio talks to a running API over HTTP."""

    def test_the_client_talks_to_a_running_api(self):
        import uvicorn

        from app import APIError, StudioAPI

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(api.api, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(setattr, server, "should_exit", True)
        deadline = time.time() + 20
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        with patch.dict(os.environ, {"ANE_API_URL": f"http://127.0.0.1:{port}"}):
            client = StudioAPI()
        self.addCleanup(client.close)
        self.assertEqual(client.mode, "http")
        self.assertIn("Locations", client.get("/v1/reference")["entity_kinds"])
        self.assertIn("Ancient Map", client.get("/v1/entities/Items", fields="names")["names"])
        with self.assertRaises(APIError) as caught:
            client.get("/v1/entities/Items/Nothing")
        self.assertEqual((caught.exception.status, str(caught.exception)), (404, "Entity not found."))
        with self.assertRaises(APIError) as caught:
            client.put("/v1/settings", {"pop_size": 0})
        self.assertEqual(caught.exception.status, 422)
        downloaded = client.download("/openapi.json")
        self.assertIn("/v1/jobs/{job_id}", json.loads(Path(downloaded).read_text())["paths"])
        unreachable = StudioAPI("http://127.0.0.1:9")
        self.addCleanup(unreachable.close)
        with self.assertRaises(APIError):
            unreachable.get("/healthz")


if __name__ == "__main__":
    unittest.main()
