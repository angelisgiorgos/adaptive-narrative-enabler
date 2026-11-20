"""Export a trained engine to run offline and integrate in a game: an "ANE AI".

The ZIP contains everything the engine needs and nothing of the studio. Every export has the
engine package:

    <name>/
      engine.json                 manifest: evolved storyteller, score, learning history, world summary
      config/engine.yaml          the complete active configuration (world, tuning, messages, stats, ...)
      data/tag_vectors.json       the tag vectors the storyteller was trained with (identical behaviour)
      saves/evolution_memory.json the learning memory, to keep training offline (optional)
      README.md

and, by ``platform``, what runs it:

- ``python``: the engine code (core_engine/, utils/, narrative_runtime/), run.py and
  requirements.txt (Python 3.10+ with pyyaml and numpy);
- ``windows-x64``, ``linux-x64``, ``macos-arm64``, ``macos-x64``: the standalone runtime built for
  that system (``ane-ai`` / ``ane-ai.exe`` with Python and the engine inside, runtime.json), from
  ``runtimes/ane-runtime-<platform>.zip`` (ANE_RUNTIMES_DIR; CI builds them, packaging/). Its code
  version must be the API's, so the runtime plays exactly like the studio.
- ``unity``: a ``.unitypackage`` (Assets > Import Package > Custom Package): the C# library that
  starts, drives and stops the ANE AI from a game (unity/com.ane.ai), and in StreamingAssets/ANE-AI
  the engine package (package.zip) and the standalone runtimes (runtimes/), unpacked by the
  library on first start.
"""
from __future__ import annotations

import datetime
import io
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
import uuid
import zipfile
from pathlib import Path

import yaml

from core_engine import Genome, version
from core_engine.evolution import LearningMemory
from narrative_runtime.engine import FORMAT
from utils.config_loader import config
from utils.tag_similarity import export_tag_vectors

from .errors import AuthoringError
from . import records
from .sessions import ENGINES, RUNS, SESSIONS, SESSION_LOCK, kept_engines
from . import engine_meta
from .store import PROJECT_ROOT

RUNTIME_CODE = [
    *(f"core_engine/{path.name}" for path in sorted((PROJECT_ROOT / "core_engine").glob("*.py")) if path.name != "augmenter.py"),
    "utils/config_loader.py",
    "utils/tag_similarity.py",
    *(f"narrative_runtime/{path.name}" for path in sorted((PROJECT_ROOT / "narrative_runtime").glob("*.py"))),
]
# The tag collector lives with the engine model (it saves the same vectors).
from core_engine.model import ENGINE_TAGS, world_tags  # noqa: E402,F401
SOURCES = ("run", "memory", "game", "engine")
PLATFORMS = ("python", "windows-x64", "linux-x64", "macos-arm64", "macos-x64", "unity")
STANDALONE = PLATFORMS[1:5]
UNITY_PACKAGE = PROJECT_ROOT / "unity" / "com.ane.ai"


class RuntimeUnavailable(AuthoringError):
    """No standalone runtime for that platform, or one built from other engine code."""


class EngineRequired(AuthoringError):
    """An ANE AI (a standalone or Unity export) is made from a complete engine, and there is none
    to make it from: no finished run or loaded engine yet, or a partial storyteller was asked for."""


ANE_AI = STANDALONE + ("unity",)
NO_ENGINE = ("No complete engine yet: the ANE AI is made from a complete engine (the engine model a finished "
             "training run saves, or a loaded engine). Run a training or load an engine first, or export the "
             "Python version.")


