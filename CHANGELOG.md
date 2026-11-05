# Changelog

All notable changes to Adaptive Narrative Enabler are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Add new entries
under **Unreleased**, in the section of the area they touch, with the commit
that introduces them.

## [Unreleased]

### Offline engine export

#### Added
- `backend/export.py`: exports the trained engine as a self-contained ZIP
  (engine code, configuration snapshot, evolved storyteller, training tag
  vectors, learning memory, manifest, integration guide). The storyteller is
  a run's best, any generation's champion, the best remembered one, or a
  game's. Available in the studio (**Export the engine**), through
  `POST /v1/exports` and `GET /v1/runs` (with `run_id` on simulations), and
  automatically at the end of `python main.py` (`offline_engine.zip`).
- `narrative_runtime/`: `NarrativeEngine` / `Game` for Python games (views,
  choices by name or index, save/load), a standard-library HTTP/JSON server
  for Unity, Godot and web games, and a command line (`info`, `play`,
  `serve`, `evolve` for offline training that adopts a better storyteller).
- `tests/test_export.py` (8 tests): package contents, standalone play in a
  clean process, identical transcripts, HTTP server, offline training, API,
  studio and terminal.

#### Changed
- World building and fitness moved from `main.py` into
  `core_engine/builder.py` and `core_engine/fitness.py` (re-exported by
  `main.py`), so the engine runs without the terminal tooling.
- `core_engine` loads the LLM augmenter only when it is used, and
  `utils/tag_similarity` imports sentence-transformers and scikit-learn only
  when a tag has to be encoded. It can load pre-computed vectors
  (`load_tag_vectors` / `export_tag_vectors`).

### Evolutionary AI and backend/UI split

#### Added
- `core_engine/evolution.py`: `NarrativeEvolution`, the single evolutionary
  engine behind the terminal, the studio and the API. It has
  multi-story evaluation on shared seeds, a fixed validation benchmark,
  tournament selection, elitism, crossover, mutation, random immigrants,
  adaptive mutation, early stopping, and seeded reproducibility.
- `LearningMemory`: a hall of fame of benchmark champions across runs. It
  warm-starts the next run, so the tool keeps learning.
- Studio: a learning curve, a "what the evolved storyteller learned" table,
  stories per storyteller, continue learning, and forget. API: `learning` on
  simulations, `episodes` and `warm_start`, and `GET|DELETE /v1/learning`.
- `backend/` (`store.py`, `content.py`, `sessions.py`): all validation,
  storage, authoring and play logic, used by the UI and the API.
  `app.py` is UI only.
- An **Engine logic** editor (studio tab 5, `GET /v1/logic`,
  `GET|PUT /v1/logic/{section}`) changes the logic through parameters, with
  strict validation and comments kept.
- Former hard-coded constants are now parameters (same defaults):
  story-phase tags, `scene.visibility`, `discovery_nodes`, `npc_leads`,
  new-path commitment weights, the unexpected-event objective, spawn
  fallbacks, the probability range, and the genome's tag-bias bounds.
- `fitness.npc_interaction_cap` stops evolution from farming conversations
  for points.
- `tests/test_learning_and_logic.py` (12 tests).

#### Removed
- The Python source editor for `core_engine/actions.py` (studio),
  `GET|PUT /v1/actions`, `ANE_AUTHORING_TOKEN`, and the unused random
  `Action.choose_outcome` / `Outcome.success`.

### Character stats and skill checks

#### Added
- `CharacterStats` (`core_engine/stats.py`) with writer-defined stats in
  `config/stats.yaml`: a point budget (50 total, 5-20 per stat,
  configurable), and `defined`, `random` or `balanced` allocation. Item
  `stat_bonus` and outcome `change_stats` modify stats.
- `CombatSystem` (`core_engine/combat.py`): 1-100 skill checks (pass at or
  under the value), action `skill_check` with `on_check: pass | fail`
  outcomes, `tag_checks` for existing content, and damage mitigation and
  failure softening from a chosen stat. The AI scores outcomes by pass chance,
  and `action_selection.skill_fit_weight` makes it prefer its strengths.
- **Skill check resolution** (Weights tab): difficulty offset, difficulty over
  time, pass-chance floor and ceiling, and mitigation settings.
- Studio: a Character stats editor, the player's stats on the play tab
  (random, balanced or own points), item Stat bonus, and stats plus the last
  check in the scene. API: `/v1/stats`, `/v1/stats/allocate`,
  `stats`/`stats_mode` when starting games, and `stat_bonus`, `stats`,
  `stat_points` and `last_check`. Terminal editor fields.
- Shipped content: the stat set, item bonuses, explicit checks (bribing the
  guard, interrogating the thief, running from a bandit), and the blessing's
  persuasion bonus.
