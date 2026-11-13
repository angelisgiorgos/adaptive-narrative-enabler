"""Sessions and the player's progression (core_engine/progression.py, core_engine/scope.py):

- one-time actions: remembered when taken; gone after a success, or after a failure unless
  they may be retried (then they stay in the session);
- every session saves the player's progression and reports to the UNP: stats, inventory and
  the story's progression (the goal achieved, or the mission failed);
- every session starts with a start and a goal; the start varies between sessions;
- an achieved goal leaves the save game's goal pool (each goal once per save game);
- a session's cast: what its goal does not need may stay out.
"""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import copy
import json
import os
import random
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import main as engine
from backend import sessions
from core_engine import Action, Character, Event, GameState, Genome, Location, Mission, Outcome, WorldGraph, starts
from core_engine import progression
from core_engine.combat import CheckResult
from core_engine.variables import VariableStore
from utils.config_loader import config


def _genome(seed=11):
    random.seed(seed)
    genome = Genome()
    genome.event_prob = 0.0
    genome.chaos_bias = 0.0
    return genome


def _rolls(*passes):
    results = iter(passes)

    def roll(combat, stat, modifier=0, inventory=(), step=0):
        passed = next(results)
        return CheckResult(stat, 1 if passed else 100, combat.target(stat, modifier, inventory, step), passed, step)

    return patch("core_engine.combat.CombatSystem.roll", roll)


def _names(state):
    return [action.name for action in state.ranked_available_actions()]


def _choose(state, name):
    return state.step(_names(state).index(name))


def _set(section, **values):
    return patch.dict(config._config, {section: {**copy.deepcopy(config.get(section) or {}), **values}})


def _world(*missions):
    """Gate - Yard - Market: a lock picked once (retryable or not), a coin box, a shop."""
    world = WorldGraph()
    gate = Location("Gate", ["gate"])
    gate.actions = [
        Action("Pick the lock", [Outcome("The lock clicks open.", tags=["success"], on_check="pass"),
                                 Outcome("The pick snaps.", tags=["failure"], on_check="fail")],
               repeat="once", skill_check={"stat": "stealth"}, source="location", owner="Gate"),
        Action("Force the shutter", [Outcome("It gives way.", tags=["success"], on_check="pass"),
                                     Outcome("It holds.", tags=["failure"], on_check="fail")],
               repeat="once", retry_on_failure=True, skill_check={"stat": "attack"}, source="location", owner="Gate"),
        Action("Wait", [Outcome("You wait.", tags=["neutral"])], repeat="always", source="location", owner="Gate"),
    ]
    yard = Location("Yard", ["yard"])
    yard.actions = [Action("Open the coin box", [Outcome("Coins!", tags=["success"], coin_change=5)], repeat="once",
                           source="location", owner="Yard")]
    market = Location("Market", ["market"])
    market.actions = [Action("Buy bread", [Outcome("Fresh bread.", tags=["neutral"])], repeat="always",
                             source="location", owner="Market")]
    for location in (gate, yard, market):
        world.add_location(location)
    gate.connect(yard)
    yard.connect(market)
    for mission in missions:
        world.add_mission(mission)
    return world


