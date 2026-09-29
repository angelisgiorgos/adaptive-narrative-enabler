# Changelog

All notable changes to Adaptive Narrative Enabler are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Add new entries
under **Unreleased**, in the section of the area they touch, with the commit
that introduces them.

## [Unreleased]

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
