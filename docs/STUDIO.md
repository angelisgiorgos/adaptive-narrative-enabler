# Running the ANE: studio, REST API, Docker, terminal

How to run the Adaptive Narrative Enabler and what each tab of the studio does. The REST API
itself is in [API.md](API.md); the ANE AI that a game runs offline is in [UNP.md](UNP.md).

The studio has five destinations in the order of the work, and one for the technical settings:
**🌍 World** (create it, with its story rules) → **🧠 Train** (train the AI storyteller) →
**▶ Play** (play the world) → **⚖ Evaluate** (with the judge) → **📦 Export** (for a game), and
**⚙ Advanced** (the simulation, the weights, the engine's logic, the app and its jobs). Inside a
destination, its sections are tabs, so a section is one click away instead of a long page of
folded panels. **Train only trains**: every game is played in ▶ Play (a trained AI, a recorded
engine, an engine file, or a save game). Under the header, a status line gives the active world's
size, the trained AIs that can be played and exported, and whether recording is on.

### Navigation

- **The workflow banner** (wide screens) is the navigation: *Workflow: 🌍 World → 🧠 Train → ▶ Play
  → ⚖ Evaluate → 📦 Export*, with ⚙ Advanced set apart. Each step is a button that opens its
  destination; the destination open now is highlighted.
- **The menu** (screens narrower than 820 px): the banner gives way to *☰ Menu: go to a section*,
  which lists every destination by name, the open one selected. No destination is ever behind an
  overflow button: the destinations' own tab bar is not shown, at any width. When a destination's
  sections do not fit a narrow screen, Gradio's overflow button for them reads **··· More**.
- **Stable identifiers**: `world`, `train`, `play`, `evaluate`, `export`, `advanced` (`DESTINATIONS`
  in `app.py`). They are the tabs' ids, the banner's buttons (`#nav-<id>`), the destinations'
  elements (`#dest-<id>`), the menu's values, and the address: `?section=play` opens ▶ Play.
- Changing destination only selects a tab: nothing is rebuilt or reloaded, so forms being edited,
  a game in progress, a training's results and every other value stay as they were. Links inside a
  destination use the same navigation (*▶ Play the newest* and *📦 Export it* after a training).

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
`http://localhost:8004/docs`. The judge (Gemma 4 on vLLM, a GPU) is a separate service, off
unless started: `docker compose --profile judge up -d judge` serves it to the API at
`http://hamlet-ane-judge:8000/v1` (`ANE_JUDGE_URL`; [`docs/LLM.md`](LLM.md)). The judge's views
in the studio say whether it is ready and, if not, why and what to do. See [`docs/API.md`](API.md) for how the API
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
recorded with the origin `studio` and its runs with the user `test` (the records' runs table
shows each run's user). Long runs (evolution, continued or
retrained training) are background jobs that the studio polls, so the
progress bar still moves generation by generation. Files the API offers
(story configuration, engine model, exports, recorded worlds) are downloaded
for the file widgets. A refusal from the API (an invalid entry, a broken
condition, an unknown engine) is shown as the studio's error message, and
nothing is written.

| Studio | What you can change | REST API |
| --- | --- | --- |
| **🌍 World → 📝 Entries** (pick a content type and an entry; **New entry**) → *Fields* | Name, tags, descriptions, goals; NPC **Unique**, **Encounter event**, **Repeat on every visit**, **Role in the story** and **Secret**; **Enabled if** (locations, NPCs, items); **Event title**; item **Stat bonus while held**; location **Stories can start here** and **Start weight** | `GET /v1/entities/{kind}?fields=names`, `GET/PUT/DELETE /v1/entities/{kind}/{name}` |
| … → *Fields*: an NPC's or a place's behaviour | NPCs: **Comes up to the player** and **How often they are seen**; places: **Draws the player in** and **How many visits a story may have there**. The choices are every tag a rule gives that behaviour (with what it does); choosing one sets that tag on the entry and removes the others of that behaviour (**Save entry** keeps it) | `GET /v1/behaviours` |
| … → *Actions & outcomes* | Pick an action (or add, delete one): its name, how often it is offered and whether a failure uses it up, its skill check, exclusive group, the event it starts, its price, the item it needs, uses or takes, and its conditions. Its **outcomes**, picked one by one: what is told, when (a check passed or failed), tags, health and coins, where it moves or sends, what it reveals, who it removes, a new path, a guide, variables and stats, other effects. Every action as YAML under *advanced* | `GET/PUT/DELETE /v1/entities/{kind}/{name}/actions/{index}` (`index` = the number of actions: a new one), `GET/PUT /v1/entities/{kind}/{name}/actions` (all), `/actions/yaml` |
| … → *Scheduling* | Events: how they start (an action, or by themselves with a chance and a weight), cooldown, at most how many times, how often. Items: unique, and where it lies. As YAML under *advanced* | `GET/PUT /v1/entities/{Events\|Items}/{name}/policy` (or `/policy/yaml`) |
| … → *More details* | People: the places and items they know, how the story calls them, and what they say (a line per condition); places: how they are seen from afar, on arrival, the ways to go there, locked exits, and their **points of interest** (a row each: label, prompt, and the place, person, item, action or outcome it leads to; the terminal editor's interaction points) | `GET/PUT /v1/entities/{kind}/{name}/details` |
| … → *✨ Suggest* | The language model's suggestions for the entry's descriptions, goals, tags, views from afar, arrival texts, ways to go there or dialogue, in the world's voice (the terminal editor's suggestions): tick them and add them to the form, then save the entry or its details. Without the language model it says why and how to start it | `POST /v1/suggestions` (503 without the model) |
| … → *Every field (YAML)* | The whole entry as stored, every field | `GET/PUT /v1/entities/{kind}/{name}/yaml` |
| **🌍 World → 🎯 Missions** | Pick a mission (or add, delete one): key, title, description, weight, its condition and rewards, and its steps as a table (title, what the player is told, the goal, the rewards). Every mission as YAML under *advanced* | `GET/PUT/DELETE /v1/missions/{key}` (`rename`), `GET/PUT /v1/documents/missions` |
| **🌍 World → 📜 Story rules** | *Story rules* (mission pacing and route weight, default repeat rules, arrest location and confinement, following new paths, persistence, event fallback); *Story messages* (every sentence the engine writes); *Persistent variables* (definitions; a profile's stored values, reset to a new player); *Character stats* (stat names, point budget, allocation mode, tag checks, damage-reduction stat) | `GET/PUT /v1/rules`, `GET/PUT /v1/documents/messages`, `GET/PUT /v1/documents/variables`, `GET/PUT /v1/documents/stored_values?profile=`, `POST /v1/variables/reset/yaml?profile=`, `GET /v1/variables/profiles`, `GET/PUT /v1/documents/stats` |
| **🌍 World → 🗺 Map** | Pick a place and tick the places it borders (routes change both ways); the whole map is drawn beside it. The map as YAML under *advanced* | `GET /v1/map`, `PUT /v1/map/{place}`, `PUT /v1/map`, `GET/PUT /v1/documents/map` |
| **🌍 World → 🏷 Behaviour by tags** | Two tables, people and places: a row per rule (its tags, and what each behaviour becomes), and what everyone does without one. A row with a new tag is a new choice in *Fields*; an invalid rule is refused with why | `GET /v1/behaviours/rules`, `PUT /v1/behaviours/rules/{npcs\|locations}` |
| **🌍 World → ✨ Grow the world** | The world's size against the augmentation targets; the language model proposes new places, people and items (a background job; nothing written), shown ticked and as editable YAML; **Add the ticked ones** adds them once each is new and the whole world checks with them in place. Replaces the old augmenter's direct overwrite of the config files | `GET /v1/world/size`, `POST /v1/jobs/world-proposal`, `POST /v1/world/additions` |
| **🌍 World → 🔍 Checks** | The actions whose failure changes nothing but health or coins and that no complication fits | `GET /v1/lint/failures` |
| **🧠 Train → Train** | Storytellers, generations, steps (with how long a story really is: *Full-length stories enabled: up to N turns*, or the steps up to the story's end, the steps slider marked when it does not set it, and the work as storytellers × generations × stories × turns against the limit), **stories per storyteller**, **continue learning from earlier runs**, deterministic runs; **Evolve storytellers** shows the report, the **learning curve**, **what the storyteller learned** and the run's files; **▶ Play the newest** opens ▶ Play with it chosen and **📦 Export it** opens Export; forget the learning memory. No game is played here | `POST /v1/jobs/simulations` and `GET /v1/jobs/{id}` (or `POST /v1/simulations`), `GET/DELETE /v1/learning` |
| **🧠 Train → Records** | Recorded engines: continue or retrain one, download its model file and world; recorded stories (judge one, or compare two), runs (their settings and every generation), worlds (download one as a configuration to upload); **import** the engines and games in the server's import folder (`ANE_ARTIFACT_DIR`, else `artifacts/`; copy or move, preview stories), as `python main.py --import-artifacts` does | `GET /v1/records/...`, `POST /v1/jobs/engines/{id}/continue\|retrain`, `POST /v1/records/stories/{id}/judge`, `POST /v1/judge/compare`, `GET /v1/records/import`, `POST /v1/jobs/records-import` |
| **▶ Play** → *🎮 New game* | Choose the **trained AI** (finished runs and loaded engines, newest first; also without the records), the **player profile**, the **quest** (any quest open to the profile, or one the story draws) and the **character** (story default, random, balanced, or *my own points*, set with Randomize/Balance), then **▶ New game**, or **📂 Continue a save game** (saved here or by the ANE AI; exactly, or rebuilt from its parameters). *Another engine*: a recorded engine, or an engine file (an engine model, an older engine ZIP, or a configuration with its model parameters; it is kept, to play again or export). *The world's quests, and this profile's save game*: the quests with their stages, the sessions saved, the latest report sent to the UNP again. The panel folds away once a game is on | `GET /v1/exports/engines`, `POST /v1/engines/{id}/play` (`latest`: the newest), `POST /v1/records/engines/{id}/play`, `POST /v1/engines/load`, `POST /v1/sessions/load`, `GET /v1/missions/playable`, `POST /v1/stats/allocate`, `GET /v1/progression`, `POST /v1/progression/reports/{id}/send` |
| **▶ Play** → the game | Sized to fit a window: the status bar (where, health, coins, the quest's step, an event, who you are talking with); the **place** (its description, who is here ★ quest · ⚠ hostile · ♥ ally, what can be seen); below it **What happened**, the last turn at a glance (your choice and with whom, the check rolled, what came of it, what it changed: ❤ health, 🪙 coins, 🎒 items, 📍 places, 👤 people; the quest step it completed); a button per choice with its check. Beside it the **quest card** (a progress bar, each step with the turn it was done, what to do now), **🧍 You** (power, inventory, stats with what items add, remembered values, the alert, the last check) and *People met & the map*. Below: **Your journey** (every choice as a table), **The story so far** (as a story), **Judge this playthrough**, and *This session as a UNP sees it* (**set the player**, tell it **what happened** in the UNP's game, its **parameters**). Save the game or end it | `POST /v1/sessions/{id}/actions`, `GET /v1/sessions/{id}/transcript`, `GET /v1/sessions/{id}/journal`, `GET /v1/sessions/{id}/savegame`, `POST /v1/sessions/{id}/end`, `POST /v1/sessions/{id}/judge`, `PATCH /v1/sessions/{id}/player`, `POST /v1/sessions/{id}/sync`, `GET /v1/sessions/{id}/state` |
| **⚖ Evaluate** | Judge the engine's stories (current settings or a recorded engine), or compare the current settings with version B on the same seeds (pairwise A/B, both orders, win rate with its 95% interval); fit the fitness to the judge and save the suggested `fitness.term_weights`, or edit them | `POST /v1/jobs/judge-benchmark`, `POST /v1/jobs/calibrate-fitness`, `GET/PUT /v1/fitness/term-weights` |
| **📦 Export** | Download the engine to run offline in a game, *Runs on* a system, Unity or Python, from the **finished runs** (and loaded engines): the latest is chosen, choose several to download each. The Python version can also be made from a generation's champion, the best remembered storyteller, or the game in ▶ Play. A finished training or an engine loaded in ▶ Play is chosen at once. With or without the learning memory. *Where the ANE AI runs*: the standalone runtimes available here (or why not), and the world as the ANE AI sends it (JSON) | `GET /v1/exports/platforms`, `GET /v1/exports/engines`, `POST /v1/exports`, `GET /v1/artifacts/{id}`, `GET /v1/world/catalogue` |
| **⚙ Advanced → Simulation** | Upload and activate a combined configuration YAML, or go back to the built-in files; population, steps, starting and **maximum health**, coins, reproducibility; every hyperparameter as one YAML document; the active configuration (read only) | `POST /v1/config/upload`, `POST /v1/config/reset`, `GET/PUT /v1/settings`, `GET/PUT /v1/hyperparameters`, `GET /v1/config` |
| **⚙ Advanced → Weights** | Difficulty (easier/harder), difficulty over time, chance floor/ceiling, damage mitigation, and every other weight | `GET/PUT /v1/weights` |
| **⚙ Advanced → Engine logic** | Every parameter of the engine's logic, section by section (no code editing) | `GET /v1/logic`, `GET/PUT /v1/logic/{section}` |
| **⚙ Advanced → App & jobs** | The studio's limits, defaults, queue and launch settings; the background jobs started by any client, with their progress | `GET/PUT /v1/app-settings`, `GET /v1/jobs` |

Every endpoint of the API is reached from the studio, directly or through an equivalent
one (the YAML form of a JSON endpoint, the job form of a long one);
[`tests/test_studio_coverage.py`](../tests/test_studio_coverage.py) fails when an endpoint is
added that the studio cannot reach.

What the studio needs to build its forms (entry kinds, suggested tags, the
action reference, repeat rules, goal keys, rule choices, stat choices, NPC roles, the
judge's criteria) comes from `GET /v1/reference`.

Choosing a new **Player profile** name in ▶ Play starts a new player.
Stored values can be edited in *🌍 World → 📜 Story rules → Persistent variables*, for example
`playthroughs: 1`, to test what a returning player sees.

**⚙ Advanced → Engine logic** and **App & jobs** change how the engine behaves
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
  storyteller; 1,000,000 by default, a few minutes on 16 CPUs, since a
  generation's stories are played in parallel). *🧠 Train → Train* shows what a run
  asks for under its sliders, before it starts, as the API counts it (`GET /v1/simulations/work`):
  how long a story is (*Full-length stories enabled: up to 100 turns*, with the configured length,
  or *Bounded stories: up to N turns*), why the steps asked for do not set it when they do not (the
  **Steps per simulation** slider then says *not used: full-length stories* or *capped at N* and is
  outlined), the multiplication (storytellers × generations × stories each × turns = work, of the
  limit), and whether the run would be refused. These are stored in
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
files are never replaced whole. A default file that you deleted from
the volume is restored on the next start, so empty it instead (for example
`world_definition: {events: {}}`) to switch its content off.

Then the volume is brought up to date with the release, entry by entry and setting by setting
(`python -m utils.config_loader upgrade`, logged as `[config] <file>: …`):

- **Added**: what the volume lacks, a setting (e.g. `mission_settings` in an older
  `hyperparameters.yaml`) or a default place, NPC, item, event, mission or variable. A release's
  new entries refer to each other (a new mission names its new NPC), so a file kept from an
  older release next to a newer one could otherwise make a world that cannot be played. An entry
  you delete afterwards stays deleted (the names offered are kept in `.ane-default-entries.json`).
- **Updated**: an entry or a setting whose value is one an earlier release shipped was never
  edited, so it takes this release's default. `config/default_history.json` lists every value
  the defaults have had (`python packaging/default_history.py` rewrites it from git; a test
  fails when it misses a current default).
- **Kept as edited**: anything else, including whatever you added. To take this release's
  defaults for edited ones too, set `ANE_CONFIG_TAKE_DEFAULTS` for a start: names
  (`Aurora,Maskwright`), setting paths (`narrative_end.max_actions`) or `all` (`all` replaces
  entries and settings one by one; your own additions stay). A request applies once per release,
  so it does not undo later edits if left set.

Every file changed is backed up first, in `.ane-backups/<time>/` in the volume. If the world the
updated files would make cannot be built, only the additions are made. After updating, run
`docker compose up --build`.

A world that has a problem from before (such a volume, a file edited by hand) does not block
other edits in the studio: an edit is refused only for the problems it makes, and the save says
which earlier ones remain. A save that changes nothing leaves the file, and its comments, as
they are.

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
