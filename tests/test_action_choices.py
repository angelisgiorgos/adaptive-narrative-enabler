"""Stable action identities, structured choices, id-based play with optimistic state versions, and the
author-only decision diagnostics (core_engine/choices.py, core_engine/diagnostics.py)."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import copy
import json
import os
import random
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import main as engine
from backend import sessions
from core_engine import Action, Character, Event, GameState, Genome, Location, Outcome, WorldGraph
from core_engine import choices, snapshot
from core_engine.builder import build_world
from core_engine.diagnostics import REASONS, decision_trace
from core_engine.variables import VariableStore
from utils.config_loader import config


def _genome():
    random.seed(9)
    genome = Genome()
    genome.event_prob = 0.0
    genome.chaos_bias = 0.0
    return genome


def _npc_action(npc, label, text, **kwargs):
    """An NPC's action as the builder makes it: "(NPC) label", keyed by its authored name."""
    action = Action(f"({npc}) {label}", [Outcome(text, tags=["social"])], character_name=npc, source="npc",
                    owner=npc, **kwargs)
    action.key = label
    action.content_ref = f"world_definition.characters[{npc}].actions[0]"
    return action


def _place_action(place, label, *outcomes, **kwargs):
    action = Action(label, list(outcomes) or [Outcome(f"{label}.", tags=["neutral"])], source="location",
                    owner=place, **kwargs)
    action.key = label
    return action


def _world(*gate_actions, npcs=(), exit_locked=False):
    world = WorldGraph()
    for name in ("Gate", "Yard", "Tower"):
        world.add_location(Location(name, [name.lower()]))
    world.locations["Gate"].actions = [*gate_actions, _place_action("Gate", "Wait", repeat="always")]
    world.locations["Yard"].actions = [_place_action("Yard", "Rest", repeat="always")]
    world.locations["Tower"].actions = [_place_action("Tower", "Look out", repeat="always")]
    world.locations["Tower"].exit_locked = exit_locked
    world.connect("Gate", "Yard")
    world.connect("Gate", "Tower")
    for npc in npcs:
        world.add_character(npc)
    return world


def _state(world, start="Gate", **kwargs):
    state = GameState(world, _genome(), start=start, rng=random.Random(3), **kwargs)
    for char in world.characters:
        if char.unique:
            state.npc_homes[char.name] = start
    state._scene_dirty = True
    return state


def _offered(state):
    options = sessions._player_options(state)
    return dict(zip(choices.assign_ids(options, state), options))


def _entry(trace, action_id):
    return next(entry for entry in trace["actions"] if entry["id"] == action_id)


def _codes(entry):
    return [reason["code"] for reason in entry["reasons"]]


def _fingerprint(state):
    """Everything a diagnostic call must leave as it was: the story (its exact snapshot), its dice,
    the global dice, and what the player has seen."""
    return (json.dumps(snapshot.dump(state), sort_keys=True), state.rng.getstate(), random.getstate(),
            state.state_version, len(state.history), state.active_event, set(state.encounters))


