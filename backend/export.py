"""Export a trained engine to run offline and integrate in a game.

The ZIP contains everything the engine needs and nothing of the studio:

    <name>/
      engine.json                 manifest: evolved storyteller, score, learning history, world summary
      config/engine.yaml          the complete active configuration (world, tuning, messages, stats, ...)
      data/tag_vectors.json       the tag vectors the storyteller was trained with (identical behaviour)
      saves/evolution_memory.json the learning memory, to keep training offline (optional)
      core_engine/  utils/        the engine code (no torch, gradio or fastapi needed)
      narrative_runtime/          embedding API, HTTP server, command line
      run.py  requirements.txt  README.md
"""
from __future__ import annotations

import datetime
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

import yaml

from core_engine import Genome
from core_engine.evolution import LearningMemory
from narrative_runtime.engine import FORMAT
from utils.config_loader import config
from utils.tag_similarity import export_tag_vectors

from .errors import AuthoringError
from . import records
from .sessions import RUNS, SESSIONS, SESSION_LOCK
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


def _guide(name: str) -> str:
    return f"""# {name}: offline Adaptive Narrative engine

An evolved storyteller with its world, ready to run without the studio,
the REST API or an internet connection. Python 3.10+ and the packages in
`requirements.txt` (`pip install -r requirements.txt`) are all it needs.

## Try it

    python run.py info          # what is inside
    python run.py play          # play in the terminal
    python run.py serve         # JSON server on http://127.0.0.1:8765 for any game engine
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
game.save("slot1.game")   # engine.load_game("slot1.game") continues later
```

## Any other engine (Unity, Godot, Unreal, web): HTTP + JSON

Start `python run.py serve --port 8765` next to the game (or as a sidecar
process), then:

| Request | Body | Returns |
| --- | --- | --- |
| `GET /engine` | | engine information |
| `POST /games` | `{{"profile": "p1", "stats_mode": "random"}}` (optional `"start"`: one of `starting_locations`) | the first view, with `game_id` |
| `GET /games/<id>` | | the current view |
| `POST /games/<id>/choose` | `{{"action": "Explore harbor"}}` or `{{"action": 0}}` | the turn text and the new view |
| `GET /games/<id>/transcript` | | the story so far |
| `DELETE /games/<id>` | | ends the session |
| `POST /evolve` | `{{"generations": 5}}` | offline training summary |

Unity (C#):

```csharp
var request = new UnityWebRequest("http://127.0.0.1:8765/games", "POST");
request.uploadHandler = new UploadHandlerRaw(System.Text.Encoding.UTF8.GetBytes("{{\\"profile\\":\\"p1\\"}}"));
request.downloadHandler = new DownloadHandlerBuffer();
request.SetRequestHeader("Content-Type", "application/json");
yield return request.SendWebRequest();   // request.downloadHandler.text is the view (JSON)
```

Godot (GDScript):

```gdscript
var http = HTTPRequest.new(); add_child(http)
http.request("http://127.0.0.1:8765/games/" + game_id + "/choose",
    ["Content-Type: application/json"], HTTPClient.METHOD_POST, JSON.stringify({{"action": 0}}))
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
                  memory=None) -> dict:
    """Build the offline engine ZIP; returns {"path", "manifest"}.

    The storyteller comes from ``source``: "run" (``run_id``, default the latest
    run; ``generation`` for a generation's champion instead of the run's best),
    "memory" (the best remembered storyteller) or "game" (``token``'s game).
    Passing ``genome`` exports that storyteller directly (e.g. from main.py)."""
    if genome is not None:
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
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name or f"narrative-engine-{stamp}").strip("-") or "narrative-engine"
    active = config.as_dict()
    world = active.get("world_definition") or {}
    manifest = {
        "format": FORMAT,
        "version": 1,
        "name": name,
        "engine_id": engine_id,
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
    }
    staging = Path(tempfile.mkdtemp(prefix="narrative-export-"))
    root = staging / name
    try:
        for relative in RUNTIME_CODE:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(PROJECT_ROOT / relative, target)
        (root / "config").mkdir()
        (root / "config" / "engine.yaml").write_text(
            yaml.safe_dump(active, sort_keys=False, allow_unicode=True), encoding="utf-8")
        (root / "data").mkdir()
        (root / "data" / "tag_vectors.json").write_text(json.dumps(export_tag_vectors(world_tags(active))), encoding="utf-8")
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
        (root / "requirements.txt").write_text("\n".join(manifest["requirements"]) + "\n", encoding="utf-8")
        (root / "run.py").write_text(
            '"""Run this engine: python run.py info | play | serve | evolve"""\n'
            "from narrative_runtime.__main__ import main\n\nif __name__ == \"__main__\":\n    main()\n",
            encoding="utf-8")
        (root / "README.md").write_text(_guide(name), encoding="utf-8")
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
