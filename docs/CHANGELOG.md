# Changelog

All notable changes to Adaptive Narrative Enabler are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Add new entries
under **Unreleased**, in the section of the area they touch, with the commit
that introduces them.

## [Unreleased]

### The API on port 8005

#### Changed
- The API listens on port 8005 (was 8004), inside the container and on the host, as the studio does on
  8003: the image (`ANE_PORT`, `EXPOSE`, health check, command), Compose (`api` command, port mapping
  `${ANE_API_PORT:-8005}:8005`, health check, the studio's `ANE_API_URL=http://hamlet-ane-api:8005`),
  the Docker smoke test, CI and the docs. Clients and a studio with `ANE_API_URL`/`ANE_STUDIO_API_URL`
  set to port 8004 must change it to 8005.

### The public deployment, and a studio that only talks to the API

#### Added
- The API's public deployment, <http://ane.hamlet.iti.gr> (documentation at `/docs`), in the README,
  API.md, FRONTEND.md, STUDIO.md, UNP.md and DEVELOPMENT.md. `ANE_PUBLIC_URL` (Compose:
  `http://ane.hamlet.iti.gr`) adds it to the OpenAPI `servers` beside this server (`/`), and to the
  API's description; empty, only this server is listed.
- `studio_client.py`: the studio's HTTP client (`StudioAPI`, `APIError`, `JobCancelledError`, moved
  from `app.py`, which re-exports them), with `api_url()`, `DEFAULT_API_URL` and `PUBLIC_API_URL`.

#### Changed
- The studio always talks HTTP to an API and never serves the API itself: to `ANE_API_URL`, or to
  `http://127.0.0.1:8005` when it is unset or empty (it used to serve the API inside its own process
  then). Start the API before the studio (`python -m uvicorn api:app --port 8005`).
- Compose: the `ui` service no longer mounts the API's volumes nor sets `ANE_RECORDS_DIR`;
  `ANE_STUDIO_API_URL` empty now means the `api` service (it used to mean the API inside the studio).
- Tests give the studio the API served in the test process (`tests/records_sandbox.py`,
  `studio_client.use_test_client`); the studio's code holds no in-process mode.

### Side-story orchestration and fitness version 2

#### Added
- `core_engine/side_story.py` and the policy `side_story` (hyperparameters.yaml): the director weighs
  the main quest (stage, turns since it moved, climax, finale), open threads, the player's earlier
  answers to offers, danger and recent attacks before letting new side content in. It holds it in an
  event, a waiting climax, the finale, while a side quest's clock runs out, when the main quest has
  stood still (`main_idle_turns`), with too many threads open, too much side content lately, right
  after the last introduction, and (after offers turned down in a row) for anything that does not
  bear on the quest. One candidate policy scores side quests and attack templates by fit plus
  relevance to the main story (`relevance_weight`) with the storyteller's appetite (its existing
  `side_quest_bias` and `threat_prob` traits; no new trait). The storyteller's pull toward side
  quests eases while the main quest stands still and in the climax and finale.
- Agency: what the story sets up (an encounter's `side_quest`) is always an offer (`agency`).
- Connections: side quest `texts.reveal` (told when completed, `[SIDE STORY]`) and `texts.callback`,
  encounter results' `callback`; up to `max_callbacks` callbacks near the climax (`[CALLBACK]`),
  also for completed side quests that helped the mission (`callbacks: all`). The Frozen Monastery's
  thieves and their hideout use them.
- What a story keeps: `main_progress_turns`, `side_story_log` (introductions, with the hold the policy
  gave even when off), `side_story_links` (what each optional thread led to).
- Fitness version 2 (`fitness.version`, `core_engine/fitness.VERSIONS`): the terms `side_relevance`,
  `side_payoff`, `side_completion`, `side_filler`, `side_unresolved`, `encounter_repetition`,
  `side_interruptions`, `main_drift` and `decision_diversity` (`fitness.side_story`; shares and means,
  never counts), and side quests with diminishing returns (`fitness.side_quests.diminishing`,
  `repeat_factor`: bounded, no contract farming). docs/AI.md "Side-story objective terms" gives each
  term and why its default weight.
- The fitness version on the evolution summary (`fitness_version`), the engine card
  (`fitness.version`), judge benchmarks and baseline comparisons.
- The judge tells meaningful optional content from filler: `optional_content` (score, meaningful,
  filler, filler share) when a playthrough has side quests or attacks; benchmarks report it and the
  agreement of the judge's filler share with the engine's (`filler_engine_vs_judge`).
- Decision trace `side_story` (the plan, candidates, codes) and `encounters.codes`; the studio's
  "Explain the current choices" shows a side-content line.
- `story_rules.failure_counts` (true): an unchecked action that tells a failure outcome (surrendering)
  counts as failed in every event; `false` restores the old behaviour.
- `encounters.captive_release` (5): a player an encounter sent to a cell is let out after this many
  turns there, also with the director's `release_after` off.
- UNP parameters (`ane.session_state/1`) carry `encounters` (attacks, standing changes, rewards paid).
- `ANE_LLM_WATCHER=off`: no idle-model watcher in this process (the API starts and stops it; the
  studio, an HTTP client of the API, runs none).

#### Changed
- Built-in configuration: `side_story.enabled: true`, `fitness.version: 2`. A configuration without
  them is unchanged (policy off, version 1). Main-only worlds tell the same stories; with version 1
  and the policy off the shipped world gives the same scores seed for seed. With the policy on, the
  shipped world's 30 seeded stories kept their endings and mission stages; their text and some
  side-quest results differ. No controlled evaluation of narrative quality has been run.
- Fitness scores of version 2 are not comparable with version 1 scores.
- Recorded stories of a play session are only listed and read for that session's user (and the
  studio); `404` for someone else's.

#### Fixed
- The exported runtime's `GET /world/side_quests/<name>` answered `404` (its route did not match an
  underscore).
- `GET /v1/conditions/reference` gave the side-quest facts no names.
- `side_story`: a first side quest offered on turn 1 was counted as all of the recent turns.
- Docs: API.md now covers the event policy's `encounter` block (a `PUT` without it removes it), the
  side-quest fields, statuses and moments, the journal's `encounter`, the saved-games envelopes and
  what persistence off really refuses, the judge status `sleeping` / `sharing`, deletions kept across
  restarts, the condition reference's side quests, retrain fields, `limit` / `offset`, and the
  status codes `403`, `409` and `503`.

### Dynamic hostile encounters: the encounter director

#### Added
- An encounter director (`core_engine/encounters.py`) starts authored encounters, rarely, when the
  story makes them plausible. Templates are ordinary events with an `encounter` block: category
  (ambush, hostile_patrol, faction_attack, npc_assault, location_raid, environmental_threat,
  defensive_encounter), where (`tags_any`, `tags_none`), when (`min_turn`, `stages`, tension, alert,
  Power, faction hostility, the event's own conditions on missions, side quests and past actions),
  how often (`max_per_story`, `cooldown`), `difficulty`, and what follows (`on_win`, `on_loss`,
  `on_escape`: text, effects, faction standing, a side quest set up, a later consequence;
  `rewards_once`). The player's choices are the event's actions (fight, defend, evade, escape,
  negotiate, surrender, seek help) with their conditions and the existing checks, item bonuses and
  health. The studio's and API's event policy editor edits the `encounter` block.
- Policy `encounters` in hyperparameters.yaml:
  - rates and context weights (`rate`, `max_chance`, `danger_tags`, `tension_weight`,
    `alert_weight`, `hostility_weight`);
  - safe places (`safe_tags`) and pacing (`opening_turns`, `min_separation`,
    `after_loss_separation`, `max_per_session`, `template_cooldown`, `no_repeat_last`);
  - `min_health`, `difficulty`, and `lethal: false` (a lost encounter leaves 1 health);
  - interruptions (`in_finale`, `in_climax`, `in_conversation`);
  - `categories` and `factions`.
- Never during an event, a pending choice (side quest offer or hand-over, bargain), a cell, a
  conversation, a waiting climax or the finale (unless allowed), and always after a hostile NPC's
  mandatory encounter. Deterministic: its roll uses a stream derived from the story's dice and the
  moment, not the dice.
- Factions: `encounters.factions` (standing, `hostile_below`, a persistent variable), changed by
  encounters' outcomes; condition `faction_hostile: <name>`.
- `side_quests.hook`: an encounter's outcome sets up a side quest (offered or started).
- Journal `encounter`, save games `dynamic_encounters` / `faction_shift` / `encounter_rewards`,
  decision trace `encounters`. Messages `encounter_<category>`, `encounter_spared`. Validation: a
  category, at least two choices and one without a check, known NPCs and side quests.
- Examples: thieves on the Frozen Monastery's glacier route (a faction, every kind of choice, a side
  quest hook); a security-drone sweep on the Orbital Station called in by the alert.

#### Changed
- An encounter's outcome told as a failure (giving in, being caught) counts as a loss for the
  encounter (on_loss), even without a check.

### Side quests that live in the story

#### Added
- `reactions` (per side quest): what the story does to it. When a condition comes to hold while it
  is active, offered or either, it happens once: its text, its effects, then `complete`, `fail`,
  `advance`, `withdraw` or `none`.
- `tags` and fit: when several side quests could come, the one that fits the story now comes first
  (the place's tags, what the last turns brought, the mission's thread).
- The story lets new side quests in at fitting moments only (`side_quest_settings`): `offer_after`
  (1), `offer_cooldown` (1: one at a time), never during an event, not above `offer_max_tension`
  (0.75), not in the finale (`finale_offers: false`).
- Continuity:
  - a side quest that can no longer be done ends with its reason (its giver gone, a unique item gone
    for good: `side_quest_item_gone`; `impossible: fail | keep`), and an offer whose giver is gone is
    withdrawn;
  - a deadline waits while the player is held (`pause_when_held`);
  - the ending recalls the side quests (`side_quest_epilogue`; `epilogue`);
  - a quest without a description is announced by what it needs;
  - conditions `side_quest_failed` and `side_quest_declined`.
- Training: `fitness.side_quests.unfinished_penalty` (2) per side quest left under way when the story
  ends: storytellers that close the threads they open score higher.
- Shipped: tags for every side quest. Reactions to the world's events:
  - the Memory Thief striking makes *Rumours from the Archive* urgent (+2 coins);
  - the fence exposed completes it;
  - a brawl won adds to *A full purse*;
  - a lost ambush is told against it.

#### Changed
- At most one new side quest per turn by default (`offer_cooldown: 1`); set it to 0 for several at
  once. In 40 simulated stories per setting: side quests completed 15 → 17, the fitness term 4.85 →
  5.25, missions won 14 → 15 of 40.

### The language models free the GPU while no evaluation runs

#### Fixed
- A vLLM server that loaded awake (the judge, started after the augmenter was put to sleep) held its
  GPU memory (18.5 GB of a 24 GB GPU at `ANE_JUDGE_GPU_MEMORY=0.72`) until the API first used it.
  The API now runs a background watcher (`llm_sharing.watch_seconds`, 30; `Turns.start_watcher`)
  that puts every idle, awake model to sleep (vLLM sleep level 1: weights in CPU memory, GPU memory
  freed); the next judge or augmentation request wakes it, as before.

### Side quests: their own storyteller trait, and richer, better-told quests

#### Added
- Genome trait `side_quest_bias` (bounds `genome_bounds.side_quest_bias`, `[0.2, 1.0]`): how hard
  a storyteller pursues side quests, learned apart from the mission's `goal_bias`; the ranking's
  side-quest pull is `side_quest_settings.rank_weight` x `side_quest_bias`. It is drawn, crossed
  and mutated with a random stream of its own, so every other trait, draw and seeded result is
  what it was. Storytellers and save games from before it load with its midpoint (0.6), and their
  snapshots still continue exactly (`Genome.same`).
- Side quest cases:
  - `turn_in` (done, it is handed back to the giver: "Hand over: <title>", id
    `side-quest:<quest>:turn-in`, or dealing with them; it fails if the giver leaves);
  - `within_turns` (a deadline, told and warned of, failing when time is up);
  - `follows` (a chain: offered once another side quest is completed);
  - `on_decline` (the cost of turning it down or letting the offer go);
  - `texts` (the story's own words for its moments: offer, accept, decline, ready, turn_in,
    complete, fail, remind).
- Better told: an offer says what is in it for the player (rewards in words), whether it helps the
  mission, and how long it may take; the way to a place a side quest needs is pointed out when no
  known way leads there (and back to a giver waiting for a hand-over); a quiet side quest is
  recalled after `side_quest_settings.remind_after` (6) turns; failures say why (time, the giver
  gone).
- The storyteller's pull counts a step on the way to a place a side quest names, pulls hardest
  toward a waiting giver, and grows as a deadline nears. Side-quest moments are something new for
  the director's rhythm and a fitness development of their own (`side_quest`).
- Shipped: *A gift for the chapel* is handed back to the Priest, in its own words; new *Rumours
  from the Archive* (the Merchant: a deadline and a hand-over) and *A blessing for the road*
  (follows the chapel's gift, costs reputation if turned down).
- Play state: `reason`, `ready`, `rewards`, `deadline`, `turns_left`, `turn_in`, `aids_mission`,
  `follows`; journal events `ready`, `hurry`, `reminded`; UNP parameters `ready`, `turns_left`.
  Messages `side_quest_stakes`, `side_quest_helps`, `side_quest_deadline`, `side_quest_ready`,
  `side_quest_turned_in`, `side_quest_expired`, `side_quest_giver_gone`, `side_quest_hurry`,
  `side_quest_reminder`, `side_quest_remind_ready`, `side_quest_way`.

#### Changed
- In 40 simulated stories per setting with the shipped world: side quests completed 6 → 15,
  failed 13 → 7, their fitness term 0.8 → 4.85 on average.
- The Merchant offers *Rumours from the Archive* in most stories, so the first choice is often a
  side quest's answer (no turn passes). A client that always takes the first choice to make a turn
  should take the first with `takes_turn: true` (the smoke test, the C# protocol test and the
  API's tests now do).

### Side quests: taken on by choice, helping the mission, part of the training objective

#### Added
- A side quest that can start is offered: "Take on: <title>" and "Turn down: <title>" come first
  among the player's choices (ids `side-quest:<quest>:accept` / `:decline`, category `side_quest`,
  no turn). An offer unanswered for `side_quest_settings.offer_turns` (3) turns lapses (turns an
  event hides it do not count). `accept: auto` (per quest, or `side_quest_settings.accept`) starts
  it at once, as before.
- Completing a side quest helps the mission (`aids_mission`, default
  `side_quest_settings.aids_mission: lead`): `lead` opens the way toward where the mission
  advances next; `ally` makes its giver an ally; `none`. *A gift for the chapel* makes the Priest
  an ally.
- Training objective: the fitness term `side_quests` (`fitness.side_quests`: `complete_bonus` 10,
  `step_bonus` 3, `mission_aid_bonus` 6, `fail_penalty` 4; on the Weights page, and calibrated
  like any term with `fitness.term_weights.side_quests`). The storyteller's ranking pulls toward a
  side quest's next step, or someone who offers one, by `side_quest_settings.rank_weight` (2.0) x
  its learned `goal_bias`. The quester player prefers to take side quests on.
- UNP: `POST /world/patch` adds, changes and removes side quests (kind `side_quests`), checked like
  the studio checks them; `GET /world/side_quests/<name>`.
- Statuses `offered` and `declined`; journal events `offered`, `declined`, `lapsed`; report
  `side_quests.declined` and `aided_mission`; play state `offered`, `aided_mission`. Messages
  `side_quest_accepted`, `side_quest_declined`, `side_quest_aid`, `side_quest_ally`. Decision trace
  code `side_quest_offer`.

#### Changed
- Side quests are offered as a choice by default; set `side_quest_settings.accept: auto` for the
  earlier behaviour.

### Side quests

#### Added
- Side quests: optional quests besides the story's mission (`world_definition.side_quests` in
  `config/side_quests.yaml`; `core_engine/side_quests.py`, `SideQuest` in `core_engine/world.py`).
  - Written like a mission (title, description, goal or stages, rewards, enabled_if), plus `giver`
    (the NPC who offers it: it starts once the player has dealt with them), `start` (a condition),
    `fail_if` and `on_fail` (failure and its cost), and `repeatable`.
  - Several are under way at once (`side_quest_settings.max_active`, 3); none ends the story. Each
    is offered once per story, and once per profile unless repeatable.
  - Checked at the end of every turn and after the UNP sets the player or reports events; nothing
    is drawn from the dice.
  - Three ship with the world: *A gift for the chapel* (the Priest), *Charting the storm* (the
    Storm Cartographer, in two steps) and *A full purse* (repeatable).
