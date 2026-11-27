"""Every functionality reaches both the API and the studio, and the studio reaches it through the
API: every endpoint is used by the studio (or is one other clients use, the studio reaching the
same function through another endpoint), and the endpoints added for NPC roles and secrets, whole
entries as YAML, the map, the failure lint, the judge (a game, a recorded story, two stories
compared, the benchmark and its A/B, fitting the fitness) and the fitness term weights work from
the API and from the studio's handlers."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import ast
import itertools
import json
import random
import re
import shutil
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

import api
from backend import content, sessions, store
from core_engine import Genome
from evaluator.game_judge import CRITERIA

try:
    from test_studio_api import _finished, _Sandbox
except ImportError:
    from tests.test_studio_api import _finished, _Sandbox

ROOT = Path(__file__).resolve().parent.parent

# Endpoints the studio does not call, each with the endpoint it uses for the same function.
CLIENT_ONLY = {
    ("GET", "/v1/variables"): "the studio edits the definitions as YAML: /v1/documents/variables",
    ("PUT", "/v1/variables"): "/v1/documents/variables",
    ("GET", "/v1/variables/values"): "/v1/documents/stored_values",
    ("PUT", "/v1/variables/values"): "/v1/documents/stored_values",
    ("POST", "/v1/variables/reset"): "/v1/variables/reset/yaml (the reset profile as YAML)",
    ("GET", "/v1/messages"): "/v1/documents/messages",
    ("PUT", "/v1/messages"): "/v1/documents/messages",
    ("PUT", "/v1/stats"): "/v1/documents/stats",
    ("GET", "/v1/missions"): "/v1/documents/missions",
    ("PUT", "/v1/missions"): "/v1/documents/missions",
    ("PUT", "/v1/entities/{kind}/{name}/actions"): "/v1/entities/{kind}/{name}/actions/yaml",
    ("PUT", "/v1/map"): "/v1/documents/map (the whole map; the studio's form sets one place's borders: /v1/map/{place})",
    ("GET", "/v1/entities/{kind}/{name}/policy"): "/v1/entities/{kind}/{name}/policy/yaml",
    ("PUT", "/v1/entities/{kind}/{name}/policy"): "/v1/entities/{kind}/{name}/policy/yaml",
    ("GET", "/v1/documents"): "/v1/reference lists the documents",
    ("GET", "/v1/runs"): "the studio keeps its run; earlier runs: /v1/records/runs",
    ("POST", "/v1/simulations"): "/v1/jobs/simulations (with progress)",
    ("GET", "/v1/simulations/work"): "/v1/simulations/resolve (its `work`, with every other resolved setting)",
    ("POST", "/v1/records/engines/{engine_id}/continue"): "/v1/jobs/engines/{engine_id}/continue",
    ("POST", "/v1/records/engines/{engine_id}/retrain"): "/v1/jobs/engines/{engine_id}/retrain",
    ("GET", "/v1/sessions/{session_id}"): "every action returns the game's state",
    ("GET", "/v1/sessions/{session_id}/report"): "the play state carries the report: /v1/sessions/{id}/end",
    ("POST", "/v1/engines/{engine_id}/activate"): "the studio sets its defaults with a configuration: /v1/config/upload "
                                                  "(a game with an engine plays in its own context)",
}
# Endpoints the studio reaches through URLs the API returns (not written in app.py).
FOLLOWED = {("GET", "/v1/artifacts/{artifact_id}"): "API.download(game['config_download_url']) and the like"}


def _signature(path):
    """A path as its literal segments, with * for every parameter: /v1/a/{x}/b -> ('a', '*', 'b')."""
    path = path.split("?")[0]
    return tuple("*" if part.startswith("{") or part == "*" else part
                 for part in path.strip("/").split("/")[1:] if part)


def _studio_requests():
    """{(method, signature)} of every request in app.py: API.get/post/put/delete/upload/download, the
    client's own self.get, _recorded(path) (GET), with paths from strings, f-strings and _path(...)."""
    tree = ast.parse((ROOT / "app.py").read_text())

    def text(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return "".join(v.value if isinstance(v, ast.Constant) else "{*}" for v in node.values)
        if isinstance(node, ast.BinOp):  # path + "?format=yaml"
            return text(node.left)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_path":
            parts = [text(arg) if isinstance(arg, (ast.Constant, ast.JoinedStr)) else "*" for arg in node.args]
            return "/".join(str(part) for part in parts)
        return None

    methods = {"get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH", "delete": "DELETE", "upload": "POST",
               "download": "GET"}
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in ("API", "self") \
                and func.attr in methods:
            path = text(node.args[0])
            if path and path.startswith("/v1"):
                found.add((methods[func.attr], _signature(path)))
        elif isinstance(func, ast.Name) and func.id == "_recorded":
            path = text(node.args[0])
            if path:
                found.add(("GET", _signature(path)))
    return found


class EndpointCoverageTests(unittest.TestCase):
    def test_every_endpoint_is_used_by_the_studio_or_named_as_client_only(self):
        used = _studio_requests()
        missing = []
        for route in api.api.routes:
            path = getattr(route, "path", "")
            if not path.startswith("/v1"):
                continue
            for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
                if (method, path) in CLIENT_ONLY or (method, path) in FOLLOWED:
                    continue
                if (method, _signature(path)) not in used:
                    missing.append(f"{method} {path}")
        self.assertEqual(missing, [], "endpoints the studio cannot reach (use them in app.py, or name the studio's "
                                      "equivalent in CLIENT_ONLY)")

    def test_client_only_endpoints_exist_and_their_equivalents_are_used(self):
        routes = {(method, route.path) for route in api.api.routes for method in getattr(route, "methods", ())}
        self.assertEqual([key for key in {**CLIENT_ONLY, **FOLLOWED} if key not in routes], [])
        used = {signature for _, signature in _studio_requests()}

        def reached(signature):  # the studio's {name} parts stand for any name
            return any(len(mine) == len(signature) and all(m in ("*", s) for m, s in zip(mine, signature))
                       for mine in used)

        for (method, path), equivalent in CLIENT_ONLY.items():
            target = re.search(r"/v1/[\w/{}.-]+", equivalent)
            if target:
                with self.subTest(endpoint=path):
                    self.assertTrue(reached(_signature(target.group(0))), equivalent)


class FrontendGuideTests(unittest.TestCase):
    """docs/FRONTEND.md, the guide a frontend is built from, stays true to the API: its endpoint index
    lists every route exactly once, and every request it names anywhere is a route."""

    GUIDE = ROOT / "docs" / "FRONTEND.md"
    REQUEST = re.compile(r"\b(GET|PUT|POST|PATCH|DELETE)\s+`?(/v1/[^\s`?)|,;\\]*)")

    @staticmethod
    def _routes():
        return {(method, route.path) for route in api.api.routes if getattr(route, "path", "").startswith("/v1")
                for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}}

    def _text(self):
        return self.GUIDE.read_text(encoding="utf-8")

    def test_the_endpoint_index_lists_every_route_once(self):
        index = self._text().split("\n# Endpoint index", 1)[1]
        rows = re.findall(r"^\| `(GET|PUT|POST|PATCH|DELETE)` \| `(/v1/[^`]*)` \|", index, flags=re.M)
        self.assertEqual(len(rows), len(set(rows)), "an endpoint is listed twice")
        listed, routes = set(rows), self._routes()
        self.assertEqual(sorted(routes - listed), [], "routes missing from the guide's endpoint index")
        self.assertEqual(sorted(listed - routes), [], "the guide's endpoint index lists routes the API does not have")

    def test_every_request_the_guide_names_exists(self):
        routes = {(method, _signature(path)) for method, path in self._routes()}

        def exists(method, signature):  # the guide may name a value (Locations) where the route has {kind}
            return any(m == method and len(s) == len(signature) and all(a == b or "*" in (a, b)
                                                                       for a, b in zip(s, signature))
                       for m, s in routes)

        unknown = sorted({f"{method} {path}" for method, path in self.REQUEST.findall(self._text())
                          if not exists(method, _signature(path.rstrip(".:")))})
        self.assertEqual(unknown, [], "requests named in docs/FRONTEND.md that the API does not have")


