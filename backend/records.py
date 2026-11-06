"""The records database: every generated story, run, world and engine, kept.

Everything the studio (app.py), the REST API (api.py) and the terminal
(main.py) generate is recorded in one SQLite database, so worlds and trained
engines can be found and used again::

    /data/angelisg/ane/            (ANE_RECORDS_DIR; ANE_RECORDS=off turns recording off)
      ane.sqlite3                  the database (tables below)
      models/<engine id>.ane.zip   engine model files (core_engine/model.py)

Tables:
    worlds      every configuration a story or run used: world and all tuning (config_json),
                identified by its content, so the same world is stored once
    runs        evolution runs: where they came from, every setting, the outcome, the
                learning summary, and the engine they continued from
    generations each generation of a run: scores, diversity, mutation step, benchmark,
                and the champion's traits
    engines     storytellers: a run's best (with a model file), every generation's champion,
                loaded, continued and retrained engines; parent_id links the lineage
    stories     every generated story: preview, played session or terminal story, with its
                parameters (seed, profile, start, mission, stats), state, result, fitness
                and transcript
    turns       every turn of a story: the action, its check, success and outcome, the
                place before and after, health, coins, and the turn's text
    artifacts   files written for a run or engine (configuration, bundle, offline export, ...)

Recording never interrupts a story: a failed write is logged and skipped.
Reading (list_* / get_* / load_*) raises errors normally.
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import logging
import os
import sqlite3
import threading
import uuid
from pathlib import Path

from utils.config_loader import config

LOG = logging.getLogger("ane.records")
DEFAULT_DIR = "/data/angelisg/ane"
SCHEMA_VERSION = 1
_LOCK = threading.RLock()
_DISABLED_REASON = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS worlds (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, config_json TEXT NOT NULL,
    locations INTEGER, npcs INTEGER, items INTEGER, events INTEGER, missions INTEGER, stats_json TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, finished_at TEXT, origin TEXT NOT NULL, kind TEXT NOT NULL,
    status TEXT NOT NULL, world_id TEXT REFERENCES worlds(id), parent_engine_id TEXT, seed INTEGER,
    settings_json TEXT, parameters_json TEXT, best_score REAL, best_engine_id TEXT, generations_run INTEGER,
    early_stopped INTEGER, warm_started INTEGER, learning_json TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS generations (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE, generation INTEGER NOT NULL,
    best REAL, mean REAL, worst REAL, spread REAL, diversity REAL, mutation_step REAL, validation REAL,
    improved INTEGER, champion_engine_id TEXT, best_traits_json TEXT, PRIMARY KEY (run_id, generation)
);
CREATE TABLE IF NOT EXISTS engines (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, run_id TEXT REFERENCES runs(id), world_id TEXT REFERENCES worlds(id),
    parent_id TEXT, origin TEXT, source TEXT NOT NULL, generation INTEGER, score REAL, genome_json TEXT NOT NULL,
    model_path TEXT, model_sha256 TEXT, resumable INTEGER DEFAULT 0, trained_generations INTEGER
);
CREATE TABLE IF NOT EXISTS stories (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, origin TEXT NOT NULL, kind TEXT NOT NULL,
    run_id TEXT REFERENCES runs(id), engine_id TEXT REFERENCES engines(id), world_id TEXT REFERENCES worlds(id),
    session_id TEXT, seed INTEGER, profile TEXT, start_location TEXT, mission TEXT, mission_status TEXT,
    parameters_json TEXT, stats_json TEXT, status TEXT NOT NULL, turns INTEGER DEFAULT 0, end_kind TEXT,
    end_reason TEXT, won INTEGER, health INTEGER, coins INTEGER, path_json TEXT, inventory_json TEXT,
    variables_json TEXT, events_json TEXT, fitness REAL, fitness_json TEXT, transcript TEXT
);
CREATE TABLE IF NOT EXISTS turns (
    story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE, turn INTEGER NOT NULL, recorded_at TEXT NOT NULL,
    action TEXT, source TEXT, owner TEXT, location_before TEXT, location_after TEXT, event TEXT, event_after TEXT,
    check_json TEXT, succeeded INTEGER, outcome TEXT, health INTEGER, coins INTEGER, ended INTEGER, end_kind TEXT,
    text TEXT, PRIMARY KEY (story_id, turn)
);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL, path TEXT NOT NULL,
    run_id TEXT, engine_id TEXT, world_id TEXT, sha256 TEXT, size INTEGER
);
CREATE INDEX IF NOT EXISTS stories_by_run ON stories(run_id);
CREATE INDEX IF NOT EXISTS stories_by_engine ON stories(engine_id);
CREATE INDEX IF NOT EXISTS stories_by_session ON stories(session_id);
CREATE INDEX IF NOT EXISTS engines_by_run ON engines(run_id);
CREATE INDEX IF NOT EXISTS runs_by_created ON runs(created_at);
"""


