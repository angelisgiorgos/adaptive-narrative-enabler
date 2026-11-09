# Adaptive Narrative Enabler

Adaptive Narrative Enabler is a Python prototype for generating interactive narrative runs from modular YAML content. It builds a world from locations, NPCs, items, actions, and outcomes, then evolves genome-like control parameters that influence movement, discovery, threat, recovery, setup/payoff, and success behavior.

The main entry point is [`main.py`](main.py). The studio ([`app.py`](app.py)) is only the user interface and a client of the REST API ([`api.py`](api.py)): every tab works through the API's endpoints, and all logic lives behind the API in the backend ([`backend/`](backend/), [`core_engine/`](core_engine/)). Content and tuning live in [`config/`](config/). Generated stories, best worlds, and per-run archives are written to [`runs/`](runs/).

## Features

- Modular story-world authoring through YAML files.
- Evolutionary search over narrative-control genomes.
- Multi-core genome scoring with `ProcessPoolExecutor`.
- Configurable fitness scoring for dramatic arc, scene contrast, setup/payoff, volatility, path variety, character interaction, combat, discovery, and ending quality.
- Optional interactive terminal editor for browsing and editing story setup.
- Optional LLM support for world augmentation, editor suggestions, and final narrative evaluation.
- Automatic public-model fallback when the configured Hugging Face model is gated or inaccessible.
- Per-run timestamped archives containing the best world, best generated story, and final transcript.
- Authored Events: self-contained situations with their own exclusive actions.
- Missions: randomly chosen goals that give every story a win condition and end it when achieved.
- Varied starting locations: each story starts at a weighted, writer-approved location from which its mission can be reached, and a player's recent starts are drawn less often.
- Unique NPCs live in a single location for the whole game; generic NPCs (e.g. guards) appear wherever they fit. Unique items exist once per story, in one place.
- Event rules enforced for every way into an event: eligibility, `repeat` (once, per location, per visit), maximum occurrences, cooldown, and authored random scheduling (`chance`, `weight`).
- Hostile NPCs (e.g. bandits) confront the player with an encounter event the moment they are in the scene.
- One-time, per-visit, and always-available options, plus exclusive choices (bribe *or* fight the guard).
- Every sentence the engine writes is customisable, with variants that rotate instead of repeating.
- Play decisions are deterministic and genome-driven; evolution also learns to avoid repetitive stories.
- Persistent variables carry over between playthroughs and switch locations, NPCs, items and missions on or off.
- One condition language (`all` / `any` / `not` over variables, stats, coins, items, places, NPCs, events) for every `enabled_if`, action, event, dialogue line and mission goal, and one effect list for outcomes, dialogue and mission rewards.
- Writer-defined character stats (any names) with a point budget, and 1-100 skill checks resolved by a combat system.

## Repository Layout

```text
adaptive-narrative-enabler/
|-- main.py                         # Terminal entry point, world building, fitness, archives
|-- app.py                          # Gradio studio: user interface only, a client of the REST API (StudioAPI)
|-- api.py                          # REST API: every service of the studio, calls the backend
|-- backend/                        # Backend of the API
|   |-- store.py                    # Configuration I/O, validation, settings, weights, rules, engine logic, app parameters
|   |-- content.py                  # World authoring: entries, actions, missions, messages, variables, stats
|   |-- sessions.py                 # Games created by evolution, bundles, play sessions, background jobs
|   |-- records.py                  # Records database: runs, worlds, engines, stories, turns
|   `-- export.py                   # Offline engine export for games
|-- narrative_runtime/              # Offline runtime shipped in exports: embedding API, HTTP server, CLI
|-- examples/worlds/                # Example storyworlds (world-only files): a frozen monastery, an orbital station
|-- docs/
|   |-- API.md                      # REST API: how it behaves, every endpoint, examples
|   `-- CHANGELOG.md                # Every change, under Unreleased until released
|-- config/
|   |-- hyperparameters.yaml        # Evolution, fitness, LLM, editor, augmentation settings
|   |-- events.yaml                 # Event definitions (self-contained situations)
|   |-- messages.yaml               # Every sentence the engine writes, customisable
|   |-- variables.yaml              # Persistent variables carried across playthroughs
|   |-- stats.yaml                  # Character stats, point budget, tag checks
|   |-- missions.yaml               # Mission goals / win conditions
|   |-- locations.yaml              # Location definitions and location actions
|   |-- items.yaml                  # Object/item definitions and item actions
|   `-- npcs.yaml                   # Character definitions and NPC actions
|-- core_engine/
|   |-- actions.py                  # Action/outcome resolution
|   |-- augmenter.py                # Optional LLM world augmentation
|   |-- evolution.py                # NarrativeEvolution: the evolutionary AI, learning memory
|   |-- builder.py                  # Builds the runtime world from the configuration
|   |-- fitness.py                  # The fitness function evolution maximises
|   |-- game_state.py               # Runtime story state and simulation loop
|   |-- health.py                   # HealthPlayer: bounded health and power of the player
|   |-- stats.py                    # CharacterStats: writer-defined stats, point budget, allocation
|   |-- combat.py                   # CombatSystem: skill checks, difficulty, combat damage
|   |-- narration.py                # Default story messages and the message formatter
|   |-- variables.py                # Persistent variables: conditions, changes, storage
|   |-- genome.py                   # Evolvable narrative parameters
|   `-- world.py                    # World graph, spawn rules, entities
|-- evaluator/
|   `-- llm_eval.py                 # Structural and optional LLM evaluation
|-- utils/
|   |-- config_loader.py            # Deep-merges config YAML files
|   |-- config_editor.py            # Terminal config/story editor
|   |-- llm_model_loader.py         # Shared LLM loader with fallback support
|   |-- llm_suggester.py            # Optional editor suggestions
|   |-- tag_similarity.py           # Cached semantic/fallback tag similarity
|   |-- visualize.py                # Optional graph visualization
|   `-- generate_presentation.py    # Optional project-summary presentation
|-- augmentations/                  # Generated augmentation snapshots
|-- runs/                           # Generated story and world archives
`-- environment.yml                 # Conda environment definition
```

## Setup

Create the Conda environment:

```bash
conda env create -f environment.yml
conda activate adaptive-narrative-enabler
```

Or with Mamba:

```bash
mamba env create -f environment.yml
mamba activate adaptive-narrative-enabler
```

The environment includes the core runtime packages plus optional local LLM and visualization dependencies.

## Running

Start the application:

```bash
python main.py
```

Or launch the browser-based authoring and play studio:

```bash
python app.py
```

The studio does everything through the REST API. On its own it serves the API
inside its process; to use an API that is already running (shared with other
clients), point it there:

```bash
python -m uvicorn api:app --port 8004 &
ANE_API_URL=http://localhost:8004 python app.py
```

### Docker, UI, and REST API

Build and start the containerized studio:

```bash
docker compose up --build
```

The complete UI is available at `http://localhost:8003`, the REST API at
`http://localhost:8004/v1`, and interactive API documentation at
`http://localhost:8004/docs`. See [`docs/API.md`](docs/API.md) for how the API
behaves (state, validation, status codes, limits), endpoint coverage, and
request examples.

The Gradio studio edits the whole story world through forms, saves simulation
settings to the YAML configuration, runs a small candidate tournament, and
turns its best genome into a player-controlled game. Every editor validates
before saving.

**The studio is a client of the REST API.** Every tab works through the API's
endpoints ([`docs/API.md`](docs/API.md)); `app.py` imports none of the
backend, so whatever the studio can do, any other client of the API can do
too. `StudioAPI`, at the top of [`app.py`](app.py), is the connection:

- With `ANE_API_URL` set (Compose sets `http://hamlet-ane-api:8004`), the
  studio talks to that API over HTTP. The studio and other clients then share
  one engine: the same sessions, runs, learning memory, and records.
- Without it (`python app.py`), the studio serves the same API inside its own
  process, so it still runs on its own.

The studio says who it is (`X-ANE-Client: studio`), so its runs and games are
recorded with the origin `studio`. Long runs (evolution, continued or
retrained training) are background jobs that the studio polls, so the
progress bar still moves generation by generation. Files the API offers
(story configuration, engine model, exports, recorded worlds) are downloaded
for the file widgets. A refusal from the API (an invalid entry, a broken
condition, an unknown engine) is shown as the studio's error message, and
nothing is written.

| Studio | What you can change | REST API |
| --- | --- | --- |
| **1 · World editor** → an entry | Name, tags, descriptions, goals; NPC **Unique**, **Encounter event**, **Repeat on every visit**; **Enabled if** (locations, NPCs, items); **Event title**; item **Stat bonus while held**; location **Stories can start here** and **Start weight** | `GET /v1/entities/{kind}?fields=names`, `GET/PUT/DELETE /v1/entities/{kind}/{name}` |
| **1 · World editor** → *Actions & outcomes of this entry* | The entry's actions as YAML: `repeat`, `exclusive_group`, `triggers_event`, `skill_check`, requirements, and outcome effects (`on_check`, `change_stats`, `send_to`, `remove_npc`, `set_variables`, `change_variables`, …); one action's **Repeat** and **Retry after failure** | `GET/PUT /v1/entities/{kind}/{name}/actions/yaml` (or `/actions` as JSON), `GET/PUT /v1/entities/{kind}/{name}/actions/{index}/repetition` |
| **1 · World editor** → *Uniqueness & scheduling* | An event's start rules, an item's uniqueness | `GET/PUT /v1/entities/{Events\|Items}/{name}/policy/yaml` (or `/policy`) |
| **1 · World editor** → *Missions* | Missions, their goals, weights, and `enabled_if` | `GET/PUT /v1/documents/missions` (or `/v1/missions`) |
| **2 · Simulation settings** → *Complete configuration file* | Upload and activate a combined YAML, or go back to the built-in files | `POST /v1/config/upload`, `POST /v1/config/reset` |
| **2 · Simulation settings** | Population, steps, starting and **maximum health**, coins, reproducibility | `GET/PUT /v1/settings` |
| **2** → *Story rules* | Mission pacing and route weight, default repeat rules, arrest location and confinement, following new paths, persistence (default profile, save on, evolution values), event fallback | `GET/PUT /v1/rules` |
| **2** → *Persistent variables* | Variable definitions; a profile's stored values and playthroughs (editable), reset to a new player | `GET/PUT /v1/documents/variables`, `GET/PUT /v1/documents/stored_values?profile=`, `POST /v1/variables/reset/yaml?profile=`, `GET /v1/variables/profiles` |
| **2** → *Story messages* | Every sentence the engine writes | `GET/PUT /v1/documents/messages` (or `/v1/messages`) |
| **2** → *Character stats* | Stat names, point budget, allocation mode, tag checks, damage-reduction stat | `GET/PUT /v1/documents/stats` (or `/v1/stats`) |
| **2** → *Advanced · all hyperparameters* | Every hyperparameter as one YAML document | `GET/PUT /v1/hyperparameters` |
| **3 · Weights** → *Skill Check Resolution* and the rest | Difficulty (easier/harder), difficulty over time, chance floor/ceiling, damage mitigation, and every other weight | `GET/PUT /v1/weights` |
| **4 · Simulate & play** → evolution | Storytellers, generations, steps, **stories per storyteller**, **continue learning from earlier runs**; the **learning curve** and **what the storyteller learned**; forget the learning memory | `POST /v1/jobs/simulations` and `GET /v1/jobs/{id}` (or `POST /v1/simulations`), `GET/DELETE /v1/learning` |
| **4 · Simulate & play** → *Load a previous best engine* | An engine model, an older engine ZIP, or a configuration YAML with its model-parameters YAML | `POST /v1/engines/load` (`file`, `parameters`, `profile`, `stats_mode`, `stats`) |
| **4 · Simulate & play** | **Player profile** and the player's **character stats** (story default, random, balanced, or own points with Randomize/Balance); the scene shows mission, health `x/max`, stats, the last check, and remembered values; taking an action | `POST /v1/stats/allocate`, `POST /v1/sessions/{id}/actions` (structured state) |
| **4 · Simulate & play** → *Export the engine* | Download the trained engine to run offline in a game: this run's best, any generation's champion, the best remembered storyteller, or the current game's; with or without the learning memory | `POST /v1/exports`, `GET /v1/artifacts/{id}` |
| **4 · Simulate & play** → *Records* | Recorded engines, runs and stories; play with, continue or retrain an engine; download its model file and world | `GET /v1/records/...`, `POST /v1/records/engines/{id}/play`, `POST /v1/jobs/engines/{id}/continue\|retrain` |
| **5 · Engine logic** | Every parameter of the engine's logic, section by section (no code editing) | `GET /v1/logic`, `GET/PUT /v1/logic/{section}` |
| **5** → *App parameters* | The studio's limits, defaults, queue, and launch settings | `GET/PUT /v1/app-settings` |