def complete_engines(limit=50):
    """The complete engines (finished runs) an export can be made from, newest first: recorded
    engines with their model file (a run's engine, a loaded, continued or retrained one; not a
    generation's champion), and, when recording is off, those kept with the saves and this
    process's. Each: engine_id, created_at, source, score, run_id, resumable, generations, loaded."""
    found = {}
    if records.readable():
        try:
            rows = records.list_engines(limit=500)
        except LookupError:  # no records yet
            rows = []
        except Exception as exc:  # noqa: BLE001 - unreadable records: the runs kept with the saves are still offered
            logging.getLogger("ane.export").warning("The recorded engines could not be read: %s", exc)
            rows = []
        for row in rows:
            if row.get("model_path") and records.model_readable(row["model_path"]):
                found[row["id"]] = {"engine_id": row["id"], "created_at": row["created_at"], "source": row["source"],
                                    "score": row["score"], "run_id": row["run_id"],
                                    "resumable": bool(row.get("resumable")),
                                    "generations": row.get("trained_generations")}
    with SESSION_LOCK:
        kept = list(ENGINES.values())
    for info in kept_engines() + kept:
        if info["engine_id"] not in found and Path(info["model_path"]).is_file():
            found[info["engine_id"]] = {k: v for k, v in info.items() if k != "model_path"}
    for info in found.values():
        info.setdefault("generations", None)
        info["loaded"] = not info.get("run_id")
    known = engine_meta.names()
    return [engine_meta.annotate(info, known)
            for info in sorted(found.values(), key=lambda e: e["created_at"] or "", reverse=True)[:limit]]


def _engine_row(engine_id):
    """The records' row of an engine, or None (not recorded, or no records)."""
    if not records.readable():
        return None
    try:
        return records.engine_record(engine_id)
    except Exception:  # noqa: BLE001 - not recorded (LookupError), or the records cannot be read
        return None


def known_engine(engine_id) -> bool:
    """Whether an engine is a complete engine here or a recorded one (a generation's champion too)."""
    return _kept_engine(engine_id) is not None or _engine_row(engine_id) is not None


def engine_card(engine_id):
    """The result card of an engine (backend/engine_meta.py): read from its model file, whether it is
    recorded or kept with the saves (recording off). A recorded generation's champion has no model
    file: its card has what its record says. LookupError for an unknown engine."""
    from utils.config_loader import config

    listed = next((e for e in complete_engines(limit=500) if e["engine_id"] == engine_id), None)
    kept = _kept_engine(engine_id)
    row = _engine_row(engine_id)
    current = engine_meta.world_digest(config.as_dict())
    path = kept["model_path"] if kept else None
    if path is None and row and row.get("model_path") and records.model_readable(row["model_path"]):
        path = row["model_path"]
    if path:
        info = listed or {**(row or {}), "engine_id": engine_id}
        return engine_meta.card(engine_id, path, info, current)
    if row is None:
        raise LookupError(f"Unknown engine `{engine_id}`: GET /v1/exports/engines lists the finished runs.")
    info = {"engine_id": engine_id, "created_at": row.get("created_at"), "source": row.get("source"),
            "score": row.get("score"), "generation": row.get("generation")}
    labelled = engine_meta.annotate(info)
    known = engine_meta.entry(engine_id)
    return {"engine_id": engine_id, "run_id": row.get("run_id"), "name": labelled["name"], "label": labelled["label"],
            "default_label": engine_meta.default_label(info), "created_at": row.get("created_at"),
            "kind": engine_meta.kind_of(row.get("source")), "source": row.get("source"),
            "parent_id": row.get("parent_id"),
            "world": {"id": row.get("world_id"), "digest": None, "summary": {}, "current": None},
            "generations": None, "generation": row.get("generation"), "trained_generations": None,
            "settings": {"main": {}, "other": {}},
            "fitness": {"training": row.get("score"), "improvement": None, "early_stopped": None, "history": []},
            "judge_in_training": None, "evaluations": list(known.get("evaluations") or []), "resumable": False,
            "scaling_traits": engine_meta.scaling_traits(row.get("genome"))}


def _kept_engine(engine_id):
    """This process's or the saves' record of a complete engine (with its model_path), or None."""
    with SESSION_LOCK:
        kept = ENGINES.get(engine_id)
    if kept is None:
        kept = next((e for e in reversed(kept_engines()) if e.get("engine_id") == engine_id), None)
    return kept if kept is not None and Path(kept["model_path"]).is_file() else None