# ---- where and whether --------------------------------------------------------------

def directory() -> Path:
    return Path(os.environ.get("ANE_RECORDS_DIR") or DEFAULT_DIR)


def database_path() -> Path:
    return directory() / "ane.sqlite3"


def models_dir() -> Path:
    return directory() / "models"


def exports_dir() -> Path:
    return directory() / "exports"


def enabled() -> bool:
    """Whether recording is on and the records directory is usable."""
    if os.environ.get("ANE_RECORDS", "on").strip().lower() in ("0", "off", "false", "no"):
        return False
    root = directory()
    if str(root) in _DISABLED_REASON:
        return False
    try:
        root.mkdir(parents=True, exist_ok=True)
        models_dir().mkdir(parents=True, exist_ok=True)
        if not os.access(root, os.W_OK):
            raise PermissionError(f"{root} is not writable")
    except OSError as exc:
        _DISABLED_REASON[str(root)] = str(exc)
        LOG.warning("Recording is off: the records directory %s cannot be used (%s).", root, exc)
        return False
    return True


@contextlib.contextmanager
def connect():
    """A connection to the records database (schema created on first use)."""
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(SCHEMA)
        connection.execute("INSERT OR IGNORE INTO schema_info VALUES ('version', ?)", (str(SCHEMA_VERSION),))
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _safe(function):
    """Recording is best effort: an error is logged, never raised into the story."""
    def wrapper(*args, **kwargs):
        if not enabled():
            return None
        try:
            with _LOCK:
                return function(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - recording must not break play
            LOG.warning("Recording %s failed: %s", function.__name__, exc)
            return None
    wrapper.__name__ = function.__name__
    wrapper.__doc__ = function.__doc__
    return wrapper


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _json(value):
    return None if value is None else json.dumps(value, default=str, ensure_ascii=False)


def _relative(path):
    """Paths inside the records folder are stored relative to it, so the folder can move."""
    path = Path(path)
    try:
        return str(path.resolve().relative_to(directory().resolve()))
    except ValueError:
        return str(path)


def _absolute(path):
    if not path:
        return path
    path = Path(path)
    return str(path if path.is_absolute() else directory() / path)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---- writing -----------------------------------------------------------------------------

def _world(connection, cfg):
    from core_engine.model import config_id, world_summary

    world_id = config_id(cfg)
    summary = world_summary(cfg)
    connection.execute(
        "INSERT OR IGNORE INTO worlds VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (world_id, _now(), _json(cfg), summary["locations"], summary["npcs"], summary["items"], summary["events"],
         summary["missions"], _json(summary["stats"])),
    )
    return world_id


@_safe
def record_world(cfg=None):
    """Store a configuration (default: the active one); returns its id."""
    with connect() as connection:
        return _world(connection, cfg if cfg is not None else config.as_dict())


@_safe
def start_run(origin, kind="evolution", settings=None, parameters=None, seed=None, parent_engine_id=None, run_id=None):
    """A run that is starting, with every setting; returns its id."""
    run_id = run_id or uuid.uuid4().hex
    with connect() as connection:
        world_id = _world(connection, config.as_dict())
        connection.execute(
            "INSERT INTO runs (id, created_at, origin, kind, status, world_id, parent_engine_id, seed, settings_json, "
            "parameters_json) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?)",
            (run_id, _now(), origin, kind, world_id, parent_engine_id, seed, _json(settings), _json(parameters)),
        )
    return run_id