What the studio needs to build its forms (entry kinds, suggested tags, the
action reference, repeat rules, goal keys, rule choices, stat choices) comes
from `GET /v1/reference`.

Choosing a new **Player profile** name on the play tab starts a new player.
Stored values can be edited in *Persistent variables*, for example
`playthroughs: 1`, to test what a returning player sees.

The **5 · Engine logic & app parameters** tab changes how the engine behaves
without editing code. The logic runs in the backend scripts (`core_engine/`,
`backend/`). Every constant that shapes a decision is a parameter in
`config/hyperparameters.yaml`: ranking weights, outcome objectives,
story-phase tags, discovery, NPC leads, scene visibility, skill checks,
fitness, and the evolutionary search itself.

- **Engine logic** edits one section at a time. Pick a section, read what it
  controls, and edit its parameters with the comments kept. A section is only
  saved when it keeps the same settings and value types, and misspelt or
  unknown settings are rejected, so an edit cannot break the engine. New
  simulations and games use it immediately.
- The same sections are available through `GET /v1/logic` and
  `GET|PUT /v1/logic/{section}`.
- **App parameters** covers the studio's session, upload, and work limits,
  simulation defaults, queue settings, and host/port/share. The slider limits
  go up to 400 candidates, 1000 generations, and 400 steps per simulation (the
  largest run the API accepts); `max_simulation_work` caps the total simulated
  turns of one run (candidates × generations × story length × stories per
  storyteller). These are stored in
  `config/ui_settings.yml` (`GET|PUT /v1/app-settings`). Restart the UI and
  the API after saving them. `ANE_UI_HOST`,
  `ANE_UI_PORT`, and `ANE_UI_SHARE` override the saved launch options.

The studio no longer edits Python files, and the REST API has no
code-editing endpoint.

#### Docker config volumes

Docker fills a named volume from the image only when the volume is first
created. The container's entrypoint
([`docker-entrypoint.sh`](docker-entrypoint.sh)) therefore compares the
mounted `/app/config` with the image's defaults in `/app/config.defaults` on
every start. It copies in any default `*.yaml` file that is missing, such as
`events.yaml` or `missions.yaml` in a volume created before those features. It
logs `[config] Added missing default config file: …` for each one. Existing
files are never overwritten, so your edits are safe. New *keys* inside
existing files, e.g. `mission_settings` in an older `hyperparameters.yaml`,
fall back to built-in defaults in code. A default file that you deleted from
the volume is restored on the next start, so empty it instead (for example
`world_definition: {events: {}}`) to switch its content off. After updating,
run `docker compose up --build`.

With Docker, app parameters persist in the existing config volume, and the
learning memory and persistent variables persist on the `narrative-saves`
volume.

Startup options:

```text
1. Run the story generator
2. Show the existing story setup
3. Edit story setup, then run
4. Edit story setup only
5. Load a world YAML path, then run
```

Option `5` accepts paths such as:

```text
runs/best_world.yaml
runs/20260428_124418/best_world.yaml
/absolute/path/to/world.yaml
```

During generation, press `q` to stop evolution early and continue to story execution with the best genome found so far.

## Execution Flow

1. `utils/config_loader.py` deep-merges all YAML files under `config/`.
2. `main.py` optionally opens the editor or loads a user-provided world YAML path.
3. Optional augmentation can expand the world before evolution.
4. `build_world(genome)` turns YAML into runtime `Location`, `Action`, `Outcome`, `Character`, and `Object` instances.
5. `evolve()` scores genomes over simulated episodes using multiple worker processes.
6. The best genome is used for a final detailed story simulation.
7. Optional LLM evaluation scores the final transcript.
8. Artifacts are saved in a timestamped folder under `runs/`.

## The evolutionary AI

Every story the tool creates comes from evolution
([`core_engine/evolution.py`](core_engine/evolution.py)). The terminal
(`python main.py`), the studio, and the REST API all use the same
`NarrativeEvolution` engine.

A **genome** is a storyteller: 19 traits such as appetite for danger,
curiosity, drive toward the mission, and avoiding repetition. Each generation:

1. Every storyteller plays `episodes_per_genome` stories. All storytellers
   of a generation play the **same** stories: the same seeds and, with
   `stratify_episodes`, the same **situations** (a starting location and a
   mission that can be reached from it). The generations cycle through every
   start × mission pair, so a storyteller is judged on the whole world, not
   on a few lucky draws. With `full_length_stories` every story is played to
   its natural end (`narrative_end.max_actions`), as in a real game, so the
   fitness judges real endings instead of stories cut at `max_steps`.
