# Adaptive Narrative Enabler

The Adaptive Narrative Enabler (ANE) generates interactive stories from a world written in YAML:
places, NPCs, items, events and missions. An evolutionary AI learns a **storyteller**, a genome of
traits that decides how a story moves, what it reveals and when it raises the stakes. The trained
engine plays the world as a game, in the studio or offline inside another game as the **ANE AI**.

## What it does

- **World authoring** in YAML or the studio's forms: places, NPCs (unique or generic, hostile,
  roles, secrets), items, events, staged missions, a map, character stats with 1–100 skill checks,
  persistent variables, and every sentence the engine writes ([WORLD.md](docs/WORLD.md)).
- **One condition and effect language** for every `enabled_if`, action, event, dialogue line and
  mission goal.
- **An evolutionary storyteller AI**: the storyteller directs (the world it builds, who is where,
  events, which choices are offered) while simulated players choose, as people do. Its fitness (a
  training objective of operational measures: arc, setup and payoff, variety, pacing, coherence) is
  not a measure of quality; a language-model judge and playtests assess that ([AI.md](docs/AI.md)).
- **The studio** (Gradio) and **a REST API** (FastAPI) with the same services: edit, train, play,
  record, judge and export ([STUDIO.md](docs/STUDIO.md), [API.md](docs/API.md)).
- **Engine models and records**: every run saves a complete engine (world, storyteller, training
  state, tag vectors) that can be played, continued or retrained ([ENGINES.md](docs/ENGINES.md)).
- **Gemma 4 on vLLM** as an optional language model: world augmentation, editor suggestions, and a
  judge of stories from a player's view ([LLM.md](docs/LLM.md)).
- **The ANE AI**: a complete engine exported as a standalone program for Windows, macOS or Linux,
  or as a Unity package, that a game (the UNP) launches, drives over local HTTP, saves, loads and
  stops, offline ([UNP.md](docs/UNP.md)).

## Quick start

```bash
conda env create -f environment.yml          # or: pip install -r requirements.txt (Python 3.10+)
conda activate adaptive-narrative-enabler

python app.py                                # the studio (it serves the API inside its process)
python -m uvicorn api:app --port 8004        # the REST API on its own (docs at /docs)
python main.py                               # the terminal generator
```

With Docker, `docker compose up --build` serves the studio at `http://localhost:8003` and the API
at `http://localhost:8004/v1` (interactive documentation at `/docs`).

In the studio: create the world (*🌍 World*), tune it (*⚙ Settings*), train the AI and export it
for a game (*🧠 Train & export*), then **play your world** in *▶ Play*: choose a trained AI (a
finished run), a recorded engine or an engine file, start a new game or continue a save game, and
play it, choice by choice. *⚖ Evaluate* judges the stories.

## Documentation

| Guide | What it covers |
| --- | --- |
| [docs/STUDIO.md](docs/STUDIO.md) | Running the studio, the API, Docker and the terminal; every studio tab |
| [docs/WORLD.md](docs/WORLD.md) | Authoring a world: the configuration files and every world feature |
| [docs/AI.md](docs/AI.md) | The evolutionary AI, the storyteller's decisions, the director, sessions, story rules |
| [docs/ENGINES.md](docs/ENGINES.md) | Engine exports, engine model files, the records database, run outputs |
| [docs/UNP.md](docs/UNP.md) | The ANE AI for a game: download, launch, protocol, save and load, stop, Unity |
| [docs/API.md](docs/API.md) | The REST API: how it behaves, every endpoint, examples |
| [docs/LLM.md](docs/LLM.md) | Gemma 4 on vLLM, and the judge |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Repository layout, tests, CI, building the ANE AI runtimes |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | Every change |

## Repository

```text
main.py, app.py, api.py    terminal, studio, REST API
backend/                   behind the API: configuration, content, sessions, records, exports
core_engine/               the engine: world, game state, storyteller, evolution, fitness
narrative_runtime/         the ANE AI: offline engine, HTTP server for the UNP, CLI
evaluator/, utils/         the judge and LLM evaluation; configuration, tag similarity, LLM loader
config/, examples/         the world and its tuning; example worlds
packaging/, unity/         ANE AI builds and smoke tests; the Unity library
tests/, docs/              tests; documentation
```

Tests: `python -m unittest tests.test_<name>` from the repository root
([DEVELOPMENT.md](docs/DEVELOPMENT.md)). Record every change in
[docs/CHANGELOG.md](docs/CHANGELOG.md).