def _engine(connection, model=None, genome=None, score=None, run_id=None, world_id=None, origin=None, source="",
            generation=None, parent_id=None, save_model=True, engine_id=None, created_at=None):
    if model is not None:
        engine_id = model.id
        genome, score, parent_id = model.genome_traits, model.score, parent_id or model.parent_id
        world_id = world_id or _world(connection, model.config)
    engine_id = engine_id or uuid.uuid4().hex
    if connection.execute("SELECT 1 FROM engines WHERE id = ?", (engine_id,)).fetchone():
        return engine_id  # already recorded (e.g. a saved model loaded again): keep its origin and lineage
    model_path = model_sha = None
    if model is not None and save_model:
        model_path = str(models_dir() / f"{engine_id}.ane.zip")
        model.save(model_path)
        model_sha = _sha256(model_path)
    connection.execute(
        "INSERT INTO engines VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (engine_id, created_at or _now(), run_id, world_id, parent_id, origin, source, generation,
         None if score is None else float(score), _json(genome), model_path and _relative(model_path), model_sha,
         int(bool(model is not None and model.checkpoint)),
         (model.checkpoint or {}).get("next_generation") if model is not None else None),
    )
    if model_path:
        _artifact(connection, "engine_model", model_path, run_id=run_id, engine_id=engine_id, world_id=world_id,
                  created_at=created_at)
    return engine_id


@_safe
def record_engine(model=None, genome=None, score=None, run_id=None, origin=None, source="", generation=None,
                  parent_id=None, save_model=True):
    """Store an engine (a model, or a bare genome); returns its id. A model is also
    saved as a file under models/."""
    with connect() as connection:
        world_id = None if model is not None else _world(connection, config.as_dict())
        return _engine(connection, model=model, genome=genome, score=score, run_id=run_id, world_id=world_id,
                       origin=origin, source=source, generation=generation, parent_id=parent_id, save_model=save_model)


@_safe
def finish_run(run_id, result, model=None, origin=None, error=None):
    """Store a run's outcome: every generation, every generation's champion, and the
    best engine (with its model file). Returns the best engine's id."""
    with connect() as connection:
        world_id = connection.execute("SELECT world_id FROM runs WHERE id = ?", (run_id,)).fetchone()
        world_id = world_id["world_id"] if world_id else _world(connection, config.as_dict())
        best_engine_id = None
        if result is not None:
            for stats in result.history:
                champion_id = _engine(connection, genome=stats.best_traits, score=stats.validation, run_id=run_id,
                                      world_id=world_id, origin=origin, source="generation_champion",
                                      generation=stats.generation)
                connection.execute(
                    "INSERT OR REPLACE INTO generations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, stats.generation, stats.best, stats.mean, stats.worst, stats.spread, stats.diversity,
                     stats.mutation_step, stats.validation, int(bool(stats.improved)), champion_id,
                     _json(stats.best_traits)),
                )
            if model is not None:
                best_engine_id = _engine(connection, model=model, run_id=run_id, world_id=world_id, origin=origin,
                                         source="run_best")
            connection.execute(
                "UPDATE runs SET finished_at = ?, status = 'finished', best_score = ?, best_engine_id = ?, "
                "generations_run = ?, early_stopped = ?, warm_started = ?, learning_json = ? WHERE id = ?",
                (_now(), result.best_score, best_engine_id, result.generations_run, int(bool(result.early_stopped)),
                 result.warm_started, _json(result.summary()), run_id),
            )
        else:
            connection.execute("UPDATE runs SET finished_at = ?, status = 'failed', error = ? WHERE id = ?",
                               (_now(), str(error), run_id))
        return best_engine_id