- The player's choices keep one that works toward a side quest under way, or meets someone who
  offers one (`side_quest_settings.guide`; `priority_side_quest` in the decision trace).
- Conditions: `talked_to: <NPC>`, `side_quest_active: <key>`, `side_quest_done: <key>`.
- PlayState, the exported ANE AI's view and the journal show them (`side_quests`); `people[]`
  lists the side quests each person offers. The session report has `side_quests: {completed,
  failed, unfinished}`, and the profile keeps the side quests it completed
  (`progression.side_quests_completed`). The UNP's session parameters, save games and snapshots
  carry them; a rebuilt session stands where they stood, quietly. `GET /world` (and
  `/v1/world/catalogue`) lists them.
- Validation: the mission rules plus giver (an NPC who is not hostile), `start`, `fail_if`,
  `on_fail` and `repeatable` (`content_rules.side_quest_errors`); a world, upload or rename that
  breaks a side quest is refused like one that breaks a mission.
- Studio: 🌍 World → 🎯 Missions → *🧭 Side quests* edits them as YAML (the document
  `side_quests`); ▶ Play shows them under the quest, marks people who offer one (❗), and tells
  what happened to them in the turn card.
- Messages `side_quest_start`, `side_quest_given`, `side_quest_stage`, `side_quest_complete`,
  `side_quest_failed`.

#### Changed
- `packaging/default_history.py` keeps the values recorded before when it rewrites
  `config/default_history.json`, so a copy without the repository's history adds to it.

### Saved games: every play session is saved, restored after a restart, and kept per user

#### Added
- Every play session is saved after each change that matters (a turn, approaching or leaving
  someone, the player or the world set through `PATCH .../player` and `POST .../sync`, the end) as
  an ordinary `ane.savegame/1` document, with a durable session index (owner, profile, origin,
  engine, world version, status, place, turn, state version, mission, start parameters, last
  change) in `ANE_SESSIONS_DIR` (default `ANE_SAVE_DIR/sessions`; `backend/persistence.py`). The
  configuration each session runs with is kept once per engine beside it.
- A session that is saved but not in memory (after a restart of the API, or one let go beyond
  `max_web_sessions`) is restored when any of its routes is used: exactly (the same dice, quest,
  NPC goals and relations, discovered places, event, encounter or conversation, and story), or
  rebuilt from its parameters when the snapshot no longer fits the engine code.
- `GET /v1/sessions`, `POST /v1/sessions/{id}/continue`, `POST /v1/sessions/{id}/restart`,
  `POST /v1/sessions/{id}/saves` (named and manual save slots), `GET /v1/saves`,
  `POST /v1/saves/{id}/restore`, `DELETE /v1/saves/{id}`, `POST /v1/sessions/cleanup`.
- Retention: finished sessions older than `ANE_SESSION_RETENTION_DAYS` (30), or beyond the
  `ANE_SESSION_KEEP_FINISHED` (20) most recent of each user, are removed (at most hourly when a
  session ends, and on `POST /v1/sessions/cleanup`); active sessions and save slots are kept.
  `ANE_SESSION_PERSISTENCE=off` keeps sessions in memory only.
- Studio ▶ Play: *📂 Saved games* lists the games in progress and save slots of the chosen
  profile, to continue, restart or delete; *💾 Save* saves into a named slot (or a new manual
  save). The save game file is still available (*⬇ Save game file*, *📂 Open a save game file*).