class _Progression(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="ane-progression-")
        self.store = VariableStore(os.path.join(self.folder, "variables.json"))
        for patcher in (patch.dict(config._config["unexpected_events"], {"enabled": False}),
                        patch.dict(os.environ, {"ANE_SAVE_DIR": self.folder}),
                        _set("session_report", url="")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _state(self, *missions, start="Gate", profile="ada", mission=None):
        return GameState(_world(*missions), _genome(), start=start, rng=random.Random(1), store=self.store,
                         profile=profile, mission=mission)


class UniqueActionTests(_Progression):
    def test_one_time_actions_are_remembered_used_up_or_kept(self):
        state = self._state()
        with _rolls(False):
            _choose(state, "Pick the lock")             # failed, not retryable: used up
        self.assertNotIn("Pick the lock", _names(state))
        with _rolls(False):
            _choose(state, "Force the shutter")         # failed, retryable: kept in the session
        self.assertIn("Force the shutter", _names(state))
        with _rolls(True):
            _choose(state, "Force the shutter")         # succeeded: used up
        self.assertNotIn("Force the shutter", _names(state))
        _choose(state, "Move to Yard")
        _choose(state, "Open the coin box")              # no check: succeeded, used up
        _choose(state, "Return to Gate")
        _choose(state, "Return to Yard")
        self.assertNotIn("Open the coin box", _names(state))
        log = [(e["action"], e["succeeded"], e["kept"]) for e in state.unique_actions()]
        self.assertEqual(log, [("Pick the lock", False, False), ("Force the shutter", False, True),
                               ("Force the shutter", True, False), ("Open the coin box", True, False)])
        self.assertEqual(state.unique_actions()[0]["where"], "Gate")


class ProgressionTests(_Progression):
    def test_every_session_saves_the_progression_and_its_report(self):
        state = self._state(Mission("shop", {"action": "Buy bread"}, title="Do the shopping"))
        self.assertEqual((state.start_location, state.mission.name), ("Gate", "shop"))  # a start and a goal
        _choose(state, "Move to Yard")
        _choose(state, "Open the coin box")
        _choose(state, "Move to Market")
        _choose(state, "Buy bread")
        self.assertTrue(state.won and state.ended)
        report = state.session_report
        self.assertEqual(report["format"], progression.FORMAT)
        self.assertEqual((report["outcome"], report["goal"]["key"], report["start_location"]), ("achieved", "shop", "Gate"))
        self.assertEqual(report["player"]["coins"], state.coins)
        self.assertEqual(report["player"]["inventory"], sorted(state.inventory))
        self.assertEqual(set(report["player"]["stats"]), set(state.stats.names))
        self.assertEqual(report["progression"]["goals_achieved"], ["shop"])
        self.assertEqual(report["story"]["unique_actions"][0]["action"], "Open the coin box")
        saved = self.store.progression("ada")
        self.assertEqual(saved["goals_achieved"], ["shop"])
        self.assertEqual([s["report_id"] for s in saved["sessions"]], [report["report_id"]])
        self.assertEqual(saved["playthroughs"], 1)
        # A session the player stops counts too: its mission failed.
        state = self._state(Mission("shop2", {"action": "Buy bread"}, title="Shop again"))
        _choose(state, "Wait")
        self.assertTrue(state.end_session())
        self.assertFalse(state.end_session())  # once
        self.assertEqual(state.session_report["outcome"], "failed")
        self.assertEqual(state.end_kind, "abandoned")
        self.assertEqual([s["outcome"] for s in self.store.sessions("ada")], ["achieved", "failed"])
        self.assertEqual(self.store.progression("ada")["playthroughs"], 2)

    def test_an_achieved_goal_leaves_the_pool_once_per_save_game(self):
        missions = (Mission("shop", {"action": "Buy bread"}), Mission("loot", {"action": "Open the coin box"}))
        self.store.complete_goal("ada", "shop")
        for seed in range(8):
            state = GameState(_world(*missions), _genome(), start="Gate", rng=random.Random(seed), store=self.store,
                              profile="ada")
            self.assertEqual(state.mission.name, "loot")
        with self.assertRaises(ValueError):
            self._state(*missions, mission="shop")
        other = self._state(*missions, profile="bea", mission="shop")  # another save game: open
        self.assertEqual(other.mission.name, "shop")
        self.store.complete_goal("ada", "loot")
        self.store.complete_goal("ada", "loot")
        self.assertEqual(self.store.completed_goals("ada"), ["shop", "loot"])  # each once
        state = self._state(*missions)
        self.assertIsNone(state.mission)
        self.assertIn("[MISSION] Every goal of this world has been achieved in this save game: explore freely.",
                      state.history)
        self.store.reset("ada")  # a new save game
        self.assertEqual(self.store.completed_goals("ada"), [])
        # Without a store (simulations, evolution) nothing is excluded.
        free = GameState(_world(*missions), _genome(), start="Gate", rng=random.Random(1), mission="shop")
        self.assertEqual(free.mission.name, "shop")


class _UNP(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):  # noqa: N802 - http.server API
        body = self.rfile.read(int(self.headers["content-length"]))
        _UNP.received.append((json.loads(body), self.headers.get("authorization")))
        self.send_response(202)
        self.end_headers()

    def log_message(self, *args):
        pass


class UNPTests(_Progression):
    def setUp(self):
        super().setUp()
        _UNP.received = []
        self.server = HTTPServer(("127.0.0.1", 0), _UNP)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/sessions"

    def test_the_report_reaches_the_unp_and_its_delivery_is_noted(self):
        with _set("session_report", url=self.url), patch.dict(os.environ, {"ANE_UNP_TOKEN": "t0k"}):
            state = self._state(Mission("shop", {"action": "Buy bread"}))
            state.end_session()  # the report is delivered in the background
            for thread in threading.enumerate():
                if thread.name.startswith("unp-report-"):
                    thread.join(10)
        report, authorization = _UNP.received[0]
        self.assertEqual((report["report_id"], report["outcome"]), (state.session_report["report_id"], "failed"))
        self.assertEqual(authorization, "Bearer t0k")
        delivery = self.store.sessions("ada")[-1]["delivery"]
        self.assertEqual((delivery["sent"], delivery["status"]), (True, 202))
        unreachable = progression.send(report, url="http://127.0.0.1:9/none", timeout=1)
        self.assertFalse(unreachable["sent"])
        self.assertTrue(unreachable["error"])


class StartVarianceTests(_Progression):
    def test_the_latest_start_is_the_least_likely(self):
        candidates = [Location(name, []) for name in ("A", "B", "C", "D")]
        for loc in candidates:
            loc.start_weight = 1.0
        chances = starts.probabilities(candidates, ["C", "B", "A"])  # A the latest
        self.assertLess(chances["A"], 0.07)
        self.assertLess(chances["A"], chances["B"])
        self.assertLess(chances["B"], chances["C"])
        self.assertLess(chances["C"], chances["D"])
        self.assertGreater(chances["A"], 0)  # possible, rarely

    def test_sessions_rarely_start_where_the_last_one_did(self):
        config.reload()
        engine.refresh_runtime_settings()
        starts_seen = []
        for seed in range(40):
            genome = Genome(rng=random.Random(seed))
            state = GameState(engine.build_world(genome), genome, rng=random.Random(500 + seed), store=self.store,
                              profile="carla")
            starts_seen.append(state.start_location)
            state.end_session()
        repeats = sum(a == b for a, b in zip(starts_seen, starts_seen[1:]))
        self.assertLessEqual(repeats, 6, starts_seen)   # a uniform draw over 4 starts would repeat ~10 times
        self.assertGreaterEqual(len(set(starts_seen)), 3)


class CastTests(_Progression):
    def test_a_session_leaves_out_what_its_goal_does_not_need(self):
        config.reload()
        engine.refresh_runtime_settings()
        genome = Genome(rng=random.Random(3))
        state = GameState(engine.build_world(genome), genome, rng=random.Random(3))
        cast = state.scope()
        left_out = cast["left_out"]
        self.assertTrue(left_out["npcs"] and len(left_out["npcs"]) < len(state.world.characters))
        required = state._required_content()
        for kind in ("npcs", "locations", "items", "events"):
            self.assertFalse(set(left_out[kind]) & required[kind], kind)
        self.assertIn(state.start_location, required["locations"])
        npc = next(c for c in state.world.characters if c.name == left_out["npcs"][0])
        self.assertFalse(state.is_enabled(npc))
        self.assertTrue(state._mission_viable_from(state.mission, state.start_location))
        # Every kept place stays reachable from the start.
        kept = set(state.world.locations) - set(left_out["locations"])
        self.assertTrue(state._reachable(kept))
        # Sessions differ in their cast.
        casts = set()
        for seed in range(6):
            genome = Genome(rng=random.Random(seed))
            other = GameState(engine.build_world(genome), genome, rng=random.Random(seed))
            casts.add(tuple(other.scope()["left_out"]["npcs"]))
        self.assertGreater(len(casts), 1)
        with _set("session_scope", enabled=False):
            whole = GameState(engine.build_world(genome), genome, rng=random.Random(3))
        self.assertEqual(whole.scope()["left_out"], {"locations": [], "npcs": [], "items": [], "events": []})

    def test_a_small_world_keeps_everything(self):
        world = _world(Mission("shop", {"action": "Buy bread"}))
        world.add_character(Character("Baker", ["market"], unique=True, known_locations=["Market"]))
        state = GameState(world, _genome(), start="Gate", rng=random.Random(1))
        self.assertEqual(sum(len(v) for v in state.scope()["left_out"].values()), 0)


class SessionAPITests(_Progression):
    def setUp(self):
        super().setUp()
        self.client = TestClient(api.api)
        self.token, state = sessions._start_game(Genome(rng=random.Random(4)), "dora", None, None)
        self.addCleanup(sessions.SESSIONS.pop, self.token, None)

    def test_end_report_progression_and_resend(self):
        self.assertEqual(self.client.get(f"/v1/sessions/{self.token}/report").status_code, 409)
        state = self.client.get(f"/v1/sessions/{self.token}").json()
        self.assertIn("cast", state)
        self.assertEqual(state["unique_actions"], [])
        ended = self.client.post(f"/v1/sessions/{self.token}/end")
        self.assertEqual(ended.status_code, 200, ended.text)
        self.assertTrue(ended.json()["ended"])
        report = ended.json()["report"]
        self.assertEqual(report["session_id"], self.token)
        self.assertEqual(report["outcome"], "failed")
        self.assertEqual(self.client.get(f"/v1/sessions/{self.token}/report").json()["report_id"], report["report_id"])
        saved = self.client.get("/v1/progression", params={"profile": "dora"}).json()
        self.assertEqual(saved["sessions"][-1]["report_id"], report["report_id"])
        resend = self.client.post(f"/v1/progression/reports/{report['report_id']}/send", params={"profile": "dora"})
        self.assertEqual(resend.status_code, 200, resend.text)
        self.assertFalse(resend.json()["sent"])  # no UNP endpoint configured here
        self.assertEqual(self.client.post("/v1/progression/reports/nope/send", params={"profile": "dora"}).status_code, 400)
        self.assertEqual(self.client.post("/v1/sessions/nope/end").status_code, 404)

    def test_an_achieved_goal_is_not_open_again(self):
        VariableStore().complete_goal("dora", "shield_bearer")
        quests = {q["key"]: q for q in self.client.get("/v1/missions/playable", params={"profile": "dora"}).json()["missions"]}
        self.assertEqual((quests["shield_bearer"]["available"], quests["shield_bearer"]["achieved"]), (False, True))
        with self.assertRaises(Exception):
            sessions._start_game(Genome(rng=random.Random(5)), "dora", None, None, mission="shield_bearer")

    def test_the_studio_ends_a_session_and_shows_the_save_game(self):
        import app
        scene, _, _, _, people = app.end_session_ui(self.token)
        self.assertIn("### Session report", people)
        text, table = app.progression_ui("dora")
        self.assertIn("Save game `dora`", text)
        self.assertEqual(len(table), 1)
        self.assertIn("Not sent", app.resend_report_ui("dora"))


if __name__ == "__main__":
    unittest.main()
