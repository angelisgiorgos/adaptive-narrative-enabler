"""Persistent play sessions: autosave, a durable session index, save slots, recovery and retention.

A play session (backend/sessions.py) lives in memory while it is played; this module keeps it on
disk, so it survives a restart of the API and can be continued, restored from a save slot or
restarted. Everything is in the sessions folder::

    <ANE_SESSIONS_DIR, default <ANE_SAVE_DIR or saves/>/sessions>
      index.sqlite3                       the session index and the save slots (tables below)
      sessions/<session id>/autosave.json the session's latest state (ane.savegame/1)
      saves/<save id>.json                named and manual saves (ane.savegame/1)
      contexts/<context key>.json         the configuration (world and tuning) and tag vectors a
                                          session runs with (ane.session_context/1), once per engine

Tables:
    sessions  one row per play session: owner, profile, origin, engine, world version, status
              (active | ended | abandoned), place, turn, state version, mission, its save's
              revision, its start parameters (for restart), and when it started, last changed, ended
    saves     save slots: owner, slot name (unique per owner), kind (named | manual), the session
              saved and where it stood

A save game is the existing ``ane.savegame/1`` document (core_engine/savegame.py): its parameters
and an exact snapshot (core_engine/snapshot.py) with the dice, the quest, the people, the map, the
event in progress and the whole story. A session is restored exactly under its own configuration
(the stored context), or rebuilt from its parameters when the snapshot no longer fits (other
engine code).

Writes are atomic (a temporary file in the same folder, flushed, then renamed over the old one)
and serialised: one lock per session in this process, and the index's write transaction
(``BEGIN IMMEDIATE``) across processes. An autosave never replaces a newer state with an older
one (``state_version``). Saving never interrupts a story: a failed write is logged and reported
in the index (``error``).

Settings (environment): ``ANE_SESSION_PERSISTENCE`` (on; off keeps sessions in memory only, as
before), ``ANE_SESSIONS_DIR``, ``ANE_SESSION_RETENTION_DAYS`` (30: finished and abandoned sessions
older than this are removed), ``ANE_SESSION_KEEP_FINISHED`` (20: the most finished or abandoned
sessions kept per owner). Active sessions and save slots are never removed by the retention policy.
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import uuid
from pathlib import Path

import yaml

LOG = logging.getLogger("ane.persistence")
CONTEXT_FORMAT = "ane.session_context/1"
STATUSES = ("active", "ended", "abandoned")
FINISHED = ("ended", "abandoned")
DEFAULT_RETENTION_DAYS = 30
DEFAULT_KEEP_FINISHED = 20
CLEANUP_EVERY = datetime.timedelta(hours=1)  # the retention policy runs at most this often by itself
SLOT_CHARACTERS = 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY, owner TEXT, profile TEXT, origin TEXT, engine_id TEXT, run_id TEXT,
    story_id TEXT, world_version TEXT, context_key TEXT, code_version TEXT,
    status TEXT NOT NULL, location TEXT, turn INTEGER, state_version INTEGER, mission TEXT, won INTEGER,
    revision INTEGER NOT NULL DEFAULT 0, parameters_json TEXT, error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, ended_at TEXT
);
CREATE INDEX IF NOT EXISTS sessions_by_owner ON sessions (owner, updated_at);
CREATE TABLE IF NOT EXISTS saves (
    save_id TEXT PRIMARY KEY, owner TEXT NOT NULL, slot TEXT NOT NULL, kind TEXT NOT NULL,
    session_id TEXT, profile TEXT, engine_id TEXT, world_version TEXT, context_key TEXT,
    location TEXT, turn INTEGER, state_version INTEGER, mission TEXT, session_status TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (owner, slot)
);
"""

_LOCKS_GUARD = threading.Lock()
_SESSION_LOCKS = {}
_CONTEXTS = {}          # context key -> EngineContext (without a storyteller), read once
_STORED_CONTEXTS = set()
_LAST_CLEANUP = {}


class PersistenceError(Exception):
    """A saved session or save slot that cannot be used (missing, damaged, or not restorable)."""


class RestoreError(PersistenceError):
    """A saved session whose state cannot be restored here."""


# ---- settings and paths ---------------------------------------------------------------------------

def enabled() -> bool:
    return os.environ.get("ANE_SESSION_PERSISTENCE", "on").strip().lower() not in ("off", "0", "false", "no")