#### Changed
- A play session belongs to the user who started it (`X-ANE-User`; the studio's are `test`): any
  other user gets `404` on its routes, and their saves are not listed or restored. Sessions started
  without `X-ANE-User` stay anonymous (anyone with the id), as before. An invalid `X-ANE-User` on a
  session's route answers `400`.
- Saves are written atomically (a temporary file renamed over the old one) and serialised per
  session and across processes (the index's write transaction); an older state never replaces a
  newer one. A failed save is logged and noted (`error`), never interrupts the game.
- Background jobs carry the identity of who submitted them, so the game a training job starts is
  theirs.

### Stable action identities and author decision diagnostics

#### Added
- Every choice has a stable `id` from what offers it, not its text or position
  (`npc:<person>:<action>`, `location:<place>:<action>`, `go:<place>`, `approach:<person>`, ...;
  `core_engine/choices.py`). Authored actions use their `id` field (already accepted, now used; one
  entry's ids must differ), else their authored name; the builder records each action's
  `content_ref` (where it is written).
- PlayState `choices[]` are structured: `id`, `label`, `category`, `owner`, `source`, `location`,
  `requirements`, `skill_check`, `failure_risk`, `available`, `availability`, `repeat`, `one_time`,
  `takes_turn`, `members` (with `name` and `check` as before); `state_version` changes whenever the game
  moves.
- `POST /v1/sessions/{id}/actions` takes `action_id` with an optional `state_version`: a choice made
  from older options is refused (`409 stale_state`), an id not offered is `409 action_unavailable`;
  `action` (the name) works as before.
- `GET /v1/sessions/{id}/decisions` (authors only; `ANE_AUTHOR_DIAGNOSTICS=off` disables it): why each
  action is shown, hidden or blocked, with reason codes and explanations (source entity, conditions
  with their failing parts, items and coins, one-time and per-visit use, events, encounters and
  conversations, ranking, relevance and choice-count limits). It is made on a copy of the game: no
  turn, no dice, no encounter, nothing saved (`core_engine/diagnostics.py`, with hooks at the engine's
  own decision points). The studio shows it in ▶ Play → *🔍 Author: why these choices*.
- `conditions.explain`: the failing parts of a condition, with their values.
- The exported ANE AI (`narrative_runtime/`, the UNP protocol) has the same ids: the view's
  `choices[]` are the same structured choices and the view has `state_version`;
  `POST /games/<id>/choose` takes `{"action_id", "state_version"}` and refuses a stale or unknown
  choice with `409 {"error", "code": "stale_state" | "action_unavailable", "state_version",
  "offered"}` (nothing done); `{"action": name | index}` works as before. `Game.choices()`,
  `Game.choose(action_id=..., state_version=...)` in Python. The API and the runtime share
  `core_engine/choices.select`.
- Unity library: `AneView.Choices` (`AneChoice`: `Id`, `Label`, `Category`, `SourceKind`,
  `SkillCheck`, `FailureRisk`, ...), `AneView.StateVersion`, `ChooseAsync(view, choice)` and
  `ChooseIdAsync(gameId, actionId, stateVersion)`; `AneAiException.Code`, `IsStale`, `IsUnavailable`,
  `StateVersion`, `Offered`. With an ANE AI older than action ids, `ChooseAsync(view, choice)` chooses
  by name. The text-adventure sample and the C# protocol test choose by id.

#### Changed
- The studio chooses by `action_id` and `state_version`; a choice made in a tab whose game moved on
  is refused with a warning and the current choices are shown.
- `GameState.take_option` finds the chosen option in its new ranking by identity, not by name: a
  place's action named like a way on (or two same-named choices) could be taken for the other.
- The runtime's `choose(<index>)` takes the choice at that index; it took the first choice with the
  same name (a place's action named like a way on could move the player instead).

### Faster training and evaluation on every core

#### Changed
- `evolution.evaluation_threads: auto` starts one process per physical core (hyperthreads of one core
  count once; a container's CPU set is respected), at most `evolution.max_evaluation_processes`. On a
  10-core / 20-thread CPU, 10, 16 and 20 processes train equally fast, so 10 leave memory and CPU to the
  rest. `cpus` keeps the previous behaviour (every logical CPU); a number sets it exactly.
- A run's preview story (the champion's best benchmark story) is chosen from the scores the benchmark
  already computed, and only that story is played again in the run's own process; before, every
  benchmark story of the champion was replayed there one after another (13 s of a 27 s run measured).
  The same story is chosen.
- Evaluations play their stories in a pool of processes (one per physical core), not one after another
  in a single process: the same stories, in seed order (30 stories: 8.7 s → 5.1 s).
- Evaluation processes bind one engine context when they start instead of copying the configuration
  for every story; two methods called on every turn (`_repeat_rule`, `_action_scope`) no longer bind the
  context themselves (their only outside caller, `resume._mark_done`, does).
- Measured (population 20 × 2, 2 generations, seed 7, 10-core CPU): 10 processes 16.0 s before, 11.4 s
  now (one process: 57 s); identical storytellers, scores and preview story.

#### Added
- Tests: the story pool really plays in other processes (not the one-process fallback), the preview
  story is the one a replay of every benchmark story would choose and is the only story played in the
  run's process, evaluations give the same stories in a pool, and the worker count follows the CPU
  topology.

### Each game, training run and evaluation keeps its own configuration

#### Added
- `core_engine/context.py`: `EngineContext`, the frozen configuration one game, training run or evaluation
  runs with (world and tuning, storyteller, hyperparameters, tag vectors, `world_version`, seed).
  `EngineModel.context()` gives an engine's own; `EngineContext.capture()` the process-wide one as it is.
- `GameState(..., context=)`; a state keeps the context it started with and binds it on every call.
  `NarrativeEvolution` freezes its run's context when it is created and reads it until the run ends.
- `utils.tag_similarity.TagSpace`: tag vectors and their caches per context (shared between contexts with
  the same vectors and embedding model).
- `config.process_get` / `config.process_dict` (the process-wide configuration whatever is bound),
  `follow_process_config(state)` for tests and debugging tools, `tests/test_engine_context.py`.

#### Changed
- A game in progress, or a running training job, keeps the configuration it started with until it ends:
  a save in an editor, an upload, *Use built-in config*, a UNP world patch or another session's engine no
  longer changes it mid-way. Two engines can be played at once, their turns alternating, each exactly as
  alone. The settings forms' note and every save's message say so (`backend/activation.py`).
- `EngineModel.build_world()`, `new_game()`, `continue_training()` and `retrain()` run in the engine's own
  context and no longer change the process-wide configuration; `activate()` still does (the studio's
  view), and now replaces the process-wide tag vectors instead of merging them into another engine's.
- Clearing the configuration override (a save, *Use built-in config*) and uploading a configuration forget
  the process-wide trained tag vectors, so the files' world is never compared with another engine's
  vectors (before, its tags missing from that engine became zero vectors). The spawn rules cache is kept
  per tag space.
- The UNP runtime restores a save game exactly when it was made with an earlier world revision it served
  (a session started before a world patch).
- Tests that change a setting to probe a story already set up say so with `follow_process_config`.

### A faster studio

#### Changed
- The studio's page no longer carries Gradio's description of its 570 events (11 MB in every page; they
  are still callable by the page, left out of "Use via API").
- The page's loading-status bookkeeping is kept small: events whose progress is hidden register nothing,
  the others only their outputs that can show progress; on opening the page, the Weights load and the
  entry editor's first load show their progress on their status lines.
- The page and its config are sent gzipped (13 MB before, 0.2 MB now).
- Measured in headless Firefox: ready in 3.5 s (was 18 s), a click to another destination 1.3–2.6 s (was
  4–38 s), and the browser idle between events (it was busy all the time). Nothing an event does changed.

### Settings say when they take effect

#### Added
- ⚙ Advanced → App & jobs: each parameter says who reads it (the API, the studio, or both, when they
  start), and a table shows it saved, running in the API, running in this studio, and after which restart
  it takes effect, with a **⏳ Restart pending** notice where saved and running differ. The studio's
  `ANE_UI_HOST`, `ANE_UI_PORT` and `ANE_UI_SHARE` are shown where they set a launch setting instead of the
  saved value (no restart pending for those). `GET`/`PUT /v1/app-settings` add `running` and `parameters`.
- The configuration's forms (Story rules, Simulation, Weights, Engine logic) carry a note on when a save
  takes effect (`GET /v1/reference`'s `activation`).

#### Changed
- Save confirmations say when they take effect, without implying that anything running changed: the
  configuration's saves (at once for new games, runs, evaluations and jobs still queued; a game in
  progress or a running job is not restarted; an uploaded or engine configuration set aside, said when it
  was), the app parameters (nothing running changes; what a restart of the API and of the studio would
  use; the environment kept), an uploaded configuration (in memory until the next save, *Use built-in
  config*, a game with a trained engine, or an API restart) and Grow the world's tick.
- The studio launches with an empty `ANE_UI_HOST`, `ANE_UI_PORT` or `ANE_UI_SHARE` treated as unset (it
  used to take the empty value), as the activation table shows it.

### The judge and the augmenter take turns on one GPU

#### Added
- `llm_sharing` (on by default; `ANE_LLM_SHARING`): the judge (Gemma 4) and the augmenter (Qwen3.5-4B)
  take turns on one GPU. The model not running sleeps in CPU memory (vLLM sleep level 1) and is woken
  when it is needed (1–2 s); a model idle for `idle_seconds` goes to sleep. Off when each model has a GPU
  of its own (several sessions).
- `compose.yaml`: both services in sleep mode on GPU 0 by default, started through
  `packaging/vllm_turn_start.py` (the other put to sleep before loading; started together, the augmenter
  waits for the judge to serve first, `ANE_LLM_WAIT_FOR_PEERS`, so they don't load at once). The judge's GPU memory share
  is 0.72 (was 0.85) and the augmenter's 0.6 (was 0.75), so each fits beside the other asleep.
- The judge's and the augmenter's status say whether the model is asleep.
- World augmentation asks vLLM for valid JSON (`augmentation.llm.json_output`, structured output): a
  small model's occasional malformed reply (a missing comma) no longer fails a proposal.

### World augmentation: a tick, and its own model (Qwen3.5-4B on vLLM)

#### Added
- 🌍 World → ✨ Grow the world: **Augment the world with the language model**, a tick saved as
  `augmentation.enabled` (what `python main.py` reads too). Off: *Propose* is disabled,
  `POST /v1/jobs/world-proposal` is refused (409), and the terminal proposes nothing.
- `GET`/`PUT /v1/augmentation`: whether augmentation is on and its model's status; switch it.
- World augmentation runs on its own model, **Qwen3.5-4B** (float16, not quantized; text only; without
  its thinking), a role of its own in `utils/llm_model_loader.py` (`augmentation.llm.*`,
  `ANE_AUGMENT_URL`/`_MODEL`/`_API_KEY`/`_MODEL_PATH`). The judge and the editor's suggestions stay on
  Gemma 4.
- The augmentation prompt spells out the engine's action shape, with one of the world's own actions as
  the example, and a small model's common deviations are put into that shape, so its proposals can be
  added (before, every proposed action was refused: the prompt said only `"actions": [...]`).
- `compose.yaml`: the `augmenter` service (profile `augmenter`), serving the weights from
  `/data/angelisg/llm_models/Qwen3.5-4B` (read-only) with the judge's vLLM image, on GPU 0; the API
  finds it at `http://hamlet-ane-augmenter:8000/v1`.

### Nine limitations fixed

#### Added
- 🧩 The condition builder is under every condition field: an action's *No longer offered once*, a
  mission's *Offered only if*, and a dialogue line's *Said when* (choose the line; its condition is kept
  in step with the table both ways), besides an entry's *Enabled if* and an action's *Offered only if*.
- Renaming an entry renames what refers to it, in the same save: the map's routes, people's known places
  and items, other entries' actions and conditions, missions, variables and the tuning parameters that
  name a place. The other files are edited in place, their comments kept, and the message counts the
  references per file. A name that also names something of another kind is not renamed elsewhere: a rename
  that would then leave references dangling is refused, as before. Both `PUT .../draft` and `PUT
  /v1/entities/{kind}/{name}` do this.
- `GET /v1/world/revision`: a fingerprint of the world's entries. The content library looks at it every
  10 seconds and lists changes made elsewhere (another browser, Grow the world, an import, a file edited
  by hand) without ↻ Refresh, and says when the entry open in the editor was renamed or deleted elsewhere.
- The address follows the destination open (`?section=…`, replaced in the browser's history), however it
  was opened, and a banner or menu choice is marked at once, before the server answers (not with
  unsaved changes, where the guard decides).
- `python main.py --apply-augmentation FILE` adds a reviewed world augmentation proposal, checked as the
  studio checks it.

#### Changed
- `python main.py` no longer writes world augmentation into `config/`: with `augmentation.enabled`, it
  keeps the proposal in `augmentations/run_<time>/proposal.yaml` for review (only when the world is below
  the augmentation targets) and runs with the world as it is. The old path rewrote `locations.yaml` and
  dropped the map's routes and the files' comments.
- The training work counts the turns a story may spend in fast travel (`travel.max_free_turns`, which do
  not count toward its end) when the steps allow them; `GET /v1/simulations/work` adds
  `fast_travel_turns`. With the shipped settings a full-length story counts 110 turns instead of 100
  when more steps than its length are asked for.
- The preview's *changes* list the map's changes too: a path that opens or closes, a place unlocked,
  being held.
- A quest cleared in ▶ Play because the new engine's world doesn't have it is named by its title (as
  the world that listed it had it), with its key.

#### Fixed
- Old lint warnings: unused imports in `main.py` (the tests and the backend now import `_build_action`,
  `_build_outcome`, `_stat_usage_errors` and `_fitness_breakdown` from `core_engine`), an unused
  variable in `core_engine/game_state.py` and an unused import in `backend/sessions.py`.

### Jobs: followed again after a refresh, and cancelled

#### Added
- The studio remembers the job it started in 🧠 Train and in ⚖ Evaluate, in the browser's local storage (one
  per section). After a refresh or a lost connection it follows that job again through `GET /v1/jobs/{id}`,
  without starting another, and shows its result as if it had never left. A failed, cancelled or lost job
  (an API restart; or one of more than the latest 100) is said in so many words. It never attaches itself to
  another job of the global list: the job panel marks the browser's own job as **yours**.
- `POST /v1/jobs/{id}/cancel`, and **🚫 Cancel it** in the job panels. A queued job is cancelled at once and
  never starts. A running one becomes `cancel_requested` and stops at its next safe point (training: after a
  generation; evaluation: before a story is judged, or after the stories are played), then is `cancelled`.
  A cancelled job keeps no result: its `partial` says how far it got. A training cancelled makes no engine
  and its recorded run is `cancelled`. Work that completes before it reaches a safe point finishes, with a
  `cancel_note`. Either way, the worker goes on to the next job.

#### Changed
- `StudioAPI.wait()` stops on a cancelled job (`JobCancelledError`) and waits through a lost connection
  for up to a minute before giving up.

#### Fixed
- The job panel's polling could stop for good when a run was submitted while the page was still loading:
  a refresh computed before the job existed switched its timer off. The timer now never stops (it slows
  down instead).
- The job panel stopped refreshing once a job chosen in its lists had ended, because the lists refused a
  value no longer among their choices.

### Job panels in Train and Evaluate; the discovered map drawn

#### Added
- **⏱ Jobs** in 🧠 Train and ⚖ Evaluate shows the jobs queued, running and recent. Each shows its state,
  its progress counted as the job counts it (generations, or stories judged), scores, elapsed time and
  queue position, and a failure's error with what to do. A finished job's result can be opened there.
  The panel refreshes every 2 seconds while one of its jobs is active, every 10 otherwise, without holding
  up the rest of the studio.
- Jobs report a `label` with their progress and a `queue_ahead`. An identical submission while a job is
  queued or running returns that job (`reused`) instead of queuing it again.
- ▶ Play draws the discovered map of the game (only what the player has found).

### 🌍 World: Content, Missions and Map

#### Changed
- 🌍 World has a sub-navigation: **📚 Content** (Entries, Story rules, Behaviour by tags, Grow the world,
  Checks; it goes back to the last one opened), **🎯 Missions** and **🗺 Map**. The missions and map
  interfaces, their API calls and their YAML stay as they were. Switching keeps selections and unsaved
  drafts: an editor with unsaved changes asks first.
- The map in 🌍 World is named the **authoring map** (the whole world). The map in ▶ Play is the
  **discovered map** (this game's: only what the player has found).

### 🌍 World → Entries: a condition builder

#### Added
- A condition builder under an entry's *Enabled if* and an action's *Offered only if*: nested ALL, ANY
  and NOT groups, and predicates (has item, visited, at location, variable, stat and resource comparisons,
  mission, events, actions, ...) chosen from the world. It is checked as a save checks it. Conditions it
  reads are written back as they were, and parts it cannot show are kept as written. The YAML field stays
  as the advanced mode.
- `GET /v1/conditions/reference` and `POST /v1/conditions/validate`.

### 🌍 World → Entries: preview the unsaved draft

#### Added
- **👁 Preview the draft**, below the entry editor, shows the entry as its unsaved draft makes it.
  *Content (static)* gives the texts, actions, outcome text and what each outcome changes, and what a save
  would refuse. *Simulated scene* is a sample scene the engine plays with the draft, with its sample state
  listed: start, seed, storyteller, character, variables at their defaults, quest. Each of the entry's
  actions is tried on a copy of that state.
- `POST /v1/entities/{kind}/{name}/preview`, read only: it writes no configuration, changes no
  progression, records nothing and advances no session.

### 🌍 World → Entries: Save changes at once, and the library's next pages

#### Added
- **💾 Save changes**, always under the entry's status line, saves every section with unsaved changes at
  once (`PUT /v1/entities/{kind}/{name}/draft`). The entry is checked whole with the world and written
  once, or not at all, with every problem named by section; fields no section touches are kept. The
  forms and the YAML are then reloaded from the stored entry.
- The status line names the sections with unsaved changes and those saved.
- The content library lists 200 results at a time, with *Show N more* for the next ones.

#### Changed
- The leave question's **Save** no longer saves some sections when another fails: it is the same
  single save as *Save changes*.

### 🌍 World: a searchable content library

#### Added
- 🌍 World → Entries has a content library listing every location, NPC, item and event, with its tags,
  facts and first description, a search, per-type counts and type filters. Choosing an entry opens it
  in the editor, guarded like the pickers (unsaved changes are asked about first). Searching and
  filtering never change the open entry. Empty worlds, empty types and searches without results say so.
- `GET /v1/entities/{kind}?fields=summary`, a light summary of each entry; the other `fields` values
  are unchanged.

### ⚖ Evaluate: a selected trained engine, or the baseline, said before and after

#### Changed
- ⚖ Evaluate has one target for the judge benchmark and the calibration: a **selected trained engine**
  or the explicit **baseline** (the current configuration with random storytellers). *Evaluate this
  result* selects the completed engine; a training or a loaded engine is offered (the newest selected)
  without a reload. An engine target without an engine is refused, never replaced by the baseline.
- The target is shown before submission, with each section's plan (seeds; in A/B: A and B are both the
  target, B with version B's settings, same seeds, both orders). It is also shown in the results.
  A/B results no longer call side A "the current settings" when an engine was selected.

#### Added
- `GET /v1/evaluation/target`; judge-benchmark and calibrate-fitness results and jobs carry `target`
  (and `seeds`; pairwise results also carry `a` and `b`).

### ▶ Play: quests from the chosen engine's own world

#### Changed
- ▶ Play's quest choices depend on the chosen trained AI and the player profile. They are read from
  that engine's saved world, without making it active, with the profile's save game, and refreshed
  when either changes. A quest chosen before that no longer fits is cleared, saying why. A note
  beside the pickers names the engine and its world.
- `GET /v1/missions/playable` takes an optional `engine_id` and answers `engine_id` and `world`
  (its identity); the existing fields are unchanged.

### 📦 Export in five steps

#### Changed
- 📦 Export is laid out as the steps of an export: choose what to export, where it runs and the options,
  then export, download and read how to use it on that platform (Python, Unity, or the standalone
  program, with the macOS and Windows notes). Multi-run exports and the Python-only sources stay.
- Systems without a runtime here are marked *not available here*, with the API's reason
  (`GET /v1/exports/platforms`). Export is enabled only for a valid combination; otherwise step 4 says
  why and how to resolve it. An invalid export is also refused before the API is asked.
- *Export this result* keeps its engine chosen: only the user's own choice of source or platform lays
  out the form again.

### Studio: edits that were lost, overwritten or could not work

#### Fixed
- 🎯 Missions:
  - A new or renamed mission whose key another mission already has is refused. It used to replace that
    mission. An empty key is refused too.
  - Weight 0 is kept; it used to be saved as 1.
  - A single step with its own text or rewards stays a step; these used to be dropped.
  - After the mission in the form is deleted, Save no longer brings it back.
  - Deleting a mission no longer reloads the missions YAML over unsaved edits.
  - A YAML save that removes or renames the open mission no longer reports an error.
- 📝 Entries:
  - A new entry named like an existing one is refused; it used to update that entry.
  - Scheduling's Save and YAML editor are shown for events and items only. For places and people
    the API refuses scheduling rules.
  - Dialogue lines with several variants stay variants; they used to be saved as one string.
  - Dropdowns of the world's names (the action editor's pickers, Lies at, known places and items,
    the map's places, the arrest location, the player profile) fetch their choices when opened.
    Entries made, renamed or deleted since the page loaded now appear.
- 📜 Story rules:
  - A cleared number is refused with why; it used to keep its old value silently.
  - Reset profile (new player) asks first, because it cannot be undone.
- 🗺 Map: a place deleted meanwhile is no longer saved to, and the place list follows the world.
- 🏷 Behaviour by tags:
  - Saving one table reloads only that table; it used to reload both, losing the other's edits.
  - A failed save keeps the edits.
  - An indicator shows unsaved changes, and closing the page with them asks first.
- ✨ Grow: the ticks follow the proposal's YAML, so an entry renamed there is no longer left out.
- ▶ Play:
  - The game survives a page reload (`ane-play-session`, while the API still has it).
  - Up to 12 choices get buttons (it was 8), and more are said in the status bar, not hidden.
  - "Sent to the UNP" is read from the save game, where the delivery is noted.
- The records import note and the records refresh show the API's error message.

### Engine names and result cards; the weights' shipped values, leave question and trait scaling

#### Added
- Completed engines can be given an optional name: on Train's result card, or in 🧠 Train → 🏷 Finished
  engines. Train, Play, Evaluate and Export show every engine by one label: its name, or, for an
  unnamed engine, what it is, when it was made, its generations and its training fitness, always
  with its short id. The engine and run ids stay the identifiers. An export without a name takes
  the engine's.
- Result cards, in Train and beside the engine pickers of Play, Evaluate and Export, show when the
  engine was made, its world, its generations and training settings, its training fitness, the
  judge's engagement (once judged: a different measure, shown apart) and the storyteller traits
  that scale the Basic weights.
- `GET /v1/engines/{engine_id}/card` and `PUT /v1/engines/{engine_id}/name`. Engine listings also
  answer `name` and `label`. A judge benchmark or calibration of an engine is kept on its card.
  Names and evaluations are kept with the saves (`engine_meta.json`, format 1, older files
  migrated), so they work with recording off and survive a restart.
- Weights: *Changed from the shipped values* lists the saved values that differ from the release's,
  and *↺ Shipped values into the draft* puts them back for saving. `config/default_history.json`
  records the shipped values, and `GET /v1/weights` answers them.
- Leaving ⚙ Advanced with unsaved weights asks Save, Discard or Stay, like the World editors.
- The Basic controls a storyteller scales say how far its traits move them (from `genome_bounds`).
  *Event frequency* has the turns between events and the most events per story.

#### Changed
- The studio waits up to 90 s (`ANE_UI_STARTUP_WAIT`) for its page to answer at start, instead of
  Gradio's single 3-second check, which a busy host could fail.

### A guide for building another frontend on the API

#### Added
- [docs/FRONTEND.md](FRONTEND.md) describes every studio view as requests and JSON fields: what each
  view loads, its components, its actions, the client-side logic a new frontend has to reimplement,
  and where it navigates. It also covers the API contract (headers, errors, jobs, downloads, drafts,
  conflicts and undo), real samples of the shared response shapes, the gaps a rebuild should not
  copy, and an index of which view uses which of the 128 endpoints.
- `FrontendGuideTests` (in `tests/test_studio_coverage.py`) fails when the index and the API's
  routes disagree, or when the guide names a request the API does not have.

#### Changed
- docs/API.md links to the guide, and its CORS note now lists `PATCH`, which the API allows.

### ANE AI runtime: stopping without cutting answers short

#### Fixed
- `POST /shutdown` sometimes answered with the headers but no body (`IncompleteRead`): the process
  exited before the answer was written. The server now stops listening only after that answer is sent.
- A stop by signal, by the UNP process ending or by stdin closing now waits up to 5 seconds
  (`DRAIN_TIMEOUT`) for the requests still being answered, rather than exiting under them.
- If saving the sessions fails during a shutdown, the server still stops (it no longer stays up
  answering `503`).

### Weights: a Basic view of common gameplay controls

#### Added
- ⚙ Advanced → Weights has two views of one draft. **Basic** (shown first) groups 20 common gameplay
  controls in Difficulty, Pacing, Repetition and Event frequency. Each control is one tuning parameter
  (`backend/tuning_basic.yaml`; no presets), explained by what raising or lowering it does in play.
  **Advanced** is the complete editor, as before. A value edited in one view is the same value in the
  other, and switching views changes no value.
- The draft's status, above both views, lists every unsaved change. Changes made in Advanced to
  parameters that Basic doesn't show are listed apart (*Advanced only, not shown in Basic*). Unsaved
  weights make the browser ask before the page is left.
- `GET /v1/weights` also answers `basic`, the Basic view. `PUT /v1/weights` takes an optional
  `expected` (the values when loaded); a parameter changed since then is refused with `409`.
- The parameters of the director, variation, unexpected events, first trips and the alert name the
  setting they need (`requires`), and are marked inactive while it is off.

#### Changed
- The weights form saves only the values that changed, rather than every field, and *Discard changes*
  reloads the saved values. The form also reloads the saved values when the page opens, so a page built
  earlier no longer writes back stale values.

### Drafts everywhere, undo histories, references, and the engine's parameters cleaned up

#### Added
- 🎯 Missions, 📜 Story rules and 🗺 Map track their drafts like the entry editor: an indicator, their
  pickers (mission, profile, place, *New mission*) guarded, and Save / Discard / Stay when leaving them.
- An entry draft is kept in the browser: after the page is closed or reloaded, *Unsaved changes from your
  last visit* offers Restore or Discard.
- Undo keeps a history per panel (newest first), kept in the browser across reloads; the API keeps the
  deletions with the saves (`deletions.json`), so an undo survives its restart.
- Deleting an entry says first what refers to it and would no longer work (`GET
  /v1/entities/{kind}/{name}/references`).
- Weights: parameters that need `outcome_resolution.hidden_adjustments` are marked inactive while it is off.

#### Changed
- Draft baselines are set from the loaded values in the same step (no second round trip); an edit sends
  only its own part, and leaving checks only which parts are unsaved.
- Saving weights rewrites only the numbers that changed: `hyperparameters.yaml` keeps its comments.
- The tuning metadata's units are short (≤ 45 characters) and its code references point at the reading
  line; a test fails when a reference no longer reads its parameter.
- `spawn_selection.immediate_repeat_penalty` now applies, to the place just left (it compared with the
  current place, never a candidate): discoveries rarely lead straight back.
- Every training run uses `evolution.crossover_rate`; the app parameter `crossover_probability` is retired
  (ignored in a saved file).
- A negative `action_selection.stay_penalty_power` or `fitness.objective_losses.scenario_duplication.power`
  is refused when the world is built (it would divide by zero mid-story).

#### Removed
- Parameters that changed nothing: `outcome_scoring.*`, `fitness.setup_payoff.early_setup_bonus`,
  `simulation.mutation_rate`, `transition_objective.character_goal_weight` and `known_location_bonus`,
  `spawn_selection.character_alignment_weight` (and the code paths no caller could reach). An older config
  folder keeping them still loads; they are ignored and not offered.

### Training defaults, Train's result, and the records by purpose

#### Changed
- The saved training defaults are 40 storytellers × 25 generations × 2 stories (with full-length stories:
  200,000 simulated turns of the 1,000,000 allowed). They were 400 × 200 × 6 (48,000,000), so a run that
  set nothing was refused; `python main.py` uses the same defaults.
- *▶ Play this result* starts a game with the completed engine at once (with the player, quest and
  character chosen in ▶ Play), instead of only choosing it.
- The records are where they are used: *🧠 Train → From a recorded engine* (continue, retrain), *⚖ Evaluate*
  (recorded stories: read, judge, compare), *📦 Export* (a recorded engine's, run's and world's files) and
  *⚙ Advanced → 🗂 Records* (recording, the import folder). Train's *Records* tab is gone.

#### Added
- After a warm-started run, its summary says how many remembered storytellers it really started from (the
  summary before the run counted the memory), and a run's `training.executed` has `warm_started`.
- Train's completed engine is kept in the browser: its card and actions come back after a reload, while
  that engine is still a finished run.

### Tuning parameters: labels, units, defaults and bounds from the engine

#### Added
- Every tuning parameter in *⚙ Advanced → Weights* (236) has presentation metadata derived from the
  engine's code (`backend/tuning_parameters.yaml`, read by `backend/tuning.py`): a readable label (e.g.
  `skill_check_resolution.difficulty_rate_per_step` is *Difficulty increase per turn*), what the engine
  does with it, its unit on its own scale (a percentage of the 1–100 roll, a probability per first trip,
  an event pressure compared with a threshold, score points per ...), the engine's default, its numeric
  type, bounds where the engine sets them (`story_rules`, `fitness.length_normalization.floor_steps`) and
  the code that reads it. The parameter's path stays under it. The parameters the engine does not use at
  present (all of `outcome_scoring`, `fitness.setup_payoff.early_setup_bonus`) or only in branches that
  never run say so; those of `outcome_resolution` that need `hidden_adjustments` say that.
- `GET /v1/weights` gains `parameters` (that metadata, per parameter); a parameter without metadata keeps
  a fallback (a label from its name, its type as written).

#### Fixed
- Saving the weights cut a fraction typed for a parameter written as a whole number down (40.5 became 40,
  even for decimal parameters like `evolution.alternatives_weight`): an integer parameter now refuses a
  fraction, a decimal one takes it. Values that did not change are kept exactly as written (40 stays 40),
  and a value outside the engine's bounds saves nothing (it used to be written, and `story_rules` then
  stopped the world from building).

### Deleting in the World editor: asked first, with Undo

#### Added
- *Delete entry*, *Delete action* and *Delete mission* ask first, naming exactly what goes (content
  type, name and place; an action's number and name; a mission's title and key) with **Delete** and
  **Cancel**; after a deletion, **↶ Undo** brings it back for the rest of the session.
- The API keeps a deleted entry, action or mission whole with its place: the delete responses carry
  `undo` (a token), and `POST /v1/deletions/{token}/restore` puts back that record only, where it was
  (next to its old neighbour if the list changed since, and said so). A name taken again or an
  action's entry gone is `409` (nothing newer overwritten); a record that would now be invalid is
  `400`; a deletion no longer kept is `404`.
- `DELETE /v1/entities/{kind}/{name}/actions/{index}?expected_name=`: deletes only if the action at
  that index has that name (`409` otherwise), as the studio now asks.

#### Unchanged
- The delete rules: at least one place, person and item remains (events may all go); an action's
  remaining list and the remaining missions are checked as before.

### World editor: drafts, and nothing discarded without asking

#### Added
- *🌍 World → 📝 Entries* tracks a draft of the entry: *Fields*, *Actions & outcomes*, *Scheduling*,
  *More details* and the three YAML editors, each against what it held right after its last load or
  save (read back from the browser, so loads are never edits). A line under the entry says whether
  anything is unsaved, and which part.
- Changing the entry, the content type or the action, *New entry*, *New action*, another World section
  or another destination with unsaved changes is undone and asked about: **Save** (each changed part
  with its own save, then on), **Discard** (then on) or **Stay**. Closing or reloading the page asks too.

#### Fixed
- Changing the entry or the content type used to reload every form at once, silently discarding what
  was being edited; so did *New entry*, adding the language model's proposals (which also changed the
  entry), and another action replacing an edited one.
- A failed save no longer reloads anything: the *Every field (YAML)*, scheduling YAML and actions YAML
  saves used to reload the other forms even when they failed, and the entry's save its YAML. A
  successful save reloads only what it made out of date and has no unsaved changes of its own.
- An NPC with a structured goal (`{goal, tags, needed, reward}`, e.g. the Smuggler) could not be opened
  in the editor (`GET /v1/entities/NPCs/{name}` failed with 500), and saving its fields would have
  turned the goal into text: it is now a line of YAML in *Goals*, saved back as the structured goal.

### Train: a run, its progress, its completed engine; the game in ▶ Play

#### Changed
- *🧠 Train → Run* is organised as the work goes: *1 · Configure the run*, *2 · Progress and the
  completed engine* (the progress bar follows the generations on the result card, which then names
  the engine made, its run, best fitness and generations, and whether the run executed what its
  summary showed), *3 · Learning results* (curve, learned traits, report, files, learning memory).
  A continued or retrained engine opens *Run* with its result.
- A training from the studio no longer starts a game. It used to start one after every run,
  continuation and retraining, which nobody played: a play session (taking a slot of
  `max_web_sessions`), a start and a mission written to the player's profile (so later games
  avoided that start), and an empty play story in the records.
- The integration diagnostics moved to *⚙ Advanced → 🔌 Integration*: the game in ▶ Play as a UNP
  sees it (set the player, tell it what happened, its parameters), sending a session report to the
  UNP again, the runtimes available and the world catalogue (formerly in ▶ Play and 📦 Export).

#### Added
- After a training, **▶ Play this result**, **⚖ Evaluate this result** and **📦 Export this
  result** open those destinations with the exact completed engine chosen (its id, never the newest
  in its place); nothing is played, judged or exported until the user starts it.
- The API: `start_game` (default `true`, as before) on `POST /v1/simulations`, `/v1/jobs/simulations`
  and the records' `continue` and `retrain` (and their jobs); with `false` the training only trains,
  and the response has `game_started: false` and `session_id: null`.
- ⚖ Evaluate's storytellers include the finished runs, and the judge jobs accept a finished run kept
  without the records (`engine_id`), which used to be refused with `404`.

### Training settings: saved engine defaults, and a run's own overrides

#### Changed
- One model for a run's settings: the **saved engine defaults** (`evolution.*`, `reproducibility.*`,
  edited in *⚙ Advanced → Simulation*, labelled *Default …* there) with what **the run sets for
  itself** in *🧠 Train*, the place a run is set up. Each Train setting shows its saved default and is
  used as it is unless *Set … for this run* is ticked; reproducibility and warm start are *Saved
  default* or chosen for the run, and the seed can be set for a run.
- Before a run starts, *This run, resolved* shows what it will execute (population, generations,
  stories per storyteller, story length, seed, warm start, the work), each with where it comes
  from; after it, the report says whether it ran exactly as summarised. Retraining a recorded
  engine shows the same, with that engine's saved defaults.
- The API: a run's `candidate_count`, `generations`, `simulation_steps`, `episodes`, `deterministic`,
  `seed` and `warm_start` are optional, the saved defaults when left out (a request leaving out the
  first three used to get 8, 5 and 16). `GET /v1/simulations/work`'s values are optional too.

#### Added
- `POST /v1/simulations/resolve`: what a run (or, with `engine_id`, a retraining) would execute,
  resolved by the backend's own rules (`backend.sessions.resolve_training`, which the runs use); every
  run's response carries `training` (`overrides`, `sources`, `resolved`, `executed`). A run can set
  its own `seed`.

#### Removed
- The app parameters `default_candidates`, `default_generations` and `default_simulation_steps`
  (a second set of run defaults): a saved file that has them still loads, without them.

#### Fixed
- A retraining marked deterministic did not seed the random generators as a new run does, and
  without a saved seed it silently ran random; it now follows the same rules (and is refused
  without a seed).

### Training: requested versus effective story length

#### Changed
- *🧠 Train* explains how long a story is before a run starts: **Full-length stories enabled: up to
  N turns** (the configured `narrative_end.max_actions`) or **Bounded stories: up to N turns**; when
  the steps asked for do not set it, why (full-length stories, or steps beyond the story's end),
  and the **Steps per simulation** slider is marked (*not used: full-length stories* or *capped at
  N*, outlined). The work is shown as its multiplication: storytellers × generations × stories each
  × turns = work, of the limit.
- `GET /v1/simulations/work` returns `requested_steps`, `effective_max_turns`, `full_length_stories`,
  `story_length`, `steps_decide`, `override` (`{setting, reason}`), `factors` and `over_by`
  (`story_turns` stays). The studio only shows it; a run is refused on the same numbers, and the
  refusal says why a story counts as many turns as it does.

#### Fixed
- Steps beyond a story's end were counted in the work though never played (e.g. 400 steps for
  100-turn stories counted 400 turns a story), so runs within the limit could be refused. The
  story's length now has one definition (`core_engine.game_state.story_length`), used by the game,
  training and the work count.

### The studio's navigation: destinations, a workflow banner, a menu on narrow screens

#### Changed
- The studio's destinations are **🌍 World**, **🧠 Train**, **▶ Play**, **⚖ Evaluate** and **📦 Export**,
  in the order of the work, and **⚙ Advanced** for the technical settings (simulation, weights,
  engine logic, app and jobs). The story's own settings (story rules, messages, persistent
  variables, character stats) moved to *🌍 World → 📜 Story rules*; Export is a destination of its
  own (it was inside Train & export).
- The workflow banner is the navigation: a button per destination, the open one highlighted. On
  screens narrower than 820 px it gives way to *☰ Menu: go to a section*, which lists every
  destination. The destinations' own tab bar is no longer shown, so no destination is behind
  Gradio's overflow button; a destination's sections that do not fit a narrow screen are behind an
  overflow button that now reads **··· More**.
- Stable destination ids (`world`, `train`, `play`, `evaluate`, `export`, `advanced`): the tabs, the
  banner's buttons (`#nav-<id>`), the destinations (`#dest-<id>`), the menu, and the address
  (`?section=<id>`). Changing destination only selects a tab, so every form, game and result keeps
  its state.

### The studio: five tabs, a game screen that fits a window

#### Changed
- Five tabs, with sub-tabs instead of long pages of folded panels: **🌍 World** (*Entries*: fields,
  actions & outcomes, scheduling, more details, suggestions, every field as YAML; *Missions*, *Map*,
  *Behaviour by tags*, *Grow the world*, *Checks*), **⚙ Settings** (simulation, story, weights,
  engine logic, app & jobs; what were tabs 2, 3 and 5), **🧠 Train & export** (*Train*, *Records*,
  *Export*), **▶ Play** and **⚖ Evaluate**. Smaller tab labels and header.
- **Train & export only trains and exports**: its game area is gone. Training shows the report, the
  learning curve, the traits and the run's files; *▶ Play the newest* opens ▶ Play. Playing a
  recorded engine or an engine file, the player profile, the quest, the character (now also *my
  own points*), the save game, the judge of a playthrough and the UNP session tools moved to ▶ Play.
- ▶ Play fits a window: a compact **place card** (where, who is here, what can be seen), **What
  happened** below it, smaller choice buttons, and **🧍 You** (power, inventory, stats, remembered
  values, the alert, the last check) beside the quest card; the people and the map, the journey
  and the story fold away. The *New game* panel folds once a game is on; a save game is continued
  with one button.

#### Added
- Everything the terminal editor and the engine's tools do is in the API and the studio:
  language-model **suggestions** for an entry's texts (`POST /v1/suggestions`); **growing the
  world** with new places, people and items the language model proposes, reviewed and added only
  when ticked and checked (`GET /v1/world/size`, `POST /v1/jobs/world-proposal`, `POST
  /v1/world/additions`; the augmenter's proposal no longer writes anything by itself:
  `WorldAugmenter.propose`); a place's **points of interest** (`interaction_points`) in its details;
  **importing** earlier engines and games from the server's import folder (`GET
  /v1/records/import`, `POST /v1/jobs/records-import`).
- Play states carry `scene_name`, `description` and `items_here`.

#### Fixed
- Editor suggestions used `vllm.model_name` only; they now use the judge's model settings
  (`ANE_JUDGE_MODEL` too).

### ▶ Play reads as a game

#### Changed
- **What happened** comes first after each choice, at a glance: the turn and your choice (with
  whom, where), the check (rolled, needed, passed or failed), what came of it, what it changed
  (❤ health, 🪙 coins, 🎒 items gained or gone, 📍 where you went, and whether it is a new place,
  👤 who you met, ⚡ an event) and the quest's progress (a step done and the next, or the quest
  complete); then the rest of the turn's story, small. Approaching someone or leaving says just that.
- The **quest card**: a progress bar (`▰▰▱ 2 of 3 steps`), each step done with the turn it was
  done, the current one with what it is about, what to do now (an event by its title), and when the
  quest was completed.
- **Your journey** (new): every choice so far as a table (the turn, where, the choice and with
  whom, the result and its check, the changes, the quest steps), the last 15.
- **The story so far** reads as a story: each place a heading, each choice in bold after a rule,
  events, discoveries, secrets and the quest marked; the engine's bookkeeping lines are left out.

#### Added
- `GameState.journal()`: the story turn by turn as a player follows it; `GET
  /v1/sessions/{id}/journal`; every play state's `turn_summary` (the last turn), also in the ANE
  AI's view. Play states now also carry `talking_to` (it was left out of the API's play state).

#### Fixed
- Training started from a script read from standard input (`python -`) failed, its evaluation
  processes unable to start; a pool that breaks now plays the stories in the process (the same
  scores).

### The world editor: forms instead of YAML

#### Changed
- *1 · Create the world* edits everything with forms; each section keeps its YAML under
  *advanced*. **Actions & outcomes**: pick an action (or add, delete one) and edit it: how often it
  is offered and whether a failure uses it up, its skill check, exclusive group, the event it
  starts, its price and the item it needs, uses or takes, its conditions; its outcomes picked one by
  one: what is told, when, tags, health and coins, where it moves or sends, what it reveals, who it
  removes, a new path, a guide, variables, stats and other effects (choices list the world's
  places, people, items, events and stats). **Uniqueness & scheduling**: an event's start rules or
  an item's uniqueness as a form. **Behaviour by tags**: two tables, people and places, a row per
  rule. **Missions**: a mission's fields and its steps as a table (the goal and rewards written
  `key: value; ...`). **More about this entry** (new): what an NPC knows, how the story calls them
  and what they say; how a place is seen from afar and on arrival, the ways to go there, locked
  exits. **Map**: tick the places a place borders (both ways), beside a drawing of the whole map.
- Nothing a form does not show is lost: every action and outcome of the shipped world, and every
  mission, goes through its form and back unchanged (tested).

#### Added
- API: `GET|PUT|DELETE /v1/entities/{kind}/{name}/actions/{index}` (one action),
  `GET|PUT /v1/entities/{kind}/{name}/details`, `GET|PUT|DELETE /v1/missions/{key}` (with
  `rename`), `GET /v1/map`, `PUT /v1/map`, `PUT /v1/map/{place}` (borders, both ways),
  `GET /v1/behaviours/rules`, `PUT /v1/behaviours/rules/{npcs|locations}`.

### Records: whose run it is

#### Added
- Every run has a user (`runs.user_id`). The studio's and the terminal's runs are `test`; a run
  started through the API is the user the calling backend names in the header `X-ANE-User`, an
  opaque id (a UUID, an account number; never a name or an e-mail address). A request that starts
  a run through the API without one is refused (`400`). `GET /v1/records/runs?user_id=` lists one
  user's runs, and the studio's runs table shows each run's user.
- A records database made before is given the column, its runs the user `test`, the first time a
  process that may write it opens it, after a backup next to it; a read-only one shows its runs as
  `test`.

### Conversations: approach someone, talk, leave

#### Added
- The people present are approached one at a time (`conversations`, on by default; the engine
  decides it, so the studio, the API, the standalone ANE AI and the simulated players all play
  it the same way). On the scene each person is one choice, *Approach the Gate Guard*; in a
  conversation the choices are theirs (talk, ask, bribe, fight, give...) and *Leave the
  conversation*. Approaching and leaving take no turn. Questions and small talk keep the
  conversation going; a decisive choice ends it (an event or a fight, a payment, an item given,
  taken or used, one of an exclusive choice, a threat, a move, the person gone).
- **What people do comes from their tags** (`npc_behaviour`), not from each character: rules
  map tags to behaviours. `starts_conversation` (never, `first_meeting`, `every_visit`): they come
  up to the player, the conversation open; `appearances` (always, `once`, or a number of visits):
  how often they are seen in a story (someone the quest needs now is always shown). Shipped rules:
  `prisoner` (the Smuggler calls a new prisoner over), `quest_giver` (the Captain of the Watch)
  and `hawker` (the Merchant) on the first meeting, `challenger` (the Pit Champion) on every visit;
  `wanderer` (the Runaway Apprentice) in two visits at most, `apparition` (the Pale Conductor)
  once. Hostile NPCs still start their event.
- **Places too** (`location_behaviour`): `draws_in` (never, `first_visit`, `every_visit`): on
  arrival the place's own activities are offered first, with *Look around* for the rest (no turn);
  `visits` (always, `once`, a number): how many visits a story may have there, then it is no
  destination (no travel option, lead or automatic move) unless the quest's next step happens there
  or it is the only way on. Shipped: `lively` (the Tavern, first visit), `spectacle` (the Festival
  of Masks, every visit), `secluded` (the Secret Garden, two visits), `fleeting` (the Bone Orchard,
  once).
- **In the studio**: *1 · Create the world* shows each behaviour of an NPC or a place as a choice
  of the tags the rules give it (with what each does); choosing one sets that tag on the entry and
  removes the others of that behaviour. *Behaviour by tags* edits the rules (a new rule's tag is a
  new choice). `GET /v1/behaviours` lists them; the rules are checked as rules when saved.
- `GameState.take_option(option)` takes any choice a player is offered; views say who the
  player is talking with (`talking_to`), and the studio's status bar shows it.

#### Changed
- Simulated players play conversations too: explorers approach new people, questers prefer a
  choice the quest log names. Simulated play counts turns, not choices, and reuses a turn's
  ranking after a choice that took no turn (the same stories, 11% faster).
- Measured (30 stories per quest, simulated players): about one choice in four is an approach or
  a leave. With people coming up to the player by their tags, quests that go through people are
  won about as often as with every choice on the scene: rid the market of the thief 17 (21 with
  every choice on the scene), a face for the Palace 21 (21), the smuggler's tunnels 19 (21),
  expose the fence 29 (27), champion of the back room 8 (6); without the tag rules, simulated
  players won 15, 16, 13, 24 and 4. With the places' rules too: 16, 15, 17, 25 and 5 (the Festival
  of Masks, `spectacle`, draws players into its own activities on every visit before they find
  the Maskwright). No choice was offered again after it was used; a story plays at about 290 turns
  a second, as before.

### Choices that make sense, quests in steps, fights

#### Changed
- **A question is asked once.** An action that only tells (it reveals a place, a person or an
  item, opens a path or shows the way, and changes nothing else) is offered once unless it sets
  its own `repeat`; an answer that told nothing comes back on the next visit
  (`action_rules.questions_once`). "Ask about the crown" no longer comes back on every visit, in
  any world, including worlds written before.
- **Guides lead toward the quest.** An NPC's lead and an ally's route prefer the places on the
  way to where the quest can advance next (`npc_leads.quest_weight`).
- The quest log says what to do in words: a step that is a tag to bring about shows its title
  ("Win a brawl in the Tavern's back room"), a condition its parts ("Take the bounty on the market
  thief or Ask about the market thief or Report the market thief"), not their raw form.
- *Commission a new identity* (Maskwright) is bought once, for 6 coins, with no stealth check, and
  gives an **Official's Mask** (+10 persuasion, +5 stealth): gate guards salute it, and it is how
  to unmask Aurora. *Ask about the crown* (Aurora) is asked once; the Archivist's, the
  Cartographer's and the Apprentice's are one-time too.
- **No bribes at a gate.** The Gate Guard takes no coins; the only bribe is the jailer's, to a
  prisoner locked in the Dungeon: to open your own cell, or to free the Smuggler, who knows the way.

#### Added
- Characters: the **Captain of the Watch** (a bounty on the thief, paid when the thief is gone),
  the **Thief's Lookout** (once you hunt the thief, they come for you), the **Smuggler** (a prisoner
  in the Dungeon: bribe the jailer, pick the lock or overpower the jailer; freed, he is an ally
  and leads the way toward the quest), the **Fence** (his bodyguard guards the ledger), and the
  **Pit Champion** (the Tavern's back room).
- Fights: the lookout's ambush, the fence's bodyguard, back-room brawls (up to four a story) and
  the champion's bout (one rematch), each with a fighting choice, a talking or sneaking one, and
  a way out.
- Quests in steps: *Rid the market of the thief* (pick up the trail, catch them in the act,
  drive them out: beat them, hand them to the watch, or follow them), *The smuggler's tunnels*,
  *Champion of the back room*, *A face for the Palace*; *Expose the fence* can also start from the
  Merchant's secret. People speak as the story goes (the Merchant after the thief is gone, the
  Thief after their lookout is beaten, the Captain, the Smuggler, Aurora and the Maskwright once
  you wear a mask); the Pit Champion remembers a past win in later playthroughs (`pit_champion`).

#### Measured (30 stories per quest, simulated players)
No choice was offered again after it was used and no bribe outside the Dungeon, in every story of
every quest. Won: rid the market of the thief 21 (14 before its fixes), the smuggler's tunnels 21,
a face for the Palace 21 (8 before Aurora could be asked again after a failure), expose the
fence 27 (5 before the Merchant's secret), champion of the back room 6 (2 before the rematch; a
fighting quest, and simulated players do not heal before a fight); every freed smuggler led the
player out. A story plays at about 290 turns a second in one process in this bigger world (the
names of actions in conditions are now worked out once, not every turn).

### Deployments: the config volume follows the release

#### Changed
- On start, a config volume kept from an older release is brought up to date entry by entry and
  setting by setting: what it lacks is added, what has a value an earlier release shipped (never
  edited: `config/default_history.json`, from git) takes this release's default, and anything
  edited is kept. Edited ones take the defaults when `ANE_CONFIG_TAKE_DEFAULTS` names them (or
  `all`), once per release. Changed files are backed up in `.ane-backups/`; if the result would
  not build, only the additions are made. A volume used to keep an old release's world and
  settings for good: the deployed one lacked 26 of 48 settings sections (the director, mission
  pacing, NPC goals, story rules, the judge's) and the map's routes, and ran stories of up to 200
  turns.

### The judge: a server, and whether it is there

#### Added
- `ANE_JUDGE_URL` (`ANE_JUDGE_API_KEY`, `ANE_JUDGE_MODEL`) points the judge at a vLLM server;
  `compose.yaml` has an opt-in `judge` service (Gemma 4 E4B on a GPU, `--profile judge`) that the
  API finds by default.
- `GET /v1/judge/status` and the studio's judge views say whether the judge is ready and, if not,
  why (no server answering, another model, a refused key, vLLM not installed) and how to start
  one, instead of an error after a click.

### Training speed: runs of up to 1,000,000 simulated turns

#### Changed
- `max_simulation_work` (the largest run, in simulated turns) is 1,000,000 (was 300,000). A run of
  1,000,000 (40 storytellers x 50 generations x 5 whole stories) took 2 min 44 s through the
  studio's code path on a 20-core server, game and export included (4 min 23 s in this release's
  bigger world); the same run took about two hours before (estimated from the measured cost of a
  story).
- Training in the studio and the API plays a generation's stories in a pool of processes
  (`evolution.evaluation_threads`, at most `evolution.max_evaluation_processes`); it used one CPU.
  The processes are spawned with the world and its tag vectors (a file, so they all start at
  once), which is safe in a server with threads. Every pool (the terminal's too) gets one story
  per task, so all CPUs stay busy whichever storyteller tells it.
- A storyteller kept from the generation before keeps its benchmark score instead of playing the
  benchmark's fixed stories again.
- A story plays about 3x faster in one process (134 to 406 turns a second): the actions on offer
  are copied for each turn without the generic deep copy's overhead.

#### Fixed
- The cache of where the quest can advance never hit (the loop over the goal reused the name of
  its key), so every choice the storyteller weighed recomputed it.

None of this changes a result: runs, single evaluations and stories were compared before and after
(the same seeds give the same storytellers, scores and stories, in one process or many). A run of
12 storytellers x 4 generations x 3 stories went from 158 s to 13 s with the same result.

### The studio's interface

#### Changed
- The tabs follow the workflow: *1 · Create the world* (was World editor), *4 · Train the AI*
  (was Simulate & play), then *▶ Play*. The header shows the steps and a live status line (the
  world's places, people, items, events and quests; the trained AIs; recording), refreshed after
  each run. A themed look (violet, light and dark).
- *▶ Play* is a game screen: a status bar (where, a health bar, coins, the quest's current step,
  an event), the scene as a card, what just happened, numbered choice buttons with their check
  (🎲), and an ending banner that appears when the story ends.

#### Added
- `python -m evaluator.compare_baselines`: storytellers trained the old way (the storyteller
  plays) and the new way (simulated players), and random ones, played on the same seeds and
  measured with fitness and the judge (engagement, criteria, agreement, head to head).

#### Measured (Gemma 4 E4B, float16; 12 storytellers x 8 generations x 3 stories per run)
Judged engagement (1-10, mean ± 95% CI, each story read twice), stories played by simulated
players unless noted:

| World (stories) | Random | Old (storyteller trained playing itself) | New (trained with simulated players) | Old, playing its own stories (earlier protocol) |
| --- | --- | --- | --- | --- |
| City (16) | 7.23 ± 0.36 | 7.05 ± 0.41 | 7.30 ± 0.27 | 7.88 ± 0.19 |
| Frozen monastery (10) | 7.17 ± 0.46 | 7.40 ± 0.50 | 6.78 ± 0.44 | 7.65 ± 0.29 |
| Orbital station (10) | 6.97 ± 0.35 | 7.15 ± 0.32 | 7.15 ± 0.38 | 7.05 ± 0.22 |

Head to head, new against old (decided pairs): city 5-4 (7 ties), monastery 2-5 (3 ties), orbital
1-3 (6 ties); every 95% Wilson interval includes 50%. The clearest result is the earlier
protocol's inflation: in the city, the old storyteller's own stories score 7.88 (100% quests won,
33 turns), but 7.05 when a simulated player plays (50% won, 59 turns); the earlier documented
engagements (7.2-7.5) were measured that way. With players, trained and random storytellers are
within the judge's noise at these sizes (the new one in the city has the best payoff, 6.81
against 6.16, and quests won, 62% against 50%). Longer training with more stories per storyteller
(simulated players make scores noisier) and 30+ judged stories per condition are needed to tell
them apart.

### ▶ Play: play your world after training

#### Added
- A **▶ Play** tab in the studio: choose a trained AI (a finished run, the newest first), a
  player profile, a quest and the character, then **New game** (or continue a save game) and play
  the world it was trained on, its storyteller directing: the scene, what just happened, a button
  per choice with its skill check, the quest log, the people met and the map, the ending, the
  story so far, save and end. It works without the records (finished runs are kept with the
  saves), so in Docker too.
- API: `POST /v1/engines/{id}/play` (`latest`: the newest finished run) and
  `GET /v1/sessions/{id}/transcript`.

### The evolutionary AI: the storyteller directs, players play

#### Changed
- Evaluated stories are played by **simulated players** (`core_engine/players.py`: follower,
  explorer, quester, cautious, random; `evolution.player_mix`), who choose among the choices shown
  with only what a person sees. The storyteller (genome) only directs: the world it builds, who is
  where, events and encounters, how the story answers, and which choices are shown in what order.
  Before, a story told without a player's choice took the storyteller's own favourite, so training
  could reward a storyteller for choosing well for the player, which does not transfer to people.
  Measured on 12 storytellers x 6 stories: fitness 758 with the storyteller playing, 561 with
  players, and the two rank storytellers differently (Spearman 0.43). `evolution.player:
  storyteller` keeps the older evaluation to compare with; training states saved before continue
  with it, exactly. The judge's benchmarks, previews and the terminal's stories are played the same
  way. A player's turn ranks the choices once (shared by the choices shown and the step), so it
  costs about as much as the storyteller's own.
- Fitness is presented as what it is, the **training objective** (operational measures), not a
  measure of narrative quality: the studio reports "fitness on the benchmark (the training
  objective)", and docs/AI.md ("Who controls what", "What fitness is, and what it is not") says
  what the director and the player control and how quality is assessed (the judge, people playing).

### Exporting finished runs, for Linux and Unity in Docker

#### Added
- The Docker image builds the ANE AI's Linux runtime from its own code (an `ane-runtime` build
  stage, Debian 11 for older Linux systems), so a deployment exports `linux-x64` and `unity`
  without CI's runtimes ("not available here" before).
- The studio exports **finished runs**: the latest is chosen, and several can be chosen to
  download each (for any platform; the Python version can still be made from a generation's
  champion, the remembered storyteller or this game's storyteller). The list is refreshed when
  the page opens and after each run.
- Finished runs are kept when recording is off (a records folder the container cannot write):
  their engines go with the saves (`ANE_SAVE_DIR/engines`, the newest 20), so they can be
  exported after a restart. `GET /v1/exports/engines` gives `generations` and `loaded`.

#### Fixed
- Docker: exporting a run recorded on the host failed with *[Errno 13] Permission denied:
  '/data/…/models/….ane.zip'*: the records folder is shared with the host, whose engine model
  files only their owner can read. Runs whose engine this server cannot read are no longer
  offered; choosing one gives a clear message. The list of finished runs also survives records
  that cannot be read at all.
- No error from the API shows the server's file paths any more (only a file's name), and the
  studio's records status no longer shows the records folder.
- `PATCH` (setting the player) is allowed for browser clients when `ANE_CORS_ORIGINS` is set.

#### Changed
- A Python export of a run (`source: run` without `generation`, or `source: engine`) is made from
  the run's engine model, with its own world, without switching the studio's active world.

### Studio, Docker and the ANE AI: fixes from a bug hunt

#### Fixed
- Docker: a config volume from an older release mixed old and new files (the old `npcs.yaml`
  next to a newer `missions.yaml`), so its world named an NPC it lacked (*Mission
  'rid_the_market_of_the_thief'.goal.npc_removed: unknown NPC `Thief`*) and every run and edit
  failed. The entrypoint now adds, once, each default entry the volume lacks
  (`python -m utils.config_loader upgrade`); entries already there are kept, and an entry
  deleted afterwards stays deleted.
- An earlier problem of the world no longer blocks every other edit: only the problems an edit
  makes refuse it, and the save names the earlier ones; a world that cannot be played says so
  once (`The world cannot be played: …`, each problem once).
- A save that changes nothing no longer rewrites the YAML file (and loses its comments).
- Runs: the work limit (`max_simulation_work`) was 60,000 simulated turns, so with whole stories
  (200 turns) little more than the default run was allowed while the sliders went far higher.
  It is 300,000 (about ten minutes at most), the play tab shows what a run asks for before it
  starts (`GET /v1/simulations/work`), and a refusal says how to raise the limit.
- ANE AI: a malformed world patch (a string or list where a mapping is expected) answered 500;
  every part of a patch now has its shape checked and the 400 names it.

#### Checked
- 760 stories with a random player on the repository's world, both example worlds and the
  upgraded deployed volume (invariants every turn; save games exact and rebuilt at random
  points), 80 stories for text glitches, every GET endpoint of the API (306 calls), every entry,
  action list, policy, the missions and the map saved back unchanged (100 saves), and 600
  random requests to a running ANE AI: no other problem found.

### The studio, the API and the ANE AI

#### Added
- The studio and the API do for their games what the ANE AI does for a UNP: a session's
  parameters (`GET /v1/sessions/{id}/state`), setting the player (`PATCH .../player`), what
  happened in the UNP's game (`POST .../sync`), save games (`GET .../savegame`,
  `POST /v1/sessions/load`: exactly, or rebuilt from the parameters; save games go both ways
  with the ANE AI) and the world as the ANE AI sends it (`GET /v1/world/catalogue`). In the
  studio: *4 · Simulate & play → This session as a UNP sees it*, and in the export, where the
  ANE AI runs here (each runtime, or why not) and the world catalogue as a file.
- Tests: the ANE AI's command line (`info`; `play` with scripted input; `evolve` and
  `--resume`; `serve` with a ready file, without a token, for one web origin, with offline
  training, on a port in use, on SIGTERM, and with the Unity library's arguments); the Unity
  library against the ANE AI (every route it calls exists and every route is called, the
  fields it sends, platforms and programs, its package and assemblies, a newer export keeps
  the asset GUIDs, the package it ships runs); the new API endpoints and studio views; a
  read-only records folder.

#### Fixed
- Docker: the records folder mounted from the host is not writable by the container's user
  (uid 10001): recording turned itself off, but reading the records still wrote to the database
  (`PRAGMA journal_mode`), so every records list answered 500 and the studio did not start.
  Records this process cannot write are now opened read only, `GET /v1/records` says why
  recording is off (`reason`), and the studio starts without readable records.
- Studio games had the shared random generator, so they could not be saved: each now has its
  own, drawn from the shared one (seeded runs stay reproducible).
- `python run.py play` / `ane-ai play` stops quietly when its input ends.
- A player field or event amount that is not a whole number is refused with its name, not
  Python's message.

### The ANE AI

#### Fixed
- A save game kept by a UNP whose JSON library writes the real number `1.0` as `1` (C#,
  JavaScript) loaded from its parameters instead of exactly: the snapshot's checksum changed.
  Integral reals are now kept as text in the snapshot (`$f`), the checksum reads `1.0` and `1`
  alike, and the Unity library's JSON writes `1.0`. Found by CI's C# protocol test, which now
  runs (its project file held `--` in an XML comment, so MSBuild refused it).
- CI: the macOS arm64 runtime is built on `macos-15` (GitHub retired `macos-14`).

### Documentation and repository

#### Changed
- The README is a short overview (what the ANE does, quick start, the documentation, the
  repository); its sections moved, whole, into guides: `docs/STUDIO.md` (running, every studio
  tab, Docker, the terminal), `docs/WORLD.md` (authoring a world), `docs/AI.md` (the
  evolutionary AI, the director, sessions, story rules), `docs/ENGINES.md` (exports, engine
  models, records, outputs), `docs/LLM.md` (Gemma 4 and the judge) and `docs/DEVELOPMENT.md`
  (layout, tests, CI, building the ANE AI runtimes). Links and code comments point to them.
- Fewer files and folders: `narrative_runtime/catalogue.py` and `patches.py` are one
  `world.py`, `events.py` joined `lifecycle.py`; the example worlds are in `examples/`; the
  Docker smoke test is `packaging/docker_smoke_test.py`; `runtimes/` is no longer in the
  repository (CI and deployments fill it; `docs/DEVELOPMENT.md` explains it).

#### Removed
- `utils/visualize.py` (unused), and with it the `matplotlib` and `networkx` requirements.

### The ANE AI: a standalone engine a UNP launches, drives and stops

#### Added
- Standalone ANE AI per system (requirements 1.1–1.4): `POST /v1/exports` takes `platform`
  (`windows-x64`, `linux-x64`, `macos-arm64`, `macos-x64`, `unity`, or `python` as before) and
  `GET /v1/exports/platforms` lists what can be exported now; the studio's export has *Runs on*.
  A standalone export is a program (`ane-ai`, PyInstaller onedir, no Python needed) with the
  engine package; a platform whose runtime is missing or was built from other engine code
  answers 409. `packaging/` builds and smoke-tests a runtime; `.github/workflows/runtime.yml`
  builds the four on every push, the API's Docker image ships them (`ANE_RUNTIMES_DIR`), and
  `release.yml` attaches them to `v*` releases.
- The ANE AI is made only from a complete engine (a finished run's engine model, or a loaded,
  continued or retrained engine), with that engine's own world, storyteller, training state and
  tag vectors, and can be downloaded once one exists: `GET /v1/exports/engines` lists them;
  `POST /v1/exports` with a standalone platform or `unity` takes `source: engine` (default the
  newest) or `source: run` and answers 409 for a generation's champion, the remembered
  storyteller, a play session or when there is none yet. In the studio, *Runs on* a system or
  Unity shows the complete engines (the newest after each training or load) and keeps the
  export off until one exists. The Python export is unchanged.
- A launch contract: `ane-ai serve --port 0 --parent-pid P --exit-on-stdin-eof [--ready-file]
  [--unp-url] [--data-dir]`; one JSON ready line on stdout (`url`, `port`, `pid`,
  `code_version`, `protocol`), lifecycle events only on stdout, `{"event": "error"}` and exit 1
  when it cannot start. Profiles, saves and the UNP's content go to the data folder (the user's
  app-data folder by default), never next to the program.
- A clean stop: `POST /shutdown` saves every session (`data-dir/suspended/`), delivers events
  and exits 0; the same on SIGTERM/SIGINT/SIGHUP/Ctrl+Break, when the UNP's process ends and
  when stdin closes. `GET /health`.
- ANE → UNP (1.5.1): `GET /world` and `GET /world/<kind>/<name>` (`ane.world/1`: every
  location, NPC, item, event and mission with all its fields, the stats, variables, routes and
  starts), `GET /games/<id>/state` (the session's parameters, `ane.session_state/1`), and events
  POSTed to the UNP (`ane.event/1`: `session.started`, `session.cancelled`, `engine.stopping`).
- UNP → ANE (1.5.2): `player` in `POST /games` and `PATCH /games/<id>/player` (stats, items,
  coins, health, variables: set, never played); `POST /games/<id>/sync` (actions done, items
  gained or lost, NPCs removed, places visited, coins and health; goals checked afterwards);
  `POST /world/patch` (upsert, update, delete, actions, tags, stats), checked like the studio
  checks a world (422 with every problem), kept as world revisions in the data folder or
  written into the package; `POST /world/reset`.
- Save and load games (1.5.3, 1.5.4): `ane.savegame/1` (JSON: the parameters, an exact
  snapshot, its checksum and the engine's fingerprints; `core_engine/snapshot.py`,
  `savegame.py`). `POST /games/<id>/save`; `POST /games/<id>/cancel` returns the session's
  status (outcome `suspended`, kept in the profile) and its save game; `POST /games
  {"savegame"}` continues exactly when the snapshot fits (the UNP's changed parameters applied
  on top), else rebuilds the session from the parameters (`core_engine/resume.py`: nothing
  narrated or paid twice). `Game.save`/`load_game` write and read it; old pickles still load
  from local files, with a warning.
- Security of the local server: a bearer token on every request (`ANE_RUNTIME_TOKEN`, or one
  made and given in the ready line), only `Host: 127.0.0.1|localhost|[::1]:<port>`, no CORS
  unless `--allow-origin`, JSON bodies up to 8 MB, `/evolve` only with `--allow-evolve`.
- Unity library `unity/com.ane.ai` (C#, Unity 2021.3+, no dependencies): installs the runtime
  from StreamingAssets, starts it with the game and stops it on quit (`AneAiBehaviour`,
  `AneAiProcess`), a client with a method per endpoint (`AneAiClient`), its own JSON
  (`AneJson`), an Editor importer for standalone exports, and a text-adventure sample. The
  `unity` export is a `.unitypackage` with the library, the engine package and every runtime
  available (GUIDs fixed per path). CI compiles it outside Unity and drives a real ANE AI with it.
- `core_engine/content_rules.py`: the world checks as pure functions, shared by the studio's
  backend and the ANE AI. `core_engine/version.py`: the engine code's version.
- `docs/UNP.md`: the integration guide (lifecycle, every endpoint, parameters, save game,
  events, errors, C# and Python code, Unity).

#### Changed
- The offline server answers only authorised local requests (it sent `CORS: *` and took
  any request before). Existing integrations add `Authorization: Bearer <token>`.

### Sessions, the save game and the UNP

#### Added
- Session reports (`core_engine/progression.py`): when a session of real play ends, by itself
  or with `POST /v1/sessions/{id}/end`, the player's progression is saved in the profile and
  the report (goal and outcome achieved or failed, stats, inventory, health, coins, goals
  achieved) is sent to the UNP (`session_report.url`, `ANE_UNP_URL`, `ANE_UNP_TOKEN`), its
  delivery noted. `GET /v1/sessions/{id}/report`, `GET /v1/progression`,
  `POST /v1/progression/reports/{id}/send`; in the offline engine `POST /games/<id>/end`,
  `GET /games/<id>/report`, `GET /progression/<profile>`.
- Each goal once per save game (`progression.goals_once`): achieved goals leave the profile's
  pool; `GET /v1/missions/playable` marks them `achieved`.
- One-time actions remembered: `unique_actions` (every attempt, its success, and whether a
  failed retryable attempt stays on offer).
- A session's cast (`session_scope`, `core_engine/scope.py`): what the goal needs stays, the
  most valuable share of the rest joins, the rest stays out (`cast`).
- Start variety by recency (`start_selection.recency_factors`, `mission_variety`).
- Studio: *End this session*, the session's cast, one-time actions and report beside the game,
  and *Save game* (reports, UNP delivery, send again).

#### Fixed
- The play state's session report is `session_report` (it clashed with the run's `report`, so
  every evolution that starts a game failed with 400; CI's smoke test caught it).
- A story's route planning starts afresh after its cast is chosen (stale caches hid goal sites
  added later); the cast is not computed at all when `session_scope` is off.
- CI actions on Node 24 (`checkout@v5`, `setup-python@v6`, `upload-artifact@v5`).

### Everything in the API and in the studio

#### Added
- API: `GET|PUT /v1/entities/{kind}/{name}/yaml` (every field of an entry, the whole world
  checked before saving), `role` and `secret` on NPC entities, the `map` document
  (`world_definition.routes`), `POST /v1/judge/compare` (two stories, both orders),
  `POST /v1/jobs/judge-benchmark` (absolute, or a pairwise A/B of settings on the same seeds,
  played in a separate process so games in progress keep their settings),
  `POST /v1/jobs/calibrate-fitness`, `GET|PUT /v1/fitness/term-weights`; `npc_roles` and
  `judge_criteria` in `GET /v1/reference`; judge jobs report stories judged as progress.
- Studio: an NPC's **Role in the story** and **Secret**; *Every field of this entry (YAML)*,
  *Map* and *Failures that lead nowhere* in the world editor; *Judge this playthrough*;
  judging and comparing recorded stories, recorded runs' settings and generations, and
  recorded worlds to download; *The active configuration*; *Background jobs*; and the tab
  **6 · Evaluate with the judge** (benchmark, A/B, fitting the fitness, term weights).
- `tests/test_studio_coverage.py`: every endpoint is used by the studio or named with the
  equivalent the studio uses; the new endpoints and the studio's new controls.

### Continuous integration

#### Added
- GitHub Actions (`.github/workflows/ci.yml`): on every push and pull request the test suite
  runs (Python 3.10, CPU-only PyTorch, one process per test module), and the Docker image is
  built with `docker compose build`, started as deployed (API and studio, waiting for their
  health checks) and smoke-tested (`.github/scripts/docker_smoke_test.py`: health, missions,
  failure lint, a short evolution that starts a game, one turn, the studio, a recorded run).

### Story beats made by the engine, NPC roles, and learning from the judge

Measured pairwise with Gemma 4 E4B (30 City stories, random storytellers, the same seeds
with the new beats on and off, each pair judged in both orders): the stories with the beats
won 19, lost 3 and tied 8, a win rate of 86% of decided pairs (95% Wilson interval 67-95%).
They won on pacing and coherence in every decided pair, payoff 95%, surprise 90%,
replayability 86%, stakes 84%, agency 75%; exploration was even (50%), and the three losses
were stories whose journey the judge found more varied without fast travel.

#### Added
- NPC roles (`npc_roles`): every NPC has a role in a story, its own (`role:`) or inferred
  from its tags (the quest's hostile target is its nemesis; lieutenants, power brokers,
  informants, henchmen, bystanders). A role's `importance` (0-3), `check_modifier` (harder or
  easier to beat) and `reveal_turns` / `reveal_clues` (out of sight until then, unless a lead
  or the road names them). `[REVEAL]` on meeting, `[TRIUMPH]` on beating someone who matters;
  `role` and `importance` in `people` and `people_met`.
- NPC secrets (`secret: {text, talks, clues, effects}`), told as `[SECRET]`, and the
  `secret_known` condition for the actions they unlock; the city's Merchant and Gate Guard
  have one.
- A climax for every quest (`climax`): a showdown before the last step completes, with one
  choice per stat and calling on an ally; setbacks bring it back, and after `max_attempts` it is
  won at a price. Open threads close as `[REVELATION]`s.
- Failures as beats: another way after a failure no complication followed
  (`director.another_way`: a witness, someone named, a way toward the quest), refunds of
  failed paid attempts (`director.failure_refund`), and `GET /v1/lint/failures`.
- Variation (`variation`): outcome `variants`, result lines that know an outcome was told
  before (`result_again`, `result_habit`), and checks that grow harder with each success of
  the same action.
- Travel (`travel`): fast travel along known ways (not counted toward the action limit), and
  first trips with a scene on the way and word of who is to be found there.
- Stakes (`stakes`): the alert level (`[ALERT]`, a manhunt at the top, harder checks
  against guards), `stakes` in the play state, and what failing costs (`risk`) with every
  check, shown in the studio's choices.
- Consequences (`consequences`) told a few turns later, and memory (`memory`): in real play
  the next story starts with `[LAST TIME] ...` and old friends and foes recognise the player.
- Fitness: `fitness.beats` (climax, triumphs, reveals, consequences, failures that led on,
  dead ends) and `fitness.term_weights`, multipliers of any term.
- Pairwise judging: `GameJudge.compare_states` / `compare_digests` judge a pair in both
  orders and declare a winner only when both agree.
- `judge_benchmark --pairwise --variant key.path=value`: A/B of any setting on the same
  seeds, with win rate, 95% Wilson interval and per-criterion win rates.
- `evaluator/calibrate_fitness.py`: fits judged engagement on the fitness terms and suggests
  `fitness.term_weights` multipliers, choosing the ridge strength by cross-validation and
  reporting how well the fit predicts unseen stories.
- Judge in the loop: `evolution.judge_weight` (default 0, off), `judge_top_k` (2),
  `judge_workers` (4); per-generation `judge_engagement` in the history; the run never stops
  when no model is available.

#### Changed
- Repetition costs more: `fitness.engagement` filler 80 -> 100, loops 6 -> 8, novelty fade
  75 -> 100, and `objective_losses.scenario_duplication` 160 -> 200.
- The Gate Guard has a fourth way past him (settling his debt, once his secret is out).
- The judge's digest shows an outcome as the player read it (its variant) and the new beats.

### Stories that keep their word: twists, callbacks, failing forward

Measured with Gemma 4 E4B as the judge (60 stories per version, City and Monastery,
random and evolved storytellers, each judged twice, with the same digest): engagement
7.20 -> 7.23, payoff 6.72 -> 7.11, stakes 6.32 -> 6.52, coherence 7.85 -> 7.98,
exploration 7.51 -> 7.60; the monastery's stories rose (random storytellers 6.98 ->
7.32), the city's random ones dipped within the judge's session-to-session noise
(7.55 -> 7.27, 7.43 when judged again). Letting the judge see the beats a player sees
raised every score (engagement about 6.0 -> 7.2).

#### Added
- Twists matched to what they do, mystery twists that pay off later (`[TWIST]`,
  `unexpected_events.twist_payoff_delay`), and bargains as choices with a price
  that comes due (`bargain` effects).
- Callbacks: a payoff that closes a related clue says what it pays off (`[CALLBACK]`).
- Failing forward (`director.fail_forward`): a failure that led nowhere may start a
  complication, an event tagged `complication`; the city's *The guards come running*
  (hide, bluff, slip into the Sewers) follows a jammed safe or a botched bribe.
- Quest momentum (`mission_settings.momentum_after`), a dead-end travel penalty
  (`director.dead_end_penalty`), first-journey views (`travel_first`) and a victory
  epilogue of the journey (`end_victory`).

#### Changed (epilogues)
- Both epilogues tell the journey (the road, the quest's steps, the friends made, the
  turns of fate) instead of counting places, people and items; `{places}`, `{people}`
  and `{items}` stay available to custom messages.
- The judge's digest shows the story beats the player saw and the choices passed up;
  every turn of the turn log keeps its best alternatives (`options`).

#### Changed
- The bandit ambush strikes at most 3 times per story, every 8 turns at most, and
  its losses tell something (who buys the loot, who the guards are paid by).

#### Fixed
- Procedural twists told one thing and did another ("offers a dangerous bargain",
  then +8 coins), and their mysteries were never followed up.
- Someone already in view was "noticed" again, and an item already seen "spotted".
- An event could offer to slip away to the place one is in, and a move to the
  current place entered it into the path again.

### Alternative, unpredictable stories; Gemma 4 as a game critic

#### Added
- The director's rhythm (`director`): the storyteller now and then takes an option
  nearly as good as its favourite (`surprise`, from the story's own fixed randomness,
  never its dice, so a seed still gives one story); after `boredom_turns` calm turns, what brings something new
  ranks higher and unexpected events are likelier; clues open for `payoff_delay`
  turns want their payoff; places not visited pull harder the longer since the last
  new one (`explore_weight`).
- Fitness `rhythm` terms (predictability, variety of play, events lived,
  exploration, open threads) and `evolution.alternatives_weight` with
  `fitness.alternatives`: storytellers whose stories differ from one another score
  higher.
- `evaluator/game_judge.py`: Gemma 4 as a game critic (LLM-as-a-judge) scoring a
  playthrough on agency, pacing, surprise, payoff, exploration, stakes, coherence
  and replayability, with engagement, verdict and best/weakest moment; `llm_judge`
  settings; `POST /v1/records/stories/{id}/judge`, `POST /v1/sessions/{id}/judge`;
  `python -m evaluator.game_judge STORY_ID`.
- `evaluator/judge_benchmark.py`: play stories with the current engine (or a recorded
  one), judge each twice, and report engagement, criteria, the fitness's agreement
  with the judge and the judge's test-retest difference.
- Failing forward, after the judge's most frequent complaint (the same failed check
  tried again and again): every failed attempt eases the next (`director.retry_bonus`),
  a just-failed action steps back while there is something else to do there
  (`retry_cooldown`), travelling straight back to the place just left costs
  (`ping_pong_penalty`), and the fitness counts such retries (`rhythm.repeated_failures`).
- Tests for the rhythm, the alternatives, the judge and the benchmark
  (`tests/test_game_judge.py`).

#### Changed
- The model is Gemma 4 E4B (`google/gemma-4-E4B-it`), with `vllm.max_model_len`
  8192 so that a judged playthrough fits.
- Fitness weights calibrated with the judge (Spearman of each term with the judged
  engagement over 50 stories): volatility, scene contrast and combat successes count
  half as much; scenario duplication, loops, filler and fading novelty cost more;
  escalation counts less.
- The alternatives bonus belongs to the engine's own fitness
  (`fitness.rewards_alternatives`); a fitness passed in by a caller is left as it is.

#### Fixed
- Two threads loading the tag-embedding model at once (an API request and a story)
  could break the torch import or briefly fall back to other tag vectors; loading is
  now done once, under a lock (`utils/tag_similarity.py`).

### A graph to walk both ways, templates, objective losses, rules that hold

#### Added
- One bidirectional location graph kept with the game (`GameState.map_view`,
  play state `map`, the studio's map): no place leads to itself, places whose
  names differ only in case or spacing are refused, and the way back to the
  place just left is always a choice (`player_options.way_back`).
- Player-directed navigation (`navigation.player_directed: play`): in real play
  discoveries and hints open paths without moving the player, and lingering
  neither moves them on nor ends the story.
- Locked exits: `exit_locked` on a location, `unlock_exit` on an outcome
  (`[UNLOCKED] …`).
- Discoveries led by tags (`target_tags`), choosing among the neighbours on the
  map; `move_to_tags` also chooses a neighbour.
- Scenario templates (`world_definition.scenario_templates`, a location's
  `template` and `parameters`); the same template with the same parameters, the
  same content, or the same name twice is refused, also among generated places
  (`WorldAugmenter._new_scenarios`).
- Objective losses in the fitness (`fitness.objective_losses`): transition
  quality, tag coverage, named-transition dependence, scenario duplication and
  world extension, from the story's `transition_records` and `scenario_counts`.
- NPC goals that work (`npc_goals`): helping someone with their goal makes them
  an ally who shows the way to a place they know and whose checks are easier;
  goals may be mappings `{goal, tags, needed, reward}`; `base.npc_goal_bonus`.
- Explicit checks: the play state's `choices`, the scene's **Checks** and the
  studio's choice labels show each option's check and chance.
- Named threads: `setup_id`, `requires_setup`, `payoff_id`; and
  `story_rules.gate_payoffs`, which tells a payoff only when a planted clue waits
  for it.
- `return_to_previous` on outcomes (`[BACK] …`): the bandit ambush's *Make a run
  for it* flees back the way the player came.
- `tests/test_gameplay.py` for all of the above.

#### Changed
- Power counts in fights: every point gained since the story began (weapons,
  armour, health) adds `power_model.check_weight` to fight checks.
- Unchecked outcomes succeed by their own `success_prob`; the storyteller's
  traits no longer tip them unseen (`outcome_resolution.hidden_adjustments`).
- An NPC's `encounter_event` must name an event with actions.

#### Fixed
- `move_to_tags` written in the configuration was dropped by the builder.

### The language model: Gemma 4 on vLLM

#### Changed
- Story evaluation, world augmentation and the editor's suggestions share one
  Gemma 4 runtime (`google/gemma-4-E2B-it`) on vLLM (`utils/llm_model_loader.py`,
  `vllm` in the configuration), loaded once: `embedded` in this process, or
  `server` against an OpenAI-compatible `vllm serve`. Text only
  (`limit_mm_per_prompt`), `max_model_len` 4096.
- Evolution's worker processes are spawned instead of forked while an embedded
  vLLM engine holds a GPU context, and receive the active configuration and tag
  vectors, so they score storytellers exactly as before.
- `requirements.txt` no longer installs `accelerate` and `bitsandbytes`; vLLM is
  installed by `environment.yml` (the Docker image uses server mode).

#### Removed
- The Hugging Face loader with its Llama model and public fallback
  (`llm_evaluator.model_name`, `llm_fallback`).

### A map that holds, relevant choices, quests to play

#### Added
- `world_definition.routes`: a world's map (which places border which, both
  ways), written for the city. Every move follows the map: a far destination is
  reached through the places between, one turn each (`route_via`), and the story
  heads on toward it (`action_selection.heading_weight`, `heading_turns`). A far
  place revealed opens the way toward it, told once (`lead_via`). An NPC's
  action that would take the player where this place does not lead is not
  offered here. Without routes, each story's own map (story map plus the places
  a location's actions lead to) is the map; a world with no spawn rules has none.
- `location_constraints.<place>.entry: sent` (the Dungeon): a place one only
  arrives at by being sent, never a travel option, discovery or stop on the way.
- Relevant choices (`GameState.player_options`, `player_options`: `min`,
  `relevance`, `focus_relevance`, `travel_limit`, `talk_limit`, `focus_limit`,
  `tense_tags`, `relevance_filter`): what the moment is about first, then the
  rest unless they rank among the lowest; fewer in tense moments
  (`GameState.tension()`). The studio, the API and the offline engine all use it.
- Quests as playable content: an optional `mission` (key or title) on every
  start of a game in the API and the studio, `GET /v1/missions/playable`, and the
  studio's quest picker, *Quests in this world*, quest log and people journal.
- Play state `quest` (`GameState.quest_log`: stages done, current, to come, and
  what is still to do in words), `people` (`people_here`: description, wants,
  hostile, can help with the quest, times spoken with), `people_met`
  (`people_met`: where each was met) and `ways_on` (`ways_on`); the scene shows
  the quest line, the people with who they are and the ways on. The offline
  engine's `view()` has them too.
- Tests for the map, the choices, quests and people.

#### Changed
- `player_options.limit` 6 (was 8): the most choices ever shown.
- The city's map has secret ways the content describes: the Sewers to the
  Palace basement, the Drowned Archive's current to the Bone Orchard, the
  ghostline to the Palace and the Storm Observatory.

#### Fixed
- A move, a discovery or an NPC could carry the player between places that do
  not border each other: 630 of 1,356 moves in 40 city stories left the story's
  own map, and on the city's map 140 of 512 moves of the storyteller and 385 of
  992 of a random player jumped (the Dungeon opened onto the Palace). Now none
  do; in the example worlds only the way an arrest opened is walked back.
- A failed attempt to take an item (or an arrest for it) still gave the item.
- A hostile NPC gave directions right after an ambush.
- Engine models did not carry the vectors of the tags the engine adds itself
  (`milestone` when a quest stage completes, `finale`), so a loaded or exported
  engine told such stories slightly differently from the one that was trained
  (training continued in another process diverged).

### Quests, hints and threads; worlds in files

#### Added
- Missions in stages (`stages: [{title, description, goal, rewards}]`):
  completed in order with a `[QUEST]` message (`stage_complete`), the story
  pursues the current stage, `mission_status` names it, and the score counts
  each stage reached (`fitness.base.stage_bonus`). The city's Expose the fence,
  Break into the Vault and Buy your way into the Palace are staged.
- `until` on actions and outcomes: no longer offered or told once the
  condition holds. Numbered clues (`setup_N`) are not told once `payoff_N` has
  been; an action that could only tell such clues is not offered.
- After a failed attempt, its success destinations are barred from the
  engine's own moves for `director.barred_turns`.
- `director.close_threads`: as the story escalates, setups are worth less and
  payoffs more; no new clue in the finale.
- World-only configuration files: `store.complete_config` fills every tuning
  section a file lacks from the built-in configuration (also used by the
  artifact import); the upload message names them; places the tuning names
  must be the world's own.
- `examples/worlds/`: a frozen monastery and an orbital station.
- Prisons: `director.escape_bonus` (each failed escape makes the next easier)
  and `director.release_after` (a prisoner still locked in is released with a
  `released` message instead of the story ending as stalled).
- Tests for stages, hints, barred destinations, thread closure and the
  example worlds.

#### Changed
- Pacing: `mission_settings.pursuit_start_step` 18 (was 12),
  `pursuit_full_step` 40 (was 30), `fitness.base.mission_win_min_steps` 25
  (was 20): stories run about 28 turns instead of 21 with as many wins.

#### Fixed
- Configuration errors listed "…and -19 more problem(s)" when there were fewer
  than 20.

### Coherent and unique storyworlds

An audit of 390 generated stories found narration, character and content
errors in most stories; they are fixed, and each story now has its own map.

#### Added
- `world_definition.story_map` (`enabled`, `neighbors`): every story draws its
  own connected geography from the spawn rules (`GameState.story_map`,
  `spawn_options`); discovery, route planning and mission choice follow it.
- NPC `article` (`""` for a proper name); `GameState._called` names characters
  and items with their article.
- Coherence rules: escape actions only for a locked player (and retried until
  they succeed); no generic small talk with hostile NPCs; repeated
  conversations count toward stalling; leads never point to the current place
  or back where the turn started; actions whose successes only bring the player
  where they are are not offered; variable goals are pursued only through
  actions that move the value the right way (`GameState._moves_variable`);
  no NPC lead after a failure outcome; a way to move on (travel, an escape)
  is enough when the director hides loops; route planning follows the routes
  written into location actions (`move_to`, `reveal_location`).
- Content: descriptions and actions for the Guard and the Gardener (ways to
  raise reputation, and leads); actions for Gold Coins, the Antique Statue and
  the Rusty Sword (now a weapon).
- Tests for every rule and content fix.

#### Changed
- Messages: `arrival_npc` is "Met {npc}: {description}" and `arrival_item`
  "Nearby you see {item}: {description}" (the names carry their article); the
  `[LOCATION] Current location` line after an outcome is gone (the move is told
  as `Transferred from … to …` or `Still at …`).
- Content: the three ways past the Gate Guard are not offered where they lead
  (the Palace, the Vault); a failed coin bribe no longer moves the player into
  the Vault; Aurora's crown hint ends once the safe is opened.

### A story that develops like a game

Measured problems (348 games played through the API): training did not make
stories more engaging because the score rewarded length; novelty faded late in
a story; up to 60% of stories were cut off by the action limit; no player ever
died; following the first option won almost every game; missions were badly
unbalanced.

#### Added
- The director (`director` in `hyperparameters.yaml`, `core_engine/game_state.py`):
  escalation over the story (events, threats, damage), a `[FINALE]` before the
  action limit with full goal pursuit, an epilogue ending (`end_kind: epilogue`)
  instead of a cut-off, and no options that can only repeat themselves.
- Engagement terms in the score (`fitness.engagement`): filler turns, loops,
  fading novelty, and rising tension, from the story's turn log.
- `fitness.length_normalization.mode: rate`: the terms that grow with every
  turn (arc, contrast, combat, volatility, interactions, discovery, tone) count
  per turn; `floor_steps`. `fitness.base.action_limit_penalty`.
- Learning: `evolution.benchmark_top_k` (champions chosen on the benchmark
  among the best of a generation, and kept as elites), `cover_situations`,
  `min_diversity` and `max_immigrant_rate` (diversity kept), and children
  identical to another are mutated again.
- Per-profile mission history (`mission_selection`): recent missions are
  drawn less often. `VariableStore.recent_missions`, `record_mission`.
- `player_options` (`limit`, `order`): players see the storyteller's best
  options in the scene's order, not best first
  (`GameState.ranked_available_actions(order="world")`).
- `skill_check_resolution.failure_damage` and `dangerous_failure_damage`;
  the director's `damage_scale` and `damage_escalation`.
- `python main.py --import-artifacts [FOLDER]` (`sessions.import_artifacts`,
  `records.import_engine`): engine models, exports, bundles and story
  configurations become recorded engines (completed: missing tuning, tag
  vectors, benchmark score, rebuilt runs and generations, a preview story),
  duplicates are linked, and the files are moved into `imports/<kind>/`.
- Tests: director, engagement score, reliable learning, mission history,
  shipped content, and the artifact import.

#### Changed
- Fitness: `mission_win_bonus` 80 (was 60), `stall_penalty` 30 (was 20),
  `max_action_completion_bonus` 0 (was 8, reaching the limit is not an ending).
- Skill checks get harder over a story: `difficulty_rate_per_step` 0.3 from
  step 15 (was 0).
- Content: Break into the Vault is won by cracking its main safe (was: just
  arriving); a failed interrogation or fight no longer removes the thief (the
  failed fight used to win "Rid the market of the thief"), and interrogation
  has no penalty; the Gate Guard's bribe reveals the Heavy Shield.
- Generation statistics: `best_traits` is the benchmark champion's;
  `best`/`worst` are the generation's highest and lowest scores.
- Tests write the API's downloads to a temporary folder (`ANE_ARTIFACT_DIR`
  in `tests/records_sandbox.py`), no longer into `artifacts/`.

#### Fixed
- An item placeholder in an action name showed the item's description
  ("Give him the The labeling is in a prestigious script; a perfect bribe."):
  it is the item's name.
- Bandits ambushed travelers inside the Vault; the Gate Guard "let you pass"
  when you were already inside it.

### Studio on the REST API

#### Changed
- The studio (`app.py`) is a client of the REST API: every tab works through
  the API's endpoints and `app.py` uses none of the backend code
  (`backend/`, `core_engine/`, `utils/`; checked in the tests). The tabs,
  controls, labels, and behaviour are unchanged.
- `StudioAPI` (top of `app.py`) connects the studio: HTTP to `ANE_API_URL`, or
  the same API inside the studio's process when it is not set. Requests carry
  `X-ANE-Client: studio`; API refusals show as studio error messages; files
  the API offers are downloaded for the file widgets.
- Evolution, continued and retrained training run as background jobs that the
  studio polls, so the progress bar still moves per generation.
- Compose: the `ui` service sets `ANE_API_URL=http://hamlet-ane-api:8004`
  (`ANE_STUDIO_API_URL` overrides it; empty serves the API inside the studio)
  and starts after the API is healthy.
- `POST /v1/engines/load` also takes `parameters` (a model-parameters YAML
  for a configuration without `generated_run.genome`) and `stats` (the
  player's own points as JSON), as the studio's *Load a previous best engine*
  does. An upload rejected there no longer leaves the engine upload behind.
- Runs, engines, and stories started through the API are recorded with the
  origin `api`, and with `studio` when the request says it comes from the
  studio.
- Larger runs: up to 400 candidate storytellers, 1000 generations, and 400
  steps per simulation (before: 50, 25, 100), in the API (simulations, jobs,
  continue, retrain) and the studio's slider limits (new defaults of
  `candidate_limit`, `generation_limit`, `simulation_step_limit`).
  `RUN_LIMITS` in `backend/store.py` holds the maximums; app parameters above
  them are rejected. `max_simulation_work` still caps the total work of a run.
- Fewer files: `studio_client.py` is part of `app.py`, `ui_authoring.py` (the
  app parameters) is part of `backend/store.py`, the background jobs are part
  of `backend/sessions.py`, and `tests/test_ui_authoring.py` is part of
  `tests/test_studio_api.py`. The changelog moved to `docs/CHANGELOG.md`.
- `POST /v1/sessions/{id}/actions` answers `404` for an unknown session, like
  `GET /v1/sessions/{id}` (before: `400`).
- Playing, continuing, or retraining an unknown recorded engine answers `404`
  before any work starts, like `GET /v1/records/engines/{id}` (before: `400`,
  and the continue and retrain jobs were queued and then failed).
- CORS (`ANE_CORS_ORIGINS`) also allows the `X-ANE-Client` header, so browser
  clients can say who they are.
- `docs/API.md`: **How the API behaves**: what lives in memory and on disk,
  how writes are checked and saved, status codes, run limits and work, play,
  records, files and CORS.

#### Added
- `GET /v1/reference`: entry kinds, suggested tags, action reference, repeat
  rules, goal keys, rule choices and fields, stat choices, documents, and run
  defaults.
- `GET|PUT /v1/app-settings`: the studio's app parameters.
- `GET /v1/documents` and `GET|PUT /v1/documents/{name}`: missions, messages,
  variables, stats, and a profile's stored values as YAML text, comments kept.
- `POST /v1/variables/reset/yaml`.
- `GET|PUT /v1/entities/{kind}/{name}/actions/yaml`,
  `.../policy/yaml`, and `.../actions/{index}/repetition`.
- `GET /v1/entities/{kind}?fields=names`.
- Background jobs (`backend/sessions.py`) and `POST /v1/jobs/simulations`,
  `POST /v1/jobs/engines/{id}/continue|retrain`, `GET /v1/jobs`,
  `GET /v1/jobs/{id}`.
- Tests for the new endpoints, the studio's use of the API (in-process and
  over HTTP against a running server), and refusals shown in the studio.

