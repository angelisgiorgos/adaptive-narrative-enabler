"""Studio and REST API coverage: actions, missions, rules, profiles, play state, the studio's
services and jobs, app parameters, and the studio as a client of the API (HTTP; in tests, the API in
this process)."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import ast
import copy
import io
import json
import os
import re
import shutil
import socket
import sqlite3
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
        for name in ("npcs.yaml", "items.yaml", "events.yaml", "missions.yaml", "hyperparameters.yaml", "locations.yaml",
                     "variables.yaml", "side_quests.yaml"):
            shutil.copy(store.CONFIG_DIR / name, self.dir / name)
        entity_files = {
            "Locations": (self.dir / "locations.yaml", "locations", "mapping"),
            "NPCs": (self.dir / "npcs.yaml", "characters", "list"),
            "Items": (self.dir / "items.yaml", "objects", "list"),
            "Events": (self.dir / "events.yaml", "events", "mapping"),
        }
        for patcher in (
            patch.dict(content.ENTITY_FILES, entity_files),
            patch.object(content, "MISSIONS_PATH", self.dir / "missions.yaml"),
            patch.object(content, "SIDE_QUESTS_PATH", self.dir / "side_quests.yaml"),
            patch.object(content, "VARIABLES_PATH", self.dir / "variables.yaml"),
            patch.object(content, "MAP_PATH", self.dir / "locations.yaml"),
            patch.object(store, "HYPERPARAMETERS_PATH", self.dir / "hyperparameters.yaml"),
            patch.object(store.config, "reload"),
            patch.dict(os.environ, {"ANE_SAVE_DIR": str(self.dir / "saves")}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(api.api, headers={"X-ANE-User": "api-tests"})  # a backend calling the API, for its user

    def _yaml(self, name):
        return yaml.safe_load((self.dir / name).read_text())


class BehaviourEditorTests(_Sandbox):
    """What people and places do is chosen in the world editor: the choices are the rules' tags, and
    choosing one sets that tag on the entry; the rules are edited in the same tab."""

    def test_the_choices_are_the_rules_tags_and_set_them(self):
        import app

        conversation, appearances = app.behaviour_fields_ui("NPCs", ["urban", "prisoner"])
        self.assertTrue(conversation["visible"])
        self.assertIn(("prisoner · the first time they meet", "prisoner"), conversation["choices"])
        self.assertIn(("challenger · on every visit", "challenger"), conversation["choices"])
        self.assertEqual((conversation["value"], appearances["value"]), ("prisoner", ""))
        self.assertIn(("wanderer · 2 visits", "wanderer"), appearances["choices"])
        draws, visits = app.behaviour_fields_ui("Locations", ["dark", "fleeting"])
        self.assertEqual((draws["label"], visits["value"]), ("Draws the player in", "fleeting"))
        self.assertFalse(app.behaviour_fields_ui("Items", ["weapon"])[0]["visible"])
        self.assertEqual(app.apply_behaviour_ui("NPCs", 0, "challenger", ["urban", "prisoner"])["value"],
                         ["urban", "challenger"])           # one tag per behaviour: it replaces the other
        self.assertEqual(app.apply_behaviour_ui("NPCs", 0, "", ["urban", "prisoner"])["value"], ["urban"])
        self.assertEqual(app.apply_behaviour_ui("Locations", 1, "secluded", ["dark", "fleeting"])["value"],
                         ["dark", "secluded"])

    def test_the_rules_are_edited_and_checked(self):
        text = self.client.get("/v1/logic/npc_behaviour").json()["parameters"]
        added = text.rstrip() + "\n  - {tags: [bard], starts_conversation: every_visit}\n"
        self.assertEqual(self.client.put("/v1/logic/npc_behaviour", json={"parameters": added}).status_code, 200)
        self.assertIn("bard", self._yaml("hyperparameters.yaml")["npc_behaviour"]["rules"][-1]["tags"])
        with patch.dict(store.config._config, {"npc_behaviour": self._yaml("hyperparameters.yaml")["npc_behaviour"]}):
            tags = self.client.get("/v1/behaviours").json()["npcs"]["starts_conversation"]["tags"]
        self.assertIn({"tag": "bard", "value": "every_visit"}, tags)  # a new choice in the editor
        bad = text.rstrip() + "\n  - {tags: [bard], starts_conversation: sometimes}\n"
        refused = self.client.put("/v1/logic/npc_behaviour", json={"parameters": bad})
        self.assertEqual(refused.status_code, 400)
        self.assertIn("starts_conversation", refused.json()["detail"])
        places = self.client.get("/v1/behaviours").json()["locations"]
        self.assertEqual(places["visits"]["values"], ["always", "once", "a number of visits"])


def _plain(value):
    """A value without what is empty (None, False, 0, "", [], {}): the defaults it stands for."""
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items() if _plain(item) not in (None, False, 0, "", [], {})}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


class WorldFormsTests(_Sandbox):
    """The world editor's forms (one action and its outcomes, scheduling, details, missions, the map,
    the behaviour rules), each backed by a JSON endpoint: nothing a form does not show is lost."""

    def test_one_action_at_a_time(self):
        base = "/v1/entities/NPCs/Gate Guard/actions"
        actions = self.client.get(base).json()["actions"]
        first = self.client.get(f"{base}/0").json()["action"]
        self.assertEqual(first, actions[0])
        added = dict(first, name="Ask him the way", exclusive_group=None)
        added.pop("exclusive_group")
        added.pop("id", None)  # an action's id is its identity: a new action does not take another's
        self.assertEqual(self.client.put(f"{base}/{len(actions)}", json={"action": added}).status_code, 200)
        self.assertEqual(self.client.get(base).json()["actions"][-1]["name"], "Ask him the way")
        bad = dict(first, triggers_event="no_such_event")
        refused = self.client.put(f"{base}/0", json={"action": bad})
        self.assertEqual(refused.status_code, 400)
        self.assertIn("no_such_event", refused.json()["detail"])
        self.assertEqual(self.client.delete(f"{base}/{len(actions)}").status_code, 200)
        self.assertEqual(self.client.get(base).json()["actions"], actions)
        self.assertEqual(self.client.get(f"{base}/99").status_code, 404)

    def test_the_action_and_outcome_forms_lose_nothing(self):
        import app

        for kind in ("Locations", "NPCs", "Items", "Events"):
            for name in self.client.get(f"/v1/entities/{kind}", params={"fields": "names"}).json()["names"]:
                for action in self.client.get(app._path(f"/v1/entities/{kind}", name, "actions")).json()["actions"]:
                    with self.subTest(kind=kind, entry=name, action=action.get("name")):
                        rebuilt = app._action_from(action, app._action_fields(action))
                        rebuilt["outcomes"] = [app._outcome_from(o, app._outcome_fields(o))
                                               for o in action.get("outcomes") or []]
                        self.assertEqual(_plain(rebuilt), _plain(action))

    def test_the_action_form_saves(self):
        import app

        names = [a["name"] for a in self.client.get("/v1/entities/NPCs/Gate Guard/actions").json()["actions"]]
        index = names.index("Give him the {alcohol_desc}")
        form = app.load_action_form_ui("NPCs", "Gate Guard", index)
        state, values = form[0], list(form[1:15])
        values[0] = "Give him the {alcohol_desc}, smiling"
        outcome = list(form[16:])
        outcome[0] = "He takes the bottle and grins."
        saved = app.save_action_form_ui(state, *values, *outcome)
        self.assertIn("Saved the action", saved[-2])
        action = self.client.get(f"/v1/entities/NPCs/Gate Guard/actions/{index}").json()["action"]
        self.assertEqual(action["name"], "Give him the {alcohol_desc}, smiling")
        self.assertEqual(action["outcomes"][0]["desc"], "He takes the bottle and grins.")
        self.assertEqual(action["outcomes"][0]["id"], "gate_guard_bribe_pass")  # what the form does not show stays

    def test_scheduling_and_details(self):
        import app

        shown = app.policy_form_ui("Events", "tavern_brawl")
        self.assertEqual((shown[0]["visible"], shown[1]["visible"]), (True, False))
        self.assertEqual(app.save_policy_form_ui("Events", "tavern_brawl", "action", 0, 1, 5, 2, "always", False, None)[0],
                         "Saved when **tavern_brawl** happens.")
        self.assertEqual(self.client.get("/v1/entities/Events/tavern_brawl/policy").json()["policy"]["cooldown"], 5)
        details = self.client.get("/v1/entities/NPCs/Smuggler/details").json()["details"]
        self.assertIn("Vault", details["known_locations"])
        changed = dict(details, known_locations=["Dungeon", "Sewers"], article="none")
        self.assertEqual(self.client.put("/v1/entities/NPCs/Smuggler/details", json={"details": changed}).status_code, 200)
        npc = next(c for c in self._yaml("npcs.yaml")["world_definition"]["characters"] if c["name"] == "Smuggler")
        self.assertEqual((npc["known_locations"], npc["article"]), (["Dungeon", "Sewers"], ""))
        self.assertTrue(npc["actions"])  # the rest of the entry stays
        refused = self.client.put("/v1/entities/NPCs/Smuggler/details", json={"details": {"known_locations": ["Moon"]}})
        self.assertEqual(refused.status_code, 400)
        place = self.client.get("/v1/entities/Locations/Harbor/details").json()["details"]
        self.assertTrue(place["distant_descriptions"])

    def test_missions_one_at_a_time_and_their_form(self):
        import app

        for key in self.client.get("/v1/missions").json()["missions"]:
            with self.subTest(mission=key):
                state_values = app.mission_form_ui(key)
                fields, state = state_values[:7], state_values[7]
                app.save_mission_form_ui(state, *fields)
                stored = self._yaml("missions.yaml")["world_definition"]["missions"][key]
                self.assertEqual(_plain(stored), _plain(self._missions_before[key]))
        mission = {"title": "Ring the bell", "description": "", "goal": {"location": "Storm Observatory"}}
        self.assertEqual(self.client.put("/v1/missions/ring_the_bell", json={"mission": mission}).status_code, 200)
        renamed = self.client.put("/v1/missions/ring_the_bell", json={"mission": mission, "rename": "ring_it"}).json()
        self.assertIn("ring_it", renamed["missions"])
        self.assertNotIn("ring_the_bell", renamed["missions"])
        bad = {"title": "x", "goal": {"location": "Moon"}}
        self.assertEqual(self.client.put("/v1/missions/ring_it", json={"mission": bad}).status_code, 400)
        self.assertEqual(self.client.delete("/v1/missions/ring_it").status_code, 200)

    def setUp(self):
        super().setUp()
        self._missions_before = self.client.get("/v1/missions").json()["missions"]

    def test_the_map_one_place_at_a_time(self):
        import app

        found = self.client.get("/v1/map").json()
        self.assertIn("Harbor", found["routes"])
        borders = sorted(set(found["routes"]["Tavern"]) - {"Alley"} | {"Sewers"})
        saved = self.client.put("/v1/map/Tavern", json={"borders": borders}).json()
        self.assertIn("Tavern", saved["routes"]["Sewers"])                  # both ways
        self.assertNotIn("Tavern", saved["routes"].get("Alley", []))
        self.assertEqual(self.client.put("/v1/map/Tavern", json={"borders": ["Moon"]}).status_code, 400)
        view, place, ticked = app.map_form_ui("Tavern")
        self.assertIn("<svg", view)
        self.assertIn("Tavern", view)
        self.assertEqual(sorted(ticked.value), borders)

    def test_the_behaviour_tables(self):
        import app

        table, *defaults = app.behaviour_tables_ui()[:3]
        self.assertIn("prisoner", list(table.iloc[:, 0]))
        table.loc[len(table)] = ["bard, singer", "every_visit", ""]
        self.assertIn("Saved", app.save_behaviour_table_ui("npcs", table, *defaults))
        rules = self._yaml("hyperparameters.yaml")["npc_behaviour"]["rules"]
        self.assertEqual(rules[-1], {"tags": ["bard", "singer"], "starts_conversation": "every_visit"})
        self.assertEqual(next(r for r in rules if r["tags"] == ["wanderer"])["appearances"], 2)  # numbers stay numbers
        refused = self.client.put("/v1/behaviours/rules/npcs", json={"rules": [{"tags": ["x"], "appearances": "never"}]})
        self.assertEqual(refused.status_code, 400)


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
        self.assertEqual(yaml.safe_load(text), {"playthroughs": 1, "values": {"thief_caught": True, "fence_exposed": False, "reputation": 0,
                                                                   "pit_champion": False}})
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


class UnpSessionTests(_Sandbox):
    """A play session as a UNP sees it: the exported ANE AI's protocol in the API, and the studio's views."""

    def _game(self, headers=None):
        body = {"candidate_count": 1, "generations": 1, "simulation_steps": 2, "deterministic": False, "profile": "unp"}
        game = self.client.post("/v1/simulations", json=body, headers=headers)
        self.assertEqual(game.status_code, 200, game.text)
        return game.json()

    def test_parameters_the_player_events_and_save_games(self):
        game = self._game()
        sid = game["session_id"]
        state = self.client.get(f"/v1/sessions/{sid}/state").json()
        self.assertEqual((state["format"], state["current_location"]), ("ane.session_state/1", game["location"]))
        catalogue = self.client.get("/v1/world/catalogue").json()
        self.assertEqual(catalogue["format"], "ane.world/1")
        item = catalogue["items"][0]["name"]

        changed = self.client.patch(f"/v1/sessions/{sid}/player", json={"coins": 42, "health": 3, "add_items": [item]})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual((changed.json()["coins"], changed.json()["health"]), (42, 3))
        self.assertIn(item, changed.json()["inventory"])
        for bad in ({"coins": "lots"}, {"gold": 1}, {"add_items": ["No Such Thing"]}):
            with self.subTest(player=bad):
                refused = self.client.patch(f"/v1/sessions/{sid}/player", json=bad)
                self.assertEqual(refused.status_code, 400, refused.text)
        synced = self.client.post(f"/v1/sessions/{sid}/sync", json={"type": "coins_changed", "amount": -2})
        self.assertEqual(synced.json()["coins"], 40)
        synced = self.client.post(f"/v1/sessions/{sid}/sync", json={"events": [
            {"type": "item_lost", "item": item}, {"type": "health_changed", "amount": 1}]})
        self.assertNotIn(item, synced.json()["inventory"])
        self.assertEqual(synced.json()["health"], 4)
        self.assertEqual(self.client.post(f"/v1/sessions/{sid}/sync", json={"type": "teleported"}).status_code, 400)

        doc = self.client.get(f"/v1/sessions/{sid}/savegame").json()
        self.assertEqual(doc["format"], "ane.savegame/1")
        loaded = self.client.post("/v1/sessions/load", json={"savegame": doc})
        self.assertEqual(loaded.status_code, 200, loaded.text)
        self.assertEqual(loaded.json()["restored"], "exact")
        self.assertNotEqual(loaded.json()["session_id"], sid)
        again = self.client.get(f"/v1/sessions/{loaded.json()['session_id']}/state").json()
        for field in ("current_location", "coins", "health", "inventory", "turns", "path"):
            self.assertEqual(again[field], state[field] if field in ("current_location", "turns", "path") else
                             self.client.get(f"/v1/sessions/{sid}/state").json()[field], field)

        damaged = json.loads(json.dumps(doc))
        damaged["checksum"] = "0" * 64
        rebuilt = self.client.post("/v1/sessions/load", json={"savegame": damaged}).json()
        self.assertEqual(rebuilt["restored"], "parameters")
        self.assertIn("checksum", rebuilt["note"])
        bare = {"format": "ane.savegame/1", "parameters": doc["parameters"]}
        self.assertEqual(self.client.post("/v1/sessions/load", json={"savegame": bare}).status_code, 400)  # no storyteller
        self.assertEqual(self.client.post("/v1/sessions/load", json={"savegame": bare, "session_id": sid})
                         .json()["restored"], "parameters")
        self.assertEqual(self.client.post("/v1/sessions/load", json={"savegame": {"format": "nope"}}).status_code, 400)
        for request in (self.client.get("/v1/sessions/nothing/state"), self.client.get("/v1/sessions/nothing/savegame"),
                        self.client.patch("/v1/sessions/nothing/player", json={"coins": 1}),
                        self.client.post("/v1/sessions/nothing/sync", json={"type": "coins_changed", "amount": 1})):
            self.assertEqual(request.status_code, 404, request.text)

    def test_the_studio_views(self):
        import gradio as gr

        import app

        theirs = self._game()["session_id"]  # the backend's user's game: not the studio's (the user `test`)
        with self.assertRaises(gr.Error):
            app.session_parameters_ui(theirs)
        token = self._game(headers=STUDIO)["session_id"]
        self.assertEqual(self.client.get(f"/v1/sessions/{token}").status_code, 404)  # and the other way round
        self.assertEqual(json.loads(app.session_parameters_ui(token))["format"], "ane.session_state/1")
        shown = app.update_player_ui(token, '{"coins": 9}')                # the game screen, then the story
        self.assertIn("🪙 9", app.play_part(shown, "hud").value)
        app.sync_session_ui(token, "coins_changed", "-4")
        self.assertEqual(self.client.get(f"/v1/sessions/{token}/state", headers=STUDIO).json()["coins"], 5)
        for bad in (lambda: app.sync_session_ui(token, "coins_changed", "a lot"),
                    lambda: app.update_player_ui(token, "[1]"), lambda: app.session_parameters_ui("")):
            with self.assertRaises(gr.Error):
                bad()
        path, status = app.download_savegame_ui(token)
        self.assertEqual(json.loads(Path(path).read_text())["format"], "ane.savegame/1")
        new_token, *view, status = app.play_continue_ui(path, token)
        self.assertNotEqual(new_token, token)
        self.assertIn("Continued exactly", status)
        path, status = app.world_catalogue_ui()
        self.assertEqual(json.loads(Path(path).read_text())["format"], "ane.world/1")
        self.assertIn("python", list(app.export_platforms_table()["runs on"]))


class PlayTabTests(_Sandbox):
    """▶ Play: the world of a finished run, played as a game, also without the records."""

    def test_play_the_world_of_a_finished_run(self):
        import app
        from backend import export

        with patch.dict(os.environ, {"ANE_RECORDS": "off", "ANE_SAVE_DIR": str(self.dir / "fresh")}), \
                patch.dict(export.ENGINES, {}, clear=True), patch.dict(export.RUNS, {}, clear=True):
            early = self.client.post("/v1/engines/latest/play", json={})
            self.assertEqual(early.status_code, 409, early.text)  # nothing trained yet
            self.assertIn("train the AI first", early.json()["detail"])

            trained = self.client.post("/v1/simulations", json={"candidate_count": 1, "generations": 1,
                                                                 "simulation_steps": 2, "deterministic": False})
            self.assertEqual(trained.status_code, 200, trained.text)
            engine_id = trained.json()["engine_id"]
            self.assertEqual(app.play_runs_ui().value, engine_id)  # the newest is offered first

            game = self.client.post(f"/v1/engines/{engine_id}/play", json={"profile": "player", "stats_mode": "random"})
            self.assertEqual(game.status_code, 200, game.text)
            self.assertEqual(game.json()["engine_id"], engine_id)
            sid = game.json()["session_id"]
            self.assertEqual(self.client.get(f"/v1/sessions/{sid}/transcript").json()["session_id"], sid)
            self.assertEqual(self.client.get("/v1/sessions/nothing/transcript").status_code, 404)
            self.assertEqual(self.client.post("/v1/engines/nothing/play", json={}).status_code, 409)

            # The studio's game screen: a button per choice, a turn per press, save and continue.
            started = app.play_new_game_ui(engine_id, "player", "", "balanced")
            token, story = started[0], started[-2]
            hud, place = app.play_part(started, "hud", 1), app.play_part(started, "place", 1)
            buttons = [app.play_part(started, f"choice {i + 1}", 1) for i in range(app.CHOICE_BUTTONS)]
            requests = app.play_part(started, "actions", 1)   # what each button sends
            actions = [request["action"] for request in requests]
            self.assertTrue(all(request.get("action_id") and request.get("state_version") is not None
                                for request in requests))
            self.assertIn("📍", hud.value)  # the status bar: where, health, coins, the quest
            self.assertIn("❤", hud.value)
            state = self.client.get(f"/v1/sessions/{token}", headers=STUDIO).json()  # the studio's game
            self.assertTrue(place.startswith(f"### 📍 {state['scene_name']}"))  # where you are, compactly
            self.assertIn(state["description"], place)
            self.assertNotIn("**Inventory:**", place)                          # the player has a card of their own
            self.assertIn("### 🧍 You", app.play_part(started, "you", 1))
            shown = [b.value for b in buttons if b.visible]
            self.assertEqual(len(shown), len(actions))
            self.assertTrue(all(name in label for label, name in zip(shown, actions)))
            took = next(i for i, name in enumerate(actions) if not name.startswith("Approach "))  # a turn
            out = app.play_choose_ui(token, requests, took)
            turn = app.play_part(out, "turn")
            self.assertIn("What happened", turn.value)
            self.assertIn("you chose", turn.value)                              # the choice, what came of it
            self.assertGreater(len(out[-1]), len(story))  # the story so far grew
            journey = app.play_part(out, "journey")
            self.assertIn("| # | Where | You chose |", journey)                  # every choice so far
            self.assertIn("🎯", app.play_part(out, "quest"))                     # the quest card
            state = self.client.get(f"/v1/sessions/{token}", headers=STUDIO).json()
            journal = self.client.get(f"/v1/sessions/{token}/journal", headers=STUDIO).json()["journal"]
            self.assertEqual(state["turn_summary"], journal[-1])
            self.assertEqual(journal[-1]["action"].split(") ", 1)[-1], actions[took].split(") ", 1)[-1])
            # The same, older buttons again (another tab): refused, nothing done, the current choices shown.
            stale = app.play_choose_ui(token, requests, took)
            self.assertEqual(len(self.client.get(f"/v1/sessions/{token}/journal", headers=STUDIO).json()["journal"]),
                             len(journal))
            current = self.client.get(f"/v1/sessions/{token}", headers=STUDIO).json()
            self.assertEqual([r["action_id"] for r in app.play_part(stale, "actions")],
                             [c["id"] for c in current["choices"]])
            self.assertIn("| ✅ shown |", app.decisions_ui(token))   # the author's view of the same choices
            path, _ = app.download_savegame_ui(token)
            continued = app.play_continue_ui(path, token)
            self.assertEqual(continued[-1], "Continued exactly where it was saved.")
            ended = app.play_end_ui(continued[0])
            ending = app.play_part(ended, "ending")
            self.assertTrue(ending.visible and ending.value.startswith("## "))  # how the story ended
            with self.assertRaises(Exception):
                app.play_choose_ui("", [], 0)


class GamePresentationTests(unittest.TestCase):
    """How a game reads in ▶ Play: the story without the engine's bookkeeping, the last turn at a
    glance, the journey as a table, the quest with its progress."""

    def test_the_story_reads_as_a_story(self):
        import app

        text = "\n".join([
            "[MISSION] Rid the market of the thief: Drive the thief out.", "\nYou are in the Market. Stalls everywhere.",
            "Met a Guard: A city guard.", "Continuing your activities in the Market...", "> (Merchant) Ask about the thief",
            "RESULT: Patched cloak, quick fingers.", "[LOCATION] Still at Market",
            "[QUEST] Pick up the trail: done. Next: Catch the thief.", "[EVENT] The lookout: A boy blocks your way."])
        story = app.story_markdown(text)
        self.assertIn("🎯 **Rid the market of the thief", story)
        self.assertIn("#### 📍 The Market", story)
        self.assertIn("---\n\n**▶ (Merchant) Ask about the thief**", story)   # each choice after a rule
        self.assertIn("✅ **Pick up the trail", story)
        self.assertNotIn("Continuing your activities", story)
        self.assertNotIn("Still at Market", story)
        self.assertNotIn("RESULT:", story)

    def test_the_last_turn_the_journey_and_the_quest(self):
        import app

        turn = {"turn": 4, "action": "(Captain of the Watch) Take the bounty", "with": "Captain of the Watch",
                "where": "Market", "moved_to": "Sewers", "new_places": ["Sewers"], "check": {"stat": "persuasion",
                "roll": 30, "needed": 55, "passed": True}, "succeeded": True, "told": "The captain hands you a warrant.",
                "health_change": -2, "coin_change": 5, "items_gained": ["Warrant"], "items_lost": [], "met": ["Thief"],
                "event": None, "quest_done": ["Pick up the trail"], "quest_now": "Catch the thief", "quest_complete": False}
        card = app.turn_card_markdown(turn, "> (Captain of the Watch) Take the bounty\nRESULT: x\n[SPAWN] New path: Sewers")
        for shown in ("Turn 4: you chose _Take the bounty_", "rolled 30, needed 55", "**passed ✓**",
                      "> The captain hands you a warrant.", "❤ −2", "🪙 +5", "🎒 got Warrant", "📍 Sewers (new place)",
                      "👤 met Thief", "Quest step done:** Pick up the trail → **now:** Catch the thief", "🗺 New path"):
            self.assertIn(shown, card)
        journey = app.journey_markdown([turn] * 20)
        self.assertIn("*… 5 earlier turn(s)*", journey)
        self.assertEqual(journey.count("| 4 | Market |"), 15)
        quest = {"title": "Rid the market", "description": "", "status": "in progress", "todo": ["Catch the thief"],
                 "stages": [{"title": "Pick up the trail", "state": "done"},
                            {"title": "Catch the thief", "state": "current", "description": "At dusk."},
                            {"title": "Drive them out", "state": "todo"}]}
        shown = app.quest_markdown(quest, [turn])
        self.assertIn("`▰▱▱` **1 of 3 steps**", shown)
        self.assertIn("✅ **Pick up the trail** · turn 4", shown)
        self.assertIn("**To do now:** Catch the thief", shown)


@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can write anything")
class ReadOnlyRecordsTests(unittest.TestCase):
    """A records folder the process cannot write (the container's user and a mounted folder owned by
    someone else): recording is off, the records there are still read, the studio starts."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ane-ro-records-"))
        patcher = patch.dict(os.environ, {"ANE_RECORDS_DIR": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertTrue(records.enabled())
        with records.connect() as connection:
            connection.execute("INSERT INTO engines (id, created_at, source, genome_json, resumable) "
                               "VALUES ('e1', '2026-11-17T00:00:00', 'run_best', '{}', 0)")
        os.chmod(records.database_path(), 0o444)
        os.chmod(self.root, 0o555)
        self.addCleanup(lambda: (os.chmod(self.root, 0o755), shutil.rmtree(self.root, ignore_errors=True)))

    def test_recording_is_off_and_the_records_are_still_read(self):
        self.assertFalse(records.enabled())
        self.assertIn("not writable", records.disabled_reason())
        client = TestClient(api.api, headers={"X-ANE-User": "api-tests"})  # a backend calling the API, for its user
        listed = client.get("/v1/records/engines")
        self.assertEqual(listed.status_code, 200, listed.text)  # it used to be a 500 (a read-only database)
        self.assertEqual([e["id"] for e in listed.json()["engines"]], ["e1"])
        info = client.get("/v1/records").json()
        self.assertEqual((info["enabled"], info["readable"]), (False, True))
        self.assertIsNone(records.record_engine(genome=None, source="nothing"))  # writes are skipped, not failed

    def test_a_run_whose_engine_file_this_server_cannot_read_is_not_offered(self):
        """The host's records folder in the container: the database is readable, the engine model
        files (mode 600, another user's) are not. The export used to fail with Python's error and
        the host's path."""
        from core_engine import Genome
        from core_engine.model import EngineModel
        import random

        os.chmod(self.root, 0o755)
        os.chmod(records.database_path(), 0o644)
        (self.root / "models").mkdir(exist_ok=True)
        model_file = self.root / "models" / "m1.ane.zip"
        EngineModel.capture(Genome(rng=random.Random(1)), source="host run").save(str(model_file))
        with sqlite3.connect(records.database_path()) as connection:
            connection.execute("INSERT INTO engines (id, created_at, source, genome_json, resumable, model_path) "
                               "VALUES ('m1', '2026-11-18T00:00:00', 'run_best', '{}', 0, 'models/m1.ane.zip')")
        os.chmod(model_file, 0o000)
        os.chmod(records.database_path(), 0o444)
        os.chmod(self.root, 0o555)
        self.addCleanup(lambda: os.chmod(model_file, 0o644))

        from backend import export

        self.assertNotIn("m1", [e["engine_id"] for e in export.complete_engines()])
        client = TestClient(api.api, headers={"X-ANE-User": "api-tests"})  # a backend calling the API, for its user
        for platform in ("python", "linux-x64"):
            with self.subTest(platform=platform):
                refused = client.post("/v1/exports", json={"source": "engine", "engine_id": "m1", "platform": platform})
                self.assertIn(refused.status_code, (400, 409), refused.text)
                detail = refused.json()["detail"]
                self.assertNotIn(str(self.root), detail)  # never the server's paths
                if platform == "python":
                    self.assertIn("cannot be read by this server", detail)
        self.assertNotIn(str(self.root), json.dumps(client.get("/v1/records").json()["reason"]))

    def test_errors_never_show_the_servers_paths(self):
        self.assertEqual(api.public_message("[Errno 13] Permission denied: '/data/someone/ane/models/x.ane.zip'"),
                         "[Errno 13] Permission denied: 'x.ane.zip'")
        self.assertEqual(api.public_message("Use GET /v1/exports/engines or http://host:8004/v1/x."),
                         "Use GET /v1/exports/engines or http://host:8004/v1/x.")  # routes and URLs stay

    def test_the_studio_starts_when_the_records_cannot_be_read(self):
        import app

        with patch.object(records, "list_engines", side_effect=RuntimeError("disk on fire")):
            self.assertEqual(app.recorded_engines(), [])  # a 500 from the API: no records, no crash
            app.build_app()
        self.assertIn("Recording is off", app.records_status_text())


class ConfigFolderTests(_Sandbox):
    """A config folder kept from an older release (the Docker volume), and saves that change nothing."""

    def test_an_older_folder_gets_the_new_default_entries_once(self):
        from utils.config_loader import upgrade_config_dir

        old = self.dir / "volume"
        old.mkdir()
        for name in ("npcs.yaml", "missions.yaml", "items.yaml"):
            shutil.copy(store.CONFIG_DIR / name, old / name)
        npcs = yaml.safe_load((old / "npcs.yaml").read_text())
        mine = npcs["world_definition"]["characters"][0]
        mine["descriptions"] = ["Edited by the writer."]
        npcs["world_definition"]["characters"] = [c for c in npcs["world_definition"]["characters"]
                                                  if c["name"] != "Thief"]  # as before the Thief came
        (old / "npcs.yaml").write_text(yaml.safe_dump(npcs, sort_keys=False))
        os.chmod(old / "npcs.yaml", 0o640)

        added = upgrade_config_dir(str(store.CONFIG_DIR), str(old), log=lambda *_: None)
        self.assertEqual(added, [("npcs.yaml", "Thief")])
        upgraded = yaml.safe_load((old / "npcs.yaml").read_text())["world_definition"]["characters"]
        self.assertIn("Thief", [c["name"] for c in upgraded])
        self.assertEqual(next(c for c in upgraded if c["name"] == mine["name"])["descriptions"],
                         ["Edited by the writer."])  # what is there is never changed
        self.assertEqual((old / "npcs.yaml").stat().st_mode & 0o777, 0o640)
        self.assertEqual(upgrade_config_dir(str(store.CONFIG_DIR), str(old), log=lambda *_: None), [])

        # Deleted afterwards: it stays deleted.
        upgraded = [c for c in upgraded if c["name"] != "Thief"]
        (old / "npcs.yaml").write_text(yaml.safe_dump({"world_definition": {"characters": upgraded}}, sort_keys=False))
        self.assertEqual(upgrade_config_dir(str(store.CONFIG_DIR), str(old), log=lambda *_: None), [])

    def _release(self, history):
        """A defaults folder (this release) whose history says which values earlier releases had."""
        from utils.config_loader import HISTORY_FILE

        defaults = self.dir / "defaults"
        shutil.copytree(store.CONFIG_DIR, defaults, ignore=shutil.ignore_patterns(HISTORY_FILE, ".*"))
        (defaults / HISTORY_FILE).write_text(json.dumps({"format": 1, "files": history}))
        volume = self.dir / "old-volume"
        shutil.copytree(defaults, volume, ignore=shutil.ignore_patterns(HISTORY_FILE))
        return defaults, volume

    def test_what_nobody_edited_takes_this_release_s_defaults_and_edits_stay(self):
        from utils.config_loader import digest, upgrade_config_dir

        npcs = yaml.safe_load((store.CONFIG_DIR / "npcs.yaml").read_text())["world_definition"]["characters"]
        old_priest = {**next(c for c in npcs if c["name"] == "Priest"), "goals": ["An old release's goal"]}
        old_steps = 60
        defaults, volume = self._release({
            "npcs.yaml": {"world_definition.characters[Priest]": [digest(old_priest)]},
            "hyperparameters.yaml": {"evolution.max_steps": [digest(old_steps)]},
        })
        world = yaml.safe_load((volume / "npcs.yaml").read_text())
        people = world["world_definition"]["characters"]
        people[[c["name"] for c in people].index("Priest")] = old_priest                 # an earlier release's
        people[[c["name"] for c in people].index("Aurora")]["goals"] = ["Edited by the writer."]  # edited
        people.append({"name": "Lamplighter", "associated_tags": ["urban"], "descriptions": ["Mine."]})  # added
        (volume / "npcs.yaml").write_text(yaml.safe_dump(world, sort_keys=False))
        settings = yaml.safe_load((volume / "hyperparameters.yaml").read_text())
        settings["evolution"]["max_steps"] = old_steps                   # an earlier release's value
        settings["narrative_end"]["max_actions"] = 140                   # edited
        del settings["npc_leads"]                                        # a section an earlier release lacked
        (volume / "hyperparameters.yaml").write_text(yaml.safe_dump(settings, sort_keys=False))

        logged = []
        upgrade_config_dir(str(defaults), str(volume), log=logged.append)
        people = {c["name"]: c for c in yaml.safe_load((volume / "npcs.yaml").read_text())["world_definition"]["characters"]}
        self.assertEqual(people["Priest"], next(c for c in npcs if c["name"] == "Priest"))  # updated
        self.assertEqual(people["Aurora"]["goals"], ["Edited by the writer."])               # kept
        self.assertIn("Lamplighter", people)                                                  # never touched
        settings = yaml.safe_load((volume / "hyperparameters.yaml").read_text())
        defaults_settings = yaml.safe_load((defaults / "hyperparameters.yaml").read_text())
        self.assertEqual(settings["evolution"]["max_steps"], defaults_settings["evolution"]["max_steps"])
        self.assertEqual(settings["narrative_end"]["max_actions"], 140)
        self.assertEqual(settings["npc_leads"], defaults_settings["npc_leads"])
        self.assertTrue(any("Kept as edited" in line and "Aurora" in line for line in logged))
        backups = list((volume / ".ane-backups").glob("*/npcs.yaml"))
        self.assertEqual(len(backups), 1)
        self.assertIn("Edited by the writer.", backups[0].read_text())

        # Asked for, an edited entry takes the default too, once per release; additions stay.
        upgrade_config_dir(str(defaults), str(volume), log=lambda *_: None, take_defaults=["all"])
        people = {c["name"]: c for c in yaml.safe_load((volume / "npcs.yaml").read_text())["world_definition"]["characters"]}
        self.assertNotEqual(people["Aurora"]["goals"], ["Edited by the writer."])
        self.assertIn("Lamplighter", people)
        self.assertEqual(yaml.safe_load((volume / "hyperparameters.yaml").read_text())["narrative_end"]["max_actions"],
                         defaults_settings["narrative_end"]["max_actions"])
        world = yaml.safe_load((volume / "npcs.yaml").read_text())
        next(c for c in world["world_definition"]["characters"] if c["name"] == "Aurora")["goals"] = ["Edited again."]
        (volume / "npcs.yaml").write_text(yaml.safe_dump(world, sort_keys=False))
        upgrade_config_dir(str(defaults), str(volume), log=lambda *_: None, take_defaults=["all"])  # left set
        people = {c["name"]: c for c in yaml.safe_load((volume / "npcs.yaml").read_text())["world_definition"]["characters"]}
        self.assertEqual(people["Aurora"]["goals"], ["Edited again."])  # applied once for this release

    def test_a_world_that_would_not_build_gets_only_the_additions(self):
        from utils import config_loader

        npcs = yaml.safe_load((store.CONFIG_DIR / "npcs.yaml").read_text())["world_definition"]["characters"]
        old_priest = {**next(c for c in npcs if c["name"] == "Priest"), "goals": ["An old release's goal"]}
        defaults, volume = self._release({"npcs.yaml": {"world_definition.characters[Priest]":
                                                        [config_loader.digest(old_priest)]}})
        world = yaml.safe_load((volume / "npcs.yaml").read_text())
        people = world["world_definition"]["characters"]
        people[[c["name"] for c in people].index("Priest")] = old_priest
        world["world_definition"]["characters"] = [c for c in people if c["name"] != "Smuggler"]
        (volume / "npcs.yaml").write_text(yaml.safe_dump(world, sort_keys=False))
        logged = []
        with patch.object(config_loader, "_world_problem", return_value="a mission names a missing NPC"):
            added = config_loader.upgrade_config_dir(str(defaults), str(volume), log=logged.append)
        self.assertEqual(added, [("npcs.yaml", "Smuggler")])
        people = {c["name"]: c for c in yaml.safe_load((volume / "npcs.yaml").read_text())["world_definition"]["characters"]}
        self.assertEqual(people["Priest"]["goals"], ["An old release's goal"])
        self.assertTrue(any("could not be built" in line for line in logged))

    def test_the_shipped_history_covers_the_current_defaults(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "default_history", Path(__file__).resolve().parent.parent / "packaging" / "default_history.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.main(["--check"])  # exits when config/default_history.json misses a current default

    def test_an_earlier_problem_of_the_world_does_not_block_other_edits(self):
        world = store.config.as_dict()
        world["world_definition"]["characters"] = [c for c in world["world_definition"]["characters"]
                                                   if c["name"] != "Thief"]  # a mission still names it
        store.config.use_override(world)
        # The sandbox makes reload() do nothing: restore the world with the real one afterwards.
        self.addCleanup(lambda: (setattr(store.config, "_override", None), type(store.config).reload(store.config)))
        name = world["world_definition"]["objects"][0]["name"]
        text = self.client.get(f"/v1/entities/Items/{name}/yaml").json()["yaml"]
        saved = self.client.put(f"/v1/entities/Items/{name}/yaml", json={"yaml": text})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertIn("earlier problem", saved.json()["message"])
        broken = yaml.safe_load(text)
        broken["enabled_if"] = {"npc_removed": "Nobody"}  # a problem this edit makes is still refused
        refused = self.client.put(f"/v1/entities/Items/{name}/yaml", json={"yaml": yaml.safe_dump(broken)})
        self.assertEqual(refused.status_code, 400, refused.text)

    def test_a_save_that_changes_nothing_keeps_the_file(self):
        path = self.dir / "kept.yaml"
        path.write_text("# A comment the writer wrote.\nworld_definition:\n  routes: {}\n")
        before = path.read_bytes()
        store._write_yaml(path, {"world_definition": {"routes": {}}})
        self.assertEqual(path.read_bytes(), before)
        store._write_yaml(path, {"world_definition": {"routes": {"A": ["B"]}}})
        self.assertNotEqual(path.read_bytes(), before)

    def test_the_work_a_run_asks_for_is_shown_before_it_starts(self):
        refused = self.client.post("/v1/simulations", json={"candidate_count": 400, "generations": 1000,
                                                            "simulation_steps": 400, "episodes": 10})
        self.assertEqual(refused.status_code, 400)
        self.assertIn("max_simulation_work", refused.json()["detail"])
        self.assertEqual(self.client.get("/v1/simulations/work", params={"episodes": 0}).status_code, 422)


def _controls(population=None, generations=None, episodes=None, steps=None, reproducibility="default", seed=None,
              warm_start="default"):
    """The Train form's controls (app.RUN_CONTROLS): a value given is set for the run, None: the saved default."""
    controls = []
    for value, fallback in ((population, 8), (generations, 5), (episodes, 3), (steps, 16)):
        controls += [value is not None, value if value is not None else fallback]
    return (*controls, reproducibility, seed is not None, seed if seed is not None else 42, warm_start)


def _form(controls):
    """app.run_form_ui's answer: (summary text, the steps slider's update, the resolution)."""
    import app

    outputs = app.run_form_ui(*controls)
    return outputs[7], outputs[3], outputs[8]


class TrainingWorkTests(_Sandbox):
    """How long a training story is and how much a run asks for, as the API counts it (the studio
    only shows it, and a run is refused on the same numbers): the steps asked for against the turns
    a story really takes, why they differ, and the multiplication against the work limit."""

    def _settings(self, full_length, max_actions):
        world = store.config.as_dict()
        world["evolution"]["full_length_stories"] = full_length
        world["narrative_end"]["max_actions"] = max_actions
        store.config.use_override(world)
        # The sandbox makes reload() do nothing: put the real configuration back afterwards.
        self.addCleanup(lambda: (setattr(store.config, "_override", None), type(store.config).reload(store.config)))

    def _work(self, candidates, generations, steps, episodes):
        response = self.client.get("/v1/simulations/work", params={
            "candidate_count": candidates, "generations": generations, "simulation_steps": steps, "episodes": episodes})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _steps_the_run_plays(self, steps):
        """The steps the evolution itself gives a story (what training does, not what the API says)."""
        from core_engine.evolution import NarrativeEvolution

        return NarrativeEvolution(None, None, settings={"max_steps": steps})._max_steps()

    def test_full_length_stories_use_the_configured_length_whatever_the_steps(self):
        import app

        self._settings(True, 120)
        free = store.config.get("travel.max_free_turns")
        self.assertTrue(store.config.get("travel.fast_travel") and free)
        # 16 steps: training plays the story to its end (120), not beyond. 400: it may also take its turns of
        # fast travel, which do not count toward the end (travel.max_free_turns), and the work counts them.
        for steps, turns in ((16, 120), (400, 120 + free)):
            with self.subTest(steps=steps):
                asked = self._work(2, 3, steps, 4)
                self.assertEqual((asked["requested_steps"], asked["effective_max_turns"], asked["story_length"],
                                  asked["fast_travel_turns"]), (steps, turns, 120, free))
                self.assertTrue(asked["full_length_stories"])
                self.assertFalse(asked["steps_decide"])
                self.assertEqual(asked["override"]["setting"], "evolution.full_length_stories")
                self.assertIn(f"the {steps} steps asked for", asked["override"]["reason"])
                self.assertEqual(asked["factors"], {"storytellers": 2, "generations": 3, "stories_per_storyteller": 4,
                                                    "turns_per_story": turns})
                self.assertEqual(asked["work"], 2 * 3 * 4 * turns)
                self.assertEqual(asked["story_turns"], turns)                  # older clients
                self.assertGreaterEqual(self._steps_the_run_plays(steps), turns)  # what training lets a story play
                if steps == 400:
                    self.assertIn(f"plus up to {free} turns of fast travel", asked["override"]["reason"])
        text, steps_look, _ = _form(_controls(population=2, generations=3, episodes=4, steps=16))
        self.assertIn("Full-length stories enabled: up to 120 turns", text)  # the configured value
        self.assertIn("Steps per simulation (16) does not set the story length.", text)
        self.assertIn("2 storytellers × 3 generations × 4 stories each × 120 turns = **2,880**", text)
        self.assertNotIn("⚠", text)
        self.assertIn("not used: full-length stories", steps_look["label"])
        self.assertIn("steps-unused", steps_look["elem_classes"])

    def test_bounded_stories_follow_the_steps_up_to_the_story_s_end(self):
        import app

        self._settings(False, 100)
        asked = self._work(2, 3, 16, 4)
        self.assertEqual((asked["effective_max_turns"], asked["steps_decide"], asked["override"]), (16, True, None))
        self.assertFalse(asked["full_length_stories"])
        self.assertEqual(asked["work"], 2 * 3 * 4 * 16)
        self.assertEqual(self._steps_the_run_plays(16), 16)
        text, steps_look, _ = _form(_controls(population=2, generations=3, episodes=4, steps=16))
        self.assertIn("Bounded stories: up to 16 turns", text)
        self.assertNotIn("Full-length", text)
        self.assertNotIn("does not set the story length", text)
        self.assertIn("× 16 turns = **384**", text)
        self.assertEqual(steps_look["label"], "Steps per simulation")
        self.assertNotIn("steps-unused", steps_look["elem_classes"])

        # More steps than a story lasts: the story's end decides (plus its turns of fast travel, which do not
        # count toward it), and the work counts that, not the steps.
        free = store.config.get("travel.max_free_turns")
        capped = self._work(2, 3, 400, 4)
        self.assertEqual((capped["effective_max_turns"], capped["steps_decide"]), (100 + free, False))
        self.assertEqual(capped["override"]["setting"], "narrative_end.max_actions")
        self.assertEqual(capped["work"], 2 * 3 * 4 * (100 + free))
        # Fast travel off: the story's end alone.
        bounded = store.config.as_dict()
        store.config.use_override({**bounded, "travel": {**bounded["travel"], "fast_travel": False}})
        try:
            self.assertEqual(self._work(2, 3, 400, 4)["effective_max_turns"], 100)
        finally:
            store.config.use_override(bounded)
        text, steps_look, _ = _form(_controls(population=2, generations=3, episodes=4, steps=400))
        self.assertIn("Steps per simulation (400) does not set the story length.", text)
        self.assertIn(f"capped at {100 + free}", steps_look["label"])
        self.assertIn(f"after 100 turns (plus up to {free} of fast travel)", steps_look["info"])

        # A configured length below the minimum: the story lasts the minimum, and the reason says so.
        self._settings(False, 80)
        low = self._work(1, 1, 400, 1)
        self.assertEqual((low["story_length"], low["effective_max_turns"]), (100, 100 + free))
        self.assertIn("narrative_end.max_actions is 80", low["override"]["reason"])

    def test_the_work_limit_boundary(self):
        import app

        self._settings(True, 120)
        at_limit = 2 * 1 * 1 * 120
        with patch.object(sessions, "MAX_SIMULATION_WORK", at_limit):
            asked = self._work(2, 1, 16, 1)
            self.assertEqual((asked["work"], asked["limit"], asked["allowed"], asked["over_by"]),
                             (at_limit, at_limit, True, 0))
            sessions._training_guard(2, 1, 1, 16)  # exactly at the limit: the run may start
            self.assertTrue(sessions.resolve_training({"candidate_count": 2, "generations": 1, "episodes": 1,
                                                       "simulation_steps": 16})["ready"])
            self.assertNotIn("⚠", _form(_controls(population=2, generations=1, episodes=1, steps=16))[0])
        with patch.object(sessions, "MAX_SIMULATION_WORK", at_limit - 1):
            asked = self._work(2, 1, 16, 1)
            self.assertEqual((asked["allowed"], asked["over_by"]), (False, 1))
            text = _form(_controls(population=2, generations=1, episodes=1, steps=16))[0]
            self.assertIn("⚠ **It will be refused:** 1 turns over the limit", text)
            # The run is refused on the same numbers, before anything is played.
            refused = self.client.post("/v1/simulations", json={"candidate_count": 2, "generations": 1,
                                                                "simulation_steps": 16, "episodes": 1})
            self.assertEqual(refused.status_code, 400)
            detail = refused.json()["detail"]
            self.assertIn("2 storytellers x 1 generations x 1 stories x 120 turns", detail)
            self.assertIn("1 above the limit of 239", detail)
            self.assertIn("evolution.full_length_stories", detail)  # why a story counts 120 turns, not 16


class TrainingSettingsTests(_Sandbox):
    """One model for a run's settings: the saved engine defaults (evolution.*, reproducibility.*: ⚙ Advanced)
    with what the run sets for itself (🧠 Train), resolved by the API (POST /v1/simulations/resolve). The
    summary the studio shows before a run is what it submits, what the run resolves to and what its
    evolution executes."""

    SAVED = {"pop_size": 12, "generations": 7, "episodes_per_genome": 2, "max_steps": 30, "warm_start": False,
             "full_length_stories": False}

    def setUp(self):
        super().setUp()
        world = store.config.as_dict()
        world["evolution"].update(self.SAVED)
        world["reproducibility"] = {"deterministic": True, "seed": 99}
        world["narrative_end"]["max_actions"] = 100
        store.config.use_override(world)
        self.addCleanup(lambda: (setattr(store.config, "_override", None), type(store.config).reload(store.config)))

    def _resolve(self, body):
        answer = self.client.post("/v1/simulations/resolve", json=body)
        self.assertEqual(answer.status_code, 200, answer.text)
        return answer.json()

    def test_the_saved_defaults_and_a_run_s_overrides(self):
        plan = self._resolve({})
        self.assertEqual(plan["effective"], {
            "population": 12, "generations": 7, "stories_per_storyteller": 2, "requested_steps": 30, "story_turns": 30,
            "full_length_stories": False, "story_length": 100, "deterministic": True, "seed": 99, "warm_start": False,
            "warm_start_mode": "off"})
        self.assertEqual(set(plan["sources"].values()), {"default"})
        self.assertEqual(plan["overrides"], {})
        self.assertEqual(self.client.get("/v1/simulations/work").json(), plan["work"])  # the work, with no values set

        plan = self._resolve({"candidate_count": 3, "deterministic": False, "seed": 5, "warm_start": True})
        e = plan["effective"]
        self.assertEqual((e["population"], e["generations"], e["seed"], e["warm_start_mode"]), (3, 7, None, "memory"))
        self.assertEqual(plan["sources"]["population"], "override")
        self.assertEqual(plan["sources"]["generations"], "default")
        self.assertIn("Seed 5 is not used", " ".join(plan["notes"]))

        with patch.dict(sessions.APP_SETTINGS, {"candidate_limit": 5}):  # an app limit lower than asked
            held = self._resolve({"candidate_count": 10})
        self.assertEqual(held["effective"]["population"], 5)
        self.assertIn("held to 5 (app parameter candidate_limit)", " ".join(held["notes"]))

    def test_a_deterministic_run_without_a_seed_is_refused_before_it_starts(self):
        world = store.config.as_dict()
        world["reproducibility"] = {"deterministic": True, "seed": None}
        store.config.use_override(world)
        plan = self._resolve({"candidate_count": 1, "generations": 1})
        self.assertFalse(plan["ready"])
        self.assertIn("needs a seed", plan["errors"][0])
        refused = self.client.post("/v1/simulations", json={"candidate_count": 1, "generations": 1})
        self.assertEqual(refused.status_code, 400)
        self.assertIn("needs a seed", refused.json()["detail"])
        self.assertTrue(self._resolve({"seed": 3})["ready"])  # a seed set for the run is enough
        self.assertTrue(self._resolve({"deterministic": False})["ready"])

    def test_what_is_shown_is_what_is_submitted_resolved_and_executed(self):
        import app
        from core_engine.evolution import NarrativeEvolution

        controls = _controls(population=2, generations=1, episodes=1, steps=3, reproducibility="on", seed=7,
                             warm_start="off")
        text, _, shown = _form(controls)
        for line in ("| Population | **2** storytellers | set for this run (saved default 12) |",
                     "| Generations | **1** | set for this run (saved default 7) |",
                     "| Stories per storyteller | **1** | set for this run (saved default 2) |",
                     "Bounded stories: up to 3 turns", "**7** (deterministic)", "**off**: random storytellers",
                     "2 storytellers × 1 generations × 1 stories each × 3 turns = **6**"):
            self.assertIn(line, text)
        body = app._run_overrides(*controls)
        self.assertEqual(body, {"candidate_count": 2, "generations": 1, "episodes": 1, "simulation_steps": 3,
                                "seed": 7, "deterministic": True, "warm_start": False})
        self.assertEqual(self._resolve(body)["effective"], shown["effective"])  # the summary is the API's answer

        ran = []
        original = NarrativeEvolution.run

        def spy(evolution, *args, **kwargs):  # what the evolution really runs with
            ran.append((dict(evolution.settings), evolution.seed))
            return original(evolution, *args, **kwargs)

        with patch.object(NarrativeEvolution, "run", spy):
            report, *outputs = app.run_simulation_ui(*controls, shown, progress=lambda *a, **k: None)
        settings, seed = ran[-1]
        self.assertEqual((settings["pop_size"], settings["generations"], settings["episodes_per_genome"],
                          settings["max_steps"], settings["warm_start"], settings["full_length_stories"], seed),
                         (2, 1, 1, 3, False, False, 7))
        done, card = outputs[-5:-3]  # the completed engine, and its card
        self.assertIn("✅ **Ran exactly as summarised before it started:**", done["check"])
        self.assertIn("✅ **Ran exactly as summarised before it started:**", card)
        run = records.get_run(outputs[5])  # the run as recorded
        self.assertEqual(run["seed"], 7)
        self.assertEqual((run["settings"]["pop_size"], run["settings"]["generations"],
                          run["settings"]["episodes_per_genome"], run["settings"]["max_steps"]), (2, 1, 1, 3))
        self.assertEqual(run["parameters"]["overrides"], body)

        # The API's answer says the same: what was resolved, and what the evolution ran with.
        with patch.object(NarrativeEvolution, "run", spy):
            game = self.client.post("/v1/simulations", json=body)
        self.assertEqual(game.status_code, 200, game.text)
        training = game.json()["training"]
        self.assertEqual(training["resolved"], shown["effective"])
        self.assertEqual({key: training["executed"][key] for key in app.RUN_COMPARED},
                         {key: shown["effective"][key] for key in app.RUN_COMPARED})
        self.assertEqual(training["overrides"], body)

        # A summary the run did not follow (the saved defaults changed in between) is said so.
        stale = {**shown, "effective": {**shown["effective"], "population": 12}}
        self.assertIn("population: shown 12, ran 2", app.executed_markdown(training, stale))

        # Retraining the recorded engine: its saved defaults, the form's settings, a warm start from it.
        engine_id = game.json()["engine_id"]
        retrain = {**app._retrain_overrides(1, *controls), "engine_id": engine_id}
        plan = self._resolve(retrain)
        self.assertEqual(plan["effective"]["warm_start_mode"], "engine")
        self.assertEqual(plan["sources"]["warm_start"], f"engine {engine_id}")
        text, retrain_shown = app.retrain_form_ui(engine_id, 1, *controls)
        self.assertIn("**from the recorded engine**", text)
        with patch.object(NarrativeEvolution, "run", spy):
            retrained = app.retrain_recorded_ui(engine_id, 1, *controls, retrain_shown, progress=lambda *a, **k: None)
        self.assertEqual(retrained[-5]["kind"], "retrain")
        self.assertIn("✅ **Ran exactly as summarised before it started:**", retrained[-4])
        settings, seed = ran[-1]
        self.assertEqual((settings["pop_size"], settings["generations"], settings["max_steps"], seed), (2, 1, 3, 7))


class TrainResultTests(_Sandbox):
    """🧠 Train only trains: no game is started, and nothing is written to a player's profile, unless an
    API client asks for the game (the default, for compatibility). Its completed engine is carried by
    its exact id to ▶ Play, ⚖ Evaluate and 📦 Export, never replaced by the newest."""

    SMALL = {"candidate_count": 2, "generations": 1, "episodes": 1, "simulation_steps": 3, "deterministic": False,
             "warm_start": False}

    def setUp(self):
        super().setUp()
        world = store.config.as_dict()
        world["evolution"].update({"full_length_stories": False, "warm_start": False})
        store.config.use_override(world)
        self.addCleanup(lambda: (setattr(store.config, "_override", None), type(store.config).reload(store.config)))

    @staticmethod
    def _quiet(*args, **kwargs):
        return None

    def _train(self):
        import app

        controls = _controls(population=2, generations=1, episodes=1, steps=3, reproducibility="off", warm_start="off")
        return app.run_simulation_ui(*controls, None, progress=self._quiet)

    def test_training_alone_starts_no_game_and_api_clients_still_get_one(self):
        import app

        before = set(sessions.SESSIONS)
        answer = self.client.post("/v1/simulations", json={**self.SMALL, "start_game": False, "profile": "train-only"})
        self.assertEqual(answer.status_code, 200, answer.text)
        body = answer.json()
        self.assertEqual((body["session_id"], body["game_started"], body["scene"], body["actions"]), (None, False, "", []))
        self.assertTrue(body["engine_id"] and body["run_id"] and body["engine_download_url"] and body["learning"])
        self.assertEqual(body["learning"]["fitness_version"], 3)                          # fitness.version
        self.assertNotIn("Player profile", body["report"])
        self.assertEqual(set(sessions.SESSIONS), before)                                 # no session
        self.assertEqual(persistent.VariableStore().recent_starts("train-only"), [])     # the profile untouched
        self.assertEqual([s["kind"] for s in records.list_stories(run_id=body["run_id"])], ["preview"])  # no play story

        # An API client that does not say (start_game left out) still gets the game, as before.
        game = self.client.post("/v1/simulations", json={**self.SMALL, "profile": "train-only"}).json()
        self.assertTrue(game["game_started"])
        self.assertIn(game["session_id"], sessions.SESSIONS)
        self.assertTrue(game["scene"] and game["actions"])
        self.assertEqual(len(persistent.VariableStore().recent_starts("train-only")), 1)
        before = set(sessions.SESSIONS)

        # The studio: a run, a continued and a retrained engine; none starts a game.
        outputs = self._train()
        done = outputs[-5]  # (completed engine, card, play, evaluate, export)
        self.assertEqual((done["kind"], done["engine_id"]), ("run", records.get_run(done["run_id"])["best_engine_id"]))
        continued = app.continue_recorded_ui(done["engine_id"], 1, progress=self._quiet)
        retrained = app.retrain_recorded_ui(done["engine_id"], 1, *_controls(population=2, episodes=1, steps=3,
                                                                             reproducibility="off"),
                                            progress=self._quiet)
        self.assertEqual([continued[-5]["kind"], retrained[-5]["kind"]], ["continue", "retrain"])
        self.assertEqual(set(sessions.SESSIONS), before)
        for result in (outputs, continued, retrained):
            with self.subTest(kind=result[-5]["kind"]):
                engine_id = result[-5]["engine_id"]
                self.assertIn("Completed engine", result[-4])
                self.assertIn(f"engine `{engine_id}`", result[-4])
                self.assertIn("No game has been started", result[-4])
                self.assertTrue(all(_prop(button, "interactive") for button in result[-3:]))
                self.assertFalse([s for s in records.list_stories(engine_id=engine_id) if s["kind"] == "play"])

    def test_each_follow_up_action_carries_the_exact_completed_engine(self):
        import gradio as gr

        import app
        from backend import evaluation

        first = self._train()
        done, card, *buttons = first[-5:]
        self.assertTrue(all(_prop(button, "interactive") for button in buttons))
        newest = self._train()[-5]["engine_id"]           # a later run: the first is no longer the newest
        self.assertNotEqual(newest, done["engine_id"])
        self.assertEqual(app.finished_run_choices()[0][1], newest)
        engine_id = done["engine_id"]

        # ▶ Play this result: a game with exactly this engine starts in Play (the player's choices there).
        before = set(sessions.SESSIONS)
        opened = app.play_result_ui(done, "result-test", "", "story default")
        nav = len(app.DESTINATIONS) + 3
        picker, setup, token = opened[nav:nav + 3]
        self.assertEqual(_prop(opened[0], "selected"), "play")
        self.assertEqual(_prop(picker, "value"), engine_id)
        self.assertFalse(_prop(setup, "open"))                   # the game is on: its setup folds away
        self.assertIn("Playing the engine just trained", opened[-1])
        self.assertIn(f"(engine `{engine_id}`)", opened[-1])                 # its label, and its exact id
        self.assertEqual(set(sessions.SESSIONS) - before, {token})  # one game, the one asked for
        (story,) = records.list_stories(session_id=token)
        self.assertEqual(story["engine_id"], engine_id)

        # ⚖ Evaluate this result: the selected trained engine is this one, and that is what is submitted.
        opened = app.evaluate_result_ui(done)
        target, engine_picker, note = opened[-3:]
        self.assertEqual(_prop(opened[0], "selected"), "evaluate")
        self.assertEqual((_prop(target, "value"), _prop(engine_picker, "value")), (app.TARGET_ENGINE, engine_id))
        chosen = (_prop(engine_picker, "value"), _prop(target, "value"))
        submitted = []

        def stop(path, body=None, **kwargs):  # what would be submitted, before any story is judged
            submitted.append((path, body))
            raise RuntimeError("stopped before judging")

        with patch.object(app.API, "post", side_effect=stop):
            with self.assertRaises(RuntimeError):
                app.judge_benchmark_ui(10, 0, chosen[0], False, "", 4, chosen[1], progress=self._quiet)
            with self.assertRaises(RuntimeError):
                app.calibrate_fitness_ui(30, 0, chosen[0], False, chosen[1], progress=self._quiet)
        self.assertEqual([(path, body["engine_id"]) for path, body in submitted],
                         [("/v1/jobs/judge-benchmark", engine_id), ("/v1/jobs/calibrate-fitness", engine_id)])
        recorded = records.load_engine(engine_id)
        self.assertEqual(evaluation._source(engine_id)[2], dict(recorded.genome_traits))  # the judge plays this one
        # A finished run kept without the records (recording off) can be evaluated too; an unknown engine cannot.
        with patch.object(records, "enabled", return_value=False):
            unrecorded = self.client.post("/v1/simulations", json={**self.SMALL, "start_game": False}).json()
        kept_id = unrecorded["engine_id"]
        with patch.object(records, "load_engine", side_effect=LookupError("not recorded")), \
                patch.object(records, "get_engine", side_effect=LookupError("not recorded")):
            from backend import export
            from core_engine.model import EngineModel

            kept = EngineModel.load(export._kept_engine(kept_id)["model_path"])  # the copy kept with the saves
            self.assertEqual(kept.id, kept_id)
            self.assertEqual(evaluation._source(kept_id)[2], dict(kept.genome_traits))
            api._evaluable_engine(kept_id)
            self.assertEqual(_prop(app.evaluate_result_ui({"engine_id": kept_id})[-2], "value"), kept_id)
        with self.assertRaises(api.HTTPException) as unknown:
            api._evaluable_engine("0" * 32)
        self.assertEqual(unknown.exception.status_code, 404)

        # 📦 Export this result: the one finished run chosen is this engine; the export is made from it.
        opened = app.export_result_ui(done, "python")
        form_at = len(app.DESTINATIONS) + 3
        source, generation, engines, note, export_button = opened[form_at:form_at + 5]
        self.assertEqual(_prop(opened[0], "selected"), "export")
        self.assertEqual((_prop(source, "value"), _prop(engines, "value")), (app.FINISHED_RUNS, [engine_id]))
        self.assertTrue(_prop(export_button, "interactive"))
        files, status = app.export_engine_ui(_prop(source, "value"), None, None, None, False, "", "python",
                                             _prop(engines, "value"))
        self.assertEqual(len(files), 1)
        self.assertIn(f"engine `{engine_id[:8]}`", status)
        self.assertNotIn(newest[:8], status)
        # A system without a runtime here: the engine is still the one chosen; Export waits, saying why.
        opened = app.export_result_ui(done, "linux-x64")
        self.assertEqual(_prop(opened[form_at + 2], "value"), [engine_id])
        self.assertFalse(_prop(opened[form_at + 4], "interactive"))
        self.assertIn("not available here", opened[-1])

        # Without a completed engine, or with one no longer offered, nothing is chosen in its place.
        for action in (lambda d: app.play_result_ui(d, "result-test", "", "story default"), app.evaluate_result_ui,
                       lambda d: app.export_result_ui(d, "python")):
            for missing in (None, {**done, "engine_id": "0" * 32}):
                with self.subTest(action=action, missing=bool(missing)), self.assertRaises(gr.Error):
                    action(missing)


def _applied(update, current):
    """What a component holds after a handler's output, as Gradio applies it: a skip or an update without a
    value keeps what it held; a component returned as an update, or a plain value, replaces it."""
    if isinstance(update, dict) and update.get("__type__") == "update":
        return update.get("value", current)
    import gradio as gr

    if isinstance(update, gr.blocks.Block):  # as Gradio applies it: the value only if one was passed
        given = update.constructor_args
        return given["value"] if "value" in given else current
    return update


class _Editor:
    """🌍 World → 📝 Entries as the browser holds it: the draft's values (app.DRAFT_SIZES order), the
    selection, the states and the prompt, changed only by the outputs of the studio's own handlers."""

    def __init__(self, app, kind, name):
        self.app = app
        self.values = [None] * sum(app.DRAFT_SIZES.values())
        self.kind = self.entity = self.loaded = self.pending = self.act_pick = None
        self.base, self.prompt, self.message, self.section, self.nav = {}, False, "", None, None
        out = app.load_selection_ui(kind, name)
        self._entry(out[:app.ENTRY_OUTPUTS])
        self._capture(out[app.ENTRY_OUTPUTS])

    # where each part of the draft sits among the values, and among the editor's outputs
    def at(self, part, offset=0):
        start = 0
        for name, size in self.app.DRAFT_SIZES.items():
            if name == part:
                return start + offset
            start += size
        raise KeyError(part)

    def _entry(self, out):
        a = self.app
        assert len(out) == a.ENTRY_OUTPUTS
        segments, at = {}, 0
        for name, size in a.ENTRY_SEGMENTS.items():
            segments[name], at = out[at:at + size], at + size
        self.kind = _applied(segments["selection"][0], self.kind)
        self.entity = _applied(segments["selection"][1], self.entity)
        self.loaded = _applied(segments["loaded"][0], self.loaded)
        targets = {"fields": (segments["fields"], range(self.at("fields"), self.at("fields") + 14)),
                   "entry_yaml": (segments["entry_yaml"], [self.at("entry_yaml")]),
                   "actions_yaml": (segments["actions_yaml"], [self.at("actions_yaml")]),
                   "policy_yaml": (segments["policy_yaml"], [self.at("policy_yaml")]),
                   "scheduling": (segments["scheduling"][3:], range(self.at("scheduling"), self.at("scheduling") + 8)),
                   "details": (segments["details"][3:], range(self.at("details"), self.at("details") + 9))}
        action = segments["action"]  # the picker, the state, 14 fields, the outcome picker, 17 outcome fields
        self.act_pick = _applied(action[0], self.act_pick)
        targets["action"] = ((action[1], *action[2:16], *action[17:]), range(self.at("action"), self.at("action") + 32))
        for updates, places in targets.values():
            for update, place in zip(updates, places):
                self.values[place] = _applied(update, self.values[place])

    def _capture(self, parts):
        self.base, self.status, _ = self.app.capture_draft_ui(parts, self.base, self.loaded, *self.values)

    def _draft(self, out):
        n = self.app.ENTRY_OUTPUTS
        self._entry(out[:n])
        self.nav = out[n:n + len(self.app.DESTINATIONS) + 3]
        rest = out[n + len(self.app.DESTINATIONS) + 3:]
        section, pending, prompt, message, section_now, capture = rest
        self.section = _applied(section, None) if not isinstance(section, dict) else None
        self.pending = _applied(pending, self.pending)
        if isinstance(prompt, dict) and "visible" in prompt:
            self.prompt = prompt["visible"]
        self.message = _applied(message, self.message)
        self._capture(capture)
        return out

    def _form(self, out, said=True):  # a form's own save, reload or delete: (editor, [message,] parts)
        self._entry(out[:self.app.ENTRY_OUTPUTS])
        self._capture(out[-1])
        return out[self.app.ENTRY_OUTPUTS] if said else None

    @property
    def guard(self):
        return (self.loaded, self.base, *self.values)

    @property
    def dirty(self):
        """drafts_dirty as the per-part status events keep it (the entry editor's unsaved parts)."""
        return {"entries": self.unsaved}

    def leave(self, result):
        """A leave guard's result (nav_choice_ui, section_choice_ui): its navigation, section and question."""
        self.nav = result.get("nav")
        self.section = result.get("section")
        asked = (result.get("prompts") or {}).get("entries")
        if asked:
            self.prompt, self.message, self.pending = asked
        return result

    @property
    def unsaved(self):
        return self.app.unsaved_parts(self.base, *self.values)

    def choose(self, kind, name=None):
        """The user picks a content type, then (``name``) an entry: each control changes, then its guard runs."""
        out = None
        if kind != (self.loaded or {}).get("kind"):
            self.kind = kind
            out = self._draft(self.app.entry_choice_ui(kind, self.entity, *self.guard))
            if self.prompt:
                return out
        if name is not None:
            self.entity = name
            out = self._draft(self.app.entry_choice_ui(self.kind, name, *self.guard))
        return out

    def pick_action(self, index):
        self.act_pick = index
        return self._draft(self.app.action_choice_ui(index, *self.guard))

    def answer(self, choice):
        return self._draft(self.app.resolve_draft_ui(choice, self.pending, *self.guard))

    def save(self, part):
        return self._form(self.app.save_part_ui(part, *self.guard))

    def type(self, part, offset, value):
        self.values[self.at(part, offset)] = value

    def value(self, part, offset=0):
        return self.values[self.at(part, offset)]


class EntryDraftTests(_Sandbox):
    """🌍 World → 📝 Entries tracks its draft: each form (fields, the action with its outcomes, scheduling,
    details, the three YAML editors) against what was last loaded or saved. Changing the entry, the content
    type or the action, starting a new one, or leaving the editor with unsaved changes undoes the choice and
    offers Save, Discard or Stay; a failed save keeps the draft and the entry; loads are never edits."""

    def _editor(self, kind="Locations", name="Harbor"):
        import app

        return app, _Editor(app, kind, name)

    def _stored(self, kind, name):
        return self.client.get(f"/v1/entities/{kind}/{name}").json()

    def test_loads_are_never_edits(self):
        app, editor = self._editor()
        self.assertEqual(editor.unsaved, [])
        self.assertIn('data-dirty="0"', editor.status)
        for kind, name in (("Locations", None), ("NPCs", None), ("NPCs", "Smuggler"), ("Events", "tavern_brawl"),
                           ("Items", "Ancient Map"), ("Locations", "Harbor")):
            with self.subTest(kind=kind, name=name):
                editor.choose(kind, name)
                self.assertEqual((editor.kind, editor.unsaved, editor.prompt), (kind, [], False))
                if name:
                    self.assertEqual((editor.entity, editor.loaded["entity"]), (name, name))
                self.assertEqual(set(editor.base), set(app.DRAFT_PARTS))  # every part has its baseline
        # Opening an action, switching between its outcomes and starting a new one are not edits either.
        editor.choose("NPCs", "Gate Guard")
        editor.pick_action(1)  # an action with two outcomes
        self.assertTrue(editor.value("action", 1))  # the action's name is shown
        state = editor.value("action")
        self.assertGreater(len(state["action"]["outcomes"]), 1)
        outcome_values = editor.values[editor.at("action", 15):editor.at("action", 32)]
        state, *shown = app.pick_outcome_ui(state, 1, *outcome_values)
        editor.values[editor.at("action")] = state
        editor.values[editor.at("action", 15):editor.at("action", 32)] = shown
        self.assertEqual(editor.unsaved, [])
        editor._draft(app.new_action_choice_ui(*editor.guard))
        self.assertEqual((editor.value("action", 1), editor.unsaved), ("New action", []))

    def test_every_form_s_edits_are_tracked_and_undone_edits_are_not(self):
        app, editor = self._editor("Events", "tavern_brawl")
        edits = [("fields", 2, "A brawl breaks out.\nChairs fly."),       # descriptions
                 ("scheduling", 3, 7),                                     # cooldown
                 ("entry_yaml", 0, "name: tavern_brawl\ntags: [bar]\n"),
                 ("actions_yaml", 0, "[]\n"), ("policy_yaml", 0, "trigger: random\n")]
        for part, offset, value in edits:
            with self.subTest(part=part):
                before = editor.value(part, offset)
                editor.type(part, offset, value)
                self.assertEqual(editor.unsaved, [part])
                self.assertIn(app.DRAFT_PARTS[part], app.draft_status_ui(*editor.guard[1::-1], *editor.values))
                editor.type(part, offset, before)  # typed back: nothing unsaved
                self.assertEqual(editor.unsaved, [])
        editor.choose("Locations", "Harbor")
        before = editor.value("details", 4)
        editor.type("details", 4, "Masts against the sky.")  # seen from afar
        self.assertEqual(editor.unsaved, ["details"])
        editor.type("details", 4, before)
        editor.choose("NPCs", "Smuggler")
        import pandas as pd
        dialogue = editor.value("details", 3)
        editor.type("details", 3, pd.concat([dialogue, pd.DataFrame([["", "Not here."]], columns=dialogue.columns)]))
        self.assertEqual(editor.unsaved, ["details"])
        editor.type("details", 3, dialogue)
        # The action: its fields, and an outcome edited then switched away from (kept in its draft).
        editor.choose("NPCs", "Gate Guard")
        editor.pick_action(1)  # an action with two outcomes
        editor.type("action", 15, "He shrugs.")  # the first outcome's text
        state, *shown = app.pick_outcome_ui(editor.value("action"), 1, *editor.values[editor.at("action", 15):
                                                                                       editor.at("action", 32)])
        editor.values[editor.at("action")] = state
        editor.values[editor.at("action", 15):editor.at("action", 32)] = shown
        self.assertEqual(editor.unsaved, ["action"])
        editor.type("action", 15, "{not yaml: [")  # unreadable as typed: still an edit
        self.assertEqual(editor.unsaved, ["action"])

    def test_changing_the_entry_never_discards_unsaved_changes(self):
        app, editor = self._editor()
        original = editor.value("fields", 2)
        editor.type("fields", 2, "A new harbour, rebuilt.")
        stored = self._stored("Locations", "Harbor")["descriptions"]
        # Another entry, another content type, a new entry, another destination, another World section:
        # each is undone and asked about; the draft and the entry stay.
        attempts = [lambda: editor.choose("Locations", "Market"), lambda: editor.choose("NPCs"),
                    lambda: editor._draft(app.new_entry_choice_ui(*editor.guard)),
                    lambda: editor.leave(app.nav_choice_ui("play", "world", "entries", editor.loaded, editor.dirty)),
                    lambda: editor.leave(app.nav_choice_ui("advanced", "world", "entries", editor.loaded, editor.dirty)),
                    lambda: editor.leave(app.section_choice_ui("map", "entries", editor.loaded, editor.dirty))]
        for attempt in attempts:
            with self.subTest(attempt=attempt):
                attempt()
                self.assertEqual((editor.kind, editor.entity, editor.loaded["entity"]), ("Locations", "Harbor", "Harbor"))
                self.assertEqual(editor.value("fields", 2), "A new harbour, rebuilt.")
                self.assertTrue(editor.prompt)
                self.assertIn("Unsaved changes", editor.message)
                self.assertIn("**Fields**", editor.message)
                self.assertEqual(editor.unsaved, ["fields"])
                if editor.pending["type"] in ("destination", "section"):
                    self.assertEqual(editor.section, "entries")  # back to the editor
                if editor.pending["type"] == "destination":
                    self.assertEqual(_prop(editor.nav[0], "selected"), "world")
                    self.assertEqual(editor.nav[-1], "world")
        self.assertEqual(self._stored("Locations", "Harbor")["descriptions"], stored)  # nothing written
        # Leaving from another destination (the editor is not open) goes at once.
        out = app.nav_choice_ui("train", "play", "entries", editor.loaded, editor.dirty)
        self.assertEqual((_prop(out["nav"][0], "selected"), out.get("prompts")), ("train", None))
        # Stay: the question goes, the draft and the entry stay.
        editor.choose("Locations", "Market")
        editor.answer("stay")
        self.assertEqual((editor.prompt, editor.pending, editor.entity), (False, None, "Harbor"))
        self.assertEqual(editor.unsaved, ["fields"])
        # Discard: the other entry opens; what was typed is gone, the store unchanged.
        editor.choose("Locations", "Market")
        editor.answer("discard")
        self.assertEqual((editor.entity, editor.loaded["entity"], editor.prompt, editor.unsaved), ("Market", "Market",
                                                                                                    False, []))
        self.assertEqual(self._stored("Locations", "Harbor")["descriptions"], stored)
        editor.choose("Locations", "Harbor")
        self.assertEqual(editor.value("fields", 2), original)

    def test_save_from_the_question_saves_every_changed_form_then_goes_on(self):
        app, editor = self._editor("Events", "tavern_brawl")
        editor.type("fields", 2, "Fists and flying stools.")
        editor.type("scheduling", 3, 6)  # cooldown
        editor.choose("Items", "Ancient Map")
        self.assertEqual(editor.unsaved, ["fields", "scheduling"])
        editor.answer("save")
        self.assertEqual((editor.kind, editor.entity, editor.prompt, editor.unsaved), ("Items", "Ancient Map", False, []))
        self.assertEqual(self._stored("Events", "tavern_brawl")["descriptions"], ["Fists and flying stools."])
        self.assertEqual(self.client.get("/v1/entities/Events/tavern_brawl/policy").json()["policy"]["cooldown"], 6)
        # Leaving for another destination: saved, then gone there.
        editor.type("fields", 3, "Find the map's owner.")  # goals
        editor.leave(app.nav_choice_ui("train", "world", "entries", editor.loaded, editor.dirty))
        editor.answer("save")
        self.assertEqual(_prop(editor.nav[0], "selected"), "train")
        self.assertEqual(self._stored("Items", "Ancient Map")["goals"], ["Find the map's owner."])
        self.assertEqual(editor.unsaved, [])

    def test_a_failed_save_keeps_the_draft_and_the_entry(self):
        app, editor = self._editor("Events", "tavern_brawl")
        stored = self._stored("Events", "tavern_brawl")
        condition = editor.value("fields", 7)
        policy = self.client.get("/v1/entities/Events/tavern_brawl/policy").json()["policy"]
        editor.type("scheduling", 3, 9)                 # cooldown: valid on its own
        editor.type("fields", 7, "{any: [")              # enabled_if: cannot be read
        editor.choose("Locations", "Harbor")
        editor.answer("save")
        self.assertEqual((editor.kind, editor.entity, editor.loaded["entity"]), ("Events", "tavern_brawl", "tavern_brawl"))
        self.assertTrue(editor.prompt)
        self.assertIn("Nothing was saved", editor.message)       # the entry is saved whole, or not at all
        self.assertIn("(Fields, Scheduling)", editor.message)
        self.assertEqual(editor.value("fields", 7), "{any: [")  # the draft, as typed
        self.assertEqual(editor.unsaved, ["fields", "scheduling"])  # nothing was saved: both still unsaved
        self.assertEqual(self._stored("Events", "tavern_brawl")["enabled_if"], stored["enabled_if"])
        self.assertEqual(self.client.get("/v1/entities/Events/tavern_brawl/policy").json()["policy"], policy)
        self.assertEqual(editor.pending["entity"], "Harbor")   # still asked: correct it and save, or discard
        # A form's own save that fails changes nothing either (the error is shown; no output is applied).
        import gradio as gr
        with self.assertRaises(gr.Error):
            app.save_part_ui("fields", *editor.guard)
        # Two forms editing the same data: nothing is saved over the other.
        editor.type("fields", 7, condition)
        editor.type("fields", 2, "A brawl, rewritten.")
        editor.type("entry_yaml", 0, editor.value("entry_yaml") + "\n# edited\n")
        editor.answer("save")
        self.assertIn("Nothing was saved", editor.message)
        self.assertEqual(editor.entity, "tavern_brawl")
        self.assertEqual(sorted(editor.unsaved), ["entry_yaml", "fields", "scheduling"])  # still nothing saved

    def test_a_form_s_save_keeps_the_other_forms_drafts(self):
        app, editor = self._editor("Locations", "Harbor")
        editor.type("details", 4, "Masts on the horizon.")       # details
        editor.type("entry_yaml", 0, editor.value("entry_yaml") + "\n# a note being written\n")
        message = editor.save("details")
        self.assertIn("Saved the details", message)
        self.assertIn("Not reloaded", message)                  # the YAML being edited is kept
        self.assertEqual(editor.unsaved, ["entry_yaml"])
        self.assertIn("# a note being written", editor.value("entry_yaml"))
        # A rename reloads what is not being edited under the new name, and keeps what is.
        editor._form(app.reload_part_ui("entry_yaml", *editor.guard), said=False)  # Reload from disk
        self.assertEqual(editor.unsaved, [])
        editor.type("fields", 0, "Old Harbor")
        editor.type("details", 5, "Gulls scream.")              # on arrival: unsaved, kept
        editor.save("fields")
        self.assertEqual((editor.entity, editor.loaded["entity"]), ("Old Harbor", "Old Harbor"))
        self.assertEqual(editor.unsaved, ["details"])           # kept, as typed, to be saved under the new name
        self.assertEqual(editor.value("details", 5), "Gulls scream.")
        editor.choose("NPCs")                                   # the unsaved details: asked first
        self.assertTrue(editor.prompt)
        self.assertEqual(editor.kind, "Locations")
        editor.answer("discard")
        self.assertEqual((editor.kind, editor.unsaved), ("NPCs", []))
        # The action: another action asked about with only the action's draft at stake; Discard keeps the rest.
        editor.choose("NPCs", "Gate Guard")
        editor.pick_action(0)
        editor.type("action", 1, "Bribe him")
        editor.type("fields", 3, "Keep the gate.")              # goals: unsaved too
        editor.pick_action(1)
        self.assertEqual((editor.act_pick, editor.pending["type"]), (0, "action"))
        self.assertIn("**Actions & outcomes**", editor.message)
        self.assertNotIn("Fields", editor.message.split("**Actions & outcomes**")[0].split("in ")[-1])
        editor.answer("discard")
        self.assertEqual(editor.value("action")["index"], 1)
        self.assertEqual(editor.unsaved, ["fields"])            # the other form's draft is kept
        self.assertEqual(editor.value("fields", 3), "Keep the gate.")

    def test_structured_goals_survive_the_fields_form(self):
        app, editor = self._editor("NPCs", "Smuggler")
        goals = editor.value("fields", 3)
        self.assertIn("{goal: Get out of this cell, tags: [smuggler_freed], needed: 1, reward: lead}", goals)
        editor.type("fields", 2, "A wiry man who knows every tunnel.")
        editor.save("fields")
        stored = yaml.safe_load((self.dir / "npcs.yaml").read_text())
        record = next(npc for npc in stored["world_definition"]["characters"] if npc["name"] == "Smuggler")
        self.assertEqual(record["goals"][0], {"goal": "Get out of this cell", "tags": ["smuggler_freed"], "needed": 1,
                                              "reward": "lead"})
        self.assertEqual(record["goals"][1], "Settle the score with the fence")
        self.assertEqual(editor.unsaved, [])

    def test_the_editor_is_wired_to_its_guards(self):
        import gradio as gr

        app, demo, = self._editor()[0], None
        demo = app.build_app()
        blocks = list(demo.blocks.values())
        by = {b.elem_id: b for b in blocks if getattr(b, "elem_id", None)}
        kind = next(b for b in blocks if isinstance(b, gr.Radio) and b.label == "Content type")
        entity = next(b for b in blocks if isinstance(b, gr.Dropdown) and b.label == "Entry")

        def listeners(component, event):
            return [fn for fn in demo.fns.values() if (component._id, event) in fn.targets]

        # Nothing loads on a dropdown's change (a load would replace the draft); the user's choice is guarded.
        self.assertEqual(listeners(entity, "change"), [])
        self.assertEqual([fn.fn.__name__ for fn in listeners(entity, "input")], ["entry_choice_ui"])
        self.assertEqual([fn.fn.__name__ for fn in listeners(kind, "input")], ["entry_choice_ui"])
        # What follows the content type only shows or hides fields (nothing loads: the draft stays).
        self.assertTrue(all(fn.fn.__name__ in ("behaviour_fields_ui", "policy_controls_ui")
                            for fn in listeners(kind, "change")))
        prompt = [b.value for b in self._inside(by["draft-prompt"], gr.Button)]
        self.assertEqual(sorted(prompt), sorted(["💾 Save", "Discard", "Stay"]))
        self.assertIn("draft-status", by)
        self.assertIn("beforeunload", app.DRAFT_HEAD)
        for button in (b for b in blocks if isinstance(b, gr.Button) and (b.elem_id or "").startswith("nav-")):
            (fn,) = listeners(button, "click")
            self.assertEqual(len(fn.inputs), 4)  # where, which World section, the entry, the unsaved parts: no draft

    @staticmethod
    def _inside(block, kind):
        return StudioLayoutTests._inside(block, kind)


class DeletionUndoTests(_Sandbox):
    """Deleting asks first, naming exactly what goes; the deleted entry, action or mission is kept whole
    with its place, and Undo (this session) puts back that record only: where it was, or next to its old
    neighbour when the list changed, never over a newer one with its name. The delete rules stay: at least
    one place, person and item remains; an action's remaining list and the missions are checked."""

    def _names(self, kind):
        return self.client.get(f"/v1/entities/{kind}", params={"fields": "names"}).json()["names"]

    def _stored(self, kind, name):
        files = {"Locations": ("locations.yaml", "locations"), "NPCs": ("npcs.yaml", "characters"),
                 "Items": ("items.yaml", "objects"), "Events": ("events.yaml", "events")}
        file, key = files[kind]
        records = yaml.safe_load((self.dir / file).read_text())["world_definition"][key]
        return records.get(name) if isinstance(records, dict) else next((r for r in records if r["name"] == name), None)

    def _restore(self, undo):
        return self.client.post(f"/v1/deletions/{undo['token']}/restore")

    def test_an_entry_comes_back_whole_and_where_it_was(self):
        for kind, name in (("NPCs", "Smuggler"), ("Locations", "Alley"), ("Events", "bandit_ambush"),
                           ("Items", "Steel Dagger")):
            with self.subTest(kind=kind):
                names, record = self._names(kind), self._stored(kind, name)
                deleted = self.client.delete(f"/v1/entities/{kind}/{name}")
                self.assertEqual(deleted.status_code, 200, deleted.text)
                undo = deleted.json()["undo"]
                self.assertEqual((undo["type"], undo["kind"], undo["name"], undo["position"], undo["of"]),
                                 ("entity", kind, name, names.index(name) + 1, len(names)))
                self.assertIsNone(self._stored(kind, name))
                restored = self._restore(undo)
                self.assertEqual(restored.status_code, 200, restored.text)
                self.assertFalse(restored.json()["changed_since"])
                self.assertIn("where it was", restored.json()["message"])
                self.assertEqual(self._names(kind), names)              # its place
                self.assertEqual(self._stored(kind, name), record)      # every field, as stored
                self.assertEqual(self._restore(undo).status_code, 404)  # once

    def test_a_name_taken_again_or_a_changed_list_is_detected_and_nothing_newer_is_overwritten(self):
        names = self._names("NPCs")
        undo = self.client.delete("/v1/entities/NPCs/Smuggler").json()["undo"]
        newer = {"name": "Smuggler", "tags": ["dock"], "descriptions": ["A newer smuggler."], "goals": []}
        self.assertEqual(self.client.put("/v1/entities/NPCs/Smuggler", json=newer).status_code, 200)
        conflict = self._restore(undo)
        self.assertEqual(conflict.status_code, 409)
        self.assertIn("not overwritten", conflict.json()["detail"])
        self.assertEqual(self._stored("NPCs", "Smuggler")["descriptions"], ["A newer smuggler."])  # the newer one stays
        self.client.delete("/v1/entities/NPCs/Smuggler")         # that one gone: the first can come back
        before = names[names.index("Smuggler") - 1]
        self.client.delete(f"/v1/entities/NPCs/{names[0]}")      # and the list changed meanwhile
        restored = self._restore(undo).json()
        self.assertTrue(restored["changed_since"])
        self.assertIn(f"after **{before}** as before", restored["message"])
        now = self._names("NPCs")
        self.assertEqual(now.index("Smuggler"), now.index(before) + 1)

    def test_an_action_comes_back_and_only_the_one_asked_for_is_deleted(self):
        actions = self.client.get("/v1/entities/NPCs/Gate Guard/actions").json()["actions"]
        wrong = self.client.delete("/v1/entities/NPCs/Gate Guard/actions/1", params={"expected_name": actions[2]["name"]})
        self.assertEqual(wrong.status_code, 409)                 # the action at 2 is not that one: nothing deleted
        self.assertEqual(self.client.get("/v1/entities/NPCs/Gate Guard/actions").json()["actions"], actions)
        deleted = self.client.delete("/v1/entities/NPCs/Gate Guard/actions/1", params={"expected_name": actions[1]["name"]})
        self.assertEqual(deleted.status_code, 200, deleted.text)
        undo = deleted.json()["undo"]
        self.assertEqual((undo["type"], undo["entry"], undo["name"], undo["position"]),
                         ("action", "Gate Guard", actions[1]["name"], 2))
        self.assertEqual(self._restore(undo).status_code, 200)
        self.assertEqual(self.client.get("/v1/entities/NPCs/Gate Guard/actions").json()["actions"], actions)
        # Its entry gone, or an action of that name back: a conflict, nothing written.
        undo = self.client.delete("/v1/entities/NPCs/Gate Guard/actions/1").json()["undo"]
        entry = self.client.delete("/v1/entities/NPCs/Gate Guard").json()["undo"]
        gone = self._restore(undo)
        self.assertEqual(gone.status_code, 409)
        self.assertIn("no longer in NPCs", gone.json()["detail"])
        self.assertEqual(self._restore(entry).status_code, 200)  # the entry back (without that action)
        again = {**actions[1], "outcomes": [{"desc": "Another take.", "tags": ["neutral"]}]}
        added = self.client.put("/v1/entities/NPCs/Gate Guard/actions/4", json={"action": again})
        self.assertEqual(added.status_code, 200, added.text)
        self.assertEqual(self._restore(undo).status_code, 409)
        self.assertEqual(self.client.get("/v1/entities/NPCs/Gate Guard/actions").json()["actions"][-1]["outcomes"],
                         again["outcomes"])

    def test_a_restore_that_would_now_be_invalid_is_refused(self):
        undo = self.client.delete("/v1/entities/NPCs/Thief/actions/0").json()["undo"]  # it starts confront_thief
        world = store.config.as_dict()
        del world["world_definition"]["events"]["confront_thief"]                    # gone since
        store.config.use_override(world)
        self.addCleanup(lambda: (setattr(store.config, "_override", None), type(store.config).reload(store.config)))
        refused = self._restore(undo)
        self.assertEqual(refused.status_code, 400)
        self.assertIn("would now be invalid", refused.json()["detail"])
        self.assertNotIn(undo["name"], [action["name"] for action in               # nothing written
                                        self.client.get("/v1/entities/NPCs/Thief/actions").json()["actions"]])
        store.config._override = None                                            # the event back: it can
        type(store.config).reload(store.config)
        self.assertEqual(self._restore(undo).status_code, 200)

    def test_a_mission_comes_back_where_it_was(self):
        missions = self.client.get("/v1/missions").json()["missions"]
        deleted = self.client.delete("/v1/missions/shield_bearer").json()
        self.assertEqual((deleted["undo"]["type"], deleted["undo"]["position"]), ("mission", 3))
        self.assertEqual(self._restore(deleted["undo"]).status_code, 200)
        stored = yaml.safe_load((self.dir / "missions.yaml").read_text())["world_definition"]["missions"]
        self.assertEqual(list(stored), list(missions))
        self.assertEqual(stored["shield_bearer"], missions["shield_bearer"])
        undo = self.client.delete("/v1/missions/shield_bearer").json()["undo"]
        self.client.put("/v1/missions/shield_bearer", json={"mission": missions["royal_audience"]})
        self.assertEqual(self._restore(undo).status_code, 409)

    def test_the_last_place_person_and_item_cannot_be_deleted_but_events_can_all_go(self):
        names = self._names("Items")
        for name in names[1:]:
            self.assertEqual(self.client.delete(f"/v1/entities/Items/{name}").status_code, 200)
        last = self.client.delete(f"/v1/entities/Items/{names[0]}")
        self.assertEqual(last.status_code, 400)
        self.assertIn("At least one item must remain", last.json()["detail"])
        self.assertEqual(self._names("Items"), names[:1])
        for name in self._names("Events"):
            self.assertEqual(self.client.delete(f"/v1/entities/Events/{name}").status_code, 200)
        self.assertEqual(self._names("Events"), [])

    def test_the_studio_asks_exactly_then_deletes_then_undoes(self):
        import gradio as gr

        import app

        editor = _Editor(app, "NPCs", "Smuggler")
        names = self._names("NPCs")
        # Cancel: nothing deleted.
        pending, panel, question, button = app.ask_delete_entry_ui(*editor.guard)
        self.assertEqual(pending, {"type": "entity", "kind": "NPCs", "name": "Smuggler"})
        self.assertTrue(panel["visible"])
        self.assertIn("Delete the person **Smuggler**?", question)
        self.assertIn(f"NPCs, {app._ordinal(names.index('Smuggler') + 1)} of {len(names)}", question)
        editor.type("fields", 2, "Unsaved words.")
        self.assertIn("unsaved changes (Fields) go with it", app.ask_delete_entry_ui(*editor.guard)[2])
        cleared = app.cancel_delete_ui()
        self.assertEqual((cleared[0], cleared[1]["visible"]), (None, False))
        self.assertIn("Smuggler", self._names("NPCs"))
        # Confirmed: exactly that entry goes, the next one opens, Undo is offered.
        out = app.confirm_delete_entry_ui(pending, [], *editor.guard)
        n = app.ENTRY_OUTPUTS
        editor._entry(out[:n])
        editor._capture(out[n + 1])
        message, _, left, panel, _, _, undo, bar, said = out[n:]
        self.assertNotIn("Smuggler", self._names("NPCs"))
        self.assertEqual((left, panel["visible"], bar["visible"]), (None, False, True))
        self.assertIn("Deleted the person **Smuggler**", said)
        self.assertEqual(editor.entity, names[0])
        self.assertEqual(editor.unsaved, [])
        # Undo: it comes back where it was and opens.
        out = app.undo_delete_entry_ui(undo, *editor.guard)
        editor._entry(out[:n])
        editor._capture(out[n + 1])
        self.assertEqual(self._names("NPCs"), names)
        self.assertEqual((editor.entity, editor.unsaved), ("Smuggler", []))
        self.assertIn("Restored **Smuggler** to NPCs, where it was", out[n])
        self.assertEqual((out[-3], out[-2]["visible"]), ([], False))
        # Undo again from a stale history (it was undone): said, and dropped from the history.
        again = app.undo_delete_entry_ui(undo, *editor.guard)
        self.assertIn("Not restored", again[-1])
        self.assertEqual(again[-3], [])
        # A conflict: said, and Undo stays to try again once the name is free.
        undo = app.confirm_delete_entry_ui(pending, [], *editor.guard)[n + 6]
        self.client.put("/v1/entities/NPCs/Smuggler", json={"name": "Smuggler", "tags": [], "descriptions": ["New."],
                                                             "goals": []})
        conflict = app.undo_delete_entry_ui(undo, *editor.guard)
        self.assertIn("not overwritten", conflict[-1])
        self.assertEqual(conflict[-3], undo)
        # The last item: the API refuses; the question says why, without its Delete button; no Undo.
        for name in self._names("Items")[1:]:
            self.client.delete(f"/v1/entities/Items/{name}")
        editor.choose("Items")
        pending = app.ask_delete_entry_ui(*editor.guard)[0]
        refused = app.confirm_delete_entry_ui(pending, [], *editor.guard)
        self.assertIn("Not deleted", refused[n + 4])
        self.assertIn("At least one item must remain", refused[n + 4])
        self.assertEqual((refused[n + 3]["visible"], refused[n + 5]["visible"]), (True, False))
        self.assertEqual(refused[n + 6], gr.skip())  # no Undo: nothing was deleted
        self.assertEqual(len(self._names("Items")), 1)

    def test_the_studio_deletes_and_undoes_an_action_and_a_mission(self):
        import app

        editor = _Editor(app, "NPCs", "Gate Guard")
        editor.pick_action(1)
        stored = self.client.get("/v1/entities/NPCs/Gate Guard/actions").json()["actions"]
        pending, panel, question, _ = app.ask_delete_action_ui(*editor.guard)
        self.assertIn(f"Delete the action **2. {stored[1]['name']}** of **Gate Guard** (NPCs)?", question)
        self.assertIn(f"{len(stored[1]['outcomes'])} outcome(s)", question)
        n = app.ENTRY_OUTPUTS
        out = app.confirm_delete_action_ui(pending, [], *editor.guard)
        editor._entry(out[:n])
        editor._capture(out[n + 1])
        self.assertEqual(len(self.client.get("/v1/entities/NPCs/Gate Guard/actions").json()["actions"]), len(stored) - 1)
        self.assertEqual(editor.unsaved, [])
        undo = out[n + 6]
        out = app.undo_delete_action_ui(undo, *editor.guard)
        editor._entry(out[:n])
        editor._capture(out[n + 1])
        self.assertEqual(self.client.get("/v1/entities/NPCs/Gate Guard/actions").json()["actions"], stored)
        self.assertEqual((editor.value("action")["index"], editor.value("action", 1)), (1, stored[1]["name"]))
        self.assertEqual(editor.unsaved, [])
        # A mission: asked, deleted, undone (it opens in the form).
        missions = list(self.client.get("/v1/missions").json()["missions"])
        state = app.mission_form_ui("shield_bearer")[-1]
        pending, _, question, _ = app.ask_delete_mission_ui(state)
        self.assertIn("(`shield_bearer`)?", question)
        self.assertIn(f"3rd of {len(missions)}", question)
        def stored():  # missions.yaml (the sandbox does not reload the active configuration)
            return list(yaml.safe_load((self.dir / "missions.yaml").read_text())["world_definition"]["missions"])

        out = app.confirm_delete_mission_ui(pending, None, state, {}, {})
        self.assertNotIn("shield_bearer", stored())
        self.assertIn("Deleted the mission **Find a shield** (`shield_bearer`)", out[9])
        # The form no longer stands for the deleted mission: its Save cannot bring it back.
        self.assertIsNone(out[10 + app.MISSION_FORM_SIZE]["key"])
        out = app.undo_delete_mission_ui(out[7])
        self.assertEqual((_prop(out[0], "value"), out[1]), ("shield_bearer", "shield_bearer"))
        self.assertEqual(stored(), missions)

    def test_the_studio_s_delete_buttons_ask_first(self):
        import gradio as gr

        import app

        demo = app.build_app()
        blocks = list(demo.blocks.values())
        by = {b.elem_id: b for b in blocks if getattr(b, "elem_id", None)}
        buttons = {b.value: b for b in blocks if isinstance(b, gr.Button)}

        def handler(component):
            (fn,) = [fn for fn in demo.fns.values() if (component._id, "click") in fn.targets]
            return fn.fn.__name__

        self.assertEqual([handler(buttons[label]) for label in ("Delete entry", "🗑 Delete action", "🗑 Delete mission")],
                         ["ask_delete_entry_ui", "ask_delete_action_ui", "ask_delete_mission_ui"])
        for prefix, confirmed, undone in (("entry", "confirm_delete_entry_ui", "undo_delete_entry_ui"),
                                          ("action", "confirm_delete_action_ui", "undo_delete_action_ui"),
                                          ("mission", "confirm_delete_mission_ui", "undo_delete_mission_ui")):
            with self.subTest(prefix=prefix):
                self.assertFalse(by[f"{prefix}-confirm"].visible)
                self.assertFalse(by[f"{prefix}-undo"].visible)
                self.assertEqual(handler(by[f"{prefix}-confirm-delete"]), confirmed)
                self.assertEqual(handler(by[f"{prefix}-confirm-cancel"]), "cancel_delete_ui")
                self.assertEqual(handler(by[f"{prefix}-undo-button"]), undone)


class TuningParameterTests(_Sandbox):
    """⚙ Advanced → Weights presents each tuning parameter from the engine's own code (backend/tuning.py,
    tuning_parameters.yaml): a readable label, what the engine does with it, its unit on its own scale,
    the engine's default, its numeric type and bounds only where the engine sets them, with its path as
    secondary help; a parameter without metadata falls back to its name. Saving keeps unchanged values as
    written, keeps integers integers (a fraction is refused, not cut down) and enforces the bounds."""

    def _weights(self):
        answer = self.client.get("/v1/weights")
        self.assertEqual(answer.status_code, 200, answer.text)
        return answer.json()

    def _file(self):
        return (self.dir / "hyperparameters.yaml").read_text(encoding="utf-8")

    def _value(self, path):
        node = yaml.safe_load(self._file())
        for key in path.split("."):
            node = node[key]
        return node

    def test_each_parameter_is_presented_from_the_engine_s_code(self):
        weights = self._weights()
        info = {p["path"]: p for p in weights["parameters"]}
        self.assertEqual(list(info), list(weights["values"]))   # the same parameters, in the same order
        rate = info["skill_check_resolution.difficulty_rate_per_step"]
        self.assertEqual((rate["label"], rate["type"], rate["default"], rate["described"]),
                         ("Difficulty increase per turn", "float", 0.0, True))
        self.assertIn("per turn", rate["unit"])
        self.assertIn("core_engine/combat.py:82", rate["source"])
        # Chances on their own scales: a percentage of the 1-100 roll, a probability, a pressure.
        self.assertIn("percent of the roll", info["skill_check_resolution.min_chance"]["unit"])
        self.assertIn("probability", info["travel.first_trip.chance"]["unit"])
        self.assertRegex(info["travel.first_trip.chance"]["unit"], r"\(0[–-]1\)")
        self.assertIn("not a probability", info["unexpected_events.base_chance"]["unit"])
        # Bounds only where the engine sets them (rules.validate_settings; fitness clamps floor_steps).
        bounded = {path: (p["minimum"], p["maximum"]) for path, p in info.items()
                   if p["minimum"] is not None or p["maximum"] is not None}
        self.assertEqual(bounded, {"story_rules.repetition_power": (1, None), "story_rules.backtrack_penalty": (0, None),
                                   "story_rules.collect_weight": (0, None), "story_rules.payoff_weight": (0, None),
                                   "story_rules.payoff_route_weight": (0, None),
                                   "fitness.length_normalization.floor_steps": (1, None),
                                   "action_selection.stay_penalty_power": (0, None),
                                   "fitness.objective_losses.scenario_duplication.power": (0, None)})
        for path, p in info.items():
            with self.subTest(path=path):
                self.assertTrue(p["described"] and p["label"] and p["unit"] and p["explanation"])
                self.assertIn(p["type"], ("integer", "float"))
                if p["type"] == "integer":
                    self.assertEqual(float(p["value"]), int(p["value"]))
                for reference in p["source"]:  # where the engine reads it: that line still reads it
                    file, _, line = reference.partition(":")
                    text = (store.PROJECT_ROOT / file).read_text(encoding="utf-8").splitlines()
                    first, _, last = line.partition("-")
                    first = int(first)
                    last = int(last or first)
                    near = "\n".join(text[max(0, first - 3):last + 2])  # ±2 lines around a single line
                    # A world's own names (a location's penalties) are read by looping over their mapping.
                    name = path.split(".")[-1] if ".location_penalties." not in path else path.split(".")[-2]
                    self.assertIn(name, near, f"{path}: {reference} does not read it (the code moved?)")
        # A world's own names (a new tag, another location) are described by a pattern; known ones by their own.
        from backend import tuning

        self.assertNotEqual(info["outcome_resolution.tag_weights.threat"]["label"],
                            info["outcome_resolution.tag_weights.urban"]["label"])
        self.assertIs(tuning.find("outcome_resolution.tag_weights.harbour"),
                      tuning.metadata()["outcome_resolution.tag_weights.*"])
        self.assertIs(tuning.find("action_selection.location_penalties.Vault.destination_tags.gold"),
                      tuning.metadata()["action_selection.location_penalties.*.destination_tags.*"])
        self.assertIsNone(tuning.find("outcome_resolution.tag_weights.harbour.extra"))  # one segment per *

    def test_a_parameter_without_metadata_falls_back_to_its_name(self):
        import app

        text = self._file().replace("fitness:\n", "fitness:\n  ferry_bonus: 3\n", 1)
        (self.dir / "hyperparameters.yaml").write_text(text, encoding="utf-8")
        found = next(p for p in self._weights()["parameters"] if p["path"] == "fitness.ferry_bonus")
        self.assertEqual((found["described"], found["label"], found["type"], found["unit"], found["explanation"],
                          found["minimum"], found["default"]), (False, "Ferry bonus", "integer", None, None, None, None))
        help_text = app.weight_help(found)
        self.assertIn("No description yet", help_text)
        self.assertTrue(help_text.endswith("Parameter: fitness.ferry_bonus"))
        refused = self.client.put("/v1/weights", json={"values": {"fitness.ferry_bonus": 3.5}})
        self.assertEqual(refused.status_code, 400)
        self.assertIn("is a whole number (as it is written)", refused.json()["detail"])
        self.assertEqual(self.client.put("/v1/weights", json={"values": {"fitness.ferry_bonus": 4}}).status_code, 200)
        self.assertEqual(self._value("fitness.ferry_bonus"), 4)

    def test_integers_stay_integers_fractions_are_refused_and_unchanged_values_are_kept(self):
        before = self._file()
        values = self._weights()["values"]
        # Saved unchanged (as the form sends them: decimals as floats): the file is not rewritten.
        same = self.client.put("/v1/weights", json={"values": {p: float(v) for p, v in values.items()}})
        self.assertEqual(same.status_code, 200, same.text)
        self.assertIn("(0 changed)", same.json()["message"])
        self.assertEqual(self._file(), before)
        # An integer parameter: a whole number is stored as an integer; a fraction is refused, not cut down.
        saved = self.client.put("/v1/weights", json={"values": {"stakes.alert.check_penalty": 4.0}})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertIs(type(self._value("stakes.alert.check_penalty")), int)
        self.assertEqual(self._value("stakes.alert.check_penalty"), 4)
        after = yaml.safe_load(self._file())
        original = yaml.safe_load(before)

        def leaves(node, prefix=()):
            for key, value in node.items():
                if isinstance(value, dict):
                    yield from leaves(value, prefix + (key,))
                else:
                    yield ".".join(prefix + (key,)), value

        changed = {path: (old, type(old)) for path, old in leaves(original)}
        now = {path: (new, type(new)) for path, new in leaves(after)}
        self.assertEqual({p for p in changed if changed[p] != now[p]}, {"stakes.alert.check_penalty"})  # nothing else
        written = self._file()
        for value, problem in ((4.5, "is a whole number"),):
            refused = self.client.put("/v1/weights", json={"values": {"stakes.alert.check_penalty": value}})
            self.assertEqual(refused.status_code, 400)
            self.assertIn(problem, refused.json()["detail"])
            self.assertIn("Nothing was saved", refused.json()["detail"])
        self.assertEqual(self._file(), written)
        # A decimal parameter written as a whole number (40) takes a fraction, and stays a number.
        self.assertIs(type(values["evolution.alternatives_weight"]), int)
        self.client.put("/v1/weights", json={"values": {"evolution.alternatives_weight": 40.5}})
        self.assertEqual(self._value("evolution.alternatives_weight"), 40.5)
        self.client.put("/v1/weights", json={"values": {"skill_check_resolution.min_chance": 6.0}})
        self.assertIs(type(self._value("skill_check_resolution.min_chance")), int)  # written whole, kept whole
        # The engine's bounds: below them nothing is saved.
        for path, value in (("story_rules.repetition_power", 0.5), ("story_rules.payoff_weight", -1),
                            ("fitness.length_normalization.floor_steps", 0)):
            with self.subTest(path=path):
                written = self._file()
                refused = self.client.put("/v1/weights", json={"values": {path: value}})
                self.assertEqual(refused.status_code, 400)
                self.assertIn("must be at least", refused.json()["detail"])
                self.assertEqual(self._file(), written)

    def test_the_weights_tab_shows_labels_help_and_whole_numbers(self):
        import gradio as gr

        import app

        info = {p["path"]: p for p in self._weights()["parameters"]}
        rate = app.weight_field(info["skill_check_resolution.difficulty_rate_per_step"])
        self.assertEqual((rate.label, rate.precision), ("Difficulty increase per turn", None))
        self.assertIn("engine default 0", rate.info)
        self.assertTrue(rate.info.endswith("Parameter: skill_check_resolution.difficulty_rate_per_step"))
        start = app.weight_field(info["skill_check_resolution.difficulty_rate_start_step"])
        self.assertEqual(start.precision, 0)  # whole numbers only
        self.assertIn("whole number", start.info)
        power = app.weight_field(info["story_rules.repetition_power"])
        self.assertEqual(power.minimum, 1)
        self.assertIn("at least 1", power.info)
        demo = app.build_app()
        labels = {b.label for b in demo.blocks.values() if isinstance(b, gr.Number)}
        self.assertIn("Difficulty increase per turn", labels)
        self.assertNotIn("skill_check_resolution.difficulty_rate_per_step", labels)  # the path is help, not the label
        # The form's own save, with what its fields hold, leaves the file as it was.
        before = self._file()
        weights = self._weights()
        meta = app.weights_meta(weights["parameters"], weights["basic"])
        values = [int(p["value"]) if p["type"] == "integer" else float(p["value"]) for p in info.values()]
        self.assertIn("Nothing to save", app.save_weights(meta, weights["values"], *values)[0])
        self.assertEqual(self._file(), before)


class BasicTuningTests(_Sandbox):
    """Weights → Basic: a chosen set of common gameplay controls (difficulty, pacing, repetition, event
    frequency), each ONE tuning parameter (no presets), explained by what it does in play; Advanced keeps
    every parameter. Both views edit one draft: a Basic field and its parameter's Advanced field hold the
    same value, switching views touches no value, the draft's status lists the changes made in Advanced
    to parameters Basic does not show, and a save sends only the changes (refused if one moved since)."""

    BASIC = ("difficulty", "pacing", "repetition", "events")
    _weights, _file, _value = TuningParameterTests._weights, TuningParameterTests._file, TuningParameterTests._value

    def _app(self):
        import gradio as gr

        import app

        demo = app.build_app()
        by = {b.elem_id: b for b in demo.blocks.values() if getattr(b, "elem_id", None)}
        return app, gr, demo, by

    def test_each_basic_control_is_one_described_parameter(self):
        weights = self._weights()
        info = {p["path"]: p for p in weights["parameters"]}
        areas = weights["basic"]
        self.assertEqual([area["key"] for area in areas], list(self.BASIC))
        paths = [control["path"] for area in areas for control in area["controls"]]
        self.assertEqual(len(paths), len(set(paths)))                     # each parameter in one place
        self.assertGreaterEqual(len(paths), 12)
        for area in areas:
            self.assertTrue(area["title"] and area["summary"])
            for control in area["controls"]:
                with self.subTest(path=control["path"]):
                    self.assertLessEqual(set(control), {"path", "effect", "scaled_by", "scaling"})  # one parameter
                    self.assertIn(control["path"], info)                   # a parameter of the configuration
                    self.assertTrue(info[control["path"]]["described"])    # from the engine's code
                    self.assertTrue(info[control["path"]]["source"])
                    self.assertGreater(len(control["effect"]), 20)
        self.assertIn("skill_check_resolution.difficulty_rate_per_step", paths)
        self.assertIn("unexpected_events.base_chance", paths)
        # A control behind a switch says so (the director, variation, events, first trips, the alert).
        self.assertEqual(info["director.retry_bonus"]["requires"], "director.enabled")
        self.assertEqual(info["unexpected_events.base_chance"]["requires"], "unexpected_events.enabled")
        self.assertEqual(info["travel.first_trip.chance"]["requires"], "travel.first_trip.enabled")
        # A configuration without one of the parameters: its control is left out, nothing invented.
        text = self._file().replace("  ping_pong_penalty:", "  ping_pong_retired:", 1)
        (self.dir / "hyperparameters.yaml").write_text(text, encoding="utf-8")
        left = [c["path"] for area in self._weights()["basic"] for c in area["controls"]]
        self.assertNotIn("director.ping_pong_penalty", left)
        self.assertEqual(len(left), len(paths) - 1)

    def test_basic_and_advanced_fields_are_one_draft(self):
        app, gr, demo, by = self._app()
        weights = self._weights()
        basic = [c["path"] for area in weights["basic"] for c in area["controls"]]
        info = {p["path"]: p for p in weights["parameters"]}

        def listeners(component, event):
            return [fn for fn in demo.fns.values() if (component._id, event) in fn.targets]

        for path in basic:
            with self.subTest(path=path):
                simple = by[app.weight_elem_id("basic", path)]
                full = by[app.weight_elem_id("advanced", path)]
                self.assertEqual((simple.value, simple.label, simple.precision, simple.minimum, simple.maximum),
                                 (full.value, full.label, full.precision, full.minimum, full.maximum))
                self.assertEqual(simple.value, info[path]["value"])
                for source, target in ((simple, full), (full, simple)):
                    (mirror,) = listeners(source, "input")                # a user's edit only (not .change)
                    self.assertEqual(([i._id for i in mirror.inputs], [o._id for o in mirror.outputs], mirror.js),
                                     ([source._id], [target._id], "(value) => value"))
                    (status,) = [fn for fn in demo.fns.values() if fn.trigger_after == mirror._id]
                    self.assertIs(status.fn, app.weights_draft_ui)
                self.assertIn("Also in Basic", full.info)
        self.assertEqual(sum(1 for key in by if key.startswith("weight-basic-")), len(basic))
        self.assertEqual(sum(1 for key in by if key.startswith("weight-advanced-")), len(info))
        # Switching views only shows and hides the two boxes: no field is an output, so no value can change.
        (switch,) = listeners(by["weights-view"], "change")
        self.assertEqual({o._id for o in switch.outputs}, {by["weights-basic"]._id, by["weights-advanced"]._id})
        self.assertEqual(by["weights-view"].value, "Basic")
        shown, hidden = app.weights_view_ui("Advanced")
        self.assertEqual((shown["visible"], hidden["visible"]), (False, True))
        self.assertEqual(set(shown) | set(hidden), {"__type__", "visible"})
        # An edit to a parameter Basic does not show updates the draft's status too.
        other = next(p for p in info if p not in basic)
        (status,) = listeners(by[app.weight_elem_id("advanced", other)], "input")
        self.assertIs(status.fn, app.weights_draft_ui)

    def test_advanced_changes_are_shown_and_a_save_sends_only_the_changes(self):
        import gradio as gr

        import app

        weights = self._weights()
        meta = app.weights_meta(weights["parameters"], weights["basic"])
        saved = dict(weights["values"])
        values = [saved[path] for path in meta["paths"]]
        clean = app.weights_draft_markup(meta, saved, *values)
        self.assertIn('data-dirty="0"', clean)
        # A Basic edit (copied to its Advanced field) and an edit only Advanced shows.
        draft = dict(zip(meta["paths"], values))
        draft["skill_check_resolution.difficulty_penalty"] = 5
        draft["fitness.base.mission_win_bonus"] = 70
        status = app.weights_draft_markup(meta, saved, *draft.values())
        self.assertIn('data-dirty="1"', status)
        self.assertIn('data-advanced="1"', status)
        basic_part, advanced_part = status.split("Advanced only, not shown in Basic (1)")
        self.assertIn("skill_check_resolution.difficulty_penalty", basic_part)
        self.assertIn(f"fitness.base.mission_win_bonus</code>: {saved['fitness.base.mission_win_bonus']} → 70",
                      advanced_part)
        # The save sends those two only: nothing else in the file changes, and the draft is clean after.
        before = yaml.safe_load(self._file())
        message, now, after = app.save_weights(meta, saved, *draft.values())
        self.assertIn("(2 changed)", message)
        self.assertEqual((now["skill_check_resolution.difficulty_penalty"], now["fitness.base.mission_win_bonus"]),
                         (5, 70))
        self.assertIn('data-dirty="0"', after)
        written = yaml.safe_load(self._file())
        before["skill_check_resolution"]["difficulty_penalty"] = 5
        before["fitness"]["base"]["mission_win_bonus"] = 70
        self.assertEqual(written, before)
        # Reloaded (the page opens, or Discard): both views get the saved values, in their fields' order.
        reloaded = app.reload_weights_ui(meta)
        values_now, advanced = reloaded[0], reloaded[1:1 + len(meta["paths"])]
        basic = reloaded[1 + len(meta["paths"]):1 + len(meta["paths"]) + len(meta["basic_paths"])]
        self.assertEqual(list(advanced), [values_now[p] for p in meta["paths"]])
        self.assertEqual(list(basic), [values_now[p] for p in meta["basic_paths"]])
        self.assertIn('data-dirty="0"', reloaded[-2])
        # Someone else changes a value after the form was loaded: the stale draft does not overwrite it.
        moved = self.client.put("/v1/weights", json={"values": {"director.retry_bonus": 20}})
        self.assertEqual(moved.status_code, 200, moved.text)
        draft = dict(zip(meta["paths"], [now[p] for p in meta["paths"]]))
        draft["director.retry_bonus"] = 10
        file_before = self._file()
        with self.assertRaises(gr.Error) as refused:
            app.save_weights(meta, now, *draft.values())
        self.assertIn("Changed since it was loaded", str(refused.exception))
        self.assertEqual(self._file(), file_before)
        self.assertEqual(self._value("director.retry_bonus"), 20)
        # An emptied field is not saved as anything.
        draft["director.retry_bonus"] = None
        with self.assertRaises(gr.Error):
            app.save_weights(meta, now, *draft.values())
        self.assertEqual(self._file(), file_before)


class TrainFollowUpTests(_Sandbox):
    """The shipped training defaults fit the work limit; a run says how many remembered storytellers it
    really started from; the completed engine's card comes back after a reload, if the engine is still there."""

    def test_the_shipped_defaults_can_run(self):
        plan = sessions.resolve_training({})  # the configuration as shipped (config/hyperparameters.yaml)
        self.assertTrue(plan["ready"], plan["errors"])
        self.assertTrue(plan["work"]["allowed"])
        self.assertLessEqual(plan["work"]["work"], plan["work"]["limit"])

    def test_the_run_reports_how_many_storytellers_it_started_from(self):
        import app

        world = store.config.as_dict()
        world["evolution"].update({"full_length_stories": False, "warm_start": True})
        store.config.use_override(world)
        self.addCleanup(lambda: (setattr(store.config, "_override", None), type(store.config).reload(store.config)))
        controls = _controls(population=2, generations=1, episodes=1, steps=3, reproducibility="off", warm_start="on")
        app.run_simulation_ui(*controls, None, progress=lambda *a, **k: None)        # something to remember
        shown = _form(controls)[2]
        outputs = app.run_simulation_ui(*controls, shown, progress=lambda *a, **k: None)
        done = outputs[-5]
        training = self.client.get(f"/v1/records/runs/{done['run_id']}").status_code  # recorded
        self.assertEqual(training, 200)
        self.assertIn("started from", done["check"])
        self.assertRegex(done["check"], r"started from \d+ remembered storyteller")

    def test_the_card_comes_back_after_a_reload_while_its_engine_is_there(self):
        import app

        controls = _controls(population=2, generations=1, episodes=1, steps=3, reproducibility="off", warm_start="off")
        world = store.config.as_dict()
        world["evolution"]["full_length_stories"] = False
        store.config.use_override(world)
        self.addCleanup(lambda: (setattr(store.config, "_override", None), type(store.config).reload(store.config)))
        done = app.run_simulation_ui(*controls, None, progress=lambda *a, **k: None)[-5]
        restored, card, *buttons = app.restore_result_ui(done)
        self.assertEqual(restored, done)
        self.assertIn("Completed engine", card)
        self.assertIn(f"engine `{done['engine_id']}`", card)
        self.assertIn("Trained earlier in this browser", card)
        self.assertTrue(all(_prop(b, "interactive") for b in buttons))
        gone, card, *buttons = app.restore_result_ui({**done, "engine_id": "0" * 32})
        self.assertIsNone(gone)
        self.assertIn("no longer among the finished runs", card)
        self.assertFalse(any(_prop(b, "interactive") for b in buttons))
        self.assertIsNone(app.restore_result_ui(None)[0])


class EngineNamesTests(_Sandbox):
    """Completed engines get an optional name, kept with the saves (recording on or off), shown by one
    label in Train, Play, Evaluate and Export; the engine and run ids stay their identifiers. An unnamed
    engine is labelled by what it is, when it was made, its generations and its training fitness. Its
    result card (creation, world, generations, settings, training fitness, the judge's engagement when
    evaluated) keeps the training fitness and the judge's engagement apart."""

    def _train(self):
        trained = self.client.post("/v1/simulations", json={"candidate_count": 1, "generations": 1,
                                                             "simulation_steps": 2, "deterministic": False})
        self.assertEqual(trained.status_code, 200, trained.text)
        return trained.json()

    def _off(self):
        """Recording off, a fresh save folder, nothing in this process's memory (as after a restart)."""
        from backend import export

        stack = [patch.dict(os.environ, {"ANE_RECORDS": "off", "ANE_SAVE_DIR": str(self.dir / "fresh")}),
                 patch.dict(export.ENGINES, {}, clear=True), patch.dict(export.RUNS, {}, clear=True)]
        for patcher in stack:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_name_is_shown_everywhere_and_the_ids_stay(self):
        import app
        from backend import export

        self._off()
        result = self._train()
        engine_id, run_id = result["engine_id"], result["run_id"]
        listed = self.client.get("/v1/exports/engines").json()["engines"][0]
        self.assertEqual((listed["engine_id"], listed["name"]), (engine_id, None))
        self.assertRegex(listed["label"], r"^Run of \d{4}-\d\d-\d\d \d\d:\d\d · 1 generation · fitness -?\d+\.\d · "
                         + engine_id[:8] + "$")  # an unnamed result: what, when, generations, fitness, its id
        card = self.client.get(f"/v1/engines/{engine_id}/card").json()
        self.assertEqual((card["engine_id"], card["run_id"], card["name"], card["generations"]),
                         (engine_id, run_id, None, 1))
        self.assertTrue(card["world"]["current"])                      # trained on the active world
        self.assertEqual(card["world"]["summary"]["locations"], len(store.config.get("world_definition.locations")))
        self.assertEqual(card["settings"]["main"]["pop_size"], 1)    # what its training ran with
        self.assertEqual(card["fitness"]["training"], listed["score"])
        self.assertEqual(card["fitness"]["version"], 3)                # the scoring it was trained with
        self.assertEqual(card["evaluations"], [])
        self.assertEqual(set(card["scaling_traits"]), {"voyage_bias", "goal_bias", "novelty_bias", "event_prob",
                                                       "chaos_bias"})
        # Named: every listing and picker shows the name; the ids do not change.
        named = self.client.put(f"/v1/engines/{engine_id}/name", json={"name": "  Harbor   tales v1 "})
        self.assertEqual(named.status_code, 200, named.text)
        self.assertEqual(named.json()["name"], "Harbor tales v1")
        label = named.json()["label"]
        self.assertTrue(label.startswith("Harbor tales v1 · ") and label.endswith(engine_id[:8]), label)
        self.assertEqual(self.client.get("/v1/exports/engines").json()["engines"][0]["label"], label)
        self.assertEqual(self.client.get("/v1/runs").json()["runs"][0]["label"], label)
        self.assertIn((label, engine_id), app.finished_run_choices())                 # Play, Export
        self.assertIn((label, engine_id), app.evaluation_engine_choices())            # Evaluate
        done = app.completed_engine(result, "run")
        self.assertIn("Completed engine: Harbor tales v1", app.completed_view_ui(done)[0])  # Train
        self.assertIn(f"engine `{engine_id}` · run `{run_id}`", app.completed_view_ui(done)[0])
        played = app.play_result_ui(done, "player", "", "balanced")
        self.assertIn("Harbor tales v1", played[-1])
        self.assertIn("Harbor tales v1", _prop(app.export_result_ui(done, "python")[len(app.DESTINATIONS) + 6], "value"))
        # The studio's rename refreshes every picker's labels (values unchanged).
        message, card_text, box, result_card, *pickers = app.rename_engine_ui(engine_id, "Second name", done)
        self.assertIn("its id stays", message)
        self.assertEqual(_prop(box, "value"), "Second name")
        self.assertIn("Completed engine: Second name", result_card)
        self.assertEqual(len(pickers), 7)
        self.assertTrue(all(any(engine_id == value and text.startswith("Second name") for text, value in
                                _prop(picker, "choices")) for picker in pickers[:4]))
        # An export is named after the engine unless a name is given, and carries it.
        exported = export.export_engine(source="engine", engine_id=engine_id)
        self.assertEqual((exported["manifest"]["name"], exported["manifest"]["engine_name"]),
                         ("Second-name", "Second name"))
        # Kept with the saves: after a restart (nothing in memory) and with recording off, still named.
        export.ENGINES.clear()
        export.RUNS.clear()
        again = self.client.get("/v1/exports/engines").json()["engines"][0]
        self.assertEqual((again["engine_id"], again["name"]), (engine_id, "Second name"))
        stored = json.loads((self.dir / "fresh" / "engine_meta.json").read_text())
        self.assertEqual((stored["format"], stored["engines"][engine_id]["name"]), (1, "Second name"))
        # No name: the label again.
        cleared = self.client.put(f"/v1/engines/{engine_id}/name", json={"name": ""}).json()
        self.assertIsNone(cleared["name"])
        self.assertTrue(cleared["label"].startswith("Run of "))
        # Refused: too long, control characters, an unknown engine.
        for bad in ("x" * 81, "bell\x07"):
            self.assertEqual(self.client.put(f"/v1/engines/{engine_id}/name", json={"name": bad}).status_code, 400)
        self.assertEqual(self.client.put("/v1/engines/nothing/name", json={"name": "x"}).status_code, 404)
        self.assertEqual(self.client.get("/v1/engines/nothing/card").status_code, 404)

    def test_recorded_engines_are_named_and_carded_too(self):
        import app

        with patch.dict(os.environ, {"ANE_RECORDS_DIR": str(self.dir / "records")}):
            self.assertTrue(records.enabled())
            engine_id = self._train()["engine_id"]
            self.client.put(f"/v1/engines/{engine_id}/name", json={"name": "Recorded one"})
            rows = self.client.get("/v1/records/engines").json()["engines"]
            best = next(row for row in rows if row["id"] == engine_id)
            self.assertTrue(best["label"].startswith("Recorded one · "))
            champion = next(row for row in rows if row["source"] == "generation_champion")
            self.assertIsNone(champion["name"])
            self.assertTrue(champion["label"].startswith("Generation champion of "))
            self.assertIn("generation 0", champion["label"])
            self.assertTrue(any(label.startswith("Recorded one") for label, _ in app.recorded_engines()))
            card = self.client.get(f"/v1/engines/{engine_id}/card").json()
            self.assertEqual(card["name"], "Recorded one")
            no_model = self.client.get(f"/v1/engines/{champion['id']}/card")  # no model file: what its record says
            self.assertEqual(no_model.status_code, 200, no_model.text)
            self.assertEqual(no_model.json()["kind"], "Generation champion")

    def test_training_fitness_and_judge_engagement_are_kept_apart(self):
        import app
        from backend import engine_meta

        self._off()
        engine_id = self._train()["engine_id"]
        text = app.engine_card_ui(engine_id)
        self.assertIn("**Training fitness:**", text)
        self.assertIn("**Judge engagement:** not evaluated yet", text)
        judged = {"stories": 4, "judged": 4, "engagement": 6.25, "criteria": {"surprise": 6.0},
                  "rows": [{"fitness": 10.0}, {"fitness": 14.0}]}
        with patch.object(api.evaluation, "benchmark", return_value=judged) as benchmark:
            job = self.client.post("/v1/jobs/judge-benchmark", json={"stories": 4, "engine_id": engine_id})
            self.assertEqual(job.status_code, 202, job.text)
            self.assertEqual(_finished(self.client, job.json()["id"])["status"], "finished")
            benchmark.assert_called_once()
            pairwise = self.client.post("/v1/jobs/judge-benchmark", json={
                "stories": 4, "engine_id": engine_id, "pairwise": True, "variants": {"climax.enabled": False}})
            _finished(self.client, pairwise.json()["id"])            # a comparison, not this engine's rating
        card = self.client.get(f"/v1/engines/{engine_id}/card").json()
        self.assertEqual(len(card["evaluations"]), 1)
        self.assertEqual((card["evaluations"][0]["kind"], card["evaluations"][0]["engagement"],
                          card["evaluations"][0]["story_fitness"]), ("judge", 6.25, 12.0))
        text = app.engine_card_markdown(card)
        fitness_line = next(line for line in text.splitlines() if "Training fitness" in line)
        judge_line = next(line for line in text.splitlines() if "Judge engagement" in line)
        self.assertNotIn("/ 10", fitness_line)
        self.assertIn("6.25 / 10", judge_line)
        self.assertIn("not the fitness", judge_line)
        # Format 0 (a bare {engine id: name}) is read as names and written back in the current format.
        path = engine_meta.meta_path()
        path.write_text(json.dumps({"old-engine": "From before"}), encoding="utf-8")
        self.assertEqual(engine_meta.names(), {"old-engine": "From before"})
        engine_meta.set_name(engine_id, "New")
        stored = json.loads(path.read_text())
        self.assertEqual(stored["format"], 1)
        self.assertEqual(stored["engines"]["old-engine"], {"name": "From before"})
        path.write_text("not json", encoding="utf-8")                  # unreadable: shown unnamed, no failure
        self.assertEqual(engine_meta.names(), {})


class EngineQuestsTests(_Sandbox):
    """▶ Play's quests follow the chosen engine and the profile: read from that engine's saved world (not
    the active configuration, which is not changed), with the profile's save game; a quest chosen before
    that is no longer offered is cleared, saying why. Two engines trained on worlds with different
    missions, and an active configuration different from both."""

    # Each world has the shared missions and one of its own (a copy of *Find a shield* under its own key).
    A, B, C = "harbor_errand", "vault_errand", "palace_errand"

    def _world(self, own):
        world = copy.deepcopy(self.base)
        missions = world["world_definition"]["missions"]
        missions[own] = {**copy.deepcopy(missions["shield_bearer"]), "title": own.replace("_", " ").title()}
        world["evolution"]["full_length_stories"] = False
        return world

    def _keys(self, own):
        return [*self.base["world_definition"]["missions"], own]

    def _use(self, world):
        store.config.use_override(world)

    def setUp(self):
        super().setUp()
        from backend import export

        for patcher in (patch.dict(os.environ, {"ANE_RECORDS": "off", "ANE_SAVE_DIR": str(self.dir / "quests")}),
                        patch.dict(export.ENGINES, {}, clear=True), patch.dict(export.RUNS, {}, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(lambda: (setattr(store.config, "_override", None), type(store.config).reload(store.config)))
        self.base = store.config.as_dict()
        self.engines = {}
        for name, own in (("A", self.A), ("B", self.B)):
            self._use(self._world(own))
            trained = self.client.post("/v1/simulations", json={"candidate_count": 1, "generations": 1,
                                                                 "simulation_steps": 2, "deterministic": False})
            self.assertEqual(trained.status_code, 200, trained.text)
            self.engines[name] = trained.json()["engine_id"]
        self.current = self._world(self.C)  # the active configuration: neither engine's world
        self._use(self.current)

    def _playable(self, **params):
        answer = self.client.get("/v1/missions/playable", params=params)
        self.assertEqual(answer.status_code, 200, answer.text)
        return answer.json()

    def test_each_engine_s_saved_world_gives_its_own_quests(self):
        from backend import engine_meta

        before = store.config.as_dict()
        for name, own in (("A", self.A), ("B", self.B)):
            with self.subTest(engine=name):
                data = self._playable(engine_id=self.engines[name], profile="player")
                self.assertEqual([m["key"] for m in data["missions"]], self._keys(own))   # its own, not C's
                self.assertEqual((data["engine_id"], data["world"]["source"]), (self.engines[name], "engine"))
                self.assertEqual(data["world"]["digest"], engine_meta.world_digest(self._world(own)))
                self.assertFalse(data["world"]["current"])             # not the active world
                self.assertEqual(data["world"]["summary"]["missions"], len(self._keys(own)))
        active = self._playable(profile="player")
        self.assertEqual([m["key"] for m in active["missions"]], self._keys(self.C))
        self.assertEqual((active["engine_id"], active["world"]["source"], active["world"]["current"]),
                         (None, "active", True))
        self.assertEqual(self._playable(engine_id="latest")["engine_id"], self.engines["B"])
        self.assertEqual(store.config.as_dict(), before)               # nothing was made active
        self.assertEqual(self.client.get("/v1/missions/playable", params={"engine_id": "0" * 32}).status_code, 404)
        # The profile's save game: a goal achieved there is not offered again in that engine's world.
        from core_engine.variables import VariableStore

        VariableStore().complete_goal("dora", self.A)
        quests = {m["key"]: m for m in self._playable(engine_id=self.engines["A"], profile="dora")["missions"]}
        self.assertEqual((quests[self.A]["available"], quests[self.A]["achieved"]), (False, True))
        self.assertTrue(quests["shield_bearer"]["available"])
        self.assertTrue({m["key"]: m for m in self._playable(engine_id=self.engines["A"], profile="player")[
            "missions"]}[self.A]["available"])                         # another profile: still open

    def test_the_picker_follows_engine_and_profile_and_clears_what_no_longer_fits(self):
        import app
        from core_engine.variables import VariableStore

        before = store.config.as_dict()
        offered = lambda picker: [v for _, v in _prop(picker, "choices")]  # noqa: E731
        picker, overview, note = app.play_quests_ui(self.engines["A"], "player", "")
        self.assertIn(self.A, offered(picker))
        self.assertNotIn(self.B, offered(picker))
        self.assertNotIn(self.C, offered(picker))
        self.assertIn("Quests from the saved world of", note)
        self.assertIn(f"engine `{self.engines['A'][:8]}`", note)
        self.assertIn("not the active world", note)
        # Another engine: the quest of A's world is not in B's; cleared, saying why.
        picker, overview, note = app.play_quests_ui(self.engines["B"], "player", self.A)
        self.assertIn(self.B, offered(picker))
        self.assertNotIn(self.A, offered(picker))
        self.assertEqual(_prop(picker, "value"), "")
        self.assertIn(f"**{self.A}**, is not in this engine's world: it was cleared", note)  # no title known
        # The titles listed before (A's world): the cleared quest is named by its title, with its key.
        _, _, _, titles = app.play_quests_titled_ui(self.engines["A"], "player", "", {})
        picker, _, note, titles = app.play_quests_titled_ui(self.engines["B"], "player", self.A, titles)
        title = titles[self.A]
        self.assertNotEqual(title, self.A)
        self.assertIn(f"**{title}** (`{self.A}`), is not in this engine's world: it was cleared", note)
        self.assertIn(self.B, titles)                                   # B's quests are remembered too
        # A quest still offered stays chosen.
        picker, _, note = app.play_quests_ui(self.engines["B"], "player", self.B)
        self.assertEqual(_prop(picker, "value"), self.B)
        self.assertNotIn("cleared", note)
        # Another profile, whose save game has achieved it: cleared, saying so.
        VariableStore().complete_goal("dora", self.B)
        picker, _, note = app.play_quests_ui(self.engines["B"], "dora", self.B)
        self.assertEqual(_prop(picker, "value"), "")
        self.assertIn("already achieved in profile `dora`'s save game", note)
        # No engine: the active world's quests, said so.
        picker, _, note = app.play_quests_ui(None, "player", "")
        self.assertIn(self.C, offered(picker))
        self.assertNotIn(self.A, offered(picker))
        self.assertIn("Quests from the active world", note)
        self.assertEqual(store.config.as_dict(), before)
        # ▶ Play this result with a quest of another world: it is not passed on; the story draws one.
        done = {"engine_id": self.engines["A"], "run_id": None, "kind": "run"}
        opened = app.play_result_ui(done, "player", self.B, "story default", None, titles)
        self.assertIn(f"**{titles[self.B]}** (`{self.B}`), is not offered in this engine's world", opened[-1])
        # Wired: the engine, the profile and the page's load each refresh the quests.
        demo = app.build_app()
        by = {b.elem_id: b for b in demo.blocks.values() if getattr(b, "elem_id", None)}
        engine_picker = next(b for b in demo.blocks.values() if getattr(b, "label", None) == "Trained AI")
        def in_play(block):
            node = getattr(block, "parent", None)
            while node is not None and node is not by["dest-play"]:
                node = getattr(node, "parent", None)
            return node is not None

        profile_picker = next(b for b in demo.blocks.values() if getattr(b, "label", None) == "Player profile"
                              and in_play(b))
        for trigger in (engine_picker, profile_picker):
            names = [fn.fn.__name__ for fn in demo.fns.values() if (trigger._id, "change") in fn.targets]
            self.assertIn("play_quests_titled_ui", names)
        self.assertIn("play-quest-world", by)


class EvaluationTargetTests(_Sandbox):
    """⚖ Evaluate plays a selected trained engine or, explicitly, the baseline (the current configuration with
    random storytellers); the target is said before submission and in the results, and the engine_id meant is
    the one the job plays. In A/B, A and B are both that target (B with version B's settings) on the same
    seeds, each pair judged in both orders. Two engines, and an active configuration unlike both."""

    def setUp(self):
        super().setUp()
        from backend import export

        for patcher in (patch.dict(os.environ, {"ANE_RECORDS": "off", "ANE_SAVE_DIR": str(self.dir / "evaluate")}),
                        patch.dict(export.ENGINES, {}, clear=True), patch.dict(export.RUNS, {}, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(lambda: (setattr(store.config, "_override", None), type(store.config).reload(store.config)))
        self.engines = [self.client.post("/v1/simulations", json={
            "candidate_count": 1, "generations": 1, "simulation_steps": 2, "deterministic": False}).json()["engine_id"]
            for _ in range(2)]
        current = store.config.as_dict()  # the active configuration differs from both engines' saved worlds
        current["world_definition"]["missions"]["evaluate_errand"] = copy.deepcopy(
            current["world_definition"]["missions"]["shield_bearer"])
        store.config.use_override(current)
        self.played, self.compared = [], []

    def _fakes(self):
        """Stories and the judge stand-ins: what is played (engine, seeds, variants) and judged is recorded."""
        played, compared = self.played, self.compared

        def play_stories(count, first_seed=0, engine_id=None, variants=None, budget=None):
            played.append({"count": count, "seed": first_seed, "engine_id": engine_id, "variants": variants})
            return [{"seed": seed, "digest": f"{engine_id}|{sorted((variants or {}).items())}|{seed}",
                     "breakdown": {"term": 1.0}, "fitness": 10.0, "turns": 5, "won": False, "end_kind": "limit"}
                    for seed in range(first_seed, first_seed + count)]

        class Judge:
            def judge_digest(self, digest):
                return {"engagement": 7.0, "scores": {}, "verdict": "fine"}

            def compare_digests(self, digest_a, digest_b):  # GameJudge reads both orders here
                compared.append((digest_a, digest_b))
                return {"winner": "A", "criteria": {}, "reason": "", "agreed": True}

        return patch.object(api.evaluation, "play_stories", side_effect=play_stories), \
            patch.object(api.evaluation, "_judge", return_value=Judge())

    def test_the_target_is_said_and_the_intended_engine_reaches_the_job(self):
        engine_a, engine_b = self.engines
        target = self.client.get("/v1/evaluation/target", params={"engine_id": engine_a}).json()
        self.assertEqual((target["kind"], target["engine_id"]), ("engine", engine_a))
        self.assertTrue(target["label"].endswith(engine_a[:8]))
        self.assertFalse(target["world"]["current"])                     # its saved world, not the active one
        baseline = self.client.get("/v1/evaluation/target").json()
        self.assertEqual((baseline["kind"], baseline["engine_id"], baseline["world"]["current"]),
                         ("baseline", None, True))
        self.assertEqual(self.client.get("/v1/evaluation/target", params={"engine_id": "0" * 32}).status_code, 404)
        plays, judge = self._fakes()
        with plays, judge:
            job = self.client.post("/v1/jobs/judge-benchmark", json={"stories": 3, "seed": 7, "engine_id": engine_a,
                                                                      "workers": 2}).json()
            self.assertEqual(job["target"]["engine_id"], engine_a)          # on the job from its submission
            self.assertIn(engine_a[:8], job["description"])
            done = _finished(self.client, job["id"])
            self.assertEqual(done["status"], "finished", done.get("error"))
            self.assertEqual((done["result"]["target"]["engine_id"], done["result"]["seeds"]), (engine_a, [7, 9]))
            self.assertEqual(self.played, [{"count": 3, "seed": 7, "engine_id": engine_a, "variants": None}])
            # A/B with the selected engine: both sides are that engine, on the same seeds; each pair compared.
            self.played.clear()
            job = self.client.post("/v1/jobs/judge-benchmark", json={
                "stories": 2, "seed": 4, "engine_id": engine_b, "pairwise": True,
                "variants": {"climax.enabled": False}}).json()
            done = _finished(self.client, job["id"])
            self.assertEqual(done["status"], "finished", done.get("error"))
        a_side, b_side = self.played
        self.assertEqual((a_side["engine_id"], b_side["engine_id"]), (engine_b, engine_b))   # not ignored
        self.assertEqual((a_side["seed"], a_side["count"]), (b_side["seed"], b_side["count"]))  # same seeds
        self.assertEqual((a_side["variants"], b_side["variants"]), (None, {"climax.enabled": False}))
        self.assertEqual([(a.rsplit("|", 1)[1], b.rsplit("|", 1)[1]) for a, b in self.compared],
                         [("4", "4"), ("5", "5")])                          # pair by pair, the same seed
        result = done["result"]
        self.assertEqual(result["target"]["engine_id"], engine_b)
        self.assertIn("as it is", result["a"]["label"])
        self.assertEqual(result["b"]["variants"], {"climax.enabled": False})
        # Calibration: the job carries its target too.
        with patch.object(api.evaluation, "calibrate", side_effect=lambda stories, seed, engine_id, *a, **k: {
                "target": api.evaluation.describe_target(engine_id), "term_weights": {}, "n": stories}) as calibrate:
            job = self.client.post("/v1/jobs/calibrate-fitness", json={"stories": 10, "engine_id": engine_a}).json()
            self.assertEqual(job["target"]["engine_id"], engine_a)
            _finished(self.client, job["id"])
        self.assertEqual(calibrate.call_args.args[2], engine_a)

    def test_the_studio_keeps_the_engine_the_baseline_explicit_and_the_plan_visible(self):
        import gradio as gr

        import app

        engine_a, engine_b = self.engines
        # Evaluate this result: the selected trained engine is this one (not the newest, not the baseline).
        opened = app.evaluate_result_ui({"engine_id": engine_a})
        target, picker, note = opened[-3:]
        self.assertEqual((_prop(target, "value"), _prop(picker, "value")), (app.TARGET_ENGINE, engine_a))
        self.assertNotIn("", [value for _, value in _prop(picker, "choices")])  # the baseline is not an engine
        # The plan, before anything is submitted.
        shown, said, card, bench, calib = app.evaluation_plan_ui(app.TARGET_ENGINE, engine_a, False, "", 5, 2, 10, 0)
        self.assertIn("**Selected trained engine:**", said)
        self.assertIn(engine_a, said)
        self.assertIn("seeds 2–6", bench)
        _, _, _, bench, _ = app.evaluation_plan_ui(app.TARGET_ENGINE, engine_a, True, "climax.enabled: false", 3, 0,
                                                   10, 0)
        self.assertIn("**A/B on the same 3 seeds**", bench)
        self.assertIn("both orders", bench)
        self.assertIn("with version B's settings (`climax.enabled: False`)", bench)
        _, said, _, bench, _ = app.evaluation_plan_ui(app.TARGET_BASELINE, engine_a, False, "", 5, 0, 10, 0)
        self.assertIn("**Baseline:**", said)
        self.assertIn("Baseline: the current configuration with random storytellers", bench)
        _, said, _, _, _ = app.evaluation_plan_ui(app.TARGET_ENGINE, None, False, "", 5, 0, 10, 0)
        self.assertIn("No trained engine selected", said)
        # What is submitted: the selected engine; the baseline only when chosen; never one for the other.
        submitted = []

        def stop(path, body=None, **kwargs):
            submitted.append(body["engine_id"])
            raise RuntimeError("stopped before judging")

        with patch.object(app.API, "post", side_effect=stop):
            for engine, target_choice in ((engine_a, app.TARGET_ENGINE), (engine_a, app.TARGET_BASELINE)):
                with self.assertRaises(RuntimeError):
                    app.judge_benchmark_ui(4, 0, engine, False, "", 2, target_choice, progress=lambda *a, **k: None)
                with self.assertRaises(RuntimeError):
                    app.calibrate_fitness_ui(10, 0, engine, False, target_choice, progress=lambda *a, **k: None)
            with self.assertRaises(gr.Error):                               # an engine target needs an engine
                app.judge_benchmark_ui(4, 0, None, False, "", 2, app.TARGET_ENGINE, progress=lambda *a, **k: None)
            with self.assertRaises(gr.Error):                               # A/B needs version B's settings
                app.judge_benchmark_ui(4, 0, engine_a, True, "# none", 2, app.TARGET_ENGINE,
                                       progress=lambda *a, **k: None)
        self.assertEqual(submitted, [engine_a, engine_a, None, None])
        # The result says what it evaluated (through the real job, with the stand-ins).
        plays, judge = self._fakes()
        with plays, judge:
            summary, table = app.judge_benchmark_ui(2, 0, engine_a, True, "climax.enabled: false", 2,
                                                    app.TARGET_ENGINE, progress=lambda *a, **k: None)
        self.assertIn("**A** = ", summary)
        self.assertIn(f"engine `{engine_a}`", summary)
        self.assertNotIn("Current settings (A)", summary)
        # After a training or a loaded engine, the newest is offered and selected; otherwise the choice stays.
        target, picker = app.evaluation_engines_ui(app.TARGET_ENGINE, engine_a, newest=True)
        self.assertEqual(_prop(picker, "value"), engine_b)
        target, picker = app.evaluation_engines_ui(app.TARGET_ENGINE, engine_a)
        self.assertEqual(_prop(picker, "value"), engine_a)
        target, picker = app.evaluation_engines_ui(app.TARGET_BASELINE, engine_a)
        self.assertEqual((_prop(target, "value"), _prop(picker, "visible")), (app.TARGET_BASELINE, False))


class ContentLibraryTests(_Sandbox):
    """🌍 World → Entries' content library: every location, NPC, item and event, searchable, with per-type
    counts and each entry's tags and summary (GET /v1/entities/{kind}?fields=summary, the listing extended
    compatibly). Searching and filtering never change the entry open in the editor; choosing one opens it
    through the editor's guard (unsaved changes: asked first). Empty types and no results say so."""

    def _summary(self, kind):
        answer = self.client.get(f"/v1/entities/{kind}", params={"fields": "summary"})
        self.assertEqual(answer.status_code, 200, answer.text)
        return answer.json()

    def _empty_events(self):
        (self.dir / "events.yaml").write_text("world_definition:\n  events: {}\n", encoding="utf-8")

    def test_the_listing_summarises_each_entry_compatibly(self):
        for kind in ("Locations", "NPCs", "Items", "Events"):
            with self.subTest(kind=kind):
                names = self.client.get(f"/v1/entities/{kind}", params={"fields": "names"}).json()
                self.assertEqual(set(names), {"kind", "names"})               # unchanged
                summary = self._summary(kind)
                self.assertEqual(summary["names"], names["names"])
                self.assertEqual([e["name"] for e in summary["entries"]], names["names"])  # the same order
                for entry in summary["entries"]:
                    self.assertEqual(set(entry), {"name", "tags", "summary", "actions", "facts"})
                    self.assertLessEqual(len(entry["summary"]), 140)
        self.assertIn("entities", self.client.get("/v1/entities/Items").json())     # fields=all unchanged
        harbor = next(e for e in self._summary("Locations")["entries"] if e["name"] == "Harbor")
        self.assertIn("maritime", harbor["tags"])
        self.assertIn("can start here", harbor["facts"])
        event = self._summary("Events")["entries"][0]
        self.assertEqual(event["facts"], [self._yaml("events.yaml")["world_definition"]["events"][event["name"]]["title"]])
        self._empty_events()
        self.assertEqual(self._summary("Events"), {"kind": "Events", "names": [], "entries": []})

    def test_search_type_switching_and_a_stable_selection(self):
        import app

        loaded = {"kind": "Locations", "entity": "Harbor"}
        harbor = app.library_key("Locations", "Harbor")
        filters, results, selected, status = app.library_view("", app.LIBRARY_ALL, loaded)
        counts = {kind: len(self._summary(kind)["names"]) for kind in app.LIBRARY_KINDS}
        self.assertEqual(filters[0], (f"All ({sum(counts.values())})", app.LIBRARY_ALL))
        self.assertIn((f"🧑 NPCs ({counts['NPCs']})", "NPCs"), filters)
        self.assertEqual(len(results), sum(counts.values()))
        self.assertEqual(selected, harbor)
        self.assertIn("Open in the editor: **Harbor** (Locations).", status)
        label = dict((value, text) for text, value in results)[harbor]
        self.assertIn("📍 Harbor", label)
        self.assertIn("tags: urban, maritime", label)                       # its tags and summary
        self.assertIn("— You approach the harbor.", label)
        # A search: each word must match the name, tags, summary or facts; counts say "found of all".
        filters, results, selected, status = app.library_view("maritime", app.LIBRARY_ALL, loaded)
        self.assertTrue(results)
        self.assertTrue(all("maritime" in text.lower() for text, _ in results))
        self.assertEqual(selected, harbor)                                     # still open, still marked
        self.assertRegex(filters[1][0], r"Locations \(\d+ of 16\)")
        # Another type: Harbor is hidden, not replaced; the editor keeps it open.
        filters, results, selected, status = app.library_view("", "NPCs", loaded)
        self.assertTrue(all(json.loads(value)[0] == "NPCs" for _, value in results))
        self.assertIsNone(selected)
        self.assertIn("hidden by the search or filter: it stays open, nothing else was chosen", status)
        # No results, and a type with no entries.
        _, results, _, status = app.library_view("zzqx nothing", app.LIBRARY_ALL, loaded)
        self.assertEqual(results, [])
        self.assertIn("**No entries match “zzqx nothing”.**", status)
        self._empty_events()
        filters, results, _, status = app.library_view("", "Events", loaded)
        self.assertIn(("⚡ Events (0)", "Events"), filters)
        self.assertEqual(results, [])
        self.assertIn("**No events in this world yet.**", status)
        _, _, _, status = app.library_view("", app.LIBRARY_ALL, {}, entries={})
        self.assertIn("**The world has no entries yet.**", status)
        filter_update, results_update, _, more, shown = app.library_ui("harbor", "Locations", loaded)
        self.assertFalse(_prop(more, "visible"))                              # everything is listed
        self.assertEqual(shown, app.LIBRARY_LIMIT)
        self.assertEqual(_prop(filter_update, "value"), "Locations")
        self.assertEqual(_prop(results_update, "value"), harbor)

    def test_renamed_and_deleted_entries_and_the_editor_s_guard(self):
        import gradio as gr

        import app

        loaded = {"kind": "Locations", "entity": "Harbor"}
        item = self.client.get("/v1/entities/Items/Ancient Map").json()
        renamed = self.client.put("/v1/entities/Items/Ancient Map", json={**item, "name": "Faded Map"})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        # The entry form's own save renames what refers to it too (people and places that name the map).
        for name in ("npcs.yaml", "locations.yaml", "items.yaml"):
            self.assertEqual(content._renamed(self._yaml(name), "Ancient Map", "x")[1], 0, name)
        values = [value for _, value in app.library_view("map", app.LIBRARY_ALL, loaded)[1]]
        self.assertIn(app.library_key("Items", "Faded Map"), values)
        self.assertNotIn(app.library_key("Items", "Ancient Map"), values)
        with self.assertRaises(gr.Error) as stale:                             # an entry listed before: gone
            app.library_choice_ui(app.library_key("Items", "Ancient Map"), loaded, {})
        self.assertIn("renamed or deleted", str(stale.exception))
        self.assertEqual(self.client.delete("/v1/entities/NPCs/Smuggler").status_code, 200)
        values = [value for _, value in app.library_view("", "NPCs", loaded)[1]]
        self.assertNotIn(app.library_key("NPCs", "Smuggler"), values)
        # Unsaved changes: choosing in the library asks first, and the editor keeps its entry and draft.
        editor = _Editor(app, "Locations", "Harbor")
        editor.type("fields", 2, "Masts, gulls and a library search.")
        self.assertTrue(editor.unsaved)
        editor._draft(app.library_choice_ui(app.library_key("NPCs", "Priest"), *editor.guard))
        self.assertTrue(editor.prompt)
        self.assertEqual((editor.loaded["kind"], editor.loaded["entity"]), ("Locations", "Harbor"))
        self.assertEqual(editor.pending, {"type": "entry", "kind": "NPCs", "entity": "Priest"})
        editor.answer("discard")
        self.assertEqual((editor.loaded["kind"], editor.loaded["entity"]), ("NPCs", "Priest"))
        # Without unsaved changes it opens at once, in its own content type.
        editor._draft(app.library_choice_ui(app.library_key("Events", "confront_thief"), *editor.guard))
        self.assertFalse(editor.prompt)
        self.assertEqual((editor.kind, editor.loaded["entity"]), ("Events", "confront_thief"))
        # Wired: the library's choice goes through the editor's guard; searching and filtering list again.
        demo = app.build_app()
        by = {b.elem_id: b for b in demo.blocks.values() if getattr(b, "elem_id", None)}

        def listeners(component, event):
            return [fn.fn.__name__ for fn in demo.fns.values() if (component._id, event) in fn.targets]

        self.assertEqual(listeners(by["library-results"], "input"), ["library_choice_ui"])
        self.assertEqual(listeners(by["library-results"], "change"), [])     # a value set by code opens nothing
        self.assertIn("library_search_ui", listeners(by["library-search"], "input"))  # the first page again
        self.assertIn("library_search_ui", listeners(by["library-filter"], "input"))
        self.assertIn("library_more_ui", listeners(by["library-more"], "click"))


class StudioSpeedTests(_Sandbox):
    """What keeps the studio's page responsive, with every event doing what it did: its events are left out
    of Gradio's API description (11 MB in every page), the page's loading-status bookkeeping is kept small
    (Gradio updates every registered component on every status message), and the page and its config are
    sent compressed."""

    def test_the_page_carries_no_api_description_and_its_events_still_run(self):
        import app

        demo = app.build_app()
        self.assertTrue(all(fn.api_visibility != "public" for fn in demo.fns.values()))
        self.assertEqual(demo.get_api_info()["named_endpoints"], {})            # nothing embedded in the page
        undocumented = demo.get_api_info(all_endpoints=True)["named_endpoints"]
        self.assertGreater(len(undocumented), 100)                              # still callable by the page

    def test_loading_status_is_kept_on_what_can_show_it(self):
        import app

        demo = app.build_app()
        by = {b.elem_id: b for b in demo.blocks.values() if getattr(b, "elem_id", None)}
        for fn in demo.fns.values():
            with self.subTest(fn=getattr(fn.fn, "__name__", "?")):
                if fn.show_progress == "hidden":
                    self.assertEqual(fn.show_progress_on, [])                   # it shows nothing: it registers nothing
                elif fn.show_progress_on is not None:
                    self.assertFalse([o for o in fn.show_progress_on if type(o).__name__ in app.NO_PROGRESS_SHOWN])
        loads = {getattr(fn.fn, "__name__", "?"): fn for fn in demo.fns.values()
                 if any(event == "load" for _, event in fn.targets)}
        self.assertEqual(loads["reload_weights_ui"].show_progress_on, [by["weights-draft-status"]])
        self.assertEqual(loads["load_selection_ui"].show_progress_on, [by["draft-status"]])
        # A click's progress stays where it was: Weights' Discard (its fields), a training's follow (its result).
        (discard,) = [fn for fn in demo.fns.values() if getattr(fn.fn, "__name__", "") == "reload_weights_ui"
                      and any(event == "click" for _, event in fn.targets)]
        self.assertGreater(len(discard.show_progress_on), 200)
        config = demo.get_config_file()
        self.assertEqual(len(config["dependencies"]), len(demo.fns))
        self.assertTrue(any(dep.get("show_progress_on") == [] for dep in config["dependencies"]))

    def test_pages_are_compressed_and_streams_and_assets_left_as_they_are(self):
        import app
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.responses import PlainTextResponse, StreamingResponse
        from starlette.routing import Route

        body = "x" * 5000
        site = Starlette(routes=[
            Route("/", lambda request: PlainTextResponse(body)),
            Route("/assets/a.js", lambda request: PlainTextResponse(body)),
            Route("/stream", lambda request: StreamingResponse(iter([b"data: 1\n\n"] * 300), media_type="text/event-stream")),
        ], middleware=[Middleware(app.CompressPages)])
        client = TestClient(site)
        page = client.get("/", headers={"Accept-Encoding": "gzip"})
        self.assertEqual((page.headers.get("content-encoding"), page.text), ("gzip", body))
        self.assertIsNone(client.get("/assets/a.js", headers={"Accept-Encoding": "gzip"}).headers.get("content-encoding"))
        self.assertIsNone(client.get("/stream", headers={"Accept-Encoding": "gzip"}).headers.get("content-encoding"))


class ActivationTests(_Sandbox):
    """When saved settings take effect, said where they are set and when they are saved: the configuration
    (in use at once for what starts afterwards; nothing running restarted) and the app parameters (read
    when the API or the studio starts: saved and running values told apart, a pending restart, and the
    studio's ANE_UI_HOST, ANE_UI_PORT and ANE_UI_SHARE)."""

    def test_app_parameters_saved_against_running_and_the_pending_restart(self):
        import app

        running = dict(store.APP_SETTINGS)
        with patch.object(store, "APP_SETTINGS_PATH", self.dir / "ui.yml"):
            before = self.client.get("/v1/app-settings").json()
            rows = {row["key"]: row for row in before["parameters"]}
            self.assertEqual(before["running"], running)
            self.assertEqual(rows["max_simulation_work"]["readers"], ["API"])
            self.assertEqual(rows["candidate_limit"]["readers"], ["API", "studio"])
            self.assertEqual(rows["server_port"]["readers"], ["studio"])
            self.assertEqual(rows["server_port"]["environment"], "ANE_UI_PORT")
            self.assertFalse(any(row["pending"] for row in before["parameters"]))
            # Saved: nothing running changes; the API says what its restart would use.
            saved = self.client.put("/v1/app-settings", json={"settings": {
                "max_simulation_work": running["max_simulation_work"] * 2, "server_port": 7999}}).json()
            self.assertIn("Nothing that is running changes", saved["message"])
            self.assertIn(f"A restart of the API will use: max_simulation_work {running['max_simulation_work']!r} → "
                          f"{running['max_simulation_work'] * 2!r}", saved["message"])
            self.assertEqual(store.APP_SETTINGS, running)                       # not applied: the API runs as before
            self.assertEqual(sessions.training_work(1, 1, 1, 1)["limit"], running["max_simulation_work"])
            rows = {row["key"]: row for row in saved["parameters"]}
            self.assertTrue(rows["max_simulation_work"]["pending"])
            self.assertEqual(rows["max_simulation_work"]["running_api"], running["max_simulation_work"])
            self.assertFalse(rows["server_port"]["pending"])                    # the API does not read it
            # The studio: its own running values; the environment over the saved launch settings.
            studio = app.studio_launch_values({**before["settings"]}, environ={"ANE_UI_PORT": "7871",
                                                                               "ANE_UI_SHARE": "yes", "ANE_UI_HOST": ""})
            self.assertEqual(studio["server_port"], (7871, "ANE_UI_PORT"))
            self.assertEqual(studio["share"], (True, "ANE_UI_SHARE"))
            self.assertEqual(studio["server_host"], (before["settings"]["server_host"], "saved"))  # empty: unset
            shown = app.app_activation_markdown(saved, running=studio, launched=True,
                                                environ={"ANE_UI_PORT": "7871", "ANE_UI_SHARE": "yes"})
            self.assertIn("⏳ **Restart pending.** The API runs other values than the saved ones for "
                          "max_simulation_work: restart the API.", shown)
            self.assertIn("| server_port | `7999` | — | `7871` (from ANE_UI_PORT) |", shown)
            self.assertIn("set by ANE_UI_PORT", shown)
            self.assertIn("**ANE_UI_PORT** is set in the studio's environment (7871): the studio uses it instead of "
                          "the saved server_port (7999)", shown)
            self.assertNotIn("restart the studio", shown)                       # the environment decides it
            # Without the environment: the studio runs 7860, the saved 7999 waits for its restart.
            plain = app.studio_launch_values(before["settings"], environ={})
            shown = app.app_activation_markdown(saved, running=plain, launched=True, environ={})
            self.assertIn("This studio runs other values than the saved ones for server_port: restart the studio.", shown)
            # The studio's save: the API's message, what this studio's restart would use, the environment kept.
            with patch.object(app, "STUDIO_RUNNING", {**plain, "server_host": ("127.0.0.1", "ANE_UI_HOST")}):
                values = [saved["settings"][key] for key in app.APP_PARAMETER_KEYS]
                values[app.APP_PARAMETER_KEYS.index("server_port")] = 7998
                values[app.APP_PARAMETER_KEYS.index("server_host")] = "0.0.0.0"
                message, table = app.save_app_parameters(*values)
            self.assertIn("A restart of the studio will use: server_port 7860 → 7998", message)
            self.assertIn("ANE_UI_HOST is set ('127.0.0.1'): this studio keeps using it instead of the saved "
                          "server_host ('0.0.0.0')", message)
            self.assertIn("| Parameter | Saved | Running in the API | Running in this studio |", table)
        # The fields say who reads them and which environment variable sets them instead.
        self.assertIn("Read by the API and the studio when it starts", app.app_parameter_info("candidate_limit",
                                                                                           ["API", "studio"]))
        with patch.dict(os.environ, {"ANE_UI_PORT": "7871"}):
            self.assertIn("ANE_UI_PORT is set (7871) and is used instead.", app.app_parameter_info("server_port", ["studio"]))

    def test_configuration_saves_say_what_they_change_and_what_they_do_not(self):
        from backend import activation

        weights = self.client.get("/v1/weights").json()
        path = "fitness.base.npc_goal_bonus"
        value = weights["values"][path]
        saved = self.client.put("/v1/weights", json={"values": {path: value + 1}, "expected": {path: value}})
        self.assertEqual(saved.status_code, 200, saved.text)
        said = saved.json()["message"]
        self.assertIn("to `hyperparameters.yaml`. In use now for what starts from here: new games, training runs and "
                      "evaluations, and jobs still queued (they read it when they start).", said)
        self.assertIn("A game in progress or a job already running keeps the configuration it started with", said)
        self.assertNotIn("set aside", said)                                     # nothing else was in use
        # An uploaded (or a trained engine's) configuration in use: the save sets it aside, and says so.
        store.config.use_override(store.config.as_dict())
        self.addCleanup(lambda: setattr(store.config, "_override", None))
        self.assertTrue(activation.override_active())
        said = store.save_rules({})
        self.assertIn("Saved the story rules to `hyperparameters.yaml`.", said)
        self.assertIn("An uploaded configuration (or a trained engine's) that was in use is set aside", said)
        # The forms carry the same note (GET /v1/reference), and the uploaded configuration says how long it lasts.
        self.assertEqual(self.client.get("/v1/reference").json()["activation"]["configuration"], activation.CONFIG_NOTE)
        uploaded = self.client.post("/v1/config/upload", files={
            "file": ("all.yaml", yaml.safe_dump(store.config.as_dict()).encode(), "application/x-yaml")})
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        self.assertIn("in use now for what starts from here", uploaded.json()["message"])
        self.assertIn("kept in the API's memory only (config/ is not changed) until the next save of any setting",
                      uploaded.json()["message"])
        reset = self.client.post("/v1/config/reset").json()["message"]
        self.assertIn("The saved configuration files (config/) are in use again", reset)

    def test_the_forms_show_when_their_settings_take_effect(self):
        import app

        demo = app.build_app()
        notes = [b for b in demo.blocks.values() if "activation-note" in (getattr(b, "elem_classes", None) or [])]
        self.assertGreaterEqual(len(notes), 4)                                  # story rules, simulation, weights, logic
        self.assertTrue(all(note.value == app.REFERENCE["activation"]["configuration"] for note in notes))
        by = {b.elem_id: b for b in demo.blocks.values() if getattr(b, "elem_id", None)}
        (loaded,) = [fn for fn in demo.fns.values() if fn.fn is app.app_activation_ui]
        self.assertIn(by["app-activation"], loaded.outputs)


class LibraryWatchTests(_Sandbox):
    """The content library follows changes made elsewhere (another browser, a tool, an import, by hand)
    without ↻ Refresh: the studio looks at the world's revision now and then and lists it again only when
    it changed."""

    def test_the_revision_changes_with_the_world_and_the_list_follows(self):
        import app

        first = self.client.get("/v1/world/revision").json()["revision"]
        self.assertEqual(self.client.get("/v1/world/revision").json()["revision"], first)   # stable
        loaded = {"kind": "NPCs", "entity": "Smuggler"}
        # The first look only remembers it (the page has just listed the library); unchanged: nothing.
        self.assertEqual(app.library_watch_ui(None, "", app.LIBRARY_ALL, loaded),
                         (*[app.gr.skip()] * 5, first))
        self.assertEqual(app.library_watch_ui(first, "", app.LIBRARY_ALL, loaded), (*[app.gr.skip()] * 5, first))
        # Another client deletes the entry open here: the revision changes, the list follows and says so.
        time.sleep(0.01)
        self.assertEqual(self.client.delete("/v1/entities/NPCs/Smuggler").status_code, 200)
        second = self.client.get("/v1/world/revision").json()["revision"]
        self.assertNotEqual(second, first)
        filters, results, status, more, shown, seen = app.library_watch_ui(first, "", app.LIBRARY_ALL, loaded)
        self.assertEqual(seen, second)
        self.assertNotIn(app.library_key("NPCs", "Smuggler"), [v for _, v in _prop(results, "choices")])
        self.assertIn("**Smuggler** (NPCs), no longer in the world (renamed or deleted elsewhere)", status)
        # A file edited by hand, and a configuration uploaded in its place: new revisions too.
        time.sleep(0.01)
        items = self.dir / "items.yaml"
        items.write_text(items.read_text() + "\n")
        third = self.client.get("/v1/world/revision").json()["revision"]
        self.assertNotEqual(third, second)
        world = store.config.as_dict()
        store.config.use_override(world)
        self.addCleanup(lambda: setattr(store.config, "_override", None))
        self.assertNotEqual(self.client.get("/v1/world/revision").json()["revision"], third)
        # Wired: a timer in the library, and the page's load, look at the revision.
        demo = app.build_app()
        timers = [fn for fn in demo.fns.values() if getattr(fn.fn, "__name__", "") == "library_watch_ui"]
        self.assertEqual(len(timers), 1)
        self.assertIn("tick", [event for _, event in timers[0].targets])
        self.assertEqual(timers[0].trigger_mode, "always_last")


class LibraryPagingTests(_Sandbox):
    """The content library lists LIBRARY_LIMIT results a page: *Show more* lists the next page (those shown
    stay), a new search or filter starts at the first page again, and the entry open in the editor is
    never cut off (the list reaches it)."""

    def _many(self, count):
        return {"Locations": [{"name": "Harbor", "tags": ["port"], "summary": "", "actions": 0, "facts": []}],
                "NPCs": [], "Events": [],
                "Items": [{"name": f"Trinket {n:03d}", "tags": ["junk"], "summary": "", "actions": 0, "facts": []}
                          for n in range(count)]}

    def test_show_more_pages_through_and_the_open_entry_is_reached(self):
        import app

        entries = self._many(450)
        loaded = {"kind": "Locations", "entity": "Harbor"}
        filters, shown, selected, status, count, total = app._library_view("", app.LIBRARY_ALL, loaded, entries)
        self.assertEqual((count, total), (app.LIBRARY_LIMIT, 451))
        self.assertIn("showing the first **200**: *Show 200 more*", status)
        _, shown, _, status, count, _ = app._library_view("", app.LIBRARY_ALL, loaded, entries, limit=400)
        self.assertEqual(count, 400)
        self.assertIn("*Show 51 more*", status)
        _, shown, _, status, count, _ = app._library_view("", app.LIBRARY_ALL, loaded, entries, limit=600)
        self.assertEqual(count, 451)
        self.assertNotIn("more*", status)
        # The open entry beyond the first page: the list reaches it, and it stays marked.
        far = {"kind": "Items", "entity": "Trinket 349"}
        _, shown, selected, _, count, _ = app._library_view("", "Items", far, entries)
        self.assertEqual((count, selected), (400, app.library_key("Items", "Trinket 349")))
        # The studio's handlers: a page more each time; a search starts again at the first page.
        with patch.object(app, "library_entries", return_value=entries):
            *_, more, limit = app.library_ui("", app.LIBRARY_ALL, loaded)
            self.assertEqual((limit, _prop(more, "value")), (200, "Show 200 more (251 not shown)"))
            *_, more, limit = app.library_more_ui("", app.LIBRARY_ALL, loaded, limit)
            self.assertEqual((limit, _prop(more, "visible")), (400, True))
            *_, more, limit = app.library_more_ui("", app.LIBRARY_ALL, loaded, limit)
            self.assertEqual((limit, _prop(more, "visible")), (451, False))         # all of them
            _, results, _, more, limit = app.library_search_ui("trinket 12", app.LIBRARY_ALL, loaded)
            self.assertEqual(limit, 200)
            self.assertEqual(len(_prop(results, "choices")), sum("12" in f"{n:03d}" for n in range(450)))
            self.assertFalse(_prop(more, "visible"))


class EntrySaveChangesTests(_Sandbox):
    """💾 Save changes: every section of the entry's draft saved at once (PUT .../draft): applied to a copy of
    the stored entry (fields no section touches kept), checked whole with the world, written once. An
    invalid action or condition anywhere: nothing is written, every problem named with its section; the
    form and the YAML then show the same stored entry."""

    def _file(self, name):
        return (self.dir / name).read_text(encoding="utf-8")

    def _harbor(self):
        return self._yaml("locations.yaml")["world_definition"]["locations"]["Harbor"]

    def test_the_endpoint_saves_every_section_at_once_or_nothing(self):
        # Something no form shows is kept by every section's save.
        data = self._yaml("locations.yaml")
        data["world_definition"]["locations"]["Harbor"]["custom_note"] = "kept as written"
        (self.dir / "locations.yaml").write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                                                 encoding="utf-8")
        stored = self.client.get("/v1/entities/Locations/Harbor").json()
        actions = self.client.get("/v1/entities/Locations/Harbor/actions").json()["actions"]
        good_action = {**actions[0], "name": "Watch the ships come in"}
        fields = {**stored, "descriptions": ["Masts as far as the eye can see."], "tags": [*stored["tags"], "busy"]}
        before = self._file("locations.yaml")
        # An invalid action (an unknown item) with valid fields and details: nothing is written.
        bad_action = {**actions[0], "required_object": "No Such Thing"}
        refused = self.client.put("/v1/entities/Locations/Harbor/draft", json={
            "fields": fields, "action": {"index": 0, "action": bad_action},
            "details": {"interact_prompts": ["Walk to the docks"]}})
        self.assertEqual(refused.status_code, 400, refused.text)
        self.assertIn("Nothing was saved", refused.json()["detail"])
        self.assertIn("unknown item `No Such Thing`", refused.json()["detail"])
        self.assertEqual(self._file("locations.yaml"), before)               # not one section written
        # An invalid condition in the fields, with a valid action: nothing is written either.
        refused = self.client.put("/v1/entities/Locations/Harbor/draft", json={
            "fields": {**fields, "enabled_if": {"nope_variable": 1}}, "action": {"index": 0, "action": good_action}})
        self.assertEqual(refused.status_code, 400, refused.text)
        self.assertIn("Fields: `enabled_if` uses undefined variable `nope_variable`", refused.json()["detail"])
        self.assertEqual(self._file("locations.yaml"), before)
        # Two sections that change the same data: refused before anything.
        clash = self.client.put("/v1/entities/Locations/Harbor/draft", json={"fields": fields, "entry_yaml": "tags: []"})
        self.assertEqual(clash.status_code, 400)
        self.assertEqual(self._file("locations.yaml"), before)
        # Valid: every section in one write; what no section touches is kept.
        saved = self.client.put("/v1/entities/Locations/Harbor/draft", json={
            "fields": fields, "action": {"index": 0, "action": good_action},
            "details": {"interact_prompts": ["Walk to the docks"]}})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["saved"], ["action", "details", "fields"])
        harbor = self._harbor()
        self.assertEqual(harbor["entered_descriptions"], ["Masts as far as the eye can see."])
        self.assertIn("busy", harbor["tags"])
        self.assertEqual(harbor["actions"][0]["name"], "Watch the ships come in")
        self.assertEqual(harbor["interact_prompts"], ["Walk to the docks"])
        self.assertEqual(harbor["custom_note"], "kept as written")
        self.assertIn("custom_note: kept as written", self.client.get("/v1/entities/Locations/Harbor/yaml").json()["yaml"])
        # A name that also names something else (here an item called Tavern too): its references cannot be
        # told apart, so they are not renamed, and a rename that would leave them dangling is refused.
        before = {name: self._file(name) for name in ("locations.yaml", "npcs.yaml", "hyperparameters.yaml")}
        world = store.config.as_dict()
        store.config.use_override({**world, "world_definition": {**world["world_definition"], "objects": [
            *world["world_definition"]["objects"], {"name": "Tavern", "tags": ["urban"]}]}})
        try:
            tavern = self.client.get("/v1/entities/Locations/Tavern").json()
            ambiguous = self.client.put("/v1/entities/Locations/Tavern/draft",
                                        json={"fields": {**tavern, "name": "Old Tavern"}})
        finally:
            store.config._override = None
        self.assertEqual(ambiguous.status_code, 400)
        self.assertIn("The world: routes", ambiguous.json()["detail"])
        self.assertIn("`Tavern` also names an item", ambiguous.json()["detail"])
        self.assertEqual({name: self._file(name) for name in before}, before)
        # A rename renames what refers to the entry: the map's routes, the people who know the place, the
        # tuning parameters that name it; the other files are edited in place (their comments kept).
        renamed = self.client.put("/v1/entities/Locations/Harbor/draft", json={"fields": {**fields, "name": "Old Harbor"}})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["references"], {"locations.yaml": 6, "npcs.yaml": 3, "side_quests.yaml": 1,
                                                        "hyperparameters.yaml": 2})  # a side quest's goal, a haunt too
        self.assertIn("Updated 12 reference(s) to `Harbor` → `Old Harbor`", renamed.json()["message"])
        for name in ("locations.yaml", "npcs.yaml", "hyperparameters.yaml", "missions.yaml", "variables.yaml"):
            self.assertEqual(content._renamed(self._yaml(name), "Harbor", "x")[1], 0, name)  # nothing names Harbor
        self.assertIn("Old Harbor", self._yaml("locations.yaml")["world_definition"]["routes"])
        for name in ("npcs.yaml", "hyperparameters.yaml"):
            self.assertEqual(self._file(name).count("#"), before[name].count("#"), name)  # comments kept
            self.assertEqual(self._file(name).replace("Old Harbor", "Harbor"), before[name])  # and nothing else
        # A new entry is made from its fields; renamed, it stays where it is.
        made = self.client.put("/v1/entities/Items/Brass Key/draft", params={"create": "true"}, json={
            "fields": {"name": "Brass Key", "tags": ["urban"], "descriptions": ["A small brass key."]}})
        self.assertEqual(made.status_code, 200, made.text)
        names = self.client.get("/v1/entities/Items", params={"fields": "names"}).json()["names"]
        renamed = self.client.put("/v1/entities/Items/Brass Key/draft", json={
            "fields": {"name": "Iron Key", "tags": ["urban"], "descriptions": ["A small iron key."]}})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(self.client.get("/v1/entities/Items", params={"fields": "names"}).json()["names"],
                         [*names[:-1], "Iron Key"])

    def test_the_studio_saves_the_whole_draft_or_nothing(self):
        import app

        before = self._file("locations.yaml")
        editor = _Editor(app, "Locations", "Harbor")
        editor.type("fields", 2, "Masts as far as the eye can see.")              # Fields
        editor.type("fields", 7, "{nope_variable: 1}")                            # an invalid condition
        actions = yaml.safe_load(editor.value("actions_yaml")) or []
        actions[0]["name"] = "Watch the ships come in"
        editor.type("actions_yaml", 0, yaml.safe_dump(actions, sort_keys=False))  # Actions as YAML (valid)
        self.assertEqual(set(editor.unsaved), {"fields", "actions_yaml"})
        status = app.draft_status_markup(editor.unsaved, editor.loaded)
        self.assertIn("● Unsaved changes in Harbor: Fields, Actions as YAML", status)
        self.assertIn("✓ Saved: Actions &amp; outcomes, More details, Every field (YAML)", status)
        self.assertNotIn("Scheduling", status)                                      # places have none
        out = app.save_changes_ui(*editor.guard)
        message = out[app.ENTRY_OUTPUTS]
        self.assertIn("Nothing was saved", message)
        self.assertIn("nope_variable", message)
        self.assertEqual(out[-1], [])                                               # nothing captured as saved
        self.assertEqual(self._file("locations.yaml"), before)                      # no section written
        self.assertEqual(set(editor.unsaved), {"fields", "actions_yaml"})           # the draft is kept
        # The leave prompt's Save is the same single save: still nothing written.
        editor.choose("Locations", "Market")
        self.assertTrue(editor.prompt)
        editor.answer("save")
        self.assertTrue(editor.prompt)
        self.assertIn("Nothing was saved", editor.message)
        self.assertEqual(self._file("locations.yaml"), before)
        self.assertEqual(editor.loaded["entity"], "Harbor")
        editor.answer("stay")
        # Corrected: both sections saved at once; the forms and the YAML show the stored entry.
        editor.type("fields", 7, "")
        out = app.save_changes_ui(*editor.guard)
        self.assertIn("Saved **Harbor** at once: Fields, Actions as YAML", out[app.ENTRY_OUTPUTS])
        self.assertEqual(out[-1], list(app.DRAFT_PARTS))
        editor._form(out)
        harbor = self._harbor()
        self.assertEqual(harbor["entered_descriptions"], ["Masts as far as the eye can see."])
        self.assertEqual(harbor["actions"][0]["name"], "Watch the ships come in")
        self.assertEqual(editor.unsaved, [])
        self.assertIn("Masts as far as the eye can see.", editor.value("entry_yaml"))
        self.assertIn("Watch the ships come in", editor.value("entry_yaml"))
        self.assertIn("No unsaved changes", app.save_changes_ui(*editor.guard)[app.ENTRY_OUTPUTS])
        # Wired: the persistent button runs the whole save through the editor's draft handling.
        demo = app.build_app()
        by = {b.elem_id: b for b in demo.blocks.values() if getattr(b, "elem_id", None)}
        (fn,) = [fn for fn in demo.fns.values() if (by["entry-save-changes"]._id, "click") in fn.targets]
        self.assertEqual(fn.fn.__name__, "save_changes_ui")


class EntryPreviewTests(_Sandbox):
    """👁 Preview: the entry as its unsaved draft makes it, static (the content as written) and simulated (a
    sample scene the engine plays in a separate process, its assumptions said), with no persistent side
    effect: no configuration written, no player's progression changed, nothing recorded, no session
    advanced."""

    def setUp(self):
        super().setUp()
        patcher = patch.dict(os.environ, {"ANE_RECORDS_DIR": str(self.dir / "records")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: (setattr(store.config, "_override", None), type(store.config).reload(store.config)))

    def _draft(self):
        harbor = self.client.get("/v1/entities/Locations/Harbor").json()
        return {"fields": {**harbor, "descriptions": ["A gull-loud quay under a bruised sky."]},
                "action": {"index": 0, "action": {"name": "Feed the gulls", "outcomes": [
                    {"desc": "The gulls mob you for the bread.", "health_change": -1}]}}}

    def _everything(self, token):
        """What a preview must not change: the config files, the active configuration, the profiles' saved
        progression, the records' stories, and the game in progress."""
        from core_engine.variables import VariableStore

        files = {name: (self.dir / name).read_bytes() for name in sorted(os.listdir(self.dir)) if name.endswith(".yaml")}
        progress = Path(VariableStore().path)
        state = sessions.SESSIONS[token]
        return {"files": files, "config": store.config.as_dict(),
                "progression": progress.read_bytes() if progress.exists() else None,
                "stories": len(records.list_stories()), "session": (state.actions_taken, len(state.history))}

    def test_map_changes_are_listed_with_the_others(self):
        from backend import preview

        base = {"place": "Harbor", "health": 5, "coins": 0, "inventory": [], "variables": {}, "ended": False,
                "routes": [("Harbor", "Tavern"), ("Tavern", "Harbor")], "unlocked": [], "confined": "Dungeon"}
        after = {**base, "routes": [*base["routes"], ("Harbor", "Market"), ("Market", "Harbor"), ("Sewers", "Vault")],
                 "unlocked": ["Dungeon"], "confined": None}
        self.assertEqual(preview.scene_changes(base, after),
                         ["a path opens between Harbor and Market", "a path opens from Sewers to Vault",
                          "Dungeon can be left (unlocked)", "no longer held in Dungeon"])
        self.assertEqual(preview.scene_changes(after, base),
                         ["a path closes between Harbor and Market", "a path closes from Sewers to Vault",
                          "held in Dungeon"])
        # In the engine's sample scene: an outcome that reveals a place opens a path there.
        draft = self._draft()
        draft["action"]["action"]["outcomes"] = [{"desc": "A sailor points out the way to the Market.",
                                                  "reveal_location": "Market"}]
        sim = self.client.post("/v1/entities/Locations/Harbor/preview", params={"simulate": "true", "seed": 2},
                               json=draft).json()["simulated"]
        fed = next(a for a in sim["actions"] if a["name"] == "Feed the gulls")
        self.assertTrue(any(change.startswith("a path opens") and "Harbor" in change for change in fed["changes"]),
                        fed["changes"])

    def test_the_draft_is_previewed_static_and_simulated_without_side_effects(self):
        game = self.client.post("/v1/simulations", json={"candidate_count": 1, "generations": 1,
                                                         "simulation_steps": 2, "deterministic": False}).json()
        token = game["session_id"]
        before = self._everything(token)
        stored = self.client.get("/v1/entities/Locations/Harbor").json()
        static = self.client.post("/v1/entities/Locations/Harbor/preview", json=self._draft())
        self.assertEqual(static.status_code, 200, static.text)
        result = static.json()
        self.assertEqual((result["saved"], result["problems"], result["simulated"]), (False, [], None))
        self.assertEqual(result["sections"], ["action", "fields"])
        arrival = next(t for t in result["static"]["texts"] if t["label"] == "On arrival")
        self.assertEqual(arrival["lines"], ["A gull-loud quay under a bruised sky."])
        gulls = result["static"]["actions"][0]
        self.assertEqual(gulls["name"], "Feed the gulls")
        self.assertEqual(gulls["outcomes"][0], {"text": "The gulls mob you for the bread.", "effects": ["health -1"],
                                                "variants": 0})
        # The engine's sample scene, with the draft, and what it assumes.
        simulated = self.client.post("/v1/entities/Locations/Harbor/preview", params={"simulate": "true", "seed": 2},
                                     json=self._draft()).json()
        self.assertIsNone(simulated["simulated_error"])
        sim = simulated["simulated"]
        self.assertEqual((sim["start"], sim["present"]), ("Harbor", True))
        self.assertIn("A gull-loud quay under a bruised sky.", sim["description"])
        fed = next(a for a in sim["actions"] if a["name"] == "Feed the gulls")
        self.assertTrue(fed["offered"])
        self.assertIn("The gulls mob you for the bread.", fed["text"])
        self.assertTrue(any(change.startswith("health") for change in fed["changes"]))
        self.assertTrue(any("defaults, not any profile's stored values" in a for a in sim["assumptions"]))
        self.assertTrue(any("seed 2" in a for a in sim["assumptions"]))
        # Nothing persisted: files, configuration, progression, records, the game in progress.
        self.assertEqual(self._everything(token), before)
        self.assertEqual(self.client.get("/v1/entities/Locations/Harbor").json(), stored)
        # A draft a save would refuse is still previewed, its problems listed.
        bad = self._draft()
        bad["action"]["action"]["required_object"] = "No Such Thing"
        refused = self.client.post("/v1/entities/Locations/Harbor/preview", json=bad).json()
        self.assertTrue(any("unknown item `No Such Thing`" in p for p in refused["problems"]))
        self.assertIn("needs No Such Thing", refused["static"]["actions"][0]["requirements"])
        self.assertEqual(self._everything(token), before)
        # No draft: the stored entry; an unknown entry: 404.
        self.assertEqual(self.client.post("/v1/entities/Locations/Harbor/preview").json()["sections"], [])
        self.assertEqual(self.client.post("/v1/entities/Locations/Nowhere/preview").status_code, 404)

    def test_the_studio_previews_the_editor_s_unsaved_draft(self):
        import app

        before = {name: (self.dir / name).read_bytes() for name in ("locations.yaml", "npcs.yaml")}
        editor = _Editor(app, "Locations", "Harbor")
        editor.type("fields", 2, "A gull-loud quay under a bruised sky.")
        text = app.preview_entry_ui(*editor.guard)
        self.assertIn("A gull-loud quay under a bruised sky.", text)
        self.assertIn("Unsaved sections shown: Fields", text)
        self.assertIn("nothing is saved", text)
        self.assertIn("**Actions:**", text)
        scene = app.simulate_entry_ui(editor.loaded, editor.base, 1, *editor.values)
        self.assertIn("**Sample state (assumptions):**", scene)
        self.assertIn("A gull-loud quay under a bruised sky.", scene)
        self.assertIn("seed 1", scene)
        self.assertEqual(editor.unsaved, ["fields"])                            # the draft stays a draft
        self.assertEqual({name: (self.dir / name).read_bytes() for name in before}, before)
        # An NPC is brought into its sample scene, and says so.
        editor = _Editor(app, "NPCs", "Priest")
        scene = app.simulate_entry_ui(editor.loaded, editor.base, 0, *editor.values)
        self.assertIn("Priest is brought into the scene", scene)
        # Wired: the preview reads the editor's draft.
        demo = app.build_app()
        by = {b.elem_id: b for b in demo.blocks.values() if getattr(b, "elem_id", None)}
        for elem, name in (("entry-preview-button", "preview_entry_ui"), ("entry-simulate-button", "simulate_entry_ui")):
            (fn,) = [fn for fn in demo.fns.values() if (by[elem]._id, "click") in fn.targets]
            self.assertEqual(fn.fn.__name__, name)


class ConditionBuilderTests(_Sandbox):
    """The condition builder (🌍 World → Entries: an entry's *Enabled if*, an action's *Offered only if*):
    groups (ALL, ANY, NOT) and predicates chosen from the world (GET /v1/conditions/reference), checked as a
    save checks them (POST /v1/conditions/validate). A condition it reads is written back as it was; what it
    cannot show is kept as written, never dropped or simplified; the YAML field stays the advanced mode."""

    SAMPLES = (
        "{thief_caught: false, reputation: {gte: 2, lt: 5}}",                      # the variables' shorthand
        "{reputation: 3}",
        "[{has_item: Royal Pass}, {visited: Palace}]",                             # a list: all of them
        "{any: [{has_item: Ancient Map}, {not: {stat: persuasion, op: gte, value: 60}}, "
        "{all: [{mission: shield_bearer}, {variable: reputation, value: 1}]}]}",   # nested; an op left out
        "{resource: coins, op: in, value: [1, 2]}",
        "{all: [{has_item: Ancient Map, extra: 1}, {visited: Market}]}",           # a part it cannot show
        "{has_item: [Ancient Map, Royal Pass]}",
        "{not: {any: [{event_done: tavern_brawl}, {action_done: (Priest) Seek a blessing}]}}",
        "{any: []}",                                                               # groups being built
        "{all: [{not: null}, {any: [{visited: Market}]}]}",
    )

    def test_the_reference_and_the_check_come_from_the_backend(self):
        ref = self.client.get("/v1/conditions/reference").json()
        self.assertEqual(ref["groups"], ["all", "any", "not"])
        self.assertIn("Ancient Map", ref["facts"]["has_item"]["names"])
        self.assertIn("Market", ref["facts"]["visited"]["names"])
        self.assertIn("shield_bearer", ref["facts"]["mission"]["names"])
        self.assertIsNone(ref["facts"]["action_done"]["refers_to"])                # free text
        self.assertEqual(ref["comparisons"]["variable"]["reputation"], "int")
        self.assertIn("persuasion", ref["comparisons"]["stat"])
        self.assertEqual(ref["comparisons"]["resource"], ["coins", "health", "turn"])

        def check(condition):
            return self.client.post("/v1/conditions/validate", json={"condition": condition}).json()

        self.assertTrue(check({"any": [{"has_item": "Ancient Map"}, {"visited": "Market"}]})["valid"])
        self.assertEqual(check(None), {"valid": True, "errors": [], "empty": True})
        bad = check({"all": [{"has_item": "Nope"}, {"stat": "charm", "op": "gte", "value": 2},
                             {"variable": "reputation", "op": "gte", "value": "high"}]})
        self.assertFalse(bad["valid"])
        self.assertTrue(any("unknown item `Nope`" in e for e in bad["errors"]))
        self.assertTrue(any("undefined stat `charm`" in e for e in bad["errors"]))
        self.assertEqual(len(bad["errors"]), 3)
        self.assertFalse(check("{any: [")["valid"])                                # YAML text, unreadable

    def test_conditions_round_trip_exactly_and_unsupported_parts_are_kept(self):
        import app

        for text in self.SAMPLES:
            with self.subTest(text=text):
                value = yaml.safe_load(text)
                tree = app.condition_tree(value)
                self.assertEqual(app.condition_value(tree), value)                     # nothing changed
                self.assertEqual(app.condition_text(app.condition_yaml(app.condition_value(tree))), value)
        self.assertIsNone(app.condition_tree(None))
        self.assertEqual(app.condition_text("reputation: {gte: 1}"), {"reputation": {"gte": 1}})  # braces optional
        # Edited, a part the builder cannot show is kept as it was written.
        tree = app.condition_tree(yaml.safe_load(self.SAMPLES[5]))
        rows = app.condition_rows(tree)
        self.assertTrue(rows[1][3].startswith("⚙ kept as written"))
        tree = app.condition_add(tree, "top", app.condition_node("mission", "shield_bearer"))
        self.assertEqual(app.condition_value(tree), {"all": [{"has_item": "Ancient Map", "extra": 1},
                                                             {"visited": "Market"}, {"mission": "shield_bearer"}]})

    def test_nested_groups_shorthand_and_invalid_references(self):
        import gradio as gr

        import app
        from core_engine import conditions

        # An empty group written to the field and read back is still that group (more can be added into it).
        empty = app.condition_tree(app.condition_text(app.condition_yaml(app.condition_value(
            app.condition_add(None, "top", app.condition_node("any"))))))
        self.assertEqual((empty["t"], empty["g"], empty["c"]), ("group", "any", []))
        self.assertIn("needs a non-empty list", app.condition_status(empty))
        # Built from nothing: ANY of (has item, NOT (stat ≥ 60), ALL (visited, variable)).
        tree = app.condition_add(None, "top", app.condition_node("any"))
        tree = app.condition_add(tree, "top", app.condition_node("has_item", "Ancient Map"))
        tree = app.condition_add(tree, "top", app.condition_node("not"))
        tree = app.condition_add(tree, "top.1", app.condition_node("stat", "persuasion", "gte", "60"))
        with self.assertRaises(gr.Error):                                       # NOT holds one condition
            app.condition_add(tree, "top.1", app.condition_node("visited", "Market"))
        tree = app.condition_add(tree, "top", app.condition_node("all"))
        tree = app.condition_add(tree, "top.2", app.condition_node("visited", "Market"))
        tree = app.condition_add(tree, "top.2", app.condition_node("variable", "reputation", "eq", "1"))
        built = {"any": [{"has_item": "Ancient Map"}, {"not": {"stat": "persuasion", "op": "gte", "value": 60}},
                         {"all": [{"visited": "Market"}, {"variable": "reputation", "value": 1}]}]}
        self.assertEqual(app.condition_value(tree), built)
        self.assertIn("✓ Valid", app.condition_status(tree))
        tree = app.condition_replace(tree, "top", app.condition_node("all"))       # a group changed keeps its parts
        self.assertEqual(list(app.condition_value(tree)), ["all"])
        self.assertEqual(len(app.condition_value(tree)["all"]), 3)
        tree = app.condition_remove(tree, "top.2.0")
        self.assertEqual(app.condition_value(tree)["all"][2], {"all": [{"variable": "reputation", "value": 1}]})
        self.assertIsNone(app.condition_remove(tree, "top"))
        # The shorthand: a variable added stays shorthand; another predicate makes it a written ALL, same meaning.
        before = yaml.safe_load(self.SAMPLES[0])
        tree = app.condition_tree(before)
        tree = app.condition_add(tree, "top", app.condition_node("variable", "fence_exposed", "eq", "true"))
        self.assertEqual(app.condition_value(tree), {**before, "fence_exposed": True})
        tree = app.condition_replace(tree, "top.1", app.condition_node("variable", "reputation", "gt", "2"))
        self.assertEqual(app.condition_value(tree)["reputation"], {"gt": 2, "lt": 5})
        tree = app.condition_add(tree, "top", app.condition_node("has_item", "Ancient Map"))
        written = app.condition_value(tree)
        self.assertEqual(list(written), ["all"])
        expected = {"all": [{"thief_caught": False}, {"reputation": {"gt": 2, "lt": 5}}, {"fence_exposed": True},
                            {"has_item": "Ancient Map"}]}
        def flat(tree):  # ALL inside ALL means the same as one ALL
            if tree[0] != "all":
                return tree
            parts = []
            for child in (flat(c) for c in tree[1]):
                parts.extend(child[1] if child[0] == "all" else (child,))
            return ("all", tuple(parts))

        self.assertEqual(flat(conditions.normalize(written)), flat(conditions.normalize(expected)))  # the same meaning
        # An invalid reference: written, and said not valid (a save refuses it).
        bad = app.condition_add(None, "top", app.condition_node("has_item", "No Such Thing"))
        status = app.condition_status(bad, "Enabled if")
        self.assertIn("Not valid", status)
        self.assertIn("unknown item `No Such Thing`", status)
        with self.assertRaises(gr.Error):
            app.condition_node("stat", "persuasion", "gte", "")                    # a comparison needs a value

    def test_switching_between_the_builder_and_the_yaml(self):
        import app

        text = self.SAMPLES[3]
        mode, box, field, tree, ref, shown, targets, status = app.condition_mode_ui("Builder", text)
        self.assertTrue(box["visible"])
        self.assertFalse(_prop(field, "interactive"))                                # the builder writes the field
        self.assertIn("Stat **persuasion** ≥ `60`", shown)
        self.assertIn("✓ Valid", status)
        self.assertIn("top.1.0", [value for _, value in _prop(targets, "choices")])
        self.assertIn("Ancient Map", ref["facts"]["has_item"]["names"])
        # An edit in the builder writes the field; the YAML read back is the builder's condition.
        tree, written, shown, targets, status = app.condition_edit_ui(
            "add", tree, "top.2", "item_collected", "Royal Pass", None, "")
        self.assertEqual(app.condition_text(written)["any"][2]["all"][-1], {"item_collected": "Royal Pass"})
        self.assertEqual(app.condition_tree(app.condition_text(written)), tree)
        # A part picked: the form shows it; a group shows its kind.
        self.assertEqual(app.condition_pick_ui(tree, "top.1.0", ref)[0], "stat")
        self.assertEqual(_prop(app.condition_pick_ui(tree, "top.1.0", ref)[3], "value"), "60")
        self.assertEqual(app.condition_pick_ui(tree, "top.2", ref)[0], "all")
        # Back to the YAML: the field is editable again, as written.
        mode, box, field, *_ = app.condition_mode_ui("Advanced YAML", written)
        self.assertEqual((box["visible"], _prop(field, "interactive")), (False, True))
        # YAML the builder cannot read: it stays in Advanced YAML, saying why; nothing is lost.
        mode, box, field, tree_out, *_, status = app.condition_mode_ui("Builder", "{any: [")
        self.assertEqual(_prop(mode, "value"), "Advanced YAML")
        self.assertIn("not valid YAML", status)
        self.assertEqual(tree_out, app.gr.skip())
        # The field changed by a load: the builder follows it (only in Builder mode).
        tree, shown, targets, status = app.condition_sync_ui("Builder", "{visited: Palace}")
        self.assertEqual(app.condition_value(tree), {"visited": "Palace"})
        self.assertEqual(app.condition_sync_ui("Builder", "{visited: Palace}", "enabled_if", tree)[0],
                         app.gr.skip())                                       # what the builder wrote: kept
        self.assertEqual(app.condition_sync_ui("Advanced YAML", "{visited: Palace}")[0], app.gr.skip())
        # Wired: every condition field has a builder that writes that field, then its draft's status: an
        # entry's Enabled if, an action's Offered only if and No longer offered once, a mission's Offered
        # only if, and a dialogue line's Said when.
        demo = app.build_app()
        by = {b.elem_id: b for b in demo.blocks.values() if getattr(b, "elem_id", None)}
        labelled = lambda label: [b for b in demo.blocks.values() if getattr(b, "label", None) == label]  # noqa: E731
        fields = {"entry-condition": labelled("Enabled if (condition)")[0],
                  "action-condition": labelled("Offered only if")[0],
                  "action-until": labelled("No longer offered once")[0],
                  "mission-condition": labelled("Offered only if")[1],
                  "dialogue-condition": by["dialogue-condition-text"]}
        for prefix, field in fields.items():
            for button in ("add", "replace", "remove"):
                (fn,) = [fn for fn in demo.fns.values() if (by[f"{prefix}-{button}"]._id, "click") in fn.targets]
                self.assertIn(field, fn.outputs, prefix)                         # the builder writes its field
                followers = [f for f in demo.fns.values() if f.trigger_after == fn._id]
                self.assertEqual(len(followers), 1)                              # the draft's status
            (mode,) = [fn for fn in demo.fns.values() if (by[f"{prefix}-mode"]._id, "input") in fn.targets]
            self.assertIn(field, mode.inputs)

    def test_a_dialogue_line_s_condition_is_built_in_step_with_the_table(self):
        import app
        import pandas as pd

        table = pd.DataFrame([["has_item: Ancient Map", "You found it!"], ["", "Move along."]],
                             columns=["Said when (condition, empty: always)", "Says"])
        picker, field = app.dialogue_lines_ui(table, None, "")
        self.assertEqual(_prop(picker, "choices"), [("1. You found it!", 0), ("2. Move along.", 1)])
        self.assertEqual(_prop(picker, "value"), 0)                               # the first line, by default
        self.assertEqual(_prop(field, "value"), "has_item: Ancient Map")
        self.assertEqual(app.dialogue_lines_ui(table, 0, "has_item: Ancient Map")[1], app.gr.skip())  # no echo
        self.assertEqual(_prop(app.dialogue_line_condition_ui(table, 1), "value"), "")
        # The builder writes the field: into its line only, and only when it differs.
        tree, written, *_ = app.condition_edit_ui("add", app.condition_tree(None), app.CONDITION_TOP, "visited",
                                                  "Market", None, "", "dialogue")
        changed = app.dialogue_write_condition_ui(table, 1, written)
        self.assertEqual(app.condition_text(changed.iat[1, 0]), {"visited": "Market"})
        self.assertEqual(changed.iat[0, 0], "has_item: Ancient Map")              # the other line as it was
        self.assertEqual(changed.iat[1, 1], "Move along.")
        self.assertEqual(app.dialogue_write_condition_ui(changed, 1, written), app.gr.skip())
        self.assertEqual(app.dialogue_write_condition_ui(table, 5, written), app.gr.skip())   # no such line
        # The table changed by a load: the field follows the chosen line (a line no longer there: the first).
        picker, field = app.dialogue_lines_ui(changed.iloc[:1], 1, written)
        self.assertEqual(_prop(picker, "value"), 0)
        self.assertEqual(_prop(field, "value"), "has_item: Ancient Map")
        picker, field = app.dialogue_lines_ui(None, None, written)               # no lines: nothing to build
        self.assertEqual((_prop(picker, "choices"), _prop(picker, "value"), _prop(field, "value")), ([], None, ""))
        self.assertEqual(app.dialogue_write_condition_ui(None, None, written), app.gr.skip())


class WorldSubnavigationTests(_Sandbox):
    """🌍 World's sub-navigation: 📚 Content (Entries, Story rules, Behaviour by tags, Grow, Checks), 🎯
    Missions and 🗺 Map select the World's sections through the same guard as before (unsaved changes:
    asked in the editor first; nothing discarded); Content goes back to its last section. The authoring
    map (the whole world) is told apart from the discovered map a player sees in ▶ Play."""

    def setUp(self):
        super().setUp()
        shutil.copy(store.CONFIG_DIR / "locations.yaml", self.dir / "locations.yaml")

    def test_the_sub_navigation_selects_sections_through_the_guard(self):
        import app

        self.assertEqual(app.world_group_target("missions", "entries"), "missions")
        self.assertEqual(app.world_group_target("map", "missions"), "map")
        self.assertEqual(app.world_group_target("content", "map", "story-rules"), "story-rules")  # the last one
        self.assertEqual(app.world_group_target("content", "map", None), "entries")
        self.assertEqual(app.world_group_target("content", "checks", "entries"), "checks")
        # The bar follows the section open: its place highlighted, Content's own row only in Content.
        out = app.world_nav_ui("map", "story-rules")
        tops, row, inner, last = out[:3], out[3], out[4:-1], out[-1]
        self.assertEqual([_prop(b, "variant") for b in tops], ["secondary", "secondary", "primary"])
        self.assertTrue(all(_prop(b, "variant") == "secondary" for b in inner))  # no Content section open
        self.assertFalse(row["visible"])
        self.assertEqual(last, "story-rules")                                   # remembered
        out = app.world_nav_ui("behaviour", "entries")
        self.assertTrue(out[3]["visible"])
        self.assertEqual([_prop(b, "variant") for b in out[4:-1]],
                         ["primary" if key == "behaviour" else "secondary" for key in app.CONTENT_SECTIONS])
        self.assertEqual(out[-1], "behaviour")
        # Nothing unsaved: the section opens (its tab selected). Unsaved entry: asked first, nothing discarded.
        loaded = {"kind": "Locations", "entity": "Harbor"}
        self.assertEqual(app.world_section_choice_ui("map", "entries", loaded, {}),
                         {"section_now": "map", "section": "map"})
        asked = app.world_section_choice_ui("missions", "entries", loaded, {"entries": ["fields"]})
        self.assertEqual(asked["section"], "entries")                           # stays where the draft is
        self.assertNotIn("section_now", asked)
        self.assertEqual(asked["prompts"]["entries"][2], {"type": "section", "id": "missions"})
        editor = _Editor(app, "Locations", "Harbor")
        editor.type("fields", 2, "Kept while away.")
        editor.leave(app.world_section_choice_ui("map", "entries", editor.loaded, editor.dirty))
        editor.answer("stay")
        self.assertEqual((editor.value("fields", 2), editor.unsaved), ("Kept while away.", ["fields"]))
        # Wired: each button goes through the guard (the leave outputs), and the bar follows section_now.
        demo = app.build_app()
        by = {b.elem_id: b for b in demo.blocks.values() if getattr(b, "elem_id", None)}
        tabs = by["world-sections"]
        for key in [*app.WORLD_GROUPS, *[f"content-{k}" for k in app.CONTENT_SECTIONS]]:
            elem = f"world-nav-{key}" if key in app.WORLD_GROUPS else f"world-{key}"
            (fn,) = [fn for fn in demo.fns.values() if (by[elem]._id, "click") in fn.targets]
            self.assertIn(tabs._id, [o._id for o in fn.outputs])
        followers = [fn for fn in demo.fns.values() if fn.fn is not None and getattr(fn.fn, "__name__", "") == "world_nav_ui"]
        self.assertEqual(len(followers), 1)
        self.assertIn("#world-sections > .tab-wrapper {display: none !important}", app.CSS)
        titles = {b.label for b in demo.blocks.values() if isinstance(b, app.gr.Tab) and getattr(b, "parent", None) is tabs}
        self.assertEqual(titles, set(app.WORLD_SECTIONS.values()))              # every section still there

    def test_missions_and_the_map_keep_working_and_the_two_maps_are_told_apart(self):
        import app

        # The map's borders, saved from the studio: both places follow.
        before = self.client.get("/v1/map").json()["routes"]
        borders = sorted(set(before["Tavern"]) - {"Alley"} | {"Sewers"})
        view, message, yaml_text = app.save_borders_ui("Tavern", borders)
        routes = self.client.get("/v1/map").json()["routes"]
        self.assertEqual(sorted(routes["Tavern"]), borders)
        self.assertIn("Tavern", routes["Sewers"])                               # added both ways
        self.assertNotIn("Tavern", routes.get("Alley", []))                     # removed both ways
        self.assertIn("Sewers", yaml.safe_load(yaml_text)["routes"]["Tavern"])  # the YAML follows
        # A mission edited in its form: its steps, condition and rewards saved.
        mission = self.client.get("/v1/missions/shield_bearer").json()["mission"]
        mission = {**mission, "title": "Find a shield, again", "enabled_if": {"reputation": {"gte": 1}},
                   "rewards": [{"change_coins": 7}]}
        saved = self.client.put("/v1/missions/shield_bearer", json={"mission": mission})
        self.assertEqual(saved.status_code, 200, saved.text)
        stored = self._yaml("missions.yaml")["world_definition"]["missions"]["shield_bearer"]  # as written
        self.assertEqual((stored["title"], stored["enabled_if"], stored["rewards"]),
                         ("Find a shield, again", {"reputation": {"gte": 1}}, [{"change_coins": 7}]))
        # The two maps: the player's is the discovered map of a game.
        text = app.map_markdown({"current": "Harbor", "places": [{"name": "Harbor", "visited": True, "locked": False}],
                                 "paths": [], "one_way": []})
        self.assertIn("Discovered map (this game)", text)
        self.assertIn("authoring map (🌍 World → 🗺 Map)", text)


class JobPanelTests(_Sandbox):
    """⏱ The active-job panel (🧠 Train, ⚖ Evaluate), from GET /v1/jobs: each job's state, its progress
    counted as the job counts it (generations, stories judged), its scores, how long it queued, runs or
    took, how many jobs run before a queued one (one at a time, in order), a failure with what to do, and a
    finished job's result opened where it belongs. An identical submission while one is queued or running
    is not queued again. And ▶ Play's discovered map, drawn."""

    def _blocking_benchmark(self, release):
        def benchmark(stories, seed, engine_id, variants, pairwise, workers, progress=None, **kwargs):
            progress(1, stories, "stories judged")
            release.wait(30)
            return {"target": api.evaluation.describe_target(None), "seeds": [seed, seed + stories - 1],
                    "variants": {}, "stories": stories, "judged": stories, "engagement": 6.5,
                    "criteria": {"surprise": 6.0}, "rows": [{"seed": seed, "engagement": 6.5, "scores": {},
                                                              "fitness": 1.0, "turns": 3, "won": False,
                                                              "verdict": "ok"}]}
        return benchmark

    def test_duplicates_are_not_queued_and_the_queue_is_told(self):
        release = threading.Event()
        self.addCleanup(release.set)
        with patch.object(api.evaluation, "benchmark", side_effect=self._blocking_benchmark(release)):
            body = {"stories": 3, "seed": 4}
            first = self.client.post("/v1/jobs/judge-benchmark", json=body).json()
            self.assertFalse(first["reused"])
            for _ in range(100):  # until it runs
                if self.client.get(f"/v1/jobs/{first['id']}").json()["status"] == "running":
                    break
                time.sleep(0.05)
            again = self.client.post("/v1/jobs/judge-benchmark", json=body).json()
            self.assertEqual((again["id"], again["reused"]), (first["id"], True))     # the same job, not a second
            other = self.client.post("/v1/jobs/judge-benchmark", json={**body, "seed": 5}).json()
            self.assertNotEqual(other["id"], first["id"])
            self.assertEqual(self.client.get(f"/v1/jobs/{other['id']}").json()["queue_ahead"], 1)  # one runs first
            running = self.client.get(f"/v1/jobs/{first['id']}").json()
            self.assertEqual(running["progress"], {"generation": 1, "total": 3, "label": "stories judged"})
            self.assertIsNone(running["queue_ahead"])
            listed = {job["id"]: job for job in self.client.get("/v1/jobs").json()["jobs"]}
            self.assertEqual(listed[other["id"]]["queue_ahead"], 1)
            release.set()
            self.assertEqual(_finished(self.client, other["id"])["status"], "finished")
            self.assertTrue({job["id"]: job for job in self.client.get("/v1/jobs").json()["jobs"]}[first["id"]]["has_result"])
            # Finished: the same request is a new job again.
            third = self.client.post("/v1/jobs/judge-benchmark", json=body).json()
            self.assertFalse(third["reused"])
            _finished(self.client, third["id"])

    def test_the_panel_shows_states_progress_scores_times_and_what_to_do(self):
        import datetime

        import app

        now = datetime.datetime(2026, 11, 20, 12, 0, 0, tzinfo=datetime.timezone.utc)
        at = lambda minutes: (now - datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds")  # noqa: E731
        jobs = [
            {"id": "a" * 32, "kind": "simulation", "status": "running", "description": "25 generation(s) of 40",
             "created_at": at(5), "started_at": at(4), "progress": {"generation": 3, "total": 25,
             "label": "generation", "best": 812.4, "mean": 640.0, "validation": 790.2, "improved": True}},
            {"id": "b" * 32, "kind": "simulation", "status": "queued", "description": "a second run",
             "created_at": at(2), "queue_ahead": 2},
            {"id": "c" * 32, "kind": "judge-benchmark", "status": "running", "description": "judge benchmark",
             "created_at": at(1), "started_at": at(1), "progress": {"generation": 4, "total": 10,
                                                                    "label": "stories judged"}},
            {"id": "d" * 32, "kind": "simulation", "status": "failed", "description": "too big",
             "created_at": at(30), "started_at": at(30), "finished_at": at(29),
             "error": "The run is too much work for this server (max_simulation_work 4000)."},
            {"id": "e" * 32, "kind": "simulation", "status": "finished", "description": "a done run",
             "created_at": at(60), "started_at": at(60), "finished_at": at(52), "has_result": True},
        ]
        text, active, results, cancellable = app.jobs_panel(jobs, app.TRAIN_JOBS, now)
        self.assertTrue(active)
        self.assertIn("▶ running", text)
        self.assertIn("Generation 3/25 · best 812.4 · mean 640.0 · benchmark 790.2 · improved", text)
        self.assertIn("running for 4m 00s", text)
        self.assertIn("2 job(s) run before it (one at a time, in order) · waiting 2m 00s", text)
        self.assertIn("1 other job(s) of the studio queued or running", text)       # the evaluation
        self.assertIn("**What to do:** Lower the population", text)
        self.assertIn("took 8m 00s", text)
        self.assertEqual([value for _, value in results], ["e" * 32])                # a finished result to open
        self.assertEqual([value for _, value in cancellable], ["a" * 32, "b" * 32])  # queued and running
        evaluate_text, _, _, _ = app.jobs_panel(jobs, app.EVALUATE_JOBS, now)
        self.assertIn("Stories judged 4/10", evaluate_text)                          # not "Generation"
        self.assertNotIn("Generation 4/10", evaluate_text)
        self.assertIn("first in line", app.job_line({**jobs[1], "queue_ahead": 1}, now))
        self.assertIn("check it with *Check the judge again*", app.job_failure_hint("The judge (vllm) is unavailable"))
        # The progress bar counts as the job counts.
        said = []
        app._job_progress(lambda value, desc=None: said.append(desc), "Judged")(
            {"generation": 3, "total": 10, "label": "stories judged"})
        app._job_progress(lambda value, desc=None: said.append(desc))({"generation": 2, "total": 5, "label": "generation",
                                                                      "validation": 700.0})
        self.assertEqual(said, ["Stories judged 3/10", "Generation 2/5: benchmark 700"])

    def test_finished_results_open_where_they_belong_and_the_panel_is_wired(self):
        import app

        trained = self.client.post("/v1/jobs/simulations", json={"candidate_count": 1, "generations": 1,
                                                                 "simulation_steps": 2, "deterministic": False,
                                                                 "start_game": False}).json()
        done = _finished(self.client, trained["id"])
        self.assertEqual(done["status"], "finished", done.get("error"))
        self.assertEqual(done["progress"]["label"], "generation")
        text, timer, finished, cancellable = app.jobs_panel_ui("train")
        self.assertEqual(_prop(cancellable, "choices"), [])                          # nothing left to cancel
        self.assertIn(trained["id"][:8], text)
        self.assertEqual((_prop(timer, "value"), _prop(timer, "active")), (app.JOBS_POLL_IDLE, True))  # slowly
        self.assertEqual(_prop(app.jobs_poll(True), "value"), app.JOBS_POLL_ACTIVE)
        self.assertEqual(_prop(finished, "value"), trained["id"])
        engine, card, *buttons = app.open_train_job_ui(trained["id"])
        self.assertEqual(engine["engine_id"], done["result"]["engine_id"])
        self.assertIn("Completed engine", card)
        self.assertTrue(all(_prop(b, "interactive") for b in buttons))
        release = threading.Event()
        release.set()
        with patch.object(api.evaluation, "benchmark", side_effect=self._blocking_benchmark(release)):
            judged = self.client.post("/v1/jobs/judge-benchmark", json={"stories": 2, "seed": 1}).json()
            _finished(self.client, judged["id"])
        summary, table, calib, weights = app.open_evaluate_job_ui(judged["id"])
        self.assertIn("Engagement 6.50/10", summary)
        self.assertIn("**Baseline:**", summary)
        self.assertEqual((calib, weights), (app.gr.skip(), app.gr.skip()))
        failed = api.sessions.submit_job("judge-benchmark", lambda progress: (_ for _ in ()).throw(ValueError("nope")))
        _finished(self.client, failed["id"])
        with self.assertRaises(app.gr.Error):
            app.open_evaluate_job_ui(failed["id"])
        # Wired: a timer per panel (never off: a refresh computed while the page loads cannot stop it), the
        # submissions ignore repeated clicks; a job chosen before it ended is not refused by the dropdowns.
        demo = app.build_app()
        by = {b.elem_id: b for b in demo.blocks.values() if getattr(b, "elem_id", None)}
        timers = [b for b in demo.blocks.values() if isinstance(b, app.gr.Timer)]
        self.assertGreaterEqual(len(timers), 2)
        self.assertTrue(all(timer.active for timer in timers))
        for key in ("train", "evaluate"):
            for part in ("finished", "cancellable"):
                self.assertEqual(by[f"{key}-jobs-{part}"].preprocess("0123456789abcdef"), "0123456789abcdef")
        for elem, name in (("train-run-button", "submit_run_ui"), ("evaluate-run", "submit_benchmark_ui")):
            runs = [fn for fn in demo.fns.values() if (by[elem]._id, "click") in fn.targets
                    and getattr(fn.fn, "__name__", "") == name]
            self.assertEqual([fn.trigger_mode for fn in runs], ["once"])
        for key in ("train", "evaluate"):
            (opened,) = [fn for fn in demo.fns.values() if (by[f"{key}-jobs-open"]._id, "click") in fn.targets]
            self.assertEqual(opened.fn.__name__, f"open_{key}_job_ui")

    def test_the_discovered_map_is_drawn(self):
        import app

        known = {"current": "Market", "previous": "Harbor",
                 "places": [{"name": "Harbor", "visited": True, "locked": False},
                            {"name": "Market", "visited": True, "locked": False},
                            {"name": "Vault", "visited": False, "locked": True}],
                 "paths": [["Harbor", "Market"]], "one_way": [["Market", "Vault"]]}
        svg = app.discovered_map_svg(known)
        self.assertIn("<svg", svg)
        self.assertIn("Discovered map (this game)", svg)
        self.assertEqual(svg.count("<circle"), 3)                                    # only the places known
        self.assertIn("Market 📍", svg)
        self.assertIn("Harbor ↩", svg)
        self.assertIn("Vault 🔒", svg)
        self.assertIn('marker-end="url(#dm-arrow)"', svg)                            # the one-way path
        self.assertIn("starts with your first step", app.discovered_map_svg(None))
        screen = app._play_screen({"actions": [], "map": known})
        self.assertEqual(app.play_part(screen, "map"), svg)


class JobCancellationTests(_Sandbox):
    """Cooperative cancellation (POST /v1/jobs/{id}/cancel): a queued job is cancelled at once and never
    starts; a running one is cancel_requested, stops at its next safe point and is cancelled, with what it
    had done (never finished); work that completes before a safe point is finished, saying so; the worker
    is free for the next job. And the studio: the job this browser submitted is kept (BrowserState) and
    followed again after a reload, never a new job; its result, or why there is none, is shown."""

    def setUp(self):
        super().setUp()
        patcher = patch.dict(os.environ, {"ANE_RECORDS_DIR": str(self.dir / "records")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gates = []

    def _gate(self):
        gate = threading.Event()
        self.gates.append(gate)
        self.addCleanup(gate.set)
        return gate

    def _until(self, job_id, *states, seconds=30):
        deadline = time.time() + seconds
        while time.time() < deadline:
            job = self.client.get(f"/v1/jobs/{job_id}").json()
            if job["status"] in states:
                return job
            time.sleep(0.02)
        self.fail(f"job {job_id} never reached {states}: {job['status']}")

    def _blocker(self):
        """A job holding the one worker until its gate opens."""
        gate = self._gate()
        job = sessions.submit_job("judge-benchmark", lambda progress: (gate.wait(30), {"done": True})[1])
        self._until(job["id"], "running")
        return job, gate

    def test_a_queued_job_is_cancelled_and_never_starts(self):
        blocker, gate = self._blocker()
        ran = []
        queued = sessions.submit_job("simulation", lambda progress: ran.append("ran") or {"x": 1})
        self.assertEqual(self.client.get(f"/v1/jobs/{queued['id']}").json()["queue_ahead"], 1)
        cancelled = self.client.post(f"/v1/jobs/{queued['id']}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertIn("before it started", cancelled.json()["message"])
        after = sessions.submit_job("calibrate-fitness", lambda progress: {"after": True})
        gate.set()
        self.assertEqual(_finished(self.client, blocker["id"])["status"], "finished")
        self.assertEqual(_finished(self.client, after["id"])["result"], {"after": True})   # the worker moved on
        self.assertEqual(ran, [])                                                          # it never started
        job = self.client.get(f"/v1/jobs/{queued['id']}").json()
        self.assertEqual((job["status"], job["started_at"], job["result"]), ("cancelled", None, None))
        # Ended jobs stay as they are; other kinds cannot be cancelled; an unknown one is 404.
        self.assertIn("already been cancelled", self.client.post(f"/v1/jobs/{queued['id']}/cancel").json()["message"])
        self.assertIn("already finished", self.client.post(f"/v1/jobs/{after['id']}/cancel").json()["message"])
        other = sessions.submit_job("world-proposal", lambda progress: {})
        self.assertEqual(self.client.post(f"/v1/jobs/{other['id']}/cancel").status_code, 409)
        self.assertEqual(self.client.post("/v1/jobs/nothing/cancel").status_code, 404)

    def test_a_running_job_stops_at_a_safe_point_and_frees_the_worker(self):
        steps = []

        def work(progress):
            for step in range(1, 200):
                time.sleep(0.02)
                progress(step, 200, "stories judged")                # the safe point
                steps.append(step)
            return {"all": True}

        job = sessions.submit_job("judge-benchmark", work)
        self._until(job["id"], "running")
        while not steps:
            time.sleep(0.01)
        asked = self.client.post(f"/v1/jobs/{job['id']}/cancel").json()
        self.assertIn(asked["status"], ("cancel_requested", "cancelled"))
        done = self._until(job["id"], "cancelled")
        self.assertIsNone(done["result"])
        self.assertLess(len(steps), 199)                                    # it stopped, not ran to the end
        self.assertEqual(done["partial"]["progress"]["label"], "stories judged")
        self.assertIn("not kept as a result", done["partial"]["note"])
        nxt = sessions.submit_job("judge-benchmark", lambda progress: {"next": True})
        self.assertEqual(_finished(self.client, nxt["id"])["result"], {"next": True})  # the worker is free

    def test_work_completed_before_a_safe_point_is_finished_not_cancelled(self):
        gate = self._gate()

        def work(progress):
            progress(1, 1, "stories judged")                         # the last safe point
            gate.wait(30)                                             # then the rest of the work
            return {"complete": True}

        job = sessions.submit_job("judge-benchmark", work)
        self._until(job["id"], "running")
        while not (self.client.get(f"/v1/jobs/{job['id']}").json().get("progress")):
            time.sleep(0.01)
        self.assertEqual(self.client.post(f"/v1/jobs/{job['id']}/cancel").json()["status"], "cancel_requested")
        gate.set()
        done = _finished(self.client, job["id"])
        self.assertEqual((done["status"], done["result"]), ("finished", {"complete": True}))
        self.assertIn("came after its work had completed", done["cancel_note"])

    def test_a_training_cancelled_mid_run_makes_no_engine_and_is_recorded_as_cancelled(self):
        world = store.config.as_dict()
        world["evolution"]["full_length_stories"] = False
        store.config.use_override(world)
        self.addCleanup(lambda: (setattr(store.config, "_override", None), type(store.config).reload(store.config)))
        job = self.client.post("/v1/jobs/simulations", json={"candidate_count": 2, "generations": 30,
                                                             "simulation_steps": 2, "episodes": 1,
                                                             "deterministic": False, "start_game": False}).json()
        while not self.client.get(f"/v1/jobs/{job['id']}").json().get("progress"):
            time.sleep(0.05)                                          # a generation is done
        self.client.post(f"/v1/jobs/{job['id']}/cancel")
        done = self._until(job["id"], "cancelled", seconds=120)
        self.assertIsNone(done["result"])
        self.assertIn("No engine was made", done["partial"]["note"])
        self.assertLess(done["partial"]["progress"]["generation"], 30)
        self.assertEqual(records.list_runs()[0]["status"], "cancelled")      # not finished, not failed
        nxt = sessions.submit_job("simulation", lambda progress: {"next": True})
        self.assertEqual(_finished(self.client, nxt["id"])["status"], "finished")

    def test_the_studio_follows_its_own_job_through_reloads_and_ends(self):
        import app

        # Finished: the result shown as a run; what this browser keeps is cleared.
        result = {"engine_id": "e" * 32, "run_id": "r" * 32, "learning": {"history": [], "best_score": 1.0,
                  "generations_run": 1}, "report": "Done.", "training": None,
                  "config_download_url": None, "engine_download_url": None}
        done = sessions.submit_job("simulation", lambda progress: result)
        _finished(self.client, done["id"])
        with patch.object(app, "_training_outputs", return_value=("Done.", None, None, None, None, None, None)), \
                patch.object(app, "engine_card", return_value=None):
            out = app.follow_training_ui({"id": done["id"], "kind": "run"}, progress=lambda *a, **k: None)
        self.assertEqual(len(out), app.TRAINING_FOLLOW_OUTPUTS + 1)
        self.assertEqual((out[0], out[-1]), ("Done.", None))
        self.assertEqual(out[8]["engine_id"], "e" * 32)                       # the completed engine
        # Cancelled, failed, gone: said, and the job forgotten by this browser (never another one started).
        cancelled = sessions.submit_job("simulation", lambda progress: {})
        jobs_before = len(self.client.get("/v1/jobs").json()["jobs"])
        failed = sessions.submit_job("simulation", lambda progress: (_ for _ in ()).throw(ValueError("max_simulation_work")))
        _finished(self.client, failed["id"])
        _finished(self.client, cancelled["id"])
        stopped = sessions.submit_job("judge-benchmark", lambda progress: progress(1, 2, "stories judged") or {})
        sessions.cancel_job(stopped["id"])
        for ref, expected in (({"id": failed["id"], "kind": "run"}, "❌ Failed"),
                              ({"id": "0" * 32, "kind": "run"}, "⚠ Job lost")):
            out = app.follow_training_ui(ref, progress=lambda *a, **k: None)
            self.assertIn(expected, out[0])
            self.assertIsNone(out[-1])
        self.assertIn("restart loses them", app.follow_training_ui({"id": "0" * 32, "kind": "run"},
                                                                   progress=lambda *a, **k: None)[0])
        self.assertIn("Lower the population", app.follow_training_ui({"id": failed["id"], "kind": "run"},
                                                                     progress=lambda *a, **k: None)[0])
        self.assertEqual(len(self.client.get("/v1/jobs").json()["jobs"]), jobs_before + 2)  # nothing new submitted
        # Nothing kept: nothing followed, even with jobs of others running.
        self.assertTrue(all(o == app.gr.skip() for o in app.follow_training_ui(None, progress=lambda *a, **k: None)))
        # An evaluation: a cancelled one says so where its result would be.
        gate = self._gate()
        judging = sessions.submit_job("judge-benchmark", lambda progress: (progress(1, 3, "stories judged"),
                                                                          gate.wait(30), progress(2, 3, "x"))[2] or {})
        self._until(judging["id"], "running")
        sessions.cancel_job(judging["id"])
        gate.set()
        out = app.follow_evaluation_ui({"id": judging["id"], "kind": "judge-benchmark"}, progress=lambda *a, **k: None)
        self.assertIn("🚫 Cancelled", out[0])
        self.assertIn("1 of 3 stories judged", out[0])
        self.assertIsNone(out[-1])
        # StudioAPI.wait: cancelled ends the polling; a lost connection is tried again.
        with self.assertRaises(app.JobCancelledError):
            app.API.wait({"id": judging["id"]})
        calls = []
        real_get = app.API.get

        def flaky(path, **params):
            calls.append(path)
            if len(calls) < 3:
                raise app.APIError("could not be reached", None)
            return real_get(path, **params)

        with patch.object(app.API, "get", side_effect=flaky):
            self.assertEqual(app.API.wait({"id": done["id"]}, interval=0.01)["engine_id"], "e" * 32)
        self.assertEqual(len(calls), 3)
        # Wired: kept in this browser, followed again on load; Cancel in both panels.
        demo = app.build_app()
        keys = {b.storage_key for b in demo.blocks.values() if isinstance(b, app.gr.BrowserState)}
        self.assertLessEqual({"ane-train-job", "ane-evaluate-job"}, keys)
        loads = [getattr(fn.fn, "__name__", "") for fn in demo.fns.values() if any(t[1] == "load" for t in fn.targets)]
        self.assertIn("follow_training_ui", loads)
        self.assertIn("follow_evaluation_ui", loads)
        by = {b.elem_id: b for b in demo.blocks.values() if getattr(b, "elem_id", None)}
        for key in ("train", "evaluate"):
            (fn,) = [fn for fn in demo.fns.values() if (by[f"{key}-jobs-cancel"]._id, "click") in fn.targets]
            self.assertEqual(fn.fn.__name__, "cancel_job_ui")


class WeightsFollowUpTests(_Sandbox):
    """Weights: saved values changed from the shipped ones are listed (and the shipped values can be put
    in the draft); leaving ⚙ Advanced with an unsaved weights draft asks Save, Discard or Stay; the Basic
    controls say how far each storyteller's traits move them; the turns between events and the most
    events per story are Basic controls; the studio's start waits for a slow first page."""

    _weights, _file, _value = TuningParameterTests._weights, TuningParameterTests._file, TuningParameterTests._value

    def _meta(self):
        import app

        weights = self._weights()
        return app, weights, app.weights_meta(weights["parameters"], weights["basic"], weights["shipped"])

    def test_changes_from_the_shipped_values_are_listed_and_can_be_drafted(self):
        app, weights, meta = self._meta()
        self.assertEqual(weights["shipped"]["director.retry_bonus"], weights["values"]["director.retry_bonus"])
        info = {p["path"]: p for p in weights["parameters"]}
        self.assertEqual(info["director.retry_bonus"]["shipped"], weights["values"]["director.retry_bonus"])
        self.assertIn("shipped 15", app.weight_help(info["director.retry_bonus"]))
        self.assertIn('data-changed="0"', app.weights_shipped_markup(meta, weights["values"]))
        self.client.put("/v1/weights", json={"values": {"director.retry_bonus": 9, "fitness.base.mission_win_bonus": 99}})
        saved = self._weights()["values"]
        panel = app.weights_shipped_markup(meta, saved)
        self.assertIn('data-changed="2"', panel)
        basic_part, advanced_part = panel.split("Only in Advanced")
        self.assertIn("director.retry_bonus</code>: shipped 15, saved 9", basic_part)
        self.assertIn("fitness.base.mission_win_bonus", advanced_part)
        values = [saved[p] for p in meta["paths"]]
        out = app.shipped_into_draft_ui(meta, saved, *values)
        advanced, basic = out[:len(meta["paths"])], out[len(meta["paths"]):len(meta["paths"]) + len(meta["basic_paths"])]
        self.assertEqual(advanced[meta["paths"].index("director.retry_bonus")], 15)
        self.assertEqual(basic[meta["basic_paths"].index("director.retry_bonus")], 15)
        self.assertEqual(advanced[meta["paths"].index("fitness.base.mission_win_bonus")],
                         weights["shipped"]["fitness.base.mission_win_bonus"])
        self.assertEqual(sum(1 for value in advanced if value != app.gr.skip()), 2)  # nothing else changes
        self.assertIn("Save changes", out[-1])
        self.assertEqual(self._value("director.retry_bonus"), 9)                 # a draft, not saved

    def test_leaving_advanced_with_unsaved_weights_asks(self):
        app, weights, meta = self._meta()
        saved = weights["values"]
        draft = dict(zip(meta["paths"], [saved[p] for p in meta["paths"]]))
        draft["fitness.base.mission_win_bonus"] = 91
        dirty = app.weights_dirty(meta, saved, {"entries": []}, list(draft.values()))
        self.assertEqual(dirty, {"entries": [], "weights": ["Advanced only"]})
        asked = app.nav_choice_ui("train", "advanced", "entries", {}, dirty)
        self.assertEqual(asked["advanced_section"], "weights")
        self.assertEqual(_prop(asked["nav"][0], "selected"), "advanced")          # stays in Advanced
        shown, text, target = asked["prompts"]["weights"]
        self.assertIn("Save them before", text)
        self.assertEqual(target, {"type": "destination", "id": "train"})
        self.assertEqual(app.nav_choice_ui("train", "advanced", "entries", {}, {"entries": []})["nav"][-1], "train")
        nav = len(app.navigate("world"))
        fields = len(meta["paths"]) + len(meta["basic_paths"])
        stay = app.weights_resolve_ui("stay", target, meta, saved, dirty, *draft.values())
        self.assertFalse(stay[0]["visible"])
        self.assertEqual(self._value("fitness.base.mission_win_bonus"), saved["fitness.base.mission_win_bonus"])
        out = app.weights_resolve_ui("save", target, meta, saved, dirty, *draft.values())
        self.assertEqual(out[3 + nav - 1], "train")                                 # then where it was going
        self.assertEqual(out[3 + nav]["fitness.base.mission_win_bonus"], 91)        # saved
        self.assertEqual(self._value("fitness.base.mission_win_bonus"), 91)
        self.assertEqual(out[-1], {"entries": []})
        draft["fitness.base.mission_win_bonus"] = 92
        now = self._weights()["values"]
        out = app.weights_resolve_ui("discard", target, meta, now, dirty, *draft.values())
        self.assertEqual(out[3 + nav + 1 + meta["paths"].index("fitness.base.mission_win_bonus")], 91)  # saved back
        self.assertEqual(self._value("fitness.base.mission_win_bonus"), 91)
        self.assertEqual(len(out), 3 + nav + 1 + fields + 4)
        # A refused save keeps the question, the draft and where it was going.
        draft["fitness.base.mission_win_bonus"] = None
        kept = app.weights_resolve_ui("save", target, meta, now, dirty, *draft.values())
        self.assertTrue(kept[0]["visible"])
        self.assertIn("could not be saved", kept[1])
        self.assertEqual(kept[2], target)

    def test_basic_says_how_traits_scale_and_has_the_event_limits(self):
        weights = self._weights()
        controls = {c["path"]: c for area in weights["basic"] for c in area["controls"]}
        self.assertIn("unexpected_events.cooldown_steps", controls)
        self.assertIn("unexpected_events.max_per_story", controls)
        info = {p["path"]: p for p in weights["parameters"]}
        self.assertEqual((info["unexpected_events.cooldown_steps"]["type"], info["unexpected_events.cooldown_steps"]
                          ["requires"]), ("integer", "unexpected_events.enabled"))
        bounds = store.config.get("genome_bounds")
        low, high = bounds["novelty_bias"]
        self.assertIn(f"×{low:g} to ×{high:g}", controls["action_selection.repetition_penalty"]["scaling"])
        self.assertIn("0.5 +", controls["director.explore_weight"]["scaling"])
        self.assertIn("unexpected_events.genome_weight", controls["unexpected_events.base_chance"]["scaling"])
        self.assertIn("goal_bias", controls["mission_settings.route_weight"]["scaling"])
        self.assertNotIn("scaling", controls["director.ping_pong_penalty"])        # no trait: nothing said
        import app

        field = app.basic_weight_field(info["action_selection.repetition_penalty"],
                                       controls["action_selection.repetition_penalty"])
        self.assertIn("Each storyteller multiplies it", field.info)
        saved = self.client.put("/v1/weights", json={"values": {"unexpected_events.max_per_story": 5}})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(self._value("unexpected_events.max_per_story"), 5)
        self.assertEqual(self.client.put("/v1/weights", json={"values": {"unexpected_events.max_per_story": 2.5}})
                         .status_code, 400)

    def test_the_studio_waits_for_a_slow_first_page(self):
        import httpx

        import app

        class Answer:
            status_code = 200

        calls = []

        def slow_then_ready(url, **kwargs):
            calls.append(url)
            if len(calls) < 3:
                raise httpx.ReadTimeout("busy")
            return Answer()

        from gradio import networking

        original = networking.url_ok
        self.addCleanup(setattr, networking, "url_ok", original)
        with patch("httpx.head", side_effect=slow_then_ready), patch.object(app.time, "sleep"):
            check = app.patient_startup_check(wait=60)
            self.assertIs(networking.url_ok, check)
            self.assertTrue(check("http://127.0.0.1:1/"))
        self.assertEqual(len(calls), 3)
        with patch("httpx.head", side_effect=httpx.ConnectError("down")), patch.object(app.time, "sleep"):
            self.assertFalse(app.patient_startup_check(wait=0)("http://127.0.0.1:1/"))


class DraftEverywhereTests(_Sandbox):
    """Every World editor tracks its draft: the entry editor with baselines set from the values it loads
    (no second round trip), each edit sending only its part, its unsaved parts kept in the browser and
    offered back; 🎯 Missions, 📜 Story rules and 🗺 Map with their own indicators and questions; leaving
    any of them with unsaved changes asks first, from which parts are unsaved (not the drafts)."""

    def test_the_baseline_set_from_a_load_is_what_the_loaded_values_compare_to(self):
        import app

        for kind, name in (("Locations", "Harbor"), ("NPCs", "Smuggler"), ("Events", "tavern_brawl"),
                           ("Items", "Ancient Map")):
            with self.subTest(kind=kind):
                editor = _Editor(app, kind, name)  # the values as the browser holds them after the load
                entry = app._entry_load(kind, name)
                base, dirty, indicator, kept = app.entry_baselined(entry, list(app.DRAFT_PARTS), {}, {}, {},
                                                                   {"kind": kind, "entity": name})
                self.assertEqual(base, app.draft_parts(*editor.values))  # the same baseline, in one step
                self.assertEqual((dirty, kept), ({"entries": []}, {}))
                self.assertIn('data-dirty="0"', indicator)

    def test_an_edit_sends_its_part_and_the_draft_is_kept_and_offered_back(self):
        import app

        editor = _Editor(app, "Locations", "Harbor")
        fields = editor.values[editor.at("fields"):editor.at("fields") + 14]
        fields[2] = "Masts and gulls."
        dirty, indicator, kept = app.entry_part_status_ui("fields", editor.base, {}, editor.loaded, {}, *fields)
        self.assertEqual(dirty, {"entries": ["fields"]})
        self.assertIn("Unsaved changes in Harbor: Fields", indicator)
        self.assertEqual((kept["kind"], kept["entity"], list(kept["parts"])), ("Locations", "Harbor", ["fields"]))
        json.dumps(kept)  # it can be kept in the browser
        # Typed back: clean, and nothing kept.
        clean = app.entry_part_status_ui("fields", editor.base, dirty, editor.loaded, kept,
                                         *editor.values[editor.at("fields"):editor.at("fields") + 14])
        self.assertEqual((clean[0], clean[2]), ({"entries": []}, {}))
        # The page reopened: offered, then restored over the stored entry (still unsaved).
        offered, panel, text = app.offer_kept_draft_ui(kept)
        self.assertTrue(panel["visible"])
        self.assertIn("**Harbor** (Locations) had unsaved changes in **Fields**", text)
        fresh = _Editor(app, "Items", "Ancient Map")
        out = app.restore_kept_draft_ui(offered, fresh.base, {}, fresh.loaded, *fresh.values)
        n = app.ENTRY_OUTPUTS
        fresh._entry(out[:n])
        fresh.base, dirty = out[n], out[n + 1]
        self.assertEqual((fresh.entity, fresh.value("fields", 2), dirty), ("Harbor", "Masts and gulls.",
                                                                          {"entries": ["fields"]}))
        self.assertEqual(fresh.unsaved, ["fields"])
        self.assertEqual(app.discard_kept_draft_ui()[0], {})

    def test_missions_story_rules_and_the_map_track_their_drafts(self):
        import app

        missions = app.form_load_all_ui("missions", {})
        self.assertEqual(set(missions["base"]["missions"]), {"mission", "missions_yaml", "side_quests_yaml"})
        base, dirty = missions["base"], missions["dirty"]
        side, said = app.form_part_status_ui("missions", "side_quests_yaml", base, dirty, "world_definition: {}\n")
        self.assertEqual(side["missions"], ["side_quests_yaml"])                 # the side quests: a part of their own
        self.assertIn("Side quests as YAML", said)
        # Pick a mission, edit its title: unsaved; another mission is asked about, the picker goes back.
        opened = app.form_pick_ui("missions", "shield_bearer", {}, base, dirty)
        base, context = opened["base"], opened["context"]
        form = list(opened["values"]["mission"])
        form[1] = "Find a heavier shield"
        dirty, indicator = app.form_part_status_ui("missions", "mission", base, dirty, *form)
        self.assertEqual(dirty["missions"], ["mission"])
        self.assertIn("Mission form", indicator)
        asked = app.form_pick_ui("missions", "royal_audience", context, base, dirty)
        self.assertTrue(asked["prompt"])
        self.assertEqual(_prop(asked["picker"], "value"), "shield_bearer")
        self.assertNotIn("values", asked)                                   # nothing replaced
        # Leaving the World asks in 🎯 Missions.
        leave = app.nav_choice_ui("train", "world", "missions", None, dirty)
        self.assertEqual((leave["section"], list(leave["prompts"])), ("missions", ["missions"]))
        self.assertEqual(app.section_choice_ui("map", "missions", None, dirty)["prompts"]["missions"][2]["id"], "map")
        self.assertEqual(app.section_choice_ui("map", "entries", None, dirty), {"section_now": "map"})
        # Save from the question: saved, then the other mission opens, clean.
        values = [*form, opened.get("missions_yaml_value", app.load_missions_yaml()), app.load_side_quests_yaml()]
        done = app.form_resolve_ui("missions", "save", asked["pending"], context, base, dirty, *values)
        self.assertEqual(done["dirty"]["missions"], [])
        self.assertEqual(done["values"]["mission"][0], "royal_audience")
        stored = yaml.safe_load((self.dir / "missions.yaml").read_text())["world_definition"]["missions"]
        self.assertEqual(stored["shield_bearer"]["title"], "Find a heavier shield")
        # A mission undo keeps unsaved mission edits.
        edited = list(done["values"]["mission"])
        edited[1] = "An audience, edited"
        dirty, _ = app.form_part_status_ui("missions", "mission", done["base"], done["dirty"], *edited)
        history = app.confirm_delete_mission_ui({"type": "mission", "key": "shield_bearer", "title": "x"}, [])[7]
        undone = app.undo_delete_mission_ui(history, done["base"], dirty)
        self.assertEqual(undone[1:8], (gr_skip(),) * 7)                    # the form is not replaced
        self.assertIn("unsaved mission changes were kept", undone[9])
        self.assertIn("shield_bearer", yaml.safe_load((self.dir / "missions.yaml").read_text())["world_definition"]
                      ["missions"])
        # Story rules: a profile's stored values guarded; the map's borders guarded.
        rules = app.form_load_all_ui("rules", "default")
        dirty, _ = app.form_part_status_ui("rules", "values", rules["base"], {}, "playthroughs: 7\n")
        self.assertTrue(app.form_pick_ui("rules", "someone", "default", rules["base"], dirty)["prompt"])
        dirty, _ = app.form_part_status_ui("rules", "messages", rules["base"], {}, "messages: {}\n")
        self.assertEqual(dirty["rules"], ["messages"])
        self.assertIn("rules", app.nav_choice_ui("play", "world", "story-rules", None, dirty)["prompts"])
        maps = app.form_load_all_ui("map", "Harbor")
        borders = list(app._output_value(maps["values"]["borders"][0])[1])
        dirty, _ = app.form_part_status_ui("map", "borders", maps["base"], {}, borders[:-1])
        self.assertEqual(dirty["map"], ["borders"])
        self.assertTrue(app.form_pick_ui("map", "Market", "Harbor", maps["base"], dirty)["prompt"])

    def test_the_studio_wires_every_editor_to_its_draft(self):
        import gradio as gr

        import app

        demo = app.build_app()
        by = {b.elem_id: b for b in demo.blocks.values() if getattr(b, "elem_id", None)}
        for prefix in ("missions", "rules", "map"):
            self.assertIn(f"{prefix}-draft-status", by)
            self.assertFalse(by[f"{prefix}-draft-prompt"].visible)
        self.assertFalse(by["draft-offer"].visible)
        self.assertIn('.draft-status[data-dirty="1"]', app.DRAFT_HEAD)  # any editor's draft holds the page
        stored = [b for b in demo.blocks.values() if isinstance(b, gr.BrowserState)]
        self.assertIn("ane-entry-draft", {b.storage_key for b in stored})


def gr_skip():
    import gradio as gr

    return gr.skip()


class FollowUpFixTests(_Sandbox):
    """Undo histories that survive a restart and a reload; what refers to an entry, said before it goes;
    comments kept when weights are saved; the engine's parameters cleaned up."""

    def test_undo_survives_a_restart_and_has_a_history(self):
        import app
        from backend import content

        first = self.client.delete("/v1/missions/shield_bearer").json()["undo"]
        second = self.client.delete("/v1/missions/royal_audience").json()["undo"]
        self.assertTrue(content.deletions_path().exists())
        content.DELETIONS.clear()             # the API restarted: read back from deletions.json
        content._DELETIONS_LOADED.clear()
        history = [{"undo": first, "said": "first"}, {"undo": second, "said": "second"}]
        restored = app.undo_delete_mission_ui(history)
        self.assertEqual(_prop(restored[0], "value"), "royal_audience")   # the newest first
        left = restored[11]
        self.assertEqual([item["said"] for item in left], ["first"])
        self.assertIn("first", restored[13])                             # the next one offered
        again = app.undo_delete_mission_ui(left)
        self.assertEqual((_prop(again[0], "value"), again[11]), ("shield_bearer", []))
        stored = list(yaml.safe_load((self.dir / "missions.yaml").read_text())["world_definition"]["missions"])
        self.assertEqual(stored[2:4], ["shield_bearer", "royal_audience"])
        self.assertEqual(app.restore_history_ui(history)[0], history)    # kept in the browser, shown again

    def test_what_refers_to_an_entry_is_said_before_it_goes(self):
        import app

        found = self.client.get("/v1/entities/NPCs/Thief/references").json()["references"]
        self.assertTrue(any("rid_the_market_of_the_thief" in problem for problem in found), found)
        self.assertEqual(self.client.get("/v1/entities/NPCs/Nobody/references").status_code, 404)
        editor = _Editor(app, "NPCs", "Thief")
        question = app.ask_delete_entry_ui(*editor.guard)[2]
        self.assertIn("thing(s) refer to it", question)
        self.assertIn("rid_the_market_of_the_thief", question)

    def test_saving_weights_keeps_the_file_s_comments(self):
        before = (self.dir / "hyperparameters.yaml").read_text()
        self.assertEqual(self.client.put("/v1/weights", json={"values": {"stakes.alert.check_penalty": 5}}).status_code,
                         200)
        after = (self.dir / "hyperparameters.yaml").read_text()
        changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
        self.assertEqual(len(changed), 1)
        self.assertIn("check_penalty: 5", changed[0][1])
        self.assertIn("# per level", changed[0][1])                     # its comment stays
        self.assertEqual(before.count("#"), after.count("#"))

    def test_the_engine_s_parameters_are_cleaned_up(self):
        from backend import tuning
        from core_engine import builder

        values = self.client.get("/v1/weights").json()
        for gone in ("outcome_scoring.spawn_bonus", "simulation.mutation_rate", "fitness.setup_payoff.early_setup_bonus",
                     "transition_objective.character_goal_weight", "spawn_selection.character_alignment_weight"):
            self.assertNotIn(gone, values["values"])
            self.assertTrue(tuning.retired(gone))
        info = {p["path"]: p for p in values["parameters"]}
        gated = info["outcome_resolution.success_bias_weight"]
        self.assertEqual(gated["requires"], "outcome_resolution.hidden_adjustments")
        self.assertIn("inactive", gated["inactive"])                     # off in the shipped configuration
        self.assertIsNone(info["outcome_resolution.probability_floor"]["inactive"])
        for unit in (p["unit"] for p in info.values()):
            self.assertLessEqual(len(unit), 45, unit)
        # Negative exponents are refused when the world is built (they would divide by zero).
        world = store.config.as_dict()
        world["action_selection"]["stay_penalty_power"] = -1
        store.config.use_override(world)
        self.addCleanup(lambda: (setattr(store.config, "_override", None), type(store.config).reload(store.config)))
        self.assertIn("stay_penalty_power", " ".join(builder.exponent_errors()))
        # Every run uses evolution.crossover_rate; the old app parameter is ignored.
        self.assertNotIn("crossover_probability", store.APP_DEFAULTS)
        self.assertIn("crossover_probability", store.RETIRED_APP_SETTINGS)

    def test_a_discovery_does_not_lead_straight_back(self):
        from core_engine import Genome
        from core_engine.game_state import GameState
        from core_engine.world import WorldGraph  # noqa: F401  (the world the engine builds)
        import main as engine

        state = GameState(engine.build_world(Genome()), Genome(), store=None)
        world, current = state.world, state.current
        other = next(loc for name, loc in world.locations.items() if name != current.name)
        state.path = [other.name, current.name]
        with_back = world._score_spawn_candidate(current, other, state=state)
        state.path = [current.name]
        without = world._score_spawn_candidate(current, other, state=state)
        self.assertAlmostEqual(without - with_back, store.config.get("spawn_selection.immediate_repeat_penalty", 10.0))


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
            self.assertEqual(self.client.put("/v1/app-settings", json={"settings": {"candidate_limit": 0}}).status_code, 400)
            # A limit below the saved engine default is allowed: a run's population is held to it (with a note).
            self.assertEqual(self.client.put("/v1/app-settings", json={"settings": {"candidate_limit": 1}}).status_code, 200)
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
        # The largest run passes the API and reaches the backend unclamped; the total-work limit refuses it,
        # counting each story at the turns it can really take (GET /v1/simulations/work says how many).
        turns = self.client.get("/v1/simulations/work", params={"candidate_count": 400, "generations": 1000,
                                                                "simulation_steps": 400, "episodes": 1}).json()
        response = self.client.post("/v1/simulations", json=largest)
        self.assertEqual(response.status_code, 400, response.text)
        detail = response.json()["detail"]
        requested = int(re.search(r"requests (?:up to )?([\d,]+) simulated turns", detail).group(1).replace(",", ""))
        self.assertEqual(requested, 400 * 1000 * 1 * turns["effective_max_turns"])
        self.assertIn(f"400 storytellers x 1000 generations x 1 stories x {turns['effective_max_turns']} turns", detail)
        self.assertIn("400 steps asked for", detail)  # the steps reached the backend unclamped too

    def test_documents(self):
        names = [d["name"] for d in self.client.get("/v1/documents").json()["documents"]]
        self.assertEqual(names, ["missions", "side_quests", "messages", "variables", "map", "stats", "stored_values"])
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
                settings["candidate_limit"] = 40
                store.save_app_settings(settings)
                self.assertEqual(store.load_app_settings(), settings)
                for key, value in (("server_port", 70000), ("max_web_sessions", 0),
                                   ("candidate_limit", 401),
                                   ("generation_limit", 1001), ("simulation_step_limit", 401),
                                   ("queue_max_size", True)):
                    with self.subTest(key=key), self.assertRaises(ValueError):
                        store.save_app_settings({**settings, key: value})
                self.assertEqual(store.load_app_settings(), settings)

    def test_a_saved_file_with_the_retired_run_defaults_still_loads(self):
        """default_candidates, default_generations and default_simulation_steps are gone (a run's defaults
        are the saved engine defaults): a file that still has them loads, and saving drops them."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ui.yml"
            path.write_text(yaml.safe_dump({**store.APP_DEFAULTS, "default_candidates": 8, "default_generations": 5,
                                            "default_simulation_steps": 16}))
            with patch.object(store, "APP_SETTINGS_PATH", path):
                loaded = store.load_app_settings()
                self.assertEqual(loaded, store.APP_DEFAULTS)
                store.save_app_settings({**loaded, "default_candidates": 4})
                self.assertNotIn("default_candidates", yaml.safe_load(path.read_text()))
                answer = TestClient(api.api).put("/v1/app-settings", json={"settings": {"default_generations": 3}})
                self.assertEqual(answer.status_code, 200, answer.text)
                self.assertIn("Ignored default_generations", answer.json()["message"])


class StudioIsAnAPIClientTests(_Sandbox):
    def test_the_studio_uses_no_backend_code(self):
        """The studio never serves the API: app.py imports nothing of the project but its HTTP
        client (studio_client.py), which imports nothing of the project at all (not api.py)."""
        root = Path(__file__).resolve().parent.parent
        project = {p.stem for p in root.glob("*.py")} | {p.name for p in root.iterdir() if (p / "__init__.py").exists()}

        def imported(nodes):
            names = {a.name.split(".")[0] for n in nodes if isinstance(n, ast.Import) for a in n.names}
            return names | {n.module.split(".")[0] for n in nodes if isinstance(n, ast.ImportFrom) and n.module}

        tree = ast.parse((root / "app.py").read_text())
        self.assertEqual(imported(list(ast.walk(tree))) & project, {"studio_client"})
        client = ast.parse((root / "studio_client.py").read_text())
        self.assertEqual(imported(list(ast.walk(client))) & project, set())

    def test_the_studio_reaches_the_api_over_http_by_default(self):
        """Without a test client: HTTP to ANE_API_URL, or to http://127.0.0.1:8005 when it is unset
        or empty; an explicit URL wins over both and over a test client."""
        import studio_client

        self.assertEqual(studio_client.api_url({}), "http://127.0.0.1:8005")
        self.assertEqual(studio_client.api_url({"ANE_API_URL": "  "}), "http://127.0.0.1:8005")
        self.assertEqual(studio_client.api_url({"ANE_API_URL": "http://ane.hamlet.iti.gr/"}), "http://ane.hamlet.iti.gr")
        self.assertEqual(studio_client.PUBLIC_API_URL, "http://ane.hamlet.iti.gr")
        explicit = studio_client.StudioAPI("http://hamlet-ane-api:8005/")
        self.addCleanup(explicit.close)
        self.assertEqual((explicit.mode, explicit.base_url), ("http", "http://hamlet-ane-api:8005"))
        with patch.dict(os.environ, {"ANE_API_URL": "http://ane.hamlet.iti.gr"}):
            from_env = studio_client.StudioAPI()
        self.addCleanup(from_env.close)
        self.assertEqual((from_env.mode, from_env.base_url), ("http", "http://ane.hamlet.iti.gr"))
        factory = studio_client._TEST_CLIENT_FACTORY
        self.addCleanup(studio_client.use_test_client, factory)
        studio_client.use_test_client(None)
        with patch.dict(os.environ, {"ANE_API_URL": ""}):
            default = studio_client.StudioAPI()
        self.addCleanup(default.close)
        self.assertEqual((default.mode, default.base_url), ("http", "http://127.0.0.1:8005"))
        with self.assertRaises(studio_client.APIError) as caught:  # nothing listens there in the tests
            studio_client.StudioAPI("http://127.0.0.1:9").get("/healthz")
        self.assertIn("could not be reached", str(caught.exception))

    def test_the_public_deployment_is_listed_in_the_documentation(self):
        self.assertEqual(api.openapi_servers(""), [{"url": "/", "description": "This server"}])
        self.assertEqual(api.openapi_servers("http://ane.hamlet.iti.gr"),
                         [{"url": "/", "description": "This server"},
                          {"url": "http://ane.hamlet.iti.gr", "description": "The public deployment"}])
        servers = TestClient(api.api).get("/openapi.json").json()["servers"]
        self.assertEqual(servers[0]["url"], "/")

    def test_the_tests_studio_uses_the_api_in_this_process(self):
        import app
        self.assertEqual(app.API.mode, "test")
        self.assertIs(app.APIError, __import__("studio_client").APIError)

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


class ExampleWorldTests(unittest.TestCase):
    """The example worlds (examples/) are world-only files: uploaded as they are,
    completed with the built-in tuning, trained on and played."""

    def setUp(self):
        self.client = TestClient(api.api, headers={"X-ANE-User": "api-tests"})  # a backend calling the API, for its user
        self.addCleanup(lambda: self.client.post("/v1/config/reset"))

    def test_example_worlds_load_train_and_play(self):
        root = Path(__file__).resolve().parent.parent / "examples"
        for path in sorted(root.glob("*.yaml")):
            with self.subTest(world=path.name):
                response = self.client.post("/v1/config/upload", files={"file": (path.name, path.read_bytes())})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn("Taken from the built-in configuration", response.json()["message"])
                world = yaml.safe_load(path.read_text())["world_definition"]
                active = self.client.get("/v1/config").json()["config"]["world_definition"]
                self.assertEqual(set(active["locations"]), set(world["locations"]))
                game = self.client.post("/v1/simulations", json={"candidate_count": 2, "generations": 1, "simulation_steps": 4,
                                                                 "episodes": 1, "deterministic": False, "warm_start": False})
                self.assertEqual(game.status_code, 200, game.text)
                state = game.json()
                after = self.client.post(f"/v1/sessions/{state['session_id']}/actions", json={"action": state["actions"][0]})
                self.assertEqual(after.status_code, 200, after.text)
                self.assertIn(after.json()["location"], world["locations"])
                self.client.post("/v1/config/reset")

    def test_a_world_must_name_its_own_places(self):
        path = Path(__file__).resolve().parent.parent / "examples" / "orbital_station.yaml"
        world = yaml.safe_load(path.read_text())
        world.pop("game_state")                                      # the arrest location would be the city's Dungeon
        response = self.client.post("/v1/config/upload", files={"file": ("w.yaml", yaml.safe_dump(world).encode())})
        self.assertEqual(response.status_code, 400)
        self.assertIn("`game_state.arrest_location` is `Dungeon`, which is not a place of this world", response.json()["detail"])
        self.assertNotIn("more problem", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()


class LanguageModelEditorTests(_Sandbox):
    """The terminal editor's language-model features, in the API and the studio: suggestions for an
    entry's text, and new places, people and items proposed, reviewed, then added (nothing written
    before). The model is replaced by a stand-in: no language model runs here."""

    READY = {"available": True, "mode": "server", "model": "gemma", "server": "http://judge/v1", "reason": None,
             "how": None}
    NOT_READY = {"available": False, "mode": "server", "model": "gemma", "server": "http://judge/v1",
                 "reason": "No vLLM server answers at http://judge/v1.", "how": "Start the judge."}
    ADDITIONS = {
        "locations": {"Lighthouse": {"tags": ["harbor", "secluded"], "descriptions": ["A lamp turns above the rocks."],
                                     "actions": [{"name": "Climb to the lamp", "outcomes": [
                                         {"desc": "The whole bay lies below you.", "tags": ["discovery"]}]}]}},
        "characters": [{"name": "Lamplighter", "associated_tags": ["harbor"], "goals": ["Keep the light burning"],
                        "descriptions": ["An old keeper with soot on his hands."]}],
        "objects": [{"name": "Brass Lens", "associated_tags": ["harbor"], "descriptions": ["A heavy lens."]}],
    }

    def test_suggestions_need_the_language_model_and_save_nothing(self):
        import app

        before = (self.dir / "npcs.yaml").read_text()
        body = {"kind": "NPCs", "name": "Smuggler", "field": "goals"}
        with patch("utils.llm_model_loader.status", return_value=self.NOT_READY):
            refused = self.client.post("/v1/suggestions", json=body)
        self.assertEqual(refused.status_code, 503)
        self.assertIn("Start the judge", refused.json()["detail"])  # why, and how to make it available
        self.assertEqual(self.client.post("/v1/suggestions", json=dict(body, field="secret")).status_code, 400)
        self.assertEqual(self.client.post("/v1/suggestions", json=dict(body, name="Nobody")).status_code, 404)

        seen = {}

        class Suggester:
            def available(self):
                return True

            def last_error(self):
                return None

            def suggest_list(self, field_name, context):
                seen.update(field=field_name, context=context)
                return ["Pay off the harbour master", "Leave the city by night"]

        with patch("utils.llm_model_loader.status", return_value=self.READY), \
                patch("utils.llm_suggester.LLMSuggester", Suggester):
            found = self.client.post("/v1/suggestions", json=body)
            ticks, note = app.suggest_ui("NPCs", "Smuggler", "goals")
        self.assertEqual(found.status_code, 200, found.text)
        self.assertEqual(found.json()["suggestions"], ["Pay off the harbour master", "Leave the city by night"])
        self.assertEqual(seen["field"], "Goals")
        self.assertEqual(seen["context"]["current_entry"]["name"], "Smuggler")  # the entry and the world it is in
        self.assertIn("Vault", seen["context"]["whole_case"]["locations"])
        self.assertEqual(ticks["value"], found.json()["suggestions"])  # all ticked, to untick
        self.assertEqual((self.dir / "npcs.yaml").read_text(), before)  # nothing saved

        # The ticked ones go into the form (saved with the form's own Save).
        import pandas as pd

        dialogue = pd.DataFrame(columns=["Said when (condition, empty: always)", "Says"])
        shown = app.add_suggestions_ui("goals", ["Leave the city by night"], "A smuggler.", "Earn coin", ["urban"],
                                       "", "", "", dialogue)
        self.assertEqual(shown[1], "Earn coin\nLeave the city by night")
        self.assertIn("Save entry", shown[-1])
        shown = app.add_suggestions_ui("dialogue", ["Psst. Over here."], "", "", [], "", "", "", dialogue)
        self.assertEqual(list(shown[6]["Says"]), ["Psst. Over here."])
        self.assertEqual(app.add_suggestions_ui("tags", ["smuggler"], "", "", ["urban"], "", "", "", None)[2]["value"],
                         ["urban", "smuggler"])

    def test_the_world_grows_only_by_what_is_ticked_and_checked(self):
        import app
        from core_engine.augmenter import WorldAugmenter

        size = self.client.get("/v1/world/size").json()
        self.assertEqual(set(size["have"]), {"locations", "items", "npcs", "outcomes"})
        self.assertEqual(set(size["want"]), set(size["have"]))
        files = {name: (self.dir / name).read_text() for name in ("locations.yaml", "npcs.yaml", "items.yaml")}

        # Without the language model, the proposal job fails and says why.
        with patch("utils.llm_model_loader.status", return_value=self.NOT_READY):
            job = _finished(self.client, self.client.post("/v1/jobs/world-proposal").json()["id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("not available", job["error"])

        # A proposal writes nothing; the studio shows it ticked, to edit and untick.
        with patch("utils.llm_model_loader.status", return_value=self.READY), \
                patch.object(WorldAugmenter, "propose", return_value=self.ADDITIONS):
            job = _finished(self.client, self.client.post("/v1/jobs/world-proposal").json()["id"])
            picks, text, note = app.propose_world_ui(progress=lambda *a, **k: None)
        self.assertEqual(job["status"], "finished", job.get("error"))
        self.assertEqual(job["result"], self.ADDITIONS)
        self.assertEqual(picks["value"], ["Place: Lighthouse", "Person: Lamplighter", "Item: Brass Lens"])
        self.assertEqual({name: (self.dir / name).read_text() for name in files}, files)

        # Invalid or repeated entries are refused and nothing is written.
        for bad in ({"objects": [{"name": "Ancient Map"}]},                        # already in the world
                    {"locations": {"Harbor": {"tags": ["harbor"]}}},
                    {"characters": [{"name": "Lamplighter", "role": "emperor"}]},  # no such role
                    {"objects": [{"name": "Lens"}, {"name": "Lens"}]}, {}):
            with self.subTest(bad=bad):
                self.assertEqual(self.client.post("/v1/world/additions", json=bad).status_code, 400)
        self.assertEqual({name: (self.dir / name).read_text() for name in files}, files)

        # The ticked entries (the place and the person, not the item) are added.
        message, _ = app.add_proposal_ui(text, ["Place: Lighthouse", "Person: Lamplighter"])
        self.assertIn("Lighthouse", message)
        self.assertIn("Lighthouse", self._yaml("locations.yaml")["world_definition"]["locations"])
        self.assertIn("Lamplighter", [c["name"] for c in self._yaml("npcs.yaml")["world_definition"]["characters"]])
        self.assertEqual((self.dir / "items.yaml").read_text(), files["items.yaml"])

    def test_the_augmenter_proposes_without_writing(self):
        from core_engine.augmenter import WorldAugmenter

        augmenter = WorldAugmenter()

        class Model:
            def generate(self, prompt):
                return "{}"

            def extract_json(self, text):
                return {"locations": {"Harbor": {"tags": ["harbor"]}, "Lighthouse": {"tags": ["harbor", "secluded"]}},
                        "objects": [{"name": "Ancient Map"}, {"name": "Brass Lens"}],
                        "characters": [{"name": "Lamplighter"}]}

        augmenter._llm = Model()
        with patch.object(WorldAugmenter, "save_augmentation") as saved, \
                patch.object(WorldAugmenter, "apply_to_modular_config") as written:
            proposal = augmenter.propose()
        self.assertEqual(list(proposal["locations"]), ["Lighthouse"])  # only what is new
        self.assertEqual([o["name"] for o in proposal["objects"]], ["Brass Lens"])
        self.assertEqual([c["name"] for c in proposal["characters"]], ["Lamplighter"])
        saved.assert_not_called()
        written.assert_not_called()

    def test_a_proposal_s_actions_take_the_engine_s_shape(self):
        """The prompt shows a real action of the world as the shape to follow, and the common deviations
        of small models (Qwen3.5-4B) are put into it, so the proposal can be added."""
        from core_engine.augmenter import WorldAugmenter

        example = WorldAugmenter.example_action(store.config.get("world_definition"))
        self.assertTrue(example["name"] and example["outcomes"] and all(o["desc"] for o in example["outcomes"]))
        fixed = WorldAugmenter.normalize_actions([
            {"action": "calibrate_gears", "target_tags": ["science"], "outcome": "A hidden dial turns."},
            {"name": "Ask around", "text": "Nobody saw anything.", "colour": "red"},
            {"name": "Search", "outcomes": ["You find dust.", {"desc": "A key!", "tags": ["discovery"], "odd": 1}]},
            "Wait", {"name": "Empty", "outcomes": []}, 7])
        self.assertEqual(fixed, [
            {"name": "Calibrate gears", "outcomes": [{"desc": "A hidden dial turns.", "tags": [], "target_tags": ["science"]}]},
            {"name": "Ask around", "outcomes": [{"desc": "Nobody saw anything.", "tags": []}]},
            {"name": "Search", "outcomes": [{"desc": "You find dust.", "tags": []},
                                            {"desc": "A key!", "tags": ["discovery"]}]}])

        class Model:
            asked = []

            def generate(self, prompt):
                Model.asked.append(prompt)
                return "{}"

            def extract_json(self, text):
                return {"locations": {"Clock Tower": {"tags": ["urban", "guarded"], "descriptions": ["Gears."],
                                                      "actions": [{"action": "climb_stairs", "outcome": "Wind."}]}},
                        "objects": [], "characters": [{"name": "Tinker", "associated_tags": ["urban"],
                                                       "actions": [{"name": "Talk", "text": "She hums."}]}]}

        augmenter = WorldAugmenter()
        augmenter._llm = Model()
        proposal = augmenter.propose()
        self.assertIn('"outcomes"', Model.asked[0])
        self.assertIn(example["name"], Model.asked[0])                          # the world's own action as example
        added = self.client.post("/v1/world/additions", json=proposal)          # the engine accepts it
        self.assertEqual(added.status_code, 200, added.text)
        self.assertEqual(self._yaml("locations.yaml")["world_definition"]["locations"]["Clock Tower"]["actions"],
                         [{"name": "Climb stairs", "outcomes": [{"desc": "Wind.", "tags": []}]}])

    def test_augmentation_is_switched_by_a_saved_tick_and_runs_on_its_own_model(self):
        """🌍 World → ✨ Grow the world: the tick is augmentation.enabled (saved, the file's comments kept);
        off, nothing is proposed (409); the model shown is augmentation's own (Qwen3.5-4B on vLLM)."""
        import app

        before = (self.dir / "hyperparameters.yaml").read_text()
        with patch("utils.llm_model_loader.status", return_value={**self.READY, "model": "Qwen/Qwen3.5-4B"}) as asked:
            state = self.client.get("/v1/augmentation").json()
        self.assertEqual(asked.call_args.kwargs["role"], "augmentation")
        self.assertEqual((state["enabled"], state["model"]["model"]), (True, "Qwen/Qwen3.5-4B"))
        # Switched off: written into hyperparameters.yaml, that value only (its comments and layout kept).
        with patch("utils.llm_model_loader.status", return_value=self.NOT_READY):
            off = self.client.put("/v1/augmentation", json={"enabled": False})
        self.assertEqual(off.status_code, 200, off.text)
        after = (self.dir / "hyperparameters.yaml").read_text()
        self.assertFalse(yaml.safe_load(after)["augmentation"]["enabled"])
        changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
        self.assertEqual(changed, [("  enabled: true", "  enabled: false")])
        self.assertEqual(self.client.put("/v1/augmentation", json={"enabled": "maybe"}).status_code, 422)
        # Off (as the reloaded configuration has it): a proposal is refused before any job is queued.
        jobs = len(sessions.list_jobs())
        with patch.dict(store.config._config["augmentation"], {"enabled": False}):
            refused = self.client.post("/v1/jobs/world-proposal")
            self.assertEqual(refused.status_code, 409)
            self.assertIn("World augmentation is off", refused.json()["detail"])
            self.assertEqual(len(sessions.list_jobs()), jobs)
            with patch("utils.llm_model_loader.status", return_value=self.READY):
                tick, said, propose = app.augmentation_ui()
            self.assertEqual((_prop(tick, "value"), _prop(propose, "interactive")), (False, False))
            self.assertIn("World augmentation is off", said)
        # The studio's tick: saved, and Propose follows it.
        with patch("utils.llm_model_loader.status", return_value=self.READY):
            said, propose = app.set_augmentation_ui(True)
        self.assertTrue(_prop(propose, "interactive"))
        self.assertTrue(yaml.safe_load((self.dir / "hyperparameters.yaml").read_text())["augmentation"]["enabled"])
        self.assertIn("Its model is ready", said)
        demo = app.build_app()
        tick = next(b for b in demo.blocks.values() if getattr(b, "elem_id", None) == "grow-augment")
        (wired,) = [fn for fn in demo.fns.values() if (tick._id, "input") in fn.targets]
        self.assertEqual(wired.fn.__name__, "set_augmentation_ui")

    def test_the_terminal_proposes_first_and_adds_only_what_was_reviewed(self):
        """python main.py: augmentation keeps a proposal for review and changes no configuration;
        --apply-augmentation adds the reviewed file, checked as the studio checks it."""
        import main
        from core_engine.augmenter import WorldAugmenter

        files = {name: (self.dir / name).read_text() for name in ("locations.yaml", "npcs.yaml", "items.yaml")}
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, True)
        proposal = {"locations": {"Lighthouse": {"tags": ["harbor", "secluded"], "descriptions": ["A lamp turns."]}},
                    "objects": [], "characters": [{"name": "Lamplighter", "tags": ["harbor"]}]}
        with patch.object(WorldAugmenter, "is_sufficient", return_value=False), \
                patch.object(WorldAugmenter, "propose", return_value=proposal), \
                patch.object(WorldAugmenter, "augment") as augment, \
                patch.object(WorldAugmenter, "apply_to_modular_config") as written, \
                patch("sys.stdout", new_callable=io.StringIO) as said:
            with patch.dict(store.config._config, {"augmentation": {**store.config.get("augmentation"),
                                                                    "output_dir": str(out)}}):
                path = main.propose_augmentation()
        augment.assert_not_called()
        written.assert_not_called()
        self.assertEqual({name: (self.dir / name).read_text() for name in files}, files)  # nothing written
        self.assertTrue(Path(path).is_file() and Path(path).is_relative_to(out))
        self.assertEqual(yaml.safe_load(Path(path).read_text()), proposal)
        self.assertIn("1 places: Lighthouse", said.getvalue())
        self.assertIn(f"--apply-augmentation {path}", said.getvalue())
        # Enough already: nothing proposed.
        with patch.object(WorldAugmenter, "is_sufficient", return_value=True), \
                patch.object(WorldAugmenter, "propose") as asked, patch("sys.stdout", new_callable=io.StringIO):
            self.assertIsNone(main.propose_augmentation())
        asked.assert_not_called()
        # An invalid reviewed file (a name taken): refused, nothing written.
        taken = Path(out) / "taken.yaml"
        taken.write_text(yaml.safe_dump({"locations": {"Harbor": {"tags": ["harbor"]}}}))
        with patch("sys.stdout", new_callable=io.StringIO), self.assertRaises(SystemExit) as refused:
            main.engine_command(["--apply-augmentation", str(taken)])
        self.assertIn("`Harbor` is already a location", str(refused.exception.code))
        self.assertEqual({name: (self.dir / name).read_text() for name in files}, files)
        # The reviewed file: added, entry by entry, the rest of each file kept (the map's routes too).
        with patch("sys.stdout", new_callable=io.StringIO) as said:
            main.engine_command(["--apply-augmentation", path])
        locations = self._yaml("locations.yaml")["world_definition"]
        self.assertIn("Lighthouse", locations["locations"])
        self.assertEqual(locations.get("routes"), yaml.safe_load(files["locations.yaml"])["world_definition"].get("routes"))
        self.assertIn("Lamplighter", [c["name"] for c in self._yaml("npcs.yaml")["world_definition"]["characters"]])
        self.assertEqual((self.dir / "items.yaml").read_text(), files["items.yaml"])
        self.assertIn("Added to the configuration", said.getvalue())


class RecordsImportAndPointsTests(_Sandbox):
    """Importing earlier engines and games from the server's folder (the terminal's --import-artifacts),
    and a place's points of interest (the terminal editor's interaction points)."""

    def test_the_import_folder_and_its_job(self):
        import app

        folder = self.dir / "artifacts"
        folder.mkdir()
        with patch.dict(os.environ, {"ANE_ARTIFACT_DIR": str(folder)}):
            self.assertEqual(self.client.get("/v1/records/import").json(), {"folder": str(folder), "files": []})
            empty = self.client.post("/v1/jobs/records-import", json={})
            self.assertEqual(empty.status_code, 400)
            self.assertIn("nothing to import", empty.json()["detail"])
            (folder / "notes.yaml").write_text("just: notes\n")
            self.assertEqual(self.client.get("/v1/records/import").json()["files"], ["notes.yaml"])
            self.assertIn("notes.yaml", app.import_folder_ui())
            job = _finished(self.client, self.client.post("/v1/jobs/records-import",
                                                          json={"move": False, "preview": False}).json()["id"])
        self.assertEqual(job["status"], "finished", job.get("error"))
        self.assertEqual((job["result"]["files"], job["result"]["engines"]), (1, 0))
        self.assertEqual([f["file"] for f in job["result"]["failed"]], ["notes.yaml"])  # not an engine: it stays
        self.assertTrue((folder / "notes.yaml").exists())

    def test_a_place_s_points_of_interest(self):
        import app

        shown = app.details_form_ui("Locations", "Harbor")
        table = shown[-1]
        self.assertEqual(list(table.columns), app.POINT_COLUMNS)
        self.assertIn("Waterfront", list(table["Label"]))
        details = self.client.get("/v1/entities/Locations/Harbor/details").json()
        self.assertIn("interaction_points", details["fields"])
        status, _ = app.save_details_ui("Locations", "Harbor", [], [], "", None, *shown[7:11], table)
        self.assertIn("Harbor", status)
        place = self._yaml("locations.yaml")["world_definition"]["locations"]["Harbor"]
        self.assertEqual(place["interaction_points"], details["details"]["interaction_points"])  # nothing lost
        bad = [dict(details["details"]["interaction_points"][0], linked_location="Moon")]
        refused = self.client.put("/v1/entities/Locations/Harbor/details", json={"details": {"interaction_points": bad}})
        self.assertEqual(refused.status_code, 400)
        self.assertIn("Moon", refused.json()["detail"])


def _prop(update, name):
    """A property of what a handler returns: an update (a dict) or a component (Gradio 6)."""
    return update[name] if isinstance(update, dict) else getattr(update, name)


class StudioLayoutTests(_Sandbox):
    """The studio's destinations: World, Train, Play, Evaluate, Export and Advanced, each a tab with a stable
    id, chosen with the workflow banner (wide screens) or the labelled menu (narrow screens). The game is
    played in ▶ Play only."""

    @classmethod
    def _inside(cls, block, kind):
        found, stack = [], list(block.children)
        while stack:
            child = stack.pop()
            if isinstance(child, kind):
                found.append(child)
            stack.extend(getattr(child, "children", []) or [])
        return found

    def _studio(self):
        import gradio as gr

        import app

        demo = app.build_app()
        main = next(block for block in demo.blocks.values() if isinstance(block, gr.Tabs) and block.elem_id == "ane-nav")
        return app, demo, main

    def test_the_destinations_and_where_the_game_is(self):
        import gradio as gr

        app, demo, main = self._studio()
        self.assertEqual([(tab.id, tab.label) for tab in main.children], app.DESTINATIONS)
        self.assertEqual(app.DESTINATION_IDS, ["world", "train", "play", "evaluate", "export", "advanced"])
        by_id = {tab.id: tab for tab in main.children}
        choices = [b for b in self._inside(by_id["play"], gr.Button) if "choice" in (b.elem_classes or [])]
        self.assertEqual(len(choices), app.CHOICE_BUTTONS)
        for elsewhere in ("world", "train", "evaluate", "export", "advanced"):  # the game is in ▶ Play only
            self.assertFalse([b for b in self._inside(by_id[elsewhere], gr.Button)
                              if "choice" in (b.elem_classes or [])], elsewhere)
        sections = {key: {tab.label for tab in self._inside(tab, gr.Tab)} for key, tab in by_id.items()}
        self.assertLessEqual({"📝 Entries", "Fields", "Actions & outcomes", "Scheduling", "More details", "✨ Suggest",
                              "🎯 Missions", "📜 Story rules", "🗺 Map", "✨ Grow the world", "🔍 Checks"},
                             sections["world"])
        self.assertEqual(sections["train"], {"Run", "From a recorded engine", "🏷 Finished engines"})
        self.assertEqual(sections["advanced"], {"Simulation", "Weights", "Engine logic", "App & jobs", "🗂 Records",
                                                "🔌 Integration"})
        # The records by purpose: continue/retrain in Train, stories in Evaluate, files in Export, import in Advanced.
        buttons = {key: {b.value for b in self._inside(tab, gr.Button)} for key, tab in by_id.items()}
        self.assertLessEqual({"Continue its training", "Retrain from it"}, buttons["train"])
        self.assertLessEqual({"Judge this story (Gemma 4)", "Which is the better game?"}, buttons["evaluate"])
        self.assertLessEqual({"Download its model file and world", "Download it as a configuration to upload"},
                             buttons["export"])
        self.assertIn("Import", buttons["advanced"])
        for moved in ("Judge this story (Gemma 4)", "Download its model file and world", "Import"):
            self.assertNotIn(moved, buttons["train"])
        # The integration diagnostics are in ⚙ Advanced → 🔌 Integration only: not in Play, Export or Train.
        diagnostics = {"Set the player", "Tell the session", "Show the session's parameters (ane.session_state/1)",
                       "Send its latest report to the UNP again", "Download the world as a UNP receives it (JSON)"}
        buttons = {key: {b.value for b in self._inside(tab, gr.Button)} for key, tab in by_id.items()}
        self.assertLessEqual(diagnostics, buttons["advanced"])
        for elsewhere in ("world", "train", "play", "evaluate", "export"):
            self.assertFalse(diagnostics & buttons[elsewhere], elsewhere)
        # Train: run configuration, progress and the completed engine, learning results; no game.
        self.assertLessEqual({"🧠 Evolve storytellers", "▶ Play this result", "⚖ Evaluate this result",
                              "📦 Export this result"}, buttons["train"])
        self.assertFalse({"▶ Play the newest", "📦 Export it"} & buttons["train"])
        export_buttons = [b.value for b in self._inside(by_id["export"], gr.Button)]
        self.assertIn("Export", export_buttons)
        # Export's generation picker is filled by a training: one component, rendered in 📦 Export.
        generation = [d for d in self._inside(by_id["export"], gr.Dropdown) if (d.label or "").startswith("Generation")]
        self.assertEqual(len(generation), 1)

    def test_every_destination_is_reached_from_the_banner_the_menu_and_the_address(self):
        import gradio as gr

        app, demo, main = self._studio()
        blocks = demo.blocks.values()
        banner = next(b for b in blocks if isinstance(b, gr.Row) and b.elem_id == "ane-banner")
        menu = next(b for b in blocks if isinstance(b, gr.Radio) and b.elem_id == "ane-menu")
        buttons = [b for b in banner.children if isinstance(b, gr.Button)]
        self.assertEqual([(b.elem_id, b.value) for b in buttons],
                         [(f"nav-{key}", label) for key, label in app.DESTINATIONS])  # the whole banner, in order
        self.assertEqual([b.variant for b in buttons], ["primary"] + ["secondary"] * 5)  # the World is open first
        self.assertEqual([value for _, value in menu.choices], app.DESTINATION_IDS)    # the menu: every destination
        self.assertTrue(menu.label.startswith("☰ Menu"))

        def wired(trigger, event):
            return [fn for fn in demo.fns.values() if (trigger._id, event) in fn.targets]

        # A click: where the user is, the entry editor's draft and its baseline (nothing unsaved here).
        # A click: where the user is, the World section open, the entry, the unsaved parts (none here).
        draft = ("world", "entries", {"kind": "Locations", "entity": "Harbor"}, {})
        nav = len(app.DESTINATIONS) + 3
        for button, key in zip(buttons, app.DESTINATION_IDS):
            with self.subTest(destination=key):
                (fn,) = wired(button, "click")
                self.assertIn(main._id, [o._id for o in fn.outputs])  # the tabs follow
                out = fn.fn(*draft)
                selected, *highlight, named, now = out[:nav]
                self.assertEqual(_prop(selected, "selected"), key)
                self.assertEqual([_prop(b, "variant") for b in highlight],
                                 ["primary" if other == key else "secondary" for other in app.DESTINATION_IDS])
                self.assertEqual((_prop(named, "value"), now), (key, key))
                # Nothing else changes on the way: every other output is skipped (nothing was unsaved).
                self.assertTrue(all(o == gr.skip() for o in out[nav:]))
                (via_menu,) = wired(menu, "input")
                self.assertEqual(_prop(via_menu.fn(key, *draft)[0], "selected"), key)
                # In the browser first: the highlight at once (unless a guard may keep the page), then the server.
                self.assertEqual(fn.js, f'(...args) => {{ window.aneNavShow && window.aneNavShow("{key}"); return args; }}')
        self.assertIn("window.aneNavShow(args[0])", via_menu.js)
        head = app.DRAFT_HEAD
        self.assertIn('if (!banner || document.querySelector(\'.draft-status[data-dirty="1"]\')) return;', head)
        self.assertIn("banner.dataset.pending = key;", head)               # a mark: the buttons stay Gradio's
        self.assertIn(app.NAV_PENDING_CSS, app.CSS)
        self.assertIn('#ane-banner[data-pending="play"] > #nav-play', app.NAV_PENDING_CSS)
        self.assertIn("delete banner.dataset.pending;", head)              # the server's answer clears it
        # The address follows the destination open, however it was opened (replaced, not a new history entry).
        self.assertIn("new MutationObserver(write).observe(banner", head)
        self.assertIn('url.searchParams.set("section", key);', head)
        self.assertIn("history.replaceState(history.state, \"\", url);", head)
        # The address names a destination (?section=play); anything else opens the World.
        for asked, opened in (("play", "play"), ("EXPORT", "export"), ("nowhere", "world"), (None, "world")):
            request = type("Request", (), {"query_params": {"section": asked} if asked else {}})()
            self.assertEqual(_prop(app.open_requested(request)[0], "selected"), opened)
        self.assertEqual(_prop(app.navigate("nowhere")[0], "selected"), "world")
        # Links inside a destination: Train's actions on its completed engine (disabled until one exists).
        train = next(tab for tab in main.children if tab.id == "train")
        links = {b.elem_id: b for b in self._inside(train, gr.Button)}
        for elem_id, destination in (("train-play-result", "play"), ("train-evaluate-result", "evaluate"),
                                     ("train-export-result", "export")):
            with self.subTest(action=elem_id):
                self.assertFalse(links[elem_id].interactive)
                (fn,) = wired(links[elem_id], "click")
                self.assertIn(main._id, [o._id for o in fn.outputs])  # it opens its destination
                self.assertEqual(fn.inputs[0].__class__.__name__, "State")  # given the completed engine
                with self.assertRaises(gr.Error):  # before any training: nothing to carry
                    fn.fn(*[None] * len(fn.inputs))

    def test_narrow_screens_get_the_menu_and_no_destination_hides_behind_an_overflow_button(self):
        import app

        css = re.sub(r"\s+", " ", app.CSS)
        self.assertIn("#ane-nav > .tab-wrapper {display: none !important}", css)  # no tab bar: no overflow button
        self.assertIn("#ane-menu {display: none !important}", css)
        narrow = css[css.index("@media (max-width: 820px)"):]
        narrow = narrow[:narrow.index("} }") + 3]
        self.assertIn("#ane-banner {display: none !important}", narrow)
        self.assertIn("#ane-menu {display: block !important}", narrow)
        self.assertIn('.overflow-menu > button::after {content: "More"', css)  # a section's overflow says so