def complete_engine(source="run", run_id=None, generation=None, engine_id=None):
    """The EngineModel an ANE AI is made from: ``run`` (``run_id``, default the latest run) is the
    run's engine; ``engine`` is a complete engine (``engine_id``, default the newest). Anything
    else (a generation's champion, the remembered storyteller, a play session) is refused."""
    from core_engine.model import EngineModel

    if source == "run":
        if generation not in (None, ""):
            raise EngineRequired("The ANE AI is made from a complete engine, and a generation's champion is not one "
                                 "(it is a storyteller without its engine model): export the run's engine, without "
                                 "`generation`, or the Python version.")
        run = RUNS.get(run_id) if run_id else (RUNS[next(reversed(RUNS))] if RUNS else None)
        if run is None:
            raise EngineRequired(f"Unknown run `{run_id}`: use GET /v1/runs, or source `engine`." if run_id else NO_ENGINE)
        engine_id = run.get("engine_id")
    elif source != "engine":
        what = {"memory": "the remembered storyteller", "game": "a play session's storyteller"}.get(source, source)
        raise EngineRequired(f"The ANE AI is made from a complete engine (a finished run's engine model, or a loaded "
                             f"engine), not from {what}: use source `run` or `engine`, or export the Python version.")
    elif not engine_id:
        newest = complete_engines(limit=1)
        if not newest:
            raise EngineRequired(NO_ENGINE)
        engine_id = newest[0]["engine_id"]

    kept = _kept_engine(engine_id)
    record = None
    if records.readable():
        try:
            record = records.engine_record(engine_id)
        except LookupError:
            record = None
    if record is not None and not record.get("model_path"):
        raise EngineRequired(f"Engine `{engine_id}` is a generation's champion, a storyteller without its engine "
                             "model, so not a complete engine: export its run's engine, or the Python version.")
    if record is not None:
        try:
            return records.load_engine(engine_id)
        except (LookupError, ValueError, OSError) as exc:
            if kept is None:
                raise EngineRequired(str(exc) if not isinstance(exc, OSError) else
                                     f"The engine of this run cannot be read by this server ({exc.strerror}): "
                                     "choose another finished run.") from exc
    if kept is not None:
        return EngineModel.load(kept["model_path"])
    raise EngineRequired(f"No complete engine `{engine_id}` here (GET /v1/exports/engines lists them).")


def runtimes_dir() -> Path:
    return Path(os.environ.get("ANE_RUNTIMES_DIR") or (PROJECT_ROOT / "runtimes"))


def _runtime_info(platform):
    """runtime.json of runtimes/ane-runtime-<platform>.zip, or None."""
    archive = runtimes_dir() / f"ane-runtime-{platform}.zip"
    if not archive.is_file():
        return None
    try:
        with zipfile.ZipFile(archive) as zipped:
            return {**json.loads(zipped.read("runtime.json")), "zip": str(archive)}
    except (KeyError, ValueError, zipfile.BadZipFile):
        return None


def platforms() -> list[dict]:
    """What an export can run on: [{platform, available, reason?, code_version?, built_at?}]."""
    result = [{"platform": "python", "available": True, "description": "Python 3.10+ with pyyaml and numpy"}]
    for platform in STANDALONE:
        info = _runtime_info(platform)
        if info is None:
            result.append({"platform": platform, "available": False,
                           "reason": f"no runtime: put ane-runtime-{platform}.zip in the API's runtimes folder "
                                     "(ANE_RUNTIMES_DIR) and restart it"})
        elif info.get("code_version") != version.code_version():
            result.append({"platform": platform, "available": False, "code_version": info.get("code_version"),
                           "reason": f"its runtime was built from other engine code ({info.get('code_version')}, "
                                     f"the API runs {version.code_version()})"})
        else:
            result.append({"platform": platform, "available": True, "code_version": info["code_version"],
                           "built_at": info.get("built_at"), "python": info.get("python")})
    standalone = [p["platform"] for p in result if p["platform"] in STANDALONE and p["available"]]
    unity = {"platform": "unity", "available": bool(standalone) and UNITY_PACKAGE.is_dir(), "includes": standalone,
             "description": "a .unitypackage: the C# library and the runtimes for " + (", ".join(standalone) or "no system")}
    if not unity["available"]:
        unity["reason"] = "no standalone runtime here to put in it" if not standalone else "the Unity library is missing"
    result.append(unity)
    return result