def directory() -> Path:
    if os.environ.get("ANE_SESSIONS_DIR"):
        return Path(os.environ["ANE_SESSIONS_DIR"])
    base = os.environ.get("ANE_SAVE_DIR") or Path(__file__).resolve().parent.parent / "saves"
    return Path(base) / "sessions"


def _number(name, default):
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        LOG.warning("%s must be a whole number; using %s.", name, default)
        return default


def policy() -> dict:
    """The retention policy now: {retention_days, keep_finished}."""
    return {"retention_days": _number("ANE_SESSION_RETENTION_DAYS", DEFAULT_RETENTION_DAYS),
            "keep_finished": _number("ANE_SESSION_KEEP_FINISHED", DEFAULT_KEEP_FINISHED)}


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")


def _session_lock(session_id):
    with _LOCKS_GUARD:
        return _SESSION_LOCKS.setdefault(session_id, threading.RLock())


def _safe_id(value, what):
    """An id used in a file name: hexadecimal and dashes only (ids are made here: uuid4)."""
    text = str(value or "")
    if not text or len(text) > 64 or any(c not in "0123456789abcdefABCDEF-" for c in text):
        raise PersistenceError(f"Not a valid {what} id.")
    return text


# ---- the index ------------------------------------------------------------------------------------

@contextlib.contextmanager
def _connect():
    folder = directory()
    folder.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(folder / "index.sqlite3", timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.executescript(SCHEMA)
        yield connection
    finally:
        connection.close()


@contextlib.contextmanager
def _transaction(connection):
    """A write transaction: one writer at a time, across processes."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def _row(row):
    if row is None:
        return None
    data = dict(row)
    if "parameters_json" in data:
        data["parameters"] = json.loads(data.pop("parameters_json") or "{}")
    if "won" in data:
        data["won"] = bool(data["won"]) if data["won"] is not None else None
    return data


# ---- files ----------------------------------------------------------------------------------------

def write_json(path, data):
    """Write ``data`` to ``path`` atomically: a reader sees the old file or the new one, never part."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    with contextlib.suppress(OSError):  # the rename itself, durable (POSIX)
        folder = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)


def _read_json(path, what):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PersistenceError(f"The {what} is missing.") from exc
    except (OSError, ValueError) as exc:
        raise PersistenceError(f"The {what} cannot be read: {exc}") from exc
    if not isinstance(data, dict):
        raise PersistenceError(f"The {what} is not a JSON object.")
    return data


def _autosave_path(session_id):
    return directory() / "sessions" / _safe_id(session_id, "session") / "autosave.json"


def _save_path(save_id):
    return directory() / "saves" / f"{_safe_id(save_id, 'save')}.json"


def _context_path(key):
    return directory() / "contexts" / f"{_safe_id(key, 'context')}.json"


# ---- engine contexts ------------------------------------------------------------------------------

def _tag_vectors(context):
    vectors = getattr(context, "tag_vectors", None)
    return dict(vectors) if vectors else {}


def context_key(context):
    """The stored context's key: its world version and its tag vectors."""
    vectors = _tag_vectors(context)
    tags = hashlib.sha256(json.dumps(vectors, sort_keys=True, default=str).encode("utf-8")).hexdigest() if vectors else ""
    return hashlib.sha256(f"{context.world_version}|{tags}".encode("utf-8")).hexdigest()[:32]


def store_context(context):
    """Keep the configuration and tag vectors a session runs with (once per key); returns the key."""
    key = context_key(context)
    if key in _STORED_CONTEXTS and _context_path(key).exists():
        return key
    path = _context_path(key)
    if not path.exists():
        write_json(path, {"format": CONTEXT_FORMAT, "world_version": context.world_version,
                          "source": getattr(context, "source", "") or "",
                          "config_yaml": yaml.safe_dump(context.as_dict(), sort_keys=False, allow_unicode=True),
                          "tag_vectors": _tag_vectors(context)})
    _STORED_CONTEXTS.add(key)
    return key