### Records database and saved engine models

#### Added
- `backend/records.py`: an SQLite database in `/data/angelisg/ane`
  (`ANE_RECORDS_DIR`, `ANE_RECORDS=off`) with the tables `worlds`, `runs`,
  `generations`, `engines`, `stories`, `turns`, and `artifacts`. The studio,
  the REST API, and the terminal record every run (settings, parameters,
  outcome, learning), every generation and its champion, every engine (with
  its model file), every story (parameters, state, result, fitness,
  transcript), and every turn of every story. Recording is best effort and
  never interrupts play.
- `core_engine/model.py`: engine model files (`.ane.zip`) with the world and
  tuning, the storyteller, the complete training state, the engine's memory,
  and its tag vectors, checked by SHA-256 on load. `EngineModel.load`, `save`,
  `build_world`, `new_game`, `world_yaml`, `continue_training` (exact),
  `retrain`.
- `NarrativeEvolution` checkpoints (`last_checkpoint`, `checkpoint=` callback,
  `run(resume=...)`, `from_checkpoint`) and `ModelMemory`; resuming continues
  exactly as an uninterrupted run.
- `GameState.turn_log` and `turn_text`: one entry per turn.
- REST API: `/v1/records/...` (runs, worlds, engines, model files, stories)
  and the engine operations play, continue, and retrain; `engine_id` in
  simulation responses; `source: engine` exports.