def _story_values(state):
    from core_engine.fitness import _fitness_breakdown

    breakdown = _fitness_breakdown(state) if state.actions_taken else {}
    return {
        "updated_at": _now(), "status": "ended" if state.ended else "in_progress", "turns": state.actions_taken,
        "mission_status": state.mission_status, "end_kind": state.end_kind, "end_reason": state.end_reason,
        "won": int(bool(state.won)), "health": state.health, "coins": state.coins, "path_json": _json(state.path),
        "inventory_json": _json(sorted(state.inventory)),
        "variables_json": _json({k: v for k, v in state.variables.items() if k in state.variable_specs or k == "playthroughs"}),
        "events_json": _json(state.event_log), "fitness": sum(breakdown.values()) if breakdown else None,
        "fitness_json": _json(breakdown), "transcript": state.get_full_story(),
    }


def _turns(connection, story_id, state):
    done = connection.execute("SELECT COUNT(*) FROM turns WHERE story_id = ?", (story_id,)).fetchone()[0]
    for entry in getattr(state, "turn_log", [])[done:]:
        connection.execute(
            "INSERT OR REPLACE INTO turns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (story_id, entry["turn"], _now(), entry["action"], entry["source"], entry.get("owner"),
             entry["location_before"], entry["location_after"], entry["event"], entry["event_after"],
             _json(entry["check"]), int(entry["succeeded"]), entry["outcome"], entry["health"], entry["coins"],
             int(entry["ended"]), entry["end_kind"], state.turn_text(entry)),
        )


@_safe
def record_story(state, origin, kind, run_id=None, engine_id=None, session_id=None, seed=None, parameters=None,
                 story_id=None):
    """Store a story (its parameters, state, result and every turn so far); returns
    its id. Call ``update_story`` as play goes on."""
    story_id = story_id or uuid.uuid4().hex
    values = _story_values(state)
    with connect() as connection:
        world_id = _world(connection, config.as_dict())
        connection.execute(
            "INSERT INTO stories (id, created_at, updated_at, origin, kind, run_id, engine_id, world_id, session_id, "
            "seed, profile, start_location, mission, parameters_json, stats_json, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (story_id, _now(), values["updated_at"], origin, kind, run_id, engine_id, world_id, session_id, seed,
             state.profile if state._store is not None else None, state.start_location,
             state.mission.name if state.mission else None, _json(parameters),
             _json({"points": dict(state.stats.points), "mode": state.stats.mode,
                    "values": state.stats.values()}), values["status"]),
        )
        _update(connection, story_id, state, values)
    return story_id


def _update(connection, story_id, state, values=None):
    values = values or _story_values(state)
    connection.execute(
        "UPDATE stories SET " + ", ".join(f"{key} = ?" for key in values) + " WHERE id = ?",
        (*values.values(), story_id),
    )
    _turns(connection, story_id, state)


@_safe
def update_story(story_id, state):
    """Add the new turns of a story and its current state and result."""
    if story_id:
        with connect() as connection:
            _update(connection, story_id, state)


def _artifact(connection, kind, path, run_id=None, engine_id=None, world_id=None, created_at=None):
    path = Path(path)
    connection.execute(
        "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (uuid.uuid4().hex, created_at or _now(), kind, _relative(path), run_id, engine_id, world_id,
         _sha256(path) if path.is_file() else None, path.stat().st_size if path.is_file() else None),
    )


@_safe
def record_artifact(kind, path, run_id=None, engine_id=None, world_id=None, keep_copy=False):
    """Note a file written for a run or an engine (configuration, export, ...);
    ``keep_copy`` stores a copy under exports/ (for files that are moved or deleted later)."""
    import shutil

    if keep_copy:
        exports_dir().mkdir(parents=True, exist_ok=True)
        target = exports_dir() / f"{uuid.uuid4().hex[:8]}-{Path(path).name}"
        shutil.copy2(path, target)
        path = target
    with connect() as connection:
        _artifact(connection, kind, path, run_id=run_id, engine_id=engine_id, world_id=world_id)
    return str(path)


