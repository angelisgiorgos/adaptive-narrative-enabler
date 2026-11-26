"""Persistent play sessions (backend/persistence.py, backend/sessions.py, api.py): every game is saved
after each change that matters and restored exactly after a restart of the API; the durable session
index; named and manual save slots; continue, restore and restart; ownership (no one opens another
user's games or saves); atomic, serialised writes; the retention policy; and the existing
ane.savegame/1 format throughout."""
try:  # recording goes to a temporary folder, not the real records database
    import records_sandbox  # noqa: F401
except ImportError:  # run as tests.<module>
    from tests import records_sandbox  # noqa: F401
import copy
import datetime
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
from backend import persistence, sessions
from core_engine import Genome, savegame, snapshot

ROOT = Path(__file__).resolve().parent.parent
ALICE = {"X-ANE-User": "alice"}
BOB = {"X-ANE-User": "bob"}


def _dump(state):
    return json.dumps(snapshot.dump(state), sort_keys=True)


def _ids(state):
    return sessions.offered_choices(state)[1]


class _Saved(unittest.TestCase):
    """A sessions folder and a profile store of its own; the sessions held in memory as they were."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ane-saved-games-"))
        self.env = {"ANE_SESSIONS_DIR": str(self.dir / "sessions"), "ANE_SAVE_DIR": str(self.dir)}
        for patcher in (patch.dict(os.environ, self.env), patch.dict(sessions.SESSIONS),
                        patch.dict(sessions.SESSION_META), patch.dict(sessions.SESSION_RECORDS)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(api.api)

    def _start(self, owner="alice", profile="p1", seed=7):
        asking = sessions.SESSION_OWNER.set(owner)
        try:
            random.seed(seed)  # the game's dice are drawn from the global ones
            return sessions._start_game(Genome(rng=random.Random(seed)), profile, None, None)
        finally:
            sessions.SESSION_OWNER.reset(asking)

    def _turn(self, token, k=0):
        state = sessions.SESSIONS[token]
        ids = [i for i in _ids(state)  # a turn: not a side quest's answer, approaching or leaving someone
               if not i.startswith(("side-quest:", "approach:", "leave:", "look:"))]
        sessions.play_action(token, action_id=ids[k % len(ids)], state_version=state.state_version)
        return sessions.SESSIONS[token]

    def _turns(self, token, count, k=0):
        for n in range(count):
            if sessions.SESSIONS[token].ended:
                break
            self._turn(token, n * 3 + k)
        return sessions.SESSIONS[token]

    @staticmethod
    def _restart():
        """What a restart of the API leaves: nothing in memory, nothing cached; only the disk."""
        sessions.SESSIONS.clear()
        sessions.SESSION_META.clear()
        sessions.SESSION_RECORDS.clear()
        persistence._CONTEXTS.clear()
        persistence._STORED_CONTEXTS.clear()

    def _until(self, holds, what, seeds=range(8), turns=60):
        """A game (token, state) played until ``holds(state)``."""
        for seed in seeds:
            token, state = self._start(seed=seed, profile=f"search-{seed}")
            for n in range(turns):
                if holds(state):
                    return token, state
                if state.ended:
                    break
                state = self._turn(token, n * 3 + seed)
        self.fail(f"No game reached {what} in the shipped world.")

    def _continue_both(self, token, expected, turns=4):
        """Play the restored session and an uninterrupted copy the same way: the same story."""
        for n in range(turns):
            restored = sessions.SESSIONS[token]
            if restored.ended or expected.ended:
                break
            ids, expected_ids = _ids(restored), _ids(expected)
            self.assertEqual(ids, expected_ids)
            chosen = ids[(n * 5) % len(ids)]
            before = len(expected.history)
            expected.take_option(dict(zip(expected_ids, sessions.offered_choices(expected)[0]))[chosen])
            _, _, text = sessions.play_action(token, action_id=chosen)
            self.assertEqual(text, "\n\n".join(expected.history[before:]).strip())
        self.assertEqual(sessions.SESSIONS[token].rng.getstate(), expected.rng.getstate())


class RecoveryTests(_Saved):
    def test_save_and_resume_at_several_stages_of_a_story(self):
        token, state = self._start(seed=4)  # a story that runs past 24 turns (seed 3's mission is won at 11)
        reached = []
        for stage, total in (("the start", 0), ("early", 4), ("the middle", 12), ("late", 24)):
            state = self._turns(token, total - state.actions_taken)
            if state.ended:
                break
            expected = copy.deepcopy(state)
            row = persistence.session(token)
            self.assertEqual((row["turn"], row["state_version"], row["status"]),
                             (state.actions_taken, state.state_version, "active"), stage)
            self._restart()
            restored = sessions.open_session(token, "alice")
            self.assertEqual(_dump(restored), _dump(expected), stage)                    # exactly as it was
            self.assertEqual(restored.rng.getstate(), expected.rng.getstate())
            self.assertEqual(restored.history, expected.history)
            self.assertEqual(sessions.continue_game(token)["how"], "exact")
            self._continue_both(token, expected)
            state = sessions.SESSIONS[token]
            reached.append(stage)
        self.assertGreaterEqual(len(reached), 3, reached)

    def test_recovery_during_a_conversation(self):
        token, state = self._until(lambda s: s.conversation() and any(i.startswith("leave:") for i in _ids(s)),
                                   "a conversation")
        expected = copy.deepcopy(state)
        self._restart()
        restored = sessions.open_session(token, "alice")
        self.assertEqual(restored.conversation(), expected.conversation())
        self.assertEqual(_ids(restored), _ids(expected))
        self.assertEqual(_dump(restored), _dump(expected))
        leave = next(i for i in _ids(restored) if i.startswith("leave:"))
        sessions.play_action(token, action_id=leave)
        expected.take_option(dict(zip(_ids(expected), sessions.offered_choices(expected)[0]))[leave])
        self.assertIsNone(sessions.SESSIONS[token].conversation())
        self.assertEqual(sessions.SESSIONS[token].history, expected.history)

    def test_recovery_during_a_hostile_encounter(self):
        token, state = self._until(
            lambda s: s.active_event is not None and s.event_source_npc and any(
                char.name == s.event_source_npc and char.encounter_event for char in s.world.characters),
            "a hostile encounter")
        hostile = next(char for char in state.world.characters if char.name == state.event_source_npc)
        self.assertTrue(hostile.encounter_event)                                            # an ambush, not a chat
        expected = copy.deepcopy(state)
        self._restart()
        restored = sessions.open_session(token, "alice")
        self.assertEqual((restored.active_event.name, restored.event_source_npc),
                         (expected.active_event.name, expected.event_source_npc))
        self.assertEqual(restored.encounters, expected.encounters)
        self.assertEqual(_ids(restored), _ids(expected))   # the encounter's own choices, nothing else
        self._continue_both(token, expected, turns=2)

    def test_recovery_with_quests_and_npc_goals_under_way(self):
        token, state = self._until(lambda s: s.npc_goal_progress, "an NPC's goal under way")
        state = self._turns(token, 2)
        expected = copy.deepcopy(state)
        self._restart()
        restored = sessions.open_session(token, "alice")
        for field in ("npc_goal_progress", "mission_stage", "completed_goals", "allies", "met", "character_interactions",
                      "revealed_npcs", "removed_npcs", "path", "location_counts", "routes", "unlocked_locations",
                      "inventory", "coins", "variables"):
            self.assertEqual(getattr(restored, field, None), getattr(expected, field, None), field)
        self.assertEqual(restored.quest_log(), expected.quest_log())
        self.assertEqual(restored.people_met(), expected.people_met())
        self.assertEqual(restored.map_view(), expected.map_view())
        self._continue_both(token, expected)

    def test_a_session_let_go_from_memory_comes_back(self):
        with patch.object(sessions, "MAX_WEB_SESSIONS", 2):
            first, _ = self._start(seed=1)
            self._turns(first, 2)
            expected = copy.deepcopy(sessions.SESSIONS[first])
            self._start(seed=2)
            self._start(seed=3)
            self.assertNotIn(first, sessions.SESSIONS)                                  # let go...
            self.assertEqual(self.client.get(f"/v1/sessions/{first}", headers=ALICE).status_code, 200)
            self.assertEqual(_dump(sessions.SESSIONS[first]), _dump(expected))         # ...and back, exactly


class RestartOfTheApiTests(_Saved):
    """A real restart: one process plays and exits; another continues the game from disk."""

    CHILD = r'''
import copy, json, random, sys
from fastapi.testclient import TestClient
import api
from backend import sessions
from core_engine import Genome
client = TestClient(api.api)
H = {"X-ANE-User": "alice"}
if sys.argv[1] == "before":
    asking = sessions.SESSION_OWNER.set("alice")
    random.seed(5)
    token, _ = sessions._start_game(Genome(rng=random.Random(5)), "restart-profile", None, None)
    sessions.SESSION_OWNER.reset(asking)
    view = client.get(f"/v1/sessions/{token}", headers=H).json()
    turn = lambda view: next(c["id"] for c in view["choices"] if c["takes_turn"])  # noqa: E731
    for _ in range(6):
        view = client.post(f"/v1/sessions/{token}/actions", headers=H,
                           json={"action_id": turn(view), "state_version": view["state_version"]}).json()
    alone = copy.deepcopy(sessions.SESSIONS[token])  # how the game goes on without a restart (never saved)
    expected = []
    for _ in range(4):
        options, ids = sessions.offered_choices(alone)
        first = next(n for n, option in enumerate(options)  # a turn: as the API's takes_turn
                     if option.source not in ("side_quest", "approach", "leave"))
        before = len(alone.history)
        alone.take_option(options[first])
        expected.append([ids[first], alone.current.name, "\n\n".join(alone.history[before:]).strip()])
    print(json.dumps({"token": token, "version": view["state_version"], "scene": view["scene"], "expected": expected}))
else:
    token = sys.argv[2]
    listed = client.get("/v1/sessions", headers=H).json()["sessions"]
    continued = client.post(f"/v1/sessions/{token}/continue", headers=H).json()
    played = []
    view = continued
    for _ in range(4):
        chosen = next(c["id"] for c in view["choices"] if c["takes_turn"])
        view = client.post(f"/v1/sessions/{token}/actions", headers=H,
                           json={"action_id": chosen, "state_version": view["state_version"]}).json()
        played.append([chosen, view["location"], view["last_turn"]])
    print(json.dumps({"listed": listed, "continued": {k: continued[k] for k in ("restored", "state_version", "scene")},
                      "played": played, "bob": client.get(f"/v1/sessions/{token}", headers={"X-ANE-User": "bob"}).status_code}))
'''

    def _child(self, *args):
        env = {**os.environ, **self.env, "PYTHONPATH": str(ROOT),
               "ANE_RECORDS_DIR": str(self.dir / "records"), "ANE_ARTIFACT_DIR": str(self.dir / "artifacts")}
        script = self.dir / "child.py"
        script.write_text(self.CHILD, encoding="utf-8")
        out = subprocess.run([sys.executable, str(script), *args], cwd=ROOT, env=env, capture_output=True, text=True,
                             timeout=600)
        self.assertEqual(out.returncode, 0, out.stderr[-3000:])
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_a_game_goes_on_exactly_after_the_api_restarts(self):
        before = self._child("before")
        after = self._child("after", before["token"])
        mine = next(game for game in after["listed"] if game["session_id"] == before["token"])
        self.assertEqual((mine["status"], mine["in_memory"], mine["profile"], mine["turn"]),
                         ("active", False, "restart-profile", 6))
        self.assertEqual(after["continued"], {"restored": "exact", "state_version": before["version"],
                                              "scene": before["scene"]})
        self.assertEqual(after["played"], before["expected"])  # the same choices, places and story
        self.assertEqual(after["bob"], 404)


class AutosaveTests(_Saved):
    def _saved(self, token):
        return persistence.read_autosave(token)

    def test_every_change_that_matters_is_saved(self):
        token, state = self._start()
        row = persistence.session(token)
        self.assertEqual((row["owner"], row["profile"], row["status"], row["turn"]), ("alice", "p1", "active", 0))
        self.assertEqual(row["world_version"], state.context.world_version)
        self._turn(token)
        self.assertEqual(persistence.session(token)["turn"], 1)                           # a turn
        item = next(iter(state.world.objects)).name
        sessions.update_player(token, {"add_items": [item], "coins": 77})                  # the inventory, coins
        doc = self._saved(token)
        self.assertIn(item, doc["parameters"]["inventory"])
        self.assertEqual(doc["parameters"]["coins"], 77)
        npc = next(char.name for char in state.world.characters)
        sessions.sync_session(token, {"type": "npc_removed", "npc": npc})                   # the world
        self.assertIn(npc, json.dumps(self._saved(token)["snapshot"]["attributes"]["removed_npcs"]))
        self.assertEqual(persistence.session(token)["state_version"], sessions.SESSIONS[token].state_version)
        sessions.end_session(token)                                                         # the end
        row = persistence.session(token)
        self.assertEqual(row["status"], "ended")
        self.assertTrue(row["ended_at"])
        self.assertTrue(self._saved(token)["snapshot"]["attributes"]["ended"])

    def test_the_autosave_is_an_ordinary_save_game(self):
        token, _ = self._start()
        self._turns(token, 3)
        doc = self._saved(token)
        self.assertEqual(doc["format"], savegame.FORMAT)
        self.assertEqual(doc["checksum"], snapshot.checksum(doc["snapshot"]))
        loaded = self.client.post("/v1/sessions/load", json={"savegame": doc}, headers=ALICE)  # as a UNP's save
        self.assertEqual((loaded.status_code, loaded.json()["restored"]), (200, "exact"), loaded.text)

    def test_saving_never_interrupts_the_game(self):
        token, _ = self._start()
        with patch.object(persistence, "write_json", side_effect=OSError("disk full")):
            self._turn(token)                                                               # played all the same
        self.assertIn("disk full", persistence.session(token)["error"])
        self._turn(token)
        self.assertIsNone(persistence.session(token)["error"])

    def test_persistence_can_be_turned_off(self):
        with patch.dict(os.environ, {"ANE_SESSION_PERSISTENCE": "off"}):
            token, _ = self._start()
            self._turns(token, 2)
            self.assertFalse((self.dir / "sessions").exists())
            self.assertEqual(self.client.get("/v1/sessions", headers=ALICE).status_code, 400)
            self._restart()
            self.assertEqual(self.client.get(f"/v1/sessions/{token}", headers=ALICE).status_code, 404)


class BrokenSaveTests(_Saved):
    def _doc(self, token):
        return persistence.read_autosave(token)

    def _rewrite(self, token, doc):
        persistence.write_json(persistence._autosave_path(token), doc)

    def test_a_damaged_save_file_is_refused_not_played(self):
        token, _ = self._start()
        self._turns(token, 2)
        other, _ = self._start(seed=8)
        persistence._autosave_path(token).write_text("{not json", encoding="utf-8")
        self._restart()
        answer = self.client.get(f"/v1/sessions/{token}", headers=ALICE)
        self.assertEqual(answer.status_code, 409)
        self.assertEqual(answer.json()["detail"]["code"], "session_unrecoverable")
        self.assertEqual(self.client.get(f"/v1/sessions/{other}", headers=ALICE).status_code, 200)  # others play on

    def test_a_snapshot_that_does_not_match_its_checksum_is_rebuilt_from_its_parameters(self):
        token, state = self._start()
        self._turns(token, 3)
        version, location = state.state_version, sessions.SESSIONS[token].current.name
        doc = self._doc(token)
        doc["snapshot"]["attributes"]["coins"] = 999_999                                    # changed, checksum not
        self._rewrite(token, doc)
        self._restart()
        continued = self.client.post(f"/v1/sessions/{token}/continue", headers=ALICE).json()
        self.assertEqual(continued["restored"], "parameters")
        self.assertIn("checksum", continued["note"])
        self.assertNotEqual(continued["coins"], 999_999)
        self.assertEqual(continued["location"], location)
        self.assertGreater(continued["state_version"], version)                            # it moved on
        self.assertEqual(persistence.session(token)["state_version"], continued["state_version"])  # and is saved

    def test_a_snapshot_of_other_engine_code_is_rebuilt(self):
        token, _ = self._start()
        self._turns(token, 2)
        doc = self._doc(token)
        doc["snapshot"]["code_version"] = "0000000000000000"
        doc["checksum"] = snapshot.checksum(doc["snapshot"])
        self._rewrite(token, doc)
        self._restart()
        continued = self.client.post(f"/v1/sessions/{token}/continue", headers=ALICE).json()
        self.assertEqual(continued["restored"], "parameters")
        self.assertIn("engine code", continued["note"])

    def test_a_missing_or_changed_configuration_is_refused(self):
        token, _ = self._start()
        key = persistence.session(token)["context_key"]
        path = persistence._context_path(key)
        kept = json.loads(path.read_text(encoding="utf-8"))
        kept["config_yaml"] += "\nextra_setting: 1\n"
        persistence.write_json(path, kept)
        self._restart()
        self.assertEqual(self.client.get(f"/v1/sessions/{token}", headers=ALICE).status_code, 409)
        path.unlink()
        self._restart()
        answer = self.client.post(f"/v1/sessions/{token}/continue", headers=ALICE)
        self.assertEqual((answer.status_code, answer.json()["detail"]["code"]), (409, "session_unrecoverable"))

    def test_damaged_save_slots_and_documents(self):
        token, _ = self._start()
        saved = sessions.save_game(token, "alice", "camp")
        persistence._save_path(saved["save_id"]).write_text("[]", encoding="utf-8")
        answer = self.client.post(f"/v1/saves/{saved['save_id']}/restore", headers=ALICE, json={})
        self.assertEqual(answer.status_code, 409)
        with self.assertRaises(persistence.RestoreError):
            persistence.restore({"format": savegame.FORMAT, "parameters": {}}, persistence.session(token)["context_key"])
        self.assertEqual(self.client.post("/v1/sessions/load", json={"savegame": {"format": "nope"}}).status_code, 400)


class ConcurrencyTests(_Saved):
    @staticmethod
    def _together(work, count):
        start, errors = threading.Barrier(count), []

        def run(index):
            try:
                start.wait()
                work(index)
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        return errors

    def test_concurrent_autosaves_keep_the_newest_state_whole(self):
        token, state = self._start()
        versions = []
        for _ in range(6):
            state = self._turn(token)
            versions.append(copy.deepcopy(state))
        newest = max(v.state_version for v in versions)
        random.Random(1).shuffle(versions)
        meta = {**sessions.SESSION_META[token], "story_id": None}
        # Each writer with a lock of its own, as separate processes would be: only the index's write
        # transaction and the atomic file replacement keep them apart.
        with patch.object(persistence, "_session_lock", side_effect=lambda _: threading.RLock()):
            errors = self._together(lambda i: persistence.autosave(token, versions[i], meta), len(versions))
        self.assertEqual(errors, [])
        doc = persistence.read_autosave(token)
        self.assertEqual(doc["checksum"], snapshot.checksum(doc["snapshot"]))                # whole
        self.assertEqual(doc["snapshot"]["attributes"]["_state_version"], newest)          # the newest
        self.assertEqual(persistence.session(token)["state_version"], newest)
        leftovers = [p.name for p in persistence._autosave_path(token).parent.iterdir() if p.suffix == ".tmp"]
        self.assertEqual(leftovers, [])

    def test_an_older_state_never_replaces_a_newer_one(self):
        token, state = self._start()
        older = copy.deepcopy(self._turn(token))
        self._turns(token, 2)
        self.assertFalse(persistence.autosave(token, older, sessions.SESSION_META[token]))
        self.assertEqual(persistence.session(token)["state_version"], sessions.SESSIONS[token].state_version)

    def test_concurrent_saves_into_slots(self):
        token, state = self._start()
        self._turns(token, 2)
        errors = self._together(lambda i: sessions.save_game(token, "alice", "same slot"), 6)
        errors += self._together(lambda i: sessions.save_game(token, "alice", None), 5)
        self.assertEqual(errors, [])
        saves = sessions.list_saves("alice")
        self.assertEqual(sum(s["slot"] == "same slot" for s in saves), 1)                    # one slot, replaced
        manual = [s["slot"] for s in saves if s["kind"] == "manual"]
        self.assertEqual(len(manual), len(set(manual)))                                     # five new ones
        self.assertEqual(len(manual), 5)
        for save in saves:
            doc = persistence.read_save(save["save_id"])
            self.assertEqual(doc["checksum"], snapshot.checksum(doc["snapshot"]))


class OwnershipAndProfileTests(_Saved):
    def test_each_user_sees_and_opens_only_their_own(self):
        a1, _ = self._start("alice", "p1", seed=1)
        a2, _ = self._start("alice", "p2", seed=2)
        b1, _ = self._start("bob", "p1", seed=3)
        listed = lambda headers, **query: [g["session_id"] for g in  # noqa: E731
                                           self.client.get("/v1/sessions", headers=headers, params=query).json()["sessions"]]
        self.assertCountEqual(listed(ALICE), [a1, a2])
        self.assertEqual(listed(ALICE, profile="p2"), [a2])
        self.assertEqual(listed(BOB), [b1])
        for method, path in (("get", ""), ("get", "/journal"), ("post", "/continue"), ("post", "/restart"),
                             ("post", "/end"), ("get", "/savegame")):
            answer = getattr(self.client, method)(f"/v1/sessions/{a1}{path}", headers=BOB)
            self.assertEqual(answer.status_code, 404, (method, path))
        self.assertEqual(self.client.post(f"/v1/sessions/{a1}/actions", headers=BOB, json={"action": "x"}).status_code,
                         404)
        self.assertEqual(self.client.get(f"/v1/sessions/{a1}").status_code, 404)           # no user: not theirs
        self.assertEqual(self.client.get("/v1/sessions").status_code, 400)                  # listing needs one
        self.assertEqual(self.client.get(f"/v1/sessions/{a1}", headers={"X-ANE-User": "a@b.c"}).status_code, 400)
        self.assertFalse(sessions.SESSIONS[a1].ended)                                       # nothing happened to it

    def test_saves_are_private(self):
        token, _ = self._start("alice")
        mine = self.client.post(f"/v1/sessions/{token}/saves", headers=ALICE, json={"slot": "camp"}).json()
        self.assertEqual(self.client.get("/v1/saves", headers=BOB).json()["saves"], [])
        self.assertEqual(self.client.post(f"/v1/saves/{mine['save_id']}/restore", headers=BOB, json={}).status_code, 404)
        self.assertEqual(self.client.delete(f"/v1/saves/{mine['save_id']}", headers=BOB).status_code, 404)
        self.assertEqual(self.client.post(f"/v1/sessions/{token}/saves", headers=BOB, json={}).status_code, 404)
        bobs, _ = self._start("bob")
        theirs = self.client.post(f"/v1/sessions/{bobs}/saves", headers=BOB, json={"slot": "camp"}).json()
        self.assertNotEqual(theirs["save_id"], mine["save_id"])                             # the same name, apart
        self.assertEqual([s["save_id"] for s in self.client.get("/v1/saves", headers=ALICE).json()["saves"]],
                         [mine["save_id"]])

    def test_an_anonymous_game_is_open_to_whoever_has_its_id_as_before(self):
        token, _ = self._start(owner=None)
        self.assertEqual(self.client.get(f"/v1/sessions/{token}").status_code, 200)
        self.assertEqual(self.client.get(f"/v1/sessions/{token}", headers=BOB).status_code, 200)
        self._restart()
        self.assertEqual(self.client.get(f"/v1/sessions/{token}").status_code, 200)        # restored too
        self.assertEqual(self.client.post(f"/v1/sessions/{token}/saves", json={}).status_code, 400)  # a slot needs a user

    def test_profiles_keep_their_own_progression(self):
        p1, _ = self._start("alice", "first-profile", seed=1)
        p2, _ = self._start("alice", "second-profile", seed=2)
        sessions.end_session(p1)
        self._restart()
        self.assertEqual(sessions.progression("first-profile")["playthroughs"], 1)
        self.assertEqual(sessions.progression("second-profile")["playthroughs"], 0)
        self.assertEqual(sessions.open_session(p2, "alice").profile, "second-profile")
        self.assertEqual([g["status"] for g in sessions.list_games("alice", profile="first-profile")], ["ended"])
        self.assertEqual([g["status"] for g in sessions.list_games("alice", profile="second-profile")], ["active"])


class SlotsRestoreAndRestartTests(_Saved):
    def test_named_and_manual_saves_restore_as_new_games(self):
        token, state = self._start()
        self._turns(token, 3)
        expected = copy.deepcopy(sessions.SESSIONS[token])
        named = self.client.post(f"/v1/sessions/{token}/saves", headers=ALICE, json={"slot": "  before   the gate "})
        self.assertEqual((named.status_code, named.json()["slot"], named.json()["kind"]), (200, "before the gate", "named"))
        manual = self.client.post(f"/v1/sessions/{token}/saves", headers=ALICE, json={}).json()
        self.assertEqual(manual["kind"], "manual")
        self._turns(token, 3)                                                               # the game goes on
        again = self.client.post(f"/v1/sessions/{token}/saves", headers=ALICE, json={"slot": "before the gate"}).json()
        self.assertEqual(again["save_id"], named.json()["save_id"])                         # the slot is replaced
        self.assertGreater(again["turn"], named.json()["turn"])
        self._restart()
        restored = self.client.post(f"/v1/saves/{manual['save_id']}/restore", headers=ALICE, json={}).json()
        self.assertEqual(restored["restored"], "exact")
        self.assertNotEqual(restored["session_id"], token)
        game = sessions.SESSIONS[restored["session_id"]]
        self.assertEqual(game.history, expected.history)
        self.assertEqual(game.rng.getstate(), expected.rng.getstate())
        self.assertIn(restored["session_id"], [g["session_id"] for g in sessions.list_games("alice")])
        self.assertEqual(self.client.delete(f"/v1/saves/{manual['save_id']}", headers=ALICE).status_code, 200)
        self.assertEqual(self.client.post(f"/v1/saves/{manual['save_id']}/restore", headers=ALICE, json={}).status_code,
                         404)
        self.assertEqual(self.client.post(f"/v1/sessions/{token}/saves", headers=ALICE,
                                          json={"slot": "x" * 65}).status_code, 422)

    def test_a_save_of_an_ended_game_does_not_restore(self):
        token, _ = self._start()
        sessions.end_session(token)
        saved = sessions.save_game(token, "alice", "the end")
        answer = self.client.post(f"/v1/saves/{saved['save_id']}/restore", headers=ALICE, json={})
        self.assertEqual(answer.status_code, 400)

    def test_restart_starts_the_same_adventure_again(self):
        token, state = self._start(profile="restarter")
        self._turns(token, 4)
        mission = state.mission.name if state.mission else None
        restarted = self.client.post(f"/v1/sessions/{token}/restart", headers=ALICE)
        self.assertEqual(restarted.status_code, 200, restarted.text)
        new = restarted.json()["session_id"]
        game = sessions.SESSIONS[new]
        self.assertNotEqual(new, token)
        self.assertEqual((game.profile, game.actions_taken, game.context.world_version, game.genome.traits()),
                         ("restarter", 0, state.context.world_version, state.genome.traits()))
        self.assertEqual(persistence.session(token)["status"], "abandoned")
        self.assertEqual(persistence.session(new)["owner"], "alice")
        # Started with what was asked first (here no quest chosen: one is drawn again), not what was drawn.
        self.assertEqual(sessions.SESSION_META[new]["parameters"], sessions.SESSION_META[token]["parameters"])
        self.assertIsNone(sessions.SESSION_META[new]["parameters"]["mission"])
        self.assertIsNotNone(mission)
        self._turn(token)                                                                  # still playable...
        self.assertEqual(persistence.session(token)["status"], "abandoned")                # ...and still set aside


class RetentionTests(_Saved):
    def _age(self, token, days):
        stamp = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()
        with sqlite3.connect(persistence.directory() / "index.sqlite3") as connection:
            connection.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (stamp, token))

    def test_old_and_surplus_finished_games_are_removed_and_nothing_else(self):
        old, _ = self._start(seed=1)
        recent = [self._start(seed=seed)[0] for seed in (2, 3, 4)]
        active, _ = self._start(seed=5)
        for token in (old, *recent):
            sessions.end_session(token)
        self._age(old, 45)
        for days, token in enumerate(reversed(recent)):
            self._age(token, days + 1)
        kept_save = sessions.save_game(old, "alice", "keep me")
        with patch.dict(os.environ, {"ANE_SESSION_RETENTION_DAYS": "30", "ANE_SESSION_KEEP_FINISHED": "2"}):
            dry = self.client.post("/v1/sessions/cleanup", json={"dry_run": True}).json()
            self.assertCountEqual(dry["removed"], [old, recent[0]])                        # too old; beyond two
            self.assertIsNotNone(persistence.session(old))                                 # a dry run removes nothing
            done = self.client.post("/v1/sessions/cleanup", json={}).json()
        self.assertCountEqual(done["removed"], [old, recent[0]])
        self.assertEqual(done["policy"], {"retention_days": 30, "keep_finished": 2})
        self.assertIsNone(persistence.session(old))
        self.assertFalse(persistence._autosave_path(old).parent.exists())
        self.assertIsNotNone(persistence.session(active))                                  # active: kept
        self.assertEqual([s["slot"] for s in sessions.list_saves("alice")], ["keep me"])   # saves: kept
        self.assertEqual(sessions.list_saves("alice")[0]["save_id"], kept_save["save_id"])
        self._restart()
        self.assertEqual(self.client.get(f"/v1/sessions/{old}", headers=ALICE).status_code, 404)
        self.assertEqual(self.client.get(f"/v1/sessions/{active}", headers=ALICE).status_code, 200)


class StudioTests(_Saved):
    def test_opening_a_saved_game_is_a_normal_action(self):
        import app

        token, _ = self._start(owner="test", profile="studio-saves")                        # the studio's user
        self._turns(token, 2)
        status = app.save_to_slot_ui(token, "my slot")
        self.assertIn("my slot", status)
        self._restart()
        choices = dict((value, label) for label, value in app.saved_games_choices("studio-saves"))
        self.assertIn(f"session:{token}", choices)
        save = next(value for value in choices if value.startswith("save:"))
        opened = app.open_saved_game_ui(f"session:{token}")
        self.assertEqual((opened[0], opened[-1]), (token, "Continued exactly where it was saved."))
        restored = app.open_saved_game_ui(save)
        self.assertNotEqual(restored[0], token)
        again = app.restart_saved_game_ui(f"session:{token}", token)
        self.assertNotIn(again[0], (token, restored[0]))
        self.assertIn("Deleted", app.delete_saved_game_ui(save))
        self.assertIn("Removed", app.cleanup_games_ui())
        self.assertEqual(app.saved_games_choices("nobody-plays-this"), [])


if __name__ == "__main__":
    unittest.main()