- Studio: **Simulate & play → Records** (play, continue, retrain, download a
  recorded engine and its world, read recorded stories, list runs).
- Terminal: every run is recorded and saves `engine_model.ane.zip` in its
  archive; `python main.py --continue-training | --retrain | --play |
  --export-world ENGINE` and `--list-engines`.
- Offline exports carry the training state: `run.py evolve --resume`.
- `tests/test_records_and_models.py` (15 tests) and
  `tests/records_sandbox.py` (tests record into a temporary folder).

#### Changed
- The engine download (studio, API) is the engine model file; it remains a
  valid older bundle.
- Loading an engine model activates its own world and tag vectors.
- `compose.yaml` mounts the records folder.

#### Fixed
- Without a seed, the validation benchmark was drawn again on every call, so
  it was not fixed and consumed the evolution's random numbers.

### Story rules and the evolution method

Measured on 200 stories with an evolved storyteller; each finding has a rule,
a fitness loss, or a change to how storytellers are evaluated.

#### Added
- `core_engine/rules.py` and the `story_rules` settings: `failure_tags`,
  `follow_outcome_text`, `early_goal_avoidance`, `on_stall`,
  `repetition_power`, `backtrack_penalty`, `tie_epsilon`, `collect_weight`,
  `payoff_weight`, `payoff_route_weight`, `max_open_clues`, `clue_matching`.