def _imported_run(connection, learning, world_id, engine_id, created_at):
    """The run an imported engine came from, rebuilt from its learning summary (its
    generations and their champions); the same summary gives the same run."""
    history = [stats for stats in learning.get("history") or [] if isinstance(stats, dict)]
    key = json.dumps({"world": world_id, "history": history}, sort_keys=True, default=str)
    run_id = uuid.uuid5(uuid.NAMESPACE_URL, "ane-imported-run:" + key).hex
    if connection.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone():
        connection.execute("UPDATE runs SET best_engine_id = COALESCE(best_engine_id, ?) WHERE id = ?", (engine_id, run_id))
        return run_id
    connection.execute(
        "INSERT INTO runs (id, created_at, finished_at, origin, kind, status, world_id, best_score, best_engine_id, "
        "generations_run, early_stopped, warm_started, learning_json) "
        "VALUES (?, ?, ?, 'import', 'imported', 'finished', ?, ?, ?, ?, ?, ?, ?)",
        (run_id, created_at, created_at, world_id, learning.get("best_score"), engine_id,
         learning.get("generations_run", len(history)), int(bool(learning.get("early_stopped"))),
         learning.get("warm_started_from_memory"), _json(learning)),
    )
    for stats in history:
        traits = stats.get("best_traits")
        champion_id = None
        if isinstance(traits, dict):
            champion_id = _engine(connection, genome=traits, score=stats.get("validation"), run_id=run_id,
                                  world_id=world_id, origin="import", source="generation_champion",
                                  generation=stats.get("generation"), save_model=False, created_at=created_at)
        connection.execute(
            "INSERT OR REPLACE INTO generations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, stats.get("generation"), stats.get("best"), stats.get("mean"), stats.get("worst"),
             stats.get("spread"), stats.get("diversity"), stats.get("mutation_step"), stats.get("validation"),
             int(bool(stats.get("improved"))), champion_id, _json(traits)),
        )
    return run_id


def import_engine(model, kind, original, created_at=None, learning=None, existing_id=None):
    """Record an engine found in an artifact (not best effort: errors are raised).

    ``model``: the engine (its world, storyteller, score, memory, tag vectors);
    ``original``: the artifact file, already moved into the records folder;
    ``learning``: the training summary it carried, rebuilt as its run and generations;
    ``existing_id``: an engine already recorded with the same world and storyteller,
    which the artifact is then linked to. Returns (engine id, run id, new engine?)."""
    if not enabled():
        raise LookupError(f"Recording is off: {directory()} cannot be used.")
    with _LOCK, connect() as connection:
        world_id = _world(connection, model.config)
        engine_id = existing_id or model.id
        new = existing_id is None and not connection.execute("SELECT 1 FROM engines WHERE id = ?", (engine_id,)).fetchone()
        if new:
            _engine(connection, model=model, world_id=world_id, origin="import", source=model.source or f"imported {kind}",
                    created_at=created_at)
        run_id = None
        if isinstance(learning, dict) and learning.get("history"):
            run_id = _imported_run(connection, learning, world_id, engine_id, created_at or _now())
            connection.execute("UPDATE engines SET run_id = ? WHERE id = ? AND run_id IS NULL", (run_id, engine_id))
        _artifact(connection, f"imported_{kind}", original, run_id=run_id, engine_id=engine_id, world_id=world_id,
                  created_at=created_at)
        return engine_id, run_id, new


def engine_index():
    """(world id, storyteller key) -> engine id of the recorded engines, to recognise duplicates."""
    if not enabled():
        return {}
    with _LOCK, connect() as connection:
        rows = connection.execute("SELECT id, world_id, genome_json FROM engines WHERE source != 'generation_champion'")
        return {(row["world_id"], genome_key(json.loads(row["genome_json"]))): row["id"] for row in rows}


def genome_key(traits):
    """A storyteller's traits as a comparable key (rounded, sorted)."""
    return json.dumps({k: round(float(v), 9) for k, v in sorted((traits or {}).items())})


# ---- reading ---------------------------------------------------------------------------------

_JSON_COLUMNS = {"settings_json", "parameters_json", "learning_json", "best_traits_json", "genome_json", "stats_json",
                 "path_json", "inventory_json", "variables_json", "events_json", "fitness_json", "check_json",
                 "config_json"}


