# Running the ANE: studio, REST API, Docker, terminal

How to run the Adaptive Narrative Enabler and what each tab of the studio does. The REST API
itself is in [API.md](API.md); the ANE AI that a game runs offline is in [UNP.md](UNP.md).

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
`http://localhost:8004/docs`. See [`docs/API.md`](API.md) for how the API
behaves (state, validation, status codes, limits), endpoint coverage, and
request examples.

The Gradio studio edits the whole story world through forms, saves simulation
settings to the YAML configuration, runs a small candidate tournament, and
turns its best genome into a player-controlled game. Every editor validates
before saving.

**The studio is a client of the REST API.** Every tab works through the API's
endpoints ([`docs/API.md`](API.md)); `app.py` imports none of the
backend, so whatever the studio can do, any other client of the API can do
too. `StudioAPI`, at the top of [`app.py`](../app.py), is the connection:

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
| **1 · World editor** → an entry | Name, tags, descriptions, goals; NPC **Unique**, **Encounter event**, **Repeat on every visit**, **Role in the story** and **Secret**; **Enabled if** (locations, NPCs, items); **Event title**; item **Stat bonus while held**; location **Stories can start here** and **Start weight** | `GET /v1/entities/{kind}?fields=names`, `GET/PUT/DELETE /v1/entities/{kind}/{name}` |
| **1 · World editor** → *Every field of this entry (YAML)* | The whole entry as stored, including what the form does not show (an NPC's dialogue, known places and items, article; a location's distant views and exits) | `GET/PUT /v1/entities/{kind}/{name}/yaml` |
| **1 · World editor** → *Map* | Which places border which (`world_definition.routes`), or no authored map | `GET/PUT /v1/documents/map` |
| **1 · World editor** → *Failures that lead nowhere* | The actions whose failure changes nothing but health or coins and that no complication fits | `GET /v1/lint/failures` |
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
| **2** → *The active configuration* | What new games and runs use now (every file merged, or the uploaded configuration), read only | `GET /v1/config` |
| **3 · Weights** → *Skill Check Resolution* and the rest | Difficulty (easier/harder), difficulty over time, chance floor/ceiling, damage mitigation, and every other weight | `GET/PUT /v1/weights` |
| **4 · Simulate & play** → evolution | Storytellers, generations, steps, **stories per storyteller**, **continue learning from earlier runs**; the **learning curve** and **what the storyteller learned**; forget the learning memory | `POST /v1/jobs/simulations` and `GET /v1/jobs/{id}` (or `POST /v1/simulations`), `GET/DELETE /v1/learning` |
| **4 · Simulate & play** → *Load a previous best engine* | An engine model, an older engine ZIP, or a configuration YAML with its model-parameters YAML | `POST /v1/engines/load` (`file`, `parameters`, `profile`, `stats_mode`, `stats`) |
| **4 · Simulate & play** | **Player profile**, the **quest to play** (any quest open to the profile, or one drawn by the story; *Quests in this world* lists them with their stages) and the player's **character stats** (story default, random, balanced, or own points with Randomize/Balance); the scene shows the quest and its current stage, health `x/max`, the **people here** (who they are, what they want, hostile or able to help with the quest, how often helped and whether an ally, how often spoken with), the **checks** of the choices, stats, the last check and remembered values; beside it the **quest log** (stages done, now, to come; what to do now) the **people met** (where, how often spoken with, allies, gone) and the **map** as the player knows it (places, paths, locked exits); taking one of the **most relevant choices**, each labelled with its check | `GET /v1/missions/playable`, `POST /v1/stats/allocate`, `POST /v1/sessions/{id}/actions` (structured state: `quest`, `people`, `people_met`, `ways_on`) |
| **4 · Simulate & play** → *Export the engine* | Download the engine to run offline in a game. *Runs on* a system or Unity: **the ANE AI**, made from a complete engine (a finished training's or a loaded one; the export stays off until one exists). *Runs on* Python: this run's best, any generation's champion, the best remembered storyteller, or the current game's. With or without the learning memory | `GET /v1/exports/platforms`, `GET /v1/exports/engines`, `POST /v1/exports`, `GET /v1/artifacts/{id}` |
| **4 · Simulate & play** → *Judge this playthrough* | Gemma 4's judgement of the game played so far: engagement, the eight criteria, the best and weakest moments, a verdict | `POST /v1/sessions/{id}/judge` |
| **4 · Simulate & play** → *Records* | Recorded engines, runs (their settings and every generation), worlds (download one as a configuration to upload) and stories; play with, continue or retrain an engine; download its model file and world; judge a recorded story, or compare two (which is the better game) | `GET /v1/records/...`, `POST /v1/records/engines/{id}/play`, `POST /v1/jobs/engines/{id}/continue\|retrain`, `POST /v1/records/stories/{id}/judge`, `POST /v1/judge/compare` |
| **5 · Engine logic** | Every parameter of the engine's logic, section by section (no code editing) | `GET /v1/logic`, `GET/PUT /v1/logic/{section}` |
| **5** → *App parameters* | The studio's limits, defaults, queue, and launch settings | `GET/PUT /v1/app-settings` |
| **5** → *Background jobs* | Evolutions, continued trainings and judge runs started by any client, with their progress | `GET /v1/jobs` |
| **6 · Evaluate with the judge** | Judge the engine's stories (current settings or a recorded engine), or compare the current settings with version B on the same seeds (pairwise A/B, both orders, win rate with its 95% interval); fit the fitness to the judge and save the suggested `fitness.term_weights`, or edit them | `POST /v1/jobs/judge-benchmark`, `POST /v1/jobs/calibrate-fitness`, `GET/PUT /v1/fitness/term-weights` |

Every endpoint of the API is reached from the studio, directly or through an equivalent
one (the YAML form of a JSON endpoint, the job form of a long one);
[`tests/test_studio_coverage.py`](../tests/test_studio_coverage.py) fails when an endpoint is
added that the studio cannot reach.

What the studio needs to build its forms (entry kinds, suggested tags, the
action reference, repeat rules, goal keys, rule choices, stat choices, NPC roles, the
judge's criteria) comes from `GET /v1/reference`.

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
([`docker-entrypoint.sh`](../docker-entrypoint.sh)) therefore compares the
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

## The terminal (`main.py`)

`python main.py` starts the terminal generator.

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

## Useful commands

Run the main generator:

```bash
python main.py
```

Run the augmenter directly:

```bash
python -m core_engine.augmenter
```