- Procedural events: `coin_budget`, `effect_repeat_penalty`, `name_location`;
  no omen, actor, or twist is repeated in a story while unused ones remain.
- Fitness losses: `repetition` (action repeats, A → B → A loops), `pacing`
  (early wins), `base.unfinished_penalty`, and `length_normalization` of the
  terms that grow with every step.
- Evolution: `full_length_stories`, `stratify_episodes` (every storyteller
  plays the same start × mission situations), `score_aggregate`
  (`mean_minus_std`) and `consistency_weight`; `NarrativeEvolution.benchmark_task`,
  also used by the offline engine's `evolve`.
- The story message `stall_move`.
- `tests/test_story_rules.py` (16 tests).

#### Fixed
- A failed check no longer tells a success outcome, a passed one no longer
  tells a failure outcome, and an arrest no longer prints (or applies) the
  success it interrupted: 58 contradictions in 917 checks → 0 in 1,216.
- A discovery or NPC lead opens the place the outcome's text names.
- Procedural events no longer hand out the same effect every time (all 932
  were "coins +8"; now they rotate, with at most 10 free coins a story).
- Clues and payoffs were never recorded, so the setup-and-payoff fitness term
  was always 0; they are now recorded and matched (53% of stories pay one off).
- Stories no longer end "stalled" when the traveler can move on (9.5% → 0%).
- Near-ties are no longer broken by action name (21% of turns).