def load_context(key, genome_traits=None):
    """The EngineContext stored under ``key`` (with the storyteller ``genome_traits``)."""
    from core_engine.context import EngineContext

    if key not in _CONTEXTS:
        if not key:
            raise RestoreError("The session's configuration was not kept, so it cannot be restored.")
        data = _read_json(_context_path(key), "session's configuration")
        if data.get("format") != CONTEXT_FORMAT:
            raise RestoreError("The session's configuration is not an ane.session_context/1 document.")
        try:
            config_data = yaml.safe_load(data.get("config_yaml") or "") or {}
        except yaml.YAMLError as exc:
            raise RestoreError(f"The session's configuration cannot be read: {exc}") from exc
        context = EngineContext(config_data, tag_vectors=data.get("tag_vectors") or None,
                                source=data.get("source") or "a saved session")
        if data.get("world_version") and context.world_version != data["world_version"]:
            raise RestoreError("The session's configuration was changed or damaged.")
        _CONTEXTS[key] = context
        while len(_CONTEXTS) > 16:
            _CONTEXTS.pop(next(iter(_CONTEXTS)))
    context = _CONTEXTS[key]
    return context.with_genome(genome_traits) if genome_traits else context


# ---- autosave -------------------------------------------------------------------------------------

def _describe(state):
    return {
        "location": state.current.name if getattr(state, "current", None) else None,
        "turn": int(getattr(state, "actions_taken", 0) or 0),
        "state_version": int(state.state_version),
        "mission": state.mission.title if getattr(state, "mission", None) else None,
        "won": int(bool(state.won)),
    }


def _savegame(state, meta, key):
    from core_engine import savegame

    return savegame.make(state, {"package": "studio", "engine_id": meta.get("engine_id"), "context_key": key,
                                 "origin": meta.get("origin")})


def autosave(session_id, state, meta=None):
    """Save the session's state now (after every change that matters: a turn, approaching or leaving
    someone, the player or the world set from outside, the end). ``meta``: {owner, origin,
    engine_id, run_id, story_id, parameters}. Returns True when saved; False when a newer state is
    saved already, persistence is off, or the write failed (logged, and noted in the index)."""
    if not enabled():
        return False
    meta = dict(meta or {})
    with _session_lock(session_id):
        try:
            key = store_context(state.context)
            doc = _savegame(state, meta, key)
            described = _describe(state)
            status = "ended" if state.ended else (meta.get("status") or "active")
            now = _now()
            with _connect() as connection, _transaction(connection):
                row = connection.execute("SELECT state_version, revision, status, created_at, ended_at FROM sessions "
                                         "WHERE session_id = ?", (session_id,)).fetchone()
                if row is not None and (row["state_version"] or 0) > described["state_version"]:
                    LOG.info("Session %s: a newer state (%s) is saved already; %s is not.", session_id,
                             row["state_version"], described["state_version"])
                    return False
                if row is not None and row["status"] == "abandoned" and status == "active":
                    status = "abandoned"  # restarted: it stays set aside
                write_json(_autosave_path(session_id), doc)
                ended_at = (row["ended_at"] if row is not None else None) or (now if status in FINISHED else None)
                connection.execute(
                    "INSERT INTO sessions (session_id, owner, profile, origin, engine_id, run_id, story_id, "
                    "world_version, context_key, code_version, status, location, turn, state_version, mission, won, "
                    "revision, parameters_json, error, created_at, updated_at, ended_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, NULL, ?, ?, ?) "
                    "ON CONFLICT (session_id) DO UPDATE SET profile = excluded.profile, story_id = "
                    "COALESCE(excluded.story_id, sessions.story_id), world_version = excluded.world_version, "
                    "context_key = excluded.context_key, code_version = excluded.code_version, status = excluded.status, "
                    "location = excluded.location, turn = excluded.turn, state_version = excluded.state_version, "
                    "mission = excluded.mission, won = excluded.won, revision = sessions.revision + 1, error = NULL, "
                    "updated_at = excluded.updated_at, ended_at = excluded.ended_at",
                    (session_id, meta.get("owner"), state.profile, meta.get("origin"), meta.get("engine_id"),
                     meta.get("run_id"), meta.get("story_id"), state.context.world_version, key,
                     doc["engine"]["code_version"], status, described["location"], described["turn"],
                     described["state_version"], described["mission"], described["won"],
                     json.dumps(meta.get("parameters") or {}, default=str), row["created_at"] if row else now, now,
                     ended_at))
        except Exception as exc:  # noqa: BLE001 - saving never interrupts a story
            LOG.warning("Session %s could not be saved: %s", session_id, exc)
            with contextlib.suppress(Exception):
                with _connect() as connection, _transaction(connection):
                    connection.execute("UPDATE sessions SET error = ? WHERE session_id = ?",
                                       (f"{type(exc).__name__}: {exc}"[:500], session_id))
            return False
    if status in FINISHED:
        maybe_cleanup()
    return True


