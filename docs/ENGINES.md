# Engines: export, models, records, outputs

What a training produces and where it goes: the engine exported for games (the ANE AI and the
Python version), engine model files, the records database and the run archives.

## Exporting the engine for games

After a generation or a complete training, you can export the whole engine
as a ZIP that **runs offline** and **integrates in a game**:

- **Studio:** **📦 Export**. Choose *This run's best*, *A generation of this run* (any
  generation's champion), *Best remembered storyteller*, or *This game's
  storyteller*, and whether to include the learning memory.
- **API:** `POST /v1/exports` with
  `{"source": "run" | "memory" | "game", "run_id", "generation", "session_id", "include_memory", "name", "platform"}`
  returns a `download_url`. `GET /v1/runs` lists the runs you can export, and
  every simulation response carries its `run_id`. `GET /v1/exports/platforms`
  lists what it can be exported for.
- **Terminal:** `python main.py` writes `runs/<timestamp>/offline_engine.zip`
  after every run.

**What runs it** (`platform`, *Runs on* in the studio):

| `platform` | The download |
| --- | --- |
| `windows-x64`, `linux-x64`, `macos-arm64`, `macos-x64` | **The ANE AI**: a standalone program (`ane-ai`, no Python needed) with the engine package; a UNP (a game, a tool) starts it, talks to it over HTTP on 127.0.0.1 and stops it. Made from a complete engine. See [docs/UNP.md](UNP.md) |
| `unity` | A `.unitypackage`: the Unity library ([unity/com.ane.ai](../unity/com.ane.ai/README.md)) with the engine package and the runtimes |
| `python` (default) | The engine as Python code, described below |

The ANE AI (a system or Unity) is made from a **complete engine**: the engine model a finished
training saves, or a loaded engine, with its own world, storyteller, training state and tag
vectors. It can be downloaded once one exists: in the studio, *Runs on* a system or Unity lists
the complete engines (and the export stays off until there is one); in the API,
`GET /v1/exports/engines` lists them and `POST /v1/exports` takes `source: engine` or
`source: run` (409 otherwise). The Python version can be made from any storyteller.

The standalone runtimes are built by CI for each system (`.github/workflows/runtime.yml`,
`packaging/`), shipped in the API's Docker image and attached to each release. A platform is
offered only when its runtime was built from the API's own engine code.

The Python ZIP contains only what the engine needs:

| Path | What it is |
| --- | --- |
| `engine.json` | Manifest: the evolved storyteller (genome), its benchmark score, the learning history, a world summary |
| `config/engine.yaml` | The complete active configuration: world, tuning, messages, stats, missions, rules |
| `data/tag_vectors.json` | The tag vectors the storyteller was trained with |
| `saves/evolution_memory.json` | The learning memory (optional), to keep training offline |
| `core_engine/`, `utils/` | The engine code, without the studio, the API, or the LLM augmenter |
| `narrative_runtime/` | Embedding API, HTTP/JSON server, command line |
| `run.py`, `requirements.txt`, `README.md` | Entry point, dependencies (`pyyaml`, `numpy`), integration guide |

**It behaves exactly like the trained engine.** Tag similarity drives NPC
placement and routing, and it normally depends on which ML libraries are
installed. The export therefore ships the exact vectors used during training
and needs neither torch, sentence-transformers, nor scikit-learn. On the
same seed, an exported engine produces the same story, word for word, as
the studio engine (checked in the tests).

**Integrate it:**

```python
from narrative_runtime import NarrativeEngine          # Python games (e.g. Pygame, Ren'Py)
engine = NarrativeEngine("path/to/export")
game = engine.new_game(profile="player-1", stats_mode="random")   # start="Tavern" picks the start
view = game.view()               # location, start_location, description, npcs, items, actions, health, mission, stats, ...
view = game.choose(view["actions"][0])                  # by name or index; view["turn"] = story text
game.save("slot1.json")       # a JSON save game (ane.savegame/1); engine.load_game(...) continues it
```

For Unity, Godot, Unreal, or web games, export the ANE AI for the player's
system (or run `python run.py serve --port 0` next to the game) and use HTTP +
JSON: the world as parameters (`GET /world`), sessions (`POST /games`,
`POST /games/<id>/choose`, `GET /games/<id>`), the player's state and what
happened in the game (`PATCH /games/<id>/player`, `POST /games/<id>/sync`),
world content (`POST /world/patch`), save and load games
(`POST /games/<id>/save`, `.../cancel`, `POST /games {"savegame": ...}`) and a
clean stop (`POST /shutdown`). Every request carries the token from the ready
line. [docs/UNP.md](UNP.md) has the whole protocol, and C# and Python code;
Unity games use the Unity library. The export's README has Unity (C#) and Godot (GDScript) snippets. `python run.py play` plays in a
terminal. `python run.py evolve` keeps training offline from the shipped
memory, and adopts the new storyteller only when it beats the current one
on the same stories. Persistent variables and each profile's recent starts
are saved per profile in the export's `saves/`.