def _row(row, drop=()):
    if row is None:
        return None
    data = {}
    for key in row.keys():
        if key in drop:
            continue
        value = row[key]
        if key in _JSON_COLUMNS:
            data[key[:-5]] = json.loads(value) if value else None
        elif key in ("model_path", "path"):
            data[key] = _absolute(value)
        else:
            data[key] = value
    return data


def _require():
    if not database_path().exists():
        raise LookupError(f"No records yet: {database_path()} does not exist.")


def _list(query, args=(), drop=()):
    _require()
    with connect() as connection:
        return [_row(row, drop) for row in connection.execute(query, args).fetchall()]


def _get(table, record_id, drop=()):
    _require()
    with connect() as connection:
        row = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        raise LookupError(f"No {table[:-1]} `{record_id}`.")
    return _row(row, drop)


def list_runs(limit=50, offset=0):
    return _list("SELECT * FROM runs ORDER BY created_at DESC, id LIMIT ? OFFSET ?", (limit, offset),
                 drop=("learning_json",))


def get_run(run_id):
    run = _get("runs", run_id)
    run["generations"] = _list("SELECT * FROM generations WHERE run_id = ? ORDER BY generation", (run_id,))
    run["engines"] = list_engines(run_id=run_id)
    run["stories"] = list_stories(run_id=run_id)
    run["artifacts"] = _list("SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at", (run_id,))
    return run


def list_worlds(limit=50, offset=0):
    return _list("SELECT * FROM worlds ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset),
                 drop=("config_json",))


def get_world(world_id):
    """A recorded configuration: world and tuning (``config``), with its summary."""
    return _get("worlds", world_id)


def list_engines(run_id=None, source=None, limit=200, offset=0):
    clauses, args = [], []
    if run_id:
        clauses.append("run_id = ?")
        args.append(run_id)
    if source:
        clauses.append("source = ?")
        args.append(source)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return _list(f"SELECT * FROM engines{where} ORDER BY created_at DESC, generation LIMIT ? OFFSET ?",
                 (*args, limit, offset))


def get_engine(engine_id):
    engine = _get("engines", engine_id)
    engine["stories"] = list_stories(engine_id=engine_id)
    return engine


def load_engine(engine_id):
    """The EngineModel of a recorded engine: its model file, or (for a generation's
    champion) its genome with its run's world and the run's tag vectors."""
    from core_engine.model import EngineModel

    engine = _get("engines", engine_id)
    if engine["model_path"]:
        path = Path(engine["model_path"])
        if not path.is_file():
            raise LookupError(f"The model file of engine `{engine_id}` is missing: {path}")
        if engine["model_sha256"] and _sha256(path) != engine["model_sha256"]:
            raise ValueError(f"The model file of engine `{engine_id}` has changed since it was recorded.")
        return EngineModel.load(path)
    world = get_world(engine["world_id"])["config"]
    vectors = {}
    if engine["run_id"]:
        best = _list("SELECT id FROM engines WHERE run_id = ? AND model_path IS NOT NULL", (engine["run_id"],))
        if best:
            vectors = load_engine(best[0]["id"]).tag_vectors
    return EngineModel(world, engine["genome"], score=engine["score"], tag_vectors=vectors, model_id=engine_id,
                       parent_id=engine["parent_id"], source=engine["source"])


def list_stories(run_id=None, engine_id=None, kind=None, origin=None, session_id=None, limit=200, offset=0):
    clauses, args = [], []
    for column, value in (("run_id", run_id), ("engine_id", engine_id), ("kind", kind), ("origin", origin),
                          ("session_id", session_id)):
        if value:
            clauses.append(f"{column} = ?")
            args.append(value)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return _list(f"SELECT * FROM stories{where} ORDER BY created_at DESC LIMIT ? OFFSET ?", (*args, limit, offset),
                 drop=("transcript", "events_json", "fitness_json"))


def get_story(story_id):
    """A story with its parameters, result, transcript and every turn."""
    story = _get("stories", story_id)
    story["turn_records"] = _list("SELECT * FROM turns WHERE story_id = ? ORDER BY turn", (story_id,))
    return story