class _Quiet(unittest.TestCase):
    CONVERSATIONS = False

    def setUp(self):
        for patcher in (
            patch.dict(config._config["unexpected_events"], {"enabled": False}),
            patch.dict(config._config, {"npc_roles": {**(config.get("npc_roles") or {}), "enabled": False}}),
            patch.dict(config._config["conversations"], {"enabled": self.CONVERSATIONS}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


class IdentityTests(_Quiet):
    def test_two_npcs_with_the_same_action_label(self):
        oracle = Character("Oracle", ["gate"], unique=True,
                           actions=[_npc_action("Oracle", "Ask about the crown", "The Oracle points north.")])
        guard = Character("Guard", ["gate"], unique=True,
                          actions=[_npc_action("Guard", "Ask about the crown", "The guard shrugs.")])
        state = _state(_world(npcs=(oracle, guard)))
        offered = _offered(state)
        self.assertIn("npc:oracle:ask-about-the-crown", offered)
        self.assertIn("npc:guard:ask-about-the-crown", offered)
        details = {c["id"]: c for c in sessions.choice_details(state)}
        self.assertEqual(details["npc:guard:ask-about-the-crown"]["source"], {"kind": "npc", "name": "Guard"})
        self.assertEqual(details["npc:guard:ask-about-the-crown"]["category"], "person")
        state.take_option(offered["npc:guard:ask-about-the-crown"])
        self.assertIn("The guard shrugs.", "\n".join(state.history))
        self.assertNotIn("The Oracle points north.", "\n".join(state.history))

    def test_an_action_named_like_a_travel_option_is_told_apart(self):
        state = _state(_world())
        travel = state.narrate("travel_new", stable="Yard", location="Yard")
        world = _world(_place_action("Gate", travel, Outcome("You only look toward the yard.", tags=["neutral"]),
                                     repeat="always"))
        state = _state(world)
        offered = _offered(state)
        self.assertEqual(sorted(a.name for a in offered.values()).count(travel), 2)   # the same text twice
        self.assertIn("go:yard", offered)
        key = f"location:gate:{choices.slug(travel)}"
        self.assertIn(key, offered)
        state.take_option(offered[key])
        self.assertEqual(state.current.name, "Gate")                                   # the place's, not travel
        self.assertIn("You only look toward the yard.", "\n".join(state.history))

    def test_runtime_actions_have_stable_ids(self):
        greeter = Character("Oracle", ["gate"], unique=True,
                            actions=[_npc_action("Oracle", "Ask about the crown", "North.")])
        with patch.dict(config._config["conversations"], {"enabled": True}):
            state = _state(_world(npcs=(greeter,)))
            offered = _offered(state)
            self.assertIn("approach:oracle", offered)
            self.assertIn("go:yard", offered)
            approach = sessions.choice_details(state)
            approach = next(c for c in approach if c["id"] == "approach:oracle")
            self.assertIn("npc:oracle:ask-about-the-crown", approach["members"])   # with talking to them
            self.assertIn("talk:oracle", approach["members"])
            self.assertFalse(approach["takes_turn"])
            state.take_option(offered["approach:oracle"])
            offered = _offered(state)
            self.assertIn("leave:oracle", offered)
            self.assertIn("npc:oracle:ask-about-the-crown", offered)

    def test_ids_do_not_depend_on_order_or_text(self):
        first, second = (_place_action("Gate", "Pray"), _place_action("Gate", "Pray"))
        first.content_ref, second.content_ref = "world_definition.locations.Gate.actions[0]", "...actions[1]"
        ids = choices.assign_ids([first, second])
        self.assertEqual(len(set(ids)), 2)                                    # same key: told apart, not merged
        self.assertEqual(choices.assign_ids([second, first]), ids[::-1])      # by where they are written
        renamed = copy.deepcopy(first)
        renamed.name = "Pray to the old gods"                                 # a template filled in, a new text
        self.assertEqual(choices.base_id(renamed), choices.base_id(first))

    def test_the_builder_gives_every_authored_action_its_identity(self):
        world = build_world(Genome(rng=random.Random(0)))
        authored = [a for loc in world.locations.values() for a in loc.actions] \
            + [a for c in world.characters for a in c.actions] + [a for o in world.objects for a in o.actions] \
            + [a for e in world.events.values() for a in e.actions]
        self.assertTrue(authored)
        self.assertTrue(all(a.key and a.content_ref for a in authored))
        explicit = next(a for c in config.get("world_definition.characters") for a in c.get("actions") or []
                        if a.get("id"))
        built = next(a for c in world.characters for a in c.actions if a.key == explicit["id"])
        self.assertTrue(choices.base_id(built).endswith(":" + choices.slug(explicit["id"])))
        for entity in [*world.locations.values(), *world.characters, *world.objects]:
            ids = [choices.base_id(a) for a in entity.actions]
            self.assertEqual(len(ids), len(set(ids)), entity.name)

    def test_duplicate_explicit_ids_are_refused(self):
        from core_engine.content_rules import action_errors

        errors = action_errors(config.get("world_definition"), "locations", [
            {"id": "pray", "name": "Pray", "outcomes": [{"desc": "Silence."}]},
            {"id": "pray", "name": "Pray again", "outcomes": [{"desc": "Silence."}]}])
        self.assertTrue(any("`id` `pray`" in error for error in errors), errors)


class DiagnosticsTests(_Quiet):
    def test_condition_gated_and_resource_gated_actions(self):
        lamp = _place_action("Gate", "Light the lamp", enabled_if={"has_item": "Lamp"})
        bribe = _place_action("Gate", "Bribe the watch", required_coins=500)
        key = _place_action("Gate", "Open the gate", required_object="Gate Key")
        state = _state(_world(lamp, bribe, key))
        trace = decision_trace(state)
        lamp_entry = _entry(trace, "location:gate:light-the-lamp")
        self.assertEqual((lamp_entry["status"], _codes(lamp_entry)), ("blocked", ["condition_false"]))
        failing = lamp_entry["reasons"][0]["detail"]["failing"]
        self.assertEqual(failing[0]["kind"], "has_item")
        self.assertEqual((failing[0]["name"], failing[0]["actual"]), ("Lamp", False))
        coins = _entry(trace, "location:gate:bribe-the-watch")
        self.assertEqual(_codes(coins), ["insufficient_coins"])
        self.assertEqual(coins["reasons"][0]["detail"], {"need": 500, "have": state.coins})
        self.assertEqual(_codes(_entry(trace, "location:gate:open-the-gate")), ["missing_item"])
        state.inventory.add("Lamp")
        state.coins = 600
        trace = decision_trace(state)
        self.assertNotEqual(_entry(trace, "location:gate:light-the-lamp")["status"], "blocked")
        self.assertNotEqual(_entry(trace, "location:gate:bribe-the-watch")["status"], "blocked")

    def test_one_time_and_repeatable_actions(self):
        once = _place_action("Gate", "Read the inscription", repeat="once")
        visit = _place_action("Gate", "Ring the bell", repeat="per_visit")
        state = _state(_world(once, visit))
        for label in ("location:gate:read-the-inscription", "location:gate:ring-the-bell"):
            state.take_option(_offered(state)[label])
        trace = decision_trace(state)
        self.assertEqual(_codes(_entry(trace, "location:gate:read-the-inscription")), ["used_once"])
        self.assertEqual(_codes(_entry(trace, "location:gate:ring-the-bell")), ["done_this_visit"])
        self.assertNotEqual(_entry(trace, "location:gate:wait")["status"], "blocked")   # repeat: always
        state.take_option(_offered(state)["go:yard"])
        state.take_option(next(a for i, a in _offered(state).items() if i.startswith("go:gate")))
        trace = decision_trace(state)                                   # a new visit: the bell again, not the inscription
        self.assertNotEqual(_entry(trace, "location:gate:ring-the-bell")["status"], "blocked")
        self.assertEqual(_codes(_entry(trace, "location:gate:read-the-inscription")), ["used_once"])

    def test_npc_present_but_action_filtered_by_relevance(self):
        oracle = Character("Oracle", ["gate"], unique=True,
                           actions=[_npc_action("Oracle", "Ask about the weather", "Rain, maybe.", repeat="always")])
        many = [_place_action("Gate", f"Study carving {n}", repeat="always") for n in range(6)]
        with patch.dict(config._config["player_options"], {"limit": 3, "min": 1, "relevance": 1.0,
                                                           "focus_relevance": 1.0}):
            state = _state(_world(*many, npcs=(oracle,)))
            trace = decision_trace(state)
        oracle_row = next(row for row in trace["entities"] if row["name"] == "Oracle")
        self.assertEqual(oracle_row["status"], "in_scene")                          # present and visible
        entry = _entry(trace, "npc:oracle:ask-about-the-weather")
        self.assertEqual(entry["status"], "hidden", entry)
        self.assertTrue(set(_codes(entry)) & {"below_relevance", "choice_limit", "kind_limit"}, entry)
        hidden = [e for e in trace["actions"] if e["status"] == "hidden"]
        self.assertTrue(any("below_relevance" in _codes(e) for e in hidden), hidden)
        self.assertLessEqual(len(trace["shown"]), 3)
        self.assertEqual(trace["limits"]["limit"], 3)
        self.assertTrue(all(REASONS[c][0] == "shown" for e in trace["actions"] if e["status"] == "shown"
                            for c in _codes(e)))

    def test_npc_choices_behind_an_approach(self):
        oracle = Character("Oracle", ["gate"], unique=True,
                           actions=[_npc_action("Oracle", "Ask about the crown", "North.")])
        with patch.dict(config._config["conversations"], {"enabled": True}):
            state = _state(_world(npcs=(oracle,)))
            trace = decision_trace(state)
        entry = _entry(trace, "npc:oracle:ask-about-the-crown")
        self.assertEqual((entry["status"], _codes(entry)), ("hidden", ["behind_approach"]))
        self.assertEqual(entry["reasons"][0]["detail"]["approach"], "approach:oracle")
        self.assertIn("approach:oracle", trace["shown"])

    def test_locked_exits(self):
        state = _state(_world(exit_locked=True), start="Tower")
        trace = decision_trace(state)
        gate = _entry(trace, "go:gate")
        self.assertEqual((gate["status"], _codes(gate)), ("blocked", ["exit_locked"]))

    def test_a_mandatory_encounter_is_reported_not_started(self):
        cutthroat = Character("Cutthroat", ["gate"], unique=True, encounter_event="ambush",
                              actions=[_npc_action("Cutthroat", "Haggle", "They laugh.")])
        world = _world(npcs=(cutthroat,))
        world.add_event(Event("ambush", title="Ambush", descriptions=["A blade in the dark."], actions=[
            Action("Fight", [Outcome("They flee.", tags=["combat", "success"], remove_npc=True)], source="event",
                   owner="ambush"),
            Action("Stand still", [Outcome("They size you up.")], source="event", owner="ambush")]))
        state = _state(world)
        before = _fingerprint(state)
        trace = decision_trace(state)
        self.assertEqual(_fingerprint(state), before)            # the encounter did not start for the player
        self.assertIsNone(state.active_event)
        self.assertEqual(trace["mode"], "event")
        self.assertEqual(trace["event"]["cause"], "encounter")
        self.assertTrue(any(note["code"] == "event_would_start" for note in trace["notes"]))
        self.assertEqual(_codes(_entry(trace, "location:gate:wait")), ["encounter"])
        self.assertEqual(sorted(trace["shown"]), ["event:ambush:fight", "event:ambush:stand-still"])
        sessions._player_options(state)                           # the player sees the scene: now it starts
        self.assertEqual(state.active_event.name, "ambush")
        self.assertEqual(sorted(_offered(state)), ["event:ambush:fight", "event:ambush:stand-still"])

    def test_diagnostics_change_nothing(self):
        saves = os.path.join(self._tmp(), "vars.json")
        state = engine.GameState(engine.build_world(_genome()), _genome(), rng=random.Random(5),
                                 store=VariableStore(saves))
        for _ in range(4):
            state.take_option(sessions._player_options(state)[0])
        sessions._player_options(state)                           # the scene as the player has it
        saved = Path(saves).read_text(encoding="utf-8") if os.path.exists(saves) else None
        before, rng = _fingerprint(state), random.getstate()
        trace = decision_trace(state)
        self.assertEqual(_fingerprint(state), before)
        self.assertEqual(random.getstate(), rng)
        self.assertEqual(saved, Path(saves).read_text(encoding="utf-8") if os.path.exists(saves) else None)
        self.assertEqual(trace["shown"], list(_offered(state)))   # the same choices the player has
        self.assertEqual(_fingerprint(state), before)

    def _tmp(self):
        import tempfile

        if not hasattr(self, "_dir"):
            self._dir = tempfile.mkdtemp(prefix="ane-diag-")
            self.addCleanup(__import__("shutil").rmtree, self._dir, True)
        return self._dir


class SessionApiTests(_Quiet):
    def setUp(self):
        super().setUp()
        self.client = TestClient(api.api)
        oracle = Character("Oracle", ["gate"], unique=True, actions=[
            _npc_action("Oracle", "Ask about the crown", "North.", repeat="always")])
        guard = Character("Guard", ["gate"], unique=True, actions=[
            _npc_action("Guard", "Ask about the crown", "A shrug.", repeat="always")])
        self.state = _state(_world(npcs=(oracle, guard)))
        self.token = sessions._register_session(self.state, "api")
        self.addCleanup(sessions.SESSIONS.pop, self.token, None)

    def _get(self):
        response = self.client.get(f"/v1/sessions/{self.token}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_choices_are_structured_and_players_see_no_diagnostics(self):
        play = self._get()
        self.assertEqual([c["name"] for c in play["choices"]], play["actions"])     # aligned with the names
        for choice in play["choices"]:
            self.assertTrue({"id", "label", "category", "requirements", "skill_check", "failure_risk", "available",
                             "check", "source"} <= set(choice))
            self.assertTrue(choice["available"])
            self.assertFalse({"reasons", "status", "stage", "score", "rank"} & set(choice))
        self.assertNotIn("decisions", play)
        self.assertIsInstance(play["state_version"], int)

    def test_action_id_with_state_version_and_stale_submissions(self):
        play = self._get()
        version = play["state_version"]
        guard = "npc:guard:ask-about-the-crown"
        self.assertIn(guard, [c["id"] for c in play["choices"]])
        taken = self.client.post(f"/v1/sessions/{self.token}/actions",
                                 json={"action_id": guard, "state_version": version})
        self.assertEqual(taken.status_code, 200, taken.text)
        self.assertIn("A shrug.", taken.json()["last_turn"])
        self.assertGreater(taken.json()["state_version"], version)
        turns = len(self.state.history)
        stale = self.client.post(f"/v1/sessions/{self.token}/actions",
                                 json={"action_id": guard, "state_version": version})   # the old options
        self.assertEqual(stale.status_code, 409, stale.text)
        detail = stale.json()["detail"]
        self.assertEqual(detail["code"], "stale_state")
        self.assertEqual(detail["state_version"], taken.json()["state_version"])
        self.assertEqual(detail["offered"], [c["id"] for c in self._get()["choices"]])
        self.assertEqual(len(self.state.history), turns)                                  # nothing was done
        unknown = self.client.post(f"/v1/sessions/{self.token}/actions",
                                   json={"action_id": "npc:nobody:ask", "state_version": detail["state_version"]})
        self.assertEqual((unknown.status_code, unknown.json()["detail"]["code"]), (409, "action_unavailable"))
        self.assertEqual(len(self.state.history), turns)
        # The UNP changes the player: options shown before are stale too.
        current = self._get()["state_version"]
        self.assertEqual(self.client.patch(f"/v1/sessions/{self.token}/player", json={"coins": 3}).status_code, 200)
        refused = self.client.post(f"/v1/sessions/{self.token}/actions",
                                   json={"action_id": guard, "state_version": current})
        self.assertEqual(refused.json()["detail"]["code"], "stale_state")

    def test_the_name_api_still_works(self):
        name = next(c["name"] for c in self._get()["choices"] if c["id"] == "npc:oracle:ask-about-the-crown")
        taken = self.client.post(f"/v1/sessions/{self.token}/actions", json={"action": name})
        self.assertEqual(taken.status_code, 200, taken.text)
        self.assertIn("North.", taken.json()["last_turn"])
        self.assertEqual(self.client.post(f"/v1/sessions/{self.token}/actions", json={"action": "Fly"}).status_code,
                         400)
        self.assertEqual(self.client.post(f"/v1/sessions/{self.token}/actions", json={}).status_code, 422)

    def test_the_decision_trace_endpoint(self):
        before = _fingerprint(self.state)
        response = self.client.get(f"/v1/sessions/{self.token}/decisions")
        self.assertEqual(response.status_code, 200, response.text)
        trace = response.json()
        self.assertEqual(_fingerprint(self.state), before)
        self.assertEqual(trace["shown"], [c["id"] for c in self._get()["choices"]])
        self.assertEqual(trace["state_version"], self.state.state_version)
        self.assertTrue(trace["codes"] and trace["entities"] and trace["limits"]["limit"])
        for entry in trace["actions"]:
            self.assertIn(entry["status"], ("shown", "hidden", "blocked"))
            self.assertTrue(entry["reasons"], entry)
            self.assertTrue(all(r["code"] in REASONS and r["message"] for r in entry["reasons"]))
        self.assertEqual(self.client.get("/v1/sessions/nope/decisions").status_code, 404)
        with patch.dict(os.environ, {"ANE_AUTHOR_DIAGNOSTICS": "off"}):
            self.assertEqual(self.client.get(f"/v1/sessions/{self.token}/decisions").status_code, 403)


class RuntimeTests(_Quiet):
    """The exported ANE AI (narrative_runtime/, the protocol a UNP and the Unity library use): the
    same ids, state versions and refusals as the API."""

    def _game(self, *gate_actions, people=True):
        from narrative_runtime.engine import Game

        if not people:
            return Game(None, _state(_world(*gate_actions)))
        oracle = Character("Oracle", ["gate"], unique=True, actions=[
            _npc_action("Oracle", "Ask about the crown", "North.", repeat="always")])
        guard = Character("Guard", ["gate"], unique=True, actions=[
            _npc_action("Guard", "Ask about the crown", "A shrug.", repeat="always")])
        return Game(None, _state(_world(*gate_actions, npcs=(oracle, guard))))

    def test_the_view_gives_each_choice_its_id_and_a_state_version(self):
        game = self._game()
        view = game.view()
        self.assertEqual([c["name"] for c in view["choices"]], view["actions"])         # aligned with the names
        ids = [c["id"] for c in view["choices"]]
        self.assertEqual(ids, sessions.offered_choices(game.state)[1])                  # the API's ids
        self.assertIn("npc:oracle:ask-about-the-crown", ids)
        self.assertIn("npc:guard:ask-about-the-crown", ids)
        for choice in view["choices"]:
            self.assertTrue({"id", "label", "category", "source", "requirements", "check", "skill_check",
                             "failure_risk", "takes_turn"} <= set(choice))
            self.assertFalse({"reasons", "status", "score", "rank"} & set(choice))        # no diagnostics
        self.assertEqual(view["state_version"], game.state.state_version)
        self.assertEqual(game.choices(), view["choices"])
        json.dumps(view)                                                                # plain JSON

    def test_choose_by_id_and_refused_choices_change_nothing(self):
        game = self._game()
        view = game.view()
        guard = "npc:guard:ask-about-the-crown"
        turn = game.choose(action_id=guard, state_version=view["state_version"])
        self.assertIn("A shrug.", turn["turn"])
        self.assertNotIn("North.", turn["turn"])
        self.assertGreater(turn["state_version"], view["state_version"])
        current = game.view()
        before = _fingerprint(game.state)
        with self.assertRaises(choices.StaleChoice) as stale:                           # the old view
            game.choose(action_id=guard, state_version=view["state_version"])
        self.assertEqual(stale.exception.details(), {
            "error": str(stale.exception), "code": "stale_state", "state_version": current["state_version"],
            "offered": [c["id"] for c in current["choices"]]})
        with self.assertRaises(choices.StaleChoice):                                    # by name too
            game.choose(current["actions"][0], state_version=view["state_version"])
        with self.assertRaises(choices.UnavailableChoice) as unknown:
            game.choose(action_id="npc:nobody:ask", state_version=current["state_version"])
        self.assertEqual(unknown.exception.code, "action_unavailable")
        for wrong in (True, -1, "3"):
            with self.assertRaises(ValueError):
                game.choose(action_id=guard, state_version=wrong)
        self.assertEqual(_fingerprint(game.state), before)                              # nothing was done
        # The UNP changes the player: the view shown before is stale.
        game.update_player({"coins": 7})
        with self.assertRaises(choices.StaleChoice):
            game.choose(action_id=guard, state_version=current["state_version"])

    def test_names_and_indexes_still_work_and_an_index_takes_that_very_choice(self):
        travel = _state(_world()).narrate("travel_new", stable="Yard", location="Yard")
        place = _place_action("Gate", travel, Outcome("You only look toward the yard.", tags=["neutral"]),
                              repeat="always")
        view = self._game(place, people=False).view()
        same = [i for i, name in enumerate(view["actions"]) if name == travel]
        self.assertEqual(len(same), 2)                                                  # one label, two choices
        for index in same:                                                              # before: always the first
            game = self._game(copy.deepcopy(place), people=False)
            chosen = game.view()["choices"][index]["id"]
            game.choose(index)
            self.assertEqual(game.state.current.name, "Yard" if chosen == "go:yard" else "Gate", chosen)
        game = self._game()
        name = next(c["name"] for c in game.view()["choices"] if c["id"] == "npc:oracle:ask-about-the-crown")
        self.assertIn("North.", game.choose(name)["turn"])                              # by name, as before
        with self.assertRaises(ValueError):
            game.choose("Fly away")

    def test_the_server_answers_409_for_a_stale_or_unknown_choice(self):
        import http.client
        import threading

        from narrative_runtime.server import RuntimeServer

        game = self._game()
        stub = type("Engine", (), {"manifest": {"name": "stub"}, "revision": 0, "data_dir": ".",
                                   "games": {game.id: game}, "lock": threading.RLock()})()
        server = RuntimeServer(stub)
        threading.Thread(target=server.serve, daemon=True).start()
        self.addCleanup(server.httpd.server_close)
        self.addCleanup(server.httpd.shutdown)

        def post(body):
            connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
            connection.request("POST", f"/games/{game.id}/choose", json.dumps(body),
                               {"Host": f"127.0.0.1:{server.port}", "Content-Type": "application/json"})
            response = connection.getresponse()
            answer = (response.status, json.loads(response.read()))
            connection.close()
            return answer

        view = game.view()
        guard = "npc:guard:ask-about-the-crown"
        status, turn = post({"action_id": guard, "state_version": view["state_version"]})
        self.assertEqual(status, 200, turn)
        self.assertIn("A shrug.", turn["turn"])
        status, stale = post({"action_id": guard, "state_version": view["state_version"]})
        self.assertEqual(status, 409)
        self.assertEqual((stale["code"], stale["state_version"], stale["offered"]),
                         ("stale_state", turn["state_version"], [c["id"] for c in turn["choices"]]))
        self.assertTrue(stale["error"])
        status, unknown = post({"action_id": "npc:nobody:ask"})
        self.assertEqual((status, unknown["code"]), (409, "action_unavailable"))
        self.assertEqual(post({})[0], 400)
        self.assertEqual(post({"action_id": 3})[0], 400)
        self.assertEqual(post({"action_id": guard, "state_version": "new"})[0], 400)
        self.assertEqual(game.state.state_version, turn["state_version"])               # nothing was done
        status, by_index = post({"action": 0})                                          # as before
        self.assertEqual((status, by_index["turn_number"]), (200, 2))


if __name__ == "__main__":
    unittest.main()
