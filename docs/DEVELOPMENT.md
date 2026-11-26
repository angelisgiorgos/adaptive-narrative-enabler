# Development

The code's layout, the tests, continuous integration, and how the ANE AI's standalone runtimes
are built and released.

## Repository layout

```text
adaptive-narrative-enabler/
|-- main.py                  # Terminal entry point: world building, evolution, archives
|-- app.py                   # Gradio studio: user interface only, a client of the REST API (StudioAPI)
|-- api.py                   # REST API: every service of the studio, calls the backend
|-- backend/                 # Behind the API
|   |-- store.py             # Configuration I/O and validation, settings, weights, rules, engine logic, app parameters
|   |-- content.py           # World authoring: entries, actions, missions, messages, variables, stats
|   |-- sessions.py          # Evolution runs, engines, play sessions, background jobs
|   |-- records.py           # Records database: runs, worlds, engines, stories, turns
|   |-- evaluation.py        # The judge's benchmarks and the fitness calibration
|   `-- export.py            # Engine exports: the ANE AI (standalone, Unity) and the Python version
|-- core_engine/             # The engine: world, game state, storyteller, evolution, fitness, rules
|   |-- builder.py, world.py, game_state.py, story_beats.py, scope.py, starts.py, rules.py
|   |-- actions.py, conditions.py, effects.py, variables.py, stats.py, combat.py, health.py, narration.py
|   |-- genome.py, evolution.py, fitness.py, model.py (engine model files), augmenter.py (optional LLM)
|   |-- progression.py       # Session reports and the profile's save game
|   |-- snapshot.py, resume.py, savegame.py   # Save games: exact snapshot, parameters, ane.savegame/1
|   `-- content_rules.py, version.py          # World checks shared with the backend; the code's version
|-- narrative_runtime/       # The ANE AI: NarrativeEngine (engine.py), the HTTP server for the UNP
|                            # (server.py), the world for the UNP (world.py), lifecycle and events, the CLI
|-- evaluator/               # Gemma 4 as a judge, the judge benchmark, fitness calibration, LLM evaluation
|-- utils/                   # Configuration loader, tag similarity, LLM loader and suggester, terminal editor
|-- config/                  # The world and every parameter (YAML)
|-- examples/                # Example worlds (world-only files): a frozen monastery, an orbital station
|-- packaging/               # ANE AI builds (PyInstaller) and their smoke test; the Docker image's smoke test
|-- unity/com.ane.ai/        # Unity library (C#): installs, starts, drives and stops the ANE AI
|-- tests/                   # One unittest module per area (records go to a temporary folder)
|-- docs/                    # The documentation (README.md lists it)
|-- .github/workflows/       # CI, the runtime builds, releases
|-- Dockerfile, compose.yaml, docker-entrypoint.sh
`-- requirements.txt, environment.yml
```

Generated, not in the repository: `runs/` (run archives), `augmentations/`, `saves/` (profiles,
learning memory, records), `artifacts/` and `runtimes/` (standalone runtimes for exports).

## Tests

Each test module runs on its own, from the repository root:

```bash
python -m unittest tests.test_<name>
```

Tests never write to the real records database, saved games or saves: `tests/records_sandbox.py`
sends them to temporary folders (`ANE_RECORDS_DIR`, `ANE_SESSIONS_DIR`), and `ANE_SAVE_DIR` can
point saves elsewhere.

## Continuous integration

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs on every push and pull request
(changes to Markdown and `docs/` only are skipped; it can also be started by hand):

- **Tests**: Python 3.10 (the image's), CPU-only PyTorch, every module compiled, and every test
  module run in its own process, in parallel; the logs of a failed run are kept as an artifact.
- **ANE AI runtime** ([`runtime.yml`](../.github/workflows/runtime.yml)): the standalone runtime
  built for windows-x64, linux-x64, macos-arm64 and macos-x64, each smoke-tested through the
  whole protocol like a UNP drives it ([`packaging/smoke_test.py`](../packaging/smoke_test.py)).
- **Unity library (C#)**: the library compiled outside Unity (`unity/com.ane.ai/Tests~/Build`)
  and its client driving a real ANE AI (`Tests~/Protocol`).
- **Docker image**: `docker compose build`, `docker compose up --wait` (the API and the studio as
  deployed, with the runtimes), then
  [`packaging/docker_smoke_test.py`](../packaging/docker_smoke_test.py): health, missions, a
  short evolution that starts a game, one turn, the run recorded, an ANE AI exported from the
  run's engine and its own smoke test.

Against a running deployment:

```bash
python packaging/docker_smoke_test.py --api http://127.0.0.1:8005 --ui http://127.0.0.1:8003
python packaging/docker_smoke_test.py --api http://ane.hamlet.iti.gr --ui ''   # the public API (records the run)
```

The studio (`app.py`) only talks HTTP to an API (`studio_client.py`). The tests import
`tests/records_sandbox.py`, which gives it the API served in the test process instead
(`studio_client.use_test_client`), unless a test sets `ANE_API_URL`.

## Building the ANE AI runtimes

A runtime is the engine code frozen with PyInstaller (onedir) for one system; an export puts an
engine package next to it ([UNP.md](UNP.md)). In a clean environment with only `pyinstaller`,
`pyyaml` and `numpy`:

```bash
python packaging/build_runtime.py --target linux-x64 --dist dist    # dist/ane-runtime-linux-x64.zip
python packaging/make_package.py --out pkg                          # a test engine package
python packaging/smoke_test.py --folder <runtime folder with the package>
```

`runtime.json` in the zip records its target and `code_version` (a hash of the engine code). The
API exports a runtime only when its `code_version` is its own, so rebuild after changing the
engine code. The API looks for `ane-runtime-<platform>.zip` in `ANE_RUNTIMES_DIR` (default
`runtimes/`). The Docker image builds the Linux one itself (the `ane-runtime` stage of the
[`Dockerfile`](../Dockerfile), on Debian 11 so the program starts on Linux systems with glibc 2.31
or later), so every image exports `linux-x64` and `unity`. Windows and macOS runtimes can only be
built on those systems: CI builds them, and a `v*` tag attaches them to a GitHub release
([`release.yml`](../.github/workflows/release.yml)); put the ones of the same commit in `runtimes/`
before building the image.

## Keeping the studio fast

The studio is one large Gradio page (about 1,500 components and 570 events), and Gradio's page does
work in proportion to that size. Three things keep it responsive (`app.py`, end of `build_app` and the
launch):

- **No API description in the page.** Gradio embeds a description of every public event in each page
  (`gradio_api_info`: 11 MB here). The studio's events are its own page's (the API is the API to call),
  so they are `api_visibility="undocumented"`: callable by the page, left out of that description.
- **Small loading-status bookkeeping** (`lighten_loading_status`). Gradio's page keeps a loading status
  for every component an event has ever shown progress on, and updates all of them, searching the whole
  component tree for each, on every status message of any event. Events whose progress is hidden show
  it on nothing (`show_progress_on=[]`); the others on their outputs that can show it (not states,
  timers or layout blocks). The Weights load and the entry editor's first load, on opening the page,
  show their progress on their status lines.
- **Compressed pages** (`CompressPages`). Gradio compresses files with an extension (`.js`, `.css`);
  the page and its `/config` (about 2 MB each) are gzipped too. Event streams are never compressed.

Measured in headless Firefox on the studio's server (no GPU for the browser): the page went from 13 MB
to 0.2 MB, ready from 18 s to 3.5 s, a click to another destination from 4–38 s to 1.3–2.6 s, and the
page's main thread from busy all the time to idle between events. A new event with many outputs and a
visible progress keeps adding to the bookkeeping: give it `show_progress_on` (the components where its
progress means something) when its outputs are many.

## Notes

- Record every change in [CHANGELOG.md](CHANGELOG.md) under **Unreleased**, in the section of
  the area it touches, in the same commit.
- World augmentation never writes the configuration by itself: `python main.py` keeps a proposal
  for review, and `python main.py --apply-augmentation FILE` adds it (checked as the studio checks
  it). Keep version control active anyway, and review the diff after adding one.
- For reproducible runs, set `reproducibility.seed` in `config/hyperparameters.yaml`.
- For faster debugging, lower `pop_size`, `generations` and `episodes_per_genome`, or press `q`
  during generation to continue with the best storyteller so far.
