# Changelog

All notable changes to Adaptive Narrative Enabler are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Add new entries
under **Unreleased**, in the section of the area they touch, with the commit
that introduces them.

## [Unreleased]

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
