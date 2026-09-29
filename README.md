# Adaptive Narrative Enabler

Adaptive Narrative Enabler is a Python prototype for generating interactive narrative runs from modular YAML content. It builds a world from locations, NPCs, items, actions, and outcomes, then evolves genome-like control parameters that influence movement, discovery, threat, recovery, setup/payoff, and success behavior.

The main entry point is [`main.py`](main.py). Content and tuning live in [`config/`](config/). Generated stories, best worlds, and per-run archives are written to [`runs/`](runs/).

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
- Unique NPCs live in a single location for the whole game; generic NPCs (e.g. guards) appear wherever they fit.
- Hostile NPCs (e.g. bandits) confront the player with an encounter event the moment they are in the scene.
- One-time, per-visit, and always-available options, plus exclusive choices (bribe *or* fight the guard).
- Every sentence the engine writes is customisable, with variants that rotate instead of repeating.
- Play decisions are deterministic and genome-driven; evolution also learns to avoid repetitive stories.

## Repository Layout

```text
adaptive-narrative-enabler/
|-- main.py                         # Entry point, world building, evolution, simulation, archives
|-- config/
|   |-- hyperparameters.yaml        # Evolution, fitness, LLM, editor, augmentation settings
|   |-- events.yaml                 # Event definitions (self-contained situations)
|   |-- messages.yaml               # Every sentence the engine writes, customisable
|   |-- missions.yaml               # Mission goals / win conditions
|   |-- locations.yaml              # Location definitions and location actions
|   |-- items.yaml                  # Object/item definitions and item actions
|   `-- npcs.yaml                   # Character definitions and NPC actions
|-- core_engine/
|   |-- actions.py                  # Action/outcome resolution
|   |-- augmenter.py                # Optional LLM world augmentation
|   |-- game_state.py               # Runtime story state and simulation loop
|   |-- health.py                   # HealthPlayer: bounded health and power of the player
|   |-- narration.py                # Default story messages and the message formatter
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

### Docker, UI, and REST API

Build and start the containerized studio:

```bash
docker compose up --build
```

The complete UI is available at `http://localhost:8003`, the REST API at
`http://localhost:8004/v1`, and interactive API documentation at
`http://localhost:8004/docs`. See [`docs/API.md`](docs/API.md) for endpoint
coverage and request examples.

The Gradio studio edits locations, NPCs (including the **Unique NPC** option), items, events, and tags through forms, saves
simulation settings to the YAML configuration, runs a small candidate
tournament, and turns its best genome into a player-controlled game.