2. The **fitness function** (`fitness` in `hyperparameters.yaml`) scores how
   engaging each story is: dramatic arc, contrast, setup and payoff, variety,
   tension, mission wins, and more. It also has **losses** for what makes a
   story worse: `repetition` (an action used more than `free_uses` times, and
   A → B → A travel loops), `pacing` (a mission won before
   `mission_win_min_steps`), `base.unfinished_penalty` (a story cut before its
   end), `base.action_limit_penalty` (a story cut off by the action limit
   instead of ending), and the existing death, stall, and Dungeon penalties.
   **Engagement** (`fitness.engagement`, from the story's turn log) scores
   what a player feels turn by turn: the share of *filler* turns (no new place,
   person, event, outcome, check, or change of health or coins), *loops* (an
   action repeated with the same outcome within `loop_window` turns), *fading
   novelty* (fewer new turns in the last third than in the first), and
   *escalation* (rising tension: damage, events, failed checks). Terms that
   grow with every step (`length_normalization.terms`: arc, contrast, combat,
   volatility, interactions, discovery, tone) count **per turn**
   (`mode: rate`, scaled to `story_length.target`; a story shorter than
   `floor_steps` counts as that long), so a longer story is not better merely
   for being longer and a short won story is not worse. A storyteller's score over its stories is
   `score_aggregate`: `mean_minus_std` subtracts `consistency_weight` × the
   standard deviation, so a storyteller that tells consistently good stories
   beats one that alternates great and broken ones.
3. The `benchmark_top_k` best storytellers of the generation also play a
   fixed **validation benchmark** (`validation_episodes` stories, the same in
   every generation; with `cover_situations`, at least one per start × mission
   situation). The best of them **on the benchmark** is the generation's
   champion and leads the elites, so a storyteller that was merely lucky on
   this generation's stories is not crowned. The overall best storyteller is
   chosen on this comparable score.
4. The next generation is bred from the scores: `elitism_rate` keeps the
   best, parents come from tournament selection (`tournament_size`),
   `crossover_rate` blends them, `mutation_rate`/`mutation_step` mutate them,
   and `random_immigrant_rate` adds fresh storytellers. When the population's
   diversity falls below `min_diversity`, up to `max_immigrant_rate` newcomers
   and a wider mutation keep it from collapsing onto one storyteller, and a
   child identical to another is mutated again.
5. **Adaptive mutation.** After `stagnation_generations` without benchmark
   improvement, mutation widens up to `max_mutation_step` to escape the
   plateau, and it narrows again after an improvement. `early_stop_generations`
   stops a run that has stopped learning.

**Learning across runs.** The champions and their benchmark scores go into a
learning memory: `saves/evolution_memory.json`, keeping `hall_of_fame_size`
of them. With `warm_start`, the next run starts from the best remembered
storytellers, so the tool keeps improving from run to run. In the studio,
**Continue learning from earlier runs** switches this on or off, and **Forget
what the AI learned** resets it. The API offers `GET|DELETE /v1/learning`.

**What the AI learned is shown.** The studio's play tab draws the learning
curve: the benchmark champion, the best, and the average per generation. It
also lists the evolved storyteller's traits next to the population average
and bounds, with a plain meaning and whether the trait is high, moderate, or
low. The API returns the same data as `learning` on `POST /v1/simulations`
(history, `learned_traits`, `best_genome`, improvement, warm start). Bundles
store it too.

Measured on 20 held-out stories that evolution never played (16 storytellers,
8 generations, 4 stories each):

| | Held-out score |
| --- | --- |
| Random storytellers, average | 631 |
| Random storytellers, best of 10 | 718 |
| Evolved, first run | 750 |
| Evolved, second run (from memory) | 869 |
| Evolved, third run (from memory) | 893 |

**Resisting reward hacking.** Talking to the same NPC again and again used to
earn points without limit. `fitness.npc_interaction_cap` (3) stops that, so
evolution looks for better stories instead of farming conversations. The
setup-and-payoff term used to stay at 0 because clues were never recorded;
they are now recorded and matched as the storyteller matches them
(`story_rules.clue_matching`), so closing threads is actually rewarded.

Settings (`evolution` in `hyperparameters.yaml`, or **Engine logic →
evolution**):

```yaml
evolution:
  pop_size: 400                 # storytellers per generation (studio: "Candidate genomes")
  generations: 200
  episodes_per_genome: 6        # stories per storyteller per generation (studio: "Stories per storyteller")
  max_steps: 100                # with full_length_stories, whole stories
  full_length_stories: true
  stratify_episodes: true
  score_aggregate: mean_minus_std
  consistency_weight: 0.25
  elitism_rate: 0.08
  crossover_rate: 0.75
  random_immigrant_rate: 0.08
  tournament_size: 3
  mutation_rate: 0.22
  mutation_step: 0.14
  max_mutation_step: 0.4
  stagnation_generations: 3
  early_stop_generations: 0     # 0: never stop early
  warm_start: true
  hall_of_fame_size: 12
  validation_episodes: 10
  cover_situations: true        # the benchmark covers every start x mission situation
  benchmark_top_k: 3            # champions are chosen on the benchmark among the 3 best
  min_diversity: 0.08           # below this: more immigrants...
  max_immigrant_rate: 0.3       # ...up to this share, and wider mutation
  evaluation_threads: auto      # terminal: parallel evaluation...
  parallel_backend: processes   # ...on several CPU cores
```

With a reproducibility seed, a run is fully reproducible: the same seed gives
the same storytellers, stories, and learning curve. Tag similarity and
similarity-based spawn rules are cached to reduce repeated scoring work.

## Exporting the engine for games

After a generation or a complete training, you can export the whole engine
as a ZIP that **runs offline** and **integrates in a game**:

- **Studio:** **4 · Simulate & play → Export the engine (offline, for a
  game)**. Choose *This run's best*, *A generation of this run* (any
  generation's champion), *Best remembered storyteller*, or *This game's
  storyteller*, and whether to include the learning memory.
- **API:** `POST /v1/exports` with
  `{"source": "run" | "memory" | "game", "run_id", "generation", "session_id", "include_memory", "name"}`
  returns a `download_url`. `GET /v1/runs` lists the runs you can export, and
  every simulation response carries its `run_id`.
- **Terminal:** `python main.py` writes `runs/<timestamp>/offline_engine.zip`
  after every run.

The ZIP contains only what the engine needs:

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
game.save("slot1.game")                                 # engine.load_game(...) continues later
```

For Unity, Godot, Unreal, or web games, run `python run.py serve --port 8765`
next to the game and use HTTP + JSON: `POST /games`,
`POST /games/<id>/choose`, `GET /games/<id>`, `GET /games/<id>/transcript`,
`DELETE /games/<id>`, `GET /engine`, and `POST /evolve`. The export's README
has Unity (C#) and Godot (GDScript) snippets. `python run.py play` plays in a
terminal. `python run.py evolve` keeps training offline from the shipped
memory, and adopts the new storyteller only when it beats the current one
on the same stories. Persistent variables and each profile's recent starts
are saved per profile in the export's `saves/`.

## Saved engine models

A trained engine is saved as one **engine model file** (`.ane.zip`,
[`core_engine/model.py`](core_engine/model.py)). It holds everything needed
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
database, so worlds and trained engines can be found and used again:

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
  **Simulate & play → Records**. In the terminal:
  `python main.py --list-engines`. In Python: `backend.records`.
- **Docker.** `compose.yaml` mounts `${ANE_RECORDS_HOST_DIR:-/data/angelisg/ane}`
  at the same path. The container runs as uid 10001, so grant it write access
  to the host folder (for example `setfacl -m u:10001:rwx /data/angelisg/ane`),
  or recording stays off.
- **Tests** record into a temporary folder (`tests/records_sandbox.py`), never
  into the real database.

## LLM Fallback

LLM features are used by:

- `llm_evaluator`: final narrative evaluation.
- `augmentation`: world expansion.
- `editor_suggester`: editor field suggestions.

The primary model is configured here:

```yaml
llm_evaluator:
  enabled: true
  model_name: "meta-llama/Llama-3.2-1B-Instruct"
```

If that model is gated, restricted, or unavailable to the current Hugging Face user, the shared loader in [`utils/llm_model_loader.py`](utils/llm_model_loader.py) automatically tries the public fallback:

```yaml
llm_fallback:
  enabled: true
  model_name: "Qwen/Qwen2.5-0.5B-Instruct"
  local_files_only: false
  trust_remote_code: false
```

This fallback is shared by evaluation, augmentation, and editor suggestions. If you want offline-only behavior, set `llm_fallback.local_files_only: true` and make sure the selected models are already cached locally.

For faster non-LLM runs, disable these sections:

```yaml
llm_evaluator:
  enabled: false

augmentation:
  enabled: false

editor_suggester:
  enabled: false
```

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

Augmented worlds are written under [`augmentations/`](augmentations/). If `augmentation.save_to_source` is true, augmentation can also update files under [`config/`](config/), so review diffs after augmentation.

## Configuration Files

- [`config/locations.yaml`](config/locations.yaml): places, tags, descriptions, actions, outcomes, movement, reveals, and where stories may start. See [Starting locations](#starting-locations).
- [`config/items.yaml`](config/items.yaml): objects, tags, descriptions, collectibility, item actions.
- [`config/npcs.yaml`](config/npcs.yaml): characters, tags, descriptions, known locations/objects, NPC actions, and the `unique` flag. See [Unique and generic NPCs](#unique-and-generic-npcs).
- [`config/events.yaml`](config/events.yaml): events, their exclusive actions, and outcomes. See [Events](#events).
- [`config/stats.yaml`](config/stats.yaml): character stats. See [Character stats and skill checks](#character-stats-and-skill-checks).
- [`config/variables.yaml`](config/variables.yaml): persistent variables. See [Persistent variables](#persistent-variables).
- [`config/messages.yaml`](config/messages.yaml): every sentence the engine writes into a story. See [Story messages](#story-messages).
- [`config/missions.yaml`](config/missions.yaml): missions and their goals, which are the story's win conditions. See [Missions and winning](#missions-and-winning).
- [`config/hyperparameters.yaml`](config/hyperparameters.yaml): evolution, simulation, fitness, LLM, editor, and augmentation settings.

The web studio also accepts a single combined `.yaml`/`.yml` file in the
**Simulation settings** tab. Combine the top-level contents of all four files
into one document. The studio checks every shipped section, nested parameter,
and value type before activating the upload; an invalid or incomplete file does
not change the active configuration. The upload is a runtime alternative and
does not overwrite the files under `config/`. Saving an editor entry or the
built-in settings restores the file-based configuration.

**A file may hold only a world.** Every tuning section it does not set is taken
from the built-in configuration (its own values win), tuning keyed by places
of the built-in world that it lacks is dropped, and the places the tuning must
name (`game_state.arrest_location`, `start_selection.default_location`) have
to be its own. [`examples/worlds/`](examples/worlds/) holds two such worlds in
other genres, a frozen monastery and an orbital station, with staged missions,
`until` hints, a prison and remembered variables: upload one to play it.

Routes discovered during play are reversible, so previously connected
locations appear as travel actions from either direction. Every visible NPC
also receives a repeatable **Talk with…** action. Conversations count toward
the overall story action limit but keep the player and NPC in the current area.
The story action limit has a minimum of 100 steps in both the web UI and engine.

Reproducibility can be enabled from **Simulation settings** with an explicit
integer seed. Deterministic runs seed Python, NumPy, and Torch and request
deterministic Torch algorithms. The advanced YAML editor on the same tab makes
every setting in `hyperparameters.yaml` editable and validates the complete
document before saving.

The dedicated **Weights** tab is generated from `hyperparameters.yaml` and
exposes every numeric weight, bonus, penalty, threshold, chance, rate,
mitigation, floor, and power control. Location-specific consistency rules live
under `action_selection.location_penalties`; they can penalize action wording
or destination tags without adding location names to Python code. Return travel
uses neutral **Return to…** wording instead of reusing a destination's entrance
prompt.

### One-way moves and prisons

An outcome can **send** the player somewhere instead of moving them:

```yaml
- desc: The guards seize you and throw you in a cell.
  send_to: Dungeon      # one way: the Dungeon does not lead back here
```

`move_to` travels along a two-way route, so the player can come back. With
`send_to`, the origin learns the way to the destination, but the destination
gets no route back. Losing a fight against guards now uses `send_to` to the
`game_state.arrest_location` (default `Dungeon`), followed by
`[SENT] You are taken to the Dungeon. There is no way back the way you came.`
(message `sent`).

Prisons are set up with `location_constraints`:

```yaml
location_constraints:
  Dungeon:
    requires_escape: true
    lock_when: sent       # or: always
    escape_tags: [escape]
```

- `lock_when: sent` (the default): only a player who was **sent** to the
  Dungeon is confined. Travel, automatic transfer, and NPC-led moves cannot
  leave until an outcome tagged `escape` succeeds. A player who **found a route**
  to the Dungeon, e.g. through "Whispers of a prisoner in the Dungeon", can
  walk back as usual.
- `lock_when: always`: the old behaviour. Everyone is confined until they
  escape.
- Every arrest needs a new escape. Previously, one escape unlocked the Dungeon
  for the rest of the game, so a second arrest could simply walk out.
- After escaping, the way the player was brought in still does not lead back.
  They leave by the escape outcome's `move_to` or by routes found since.
- The mission planner treats a confined location as a dead end and otherwise
  as an ordinary place.

Across 300 simulated games, players walked into the Dungeon by normal routes
178 times and back out 177 times. Before this change, each of those visits
trapped them until an escape. `send_to` can be set in YAML or in the terminal
editor's outcome effects.

### Events

An Event is a self-contained situation that the player enters by taking an
action. Any location, NPC, or item action becomes a trigger when the writer adds
`triggers_event: <event id>`. The event is defined under
`world_definition.events` in [`config/events.yaml`](config/events.yaml):

```yaml
# config/npcs.yaml
- name: Thief
  known_locations: [Market]
  actions:
  - name: Confront the thief
    triggers_event: confront_thief   # no outcomes needed

# config/events.yaml
world_definition:
  events:
    confront_thief:
      title: Confronting the thief
      tags: [threat, market]
      descriptions: [The thief freezes as you block their escape.]
      actions:
      - name: Fight the thief
        outcomes:
        - desc: The thief flees empty-handed.
          tags: [combat, success]
          remove_npc: true           # the Thief leaves the story
      - name: Interrogate the thief
        outcomes: [...]
      - name: Steal from the thief
        outcomes: [...]
```

- While the event is active, only its actions are offered. Travel, NPC, item,
  and **Talk with…** actions are unavailable, NPCs and items do not spawn, and
  procedural unexpected events are paused.
- Choosing any event action ends the event. The player is back at the location
  where the event started, and this does not count as a new visit, so actions
  already used there stay used. An outcome with `move_to`, `move_to_tags`,
  `lead_to_known`, or a failed fight against a guard moves the player instead.
- The event starts only if the trigger action succeeds. Event actions act on
  behalf of the triggering NPC, so `remove_npc: true` and `lead_to_known` refer
  to that NPC. `remove_npc: <name>` removes a specific NPC. A removed unique
  NPC never reappears anywhere. A removed generic NPC (one bandit of many) is
  gone only for the current visit to the current location.
- An event action with its own `triggers_event` chains straight into another
  event after a successful choice.
- If the player meets none of the event actions' requirements (items or coins),
  the `authored_events.fallback_action` lets them leave.
- An NPC can also start an event by itself. See
  [Hostile NPCs and encounters](#hostile-npcs-and-encounters).
- Event actions take `enabled_if` too, and an outcome's
  `effects: [{start_event: <id>}]` starts an event once the action is resolved.

**When an event may start.** These rules are checked in one place
(`GameState._enter_event`), so they hold for every way into an event: trigger
actions, NPC encounters, chains, `start_event` effects, and the random
schedule. While they block an event, its trigger actions are not offered, an
event action that would chain into it is not offered, and its encounters wait.

```yaml
# config/events.yaml
street_parade:
  enabled_if: {at_location: Market}   # eligibility: any condition
  repeat: per_visit                   # once (unique in the story) | per_location | per_visit | always (default)
  max_occurrences: 3                  # at most this many times per story
  cooldown: 4                         # turns after it ended before it can start again
  trigger: random                     # action (default): only when something starts it;
  chance: 0.15                        #   random: also by itself, with this chance per turn...
  weight: 2                           #   ...and this weight when several random events hit together
```

- **Random schedule.** Once per turn, before the player chooses, every
  `trigger: random` event that may start rolls its `chance` with the story's
  random generator (so a seed always gives the same story). If several hit,
  `weight` picks one. An NPC encounter goes first. This is separate from the
  procedural `unexpected_events`, which build an event from configured parts
  when the story's pressure passes a threshold.
- **The event log** (`GameState.event_log`) records each run: where, which
  visit, the NPC, the `cause` (action, encounter, chain, effect, random), the
  action chosen, its `result` (`success`, `failure`, or `abandoned` for the
  fallback option), and the turn it ended. Occurrences, `repeat`, and
  `cooldown` are counted from it.
- The shipped Memory Thief's event is `repeat: once`. The world ships no random
  event, so its stories keep the rule that only skill checks roll dice.
  Validation rejects an unknown `trigger` or `repeat`, a random event without
  a `chance` in (0, 1], `chance` or `weight` on a non-random event, a negative
  `cooldown`, and a `max_occurrences` below 1.
- During automated evolution, a trigger action is ranked by the content of its
  event, plus `genome.event_prob × action_selection.event_trigger_weight`.

Configurations uploaded before Events existed remain valid without an
`events` section. Existing Docker config volumes receive `events.yaml`
automatically (see [Docker config volumes](#docker-config-volumes)).

### Missions and winning

**Missions in stages.** A mission may be a quest in steps: `stages` lists
`{title, description, goal, rewards}`, completed in order. Completing one is
told (`[QUEST] Make the thief talk: done. Next: Find the fence's ledger. …`),
applies its rewards, and the story then pursues the next stage (route planning,
the director's finale); the last one completes the mission. The score counts
each stage reached (`fitness.base.stage_bonus`), won or not.

```yaml
expose_the_fence:
  title: Expose the fence
  stages:
  - {title: Make the thief talk, goal: {action: Interrogate the thief}}
  - {title: Find the fence's ledger, description: The thief named someone in the Sewers., goal: {item: Fence's Ledger}}
```

Every story has a mission, a goal the player must achieve to win. Missions are
defined under `world_definition.missions` in
[`config/missions.yaml`](config/missions.yaml). When a story starts, one mission
is chosen at random (weighted by the optional `weight`, default 1) and is
announced as `[MISSION] ...` in the transcript:

```yaml
world_definition:
  missions:
    expose_the_fence:
      title: Expose the fence
      description: Find out who buys the goods stolen at the market.
      goal:
        action: Interrogate the thief     # take this action successfully
    royal_audience:
      title: Buy your way into the Palace
      weight: 2                           # chosen twice as often
      goal:                               # ALL conditions must hold
        location: Palace
        coins: 20
```

| Goal condition | Achieved when |
| --- | --- |
| `action` | The action is taken **successfully**. Match its full name (`(Priest) Seek a blessing`) or its name without the `(NPC) ` / `[Item] ` prefix (`Seek a blessing`). |
| `location` | The player has reached the location. |
| `item` | The item is in the inventory. |
| `event` | The event was entered and resolved, whatever the result. |
| `event_succeeded` | The event was resolved by an event action that **succeeded**. A failed action, or stepping away with the fallback option, does not count. |
| `npc_removed` | The NPC was removed by an outcome's `remove_npc`. |
| `outcome_tag` | An outcome with this tag succeeded (e.g. `escape`). |
| `coins` | The player holds at least this many coins. |
| `variables` | Persistent variables compare as stated, e.g. `{reputation: {gte: 1}}`. |
| `condition` | Any [condition](#conditions-and-effects) holds, e.g. `{any: [{has_item: Heavy Shield}, {stat: defence, op: gte, value: 70}]}`. |

Each goal key is shorthand for a condition, so goals, `enabled_if`, and the
rest of the world are checked by the same evaluator. A mission's `rewards`
(an [effect list](#conditions-and-effects)) apply when it is completed, before
the story ends, and an effect `complete_mission: true` completes it directly.

**Win condition and end flag.** When the goal is achieved, `GameState.won`
becomes `True`, `[MISSION COMPLETE] ...` is logged, and the story ends with
`end_reason` set to `Mission complete: <title>.`. The existing endings still
apply and count as a loss: health reaching 0, the action limit, stalling in one
location, or `end_on_dungeon`. Health is checked first, so a traveler who dies
on the winning move does not win. `GameState.mission_status` summarises the
result (`completed`, `failed`, or `in progress`). It is shown in the terminal
run, the archived `story_*.txt`, the studio's scene, and the simulation report.

**Effect on simulations.** Because a won story stops immediately, simulated
episodes are shorter. In 200 runs with random, unevolved genomes, the mean
story length fell from 59.9 steps (missions disabled) to 23.2 steps (median
21), and 95% of stories were won.

**Route planning toward the goal.** Every turn, the engine works out where the
unmet goal conditions can be advanced. These are the *goal sites*:

- the goal location;
- the locations where the goal item is on show (the same top-N visibility
  ranking a scene uses);
- the home of a unique NPC, or the locations a generic NPC fits;
- the locations offering the goal action, the goal event's trigger, or an
  outcome with the goal tag.

Event actions count at the sites of the actions that trigger the event. A
`coins` condition has no site, because coins can be earned almost anywhere.

A shortest-path search from those sites gives each location its distance to
the goal:

- A **known route** (`location.connected`) costs 1.
- A route that must first be **discovered** costs
  `mission_settings.discovery_route_cost` (default `2`). A discovery route
  follows the world's `spawn_rules` from a location whose own actions can open
  paths (`spawn` or `reveal_location` outcomes).
- **Locked locations** (e.g. the Dungeon, until the player escapes) are dead
  ends unless they are a site themselves.

The distances steer four decisions:

1. **Action ranking:** travelling to, revealing, or discovering a path to a
   location closer to the goal earns route progress `1 / (1 + distance)`.
   Discovering earns half of that for the best undiscovered target.
2. **Outcome selection:** outcomes that advance a goal condition, for example
   revealing the goal item or removing the goal NPC, are preferred. So are
   outcomes that discover a path toward the goal.
3. **Spawn selection:** newly discovered paths lean toward the goal.
4. **Destination choice** for `move_to_tags`, automatic transfers, and outcome
   scoring prefers locations on the route.

Measured over 300 runs with random genomes, goals several hops from the
start were won 95% of the time with route planning and 41% without it. They
took 5–12 steps instead of 24–45.

**Pacing: the goal is the story's climax.** Goal steering is off during the
opening of a story. It ramps up linearly between
`mission_settings.pursuit_start_step` (default `12`) and `pursuit_full_step`
(default `30`), so the player first explores, meets people, and runs into
trouble, and then drives toward the goal. A win that happens by chance during
the opening still counts. Every steering term is scaled by
`genome.goal_bias × pacing`. `goal_bias` is an evolvable genome parameter
(bounds `genome_bounds.goal_bias`, default `[0.3, 1.0]`) that sets how hard
the climax pushes.

**Rushed wins are worth less.** The fitness bonus for winning is
`mission_win_bonus × min(1, steps / mission_win_min_steps)`. With the defaults
(60 and 20), a win at step 5 earns 15 points and a win at step 20 or later
earns 60.

Without pacing, evolution learned to rush. In a small evolution run (24
genomes, 10 generations, then 60 test stories with the best genome), the
median story was 6 steps long and 68% were won. With both changes the median
is 25 steps, the mean 27.5, and 88% are won. Lowering the `goal_bias` bounds
instead made evolution avoid winning altogether (15% won). A later arc
(15 → 35) gets closer to the 35-step target (median 28) but wins only 68%.

Settings in `hyperparameters.yaml`:

```yaml
mission_settings:
  enabled: true            # false: no missions, the pre-mission behaviour
  end_on_goal: true        # false: mark the story won but keep playing
  route_weight: 6.0        # pull of route progress (× goal_bias × pacing)
  discovery_route_cost: 2  # cost of a path that must be discovered first
  pursuit_start_step: 18   # goal steering is off before this step...
  pursuit_full_step: 40    # ...and at full strength from this step
action_selection:
  mission_goal_weight: 6.0 # bonus per goal condition an action or outcome advances (× goal_bias × pacing)
fitness:
  base:
    mission_win_bonus: 60      # fitness reward for winning...
    mission_win_min_steps: 25  # ...paid in full only for stories at least this long
```

For longer stories, move the arc later (`pursuit_start_step` /
`pursuit_full_step`), accepting a lower win rate. Each evaluation episode picks
its own random mission, so genomes are scored across different goals.

If the player stands at a goal site whose goal actions are used up for this
visit, the planner treats the site as "leave and come back". Actions are
offered again on the next visit, which prevents the player from waiting at a
dead end.

**Saved bundles.** Model bundles and parameter files exported before
`goal_bias` existed still load. The missing parameter takes the midpoint of its
bounds (`Genome.added_field_defaults()`). Any other missing genome parameter is
still an error.

A mission can be forced from code with `GameState(world, genome, mission="expose_the_fence")`.
Missions whose goal already holds at the start (e.g. `location` equal to the
starting location) are never chosen, and each mission is paired with a start
from which it can be reached (see [Starting locations](#starting-locations)).
A mission with an empty goal or an
unknown goal key raises a `ValueError` when the world is built. A combined
configuration upload rejects it with a clear message. Configurations uploaded
before missions existed stay valid and simply have no mission.

### Starting locations

A writer decides where stories may begin. In
[`config/locations.yaml`](config/locations.yaml), a location opts in with
`start_eligible` and sets its relative chance with `start_weight`:

```yaml
Harbor:
  start_eligible: true   # default false: a story never starts here
  start_weight: 1.5      # relative chance among the starting locations (default 1)
Festival of Masks:
  enabled_if: {reputation: {gte: -2}}
  start_eligible: true   # a starting location only while it is enabled
  start_weight: 0.5
```

The shipped world starts at the Harbor (1.5), the Market (1), the Tavern (1),
or the Festival of Masks (0.5, only for travelers with a good enough name).
Each new story picks its start in three steps:

1. **Eligible and enabled.** Only locations with `start_eligible: true` whose
   `enabled_if` holds for the player can be drawn. If every starting location
   is switched off for a profile, creating the story fails with a clear error
   instead of starting elsewhere.
2. **The mission can be reached.** The mission is drawn first, by `weight`,
   among the missions that some starting location can reach. The start is
   then drawn among the locations from which that mission can be reached:
   its goal does not already hold there, and the route planner (known routes
   plus routes that can still be discovered through spawn rules) leads from
   there to a place where the goal can be advanced. Start variation therefore
   never strands the player away from the goal. A requested mission
   (`mission=`) restricts the starts in the same way.
3. **Weighted, with recent starts discouraged.** The draw uses the session's
   random generator, so a seed always gives the same start. Each location's
   weight is its `start_weight`, multiplied by `start_selection.repeat_factor`
   once for every time it started one of the profile's last
   `start_selection.recent_starts` stories. Repeats are less likely but never
   impossible.

```yaml
# config/hyperparameters.yaml
start_selection:
  default_location: Harbor  # the single start of worlds where no location is start_eligible
  recent_starts: 3          # how many of a profile's latest starts count as recent...
  repeat_factor: 0.25       # ...each one multiplies that location's weight by this (0 < f <= 1)
```

With the shipped weights and no history, the Harbor starts 37.5% of stories.
Right after a story that started there, its weight drops to 1.5 × 0.25 and its
chance to 13%. With two equal starting locations, a profile's second story
repeats the first one's start 20% of the time instead of 50%.

**Start history.** Real play (a game with a player profile: the studio, the
REST API, and exported engines with `persist`) records each story's start
with the profile's persistent variables (`starts` in the profile's record,
the latest 20). It is recorded when the story is created, also with
`persistence.save_on: end`. Simulations and evolution never record starts and
use no history, unless `persistence.simulate_with: stored`. Resetting a
profile also forgets its starts.

**Choosing the start.** `NarrativeEngine.new_game(start="Tavern")`, the HTTP
server's `POST /games` with `{"start": "Tavern"}`, and
`python run.py play --start Tavern` start at a given starting location.
Asking for a location that is not one raises `ValueError` (HTTP 400). If the
requested start is switched off for the profile by its `enabled_if`, the start
is drawn as usual. `engine.info()["starting_locations"]` lists the choices, and
every view and play state includes `start_location`. In code,
`GameState(world, genome, start="Palace")` forces any enabled location, which
is useful for tests and debugging.

**Older worlds.** A world in which no location sets `start_eligible` keeps a
single start, `start_selection.default_location` (Harbor) or else its first
enabled location. No random number is drawn for it, so its stories replay as
before, except that a mission the route planner cannot reach from that start
is no longer chosen while another one can be reached. Configurations uploaded
without a `start_selection` section stay valid.

In the studio, **World editor → Locations** has **Stories can start here**
and **Start weight**, and the entity API exposes `start_eligible` and
`start_weight` for locations. `start_selection` is editable in **Engine
logic**. `start_eligible` must be `true` or `false` and `start_weight` must be
above 0. `repeat_factor` must be above 0 and at most 1, since 0 would forbid
repeats. Invalid values are rejected when the world is built or uploaded.

### Unique and generic NPCs

Every NPC in [`config/npcs.yaml`](config/npcs.yaml) declares whether it is
unique:

```yaml
- name: Pale Conductor
  unique: true      # one person, one place: lives only at the Ghostline Station
  associated_tags: [spectral, travel, industrial, underground, social]
  known_locations: [Ghostline Station, Palace, Harbor]
- name: Gate Guard
  unique: false     # generic: any guarded location can have a gate guard
  associated_tags: [guarded, urban, palace, vault, dungeon]
```

- **Unique NPCs** (`unique: true`) live in exactly one location, their *home*,
  for the whole game. They never appear anywhere else, and they are always
  present when the player is at their home. They take a scene slot before
  generic NPCs, even beyond `scene.max_visible_npcs`.
- **Generic NPCs** (`unique: false`, and the default when the key is missing)
  appear in every location they fit, as before.
- **How a home is chosen:** at the start of each game, a unique NPC's home is
  the location it fits best. The ranking uses the same placement score as
  scenes (`known_locations`, exact and semantic tag matches, and
  `scene.location_npc_tags`) without genome biases. The same content therefore
  always produces the same home. `known_locations` makes a location
  *eligible* and adds `scene.npc_known_location_bonus`. The NPC's tags decide
  between eligible locations, so give a unique NPC the distinctive tags of its
  home. `known_locations` are still used for leads ("the Conductor tells you
  the way to the Palace"), so they can list places the NPC knows about without
  living there.
- **Reveals:** an outcome with `reveal_npc` brings that NPC into the current
  scene with its actions. A unique NPC can only be revealed at its home. A
  generic NPC can be revealed anywhere, because the writer named it
  explicitly.

**Where to set it:**
- **Studio:** tab **1 · World editor** → content type **NPCs** → the
  **Unique NPC** checkbox.
- **REST API:** the `unique` field on `GET|PUT /v1/entities/NPCs/{name}`.
  Omitting it on `PUT` keeps the stored value.
- **Terminal editor:** `python main.py`, option 3 or 4 → *npcs* → edit an
  entry → **5. Unique NPC**.
- **YAML:** `unique: true|false` on each character.

A removed unique NPC (`remove_npc`) is gone for the rest of the game. A removed
generic NPC is gone only for the current visit: drive off one bandit and
others still lurk elsewhere, and the same place can have one again on your
next visit. Both count for `npc_removed` mission goals.

Saving an NPC from the studio or the API always writes an explicit `unique`
value. Uploaded configurations are rejected if `unique` is not `true` or
`false`.

The shipped NPCs are unique except the Gate Guard, Guard, Dock Worker, Bandit,
and Merchant. Their tags were also made consistent with their homes. Stray tags
such as `guarded` on the Pale Conductor, `luxury` on the Maskwright, and
`maritime` on the Storm Cartographer used to pull them into the Palace or the
Harbor. Across 150 simulated games, no unique NPC appeared in more than one
location. Before this change, most NPCs did so in most games.

### Uniqueness of locations, items, and events

Each kind of entity has its own uniqueness rule:

| Entity | Rule |
| --- | --- |
| Location | Always unique: one named node of the world graph, visited any number of times. Visits are counted (`location_counts`), which is what `per_visit` rules use. There are no repeatable location instances. |
| NPC | `unique: true` lives in one home location for the whole story; a generic NPC appears wherever it fits. Removal is permanent for unique NPCs and lasts one visit for generic ones (see above). |
| Item | `unique: true` exists once per story (below); a generic item is shown wherever it fits, each location with its own copy. |
| Event | `repeat: once` happens at most once per story, whatever starts it; `per_location`, `per_visit`, `max_occurrences`, and `cooldown` limit it otherwise (see [Events](#events)). |

**Unique items.** The item in `config/items.yaml` is a definition. A unique
item gets one **instance** per story (`GameState.item_instances`), separate
from the definition and from the inventory:

```yaml
- name: Fence's Ledger
  enabled_if: {fence_exposed: true}
  unique: true
  location: Sewers     # where its instance lies (default: the location its tags fit best)
```

- **Placed once.** At the start of a story the instance is placed at its
  `location`, or at the location its tags fit best (shared tags, then tag
  similarity), so the same content always places it in the same spot. It is
  shown only there, always (it does not compete for
  `scene.max_visible_objects`), and `reveal_item` elsewhere does not show it.
- **One copy.** Collecting it (an action's `collects_object`, from any NPC or
  location) or receiving it (`give_item`) moves the instance into the
  inventory and out of the world. Using it (`consumes_object`) or losing it
  (`take_item`) makes it gone for good: it cannot be collected or received
  again. Instance states are `placed`, `held`, `gone`, and `unplaced` (no
  location fits; only an action or an effect can hand it over).
- **History.** `GameState.item_log` records, for every item, when it was
  placed, collected, received, used, or removed, independent of the current
  inventory. The condition `item_collected: X` reads it.
- The shipped Fence's Ledger is unique and lies in the Sewers. Its tags alone
  would also show it in the Bone Orchard and the Ghostline Station.
- Validation: `unique` must be true or false, and only a unique item may have
  a `location`, which must exist.

**Where to edit.** In the studio, **World editor → Uniqueness & scheduling**
edits an event's start rules or an item's uniqueness as YAML (validated, and
nothing is written when invalid). The API offers
`GET|PUT /v1/entities/{Events|Items}/{name}/policy`.

### Hostile NPCs and encounters

Some NPCs do not wait to be approached. Give an NPC an `encounter_event` and
that event starts **by itself** as soon as the NPC is present in the scene. The
player cannot do anything else first, including leave, until they pick one of
the event's actions:

```yaml
# config/npcs.yaml
- name: Bandit
  unique: false
  encounter_event: bandit_ambush   # an event from events.yaml
  encounter_repeat: true           # ambush on every visit, not just the first
  actions: []                      # the confrontation lives in the event
- name: Memory Thief
  unique: true
  encounter_event: memory_theft    # confronts the player the first time they meet
```

- By default the encounter happens **once per game**, the first time the player
  meets the NPC. With `encounter_repeat: true` it happens on every visit to a
  location where the NPC is present, once per visit. This suits generic
  hostiles such as bandits.
- The encounter starts on arrival: the transcript reads "You are in the Sewers
  … Met a Bandit … [ENCOUNTER] Bandit confronts you!" followed by the event. An
  NPC revealed during a visit (`reveal_npc`) confronts the player straight away.
- Encounter events are ordinary [events](#events). Only their actions are
  available, and `remove_npc: true` refers to the hostile NPC. When the event
  ends the player stays where they are, unless an outcome moves them. If the
  NPC is not removed it stays in the scene, but the encounter is not repeated
  during the same visit.
- Shipped hostiles: the **Bandit** (`bandit_ambush`, every visit): fight
  (needs a weapon), hand over your coins, plead for mercy, or run. The
  **Memory Thief** (`memory_theft`, once): defend your memories or offer a
  false one. Their former NPC actions moved into these events.
- Missions route through encounters too. An `event` goal, or a goal action
  inside an encounter event, sends the player to where that NPC is.

**Where to set it:**
- **Studio:** World editor → **NPCs** → **Encounter event** (a dropdown of
  the events in `events.yaml`, or *None*) and **Repeat on every visit**.
- **REST API:** `encounter_event` and `encounter_repeat` on
  `GET|PUT /v1/entities/NPCs/{name}`. Omit a field to keep the stored value,
  and send `"encounter_event": ""` to remove the encounter.
- **Terminal editor:** edit an NPC → **6. Encounter event**.

An unknown event name is rejected when saving from the studio or the API. At
run time it is ignored with a note in the transcript.

### Player health and power

`HealthPlayer` ([`core_engine/health.py`](core_engine/health.py)) owns
everything about the player's health and power. Every `GameState` has one as
`state.health_player`. Both stats have a hard maximum:

- **Health** is an integer from `0` to `game_state.max_health` (default `10`;
  the story starts at `initial_health: 8`). Healing never raises it above the
  maximum, and damage never takes it below 0. If `initial_health` is above the
  maximum, the story starts at the maximum.
- **Power** is recomputed after every change from the health ratio
  (`health / max_health`) and the player's average skill value
  (`power_model.stat_weight`; see [Character stats and skill checks](#character-stats-and-skill-checks)). It
  is clamped to `power_model.min_power` .. `power_model.max_power` (default
  `0` .. `5`).

| Member | Purpose |
| --- | --- |
| `heal(n)` / `damage(n)` / `change(delta)` / `set(value)` / `restore_full()` | Change health within bounds; each returns the change that really applied. |
| `effective_change(delta)` | Preview what a change would do, without applying it. |
| `is_alive`, `is_dead`, `is_low` (≤ `low_health_threshold`), `is_full`, `missing`, `ratio` | State checks used by the engine. |
| `calculate_power(stat_average)` | Recompute and clamp power. Combat damage and failure penalties are handled by `CombatSystem`. |
| `damage_taken`, `healing_received`, `healing_wasted`, `lowest_health`, `highest_health`, `summary()` | Statistics over the game. |

`state.health`, `state.max_health`, and `state.power` still work and read
from the `HealthPlayer`. Assigning `state.health` is clamped too. The engine
only values healing that would actually apply, so at full health a healing
outcome is not preferred. When healing does hit the cap, the transcript notes
`[HEALTH] Already at 10/10; healing is capped at the maximum.`, and the scene
shows health as `8/10`.

Before the cap, 75 of 200 simulated games went above 10 health, the highest
reaching 14. With the cap, health never exceeds the maximum (checked in the
tests over 150 full games).

Set **Maximum health** in the studio's **Simulation settings** tab next to
**Starting health** (which cannot exceed it), with `max_health` on
`GET|PUT /v1/settings`, or with `game_state.max_health` in
`hyperparameters.yaml`. Configurations without `max_health` stay valid and use
10.

### Repeatable and one-time options

Every action (location, NPC, item, or event) can say how often it is offered,
and actions can form one exclusive choice:

```yaml
# config/npcs.yaml — Gate Guard
- name: Give him the {alcohol_desc}
  exclusive_group: get_past_gate_guard  # one way past the guard...
- name: Bribe with coins
  exclusive_group: get_past_gate_guard
- name: Fight the guard
  exclusive_group: get_past_gate_guard  # ...once one succeeds, the others disappear
- name: Approach the guard
  repeat: always
# config/items.yaml — Ancient Map
- name: Study the map
  repeat: once
```

- `repeat: once`: offered until it is taken once, then never again in this
  game, whatever the outcome.
- `repeat: per_visit`: gone for the rest of the visit, offered again on the
  next visit.
- `repeat: always`: offered every turn.
- **Retry after failure.** `retry_on_failure: true` makes a failed attempt
  use nothing up. A `once` action then stays on offer (in this visit, on later
  visits, and in a saved game) until it **succeeds**, and only then disappears
  for good. A `per_visit` action stays on offer in the visit until it
  succeeds. "Failure" is the same as everywhere else: a failed skill check, or
  an outcome that does not succeed.

  ```yaml
  - name: Pick the lock
    repeat: once
    retry_on_failure: true   # try again after a snapped pick; gone once the door opens
    skill_check: {stat: stealth}
  ```

  | `repeat` | `retry_on_failure` | After a failure | After a success |
  | --- | --- | --- | --- |
  | once | false (default) | gone for the game | gone for the game |
  | once | true | still offered | gone for the game |
  | per_visit | false | gone for this visit | gone for this visit |
  | per_visit | true | still offered | gone for this visit |
  | always | (not allowed) | offered | offered |
- **Defaults.** Without `repeat`, `action_rules.default_repeat` in
  `hyperparameters.yaml` decides by source: `location: per_visit`,
  `npc: per_visit`, `item: once`, `event: always`. Travel and conversation
  options are always available.
- **Exclusive groups.** `exclusive_group: <name>` makes the actions with the
  same name one choice. When one of them **succeeds**, the whole group
  disappears for the rest of the game. After a failed attempt the other options
  stay (a failed bribe still lets you fight), while the failed option itself
  follows its `repeat` rule.
- **Scope.** One-time options and groups belong to their owner: a location, a
  unique NPC, an item, or an event. A generic NPC is a different person in each
  location, so bribing the Palace gate guard does not open the Vault's.
- The mission planner respects these rules. A goal option that is used up no
  longer counts as a way to the goal.
- Set them in YAML, in the studio (**World editor → Actions & outcomes →
  How often one action is offered**: pick the action, choose **Repeat**, tick
  **Retry after failure**), through `PUT /v1/entities/{kind}/{name}/actions`,
  or in the terminal editor: edit an action → **6. Repeat rule** and
  **7. Exclusive group**. An invalid `repeat`, a non-boolean
  `retry_on_failure`, or `retry_on_failure` with `repeat: always` (which never
  runs out) stops the world build, an upload, or a studio save with a clear
  error.
- Saved games (`Game.save`) keep what was used up and each action's retry
  rule; games saved before `retry_on_failure` existed load with it off.

The old name-based rules are gone. Actions starting with "Approach" used to
be always available, and "Move to" was how travel was recognised. Actions
now carry their `source` (location, npc, item, event, navigation,
conversation), so renaming anything, including travel options, is safe.

### Story messages

Every sentence the engine adds to a story lives in
[`config/messages.yaml`](config/messages.yaml), 53 messages in all: travel,
arrivals, discoveries, leads, events, encounters, failures, missions, and
endings. Rewrite any of them:

```yaml
messages:
  voyage: "You head towards the {location}."   # was "[VOYAGE] The opening draws you toward {location}."
  travel_return: "Go back to the {location}"   # the name of the travel option
  end_stalled:                                  # a list of variants...
  - "Nothing more happens in the {location}."
  - "The {location} has nothing left to offer."  # ...used in turn, never randomly
```

- Each entry lists its available `{placeholders}`, and Python format syntax
  works (`{health:+d}`). Leave an entry out to get the built-in default from
  [`core_engine/narration.py`](core_engine/narration.py).
- **Variants rotate** each time the message is used, so a story never repeats
  the same wording twice in a row, and the result is reproducible. Option
  names (`travel_new`, `travel_return`, `talk_action`) pick their variant from
  the location or NPC name instead, so an option keeps its name between turns.
- **Editing:** the studio's **Simulation settings** tab → **Story messages**
  (a YAML editor that keeps your comments), or `GET|PUT /v1/messages`. The
  `GET` returns every message with its default and allowed placeholders.
  Unknown messages, unknown placeholders, and empty lists are rejected before
  anything is saved. A malformed text found at run time falls back to the
  default instead of breaking the story.
- **Scoring ignores the wording.** Endings carry an `end_kind` (`death`,
  `mission`, `epilogue`, `action_limit`, `stalled`, `dungeon`, `story_end`) that fitness uses instead of
  parsing `end_reason`. Previously, stalling *in* the Dungeon was penalised
  twice because the text contained both "stalled" and "Dungeon".

### Genome-driven decisions and learning

Play involves no dice rolls. Every decision comes from the configuration, the
current state, and the evolved genome, so a genome's story is reproducible
and evolution can learn what makes stories engaging:

- **Following a newly found path.** This used to be a 40% chance
  (`auto_travel_on_spawn_chance`), which produced "The opening draws you
  toward the Dungeon." at random. The player now follows the path when the
  genome's commitment score reaches `game_state.auto_travel_on_spawn_threshold`
  (`1.5`). The score combines `voyage_bias`, `discovery_bias`, tag
  similarity, earlier visits, and the pull of the mission goal. The threshold
  was tuned to the old rate: 29% of new paths are followed, against 28%.
- **Novelty (`genome.novelty_bias`, bounds `[0.2, 0.9]`).** Each earlier use of
  the same choice lowers its ranking by `novelty_bias ×
  action_selection.repetition_penalty`. Travel counts per destination, so
  Harbor → Market → Harbor loops wear out. Each earlier showing of the same
  outcome text lowers that outcome by `novelty_bias ×
  outcome_objective.repetition_penalty`.
- **Variety fitness (`fitness.variety`).** Stories earn
  `distinct_choice_weight` (30) × the share of choices that were distinct,
  minus `repeated_outcome_penalty` (2) per outcome shown again. Evolution
  therefore learns the right `novelty_bias`.

In a small evolution run (24 genomes, 10 generations, 60 test stories with
the best genome), novelty changed the evolved stories as follows:

| | Without novelty | With novelty |
| --- | --- | --- |
| Distinct choices | 44% | 75% |
| Repeated outcomes per story | 30.2 | 6.1 |
| Back-and-forth moves (A → B → A) | 13.3 | 1.7 |
| Median length | 52 steps | 22 steps |
| Won | 88% | 80% |

Without novelty, evolution padded stories with repetition. The cost of novelty
is a slightly lower win rate. Lower the two `repetition_penalty` weights to
trade some variety for more wins.

What remains random is where randomness is the point. The mission is drawn
at the start of a story, as requested, and weighted by `weight`; a mission
passed to `GameState(..., mission=...)` uses no randomness. Evolution itself
also uses randomness to create, cross, and mutate genomes. Saved bundles
without `novelty_bias` still load and take the midpoint of its bounds.
Outcomes are chosen by the deterministic objective. The only dice of a
story are its skill checks.

### The director: how a story builds

Like a game's director, the engine shapes the course of a whole story
(`director` in `hyperparameters.yaml`, or **Engine logic → director**), so
that it rises toward an ending instead of wandering until it is cut off:

- **Escalation.** From `escalation_start` (a share of
  `narrative_end.max_actions`) the story escalates up to 1 at the limit:
  unexpected events become more likely (`event_pressure`), the storyteller is
  drawn to threats (`threat_weight`), and skill checks get harder over time
  (`skill_check_resolution.difficulty_rate_per_step`).
- **Stakes.** Every health loss is multiplied by `damage_scale` and grows with
  the escalation (`damage_escalation`); a failed action costs
  `skill_check_resolution.failure_damage`, a dangerous one (threats, combat,
  guards) `dangerous_failure_damage`. A careless traveler can die.
- **The finale.** `finale_actions` before the action limit, a `[FINALE]`
  announces that time is running out and the mission pulls at full strength
  (`finale_goal_pull`). A story that reaches the limit ends with an
  **epilogue** (`end_kind: epilogue`): what was achieved, what is left for
  another day, the places, people and items of the journey. Without the
  director it is cut off (`action_limit`), which the score penalises.
- **No loops.** An action used within `loop_window` turns whose outcomes have
  all been told already is not offered again (unless fewer than
  `min_options` options would remain); events and travel are always offered.

```yaml
director:
  enabled: true
  escalation_start: 0.3
  event_pressure: 0.6
  threat_weight: 0.8
  damage_scale: 1.25
  damage_escalation: 0.6
  finale_actions: 15
  finale_goal_pull: 1.0
  loop_window: 6
  min_options: 2
```

**What players see** (`player_options`): the storyteller's `limit` best
options, in the scene's order (`order: world`: activities, people, items,
travel) rather than best first (`order: ranked`), so the first option is not
always the storyteller's favourite and following it is not an autopilot.

**Missions per player** (`mission_selection`): a profile's
`recent_missions` latest missions are drawn `repeat_factor` times less often
each time they appear, as recent starts are, so a returning player meets
other missions.

### Each story's own geography

The spawn rules (`world_definition.spawn_strategy: similarity`) say which
places *may* open onto which; with `similarity_threshold: 0.5` that is almost
every pair. With `world_definition.story_map` every story draws its own
geography from them: each place opens onto `neighbors` (3) of the places it
fits, more fitting ones more often, joined into one connected map. Discovery,
route planning, mission choice and moving a stuck traveler all follow it,
so two stories of the same world have different maps (about a quarter of their
links in common) and each map stays consistent for the whole story. NPC leads
still cross it (people know distant places). The map is drawn from the
story's seed, so storytellers compared on the same story share the same map.

```yaml
world_definition:
  story_map:
    enabled: true
    neighbors: 3
```

### Coherence rules

Rules found by auditing generated stories, applied to every world:

- **Narration.** The location is told after a move (`Transferred from A to
  B`), never before it. Characters and items are named with an article:
  "Met Aurora" (an NPC with `article: ""`), "Met the Thief" (unique), "Met a
  Guard" (generic), "Nearby you see an Ancient Map: …", "some Gold Coins". An
  NPC's `article` sets it explicitly.
- **Escapes are for prisoners.** An action whose outcome carries the
  location's `escape_tags` is offered only while the player is locked there
  (`lock_when: sent` or `always`), and may be tried again until it works.
- **No small talk with an attacker.** A hostile NPC (one with an
  `encounter_event`) gets no generic conversation; authored `dialogue` and
  actions still apply.
- **Talking in circles stalls.** A conversation that only repeats what was
  said counts toward `narrative_end.max_consecutive_steps_in_location`.
- **Leads go somewhere.** An NPC lead never points to the place the player is
  in, and never takes them back where the turn started after the outcome
  moved them. Someone who just refused or beat the player (a `failure`
  outcome) gives no directions.
- **A way to move on is a real choice.** The director hides what would only
  repeat itself even down to one option, when that option is travel or (for a
  prisoner) an escape; otherwise at least `min_options` stay.
- **Written routes count.** Route planning (mission choice, starts, goal
  pursuit) also follows the routes a world's location actions open (`move_to`,
  `reveal_location`), not only the map, so worlds that connect places through
  their actions get every start their missions allow.
- **No way here while already here.** An action whose successes only bring
  the player where they are ("the officer waves you through to the command
  deck", on the deck) is not offered.
- **Goals with a direction.** A goal such as `variables: {reputation: {gte:
  1}}` is pursued through actions that *raise* the value; actions that lower
  it no longer count as a way to the goal.
- **Hints have their time.** An action or outcome with `until` (a condition)
  is no longer offered or told once it holds, e.g. `until: {action_done: Crack
  the main safe}` on a hint about the safe, or `until: {reactor_fixed: true}`
  across playthroughs. A numbered clue (`setup_N`) is not told once its payoff
  (`payoff_N`) has been, and an action that could only tell such clues is not
  offered.
- **A failure is not a free ride.** After a failed attempt, the places its
  success would have led to are barred from the engine's own moves (moving on
  after stalling, following a new path, a lead) for `director.barred_turns`;
  the player may still walk there.
- **Threads close.** As the story escalates, planting new clues is worth less
  and paying off open ones more (`director.close_threads`); in the finale no
  new clue is planted.
- **A prison costs turns, never the story.** Each failed escape makes the
  next attempt easier (`director.escape_bonus` on its skill check), and a
  player still locked in after `director.release_after` turns is released
  (`[RELEASED] …`) instead of the story ending as stalled.
- **Pacing.** Goal pursuit starts at `mission_settings.pursuit_start_step`
  (18) and reaches full strength at `pursuit_full_step` (40), and a win before
  `fitness.base.mission_win_min_steps` (25) counts as rushed: stories run
  about 28 turns instead of 21, with as many missions won.

### Story rules

Simulated stories were measured for coherence, pacing, and variety (200
stories with an evolved storyteller, every start, mission, stat split, and die
roll varying). Each problem found is now handled by a rule in
`story_rules` (`hyperparameters.yaml`, **Engine logic → story_rules**,
[`core_engine/rules.py`](core_engine/rules.py)), by a fitness loss, or by the
evaluation method above:

| Problem measured | Rule | Before | After |
| --- | --- | --- | --- |
| A failed check told a success text; every arrest printed the success it interrupted | `failure_tags`: outcomes tagged `failure` (or `on_check: fail`) are the failure branch. The storyteller attempts a success; a passed check tells a success, a failed one the failure branch or a plain failure. An arrest tells no outcome text and applies no effects of the attempt | 58 contradictions in 917 checks | 0 in 1,216 |
| An NPC said "hidden in the Vault" but a path to the Harbor opened | `follow_outcome_text`: a `spawn` or an NPC lead opens the place the outcome's text names; an outcome that routes by itself adds no extra lead | | text and route agree |
| Every procedural event paid "coins +8" (about 37 free coins a story) | `unexpected_events`: effects rotate (`effect_repeat_penalty`), coins come from `coin_budget` (10), at most `max_per_story` (3), `cooldown_steps` (4) apart; parts are not repeated and the text names the place | 4.7 per story, 1 effect | 3 per story, effects rotate, ≤ 10 coins |
| Missions won within a few steps | `early_goal_avoidance`: before `mission_settings.pursuit_start_step` the storyteller holds back from the mission; fitness `pacing` loss | 28.5% of wins before step 12 | 10% |
| Stories ended "stalled" | `on_stall: move`: after `max_consecutive_steps_in_location` the traveler moves on (`[MOVE ON] ...`) | 9.5% stalled | 0% (only an unescaped prisoner can still stall) |
| Clues were rarely paid off, and never scored | Clues are recorded; `payoff_weight` and `payoff_route_weight` pull toward a payoff while a clue is open; `max_open_clues` stops planting more; `clue_matching: any` | 4.5% (and 0 in the fitness) | 53% of stories |
| Items were rarely picked up | `collect_weight` (useful items × 1.5) | 243 item turns | 357 |
| The same action again and again; back-and-forth travel | `repetition_power` (each use costs more), `backtrack_penalty`; fitness `repetition` loss; one-shot content as `repeat: once` + `retry_on_failure` | 69 stories repeat an action 3+ times; 2.7 A→B→A loops | 32; 1.3 |
| One turn in five decided by a near-tie, broken by name | `tie_epsilon`: near-ties are ordered by fewer uses, then more unseen outcomes, then a per-story order (no dice) | 21% by name | 0% by name |
| Evolution scored stories cut at 60 steps | `evolution.full_length_stories`, `stratify_episodes`, `score_aggregate` (see [The evolutionary AI](#the-evolutionary-ai)) | 60-step stories, random situations | whole stories, every start × mission |

The same measurement also found more varied stories (distinct action
sequences 128 → 172 of 200, distinct paths 118 → 141, authored events in 98%
of stories instead of 78%), longer ones (24.5 → 29.5 actions), and more wins
(85% → 91%).

```yaml
story_rules:
  failure_tags: [failure]
  follow_outcome_text: true
  early_goal_avoidance: 1.0   # x mission_goal_weight, fading out by pursuit_start_step
  on_stall: move              # or end
  repetition_power: 1.5       # uses ** power x novelty_bias
  backtrack_penalty: 1.5      # x novelty_bias
  tie_epsilon: 0.1
  collect_weight: 1.0         # x discovery_bias
  payoff_weight: 2.0          # x setup_prob
  payoff_route_weight: 1.5    # x setup_prob
  max_open_clues: 2
  clue_matching: any          # or numbered (payoff_N closes setup_N)
```

**Content.** The rules work with what writers tag: tag an outcome `failure`
to make it what a failed attempt tells, tag a climactic outcome `payoff_clue`
(any `payoff_*`) to close an open clue, and make one-shot actions
`repeat: once` with `retry_on_failure: true`. The shipped world does this for
cracking the Vault's safe and infiltrating the Palace, and the bandit ambush
has a `cooldown` of 6 turns.

Every rule can be turned down or off (a weight of 0, `on_stall: end`,
`follow_outcome_text: false`), and invalid values are rejected when the world
is built, uploaded, or saved in the logic editor.

### Persistent variables

Persistent variables remember what happened in earlier playthroughs and change
the world of the next one. They are defined in
[`config/variables.yaml`](config/variables.yaml):

```yaml
world_definition:
  variables:
    thief_caught: {type: bool, default: false}
    reputation:   {type: int, default: 0, min: -5, max: 5}
```

The types are `bool`, `int`, `float`, and `str`. `min` and `max` bound numbers.
The built-in, read-only `playthroughs` counts finished playthroughs.

**Change them with outcomes:**

```yaml
- desc: A short scuffle ends with the thief fleeing empty-handed.
  set_variables: {thief_caught: true}   # set a value
  change_variables: {reputation: -1}    # add to a number, clamped to min/max
```

**Switch things on or off** with `enabled_if` on a location, NPC, item, event,
action, dialogue line, or mission. In the shorthand below every comparison
must hold; [Conditions and effects](#conditions-and-effects) has the full
condition language (`any`, `not`, stats, items held, places visited, ...):

```yaml
- name: Thief
  enabled_if: {thief_caught: false}              # equals
- name: Memory Thief
  enabled_if: {playthroughs: {gte: 1}}           # only from the second playthrough on
Festival of Masks:
  enabled_if: {reputation: {gte: -2}}            # eq, ne, gt, gte, lt, lte, in, not_in
```

- **What a disabled entry means.** A disabled location cannot be reached by
  travel, discovery, spawning, leads, automatic transfer, or the mission
  planner. The story never starts in one. Disabled NPCs and items do not
  appear. Disabled missions are not drawn. Unique NPCs keep the same home
  across playthroughs.
- **Changes apply at once.** When an outcome changes a variable, the current
  scene is re-evaluated. A newly enabled NPC appears without leaving the
  location, and what the player already discovered stays.
- **Mission goals** can use them too:
  `goal: {variables: {reputation: {gte: 1}}}`. The planner routes the player
  to where an outcome changes that variable.
- The transcript notes each change as
  `[PERSISTENT] reputation: 0 -> 1` (message `variable_changed`; set it to
  `""` to hide changes).

**Storage.** Values are kept per player profile in a JSON file:
`persistence.path` (default `saves/persistent_variables.json`, which git
ignores), or `$ANE_SAVE_DIR/persistent_variables.json` (in Docker,
`/app/saves` on the `narrative-saves` volume). Writes are atomic.

- **Only real play saves.** That means the studio game (after a simulation or
  a loaded engine) and the terminal story at the end of `python main.py`.
  Changes are saved as they happen (`persistence.save_on: change`, or `end`),
  and a finished story counts as a playthrough.
- **Simulations and evolution never write.** By default they play with the
  default values, so runs stay reproducible
  (`persistence.simulate_with: defaults`). Set `stored` to evolve for the
  player's current world instead (read only).

**Shipped example**, across playthroughs:
- Beating the Market thief in a fight sets `thief_caught`. From then on the
  Thief is gone, together with the two missions that need him.
- Making him confess sets `fence_exposed`. A new **Fence's Ledger** then
  appears in the Sewers, and "Expose the fence" is no longer offered.
- Blessings raise `reputation`; theft, bribes, and fighting guards lower it.
  Below −2 the Festival of Masks turns the traveler away, and below 0 a
  **Clear your name** mission becomes available.
- The **Memory Thief**, who "remembers meeting you in futures that never
  happened", appears from the second playthrough on.

In a five-playthrough run with one profile, the first playthrough exposed the
fence. From the second on, the ledger and the Memory Thief appeared and the
fence mission was no longer drawn.

**Where to edit:**
- **Studio:** **Simulation settings → Persistent variables** has the
  definitions editor (validated, comments kept), the stored values of the
  profile, and **Reset stored values (new player)**. **World editor → Enabled
  if** covers locations, NPCs, items, and events, and the scene lists the current values.
- **REST API:** `GET|PUT /v1/variables` (definitions), `GET /v1/variables/values`,
  `POST /v1/variables/reset` (optional `?profile=`), and `enabled_if` on entities.
- **Terminal editor:** **e. Enabled if** on any entry, and
  `set_variables` / `change_variables` in outcome effects.

Undefined variables, unknown comparisons, and values of the wrong type stop
the world build with a clear message. The studio and the API reject them when
saving.

### Conditions and effects

Everything in the world that can be switched on or off uses **one condition
language**, evaluated by one function
([`core_engine/conditions.py`](core_engine/conditions.py)). The same condition
therefore gives the same answer wherever it is written. Everything a story
can change is written as **one effect list**
([`core_engine/effects.py`](core_engine/effects.py)).

**Where conditions go** (`enabled_if` everywhere):

| On | While the condition does not hold |
| --- | --- |
| Location, NPC, item | It cannot be reached, does not appear, is not on show |
| Event | Its trigger actions are not offered, its encounters wait, nothing chains into it |
| Action (location, NPC, item, event) | It is not offered (`required_object`, `required_tag`, and `required_coins` still work) |
| NPC dialogue line | The next line is tried |
| Mission | It is not drawn |
| Mission goal (`goal.condition`) | The mission is not complete |

**A condition** is a tree. `all`, `any`, and `not` combine conditions; the
leaves read the story:

```yaml
enabled_if:
  all:
    - variable: palace_access          # persistent variable (also `playthroughs`)
      op: eq                           # eq (default), ne, gt, gte, lt, lte, in, not_in
      value: true
    - any:
        - {stat: persuasion, op: gte, value: 60}   # character stat, 1-100 with item bonuses
        - {has_item: Royal Pass}
    - not: {npc_removed: Gate Guard}
```

| Leaf | Holds when |
| --- | --- |
| `variable: x, op, value` | the persistent variable compares as stated |
| `stat: s, op, value` | the character stat (with item bonuses) compares as stated |
| `resource: coins` / `health` / `turn`, `op, value` | the player's coins, health, or actions taken |
| `has_item: X` | X is in the inventory |
| `item_collected: X` | X was collected or received in this story, even if it was used since |
| `visited: L` / `at_location: L` | L was reached in this story / the player is there now |
| `npc_removed: N` | N was removed from the story |
| `event_done: E` | E was entered and resolved (any result) |
| `event_succeeded: E` / `event_failed: E` | E was resolved by an event action that succeeded / failed (stepping away is neither) |
| `action_done: A` | A was taken successfully (with or without its `(NPC) ` prefix) |
| `outcome_tag: t` | an outcome tagged t succeeded |
| `mission: m` | m is the story's mission |

**Shorthand.** Everything written before trees existed still works: a mapping
of variable names is `all` of its comparisons, and a list is `all` of its
items. `{thief_caught: false, reputation: {gte: 2}}` is the same condition as
the tree with two `variable` leaves. The mission goal keys (`action`,
`location`, `item`, `event`, `npc_removed`, `outcome_tag`, `coins`,
`variables`) are shorthand for leaves too. Condition keywords (`all`, `any`,
`not`, `variable`, `stat`, `visited`, ...) cannot name a variable.

**Effects** are lists, run in order:

```yaml
effects:
  - set_variable: {palace_access: true}    # persistent variables, clamped to min/max
  - change_variable: {reputation: 1}       # numbers only
  - set_stat: {persuasion: 60}             # character stats for this story (1-100)
  - change_stat: {persuasion: 5}
  - give_item: Royal Pass                  # a name or a list of names
  - take_item: Bribe Purse
  - change_coins: -5                       # never below 0
  - change_health: 1                       # between 0 and max_health
  - remove_npc: Thief                      # true: the NPC who acts
  - reveal_npc: Priest
  - reveal_item: Fence's Ledger
  - reveal_location: Vault                 # opens a route from the current location
  - unlock_location: Dungeon               # a confined player may leave
  - move_to: Palace
  - send_to: Dungeon                       # one way
  - start_event: bandit_ambush             # once the current action is resolved
  - complete_mission: true                 # or the mission's id
  - end_story: You sail into the sunrise.  # the story ends with this text
```

They go on outcomes (`effects:`, run before the outcome's other fields), on
NPC dialogue lines, and on missions (`rewards:`, applied when the mission is
completed). An outcome's `set_variables`, `change_variables`, and
`change_stats` are shorthand for the matching effects and go through the same
code. Game code can call `state.apply_effects([...])`.

```yaml
# config/npcs.yaml: what an NPC says when talked with is the first line whose
# enabled_if holds; without one, the NPC's usual descriptions.
- name: Priest
  dialogue:
    - enabled_if: {reputation: {gte: 2}}
      text: The whole city speaks well of you, child. Walk in peace.
    - enabled_if: {any: [{variable: reputation, op: lt, value: 0}, {has_item: "Fence's Ledger"}]}
      text: I hear troubling things about you. My door is open, should you wish to make amends.
    # A line may also carry `effects`, applied when it is said.

# config/missions.yaml
rid_the_market_of_the_thief:
  goal: {npc_removed: Thief}
  rewards: [{change_variable: {reputation: 1}}, {change_coins: 10}]
```

**Validation, and no partial changes.** Every condition is checked for its
structure, operators, references (variables, stats, items, locations, NPCs,
events, missions) and operand types: a `bool` variable cannot be ordered with
`gte`, an `int` cannot be compared with text, and `in` needs a list of the
variable's type. Every effect list is checked for references, value types, and
writable fields: `playthroughs` is read-only, only numbers change by a delta,
and stats are set within 1-100. Nothing is applied until the whole list has
passed:

- **World build and upload:** `build_world` checks every condition and effect
  list of the world before building anything, and a combined configuration
  upload is checked the same way before it is activated.
- **Studio and API saves:** actions, missions, and `enabled_if` are validated
  before the YAML file is written.
- **During play:** `apply_effects` and every outcome validate the list before
  the first effect. A bad list raises `EffectError` and leaves variables,
  stats, inventory, coins, health, location, history, and the save file as
  they were.

**What the story does with them.** Effects can matter for a mission goal: the
route planner sees what an outcome's effects write (variables, items, places,
NPCs, events, coins), and a `goal.condition` is planned toward any action
that changes something it reads. In worlds where a location, NPC, or item
depends on more than persistent variables (`has_item`, `visited`, ...), the
scene and the planner re-evaluate after every change of the story, not only
when a variable changes. The shipped world uses conditions only on variables
there, so it pays nothing for this.

**Shipped examples:** the Priest's dialogue above, and the reward for ridding
the market of the thief.

**Where to edit:** `enabled_if` in the studio's **World editor** (any
condition as YAML, events included) and on the entity API; action
`enabled_if` and outcome `effects` in **Actions & outcomes** and
`PUT /v1/entities/{kind}/{name}/actions`; mission `rewards` and
`goal.condition` in **Missions** and `PUT /v1/missions`. Dialogue lines are
written in [`config/npcs.yaml`](config/npcs.yaml) and checked when the world
is built or uploaded.

### Character stats and skill checks

The fixed combat model (attack and defence from weapon and armour tags, a
single power number, `combat_resolution` weights) is replaced by **character
stats the writer defines** and **skill checks** resolved by a combat system.

**Define the stats** in [`config/stats.yaml`](config/stats.yaml), with any
names. The story decides:

```yaml
world_definition:
  character_stats:
    stats:                         # a combat story...
      attack: {description: Landing blows and winning fights.}
      defence: {description: Taking a hit; softens combat damage.}
      stealth: {description: Moving unseen.}
      persuasion: {description: Talking and bribing your way through.}
    # ...or a social one: stealth, persuasion, watchfulness, intimidation
    allocation:
      total_points: 50             # every player spends exactly this
      min_points: 5                # per stat
      max_points: 20
      value_per_point: 5           # points -> 1-100 skill value (5..20 -> 25..100)
    mode: random                   # random | defined | balanced
    defined_points: {attack: 13, defence: 13, stealth: 12, persuasion: 12}
    tag_checks: {combat: attack, stealth: stealth}   # outcome tag -> stat to check
    damage_reduction_stat: defence
```

**`CharacterStats`** ([`core_engine/stats.py`](core_engine/stats.py)) holds
one player's stats. The player chooses their points (`defined`, validated
against the budget and the per-stat limits), or gets a `random` split within
the limits (seeded, so reproducible), or a `balanced` one. Held items add
`stat_bonus: {attack: 15}`, and outcomes change stats for the rest of the
playthrough with `change_stats: {persuasion: 5}`. Values always stay within
1–100.

**`CombatSystem`** ([`core_engine/combat.py`](core_engine/combat.py)) resolves
every check and fight:

- **The check.** It rolls 1–100. The check passes when the roll is at or below
  the stat's value (with item bonuses), plus the action's modifier, minus the
  difficulty. The transcript shows each check, e.g. `[CHECK] persuasion: rolled
  34, needed 60 or less - passed.`
- **Which checks apply.** An action can require one, and its outcomes can be
  written for each result:

  ```yaml
  - name: Interrogate the thief
    skill_check: {stat: persuasion, modifier: -10}
    outcomes:
    - {desc: The thief talks., on_check: pass, set_variables: {fence_exposed: true}}
    - {desc: The thief slips away., on_check: fail, remove_npc: true}
  ```

  Without a `skill_check`, an outcome whose tag appears in `tag_checks` (e.g.
  `combat`) needs a check of that stat, so existing content uses stats without
  edits. A failed check plays the `on_check: fail` outcome when there is one.
  Otherwise the generic failure applies: a health penalty, or an arrest by
  guards.
- **Combat consequences.** Combat damage is reduced by 1 per
  `damage_mitigation_per_point` (25) of the `damage_reduction_stat`. At
  `failure_mitigation_threshold` (80) or above, failed dangerous actions cost
  no health.
- **The AI uses it.** Outcomes are scored with their real pass chance, and
  `action_selection.skill_fit_weight` makes the AI prefer checks its
  character is likely to pass. A stealthy character sneaks, and a strong one
  fights.

**Skill check resolution** replaces *Combat resolution* in the **Weights** tab
(`skill_check_resolution` in `hyperparameters.yaml`):

| Setting | Effect |
| --- | --- |
| `difficulty_penalty` | Added to every check. Positive makes checks harder, negative easier. |
| `difficulty_rate_per_step`, `difficulty_rate_start_step` | Difficulty change per story step from that step on. Positive gets harder over time, negative easier. |
| `min_chance`, `max_chance` | The pass chance never goes below or above these percentages (5 and 95). |
| `damage_mitigation_per_point`, `failure_mitigation_threshold` | How the damage-reduction stat softens combat. |

Shipped content:
- **Item bonuses.** The Steel Dagger gives +15 attack, the Heavy Shield +20
  defence, the Rusty Sword +5 attack, the Vintage Wine +5 persuasion, and the
  Fence's Ledger +10 persuasion.
- **Explicit checks.** Bribing the gate guard is a persuasion check,
  interrogating the thief a persuasion check at −10, and running from a bandit
  a stealth check.
- **Stat changes.** The priest's blessing gives +5 persuasion.

Over the same 100 stories, a stealth-and-persuasion build won 97%, a
balanced one 76%, and an attack-and-defence build 64%. The brawler made the
most attack checks: 80, against 42 for the balanced build. The shipped world
rewards stealth and talk more than fighting, because most fights first need
a weapon.

**Where to set it:**
- **Studio:** *Simulation settings → Character stats* (definitions);
  *Simulate & play → Character stats of the player* (story default, random,
  balanced, or your own points, with **Randomize**/**Balance** and a live
  points check); *World editor* → item **Stat bonus while held**; and
  `skill_check`, `on_check`, and `change_stats` in the actions editor.
- **REST API:** `GET|PUT /v1/stats`, `POST /v1/stats/allocate` (check or
  preview points), `stats` / `stats_mode` on `POST /v1/simulations`,
  `stat_bonus` on items, and `stats`, `stat_points`, and `last_check` in the
  play state.
- **Terminal editor:** action **8. Skill check**, outcome effects `on_check` /
  `change_stats`, and item **s. Stat bonus**.

Undefined stats, invalid `on_check` values, impossible budgets, and invalid
points are rejected with a clear message.

### Coins and arrival text

- A purse cannot go below 0 coins. A loss larger than the purse, for example
  pleading with a bandit who takes 20 coins from someone holding 5, empties it
  instead. Previously the balance could go negative, and then every action,
  even free ones, was hidden and only travel remained.
- Each visit to a location now logs its arrival text ("You are in the …",
  "Met a …", "Nearby you see …") once, when the scene is set. The engine had
  never written these lines to transcripts, so stories and the LLM evaluator
  only saw "Continuing your activities…".

NPC placement can be constrained with `scene.location_npc_tags`. When a
location has an explicit tag list, characters must share at least one of those
associated tags unless the location is explicitly listed in their
`known_locations`. Matching tags also receive the configurable
`scene.location_tag_match_bonus` during NPC ranking.

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

## Useful Commands

Run the main generator:

```bash
python main.py
```

Run the augmenter directly:

```bash
python -m core_engine.augmenter
```

Generate the project presentation:

```bash
python -m utils.generate_presentation
```

The presentation conversion path expects LibreOffice's `soffice` command if PPTX output is needed.

## Development Notes

- Record every change in [`docs/CHANGELOG.md`](docs/CHANGELOG.md) under **Unreleased**, in the section of the area it touches, in the same commit.

- Keep version control active when using augmentation, especially with `augmentation.save_to_source: true`.
- For reproducible runs, set `reproducibility.seed` in `config/hyperparameters.yaml`.
- For faster debugging, temporarily lower `pop_size`, `generations`, and `episodes_per_genome`, or press `q` during generation to continue with the current best result.