#### Changed
- Outcome choice: the storyteller attempts a success outcome when a skill
  check decides, and the check picks the branch that is told.
- `evolution.max_steps` 60 → 100, the studio and API step limit 60 → 100, and
  `max_simulation_work` 20,000 → 60,000 (whole stories are counted at the
  action limit). Procedural events: `max_per_story` 5 → 3, `cooldown_steps`
  2 → 4.
- Content: "Crack the main safe" and "Attempt to infiltrate" are
  `repeat: once` with `retry_on_failure`, with their failure outcomes tagged
  `failure` and their successes tagged `payoff_clue`; the bandit ambush has a
  6-turn `cooldown`.
- Tests: the novelty test follows `story_rules.repetition_power`.

### Retry after failure

#### Added
- `retry_on_failure` on actions: a failed attempt uses nothing up, so a
  `once` action disappears only after it succeeds, and a `per_visit` action
  stays on offer in the visit until it succeeds. Implemented in `Action`
  (with a class default, so older saved games load), `_build_action`,
  `ACTION_FIELDS` and validation (`repetition_errors`: a boolean, and not
  with `repeat: always`; also checked for uploads), and
  `GameState._record_choice`.
- Studio: **How often one action is offered** in the actions editor, with the
  **Repeat** choice and a **Retry after failure** checkbox per action
  (validated before anything is written).