## Saved engine models

A trained engine is saved as one **engine model file** (`.ane.zip`,
[`core_engine/model.py`](../core_engine/model.py)). It holds everything needed
to play it, regenerate its world, and train it further:

| File | What it holds |
| --- | --- |
| `manifest.json` | format, version, id, parent engine, source, score, creation time, a fingerprint of the engine code, a world summary, and a SHA-256 of every file below |
| `config.yaml` | the complete configuration: the world and all tuning |
| `genome.json` | the storyteller |
| `training.json` | the complete training state: the whole population for the next generation, the generation reached, mutation step, stagnation count, random-generator state, the fixed benchmark and situations, every generation's statistics and champions |
| `memory.json` | the engine's own hall of fame (where retraining starts) |
| `tag_vectors.json` | the tag vectors it was trained with, so it behaves the same on any machine |
| `best_model.yaml` | the storyteller in the older bundle format, so older loaders still read the file |

Every run in the studio, the API and the terminal produces one: it is the
studio's engine download, `engine_download_url` in the API, and
`engine_model.ane.zip` in a terminal run's archive. Loading checks the format,
version, checksums, genome, and world, and warns when the engine code has
changed since training.

| To | Studio | REST API | Terminal | Python |
| --- | --- | --- | --- | --- |
| Play it | Load a saved engine, or Records → Play | `POST /v1/engines/load`, `POST /v1/records/engines/{id}/play` | `python main.py --play ENGINE` | `EngineModel.load(path).new_game()` |
| Continue its training exactly | Records → Continue its training | `POST /v1/records/engines/{id}/continue` | `python main.py --continue-training ENGINE --generations N` | `model.continue_training(N)` |
| Retrain it (new run from it) | Records → Retrain from it | `POST /v1/records/engines/{id}/retrain` | `python main.py --retrain ENGINE` | `model.retrain(settings)` |
| Generate its world | Records → Download its model file and world | `GET /v1/records/worlds/{id}?format=yaml` | `python main.py --export-world ENGINE --out world.yaml` | `model.build_world()`, `model.world_yaml()` |
| Use it as a game engine | Export the engine (or source `engine`) | `POST /v1/exports` with `source: engine` | `offline_engine.zip` | `export.export_engine("engine", engine_id=...)` |

`ENGINE` is a model file or a recorded engine id.

- **Continuing is exact.** Training restored from a model file continues as if
  it had never stopped: two generations, saved, loaded in another process, and
  continued for two more give the same storytellers, scores, and history as
  four uninterrupted generations (checked in the tests). The engine's own world
  and tag vectors are activated first, whatever configuration is active.
- **Retraining** starts a new run on the engine's world, with its first
  population seeded from the engine's storyteller and memory.
- **Lineage.** A continued or retrained engine records its parent engine.
- **Offline.** An offline export of an engine carries its training state:
  `python run.py evolve --resume --generations N` continues it exactly, and
  `POST /evolve` takes `"resume": true`.
- **Benchmark fix.** Without a seed, the validation benchmark used to be drawn
  again on every call; it is now drawn once per run and saved with the
  training state.

## Records database

Every story, run, world, and engine generated by the studio (`app.py`), the
REST API (`api.py`), and the terminal (`main.py`) is recorded in one SQLite
database, so worlds and trained engines can be found and used again. Every run
has the user it was made for (`runs.user_id`): `test` for the studio, the
terminal and runs recorded before users existed; for the API, the opaque id the
calling backend gives in `X-ANE-User` ([docs/API.md](API.md)). A database made
before is given the column the first time a process that may write it opens it,
after a backup next to it (`ane.sqlite3.before-user-id-<time>`); one that is read
only shows its runs as `test`.

```
/data/angelisg/ane/            records folder (ANE_RECORDS_DIR; ANE_RECORDS=off turns recording off)
  ane.sqlite3                  the database
  models/<engine id>.ane.zip   engine model files
  exports/                     copies of offline engine exports
  imports/<kind>/              files brought in by --import-artifacts
```

