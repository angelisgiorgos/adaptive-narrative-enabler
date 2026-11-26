"""The upload and export result contract: a configuration upload is staged, validated, committed and
cleaned up in that order; its answer carries the accepted revision; a rejected upload leaves the active
revision as it was; a cleanup problem after the commit never turns the accepted upload into a failure,
and never hides the validation error of a rejected one. Temporary files are written through the
descriptor ``mkstemp`` opens, and that descriptor is closed before the path is reopened or removed (on
Windows an open handle keeps a file from being removed: the failure these tests simulate on any OS)."""
try:  # recording and downloads go to temporary folders, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import copy
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

import api
from backend import export, store


def _room(tags, description, other):
    return {"tags": tags, "descriptions": [description], "start_eligible": True, "start_weight": 1.0,
            "actions": [{"name": "Look around", "outcomes": [
                {"desc": description, "tags": ["discovery"], "reveal_location": other}]}]}


TWO_ROOMS = {
    "world_definition": {
        "locations": {"Hall": _room(["exploration"], "A quiet hall.", "Cellar"),
                      "Cellar": _room(["secluded"], "A damp cellar.", "Hall")},
        "characters": [{"name": "Keeper", "associated_tags": ["secluded"], "descriptions": ["An old keeper."]}],
        "objects": [{"name": "Lamp", "associated_tags": ["exploration"], "descriptions": ["A brass lamp."]}],
    },
    "game_state": {"arrest_location": "Cellar"},
    "start_selection": {"default_location": "Hall"},
}


def _bad_room_reference():
    world = copy.deepcopy(TWO_ROOMS)
    world["start_selection"]["default_location"] = "Attic"  # not a room of this world
    return world


def _file(world, name="two_rooms.yaml"):
    return {"file": (name, yaml.safe_dump(world, sort_keys=False).encode(), "application/x-yaml")}


class _TrackedDescriptors:
    """Wraps ``tempfile.mkstemp`` and remembers the descriptors it opened, to check they are closed."""

    def __init__(self):
        self.opened = []
        self._real = tempfile.mkstemp

    def __call__(self, *args, **kwargs):
        handle, path = self._real(*args, **kwargs)
        identity = os.fstat(handle)
        self.opened.append((handle, path, (identity.st_dev, identity.st_ino)))
        return handle, path

    def leaked(self):
        """The files whose descriptor is still open (the same file, even if it was removed since)."""
        leaked = []
        for handle, path, identity in self.opened:
            try:
                now = os.fstat(handle)
            except OSError:
                continue  # closed
            if (now.st_dev, now.st_ino) == identity:  # not a reused number for another file
                leaked.append(path)
        return leaked


def _locked_unlink(marker):
    """A ``Path.unlink`` that fails for the files whose name holds ``marker``, as Windows does for a
    file another handle still holds open."""
    real = Path.unlink

    def unlink(self, missing_ok=False):
        if marker in self.name and self.exists():
            raise PermissionError(13, "The process cannot access the file because it is being used by another "
                                      "process", str(self))
        return real(self, missing_ok=missing_ok)

    return unlink