The **5 · Actions & app parameters** tab provides a Python editor for
`core_engine/actions.py` and forms for the app's session/upload/work limits,
simulation defaults and bounds, crossover probability, queue settings, and
host/port/share options. Saving action logic checks syntax and the required
`Action`/`Outcome` classes, rejects stale edits, and keeps the previous source
in `core_engine/actions.py.bak`. Use **Reload actions.py from disk** to discard
unsaved edits and fetch the latest source. Syntax validation does not guarantee
that edited code will run correctly. The editor loads the latest source when
opening the UI. The REST API provides matching `GET` and `PUT /v1/actions`
endpoints; set `ANE_AUTHORING_TOKEN` and send it as a bearer token to enable
access. See [action API examples](docs/API.md#edit-action-logic).

App parameters persist in `config/ui_settings.yml`, separate from story
hyperparameters. Restart the UI after saving parameters, and restart engine
processes after saving action logic. Existing games keep their loaded code until
restart. `ANE_UI_HOST`, `ANE_UI_PORT`, and `ANE_UI_SHARE` override saved launch
options. Restrict access to trusted authors: the source editor can change Python
code that executes on the server.

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

With Docker, app parameters persist in the existing config volume. Source edits
are local to the container that saved them and are lost when that container is recreated;
copy edited `actions.py` back to the project and rebuild to retain changes and
apply them to both services.

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

## Evolution And Speed

The `evolution` section in [`config/hyperparameters.yaml`](config/hyperparameters.yaml) controls search size and parallelism:

```yaml
evolution:
  pop_size: 400
  generations: 200
  episodes_per_genome: 6
  max_steps: 60
  evaluation_threads: 8
  parallel_backend: processes
```

`parallel_backend: processes` uses multiple CPU cores for score generation. Tag similarity and similarity-based spawn rules are cached to reduce repeated scoring work.

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

- [`config/locations.yaml`](config/locations.yaml): places, tags, descriptions, actions, outcomes, movement, reveals.
- [`config/items.yaml`](config/items.yaml): objects, tags, descriptions, collectibility, item actions.
- [`config/npcs.yaml`](config/npcs.yaml): characters, tags, descriptions, known locations/objects, NPC actions, and the `unique` flag. See [Unique and generic NPCs](#unique-and-generic-npcs).
- [`config/events.yaml`](config/events.yaml): events, their exclusive actions, and outcomes. See [Events](#events).
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
- During automated evolution, a trigger action is ranked by the content of its
  event, plus `genome.event_prob × action_selection.event_trigger_weight`.

Configurations uploaded before Events existed remain valid without an
`events` section. Existing Docker config volumes receive `events.yaml`
automatically (see [Docker config volumes](#docker-config-volumes)).

### Missions and winning

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
| `event` | The event was entered and resolved. |
| `npc_removed` | The NPC was removed by an outcome's `remove_npc`. |
| `outcome_tag` | An outcome with this tag succeeded (e.g. `escape`). |
| `coins` | The player holds at least this many coins. |

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
  pursuit_start_step: 12   # goal steering is off before this step...
  pursuit_full_step: 30    # ...and at full strength from this step
action_selection:
  mission_goal_weight: 6.0 # bonus per goal condition an action or outcome advances (× goal_bias × pacing)
fitness:
  base:
    mission_win_bonus: 60      # fitness reward for winning...
    mission_win_min_steps: 20  # ...paid in full only for stories at least this long
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
starting location) are never chosen. A mission with an empty goal or an
unknown goal key raises a `ValueError` when the world is built. A combined
configuration upload rejects it with a clear message. Configurations uploaded
before missions existed stay valid and simply have no mission.

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
  (`health / max_health`) and the inventory's attack and defense bonuses. It
  is clamped to `power_model.min_power` .. `power_model.max_power` (default
  `0` .. `5`).

| Member | Purpose |
| --- | --- |
| `heal(n)` / `damage(n)` / `change(delta)` / `set(value)` / `restore_full()` | Change health within bounds; each returns the change that really applied. |
| `effective_change(delta)` | Preview what a change would do, without applying it. |
| `is_alive`, `is_dead`, `is_low` (≤ `low_health_threshold`), `is_full`, `missing`, `ratio` | State checks used by the engine. |
| `mitigated_combat_change(delta, attack, defense)` | Combat damage reduced by weapons and armour (`combat_resolution.*_damage_mitigation`); never turns damage into healing. |
| `failure_penalty(defense, dangerous)` | Health lost on a failed action; armour softens failures in fights or against guards. |
| `calculate_power(attack, defense)` | Recompute and clamp power. |
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
- Set them in YAML, or in the terminal editor: edit an action → **6. Repeat
  rule** and **7. Exclusive group**. An invalid `repeat` value stops the world
  build with a clear error.

The old name-based rules are gone. Actions starting with "Approach" used to
be always available, and "Move to" was how travel was recognised. Actions
now carry their `source` (location, npc, item, event, navigation,
conversation), so renaming anything, including travel options, is safe.

### Story messages

Every sentence the engine adds to a story lives in
[`config/messages.yaml`](config/messages.yaml), 51 messages in all: travel,
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
  `mission`, `action_limit`, `stalled`, `dungeon`) that fitness uses instead of
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
`Action.choose_outcome` and `Outcome.success` in `actions.py` still contain
random code but are not used; outcomes are chosen by the deterministic
objective.

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

- Record every change in [`CHANGELOG.md`](CHANGELOG.md) under **Unreleased**, in the section of the area it touches, in the same commit.

- Keep version control active when using augmentation, especially with `augmentation.save_to_source: true`.
- For reproducible runs, set `reproducibility.seed` in `config/hyperparameters.yaml`.
- For faster debugging, temporarily lower `pop_size`, `generations`, and `episodes_per_genome`, or press `q` during generation to continue with the current best result.