**Importing earlier files.** `python main.py --import-artifacts [FOLDER]`
(default: the `artifacts/` folder) records every engine and game found in a
folder: engine models (`.ane.zip`), offline engine exports, older bundles
(`config.yaml` + `best_model.yaml`), and generated story configurations. Each
is read and checked; what it lacks is filled in: its world gets the tuning
it misses from the current defaults (its own values win), its tag vectors are
computed, a missing score is measured on the benchmark, its training run and
generations are rebuilt from its learning summary, and a preview story is
played and recorded. A file whose world and storyteller are already recorded
is linked to that engine. The files are moved into `imports/<kind>/` of the
records folder (`--copy` copies them); files that are not engines or games
stay where they are.

| Table | One row per | Holds |
| --- | --- | --- |
| `worlds` | configuration used | the world and all tuning (`config_json`), identified by its content, so it is stored once |
| `runs` | evolution run (new, continued, retrained) | origin (`studio`, `api`, `terminal`), every resolved setting, the request parameters, seed, status, best score and engine, the learning summary, the parent engine |
| `generations` | generation of a run | best, mean, worst, spread, diversity, mutation step, benchmark, improvement, the champion's traits and engine |
| `engines` | storyteller | a run's best (with its model file), every generation's champion, loaded, continued, and retrained engines; score, genome, model file and checksum, parent |
| `stories` | generated story | its kind (`preview`: the run's benchmark story, `play`: a played game, `terminal`), session, seed, profile, start, mission, stats, parameters, status, result (end, win, health, coins, path, inventory, variables, events), fitness and its breakdown, and the transcript |
| `turns` | turn of a story | the action and where it came from, the check, success, the outcome, the place before and after, the event, health, coins, the end, and the turn's text |
| `artifacts` | file written | configuration YAMLs, stories, offline exports, model files, with checksum and size |

- **When.** A run is recorded when it starts (status `running`) and when it
  ends (`finished` or `failed` with the error). A played game is recorded when
  it starts and after every turn; the turn log comes from `GameState.turn_log`,
  which every story now keeps.
- **Never in the way.** Recording is best effort: if the folder cannot be
  written, recording switches itself off with a warning and stories, games, and
  training carry on. Paths inside the folder are stored relative to it, so it
  can be moved or mounted elsewhere. Training episodes inside evolution are
  not stored one by one (a terminal run plays hundreds of thousands); their
  generation statistics and champions are.
- **Reading.** `GET /v1/records`, `/v1/records/runs[/{id}]`,
  `/v1/records/worlds[/{id}]` (`?format=yaml` gives a configuration that
  `POST /v1/config/upload` accepts), `/v1/records/engines[/{id}]`,
  `/v1/records/engines/{id}/model`, and `/v1/records/stories[/{id}]` (with
  every turn; filter by run, engine, kind, origin, or session). In the studio:
  **🧠 Train → From a recorded engine** (continue, retrain), **⚖ Evaluate** (recorded
  stories), **📦 Export** (a recorded engine's, run's or world's files) and **⚙ Advanced →
  🗂 Records** (recording, the server's import folder). In the terminal:
  `python main.py --list-engines`. In Python: `backend.records`.
- **Docker.** `compose.yaml` mounts `${ANE_RECORDS_HOST_DIR:-/data/angelisg/ane}`
  at the same path. The container runs as uid 10001, so grant it write access
  to the host folder (for example `setfacl -m u:10001:rwx /data/angelisg/ane`),
  or recording stays off.
- **Tests** record into a temporary folder (`tests/records_sandbox.py`), never
  into the real database.

## Outputs

Each generation run creates a timestamped folder:

```text
runs/YYYYMMDD_HHMMSS/
```

The folder contains:

```text
best_world.yaml
best_story.txt
story_YYYYMMDD_HHMMSS.txt
```

The story file records the chosen mission and whether it was completed.

The latest best artifacts are also copied to:

```text
runs/best_world.yaml
runs/best_story.txt
```

Augmented worlds are written under `augmentations/`. If `augmentation.save_to_source` is true, augmentation can also update files under [`config/`](../config/), so review diffs after augmentation.

## Generated configuration and older bundles

After a simulation finishes, the **Generated story configuration** download
contains the active combined configuration together with a `generated_run`
section recording the winning genome, score, population, generations, and
simulation-step count. This YAML can be retained with the story or uploaded
again as a complete configuration.

The app also creates a data-only **Best model bundle** ZIP containing that
configuration and the winning evolved model parameters. It does not include a
snapshot of the application source code. A bundle from an earlier run can be
uploaded under **Load a previous best engine** to skip evolution and begin a
game with its winning model. You can also upload a complete configuration YAML
and a separate model-parameters YAML. The parameters file may contain a
top-level `genome` mapping, a `generated_run.genome` mapping, or the genome
fields directly. Loading either format starts the playable story immediately
without running evolution.