class _CoverageSandbox(_Sandbox):
    def setUp(self):
        super().setUp()
        shutil.copy(store.CONFIG_DIR / "locations.yaml", self.dir / "locations.yaml")
        patcher = patch.object(content, "MAP_PATH", self.dir / "locations.yaml")
        patcher.start()
        self.addCleanup(patcher.stop)


class AuthoringTests(_CoverageSandbox):
    def test_an_npcs_role_and_secret(self):
        reference = self.client.get("/v1/reference").json()
        self.assertIn("nemesis", reference["npc_roles"])
        self.assertEqual(set(reference["judge_criteria"]), set(CRITERIA))
        merchant = self.client.get("/v1/entities/NPCs/Merchant").json()
        self.assertEqual(merchant["secret"]["talks"], 2)
        self.assertIsNone(merchant["role"])
        body = {**merchant, "role": "informant", "secret": {"text": "{npc} fences goods.", "talks": 3}}
        response = self.client.put("/v1/entities/NPCs/Merchant", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        saved = next(c for c in self._yaml("npcs.yaml")["world_definition"]["characters"] if c["name"] == "Merchant")
        self.assertEqual((saved["role"], saved["secret"]), ("informant", {"text": "{npc} fences goods.", "talks": 3}))
        # Left out: kept; "" and {}: removed; unknown role or a bad secret: refused, nothing written.
        kept = {k: v for k, v in merchant.items() if k not in ("role", "secret")}
        self.assertEqual(self.client.put("/v1/entities/NPCs/Merchant", json=kept).json()["role"], "informant")
        self.assertEqual(self.client.put("/v1/entities/NPCs/Merchant", json={**merchant, "role": "overlord"}).status_code,
                         400)
        self.assertEqual(self.client.put("/v1/entities/NPCs/Merchant",
                                         json={**merchant, "secret": {"talks": -1}}).status_code, 400)
        cleared = self.client.put("/v1/entities/NPCs/Merchant", json={**merchant, "role": "", "secret": {}}).json()
        self.assertEqual((cleared["role"], cleared["secret"]), (None, None))

    def test_every_field_of_an_entry_as_yaml(self):
        text = self.client.get("/v1/entities/NPCs/Gate%20Guard/yaml").json()["yaml"]
        record = yaml.safe_load(text)
        self.assertEqual(record["name"], "Gate Guard")
        self.assertIn("secret", record)
        record["known_locations"] = ["Vault", "Palace"]
        record["role"] = "lieutenant"
        response = self.client.put("/v1/entities/NPCs/Gate%20Guard/yaml", json={"yaml": yaml.safe_dump(record)})
        self.assertEqual(response.status_code, 200, response.text)
        saved = next(c for c in self._yaml("npcs.yaml")["world_definition"]["characters"] if c["name"] == "Gate Guard")
        self.assertEqual((saved["known_locations"], saved["role"]), (["Vault", "Palace"], "lieutenant"))
        before = (self.dir / "npcs.yaml").read_text()
        for bad in ({**record, "actions": [{"name": "x", "outcomes": [{"desc": "y", "move_to": "Atlantis"}]}]},
                    {**record, "role": "overlord"}, {**record, "name": "Gate Keeper"}, ["not", "a", "mapping"]):
            with self.subTest(bad=str(bad)[:60]):
                self.assertEqual(self.client.put("/v1/entities/NPCs/Gate%20Guard/yaml",
                                                 json={"yaml": yaml.safe_dump(bad)}).status_code, 400)
        self.assertEqual((self.dir / "npcs.yaml").read_text(), before)
        self.assertEqual(self.client.get("/v1/entities/NPCs/Nobody/yaml").status_code, 404)

    def test_the_map(self):
        text = self.client.get("/v1/documents/map").json()["yaml"]
        routes = yaml.safe_load(text)["routes"]
        self.assertIn("Market", routes["Harbor"])
        routes["Tavern"] = ["Harbor", "Market", "Alley", "Vault"]
        response = self.client.put("/v1/documents/map", json={"yaml": yaml.safe_dump({"routes": routes})})
        self.assertEqual(response.status_code, 200, response.text)
        stored = self._yaml("locations.yaml")["world_definition"]
        self.assertIn("Vault", stored["routes"]["Tavern"])
        self.assertIn("Harbor", stored["locations"])  # the places are kept
        for bad in ({"routes": {"Tavern": ["Atlantis"]}}, {"routes": {"Atlantis": []}}, {"paths": {}}):
            with self.subTest(bad=bad):
                self.assertEqual(self.client.put("/v1/documents/map", json={"yaml": yaml.safe_dump(bad)}).status_code, 400)
        self.assertIn("Vault", self._yaml("locations.yaml")["world_definition"]["routes"]["Tavern"])

    def test_the_failure_lint_and_term_weights(self):
        lint = self.client.get("/v1/lint/failures")
        self.assertEqual(lint.status_code, 200)
        self.assertIsInstance(lint.json()["dead_ends"], list)
        self.assertEqual(self.client.get("/v1/fitness/term-weights").json(), {"weights": {}})
        response = self.client.put("/v1/fitness/term-weights", json={"weights": {"climax": 2, "novelty_fade": 1.5}})
        self.assertEqual(response.status_code, 200, response.text)
        stored = self._yaml("hyperparameters.yaml")["fitness"]
        self.assertEqual(stored["term_weights"], {"climax": 2.0, "novelty_fade": 1.5})
        self.assertIn("beats", stored)  # the rest of the section is kept
        self.assertEqual(self.client.put("/v1/fitness/term-weights", json={"weights": {"climax": -1}}).status_code, 400)
        self.assertEqual(self.client.put("/v1/fitness/term-weights", json={"weights": {}}).status_code, 200)
        self.assertEqual(self._yaml("hyperparameters.yaml")["fitness"]["term_weights"], {})

    def test_the_new_logic_sections_round_trip(self):
        for section in ("npc_roles", "climax", "variation", "travel", "stakes", "consequences", "memory"):
            with self.subTest(section=section):
                text = self.client.get(f"/v1/logic/{section}").json()["parameters"]
                response = self.client.put(f"/v1/logic/{section}", json={"parameters": text})
                self.assertEqual(response.status_code, 200, response.text)
        text = self.client.get("/v1/logic/npc_roles").json()["parameters"].replace(
            "    bystander: {label: bystander", "    boss: {label: boss, importance: 3}\n    bystander: {label: bystander")
        self.assertEqual(self.client.put("/v1/logic/npc_roles", json={"parameters": text}).status_code, 200)


def _reply(engagement=7, winner="A"):
    return json.dumps({"scores": {name: 6 for name in CRITERIA}, "engagement": engagement, "best_moment": "the climax",
                       "weakest_moment": "a repeat", "verdict": "Fine.", "winner": winner,
                       "criteria": {name: winner for name in CRITERIA}, "reason": "A builds to its climax."})


class FakeRuntime:
    """A thread-safe stand-in for Gemma 4: judgements with engagement 4..9 in turn, and comparisons
    always won by the playthrough shown first."""

    def __init__(self):
        self.lock = threading.Lock()
        self.engagement = itertools.cycle([4, 5, 6, 7, 8, 9])
        self.calls = 0

    def generate(self, prompt, **kwargs):
        with self.lock:
            self.calls += 1
            if "PLAYTHROUGH A:" in prompt and "PLAYTHROUGH B:" in prompt:
                return _reply(winner="A")  # always the one shown first: the two orders disagree, a tie
            return _reply(next(self.engagement))


class JudgeTests(_CoverageSandbox):
    def setUp(self):
        super().setUp()
        self.tokens = []
        for seed in (4, 5):
            token, state = sessions._start_game(Genome(rng=random.Random(seed)), "judge-test", None, None)
            self.addCleanup(sessions.SESSIONS.pop, token, None)
            for _ in range(3):
                sessions.play_action(token, sessions._player_options(state)[0].name)
            self.tokens.append(token)

    def test_two_stories_are_compared(self):
        runtime = FakeRuntime()
        with patch("utils.llm_model_loader.get_llm_runtime", lambda *a, **k: runtime):
            response = self.client.post("/v1/judge/compare", json={"a": {"session_id": self.tokens[0]},
                                                                  "b": {"story_id": sessions.SESSION_RECORDS[self.tokens[1]]}})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertIn(result["winner"], ("A", "B", "tie"))
        self.assertEqual(set(result), {"winner", "criteria", "reason", "agreed"})
        self.assertEqual(runtime.calls, 2)  # both orders
        bad = self.client.post("/v1/judge/compare", json={"a": {"session_id": "nope"}, "b": {"session_id": self.tokens[0]}})
        self.assertEqual(bad.status_code, 400)

        def missing(*args, **kwargs):
            raise RuntimeError("vLLM is required for LLM features.")

        class Broken:
            generate = staticmethod(missing)

        with patch("utils.llm_model_loader.get_llm_runtime", lambda *a, **k: Broken()):
            response = self.client.post("/v1/judge/compare", json={"a": {"session_id": self.tokens[0]},
                                                                  "b": {"session_id": self.tokens[1]}})
        self.assertEqual(response.status_code, 503)

    def test_the_benchmark_its_ab_and_fitting_the_fitness_run_as_jobs(self):
        runtime = FakeRuntime()
        with patch("utils.llm_model_loader.get_llm_runtime", lambda *a, **k: runtime):
            job = self.client.post("/v1/jobs/judge-benchmark", json={"stories": 2, "seed": 3}).json()
            done = _finished(self.client, job["id"], timeout=600)
            self.assertEqual(done["status"], "finished", done.get("error"))
            self.assertEqual(done["result"]["judged"], 2)
            self.assertTrue(4 <= done["result"]["engagement"] <= 9)
            self.assertEqual(done["progress"], {"generation": 2, "total": 2, "label": "stories judged"})

            job = self.client.post("/v1/jobs/judge-benchmark", json={
                "stories": 2, "pairwise": True, "variants": {"climax.enabled": False, "travel.fast_travel": "false"}}).json()
            done = _finished(self.client, job["id"], timeout=600)
            self.assertEqual(done["status"], "finished", done.get("error"))
            result = done["result"]
            self.assertEqual(result["pairs"], 2)
            self.assertEqual(result["a_wins"] + result["b_wins"] + result["ties"], 2)
            self.assertEqual(result["variants"], {"climax.enabled": False, "travel.fast_travel": "false"})
            self.assertTrue(store.config.get("climax.enabled"))  # this process never saw version B

            job = self.client.post("/v1/jobs/calibrate-fitness", json={"stories": 10, "seed": 1}).json()
            done = _finished(self.client, job["id"], timeout=600)
            self.assertEqual(done["status"], "finished", done.get("error"))
            calibration = done["result"]
            self.assertEqual(calibration["n"], 10)
            self.assertFalse(calibration["applied"])
            self.assertTrue(all(0.25 <= w <= 3.0 for w in calibration["term_weights"].values()))
            self.assertIn("term_weights:", calibration["yaml"])
        self.assertEqual(self._yaml("hyperparameters.yaml")["fitness"]["term_weights"], {})  # not applied
        kinds = {job["kind"] for job in self.client.get("/v1/jobs").json()["jobs"]}
        self.assertTrue({"judge-benchmark", "calibrate-fitness"} <= kinds)
        refused = [self.client.post("/v1/jobs/judge-benchmark", json={"stories": 2, "pairwise": True}),
                   self.client.post("/v1/jobs/calibrate-fitness", json={"stories": 5}),
                   self.client.post("/v1/jobs/judge-benchmark", json={"stories": 2, "engine_id": "nope"})]
        self.assertEqual([r.status_code for r in refused], [400, 422, 404])


class StudioHandlerTests(_CoverageSandbox):
    """The studio's new controls go through the API."""

    def test_handlers_go_through_the_api(self):
        import app
        with patch.object(app.API, "request", wraps=app.API.request) as spy:
            app.load_entity_yaml_ui("NPCs", "Merchant")
            app.failure_lint_ui()
            app.jobs_table_ui()
            app.active_config_ui()
            app.load_term_weights_ui()
            app.load_document("map")
            app.recorded_run_choices()
            app.recorded_world_choices()
        paths = {call.args[1] for call in spy.call_args_list}
        self.assertTrue({"/v1/entities/NPCs/Merchant/yaml", "/v1/lint/failures", "/v1/jobs", "/v1/config",
                         "/v1/fitness/term-weights", "/v1/documents/map", "/v1/records/runs",
                         "/v1/records/worlds"} <= paths, paths)

    def test_an_npcs_role_and_secret_from_the_form(self):
        import app
        values = app.load_entity_ui("NPCs", "Merchant")
        self.assertEqual(values[-2].value, app.INFERRED_ROLE)  # role: inferred
        self.assertIn("{npc} lets it slip", values[-1].value)  # the shipped secret
        app.save_entity_ui("NPCs", "Merchant", "Merchant", ["market"], "A dealer.", "", False, "", False, "", "", "",
                           False, 1.0, "informant", "{text: '{npc} knows.', talks: 1}")
        saved = next(c for c in self._yaml("npcs.yaml")["world_definition"]["characters"] if c["name"] == "Merchant")
        self.assertEqual((saved["role"], saved["secret"]), ("informant", {"text": "{npc} knows.", "talks": 1}))
        app.save_entity_ui("NPCs", "Merchant", "Merchant", ["market"], "A dealer.", "", False, "", False, "", "", "",
                           False, 1.0, app.INFERRED_ROLE, "")
        saved = next(c for c in self._yaml("npcs.yaml")["world_definition"]["characters"] if c["name"] == "Merchant")
        self.assertNotIn("role", saved)
        self.assertNotIn("secret", saved)

    def test_judging_and_term_weights_from_the_studio(self):
        import app
        token, state = sessions._start_game(Genome(rng=random.Random(7)), "studio-judge", None, None)
        self.addCleanup(sessions.SESSIONS.pop, token, None)
        sessions.play_action(token, sessions._player_options(state)[0].name)
        with patch("utils.llm_model_loader.get_llm_runtime", lambda *a, **k: FakeRuntime()):
            text = app.judge_session_ui(token)
        self.assertIn("### This playthrough: engagement", text)
        weights, message = app.save_term_weights_ui("climax: 1.5\n")
        self.assertEqual(yaml.safe_load(weights), {"climax": 1.5})
        self.assertIn("Saved 1", message)



class StudioFixesTests(_CoverageSandbox):
    """Studio handlers that lost or overwrote data, or offered controls that could not work (the gaps found
    while writing docs/FRONTEND.md): each now keeps what was written, or says why it cannot."""

    def _mission(self, key):  # as saved (the sandbox does not reload the active configuration)
        return yaml.safe_load((self.dir / "missions.yaml").read_text())["world_definition"]["missions"][key]

    def test_a_mission_is_never_replaced_by_a_new_one_and_keeps_its_weight_and_step(self):
        import gradio as gr
        import pandas as pd

        import app
        new = app.mission_form_ui(None)[-1]
        step = pd.DataFrame([["Get a shield", "Buy one at the market.", "item: Heavy Shield", ""]],
                            columns=app.STAGE_COLUMNS)
        with self.assertRaises(gr.Error):  # a taken key: refused, the mission there is untouched
            app.save_mission_form_ui(new, "shield_bearer", "Another", "", 1.0, "", "", step)
        with self.assertRaises(gr.Error):  # no key at all
            app.save_mission_form_ui(new, "", "Another", "", 1.0, "", "", step)
        self.assertEqual(self._mission("shield_bearer")["title"], "Find a shield")
        # Weight 0 (never drawn) is a weight; one step with a description stays a step.
        app.save_mission_form_ui(new, "never_drawn", "Never drawn", "", 0, "", "", step)
        saved = self._mission("never_drawn")
        self.assertEqual(saved["weight"], 0)
        self.assertEqual(saved["stages"][0]["description"], "Buy one at the market.")
        self.assertNotIn("goal", saved)
        with patch.object(app.API, "get", return_value={"mission": saved}):
            self.assertEqual(app.mission_form_ui("never_drawn")[3], 0.0)  # shown as 0, not 1
        # A step that holds only its goal: a one-goal mission, as before.
        state = app.mission_form_ui("shield_bearer")[-1]
        only_goal = pd.DataFrame([["Find a shield", "", "item: Heavy Shield", ""]], columns=app.STAGE_COLUMNS)
        app.save_mission_form_ui(state, "shield_bearer", "Find a shield", "", 1.0, "", "", only_goal)
        self.assertEqual(self._mission("shield_bearer")["goal"], {"item": "Heavy Shield"})

    def test_a_new_entry_never_replaces_one_of_the_same_name(self):
        import gradio as gr

        import app
        with self.assertRaises(gr.Error):
            app.save_entity_ui("NPCs", None, "Merchant", ["x"], "Someone else.", "", False, "", False, "", "", "",
                               False, 1.0)
        merchant = self.client.get("/v1/entities/NPCs/Merchant").json()
        self.assertNotEqual(merchant["descriptions"], ["Someone else."])

    def test_scheduling_is_for_events_and_items_only(self):
        import gradio as gr

        import app
        self.assertEqual([u["visible"] for u in app.policy_controls_ui("Locations")], [False, False])
        self.assertEqual([u["visible"] for u in app.policy_controls_ui("Events")], [True, True])
        with self.assertRaises(gr.Error):  # a clear message, not the API's 422
            app.save_entity_policy_ui("Locations", "Harbor", "trigger: random\n")

    def test_dialogue_variants_stay_variants(self):
        import pandas as pd

        import app
        details = self.client.get("/v1/entities/NPCs/Merchant/details").json()["details"]
        details["dialogue"] = [{"text": ["Hello.", "Welcome!"]}, {"text": "Bye."}]
        self.assertEqual(self.client.put("/v1/entities/NPCs/Merchant/details", json={"details": details}).status_code,
                         200)

        def save(first):
            rows = pd.DataFrame([["", first], ["", "Bye."]], columns=["Said when (condition, empty: always)", "Says"])
            app.save_details_ui("NPCs", "Merchant", details.get("known_locations") or [],
                                details.get("known_objects") or [], details.get("article"), rows, None, None, None,
                                None)
            return [line["text"] for line in
                    self.client.get("/v1/entities/NPCs/Merchant/details").json()["details"]["dialogue"]]

        self.assertEqual(save("Hello. / Welcome!"), [["Hello.", "Welcome!"], "Bye."])  # unchanged
        self.assertEqual(save("Hi. / Welcome!"), [["Hi.", "Welcome!"], "Bye."])  # edited in its row

    def test_a_cleared_story_rule_number_is_refused_not_kept(self):
        import gradio as gr

        import app
        values = list(app.load_rules_ui())
        values[app.REFERENCE["rule_fields"].index("route_weight")] = None
        with self.assertRaises(gr.Error):
            app.save_rules_ui(*values)

    def test_a_proposal_renamed_in_its_yaml_is_still_added(self):
        import gradio as gr

        import app
        text = yaml.safe_dump({"locations": {"Old Mill": {"descriptions": ["A mill."]}},
                               "characters": [{"name": "Miller"}], "objects": []})
        ticks, offered = app.sync_proposal_ui(text, [], [])
        self.assertEqual(ticks["value"], ["Place: Old Mill", "Person: Miller"])
        renamed = text.replace("Old Mill", "Windmill")
        ticks, offered = app.sync_proposal_ui(renamed, ["Person: Miller"], offered)  # Old Mill: ticked, renamed
        self.assertEqual(ticks["value"], ["Place: Windmill", "Person: Miller"])
        with self.assertRaises(gr.Error):  # a tick for a name no longer there is refused, not dropped
            app.add_proposal_ui(renamed, ["Place: Old Mill"])

    def test_the_map_never_saves_to_a_place_that_is_gone(self):
        import gradio as gr

        import app
        with self.assertRaises(gr.Error):
            app.save_borders_ui("Atlantis", ["Harbor"])
        values, extras = app._form_load("map", "borders", "Atlantis")  # falls back to a place there is
        self.assertIn(extras["context"], self.client.get("/v1/map").json()["places"])

    def test_the_behaviour_tables_are_drafts_of_their_own(self):
        import app
        *values, draft, status = app.behaviour_load_ui()
        self.assertIn('data-dirty="0"', status)
        npcs, places = values[:3], values[3:]
        draft, status = app.behaviour_edit_ui("npcs", draft, npcs[0], "every_visit", npcs[2])
        draft, status = app.behaviour_edit_ui("locations", draft, places[0], "every_visit", places[2])
        self.assertEqual(draft["dirty"], ["npcs", "locations"])
        # The people's rules saved: only their table reloads; the places' edits stay unsaved.
        *reloaded, draft, status = app.behaviour_reload_ui("npcs", draft)
        self.assertEqual(len(reloaded), 3)
        self.assertEqual(draft["dirty"], ["locations"])
        self.assertIn('data-dirty="1"', status)

    def test_dropdowns_offer_the_world_as_it_is_now(self):
        import app
        choices = app.fresh_choices_ui("Locations", [app.NONE], "A place deleted meanwhile")["choices"]
        self.assertEqual(choices[0], app.NONE)
        self.assertIn("Harbor", choices)
        self.assertIn("A place deleted meanwhile", choices)  # the form's value stays a choice

    def test_the_game_survives_a_reload_and_more_choices_are_never_hidden(self):
        import app
        token, _ = sessions._start_game(Genome(rng=random.Random(7)), "studio-reload", None, None)
        self.addCleanup(sessions.SESSIONS.pop, token, None)
        size = len(app.PLAY_PARTS) + 1  # the game screen and the story
        restored = app.restore_game_ui(token, size)
        self.assertEqual((restored[0], restored[-1], len(restored)), (token, token, size + 2))
        gone = app.restore_game_ui("no-such-game", size)
        self.assertEqual((gone[0], gone[-1]), ("", ""))  # the API forgot it: the kept id goes too
        state = self.client.get(f"/v1/sessions/{token}").json()
        state["actions"] = [f"choice {n}" for n in range(app.CHOICE_BUTTONS + 2)]
        self.assertIn("2 more choice(s)", app._play_screen(state)[1].value)

    def test_reset_asks_first_and_a_report_s_delivery_comes_from_the_save_game(self):
        import app
        shown, text = app.reset_question_ui("someone")
        self.assertTrue(shown["visible"])
        self.assertIn("cannot be undone", text)
        self.assertEqual(app._report_delivery({"profile": "nobody-yet", "report_id": "r1"}), "")


if __name__ == "__main__":
    unittest.main()
