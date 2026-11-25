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
  elements (`#dest-<id>`), the menu's values, and the address: `?section=play` opens ▶ Play. The
  address follows the destination open, however it was opened (the banner, the menu, a guard's answer,
  a link such as *▶ Play this result*), so a reload or a copied link opens it again; it is replaced, not
  added to the browser's history. A destination chosen in the banner or the menu is marked at once,
  before the server answers, unless an editor has unsaved changes (its guard may then keep the page
  where it is, and the answer decides).
- Changing destination only selects a tab: nothing is rebuilt or reloaded, so forms being edited,
  a game in progress, a training's results and every other value stay as they were. Links inside a
  destination use the same navigation (🧠 Train's *▶ Play this result*, *⚖ Evaluate this result* and
  *📦 Export this result*, which also carry the completed engine's id).

## Running

Start the application:

```bash
python main.py
```

Or launch the browser-based authoring and play studio. It does everything
through the REST API over HTTP and never serves the API itself, so start the
API first (the studio looks for it at `http://127.0.0.1:8005` unless
`ANE_API_URL` says otherwise):

```bash
python -m uvicorn api:app --port 8005 &
python app.py                                        # the API at http://127.0.0.1:8005
ANE_API_URL=http://ane.hamlet.iti.gr python app.py   # or the public deployment
```

### Docker, UI, and REST API

Build and start the containerized studio:

```bash
docker compose up --build
```

The complete UI is available at `http://localhost:8003`, the REST API at
`http://localhost:8005/v1`, and interactive API documentation at
`http://localhost:8005/docs`. The public deployment is the same API at
<http://ane.hamlet.iti.gr> (documentation at <http://ane.hamlet.iti.gr/docs>). The judge (Gemma 4 on vLLM, a GPU) is a separate service, off
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
too. `StudioAPI`, in [`studio_client.py`](../studio_client.py), is the connection, and it is
always HTTP:

- To `ANE_API_URL` when it is set: Compose sets `http://hamlet-ane-api:8005`
  (`ANE_STUDIO_API_URL` changes it, e.g. to the public deployment
  `http://ane.hamlet.iti.gr`).
- To `http://127.0.0.1:8005` when it is unset or empty (`python app.py` beside
  `python -m uvicorn api:app --port 8005`).

The studio and every other client therefore share one engine: the same sessions,
runs, learning memory, and records. The studio keeps no data of its own (Compose
gives it none of the API's volumes); if the API cannot be reached, it says so.

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
| **🌍 World**: its sub-navigation | Three places, chosen with the bar under the workflow banner. **📚 Content** opens the content's own row (📝 Entries, 📜 Story rules, 🏷 Behaviour by tags, ✨ Grow the world, 🔍 Checks) and goes back to the last of them opened. **🎯 Missions** holds the missions with their steps, conditions, rewards and YAML. **🗺 Map** is the authoring map: borders and the map YAML. The World's own tab bar is not shown; the bar is the one way between sections, and it follows the section open, however it was opened. Switching keeps every selection (the entry, the mission, the place). It keeps unsaved drafts too: leaving an editor with unsaved changes asks first, in that editor (**💾 Save**, **Discard**, **Stay**), as the section tabs did | the endpoints of each section |
| **🌍 World → 📝 Entries** (pick a content type and an entry; **New entry**) → *Fields* | **📚 Content library** (above the editor): every location, NPC, item and event in one list. Each line gives the type's icon, the name, a few facts (a start place, an NPC's role and whether it is unique, a hostile encounter, an event's title, an item's bonus), its tags and its first description. **Search** matches every word typed against names, tags, descriptions and facts. **Type** filters to one content type, each with its count (*found of all* while searching). Searching and filtering never change the entry open in the editor; when they hide it, the status says it stays open. Choosing an entry opens it in the editor, in its own content type, like the pickers below: with unsaved changes, the editor asks first (Save, Discard or Stay), and the library keeps the open entry marked. Saves, renames, deletions and undo list it again. Changes made elsewhere (another browser, Grow the world, an import, a file edited by hand) are seen within about 10 seconds, by the world's revision (`GET /v1/world/revision`), and the library lists them without a click; ↻ Refresh does so at once. If the entry open in the editor was renamed or deleted elsewhere, the library says so. The library lists 200 results at a time: *Show N more* lists the next ones, and a new search or filter starts again from the first page. The list always reaches the entry open in the editor, so it stays marked. The library also says when the world has no entries, when a type has none yet (with how to add one), and when nothing matches the search. Name, tags, descriptions, goals (a structured goal, `{goal, tags, needed, reward}`, is a line of YAML and is saved as one); NPC **Unique**, **Encounter event**, **Repeat on every visit**, **Role in the story** and **Secret**; **Enabled if** (locations, NPCs, items); **Event title**; item **Stat bonus while held**; location **Stories can start here** and **Start weight** | `GET /v1/conditions/reference`, `POST /v1/conditions/validate`, `PUT /v1/entities/{kind}/{name}/draft`, `GET /v1/entities/{kind}?fields=summary`, `GET /v1/entities/{kind}?fields=names`, `GET/PUT/DELETE /v1/entities/{kind}/{name}` |
| … → *Fields*: an NPC's or a place's behaviour | NPCs: **Comes up to the player** and **How often they are seen**; places: **Draws the player in** and **How many visits a story may have there**. The choices are every tag a rule gives that behaviour (with what it does); choosing one sets that tag on the entry and removes the others of that behaviour (**Save entry** keeps it) | `GET /v1/behaviours` |
| … → *Actions & outcomes* | Pick an action (or add one, or delete one: asked first, with Undo, see *Deleting* below): its name, how often it is offered and whether a failure uses it up, its skill check, exclusive group, the event it starts, its price, the item it needs, uses or takes, and its conditions. Its **outcomes**, picked one by one: what is told, when (a check passed or failed), tags, health and coins, where it moves or sends, what it reveals, who it removes, a new path, a guide, variables and stats, other effects. Every action as YAML under *advanced* | `GET/PUT/DELETE /v1/entities/{kind}/{name}/actions/{index}` (`index` = the number of actions: a new one), `GET/PUT /v1/entities/{kind}/{name}/actions` (all), `/actions/yaml` |
| … → *Scheduling* | Events: how they start (an action, or by themselves with a chance and a weight), cooldown, at most how many times, how often. Items: unique, and where it lies. As YAML under *advanced*. Places and people have no scheduling (their behaviour comes from their tags): Save and the YAML are shown for events and items only | `GET/PUT /v1/entities/{Events\|Items}/{name}/policy` (or `/policy/yaml`) |
| … → *More details* | People: the places and items they know, how the story calls them, and what they say (a line per condition); places: how they are seen from afar, on arrival, the ways to go there, locked exits, and their **points of interest** (a row each: label, prompt, and the place, person, item, action or outcome it leads to; the terminal editor's interaction points) | `GET/PUT /v1/entities/{kind}/{name}/details` |
| … → *✨ Suggest* | The language model's suggestions for the entry's descriptions, goals, tags, views from afar, arrival texts, ways to go there or dialogue, in the world's voice (the terminal editor's suggestions): tick them and add them to the form, then save the entry or its details. Without the language model it says why and how to start it | `POST /v1/suggestions` (503 without the model) |
| … → *Every field (YAML)* | The whole entry as stored, every field | `GET/PUT /v1/entities/{kind}/{name}/yaml` |
| … → **Unsaved changes** | The editor keeps a **draft** in parts: *Fields*, *Actions & outcomes* (the action being edited with all its outcomes, those switched away from included), *Scheduling*, *More details*, and the three YAML editors (the entry, its actions, its scheduling). Each part is compared with what it held right after it was last loaded or saved (its baseline is set from the very values a load or a save puts in the form, in the same step: no later round trip in which a keystroke could pass for loaded), so loading an entry, an action or an outcome is never an edit, and typing a value back is no change. The line under the entry names the sections with unsaved changes and those saved: *● Unsaved changes in Harbor: Fields · ✓ Saved: Actions & outcomes, …*, listing only the sections an entry of its type has, or *✓ No unsaved changes: every section is saved*. **💾 Save changes**, always under that line, saves every section with unsaved changes at once. The sections are applied to the stored entry, keeping every field no section touches, and the entry is checked whole with the world. It is then written once, or, when anything is wrong (an invalid action, a condition, a field), not at all: every problem is named with its section, and the draft stays. After a save the whole entry is reloaded as stored, so the forms and the YAML show the same entry. **A rename renames what refers to the entry**: every reference by its exact name (the map's routes, the people who know a place or an item, other entries' actions and conditions, missions, variables, the tuning parameters that name a place) in the same save, the other files edited in place with their comments kept; the message counts them per file. A name that also names something of another kind (a place and an item both called *Lantern*) is ambiguous: its references are not renamed, and a rename that would leave them dangling is refused, saying why. The entry form's own *Save entry* renames them too. **👁 Preview the draft** (below the editor) shows the entry as the unsaved draft makes it, without saving anything. *Content (static)* is the content as written: its texts, facts, dialogue, each action with what it needs, and each outcome's text with what it changes. It also lists what a save would refuse. *Simulated scene* is a sample scene the engine plays with the draft, in a separate process, and lists its sample state first: the start (the place itself, a place an NPC knows, an item's place), the seed, a random storyteller, the story's default character, persistent variables at their defaults (no profile's), and the quest the story draws. A person, an item or an event is brought into the scene. Each of the entry's actions is tried on its own copy of that state: what the engine tells, its check and what it changes (the place, health, coins, items, variables, the end, and the map: a path that opens or closes, a place unlocked, being held). Previewing writes no configuration, changes no player's progression, records nothing and advances no game. **🧩 Condition builder**, under every condition field: an entry's *Enabled if*, an action's *Offered only if* and *No longer offered once*, a mission's *Offered only if* (🎯 Missions), and a dialogue line's *Said when* (*More details*: choose the line; its condition is kept in step with the table both ways): *Edit the condition as* **Builder** shows the condition as a numbered tree of groups (ALL of, ANY of, NOT) and predicates. The predicates are: player has item, item collected, visited location, player is at location, NPC removed, event done / succeeded / failed, action done, outcome tag, mission is, an NPC's secret known, and variable, stat and coins/health/turn comparisons. A part of the condition is chosen, then a predicate or a group is ➕ added into it, ✏ replaces it, or it is 🗑 removed. The names come from the world (`GET /v1/conditions/reference`), and every change is checked as a save checks it (`POST /v1/conditions/validate`), naming unknown items, places, variables or stats. In Builder mode the YAML field is written by the builder only, and the entry's draft follows it. A condition it reads is written back exactly as it was: the variables' shorthand, a list, an `op` left out. A part it cannot show is listed as *kept as written* and never dropped or simplified. **Advanced YAML** is the field, edited as text; YAML that cannot be read stays there, with why, until it is corrected. **Changing the entry, the content type or the action, New entry, New action, another 🌍 World section (🎯 Missions, 🗺 Map, …) or another destination** with unsaved changes does not happen: the choice is undone and the editor asks, naming the parts — **💾 Save** saves every changed part at once, as *Save changes* does, then goes on. If any part is wrong, nothing is saved, and the draft, the entry and the question stay, with why. Two parts changing the same data, a form and its YAML, are not saved over each other, **Discard** drops the draft (the entry is reloaded as stored) and goes on, **Stay** keeps editing. Another action asks only about the action's draft. A form's own save button saves that part only, then reloads it as stored with what the save made out of date (the entry's YAML, for one), except a part with unsaved changes of its own, which is kept and named; a renamed or new entry is reloaded under its name. A failed save changes nothing. An edit sends only its own part to the server, and leaving checks only which parts are unsaved, not the drafts. Closing or reloading the page with unsaved changes makes the browser ask first (it can only offer Leave or Stay); the unsaved parts are also kept in this browser, and when the studio opens again *Unsaved changes from your last visit* offers **Restore** (the entry opens with them, still unsaved) or **Discard**. **🎯 Missions**, **📜 Story rules** (the rules, the messages, the variables, a profile's stored values, the stats) and **🗺 Map** (the borders, the map YAML) track their drafts the same way, each with its indicator and its question: changing the mission, the profile or the place, *New mission*, or leaving with unsaved changes asks Save, Discard or Stay; a mission's undo does not replace unsaved mission edits | the endpoints above |
| … → **Deleting** (*Delete entry*, *🗑 Delete action*, and *🗑 Delete mission* in 🎯 Missions) | Nothing is deleted at once: a question names exactly what goes — the entry (its content type, name and place in the list, e.g. *NPCs, 18th of 20*, with its number of actions, if any its unsaved changes, and **what refers to it** and would no longer work: missions, actions, conditions, dialogue, routes, as the engine's checks word them), the action (its number and stored name, its entry, its outcomes) or the mission (title, key, place, steps) — with **🗑 Delete** and **Cancel**. Confirmed, exactly that one goes, even if the form moved on meanwhile; an action is deleted only if the one at its number still has that name. The rules stay the API's: the last place, person or item cannot go (the question then says why, without its Delete button); events may all go. After a deletion, a bar says what went, with **↶ Undo**; each panel keeps a history (the bar names the newest and how many earlier ones can be undone after it, newest first), kept in this browser across reloads, and the API keeps the deletions with the saves (`deletions.json`, the most recent 200), so an undo survives its restart: the record comes back whole, where it was (next to its old neighbour if the list changed meanwhile, and said), and opens (unless the editor has unsaved changes: kept, the list shows it). If its name was taken again, or an action's entry is gone, nothing is overwritten: the bar says why, and Undo can be tried again once the name is free | `DELETE /v1/entities/{kind}/{name}`, `DELETE /v1/entities/{kind}/{name}/actions/{index}?expected_name=`, `DELETE /v1/missions/{key}`, `POST /v1/deletions/{token}/restore`, `GET /v1/entities/{kind}/{name}/references` |
| **🌍 World → 🎯 Missions** | Pick a mission (or add one, or delete one: asked first, with Undo, see *Deleting* below): key, title, description, weight, its condition and rewards, and its steps as a table (title, what the player is told, the goal, the rewards). Every mission as YAML under *advanced*. A new mission (or a renamed one) needs a key no other mission has; it never replaces one. Weight 0 is kept (a quest never drawn). One step that holds only its goal is a one-goal mission; a step with its own text or rewards stays a step. After the mission in the form is deleted, the form no longer stands for it (Save cannot bring it back; unsaved edits stay, as a new mission) | `GET/PUT/DELETE /v1/missions/{key}` (`rename`), `GET/PUT /v1/documents/missions` |
| **🌍 World → 📜 Story rules** | *Story rules* (mission pacing and route weight, default repeat rules, arrest location and confinement, following new paths, persistence, event fallback); *Story messages* (every sentence the engine writes); *Persistent variables* (definitions; a profile's stored values, reset to a new player: asked first, as it cannot be undone); *Character stats* (stat names, point budget, allocation mode, tag checks, damage-reduction stat) | `GET/PUT /v1/rules`, `GET/PUT /v1/documents/messages`, `GET/PUT /v1/documents/variables`, `GET/PUT /v1/documents/stored_values?profile=`, `POST /v1/variables/reset/yaml?profile=`, `GET /v1/variables/profiles`, `GET/PUT /v1/documents/stats` |
| **🌍 World → 🗺 Map** | **The authoring map**: every place and every route of the world, as authored, from which every story's map is made. A player never sees it whole: ▶ Play shows each game's **discovered map** (🧭, under *People met & the discovered map*), only the places that player has found and the ways to them. Pick a place and tick the places it borders (routes change both ways); the whole map is drawn beside it. The map as YAML under *advanced*. A place deleted meanwhile is not saved to: the list follows the world | `GET /v1/map`, `PUT /v1/map/{place}`, `PUT /v1/map`, `GET/PUT /v1/documents/map` |
| **🌍 World → 🏷 Behaviour by tags** | Two tables, people and places: a row per rule (its tags, and what each behaviour becomes), and what everyone does without one. A row with a new tag is a new choice in *Fields*; an invalid rule is refused with why. Each table is saved on its own, and saving one keeps the other's unsaved edits; a line says which have unsaved changes, and closing the page with them asks first | `GET /v1/behaviours/rules`, `PUT /v1/behaviours/rules/{npcs\|locations}` |
| **🌍 World → ✨ Grow the world** | **Augment the world with the language model**: a tick, saved (`augmentation.enabled`, what `python main.py` reads too); off, *Propose* is disabled and nothing is proposed. Beside it, whether augmentation's own model (**Qwen3.5-4B** on vLLM, not the judge's Gemma) is ready, or why not and how to start it. The world's size against the augmentation targets; the augmentation model proposes new places, people and items (a background job; nothing written), shown ticked and as editable YAML (the ticks follow the YAML: an entry renamed or added there is offered, ticked); **Add the ticked ones** adds them once each is new and the whole world checks with them in place. Replaces the old augmenter's direct overwrite of the config files | `GET`/`PUT /v1/augmentation`, `GET /v1/world/size`, `POST /v1/jobs/world-proposal`, `POST /v1/world/additions` |
| **🌍 World → 🔍 Checks** | The actions whose failure changes nothing but health or coins and that no complication fits | `GET /v1/lint/failures` |
| **🧠 Train → Run** | Where a run is configured, followed and its result used, in three steps. **1 · Configure the run.** A run uses the saved engine defaults (⚙ Advanced → Simulation) except what is set for that run: each setting (population, generations, stories per storyteller, steps, the seed) shows its saved default and is used as it is unless *Set … for this run* is ticked; reproducibility and warm start are *Saved default*, or chosen for the run. Before a run starts, **This run, resolved** shows what it will execute, as the API resolves it: each value and where it comes from (set for this run, or the saved default), the story length (*Full-length stories enabled: up to N turns*, or the steps up to the story's end; the steps slider marked when it does not set it), the seed, the warm start, the work as storytellers × generations × stories × turns against the limit (the turns include those a story may spend in fast travel, `travel.max_free_turns`, which do not count toward its end, when the steps allow them), and why it would be refused. **2 · Progress and the completed engine.** While **🧠 Evolve storytellers** runs, its progress (generation by generation) shows on the result card; when it finishes, the **result card** shows the **completed engine**: its name (or its label), its engine and run ids, when it was made, its world (its size, and whether it is the active world), its generations and the settings its training ran with, its **training fitness** (the evolution's objective) and, once judged, the **judge's engagement** (1–10, a different measure, kept apart), and whether the run executed exactly what its summary showed (with a warm start: how many remembered storytellers it really started from). Under the card, **Name (optional)** and **🏷 Save name** name the engine. The name replaces the date in every picker of Train, Play, Evaluate and Export, and in an export's file name. The engine and run ids don't change, and an empty name removes it. The card, with its actions, comes back after a page reload in the same browser, while that engine is still a finished run. **▶ Play this result** starts a game with that engine in ▶ Play (with the player, quest and character chosen there), **⚖ Evaluate this result** opens ⚖ Evaluate with it as the storyteller of the judge benchmark and the fitness calibration (nothing is judged until started), **📦 Export this result** opens 📦 Export with it as the one finished run chosen. Each carries the engine's exact id, never the newest in its place: an engine no longer offered there is said so and nothing is chosen. **3 · Learning results**: the **learning curve**, **what the storyteller learned**, the run's report and files; forget the learning memory. A training here starts no game (the studio asks the API with `start_game: false`) **⏱ Jobs** (under the run button) lists the trainings queued, running and recently ended. Each job shows its state, its progress counted as the job counts it (generation n/N), its best, mean and benchmark scores, and how long it has queued, run or took. A queued job says how many jobs run before it (one runs at a time, in order). A failure shows its error and what to do about it. **Open its result** puts a finished training on the result card, with its Play, Evaluate and Export actions. The panel refreshes by itself every 2 seconds while a training is queued, running or stopping, and every 10 seconds otherwise (so a job started elsewhere shows up too). The studio stays usable meanwhile. A second click while a run is pending is ignored, and an identical submission while one is queued or running follows that job instead of queuing another. **Your job survives a refresh:** the browser remembers the training it started (local storage, this section only). After a refresh or a lost connection, the page follows it again, without starting another, and the result card comes back when it finishes. The panel marks it **yours (this browser)**. The page never attaches itself to anyone else's job from the list. A failed job says why; a lost one says so (an API restart loses every queued and running job, because jobs are kept in memory only, as are the latest 100). **🚫 Cancel it** cancels a queued or running training: a queued one never starts; a running one stops after its current generation (*cancellation requested* until then), keeps no engine and no result (it says how far it got), and is recorded as cancelled. One that completes before stopping is finished, and says so. | `GET /v1/jobs`, `GET /v1/jobs/{id}`, `POST /v1/simulations/resolve`, `POST /v1/jobs/simulations` and `GET /v1/jobs/{id}` (or `POST /v1/simulations`), `GET/DELETE /v1/learning`, `GET /v1/exports/engines`, `GET /v1/engines/{id}/card`, `PUT /v1/engines/{id}/name` |
| **🧠 Train → 🏷 Finished engines** | Every finished run and loaded engine (and, with the records, the other recorded engines), each by its label: its name, or what it is, when it was made, its generations and training fitness, with its short id. The chosen engine's **result card** and **Name (optional)** / **🏷 Save name**. Names are kept with the saves (`engine_meta.json`), so they work with recording off and after a restart. An unnamed engine keeps its label, such as *Run of 2026-11-18 14:02 · 25 generations · fitness 41.2 · 1a2b3c4d* | `GET /v1/exports/engines`, `GET /v1/records/engines`, `GET /v1/engines/{id}/card`, `PUT /v1/engines/{id}/name` |
| **🧠 Train → From a recorded engine** | Recorded engines: continue one (its own settings) or retrain from it (its saved defaults with *Run*'s settings and the further generations, resolved and shown before it starts; when it finishes, *Run* opens with its completed engine and the same three actions; no game is started). Its files are in 📦 Export, its stories in ⚖ Evaluate | `GET /v1/records/engines`, `POST /v1/jobs/engines/{id}/continue\|retrain` |
| **▶ Play** → *🎮 New game* | Choose the **trained AI** (finished runs and loaded engines, newest first; also without the records), the **player profile**, the **quest** (any quest of the chosen AI's own saved world that is open to the profile, or one the story draws). The quests follow both pickers: they are read from that engine's saved world without making it active, with the profile's save game. Under the pickers, a note names the engine and its world (its size and world id, and whether it is the active world). A quest chosen before that is not in the new engine's world, or not open to the new profile, is cleared, and the note says why, naming it by its title (remembered from the world that listed it) with its key. 🧠 Train's *Play this result* drops such a quest too, saying so and the **character** (story default, random, balanced, or *my own points*, set with Randomize/Balance), then **▶ New game**, or **📂 Continue a save game** (saved here or by the ANE AI; exactly, or rebuilt from its parameters). *Another engine*: a recorded engine, or an engine file (an engine model, an older engine ZIP, or a configuration with its model parameters; it is kept, to play again or export). *The world's quests, and this profile's save game*: the quests with their stages, the sessions saved (and whether each report reached the UNP). *🏷 About this engine* shows the chosen engine's result card. The panel folds away once a game is on; 🧠 Train's *Play this result* opens it with its completed engine chosen | `GET /v1/engines/{id}/card`, `GET /v1/exports/engines`, `POST /v1/engines/{id}/play` (`latest`: the newest), `POST /v1/records/engines/{id}/play`, `POST /v1/engines/load`, `POST /v1/sessions/load`, `GET /v1/missions/playable?profile=&engine_id=`, `POST /v1/stats/allocate`, `GET /v1/progression` |
| **▶ Play** → the game | Sized to fit a window: the status bar (where, health, coins, the quest's step, an event, who you are talking with); the **place** (its description, who is here ★ quest · ⚠ hostile · ♥ ally, what can be seen); below it **What happened**, the last turn at a glance (your choice and with whom, the check rolled, what came of it, what it changed: ❤ health, 🪙 coins, 🎒 items, 📍 places, 👤 people; the quest step it completed); a button per choice with its check. Beside it the **quest card** (a progress bar, each step with the turn it was done, what to do now), **🧍 You** (power, inventory, stats with what items add, remembered values, the alert, the last check) and *People met & the map*. Below: **Your journey** (every choice as a table), **The story so far** (as a story), and **Judge this playthrough**. Save the game or end it. The game survives a page reload in the same browser, while the API still has it. (How a UNP sees this game is in ⚙ Advanced → 🔌 Integration.) *People met & the discovered map* draws the **discovered map** of this game. It shows only the places found and the ways to them: filled where visited, hollow where only seen, 📍 here, ↩ where you came from, 🔒 locked, a dashed arrow for a one-way path. It is not the authoring map. | `POST /v1/sessions/{id}/actions`, `GET /v1/sessions/{id}/transcript`, `GET /v1/sessions/{id}/journal`, `GET /v1/sessions/{id}/savegame`, `POST /v1/sessions/{id}/end`, `POST /v1/sessions/{id}/judge` |
| **⚖ Evaluate** | **What is evaluated**, shared by the judge benchmark and the calibration, is one of two choices: - **Selected trained engine**: a finished run, also one kept without the records, or another recorded engine, listed by label. It plays its saved world with its trained storyteller. - **Baseline: current configuration with random storytellers**: the explicit point of comparison. The note under it names the target and its world (size, world id, whether it is the active world). 🧠 Train's *Evaluate this result* selects its completed engine exactly. After a training or an engine loaded in ▶ Play, the newest is offered and selected; ↻ Refresh lists them again. An engine target with no engine selected is refused; it is never replaced by the baseline. **Before submission** each section shows its plan: the stories and their seeds, of what. In **A/B** the plan reads *A* = the target as it is and *B* = the same target with version B's settings, on the same seeds, each pair judged in both orders. The selected engine is used on both sides, and version B needs at least one setting. **Results** say what was evaluated: the result's own target, with a warning if it ever differed from the one submitted. A/B results also give A's and B's labels, the win rate and its 95% interval. Fit the fitness to the judge and save the suggested `fitness.term_weights`, or edit them. *Recorded stories*: read one, judge it, or compare two (which is the better game). *🏷 About this engine* shows the target's result card. An absolute judge benchmark of an engine (and a calibration on one) is kept on its card as the judge's engagement, apart from its training fitness. **⏱ Jobs** lists the judge benchmarks and calibrations queued, running and recent: progress as *stories judged n/N* (not generations), elapsed time, queue position, errors with what to do. **Open its result** shows a finished one's summary and table, or its suggested weights. As in Train, the browser remembers the evaluation it started and follows it again after a refresh; **🚫 Cancel it** stops one before its next story is judged (or once its stories are played). The judgements made so far are not kept, since part of a benchmark would mislead. | `GET /v1/jobs`, `GET /v1/jobs/{id}`, `GET /v1/evaluation/target`, `GET /v1/engines/{id}/card`, `POST /v1/jobs/judge-benchmark`, `POST /v1/jobs/calibrate-fitness`, `GET/PUT /v1/fitness/term-weights`, `GET /v1/records/stories[/{id}]`, `POST /v1/records/stories/{id}/judge`, `POST /v1/judge/compare` |
| **📦 Export** | Download a trained result to run offline in a game, in five steps. **1 · Choose what to export**: the **finished runs** (and loaded engines), listed by their label (their name, if named), newest first. The latest is chosen, and several can be chosen: one download each. *🏷 The chosen engines' result cards* shows each one's card, and ↻ Refresh lists them again. The Python version can also be made from a generation's champion (of the run 🧠 Train made in this session), the best remembered storyteller, or the game in ▶ Play. **2 · Choose where it runs**: Python, Windows, Linux, macOS (Apple silicon or Intel) or the Unity package. A system without a runtime here is marked *not available here*, and its note gives the API's reason, such as no runtime in `ANE_RUNTIMES_DIR` or a runtime built from other engine code, with what is available instead. **3 · Options**: the learning memory, and the name (with several runs, one download per engine; without a name, the engine's name). **4 · Export** is enabled only for a valid combination. Otherwise it says why and how to resolve it, for example: the ANE AI is made from finished runs only (choose *finished runs*, or *Python*), the system is unavailable here, no run is chosen, no generation or no run this session, no game open in ▶ Play, or no remembered storyteller. **5 · Download and use it**: the downloads, what each contains, and how to use it on that platform: Python commands, Unity import, or the standalone program's start and stop, with the macOS quarantine and Windows notes. A finished training or an engine loaded in ▶ Play is chosen at once. 🧠 Train's *Export this result* chooses exactly its completed engine, keeping the platform; a value set this way never re-lays out the form, so the engine stays chosen. The runtimes available here and the world catalogue are also in ⚙ Advanced → 🔌 Integration. *Recorded engines, runs and worlds*: download a recorded engine's model file and world, see a run's settings and every generation, download a recorded world as a configuration to upload. The export's manifest carries the engine's name (`engine_name`). | `GET /v1/engines/{id}/card`, `GET /v1/exports/platforms`, `GET /v1/exports/engines`, `POST /v1/exports`, `GET /v1/learning`, `GET /v1/artifacts/{id}`, `GET /v1/records/engines/{id}/model`, `GET /v1/records/runs[/{id}]`, `GET /v1/records/worlds[/{id}]` |
| **⚙ Advanced → Simulation** | Upload and activate a combined configuration YAML, or go back to the built-in files; the **training defaults** (default population, generations, stories per storyteller, steps, deterministic runs and seed: what a run uses for whatever 🧠 Train does not set for it; the terminal and API clients use them too); the game: starting and **maximum health**, coins and the **story length**; every hyperparameter as one YAML document; the active configuration (read only) | `POST /v1/config/upload`, `POST /v1/config/reset`, `GET/PUT /v1/settings`, `GET/PUT /v1/hyperparameters`, `GET /v1/config` |
| **⚙ Advanced → Weights** | Two views of one draft. **Basic** (shown first) has 20 common gameplay controls, chosen from what the engine does with them. They are grouped as *Difficulty* (base check difficulty, its increase per turn and the turn it starts, the minimum and maximum success chance, the bonus after a failed try, the guard penalty per alert level), *Pacing* (pull toward unvisited places, toward novelty when the story is dull, away from exhausted places, toward the mission), *Repetition* (repeating a choice, a repeated success's harder check and its cap, going straight back, retrying at once, repeated event effects) and *Event frequency* (event pressure and its threshold, the chance of a scene on a first trip). Each control is one parameter (`backend/tuning_basic.yaml`: no presets, nothing set besides it), explained by what raising or lowering it does in play, with its unit, default and path. **Advanced** is the complete editor below; a field that Basic also shows says *Also in Basic → area*. A value edited in either view is the same value in the other, and switching views changes no value. The draft's status, above both views, lists every unsaved change; those made in Advanced to parameters that Basic doesn't show are listed apart (*⚠ Advanced only, not shown in Basic*). *Save changes* saves only the values that changed. A parameter someone else changed after the page loaded is refused, not overwritten. *Discard changes* reloads the saved values. The Advanced view shows every tuning parameter (difficulty and its increase per turn, chance floor and ceiling, damage mitigation, the storyteller's and the score's weights, ...), by section. Each is shown by a readable label (e.g. *Difficulty increase per turn*), with what the engine does with it, its unit on its own scale (a percentage of the 1–100 roll, a probability per turn, an event pressure, score points per ...), the engine's default (the value it uses when the parameter is left out), the bounds where the engine sets them (`story_rules`, the score's length floor, the two exponents that must not be negative), whole numbers only where the engine counts in whole numbers, and its path (`skill_check_resolution.difficulty_rate_per_step`) under it. A parameter that only acts with a setting on (the 19 of `outcome_resolution` that need `outcome_resolution.hidden_adjustments`; the director's, variation's, the unexpected events', first trips' and the alert's with their `enabled`) is greyed and says *Inactive now* while it is off. Parameters the engine no longer has are not offered (an older config folder may keep them; they have no effect). A parameter without a description (one added later) shows its name, its path and its type as written. Saving checks every value first (a fraction for a whole-number parameter, or a value outside the engine's bounds, saves nothing) and keeps the values you did not change exactly as written; only the numbers that change are rewritten, so the file's comments and layout stay **Changed from the shipped values** lists the saved values that differ from what this release ships, those in Basic and those only in Advanced apart. **↺ Shipped values into the draft** puts each such parameter's shipped value in the draft, to save or discard. The shipped values come from `config/default_history.json`, written by `packaging/default_history.py`; in Docker they come from the image's pristine copy. Each field also says its shipped value. Leaving ⚙ Advanced with unsaved weights asks first, in Weights: **💾 Save**, **Discard** (the saved values come back) or **Stay**. A refused save keeps the draft and asks again with why. The Basic controls the storyteller scales say how far its traits move them. For example, the repetition penalty counts ×0.2 to ×0.9 with the trait `novelty_bias`, from the trained range in `genome_bounds`, and each engine's card shows its own values of those traits. *Event frequency* also has the turns between two events and the most events per story (`unexpected_events.cooldown_steps`, `max_per_story`). | `GET/PUT /v1/weights` |
| **⚙ Advanced → Engine logic** | Every parameter of the engine's logic, section by section (no code editing) | `GET /v1/logic`, `GET/PUT /v1/logic/{section}` |
| **Settings: when they take effect** | The configuration's forms (📜 Story rules, ⚙ Advanced → Simulation, Weights, Engine logic, and Grow the world's tick) say when a save takes effect, and so does each save's confirmation: at once for what starts from now on (new games, training runs, evaluations, and jobs still queued, which read the configuration when they start). Nothing running is restarted: a game in progress or a running job keeps going, and values it reads as it goes may change mid-way. A game with a trained engine uses that engine's own saved world and tuning. A save that sets aside an uploaded configuration (or a trained engine's) in use says so. An uploaded configuration says that it lasts until the next save, *Use built-in config*, a game with a trained engine, or an API restart. | the configuration's save endpoints; `GET /v1/reference` (`activation`) |
| **⚙ Advanced → App & jobs** | The studio's limits, defaults, queue and launch settings; the background jobs started by any client, with their progress. **When the parameters take effect:** the API reads its own when it starts, the studio its own when it starts, so a save changes nothing that is running (games in progress, submitted jobs and the open studio keep their values). Each field says who reads it; a table shows each parameter saved, running in the API, running in this studio, and after which restart it takes effect, with a **⏳ Restart pending** notice where saved and running differ. The studio's launch settings come from its environment first: `ANE_UI_HOST`, `ANE_UI_PORT` and `ANE_UI_SHARE` (an empty one is unset) are used instead of the saved `server_host`, `server_port` and `share`, now and after a restart, and the table and the save confirmation say so (no restart is pending for a value the environment sets). The confirmation says what a restart of the API and of the studio would change. | `GET/PUT /v1/app-settings`, `GET /v1/jobs` |
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