class _Contract(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.temp = Path(directory.name) / "tmp"
        self.temp.mkdir()
        # Every temporary file of the request goes here, so what is left behind can be counted.
        temp = patch.object(tempfile, "tempdir", str(self.temp))
        temp.start()
        self.addCleanup(temp.stop)
        self.addCleanup(store.use_builtin_config)
        store.use_builtin_config()
        self.client = TestClient(api.api, headers={"X-ANE-User": "api-tests"})

    def revision(self):
        return self.client.get("/v1/world/revision").json()["revision"]

    def staged_files(self):
        return sorted(p.name for p in self.temp.iterdir() if p.name.startswith("ane-upload-"))


class UploadContractTests(_Contract):
    def test_a_valid_upload_is_accepted_once_with_its_revision(self):
        before = self.revision()
        response = self.client.post("/v1/config/upload", files=_file(TWO_ROOMS))
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["accepted"])
        self.assertNotIn("cleanup_warning", body)
        self.assertIn("in use now for what starts from here", body["message"])  # the existing message is kept
        self.assertIn("settings", body)
        self.assertNotEqual(body["revision"], before)
        self.assertEqual(body["revision"], self.revision())                 # the revision now active
        active = self.client.get("/v1/config").json()["config"]["world_definition"]
        self.assertEqual(set(active["locations"]), {"Hall", "Cellar"})
        self.assertEqual(self.staged_files(), [])                            # the staged file is gone

    def test_an_identical_retry_gives_the_same_revision(self):
        first = self.client.post("/v1/config/upload", files=_file(TWO_ROOMS)).json()["revision"]
        second = self.client.post("/v1/config/upload", files=_file(TWO_ROOMS))
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["revision"], first)                  # one accepted revision, not two
        self.assertEqual(self.revision(), first)
        self.assertEqual(self.staged_files(), [])

    def test_an_invalid_room_reference_is_refused_and_the_revision_kept(self):
        accepted = self.client.post("/v1/config/upload", files=_file(TWO_ROOMS)).json()["revision"]
        for attempt in range(2):  # a retry of the rejected upload is refused the same way
            with self.subTest(attempt=attempt):
                response = self.client.post("/v1/config/upload", files=_file(_bad_room_reference(), "bad.yaml"))
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("`start_selection.default_location` is `Attic`, which is not a place of this world",
                              response.json()["detail"])
                self.assertEqual(self.revision(), accepted)
                active = self.client.get("/v1/config").json()["config"]
                self.assertEqual(active["start_selection"]["default_location"], "Hall")
                self.assertEqual(self.staged_files(), [])

    def test_a_rejected_upload_on_the_builtin_world_keeps_it(self):
        before = self.revision()
        response = self.client.post("/v1/config/upload", files=_file(_bad_room_reference(), "bad.yaml"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.revision(), before)
        self.assertFalse(self.client.get("/v1/config").json()["using_override"])

    def test_wrong_type_and_size_are_refused_before_staging(self):
        before = self.revision()
        wrong = self.client.post("/v1/config/upload", files={"file": ("world.txt", b"world_definition: {}")})
        self.assertEqual(wrong.status_code, 400)
        self.assertIn("Allowed file types", wrong.json()["detail"])
        with patch.object(store, "MAX_CONFIG_UPLOAD_BYTES", 10):
            large = self.client.post("/v1/config/upload", files=_file(TWO_ROOMS))
        self.assertEqual(large.status_code, 413)
        self.assertEqual(self.revision(), before)
        self.assertEqual(self.staged_files(), [])

    def test_the_staged_upload_descriptor_is_closed(self):
        tracked = _TrackedDescriptors()
        with patch.object(tempfile, "mkstemp", tracked):
            ok = self.client.post("/v1/config/upload", files=_file(TWO_ROOMS))
            bad = self.client.post("/v1/config/upload", files=_file(_bad_room_reference(), "bad.yaml"))
        self.assertEqual((ok.status_code, bad.status_code), (200, 400))
        self.assertGreaterEqual(len(tracked.opened), 2)
        self.assertEqual(tracked.leaked(), [])

    def test_a_cleanup_problem_after_the_commit_does_not_fail_the_upload(self):
        before = self.revision()
        with patch.object(Path, "unlink", _locked_unlink("ane-upload-")), self.assertLogs(api.LOG, "WARNING"):
            response = self.client.post("/v1/config/upload", files=_file(TWO_ROOMS))
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["accepted"])
        self.assertIn("could not be removed", body["cleanup_warning"])
        self.assertNotEqual(body["revision"], before)
        self.assertEqual(body["revision"], self.revision())
        # The same upload again (the caller retrying): accepted again with the same revision.
        with patch.object(Path, "unlink", _locked_unlink("ane-upload-")), self.assertLogs(api.LOG, "WARNING"):
            again = self.client.post("/v1/config/upload", files=_file(TWO_ROOMS))
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["revision"], body["revision"])

    def test_a_cleanup_problem_does_not_hide_the_validation_error(self):
        accepted = self.client.post("/v1/config/upload", files=_file(TWO_ROOMS)).json()["revision"]
        with patch.object(Path, "unlink", _locked_unlink("ane-upload-")), self.assertLogs(api.LOG, "WARNING"):
            response = self.client.post("/v1/config/upload", files=_file(_bad_room_reference(), "bad.yaml"))
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("is not a place of this world", response.json()["detail"])
        self.assertEqual(self.revision(), accepted)

    def test_discard_never_raises(self):
        self.assertIsNone(api._discard(None))
        self.assertIsNone(api._discard(self.temp / "missing.yaml"))
        locked = self.temp / "ane-upload-locked.yaml"
        locked.write_text("x")
        with patch.object(Path, "unlink", _locked_unlink("ane-upload-")), self.assertLogs(api.LOG, "WARNING"):
            self.assertIn("PermissionError", api._discard(locked))