- `tests/test_stats_combat.py` (16 tests).

#### Removed
- `combat_resolution` (attack/defence tags and weights) and the combat
  adjustments in `actions.py`. `HealthPlayer.mitigated_combat_change` and
  `failure_penalty` moved into `CombatSystem`. Power now comes from health
  and average skill (`power_model.stat_weight`).

### Studio and REST API coverage

#### Added
- **Actions & outcomes editor** for every location, NPC, item and event,
  in the studio (YAML with a field reference) and via
  `GET|PUT /v1/entities/{kind}/{name}/actions`. It validates fields,
  `repeat` values, references to events, locations, NPCs and items, and
  variable changes before saving.
- **Missions editor** (studio, `GET|PUT /v1/missions`), which checks goal keys,
  references and conditions.
- **Story rules**: a studio form and `GET|PUT /v1/rules` for mission pacing,
  repeat defaults, arrests and confinement, following new paths,
  persistence, and the event fallback.
- **Player profiles**: chosen on the play tab and through the API
  (`profile`), listed with `GET /v1/variables/profiles`. A profile's stored
  values and playthroughs can be edited (studio, `PUT /v1/variables/values`)
  to test later playthroughs.
- Event **title** in the World editor and on the entity API;
  `DELETE /v1/entities/{kind}/{name}`.
- Structured play state on sessions: `location`, `health`, `max_health`,
  `coins`, `power`, `inventory`, `mission_status`, `end_reason`, `profile`,
  `variables`.
- `tests/test_studio_api.py` (13 tests).

#### Fixed
- Deleting the last event in the World editor raised an error.

### Persistent variables

#### Added
- `core_engine/variables.py` and `config/variables.yaml`: variables (`bool`,
  `int`, `float`, `str`, optional `min`/`max`) that persist across
  playthroughs, plus the built-in `playthroughs`.
- Outcomes change them with `set_variables` and `change_variables` (clamped).
  `enabled_if` switches locations, NPCs, items and missions on or off, and
  missions can have `variables` goals.
- `VariableStore`: per-profile JSON storage with atomic writes. Only real
  play (the studio game and the terminal story) saves; simulations and
  evolution never write (`persistence.simulate_with: defaults | stored`).
- Studio: a **Persistent variables** panel (definitions editor, stored
  values, reset), an **Enabled if** field in the World editor, and the
  current values in the scene. API: `/v1/variables`,
  `/v1/variables/values`, `/v1/variables/reset`, and `enabled_if` on
  entities. Terminal editor support. A Docker `narrative-saves` volume.
- Example content: `thief_caught`, `fence_exposed`, `reputation`, the Fence's
  Ledger item, the Clear your name mission, and the Memory Thief from the
  second playthrough.
- `tests/test_variables.py` (16 tests).

### Repository

- Ignore `.gradio/`, Gradio's local cache, which holds a TLS certificate that
  must not be committed.

### Narrative engine (`core_engine/`, `main.py`, terminal editor)

#### Added
- **Events** (`world.Event`): self-contained situations entered through an
  action's `triggers_event`. While an event is active only its actions are
  offered, and no NPCs, items or unexpected events spawn. Any choice ends the
  event and returns the player to where it began, unless an outcome moves
  them. Supports chained events, a fallback "step away" option, and
  `remove_npc` outcomes.
- **Hostile encounters**: an NPC with `encounter_event` starts its event by
  itself as soon as it is in the scene, once per game, or on every visit with
  `encounter_repeat`.
- **Missions and a win condition** (`world.Mission`): the goal conditions are
  `action`, `location`, `item`, `event`, `npc_removed`, `outcome_tag` and
  `coins`. One mission is drawn per story, and `GameState.won`, `end_kind` and
  `mission_status` report the result. A story ends when its goal is met
  (`mission_settings.end_on_goal`).
- **Goal route planning**: a backward shortest-path search from the goal
  sites over known routes and discoverable `spawn_rules`. It steers action
  ranking, outcome selection, spawn selection and destinations, leaves goal
  sites whose options are used up, and treats confining locations as dead
  ends.
- **Story-arc pacing** of goal pursuit (`pursuit_start_step` →
  `pursuit_full_step`), and a win bonus that pays in full only after
  `mission_win_min_steps`.
- **Unique vs generic NPCs** (`Character.unique`): a unique NPC lives in one
  best-fitting home for the whole game; generic NPCs appear wherever they fit.
- **`HealthPlayer`** (`core_engine/health.py`): health bounded by
  `game_state.max_health` and power bounded by `power_model.max_power`, with
  heal/damage/set, combat mitigation, failure penalties, power calculation
  and statistics. `GameState.health`/`power` read through it.
