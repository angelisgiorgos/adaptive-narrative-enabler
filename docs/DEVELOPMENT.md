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

Tests never write to the real records database or saves: `tests/records_sandbox.py` sends them to
a temporary folder, and `ANE_SAVE_DIR` can point saves elsewhere.

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
python packaging/docker_smoke_test.py --api http://127.0.0.1:8004 --ui http://127.0.0.1:8003
```

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
`runtimes/`): CI puts them in the Docker image, and a `v*` tag attaches them to a GitHub release
([`release.yml`](../.github/workflows/release.yml)); in a deployment, download the ones that match
the image into that folder.

## Notes

- Record every change in [CHANGELOG.md](CHANGELOG.md) under **Unreleased**, in the section of
  the area it touches, in the same commit.
- Keep version control active when using augmentation, especially with
  `augmentation.save_to_source: true`.
- For reproducible runs, set `reproducibility.seed` in `config/hyperparameters.yaml`.
- For faster debugging, lower `pop_size`, `generations` and `episodes_per_genome`, or press `q`
  during generation to continue with the best storyteller so far.