# ---- the Unity package (.unitypackage) ------------------------------------------------------------

_META = {
    ".cs": "MonoImporter:\n  externalObjects: {}\n  serializedVersion: 2\n  defaultReferences: []\n  executionOrder: 0\n"
           "  icon: {instanceID: 0}\n  userData: \n  assetBundleName: \n  assetBundleVariant: \n",
    ".asmdef": "AssemblyDefinitionImporter:\n  externalObjects: {}\n  userData: \n  assetBundleName: \n  assetBundleVariant: \n",
    ".md": "TextScriptImporter:\n  externalObjects: {}\n  userData: \n  assetBundleName: \n  assetBundleVariant: \n",
    ".json": "TextScriptImporter:\n  externalObjects: {}\n  userData: \n  assetBundleName: \n  assetBundleVariant: \n",
}
_DEFAULT_META = "DefaultImporter:\n  externalObjects: {}\n  userData: \n  assetBundleName: \n  assetBundleVariant: \n"


def _unity_guid(asset_path):
    """A GUID fixed per asset path: importing a newer export updates the same assets."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"ane-ai:{asset_path}").hex


def _unity_meta(asset_path, folder=False):
    head = f"fileFormatVersion: 2\nguid: {_unity_guid(asset_path)}\n"
    if folder:
        return head + "folderAsset: yes\n" + _DEFAULT_META
    return head + _META.get(Path(asset_path).suffix.lower(), _DEFAULT_META)


def write_unitypackage(assets, path):
    """A .unitypackage (a gzipped tar of <guid>/{pathname, asset, asset.meta}) holding ``assets``
    ({"Assets/...": bytes}) and their folders."""
    folders = set()
    for asset_path in assets:
        parts = asset_path.split("/")
        folders |= {"/".join(parts[:i]) for i in range(2, len(parts))}

    def add(tar, name, data):
        info = tarfile.TarInfo(name)
        info.size, info.mode, info.mtime = len(data), 0o644, 0
        tar.addfile(info, io.BytesIO(data))

    with tarfile.open(path, "w:gz") as tar:
        for folder in sorted(folders):
            guid = _unity_guid(folder)
            add(tar, f"{guid}/pathname", folder.encode("utf-8"))
            add(tar, f"{guid}/asset.meta", _unity_meta(folder, folder=True).encode("utf-8"))
        for asset_path, data in sorted(assets.items()):
            guid = _unity_guid(asset_path)
            add(tar, f"{guid}/pathname", asset_path.encode("utf-8"))
            add(tar, f"{guid}/asset", data)
            add(tar, f"{guid}/asset.meta", _unity_meta(asset_path).encode("utf-8"))


def _unity_assets(package_root, manifest, runtimes):
    """The assets of the Unity export: the library, the engine package and the runtimes."""
    assets = {}
    for folder, target in (("Runtime", "Runtime"), ("Editor", "Editor"), ("Samples~/TextAdventure", "Samples/TextAdventure")):
        for source in sorted((UNITY_PACKAGE / folder).glob("*")):
            if source.suffix in (".cs", ".asmdef"):
                assets[f"Assets/ANE-AI/{target}/{source.name}"] = source.read_bytes()
    assets["Assets/ANE-AI/README.md"] = (UNITY_PACKAGE / "README.md").read_bytes()
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted(package_root.rglob("*")):
            if path.is_file() and path.name != "README.md":
                zipped.write(path, path.relative_to(package_root).as_posix())
    assets["Assets/StreamingAssets/ANE-AI/package.zip"] = package.getvalue()
    for info in runtimes:
        assets[f"Assets/StreamingAssets/ANE-AI/runtimes/ane-runtime-{info['target']}.zip"] = Path(info["zip"]).read_bytes()
    assets["Assets/StreamingAssets/ANE-AI/ane-ai.json"] = json.dumps({
        "name": manifest["name"], "code_version": manifest["code_version"], "protocol": manifest["protocol"],
        "platforms": [info["target"] for info in runtimes], "created": manifest["created"]}, indent=2).encode("utf-8")
    return assets


def _choose_storyteller(source, run_id=None, generation=None, token=None, engine_id=None):
    """(genome, score, description, learning) of the storyteller to export."""
    if source == "engine":
        if not engine_id:
            raise AuthoringError("Give the `engine_id` of a recorded engine to export.")
        try:
            model = records.load_engine(engine_id)
        except (LookupError, ValueError) as exc:
            raise AuthoringError(str(exc)) from exc
        model.activate()  # its own world and tuning, not whatever is active
        return model.genome, model.score, f"recorded engine {engine_id}", model.learning
    if source == "run":
        run = RUNS.get(run_id) if run_id else (RUNS[next(reversed(RUNS))] if RUNS else None)
        if run is None:
            raise AuthoringError("No evolution run to export yet: run a simulation first (or export from memory).")
        learning = run["learning"]
        if generation is None or generation == "":
            return Genome.from_traits(learning["best_genome"]), learning["best_score"], "best of the run", learning
        history = learning["history"]
        generation = int(generation)
        if not 0 <= generation < len(history):
            raise AuthoringError(f"The run has generations 0-{len(history) - 1}, not {generation}.")
        stats = history[generation]
        return (Genome.from_traits(stats["best_traits"]), stats["validation"],
                f"champion of generation {generation}", {**learning, "history": history[: generation + 1]})
    if source == "memory":
        entries = sorted(LearningMemory().load(), key=lambda e: e["score"], reverse=True)
        if not entries:
            raise AuthoringError("The AI has not learned anything yet: run a simulation first.")
        return Genome.from_traits(entries[0]["traits"]), entries[0]["score"], "best remembered storyteller", None
    if source == "game":
        with SESSION_LOCK:
            state = SESSIONS.get(token)
        if state is None:
            raise AuthoringError("That game session no longer exists.")
        return Genome.from_traits(state.genome.traits()), None, "storyteller of the current game", None
    raise AuthoringError(f"Unknown export source `{source}`; use one of: {', '.join(SOURCES)}.")


def _standalone_guide(name: str, platform: str, executable: str) -> str:
    run = executable if platform.startswith("windows") else f"./{executable}"
    return f"""# {name}: ANE AI for {platform}