class ExportContractTests(_Contract):
    """The two-room world uploaded, a tiny run trained on it, exported, downloaded and reopened."""

    def _train(self):
        self.assertEqual(self.client.post("/v1/config/upload", files=_file(TWO_ROOMS)).status_code, 200)
        run = self.client.post("/v1/simulations", json={"candidate_count": 2, "generations": 1, "simulation_steps": 4,
                                                        "episodes": 1, "deterministic": True, "seed": 7,
                                                        "warm_start": False, "start_game": False})
        self.assertEqual(run.status_code, 200, run.text)
        return run.json()["run_id"]

    def _export_and_reopen(self, run_id):
        response = self.client.post("/v1/exports", json={"source": "run", "run_id": run_id})
        self.assertEqual(response.status_code, 200, response.text)
        download = self.client.get(response.json()["download_url"])
        self.assertEqual(download.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            self.assertIsNone(archive.testzip())
            (manifest_name,) = [n for n in archive.namelist() if n.endswith("/engine.json") and n.count("/") == 1]
            manifest = json.loads(archive.read(manifest_name))
        return response.json(), manifest

    def test_the_exported_engine_reopens_with_its_manifest_and_leaves_no_temporary_file(self):
        run_id = self._train()
        tracked = _TrackedDescriptors()
        with patch.object(tempfile, "mkstemp", tracked):
            answer, manifest = self._export_and_reopen(run_id)
        self.assertEqual(manifest["format"], export.FORMAT)
        self.assertEqual(manifest["platform"], "python")
        self.assertEqual(manifest["name"], answer["name"])
        self.assertEqual(manifest["world"]["locations"], 2)
        self.assertEqual(manifest["world"], answer["world"])
        self.assertEqual(tracked.leaked(), [])
        self.assertEqual([p.name for p in self.temp.iterdir() if p.suffix == ".zip"], [])  # moved to the artifacts
        # The same export again: another archive with the same manifest content.
        _, again = self._export_and_reopen(run_id)
        for key in ("format", "world", "genome", "engine_id", "platform"):
            self.assertEqual(again[key], manifest[key])

    def test_a_cleanup_problem_after_the_export_does_not_fail_it(self):
        run_id = self._train()
        with patch.object(Path, "unlink", _locked_unlink(".zip")), self.assertLogs(api.LOG, "WARNING"):
            response = self.client.post("/v1/exports", json={"source": "run", "run_id": run_id})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get(response.json()["download_url"]).status_code, 200)


class ArchiveAllocationTests(unittest.TestCase):
    def test_the_archive_descriptor_is_closed_and_a_failed_write_leaves_nothing(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tempfile, "tempdir", directory):
            tracked = _TrackedDescriptors()
            with patch.object(tempfile, "mkstemp", tracked):
                archive = export._temporary_archive("engine", ".zip")
            self.assertEqual(tracked.leaked(), [])
            self.assertTrue(archive.exists())
            with zipfile.ZipFile(archive, "w") as zipped:  # the path reopens for writing
                zipped.writestr("engine.json", "{}")
            archive.unlink()                                 # and is removed (Windows: no open handle left)
            failing = export._temporary_archive("engine", ".unitypackage")
            with self.assertRaises(RuntimeError), export._removed_on_failure(failing):
                failing.write_bytes(b"half")
                raise RuntimeError("writer failed")
            self.assertEqual(os.listdir(directory), [])


if __name__ == "__main__":
    unittest.main()