- **Narration** (`core_engine/narration.py`): all 52 story messages as
  templates with placeholders and variants that rotate (never random), plus
  validation.
- **Repeatable and one-time options**: `Action.repeat` (`once`, `per_visit`,
  `always`, with defaults per source in `action_rules.default_repeat`) and
  `exclusive_group` (once one option succeeds, the others disappear), scoped
  per owner and per location for generic NPCs.
- **One-way moves**: an `Outcome.send_to` outcome and `Location.connect_one_way`.
  Arrests send the player to `game_state.arrest_location`, and
  `location_constraints.<loc>.lock_when: sent` confines only players who were
  sent there.
- **Genome parameters** `goal_bias` and `novelty_bias`. Novelty lowers repeated
  choices and outcomes, and the new `fitness.variety` term rewards distinct
  choices and penalises repeated outcomes.
- Terminal editor: Unique NPC, Encounter event, Repeat rule, Exclusive group,
  and the `send_to` outcome effect.

#### Changed
- Following a newly found path is deterministic and genome-driven
  (`auto_travel_on_spawn_threshold`); the 40% dice roll is gone.
- Endings carry `end_kind`, and fitness scores that instead of parsing the
  end-reason text.
- Actions carry `source` and `owner`; travel and repeatability no longer depend
  on names such as "Move to…" or "Approach…".
- A removed generic NPC leaves only the current visit; removing a unique NPC
  is permanent.
- Power's health ratio is measured against maximum health.
- World building uses shared outcome and action builders.

#### Fixed
- Coins could go negative, which hid every action except travel.
- Arrival text ("You are in the…", "Met a…", "Nearby you see…") was never
  written to transcripts.
- Leads and `reveal_location` could create a route from a location to itself.
- `reveal_npc` marked an NPC visible without adding it to the scene.
- A removed NPC could still give leads.
- One escape unlocked the Dungeon for the rest of the game.
- Stalling in the Dungeon was penalised twice.

### World content (`config/`)

#### Added
- `events.yaml`: `confront_thief` (fight, interrogate or steal from the
  Market thief), `bandit_ambush` and `memory_theft`.
- `missions.yaml`: five missions (expose the fence, break into the Vault,
  find a shield, reach the Palace with 20 coins, rid the market of the
  thief).
- `messages.yaml`: every story message, ready to rewrite.
- A Thief NPC at the Market, an explicit `unique` flag on every NPC, and
  encounter events for the Bandit (every visit) and the Memory Thief (first
  meeting).
- The Gate Guard's wine, coins and fight options form one exclusive choice;
  "Study the map" is `repeat: once`.
- `hyperparameters.yaml`: `mission_settings`, `authored_events`,
  `action_rules`, `max_health`, `arrest_location`, `lock_when`, the goal and
  novelty genome bounds, repetition penalties, and the `variety` and
  mission-win fitness terms.

#### Changed
- NPC tags now match each unique NPC's thematic home. Stray tags such as
  `guarded`, `luxury` and `maritime` pulled NPCs into the Palace or the
  Harbor.
- The Bandit's and Memory Thief's confrontation actions moved into their
  encounter events.
- `auto_travel_on_spawn_chance` is replaced by `auto_travel_on_spawn` and its
  threshold.

### Studio, REST API and Docker

#### Added
- Gradio studio (`app.py`) and REST API (`api.py`), with configuration
  overrides in `utils/config_loader.py`, `ui_authoring.py`, `requirements.txt`,
  a `Dockerfile` and `compose.yaml`.
- World editor: Events as a content type; Unique NPC, Encounter event and
  Repeat-on-every-visit for NPCs.
- Simulation settings: Maximum health, and a **Story messages** YAML editor
  that validates and keeps comments.
- API: `unique`, `encounter_event` and `encounter_repeat` on NPC entities;
  `event`, `mission` and `won` on play sessions; `max_health` on settings;
  `GET|PUT /v1/messages`.
- `docker-entrypoint.sh` adds default config files missing from an existing
  config volume on every start, without overwriting edits.

#### Changed
- Uploaded configurations and saved bundles from before these features still
  load. New keys are optional, and missing genome parameters (`goal_bias`,
  `novelty_bias`) take the midpoint of their bounds.
- Scene view shows the active event, the mission, `health/max`, and "Mission
  complete".

### Tests (`tests/`)

- `test_events`, `test_missions` (including route planning and pacing),
  `test_npcs`, `test_encounters`, `test_health`, `test_choices_and_messages`,
  `test_one_way`, together with the existing studio and API suites: 88 tests.
  Run with `python -m unittest tests/test_<name>.py`.

### Documentation

- README sections for every feature above, with configuration examples and
  measured effects; `docs/API.md` covers the new fields and endpoints. This
  changelog was added.