def session(session_id):
    """The index row of a session, or None."""
    if not enabled():
        return None
    with _connect() as connection:
        return _row(connection.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone())


def list_sessions(owner, profile=None, status=None, limit=100):
    """An owner's sessions, the most recently changed first."""
    query, args = "SELECT * FROM sessions WHERE owner = ?", [owner]
    if profile:
        query, args = query + " AND profile = ?", args + [profile]
    if status:
        query, args = query + " AND status = ?", args + [status]
    with _connect() as connection:
        rows = connection.execute(query + " ORDER BY updated_at DESC LIMIT ?", (*args, int(limit))).fetchall()
    return [_row(row) for row in rows]


def set_status(session_id, status):
    if status not in STATUSES:
        raise ValueError(f"Unknown session status `{status}`.")
    with _connect() as connection, _transaction(connection):
        connection.execute("UPDATE sessions SET status = ?, updated_at = ?, ended_at = COALESCE(ended_at, ?) "
                           "WHERE session_id = ?", (status, _now(), _now() if status in FINISHED else None, session_id))


def read_autosave(session_id):
    return _read_json(_autosave_path(session_id), "session's save")


def restore(doc, key, profile=None, store=None):
    """(state, how, note): the save game ``doc`` played on under the stored context ``key``: exactly,
    or rebuilt from its parameters when its snapshot no longer fits (another engine code)."""
    from core_engine import savegame
    from core_engine.genome import Genome

    traits = (doc.get("snapshot") or {}).get("genome")
    if not traits:
        raise RestoreError("The save has no storyteller (no snapshot), so it cannot be restored here.")
    context = load_context(key, traits)
    try:
        with context.bind():
            return savegame.load(doc, Genome.from_traits(traits), store=store, profile=profile or None)
    except (savegame.SaveGameError, ValueError) as exc:
        raise RestoreError(f"The save cannot be restored: {exc}") from exc


# ---- save slots -----------------------------------------------------------------------------------

def slot_name(slot):
    text = " ".join(str(slot or "").split())
    if not text:
        return None
    if len(text) > SLOT_CHARACTERS:
        raise PersistenceError(f"A save slot's name is at most {SLOT_CHARACTERS} characters.")
    return text


def save_slot(owner, session_id, state, slot=None, meta=None):
    """Save the session into the owner's slot ``slot`` (replacing what that slot held), or, without a
    name, into a new manual save. Returns the slot's row."""
    if not owner:
        raise PersistenceError("Saving to a slot needs a user (X-ANE-User, or the studio).")
    meta = dict(meta or {})
    name = slot_name(slot)
    kind = "named" if name else "manual"
    with _session_lock(session_id):
        key = store_context(state.context)
        doc = _savegame(state, meta, key)
        described = _describe(state)
        now = _now()
        with _connect() as connection, _transaction(connection):
            if name is None:
                count = connection.execute("SELECT COUNT(*) FROM saves WHERE owner = ? AND kind = 'manual'",
                                           (owner,)).fetchone()[0]
                name = f"Manual save {count + 1} ({now[:16].replace('T', ' ')})"
            row = connection.execute("SELECT save_id, created_at FROM saves WHERE owner = ? AND slot = ?",
                                     (owner, name)).fetchone()
            save_id = row["save_id"] if row else uuid.uuid4().hex
            write_json(_save_path(save_id), doc)
            connection.execute(
                "INSERT OR REPLACE INTO saves (save_id, owner, slot, kind, session_id, profile, engine_id, world_version, "
                "context_key, location, turn, state_version, mission, session_status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (save_id, owner, name, kind, session_id, state.profile, meta.get("engine_id"),
                 state.context.world_version, key, described["location"], described["turn"],
                 described["state_version"], described["mission"], "ended" if state.ended else "active",
                 row["created_at"] if row else now, now))
            return _row(connection.execute("SELECT * FROM saves WHERE save_id = ?", (save_id,)).fetchone())


