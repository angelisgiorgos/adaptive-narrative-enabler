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
