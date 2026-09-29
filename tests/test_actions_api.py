"""Action editing uses temporary files, never the project's engine source."""
import os
from contextlib import ExitStack
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import ui_authoring


class ActionsAPITests(unittest.TestCase):
    def setUp(self):
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        directory = self.contexts.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "actions.py"
        self.original = "class Action: pass\nclass Outcome: pass\n"
        self.path.write_text(self.original)
        self.contexts.enter_context(patch.object(ui_authoring, "ACTIONS_PATH", self.path))
        self.contexts.enter_context(patch.dict(os.environ, {"ANE_AUTHORING_TOKEN": "test-author-token"}))
        self.client = self.contexts.enter_context(TestClient(api.api))
        self.client.headers["Authorization"] = "Bearer test-author-token"

    def test_read_save_backup_and_ui_reload(self):
        loaded = self.client.get("/v1/actions")
        self.assertEqual(loaded.status_code, 200)
        body = loaded.json()
        self.assertEqual(body["source"], self.original)
        # Saving must compile the source without executing it.
        body["source"] += "raise RuntimeError('must not execute during save')\n"
        saved = self.client.put("/v1/actions", json=body)
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.json()["restart_required"])
        self.assertNotEqual(saved.json()["revision"], body["revision"])
        self.assertEqual(self.path.read_text(), body["source"])
        self.assertEqual(self.path.with_suffix(".py.bak").read_text(), self.original)
        source, revision, _ = api.studio.load_actions_editor()
        self.assertEqual(source, body["source"])
        self.assertEqual(revision, saved.json()["revision"])
        # Both interfaces use the same revision and persistence contract.
        revision, _ = api.studio.save_actions_editor(self.original, revision)
        self.assertEqual(self.client.get("/v1/actions").json(),
                         {"source": self.original, "revision": revision})

    def test_invalid_source_does_not_modify_file(self):
        body = self.client.get("/v1/actions").json()
        for invalid in ("not valid Python!", "class Action: pass\n"):
            with self.subTest(source=invalid):
                response = self.client.put("/v1/actions", json={**body, "source": invalid})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.path.read_text(), self.original)
                self.assertFalse(self.path.with_suffix(".py.bak").exists())

    def test_stale_revision_preserves_source_and_backup(self):
        body = self.client.get("/v1/actions").json()
        updated = body["source"] + "# API edit\n"
        self.assertEqual(self.client.put("/v1/actions", json={**body, "source": updated}).status_code, 200)
        response = self.client.put("/v1/actions", json=body)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.path.read_text(), updated)
        self.assertEqual(self.path.with_suffix(".py.bak").read_text(), self.original)

    def test_authentication_and_disabled_authoring(self):
        body = self.client.get("/v1/actions").json()
        for token in (None, "Bearer wrong-token", "Basic test-author-token"):
            self.client.headers.pop("Authorization", None)
            if token:
                self.client.headers["Authorization"] = token
            self.assertEqual(self.client.get("/v1/actions").status_code, 401)
            self.assertEqual(self.client.put("/v1/actions", json=body).status_code, 401)
        with patch.dict(os.environ, {"ANE_AUTHORING_TOKEN": ""}):
            self.assertEqual(self.client.get("/v1/actions").status_code, 503)
            self.assertEqual(self.client.put("/v1/actions", json=body).status_code, 503)
        self.assertEqual(self.path.read_text(), self.original)
        self.assertFalse(self.path.with_suffix(".py.bak").exists())

    def test_request_schema_and_editor_load_event(self):
        for body in ({}, {"source": self.original}, {"source": "", "revision": "bad"}):
            self.assertEqual(self.client.put("/v1/actions", json=body).status_code, 422)
        schema = self.client.get("/openapi.json").json()
        self.assertIn("security", schema["paths"]["/v1/actions"]["put"])
        self.assertTrue(any(fn.fn is api.studio.load_actions_editor and
                            any(event == "load" for _, event in fn.targets)
                            for fn in api.studio.demo.fns.values()))


if __name__ == "__main__":
    unittest.main()
