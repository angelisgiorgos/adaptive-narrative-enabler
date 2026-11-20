# Running the ANE: studio, REST API, Docker, terminal

How to run the Adaptive Narrative Enabler and what each tab of the studio does. The REST API
itself is in [API.md](API.md); the ANE AI that a game runs offline is in [UNP.md](UNP.md).

The studio has five destinations in the order of the work, and one for the technical settings:
**🌍 World** (create it, with its story rules) → **🧠 Train** (train the AI storyteller) →
**▶ Play** (play the world) → **⚖ Evaluate** (with the judge) → **📦 Export** (for a game), and
**⚙ Advanced** (the simulation, the weights, the engine's logic, the app and its jobs). Inside a
destination, its sections are tabs, so a section is one click away instead of a long page of
folded panels. **Train only trains**: a training starts no game (no play session, nothing written to
a player's profile); every game is played in ▶ Play (a trained AI, a recorded engine, an engine
file, or a save game). After a training, *Play this result* starts a game with the exact engine it made, and
*Evaluate this result* and *Export this result* open those destinations with it chosen. The records
are where they are used: continuing or retraining a recorded engine in 🧠 Train, its stories in
⚖ Evaluate, its files in 📦 Export, the import folder in ⚙ Advanced → 🗂 Records. The integration diagnostics (a
UNP's view of a game, session reports, runtimes, the world catalogue) are in ⚙ Advanced → 🔌 Integration. Under the header, a status line gives the active world's
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
  destination use the same navigation (🧠 Train's *▶ Play this result*, *⚖ Evaluate this result* and
  *📦 Export this result*, which also carry the completed engine's id).

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
| **🌍 World → 📝 Entries** (pick a content type and an entry; **New entry**) → *Fields* | Name, tags, descriptions, goals (a structured goal, `{goal, tags, needed, reward}`, is a line of YAML and is saved as one); NPC **Unique**, **Encounter event**, **Repeat on every visit**, **Role in the story** and **Secret**; **Enabled if** (locations, NPCs, items); **Event title**; item **Stat bonus while held**; location **Stories can start here** and **Start weight** | `GET /v1/entities/{kind}?fields=names`, `GET/PUT/DELETE /v1/entities/{kind}/{name}` |
| … → *Fields*: an NPC's or a place's behaviour | NPCs: **Comes up to the player** and **How often they are seen**; places: **Draws the player in** and **How many visits a story may have there**. The choices are every tag a rule gives that behaviour (with what it does); choosing one sets that tag on the entry and removes the others of that behaviour (**Save entry** keeps it) | `GET /v1/behaviours` |
| … → *Actions & outcomes* | Pick an action (or add one, or delete one: asked first, with Undo, see *Deleting* below): its name, how often it is offered and whether a failure uses it up, its skill check, exclusive group, the event it starts, its price, the item it needs, uses or takes, and its conditions. Its **outcomes**, picked one by one: what is told, when (a check passed or failed), tags, health and coins, where it moves or sends, what it reveals, who it removes, a new path, a guide, variables and stats, other effects. Every action as YAML under *advanced* | `GET/PUT/DELETE /v1/entities/{kind}/{name}/actions/{index}` (`index` = the number of actions: a new one), `GET/PUT /v1/entities/{kind}/{name}/actions` (all), `/actions/yaml` |
| … → *Scheduling* | Events: how they start (an action, or by themselves with a chance and a weight), cooldown, at most how many times, how often. Items: unique, and where it lies. As YAML under *advanced* | `GET/PUT /v1/entities/{Events\|Items}/{name}/policy` (or `/policy/yaml`) |
| … → *More details* | People: the places and items they know, how the story calls them, and what they say (a line per condition); places: how they are seen from afar, on arrival, the ways to go there, locked exits, and their **points of interest** (a row each: label, prompt, and the place, person, item, action or outcome it leads to; the terminal editor's interaction points) | `GET/PUT /v1/entities/{kind}/{name}/details` |
| … → *✨ Suggest* | The language model's suggestions for the entry's descriptions, goals, tags, views from afar, arrival texts, ways to go there or dialogue, in the world's voice (the terminal editor's suggestions): tick them and add them to the form, then save the entry or its details. Without the language model it says why and how to start it | `POST /v1/suggestions` (503 without the model) |
| … → *Every field (YAML)* | The whole entry as stored, every field | `GET/PUT /v1/entities/{kind}/{name}/yaml` |
| … → **Unsaved changes** | The editor keeps a **draft** in parts: *Fields*, *Actions & outcomes* (the action being edited with all its outcomes, those switched away from included), *Scheduling*, *More details*, and the three YAML editors (the entry, its actions, its scheduling). Each part is compared with what it held right after it was last loaded or saved (its baseline is set from the very values a load or a save puts in the form, in the same step: no later round trip in which a keystroke could pass for loaded), so loading an entry, an action or an outcome is never an edit, and typing a value back is no change. The line under the entry says *✓ No unsaved changes* or *● Unsaved changes in …* with the parts. **Changing the entry, the content type or the action, New entry, New action, another 🌍 World section (🎯 Missions, 🗺 Map, …) or another destination** with unsaved changes does not happen: the choice is undone and the editor asks, naming the parts — **💾 Save** saves each changed part with its own save, then goes on (a part that fails is not saved, the draft, the entry and the question stay, with why; the parts saved before it stop being unsaved; two parts changing the same data, a form and its YAML, are not saved over each other), **Discard** drops the draft (the entry is reloaded as stored) and goes on, **Stay** keeps editing. Another action asks only about the action's draft. A form's own save button saves that part only, then reloads it as stored with what the save made out of date (the entry's YAML, for one), except a part with unsaved changes of its own, which is kept and named; a renamed or new entry is reloaded under its name. A failed save changes nothing. An edit sends only its own part to the server, and leaving checks only which parts are unsaved, not the drafts. Closing or reloading the page with unsaved changes makes the browser ask first (it can only offer Leave or Stay); the unsaved parts are also kept in this browser, and when the studio opens again *Unsaved changes from your last visit* offers **Restore** (the entry opens with them, still unsaved) or **Discard**. **🎯 Missions**, **📜 Story rules** (the rules, the messages, the variables, a profile's stored values, the stats) and **🗺 Map** (the borders, the map YAML) track their drafts the same way, each with its indicator and its question: changing the mission, the profile or the place, *New mission*, or leaving with unsaved changes asks Save, Discard or Stay; a mission's undo does not replace unsaved mission edits | the endpoints above |
| … → **Deleting** (*Delete entry*, *🗑 Delete action*, and *🗑 Delete mission* in 🎯 Missions) | Nothing is deleted at once: a question names exactly what goes — the entry (its content type, name and place in the list, e.g. *NPCs, 18th of 20*, with its number of actions, if any its unsaved changes, and **what refers to it** and would no longer work: missions, actions, conditions, dialogue, routes, as the engine's checks word them), the action (its number and stored name, its entry, its outcomes) or the mission (title, key, place, steps) — with **🗑 Delete** and **Cancel**. Confirmed, exactly that one goes, even if the form moved on meanwhile; an action is deleted only if the one at its number still has that name. The rules stay the API's: the last place, person or item cannot go (the question then says why, without its Delete button); events may all go. After a deletion, a bar says what went, with **↶ Undo**; each panel keeps a history (the bar names the newest and how many earlier ones can be undone after it, newest first), kept in this browser across reloads, and the API keeps the deletions with the saves (`deletions.json`, the most recent 200), so an undo survives its restart: the record comes back whole, where it was (next to its old neighbour if the list changed meanwhile, and said), and opens (unless the editor has unsaved changes: kept, the list shows it). If its name was taken again, or an action's entry is gone, nothing is overwritten: the bar says why, and Undo can be tried again once the name is free | `DELETE /v1/entities/{kind}/{name}`, `DELETE /v1/entities/{kind}/{name}/actions/{index}?expected_name=`, `DELETE /v1/missions/{key}`, `POST /v1/deletions/{token}/restore`, `GET /v1/entities/{kind}/{name}/references` |
| **🌍 World → 🎯 Missions** | Pick a mission (or add one, or delete one: asked first, with Undo, see *Deleting* below): key, title, description, weight, its condition and rewards, and its steps as a table (title, what the player is told, the goal, the rewards). Every mission as YAML under *advanced* | `GET/PUT/DELETE /v1/missions/{key}` (`rename`), `GET/PUT /v1/documents/missions` |
| **🌍 World → 📜 Story rules** | *Story rules* (mission pacing and route weight, default repeat rules, arrest location and confinement, following new paths, persistence, event fallback); *Story messages* (every sentence the engine writes); *Persistent variables* (definitions; a profile's stored values, reset to a new player); *Character stats* (stat names, point budget, allocation mode, tag checks, damage-reduction stat) | `GET/PUT /v1/rules`, `GET/PUT /v1/documents/messages`, `GET/PUT /v1/documents/variables`, `GET/PUT /v1/documents/stored_values?profile=`, `POST /v1/variables/reset/yaml?profile=`, `GET /v1/variables/profiles`, `GET/PUT /v1/documents/stats` |
| **🌍 World → 🗺 Map** | Pick a place and tick the places it borders (routes change both ways); the whole map is drawn beside it. The map as YAML under *advanced* | `GET /v1/map`, `PUT /v1/map/{place}`, `PUT /v1/map`, `GET/PUT /v1/documents/map` |
| **🌍 World → 🏷 Behaviour by tags** | Two tables, people and places: a row per rule (its tags, and what each behaviour becomes), and what everyone does without one. A row with a new tag is a new choice in *Fields*; an invalid rule is refused with why | `GET /v1/behaviours/rules`, `PUT /v1/behaviours/rules/{npcs\|locations}` |
| **🌍 World → ✨ Grow the world** | The world's size against the augmentation targets; the language model proposes new places, people and items (a background job; nothing written), shown ticked and as editable YAML; **Add the ticked ones** adds them once each is new and the whole world checks with them in place. Replaces the old augmenter's direct overwrite of the config files | `GET /v1/world/size`, `POST /v1/jobs/world-proposal`, `POST /v1/world/additions` |
| **🌍 World → 🔍 Checks** | The actions whose failure changes nothing but health or coins and that no complication fits | `GET /v1/lint/failures` |
| **🧠 Train → Run** | Where a run is configured, followed and its result used, in three steps. **1 · Configure the run.** A run uses the saved engine defaults (⚙ Advanced → Simulation) except what is set for that run: each setting (population, generations, stories per storyteller, steps, the seed) shows its saved default and is used as it is unless *Set … for this run* is ticked; reproducibility and warm start are *Saved default*, or chosen for the run. Before a run starts, **This run, resolved** shows what it will execute, as the API resolves it: each value and where it comes from (set for this run, or the saved default), the story length (*Full-length stories enabled: up to N turns*, or the steps up to the story's end; the steps slider marked when it does not set it), the seed, the warm start, the work as storytellers × generations × stories × turns against the limit, and why it would be refused. **2 · Progress and the completed engine.** While **🧠 Evolve storytellers** runs, its progress (generation by generation) shows on the result card; when it finishes, the card names the **completed engine** (its id, its run, its best fitness and generations) and whether the run executed exactly what its summary showed (with a warm start: how many remembered storytellers it really started from). The card, with its actions, comes back after a page reload in the same browser, while that engine is still a finished run. **▶ Play this result** starts a game with that engine in ▶ Play (with the player, quest and character chosen there), **⚖ Evaluate this result** opens ⚖ Evaluate with it as the storyteller of the judge benchmark and the fitness calibration (nothing is judged until started), **📦 Export this result** opens 📦 Export with it as the one finished run chosen. Each carries the engine's exact id, never the newest in its place: an engine no longer offered there is said so and nothing is chosen. **3 · Learning results**: the **learning curve**, **what the storyteller learned**, the run's report and files; forget the learning memory. A training here starts no game (the studio asks the API with `start_game: false`) | `POST /v1/simulations/resolve`, `POST /v1/jobs/simulations` and `GET /v1/jobs/{id}` (or `POST /v1/simulations`), `GET/DELETE /v1/learning`, `GET /v1/exports/engines` |
| **🧠 Train → From a recorded engine** | Recorded engines: continue one (its own settings) or retrain from it (its saved defaults with *Run*'s settings and the further generations, resolved and shown before it starts; when it finishes, *Run* opens with its completed engine and the same three actions; no game is started). Its files are in 📦 Export, its stories in ⚖ Evaluate | `GET /v1/records/engines`, `POST /v1/jobs/engines/{id}/continue\|retrain` |
| **▶ Play** → *🎮 New game* | Choose the **trained AI** (finished runs and loaded engines, newest first; also without the records), the **player profile**, the **quest** (any quest open to the profile, or one the story draws) and the **character** (story default, random, balanced, or *my own points*, set with Randomize/Balance), then **▶ New game**, or **📂 Continue a save game** (saved here or by the ANE AI; exactly, or rebuilt from its parameters). *Another engine*: a recorded engine, or an engine file (an engine model, an older engine ZIP, or a configuration with its model parameters; it is kept, to play again or export). *The world's quests, and this profile's save game*: the quests with their stages, the sessions saved (and whether each report reached the UNP). The panel folds away once a game is on; 🧠 Train's *Play this result* opens it with its completed engine chosen | `GET /v1/exports/engines`, `POST /v1/engines/{id}/play` (`latest`: the newest), `POST /v1/records/engines/{id}/play`, `POST /v1/engines/load`, `POST /v1/sessions/load`, `GET /v1/missions/playable`, `POST /v1/stats/allocate`, `GET /v1/progression` |
| **▶ Play** → the game | Sized to fit a window: the status bar (where, health, coins, the quest's step, an event, who you are talking with); the **place** (its description, who is here ★ quest · ⚠ hostile · ♥ ally, what can be seen); below it **What happened**, the last turn at a glance (your choice and with whom, the check rolled, what came of it, what it changed: ❤ health, 🪙 coins, 🎒 items, 📍 places, 👤 people; the quest step it completed); a button per choice with its check. Beside it the **quest card** (a progress bar, each step with the turn it was done, what to do now), **🧍 You** (power, inventory, stats with what items add, remembered values, the alert, the last check) and *People met & the map*. Below: **Your journey** (every choice as a table), **The story so far** (as a story), and **Judge this playthrough**. Save the game or end it. (How a UNP sees this game is in ⚙ Advanced → 🔌 Integration.) | `POST /v1/sessions/{id}/actions`, `GET /v1/sessions/{id}/transcript`, `GET /v1/sessions/{id}/journal`, `GET /v1/sessions/{id}/savegame`, `POST /v1/sessions/{id}/end`, `POST /v1/sessions/{id}/judge` |
| **⚖ Evaluate** | Judge the engine's stories (current settings, a finished run, also one kept without the records, or another recorded engine; 🧠 Train's *Evaluate this result* chooses its completed engine), or compare the current settings with version B on the same seeds (pairwise A/B, both orders, win rate with its 95% interval); fit the fitness to the judge and save the suggested `fitness.term_weights`, or edit them; *Recorded stories*: read one, judge it, or compare two (which is the better game) | `POST /v1/jobs/judge-benchmark`, `POST /v1/jobs/calibrate-fitness`, `GET/PUT /v1/fitness/term-weights`, `GET /v1/records/stories[/{id}]`, `POST /v1/records/stories/{id}/judge`, `POST /v1/judge/compare` |
| **📦 Export** | Download the engine to run offline in a game, *Runs on* a system, Unity or Python, from the **finished runs** (and loaded engines): the latest is chosen, choose several to download each. The Python version can also be made from a generation's champion, the best remembered storyteller, or the game in ▶ Play. A finished training or an engine loaded in ▶ Play is chosen at once; 🧠 Train's *Export this result* chooses exactly its completed engine. With or without the learning memory. The runtimes available here and the world catalogue are in ⚙ Advanced → 🔌 Integration. *Recorded engines, runs and worlds*: download a recorded engine's model file and world, see a run's settings and every generation, download a recorded world as a configuration to upload | `GET /v1/exports/platforms`, `GET /v1/exports/engines`, `POST /v1/exports`, `GET /v1/artifacts/{id}`, `GET /v1/records/engines/{id}/model`, `GET /v1/records/runs[/{id}]`, `GET /v1/records/worlds[/{id}]` |
| **⚙ Advanced → Simulation** | Upload and activate a combined configuration YAML, or go back to the built-in files; the **training defaults** (default population, generations, stories per storyteller, steps, deterministic runs and seed: what a run uses for whatever 🧠 Train does not set for it; the terminal and API clients use them too); the game: starting and **maximum health**, coins and the **story length**; every hyperparameter as one YAML document; the active configuration (read only) | `POST /v1/config/upload`, `POST /v1/config/reset`, `GET/PUT /v1/settings`, `GET/PUT /v1/hyperparameters`, `GET /v1/config` |
| **⚙ Advanced → Weights** | Two views of one draft. **Basic** (shown first) has 20 common gameplay controls, chosen from what the engine does with them. They are grouped as *Difficulty* (base check difficulty, its increase per turn and the turn it starts, the minimum and maximum success chance, the bonus after a failed try, the guard penalty per alert level), *Pacing* (pull toward unvisited places, toward novelty when the story is dull, away from exhausted places, toward the mission), *Repetition* (repeating a choice, a repeated success's harder check and its cap, going straight back, retrying at once, repeated event effects) and *Event frequency* (event pressure and its threshold, the chance of a scene on a first trip). Each control is one parameter (`backend/tuning_basic.yaml`: no presets, nothing set besides it), explained by what raising or lowering it does in play, with its unit, default and path. **Advanced** is the complete editor below; a field that Basic also shows says *Also in Basic → area*. A value edited in either view is the same value in the other, and switching views changes no value. The draft's status, above both views, lists every unsaved change; those made in Advanced to parameters that Basic doesn't show are listed apart (*⚠ Advanced only, not shown in Basic*). *Save changes* saves only the values that changed. A parameter someone else changed after the page loaded is refused, not overwritten. *Discard changes* reloads the saved values. The Advanced view shows every tuning parameter (difficulty and its increase per turn, chance floor and ceiling, damage mitigation, the storyteller's and the score's weights, ...), by section. Each is shown by a readable label (e.g. *Difficulty increase per turn*), with what the engine does with it, its unit on its own scale (a percentage of the 1–100 roll, a probability per turn, an event pressure, score points per ...), the engine's default (the value it uses when the parameter is left out), the bounds where the engine sets them (`story_rules`, the score's length floor, the two exponents that must not be negative), whole numbers only where the engine counts in whole numbers, and its path (`skill_check_resolution.difficulty_rate_per_step`) under it. A parameter that only acts with a setting on (the 19 of `outcome_resolution` that need `outcome_resolution.hidden_adjustments`; the director's, variation's, the unexpected events', first trips' and the alert's with their `enabled`) is greyed and says *Inactive now* while it is off. Parameters the engine no longer has are not offered (an older config folder may keep them; they have no effect). A parameter without a description (one added later) shows its name, its path and its type as written. Saving checks every value first (a fraction for a whole-number parameter, or a value outside the engine's bounds, saves nothing) and keeps the values you did not change exactly as written; only the numbers that change are rewritten, so the file's comments and layout stay | `GET/PUT /v1/weights` |
| **⚙ Advanced → Engine logic** | Every parameter of the engine's logic, section by section (no code editing) | `GET /v1/logic`, `GET/PUT /v1/logic/{section}` |
| **⚙ Advanced → App & jobs** | The studio's limits, defaults, queue and launch settings; the background jobs started by any client, with their progress | `GET/PUT /v1/app-settings`, `GET /v1/jobs` |
| **⚙ Advanced → 🗂 Records** | Whether recording is on, and where; **import** the engines and games in the server's import folder (`ANE_ARTIFACT_DIR`, else `artifacts/`; copy or move, preview stories), as `python main.py --import-artifacts` does | `GET /v1/records`, `GET /v1/records/import`, `POST /v1/jobs/records-import` |
| **⚙ Advanced → 🔌 Integration** | Diagnostics for a UNP (the game platform that runs the exported ANE AI). *The game in ▶ Play as a UNP sees it*: it names the session it acts on; **set the player**, tell it **what happened** in the UNP's game, show its **parameters** (`ane.session_state/1`; shown again after each change, which ▶ Play's screen follows too). *Session reports sent to the UNP*: send a save game's latest report again. *Where the ANE AI runs*: the standalone runtimes available here (or why not), and the world as the ANE AI sends it (JSON) | `PATCH /v1/sessions/{id}/player`, `POST /v1/sessions/{id}/sync`, `GET /v1/sessions/{id}/state`, `GET /v1/progression`, `POST /v1/progression/reports/{id}/send`, `GET /v1/exports/platforms`, `GET /v1/world/catalogue` |

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
  queue settings, and host/port/share (a run's sizes are the training defaults in *⚙ Advanced →
  Simulation*, or what 🧠 Train sets for it; the former `default_candidates`, `default_generations`
  and `default_simulation_steps` are gone, and ignored in a saved file). The slider limits
  go up to 400 candidates, 1000 generations, and 400 steps per simulation (the
  largest run the API accepts); `max_simulation_work` caps the total simulated
  turns of one run (candidates × generations × story length × stories per
  storyteller; 1,000,000 by default, a few minutes on 16 CPUs, since a
  generation's stories are played in parallel). *🧠 Train → Run* shows what a run
  asks for under its sliders, before it starts, as the API counts it (`POST /v1/simulations/resolve`):
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