- `tests/test_retry.py` (7 tests): success and failure under both settings for
  `once` and `per_visit`, including leaving and revisiting the location and a
  save/restore in the runtime's format; retrying until success; exclusive
  groups; older saves; validation; the editor checkbox.

### Event rules, random scheduling, and unique items

#### Added
- Event start rules: `repeat` (once, per_location, per_visit, always),
  `max_occurrences`, `cooldown`, and `trigger: random` with `chance` (per turn)
  and `weight`, next to `enabled_if`. `GameState._event_blocked` checks them and
  `_enter_event` enforces them for every way into an event (trigger actions,
  NPC encounters, chains, `start_event` effects, the random schedule). Blocked
  events hide their trigger and chaining options, and encounters wait.
- Authored random scheduling: once per turn, each random event that may start
  rolls its `chance` with the session RNG, and `weight` picks between events
  that hit together. The procedural `unexpected_events` are unchanged.
- The event log records the `visit`, the `cause`, the `result` (`success`,
  `failure`, `abandoned` for the fallback option), and `ended_step`.
- Event goals that distinguish success: the `event_succeeded` goal key and the
  conditions `event_succeeded` and `event_failed`. `event` / `event_done` keep
  their meaning (resolved, any result).
- Unique items: `unique: true` and an optional `location` on items. Each
  unique item gets one `ItemInstance` per story (placed, held, gone,
  unplaced), separate from its definition; it is shown only where it lies,
  leaves the world when collected or received, and is gone for good once used
  or taken. `GameState.item_log` records every item's history, independent of
  the inventory, and the condition `item_collected` reads it.
- Policy editing: `GET|PUT /v1/entities/{Events|Items}/{name}/policy` and the
  studio's **Uniqueness & scheduling** editor, validated before writing.
  `builder.logic_errors` also checks event rules and item uniqueness, for
  world builds and uploads.
- Shipped content: the Fence's Ledger is unique and lies in the Sewers (its
  tags alone also fit two other locations); the Memory Thief's event is
  `repeat: once`.
- `tests/test_event_rules_and_items.py` (19 tests): a unique event cannot be
  repeated through another trigger action, a direct call, an effect, a chain,
  or an encounter; a disabled event cannot start in any of those ways, and an
  action that disables the next event stops its chain; per-visit and
  per-location repeats, occurrences, and cooldowns; random scheduling (one
  roll per turn, frequency close to `chance`, `weight`, cooldown); event goals
  that need success; unique item placement, collection, use, and history; the
  shipped ledger; validation, and the policy editor.

#### Changed
- Generic and unique items share one scene ranking (`_scene_objects`), used by
  scenes and by the route planner's item sites. Worlds without unique items
  rank items as before.
- The event fallback action is marked (`source: event_fallback`) so stepping
  away resolves an event as `abandoned`.
- Worlds that use none of the new fields play exactly as before (40 seeded
  stories compared word for word with the original engine).

### Shared conditions and effects

#### Added
- `core_engine/conditions.py`: one condition tree for the whole world.
  `all` / `any` / `not` combine leaves on persistent variables, character
  stats, coins / health / turn, items held, places visited or current,
  removed NPCs, resolved events, successful actions, outcome tags, and the
  mission. One evaluator (`holds`) and one validator check structure,
  operators, references, and operand types (e.g. no `gte` on a `bool`, `in`
  needs a list of the variable's type).
- `core_engine/effects.py`: one effect list (`set_variable`,
  `change_variable`, `set_stat`, `change_stat`, `give_item`, `take_item`,
  `change_coins`, `change_health`, `remove_npc`, `reveal_npc`, `reveal_item`,
  `reveal_location`, `unlock_location`, `move_to`, `send_to`, `start_event`,
  `complete_mission`, `end_story`) with validation of references, value
  types, and writable fields (`playthroughs` is read-only).
  `GameState.apply_effects()` validates the whole list before the first
  effect, so a bad list changes nothing.
- `enabled_if` on events (triggers hidden, encounters wait, no chaining into
  them) and on actions. NPC `dialogue` lines chosen by condition, with
  effects. Outcome `effects`, mission `rewards`, and the `condition` goal key.
- `builder.logic_errors()` checks every condition and effect list of a world;
  `build_world` and combined-config uploads reject an invalid world before
  building or activating anything.
- Shipped content: the Priest's dialogue depends on the traveler's reputation
  and on holding the Fence's Ledger; ridding the market of the thief rewards
  +1 reputation and 10 coins.
- Studio and API: conditions as YAML trees in **Enabled if** (events
  included), action `enabled_if` and outcome `effects` in the actions editor,
  mission `rewards` and `goal.condition`.
- `tests/test_conditions_effects.py` (19 tests): one condition used as a
  location, NPC, item, action, event, dialogue line, and mission goal gives
  the same result in all 8 states it can be in, and follows changes during
  play; shorthand and trees agree; validation of operand types, references,
  and writable fields; failed validation leaves the state, the save file,
  the YAML files, and the active configuration unchanged.

#### Changed
- Every `enabled_if` and every mission goal goes through the shared
  evaluator. The variable shorthand (`{thief_caught: false, reputation:
  {gte: 2}}`) and the goal keys are kept as shorthand.
  `variables.check`, `validate_conditions`, and `validate_changes` delegate to
  the shared modules.
- An outcome's `set_variables`, `change_variables`, and `change_stats` are
  shorthand for effects and run through the same executor as `effects`,
  dialogue, and rewards. `apply()` validates them before changing anything.
  Item, health, reveal, and route changes of outcomes use the same helpers as
  their effects.
- The route planner also sees what effects write, and plans toward
  `goal.condition`. Its caches follow every change of the story in worlds
  whose locations, NPCs, or items depend on more than variables.
- Validation is stricter: operand types of shorthand conditions are checked
  (e.g. `{reputation: "high"}`), and variables cannot be named after
  condition keywords (`all`, `visited`, ...).
- With the original configuration files the engine produces the same stories
  word for word (checked on 40 seeded stories).

### Starting locations

#### Added
- Location fields `start_eligible` (default `false`) and `start_weight`
  (default 1, above 0). Each story starts at an eligible, enabled location,
  drawn with the session's random generator and weighted by `start_weight`.
  The shipped world can start at the Harbor (1.5), the Market (1), the Tavern
  (1), and the Festival of Masks (0.5, only while its `enabled_if` holds).
- `core_engine/starts.py` and the `start_selection` settings
  (`default_location`, `recent_starts`, `repeat_factor`). Each recent start of
  the profile multiplies that location's weight by `repeat_factor` (0.25), so
  repeats are discouraged but never forbidden.
- Per-profile start history: real play records each story's start with the
  profile's persistent variables (`starts`, the latest 20).
  `VariableStore.recent_starts()` and `record_start()` read and write it.
  Simulations never write it.
- Mission viability: the mission is drawn among the missions some start can
  reach, then the start among the locations from which that mission can be
  reached (goal not already met, a known or discoverable route to a goal site).
- `NarrativeEngine.new_game(start=...)`, `starting_locations()`, and
  `info()["starting_locations"]`. The HTTP server's `POST /games` takes
  `start`, and `python run.py play` takes `--start`. Views and REST play states
  include `start_location`.
- Studio and entity API: **Stories can start here** and **Start weight** for
  locations (`start_eligible`, `start_weight`), and `start_selection` in
  **Engine logic**. Uploads and world builds reject invalid values.
- `tests/test_starts.py` (23 tests): ineligible and disabled locations never
  start, several starts across seeded sessions in proportion to their
  weights, the reduced probability of recent starts (exact weights and
  seeded frequencies), per-profile history, mission viability, validation,
  and authoring. `tests/test_export.py` covers choosing the start offline and
  over HTTP.

#### Changed
- `GameState(start=None)` draws the start; it used to default to `"Harbor"`.
  An explicit `start` is still honoured (any enabled location), and an
  unknown one now raises `ValueError`. Worlds without `start_eligible`
  locations keep the single `start_selection.default_location` start and
  draw no random number for it.
- The route planner's search is split into `_route_distances()`, which mission
  viability uses without the per-visit "goal actions used up" adjustment of
  `_goal_distances()`.
- Tests: the dice test pins its start as it already pinned its mission. The
  HTTP test checks that a turn echoes the chosen action, since an unexpected
  event may come first. Offline training runs on a copy of the export, so an
  adopted storyteller no longer changes the engine the other tests play with.

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
