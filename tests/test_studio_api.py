"""Studio and REST API coverage for actions, missions, rules, profiles, and play state."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

import api
from backend import content, store
from core_engine import variables as persistent


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


if __name__ == "__main__":
    unittest.main()