An evolved storyteller with its world, ready to run on this computer without Python, the studio
or an internet connection. `{executable}` is the program; `engine.json`, `config/` and `data/`
are the engine package it plays.

## Start it (from the UNP)

    {run} serve --port 0 --ready-file ready.json --parent-pid <UNP pid> --exit-on-stdin-eof

Set `ANE_RUNTIME_TOKEN` in its environment (or read the token from the ready line). It listens
on 127.0.0.1 only, prints one JSON line when ready:

    {{"event": "ready", "protocol": 1, "url": "http://127.0.0.1:<port>", "port": ..., "pid": ..., ...}}

and every request needs `Authorization: Bearer <token>`. `--unp-url URL` makes it POST session
reports and events to the UNP; `--data-dir DIR` says where profiles and saves go (default: the
user's application-data folder).

## Stop it

`POST /shutdown {{"save": true}}`: every session is saved (data dir `suspended/`), everything is
delivered, it prints `{{"event": "stopped"}}` and exits with code 0. It also stops when the UNP
process ends (`--parent-pid`), when stdin closes (`--exit-on-stdin-eof`) or on SIGTERM. Wait a few
seconds after `/shutdown`, then kill it if it is still running.

## Talk to it

| Request | What it does |
| --- | --- |
| `GET /world`, `GET /world/<kind>/<name>` | the world as parameters: locations, NPCs, items, events, missions, stats, ... |
| `POST /world/patch` | world content from the UNP (items, actions, tags, ...); 422 with every problem |
| `POST /games` | a new session (`profile`, `start`, `mission`, `player`: stats, inventory, coins, ...) or `{{"savegame": ...}}` to continue one |
| `GET /games/<id>`, `POST /games/<id>/choose` | the scene, and the player's choice |
| `PATCH /games/<id>/player`, `POST /games/<id>/sync` | the player's state, and what happened in the UNP |
| `POST /games/<id>/save`, `POST /games/<id>/cancel` | the save game (`ane.savegame/1`); cancel also releases the session |
| `POST /games/<id>/end`, `GET /games/<id>/report` | end the session; its report |
| `GET /progression/<profile>` | the save game of a player: goals achieved, sessions |
| `GET /health`, `POST /shutdown` | is it up; stop it |

The full protocol is in the Adaptive Narrative Enabler's `docs/UNP.md`.

## Notes

- macOS: an unsigned program downloaded with a browser is quarantined; the UNP can start it
  after `xattr -dr com.apple.quarantine <folder>`, or download it itself.
- Windows: start it with `CREATE_NO_WINDOW`, and put it in a Job Object with
  `KILL_ON_JOB_CLOSE` so it never outlives the UNP.
"""


def _guide(name: str) -> str:
    return f"""# {name}: offline Adaptive Narrative engine

An evolved storyteller with its world, ready to run without the studio,
the REST API or an internet connection. Python 3.10+ and the packages in
`requirements.txt` (`pip install -r requirements.txt`) are all it needs.

## Try it

    python run.py info          # what is inside
    python run.py play          # play in the terminal
    python run.py serve         # JSON server on http://127.0.0.1:8765 for any game engine (token in the ready line)
    python run.py evolve        # keep training offline (adopts a better storyteller)

## Embed it in a Python game

```python
import sys; sys.path.insert(0, "path/to/{name}")
from narrative_runtime import NarrativeEngine

engine = NarrativeEngine("path/to/{name}")
game = engine.new_game(profile="player-1", stats_mode="random")   # or stats={{...}} points, start="Harbor"
view = game.view()        # location, description, npcs, items, actions, health, mission, stats, ...
while not view["ended"]:
    view = game.choose(view["actions"][0])   # by name or index; view["turn"] is the story text
game.save("slot1.json")   # a JSON save game; engine.load_game("slot1.json") continues it
```

## Any other engine (Unity, Godot, Unreal, web): HTTP + JSON

Start `python run.py serve --port 8765` next to the game (or as a sidecar
process). It prints one JSON line when ready, with the token every request needs
(`Authorization: Bearer <token>`; set ANE_RUNTIME_TOKEN to choose it), then:

| Request | Body | Returns |
| --- | --- | --- |
| `GET /engine` | | engine information |
| `POST /games` | `{{"profile": "p1", "stats_mode": "random"}}` (optional `"start"`: one of `starting_locations`) | the first view, with `game_id` |
| `GET /games/<id>` | | the current view |
| `POST /games/<id>/choose` | `{{"action": "Explore harbor"}}` or `{{"action": 0}}` | the turn text and the new view |
| `GET /games/<id>/transcript` | | the story so far |
| `POST /games/<id>/end` | | ends the session: the player's progression is saved and its report sent to the UNP (`session_report.url` or `ANE_UNP_URL`) |
| `GET /games/<id>/report` | | the ended session's report: goal achieved or mission failed, stats, inventory, progression |
| `GET /progression/<profile>` | | the save game: goals achieved (each once), playthroughs, session reports |
| `DELETE /games/<id>` | | forgets the game (end it first to count it as a session) |
| `GET /world` · `POST /world/patch` | | the world as parameters · world content from the UNP |
| `PATCH /games/<id>/player` · `POST /games/<id>/sync` | | the player's state · what happened in the UNP |
| `POST /games/<id>/save` · `POST /games/<id>/cancel` | | the save game (`ane.savegame/1`) · save and release |
| `POST /games` | `{{"savegame": {{...}}}}` | continue a saved session |
| `POST /shutdown` | `{{"save": true}}` | save every session and stop |
| `POST /evolve` | `{{"generations": 5}}` | offline training summary |

Unity (C#):

```csharp
var request = new UnityWebRequest("http://127.0.0.1:8765/games", "POST");
request.uploadHandler = new UploadHandlerRaw(System.Text.Encoding.UTF8.GetBytes("{{\\"profile\\":\\"p1\\"}}"));
request.downloadHandler = new DownloadHandlerBuffer();
request.SetRequestHeader("Content-Type", "application/json");
request.SetRequestHeader("Authorization", "Bearer " + token);
yield return request.SendWebRequest();   // request.downloadHandler.text is the view (JSON)
```

Godot (GDScript):

```gdscript
var http = HTTPRequest.new(); add_child(http)
http.request("http://127.0.0.1:8765/games/" + game_id + "/choose",
    ["Content-Type: application/json", "Authorization: Bearer " + token], HTTPClient.METHOD_POST,
    JSON.stringify({{"action": 0}}))
```

## What is inside

- `engine.json`: the evolved storyteller (genome), its benchmark score, how
  it was learned (history per generation), and a world summary.
- `config/engine.yaml`: the world and all tuning, as it was when exported.
  Edit it to change the world; the storyteller adapts through its traits.
- `data/tag_vectors.json`: the tag vectors used during training, so the engine
  behaves exactly as it did in the studio without any machine-learning
  libraries.
- `saves/`: persistent variables per player profile (created when playing)
  and the learning memory used by `evolve`.
"""


def export_engine(source="run", run_id=None, generation=None, token=None, include_memory=True, name=None,
                  genome=None, score=None, learning=None, description=None, engine_id=None, checkpoint=None,
                  memory=None, platform="python", model=None) -> dict:
    """Build the offline engine ZIP; returns {"path", "manifest"}.

    The storyteller comes from ``source``: "run" (``run_id``, default the latest
    run; ``generation`` for a generation's champion instead of the run's best),
    "memory" (the best remembered storyteller), "game" (``token``'s game) or "engine"
    (``engine_id``). Passing ``genome`` exports that storyteller directly (e.g. from main.py).
    ``platform``: what runs it (PLATFORMS): the Python code, or a standalone runtime
    (RuntimeUnavailable when it is missing or was built from other engine code).

    An ANE AI (a standalone or Unity export) is made only from a complete engine: ``model`` (an
    EngineModel), or the one ``complete_engine`` finds for ``source`` "run" or "engine"; it carries
    that engine's own world, storyteller, training state, memory and tag vectors
    (EngineRequired otherwise)."""
    if platform not in PLATFORMS:
        raise AuthoringError(f"Unknown platform `{platform}`; use one of: {', '.join(PLATFORMS)}.")
    runtime, unity_runtimes = None, []
    if platform in STANDALONE or platform == "unity":
        offered = next(p for p in platforms() if p["platform"] == platform)
        if not offered["available"]:
            raise RuntimeUnavailable(f"No ANE AI runtime for {platform}: {offered['reason']}.")
        if platform == "unity":
            unity_runtimes = [_runtime_info(name) for name in offered["includes"]]
        else:
            runtime = _runtime_info(platform)
    active = vectors = None
    if platform in ANE_AI and model is None:
        if genome is not None:
            raise EngineRequired("The ANE AI is made from a complete engine: pass its engine model, not a "
                                 "storyteller alone.")
        model = complete_engine(source, run_id, generation, engine_id)
    elif model is None and genome is None and (source == "engine" or (source == "run" and generation in (None, ""))):
        try:  # a finished run (its engine model), as for the ANE AI
            model = complete_engine(source, run_id, None, engine_id)
        except EngineRequired:
            model = None  # a generation's champion, or no run's engine: the storyteller, as before
    if model is not None:
        genome, score, learning = model.genome, model.score, model.learning
        checkpoint, memory, engine_id = model.checkpoint, model.memory, model.id
        description = "best of the run" if source == "run" else (model.source or f"engine {model.id}")
        active = dict(model.config)  # its own world and tuning, whatever the studio has active now
        vectors = model.tag_vectors or None
    elif genome is not None:
        description = description or "evolved storyteller"
    else:
        genome, score, description, learning = _choose_storyteller(source, run_id, generation, token, engine_id)
        if source == "engine":
            model = records.load_engine(engine_id)
            checkpoint, memory = model.checkpoint, model.memory
        elif source == "run" and generation in (None, ""):
            run = RUNS.get(run_id) if run_id else (RUNS[next(reversed(RUNS))] if RUNS else {})
            checkpoint, engine_id = run.get("checkpoint"), run.get("engine_id")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    # The engine's own name (studio), if it has one: the package's default file name, and in its manifest.
    engine_name = engine_meta.names().get(engine_id) if engine_id else None
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name or engine_name or f"narrative-engine-{stamp}").strip("-") \
        or "narrative-engine"
    active = active if active is not None else config.as_dict()
    world = active.get("world_definition") or {}
    manifest = {
        "format": FORMAT,
        "version": 1,
        "name": name,
        "engine_id": engine_id,
        "engine_name": engine_name,
        "resumable": bool(checkpoint),
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source": description,
        "score": score,
        "genome": genome.traits(),
        "learning": learning,
        "world": {
            "locations": len(world.get("locations") or {}),
            "npcs": len(world.get("characters") or []),
            "items": len(world.get("objects") or []),
            "events": len(world.get("events") or {}),
            "missions": len(world.get("missions") or {}),
            "stats": list((world.get("character_stats") or {}).get("stats") or {}),
        },
        "python": ">=3.10",
        "requirements": ["pyyaml>=6", "numpy>=1.24"],
        "platform": platform,
        "code_version": version.code_version(),
        "protocol": version.PROTOCOL,
    }
    staging = Path(tempfile.mkdtemp(prefix="narrative-export-"))
    root = staging / name
    try:
        root.mkdir(parents=True)
        if platform == "python":
            for relative in RUNTIME_CODE:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(PROJECT_ROOT / relative, target)
        elif runtime is not None:
            with zipfile.ZipFile(runtime["zip"]) as zipped:  # the standalone program, execute bits kept
                for info in zipped.infolist():
                    extracted = Path(zipped.extract(info, root))
                    mode = info.external_attr >> 16
                    if mode and not info.is_dir():
                        extracted.chmod(mode & 0o777)
        (root / "config").mkdir()
        (root / "config" / "engine.yaml").write_text(
            yaml.safe_dump(active, sort_keys=False, allow_unicode=True), encoding="utf-8")
        (root / "data").mkdir()
        (root / "data" / "tag_vectors.json").write_text(
            json.dumps(vectors if vectors is not None else export_tag_vectors(world_tags(active))), encoding="utf-8")
        (root / "saves").mkdir()
        if checkpoint:
            # The training state: `python run.py evolve --resume` continues it exactly.
            (root / "data" / "training.json").write_text(json.dumps(checkpoint), encoding="utf-8")
        if include_memory:
            entries = memory if memory is not None else LearningMemory().load()
            if entries:
                (root / "saves" / "evolution_memory.json").write_text(
                    json.dumps({"version": 1, "hall_of_fame": entries}, indent=2), encoding="utf-8")
        (root / "engine.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if platform == "python":
            (root / "requirements.txt").write_text("\n".join(manifest["requirements"]) + "\n", encoding="utf-8")
            (root / "run.py").write_text(
                '"""Run this engine: python run.py info | play | serve | evolve"""\n'
                "from narrative_runtime.__main__ import main\n\nif __name__ == \"__main__\":\n    main()\n",
                encoding="utf-8")
            (root / "README.md").write_text(_guide(name), encoding="utf-8")
        elif runtime is not None:
            (root / "README.md").write_text(_standalone_guide(name, platform, runtime["executable"]), encoding="utf-8")
        if platform == "unity":
            archive = Path(tempfile.mkstemp(prefix=f"{name}-", suffix=".unitypackage")[1])
            write_unitypackage(_unity_assets(root, manifest, unity_runtimes), archive)
            manifest["unity_platforms"] = [info["target"] for info in unity_runtimes]
            records.record_artifact("offline_engine", archive, run_id=run_id if source == "run" else None,
                                    engine_id=engine_id, keep_copy=True)
            return {"path": str(archive), "manifest": manifest}
        archive = Path(tempfile.mkstemp(prefix=f"{name}-", suffix=".zip")[1])
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
            for path in sorted(root.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    zipped.write(path, path.relative_to(staging))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    records.record_artifact("offline_engine", archive, run_id=run_id if source == "run" else None,
                            engine_id=engine_id, keep_copy=True)
    return {"path": str(archive), "manifest": manifest}