def list_saves(owner, profile=None, limit=200):
    query, args = "SELECT * FROM saves WHERE owner = ?", [owner]
    if profile:
        query, args = query + " AND profile = ?", args + [profile]
    with _connect() as connection:
        return [_row(row) for row in connection.execute(query + " ORDER BY updated_at DESC LIMIT ?",
                                                        (*args, int(limit))).fetchall()]


def save_row(save_id, owner):
    """The owner's save slot ``save_id``; LookupError when there is none (or it is someone else's)."""
    with _connect() as connection:
        row = connection.execute("SELECT * FROM saves WHERE save_id = ?", (str(save_id),)).fetchone()
    if row is None or row["owner"] != owner:
        raise LookupError("Save not found.")
    return _row(row)


def read_save(save_id):
    return _read_json(_save_path(save_id), "save")


def delete_save(save_id, owner):
    row = save_row(save_id, owner)
    with _connect() as connection, _transaction(connection):
        connection.execute("DELETE FROM saves WHERE save_id = ? AND owner = ?", (row["save_id"], owner))
    with contextlib.suppress(FileNotFoundError):
        _save_path(row["save_id"]).unlink()
    return row


# ---- retention --------------------------------------------------------------------------------------

def cleanup(retention_days=None, keep_finished=None, dry_run=False, now=None):
    """Apply the retention policy: remove finished (ended or abandoned) sessions changed more than
    ``retention_days`` ago, and those beyond the ``keep_finished`` most recent of each owner; then
    the stored configurations nothing uses any more. Active sessions and save slots are kept.
    Returns {policy, removed: [session ids], contexts_removed, dry_run}."""
    settings = policy()
    days = settings["retention_days"] if retention_days is None else max(0, int(retention_days))
    keep = settings["keep_finished"] if keep_finished is None else max(0, int(keep_finished))
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = (now - datetime.timedelta(days=days)).isoformat(timespec="microseconds")
    removed, contexts = [], []
    if not enabled():
        return {"policy": {"retention_days": days, "keep_finished": keep}, "removed": [], "contexts_removed": 0,
                "dry_run": bool(dry_run)}
    with _connect() as connection, _transaction(connection):
        finished = connection.execute(
            f"SELECT session_id, owner, updated_at FROM sessions WHERE status IN ({','.join('?' * len(FINISHED))}) "
            "ORDER BY owner, updated_at DESC", FINISHED).fetchall()
        kept_per_owner = {}
        for row in finished:
            rank = kept_per_owner.get(row["owner"], 0)
            kept_per_owner[row["owner"]] = rank + 1
            if row["updated_at"] < cutoff or rank >= keep:
                removed.append(row["session_id"])
        if not dry_run:
            connection.executemany("DELETE FROM sessions WHERE session_id = ?", [(sid,) for sid in removed])
        used = {row[0] for row in connection.execute(
            "SELECT context_key FROM sessions UNION SELECT context_key FROM saves").fetchall()}
        folder = directory() / "contexts"
        settled = (now - CLEANUP_EVERY).timestamp()  # one being written for a new session is not removed
        if folder.exists():
            contexts = [path for path in folder.glob("*.json")
                        if path.stem not in used and path.stat().st_mtime < settled]
    if not dry_run:
        for session_id in removed:
            shutil.rmtree(directory() / "sessions" / session_id, ignore_errors=True)
            with _LOCKS_GUARD:
                _SESSION_LOCKS.pop(session_id, None)
        for path in contexts:
            with contextlib.suppress(OSError):
                path.unlink()
            _STORED_CONTEXTS.discard(path.stem)
            _CONTEXTS.pop(path.stem, None)
    _LAST_CLEANUP[str(directory())] = now
    return {"policy": {"retention_days": days, "keep_finished": keep}, "removed": removed,
            "contexts_removed": len(contexts), "dry_run": bool(dry_run)}


def maybe_cleanup():
    """The retention policy, at most once an hour (when a session finishes)."""
    last = _LAST_CLEANUP.get(str(directory()))
    now = datetime.datetime.now(datetime.timezone.utc)
    if last is not None and now - last < CLEANUP_EVERY:
        return None
    try:
        return cleanup(now=now)
    except Exception as exc:  # noqa: BLE001 - retention never interrupts play
        LOG.warning("The session retention policy could not run: %s", exc)
        return None
