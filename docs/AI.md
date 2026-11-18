# The storyteller AI

How the engine makes a story: the evolutionary AI that learns a storyteller, the decisions
the storyteller takes, the director that builds a story, sessions, story beats and the rules
that keep a story coherent. Authoring the world it plays is in [WORLD.md](WORLD.md).

## Execution Flow

1. `utils/config_loader.py` deep-merges all YAML files under `config/`.
2. `main.py` optionally opens the editor or loads a user-provided world YAML path.
3. Optional augmentation can expand the world before evolution.
4. `build_world(genome)` turns YAML into runtime `Location`, `Action`, `Outcome`, `Character`, and `Object` instances.
5. `evolve()` scores genomes over simulated episodes using multiple worker processes.
6. The best genome is used for a final detailed story simulation.
7. Optional LLM evaluation scores the final transcript.
8. Artifacts are saved in a timestamped folder under `runs/`.

## The evolutionary AI

Every story the tool creates comes from evolution
([`core_engine/evolution.py`](../core_engine/evolution.py)). The terminal
(`python main.py`), the studio, and the REST API all use the same
`NarrativeEvolution` engine.

A **genome** is a storyteller: 19 traits such as appetite for danger,
curiosity, drive toward the mission, and avoiding repetition. Each generation:

1. Every storyteller tells `episodes_per_genome` stories, each played by a
   **simulated player** ([who controls what](#who-controls-what-the-director-and-the-player)).
   All storytellers of a generation tell the **same** stories: the same seeds and, with
   `stratify_episodes`, the same **situations** (a starting location and a
   mission that can be reached from it). The generations cycle through every
   start × mission pair, so a storyteller is judged on the whole world, not
   on a few lucky draws. With `full_length_stories` every story is played to
   its natural end (`narrative_end.max_actions`), as in a real game, so the
   fitness judges real endings instead of stories cut at `max_steps`.
2. The **fitness function** (`fitness` in `hyperparameters.yaml`) is the
   **training objective**: operational measures of dramatic arc, contrast,
   setup and payoff, variety, tension, mission wins, and more. It is not a
   measure of narrative quality ([what fitness is, and what it is
   not](#what-fitness-is-and-what-it-is-not)). It also has **losses** for what makes a
   story worse: `repetition` (an action used more than `free_uses` times, and
   A → B → A travel loops), `pacing` (a mission won before
   `mission_win_min_steps`), `base.unfinished_penalty` (a story cut before its
   end), `base.action_limit_penalty` (a story cut off by the action limit
   instead of ending), and the existing death, stall, and Dungeon penalties.
   **Engagement** (`fitness.engagement`, from the story's turn log) scores
   what a player feels turn by turn: the share of *filler* turns (no new place,
   person, event, outcome, check, or change of health or coins), *loops* (an
   action repeated with the same outcome within `loop_window` turns), *fading
   novelty* (fewer new turns in the last third than in the first), and
   *escalation* (rising tension: damage, events, failed checks). Terms that
   grow with every step (`length_normalization.terms`: arc, contrast, combat,
   volatility, interactions, discovery, tone) count **per turn**
   (`mode: rate`, scaled to `story_length.target`; a story shorter than
   `floor_steps` counts as that long), so a longer story is not better merely
   for being longer and a short won story is not worse. **Objective losses**
   (`fitness.objective_losses`, `core_engine/fitness.objective_losses`) subtract
   what a story lacks: `transition_quality` (moves between places that fit each
   other: tag similarity below `target_similarity`), `tag_coverage` (too little of
   the world's tags shown), `named_transition_dependence` (discoveries and engine
   moves that only follow an authored name rather than tags, a lead or the map),
   `scenario_duplication` (the same place, action and outcome told again) and
   `world_extension` (too little of the world discovered, or a known map without
   branches). Each shows in the breakdown as `loss_<name>`, and every NPC whose
   goal the story fulfilled adds `base.npc_goal_bonus`. **Rhythm**
   (`fitness.rhythm`) rewards what makes a story worth playing turn after turn:
   `predictability` costs every calm turn in a row beyond `calm_allowed`,
   `variety_of_play` counts the kinds of development the story offers (new
   place, event, person, outcome, check, fight, hurt, gain, discovery, payoff),
   `events_lived` the distinct events, `exploration` the distinct places, and
   `open_threads` costs every clue still open at the end. **Alternatives**
   (`evolution.alternatives_weight` x `fitness.alternatives`, 0..1): a
   storyteller whose stories differ from one another (places, events, people and
   outcomes; mean Jaccard distance) scores higher than one that tells the same
   story whatever the start. A storyteller's score over its stories is
   `score_aggregate`: `mean_minus_std` subtracts `consistency_weight` × the
   standard deviation, so a storyteller that tells consistently good stories
   beats one that alternates great and broken ones.
3. The `benchmark_top_k` best storytellers of the generation also play a
   fixed **validation benchmark** (`validation_episodes` stories, the same in
   every generation; with `cover_situations`, at least one per start × mission
   situation). The best of them **on the benchmark** is the generation's
   champion and leads the elites, so a storyteller that was merely lucky on
   this generation's stories is not crowned. The overall best storyteller is
   chosen on this comparable score.
4. The next generation is bred from the scores: `elitism_rate` keeps the
   best, parents come from tournament selection (`tournament_size`),
   `crossover_rate` blends them, `mutation_rate`/`mutation_step` mutate them,
   and `random_immigrant_rate` adds fresh storytellers. When the population's
   diversity falls below `min_diversity`, up to `max_immigrant_rate` newcomers
   and a wider mutation keep it from collapsing onto one storyteller, and a
   child identical to another is mutated again.
5. **Adaptive mutation.** After `stagnation_generations` without benchmark
   improvement, mutation widens up to `max_mutation_step` to escape the
   plateau, and it narrows again after an improvement. `early_stop_generations`
   stops a run that has stopped learning.

**Learning across runs.** The champions and their benchmark scores go into a
learning memory: `saves/evolution_memory.json`, keeping `hall_of_fame_size`
of them. With `warm_start`, the next run starts from the best remembered
storytellers, so the tool keeps improving from run to run. In the studio,
**Continue learning from earlier runs** switches this on or off, and **Forget
what the AI learned** resets it. The API offers `GET|DELETE /v1/learning`.

**What the AI learned is shown.** The studio's play tab draws the learning
curve: the benchmark champion, the best, and the average per generation. It
also lists the evolved storyteller's traits next to the population average
and bounds, with a plain meaning and whether the trait is high, moderate, or
low. The API returns the same data as `learning` on `POST /v1/simulations`
(history, `learned_traits`, `best_genome`, improvement, warm start). Bundles
store it too.

Measured on 20 held-out stories that evolution never played (16 storytellers,
8 generations, 4 stories each):

| | Held-out score |
| --- | --- |
| Random storytellers, average | 631 |
| Random storytellers, best of 10 | 718 |
| Evolved, first run | 750 |
| Evolved, second run (from memory) | 869 |
| Evolved, third run (from memory) | 893 |

**Resisting reward hacking.** Talking to the same NPC again and again used to
earn points without limit. `fitness.npc_interaction_cap` (3) stops that, so
evolution looks for better stories instead of farming conversations. The
setup-and-payoff term used to stay at 0 because clues were never recorded;
they are now recorded and matched as the storyteller matches them
(`story_rules.clue_matching`), so closing threads is actually rewarded.

Settings (`evolution` in `hyperparameters.yaml`, or **Engine logic →
evolution**):

```yaml
evolution:
  pop_size: 400                 # storytellers per generation (studio: "Candidate genomes")
  generations: 200
  episodes_per_genome: 6        # stories per storyteller per generation (studio: "Stories per storyteller")
  max_steps: 100                # with full_length_stories, whole stories
  full_length_stories: true
  stratify_episodes: true
  score_aggregate: mean_minus_std
  consistency_weight: 0.25
  elitism_rate: 0.08
  crossover_rate: 0.75
  random_immigrant_rate: 0.08
  tournament_size: 3
  mutation_rate: 0.22
  mutation_step: 0.14
  max_mutation_step: 0.4
  stagnation_generations: 3
  early_stop_generations: 0     # 0: never stop early
  warm_start: true
  hall_of_fame_size: 12
  validation_episodes: 10
  cover_situations: true        # the benchmark covers every start x mission situation
  benchmark_top_k: 3            # champions are chosen on the benchmark among the 3 best
  min_diversity: 0.08           # below this: more immigrants...
  max_immigrant_rate: 0.3       # ...up to this share, and wider mutation
  evaluation_threads: auto      # processes playing stories at once (auto: the CPUs)...
  max_evaluation_processes: 16  # ...at most this many
  parallel_backend: processes
```

With a reproducibility seed, a run is fully reproducible: the same seed gives
the same storytellers, stories, and learning curve, in one process or many.

**Speed.** Every run (the studio, the API, the terminal) plays a generation's stories in a pool
of processes, one story per task, so all CPUs stay busy (the studio and the API start the
processes afresh and give them the world, which is safe in a server with threads). The
benchmark's stories are fixed, so a storyteller kept from the generation before (an elite)
keeps its benchmark score instead of playing them again. Tag similarity, similarity-based
spawn rules and the places where the quest can advance are cached. None of this changes a
score: a run gives the same result, only sooner.

## Who controls what: the director and the player

A story has two sides, and evolution must reward only one of them. The **storyteller** (a genome)
is the narrative controller, the director; the **player** chooses what to do.

| The director (the storyteller, evolved) | The player (a person, or a simulated player in training) |
| --- | --- |
| The world it builds: outcome odds (`success_bias`), guards, leads | Which of the choices shown to take |
| Who is where: which NPCs and items appear in a scene (`bias_*`, `spawn_prob`) | |
| When events and encounters come (`event_prob`, `chaos_bias`, `threat_prob`) | |
| How the story answers: setups and payoffs, stakes, failures, the climax (`setup_prob`, story beats) | |
| Which choices are shown, and in what order (`player_options`: the ranking by its traits, `voyage_bias`, `discovery_bias`, `goal_bias`, `novelty_bias`, ...) | |

Before, evolution let the storyteller also take the player's turns: a story told without a
player's choice took the storyteller's own favourite (`GameState._storyteller_pick`). A
storyteller could then score well partly because it chose well for the player, which does not
transfer to people, who choose for themselves. Now **simulated players**
([`core_engine/players.py`](../core_engine/players.py)) take the player's turns in every
evaluated story. They choose among the choices shown with only what a person sees (the choices
and their order, their kind, the skill check with its chance and cost, the quest log and who can
help with it, the places and people already met, health), never the storyteller's scores:

| Player | Chooses |
| --- | --- |
| `follower` | mostly one of the first choices shown (each next one about half as likely) |
| `explorer` | new places and new people first |
| `quester` | the quest's next step first: what the quest log still needs, people who can help |
| `cautious` | avoids likely failures, the more so when hurt; likes choices without a check |
| `random` | any choice shown |

`evolution.player_mix` sets how often each plays. A story seed always has the same player making
the same choices, so every storyteller meets the same players on the same stories and runs stay
reproducible. `evolution.player: storyteller` brings back the older evaluation, to compare with;
training states saved before simulated players continue with it, so they go on exactly. The
judge's benchmarks, previews and the terminal's stories are played the same way (in the terminal,
a person plays when `simulation.interactive` is on).

Measured on 12 random storytellers, 6 whole stories each: with the storyteller playing for
itself, mean fitness 758; with simulated players, 561, so about a quarter of the old score came
from playing well. The two evaluations rank the storytellers differently (Spearman 0.43): what
evolution selected was partly a player. Storytellers still differ as much with simulated players
(spread 94 against 92), so evolution keeps a signal to learn from. A turn costs about 20% more
(stories run longer: 58 turns against 48).

## What fitness is, and what it is not

Fitness is the **training objective**: what evolution maximises because it can be computed for
thousands of stories. Its terms (events, changes of health, tags covered, clues planted and
resolved, variety, pacing and coherence losses) are operational measures. They do not establish
engagement, causality or a satisfying payoff: a fine quiet scene can score low and a mechanically
busy story high. A higher fitness means a storyteller does more of what the measures count, not
that its stories are better.

Narrative quality is assessed apart from it:

- **The judge** ([LLM.md](LLM.md)): Gemma 4 reads stories as a player would and rates engagement
  and eight criteria; *6 · Evaluate with the judge* compares storytellers on the same seeds
  (pairwise, both orders, with a confidence interval) and measures how well fitness agrees with the
  judge. Fitting the fitness to the judge (`calibrate-fitness`) only aligns the objective better;
  it does not make fitness a measure of quality.
- **People playing**: the studio's play tab, the ANE AI in a game, and session reports
  (`core_engine/progression.py`) are where quality shows.
- **Judge in the loop** (`evolution.judge_weight`): the judge's engagement can be added to the
  best storytellers' scores during training, at the cost of reproducibility.

The studio reports fitness as "fitness on the benchmark (the training objective)", never as a
quality score.

## Genome-driven decisions and learning

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
Outcomes are chosen by the deterministic objective. The only dice of a
story are its skill checks.

## The director: how a story builds

Like a game's director, the engine shapes the course of a whole story
(`director` in `hyperparameters.yaml`, or **Engine logic → director**), so
that it rises toward an ending instead of wandering until it is cut off:

- **Escalation.** From `escalation_start` (a share of
  `narrative_end.max_actions`) the story escalates up to 1 at the limit:
  unexpected events become more likely (`event_pressure`), the storyteller is
  drawn to threats (`threat_weight`), and skill checks get harder over time
  (`skill_check_resolution.difficulty_rate_per_step`).
- **Stakes.** Every health loss is multiplied by `damage_scale` and grows with
  the escalation (`damage_escalation`); a failed action costs
  `skill_check_resolution.failure_damage`, a dangerous one (threats, combat,
  guards) `dangerous_failure_damage`. A careless traveler can die.
- **The finale.** `finale_actions` before the action limit, a `[FINALE]`
  announces that time is running out and the mission pulls at full strength
  (`finale_goal_pull`). A story that reaches the limit ends with an
  **epilogue** (`end_kind: epilogue`): what was achieved, what is left for
  another day, the places, people and items of the journey. Without the
  director it is cut off (`action_limit`), which the score penalises.
- **No loops.** An action used within `loop_window` turns whose outcomes have
  all been told already is not offered again (unless fewer than
  `min_options` options would remain); events and travel are always offered.
- **Alternative stories.** Now and then (`surprise` x (1 + the storyteller's
  `chaos_bias`)) the storyteller takes an option nearly as good as its favourite
  (within `surprise_margin`, among its `surprise_top_k` best) instead of the
  favourite itself, drawn from the story's own fixed randomness (a hash of its
  seed and the turn, never its dice, so skill checks roll as before): a story is
  still the same for the same seed, but one storyteller tells different stories.
- **Rhythm.** After `boredom_turns` turns with nothing new (no new place, event,
  person, outcome, check, or change of health or coins), options that bring
  something new (an event, a place not visited, a fight, a discovery, an outcome
  not told) rank higher by `boredom_weight` for every further calm turn, and an
  unexpected event becomes likelier (`boredom_event_pressure`).
- **Payoffs come due.** A clue open for `payoff_delay` turns wants its payoff:
  the action that pays it off, and the way to where it can, rank higher
  (`payoff_due_weight`).
- **The pull of the unknown.** A place not visited yet pulls harder the longer
  since the last new place (`explore_weight`), so stories move across the world.
- **No dead ends.** Going back to a place with nothing left there (no quest step,
  no payoff, no untold outcome, nobody unmet) costs `dead_end_penalty`.
- **Failing forward.** A failure that led nowhere may turn into a complication
  (`fail_forward`): an authored event tagged `complication` whose tags fit the
  failure starts once the turn is done. A botched bribe or a jammed safe brings
  *The guards come running*: hide, bluff, or slip away into the Sewers.
- **Quest momentum.** A quest idle for `mission_settings.momentum_after` turns (no
  stage done) pulls harder, at full strength after twice that, so the main quest
  keeps the story going instead of fading behind side paths.

**Twists that keep their word** (`unexpected_events.effects`): every procedural
twist is told with a text that matches what it does (a purse pressed into your
hand pays; a trap hurts). A mystery (`payoff`) plants a thread that a later twist
pays off, `twist_payoff_delay` turns later or in the finale (`[TWIST] The one
who followed you since the Harbor makes their move at last ...`). A **bargain** is
a choice: taking it pays now, and its price comes due later; refusing it closes
the matter.

**Payoffs that call back.** A payoff that closes a clue it belongs with (told at
the same place, or sharing a word) says so: `[CALLBACK] It all fits: what you
learned at the Harbor pays off - "A drunk sailor swears ..."`. A won story ends
with an **epilogue** of its journey (`end_victory`): the road from its first place
to its last by way of those that mattered, the quest's steps, the friends made and
the turns of fate, told as a story rather than a tally (counts of places and
threads made the judge rate the same stories lower: exploration 7.46 -> 6.88); the
epilogue of a story that reaches the action limit is told the same way. A first
journey somewhere says what one sees on the way (`travel_first`).

```yaml
director:
  enabled: true
  escalation_start: 0.3
  event_pressure: 0.6
  threat_weight: 0.8
  damage_scale: 1.25
  damage_escalation: 0.6
  finale_actions: 15
  finale_goal_pull: 1.0
  loop_window: 6
  min_options: 2
```

**What players see** (`player_options`, `GameState.player_options`): the
most relevant of the storyteller's options, never more than `limit` (6), so
their number follows the moment instead of always being the same:

1. What the moment is about comes first: a prisoner's ways out, the action
   that best advances the quest's current stage (or the step toward it), the
   payoff of an open clue, and the best way on.
2. Then the rest in the storyteller's order, at most `travel_limit` (3) ways
   on and `talk_limit` (2) "Talk with" options, and beyond `min` (3) none that
   rank in the lowest `relevance` (25%) of the options now.
3. In a tense moment (`GameState.tension()`: a prisoner, the finale, low
   health, or a threat, fight, guards, escape or failure in the last turn,
   `tense_tags`) at most `focus_limit` (4) are shown and the lower
   `focus_relevance` (50%) is left out; in between, in proportion.

With a random player in 30 stories of each of three worlds, a turn offered 1
to 6 choices, 3.5 to 4.7 on average and about 3 in tense moments (before: up
to 8, in the city 6.7 on average and 8 in half the turns, 6.2 in tense
moments), and the player completed more quests (city 19 of 30 instead of 15,
monastery 24 instead of 20, station 25 instead of 24). An event's choices are
shown as they are. They are listed
in the scene's order (`order: world`: activities, people, items, travel)
rather than best first (`order: ranked`), so the first option is not always
the storyteller's favourite. `relevance_filter: false` restores the plain
`limit` best.

**Missions per player** (`mission_selection`): a profile's
`recent_missions` latest missions are drawn `repeat_factor` times less often
each time they appear, as recent starts are, so a returning player meets
other missions.

## Sessions, the save game and the UNP

A **session** is one story a player plays; a **save game** is a player profile.

| Requirement | How the engine does it |
| --- | --- |
| A session starts with a start and a goal | The goal is drawn among the open ones (weight, recent goals drawn less often), the start among the places it can be reached from |
| Starts vary between sessions | `start_selection.recency_factors` (`[0.12, 0.35, 0.7]`): the latest start is the least likely, the one before less so; `mission_variety` favours goals reachable from starts not used lately. Over 300 sessions of one profile a session started where the last one did 6% of the times (a uniform draw: 25%) |
| One-time actions are remembered | Every attempt at a `repeat: once` action is kept (`unique_actions`: turn, place, success). A success uses it up; a failure uses it up too, unless the action has `retry_on_failure`, which keeps it on offer in the session |
| Achieved goals leave the pool | An achieved goal is added to the save game (`progression.goals_once`) and is not drawn or offered again for that profile; *Reset profile* starts a new save game |
| The progression is saved after each session | When a session ends (by itself, or `POST /v1/sessions/{id}/end`), its report is kept in the profile with its goal, and the playthrough is counted |
| The UNP is told | The report (goal and outcome `achieved` or `failed`, stats, inventory, health, coins, the goals achieved so far) is POSTed as JSON to `session_report.url` (or `ANE_UNP_URL`, with `ANE_UNP_TOKEN` as a bearer token), in the background; its delivery is noted and it can be sent again |
| A session does not bring everything at once | `session_scope`: what the goal needs stays (its places and the ways there, the people, items and events it names); of the rest, the most valuable share of each kind joins (value: fit with the goal's places and people, things to do, the session's own variety) and the rest stays out. In the city a session keeps about 11 of 15 NPCs, 6 of 8 items and 15 of 16 places, and the goal stays reachable |

The studio shows a session's cast, its one-time actions and its report beside the game, ends
a session (*End this session*), and shows the save game (*Save game*, with the reports and
whether they reached the UNP). The offline engine has the same: `POST /games/<id>/end`,
`GET /games/<id>/report`, `GET /progression/<profile>`. A UNP can also save a session
(`POST /games/<id>/save`), cancel it (its status, outcome `suspended`, and its save game), and
continue it later, exactly or from parameters it changed ([docs/UNP.md](UNP.md)).

## Story beats: who matters, the climax, failures, travel, stakes, reveals

The engine makes a story's beats itself (`core_engine/story_beats.py`); each one has its
own section in `config/hyperparameters.yaml` and can be switched off there. Every beat is
told with its own tag (`[REVEAL]`, `[CLIMAX]`, ...; the texts are in `config/messages.yaml`)
and logged in the story's `beat_log`, which the fitness (`fitness.beats`) and the judge's
digest read.

**Who matters, and how much** (`npc_roles`). Every NPC has a role in a story: its own
(`role:` in `config/npcs.yaml`), or the first `infer` rule it matches by its tags (the NPC
the quest is about and that is a threat is the story's **nemesis**; a unique threat a
**lieutenant**; a unique palace NPC a **power broker**; a unique talker an **informant**;
guards **henchmen**; everyone else a **bystander**). A role sets:

| Setting | What it does |
| --- | --- |
| `importance` 0-3 | shown with the people (`people`, `people_met`); importance 2+ is met with `[REVEAL]` and beaten with `[TRIUMPH]` |
| `check_modifier` | added to every check against or with them: a nemesis -15, a bystander +10 |
| `reveal_turns`, `reveal_clues` | out of sight until the story has run this many turns or the player learned this many things (clues, quest steps, allies), unless a lead names them (`leads_name_hidden`), the road speaks of them (first trips) or an outcome reveals them |
| `defeat_coins` | optional: what beating them is worth |

An NPC may also keep a **secret** that comes out once spoken with `talks` times (and
`clues` things learned): `[SECRET] text`, its `effects`, and the actions it unlocks with
`enabled_if: {secret_known: <NPC>}`. In the city the Merchant fences the thief's goods
(and can be threatened for it) and the Gate Guard is in debt (and lets you in if you
settle it).

**A climax for every quest** (`climax`). When the last step of a quest that took at least
`min_turns` turns is reached, the win waits for a showdown, an event of the story's own:
the antagonist (the quest's own target if hostile, else the most important hostile NPC who
belongs there or was met) stands in the way, with one choice per stat (`choices`: face them,
outwit them, talk them down) and, with an ally, `Call on <ally>` (`ally_choice.bonus`). A
setback costs `failure_damage` and the showdown comes back after `retry_turns`; after
`max_attempts` setbacks the quest is won at a price (`price_health`, never the last
point): a quest is never lost at its last step. Winning closes every open thread, the first
`revelations` told as `[REVELATION]`. Beating someone who matters on the quest's last turn
is its climax already.

**Failures as beats** (`director`). A failure no complication followed shows
**another way** with chance `another_way`: a witness here, someone who matters named, or a
way toward the quest (`[ANOTHER WAY]`); otherwise it is a dead end, which costs fitness. A
paid attempt that fails returns `failure_refund` of its price (`[REFUND]`). `GET
/v1/lint/failures` lists the failures a world leaves as dead ends, to write a consequence
for.

**Variation** (`variation`). An outcome may have `variants`, other ways to tell it, used in
turn when it is told again; once they are used up, the result line says it happened before
(`result_again`, `result_habit`). Each earlier success of an action makes its check harder by
`repeat_check_penalty` (up to `max_repeat_penalty`): people catch on.

**Travel** (`travel`). A place visited before, two or more steps away along known ways and
still holding something, can be reached in one turn (`Travel by known ways to ...`, at most
`fast_travel_options`, not within `fast_travel_cooldown` turns of the last); those turns do
not count toward the action limit (up to `max_free_turns`). A first trip somewhere has a
scene on the way (`first_trip.by_tag`, with `chance`; an entry may give `coins` or
`health`) and word on the road of someone who matters to be found there.

**Stakes** (`stakes`). Crimes and failures where the watch is (`alert.watched_tags`) raise
the **alert** (`raise_on_success`, `raise_on_failure`: an action's words or an outcome's
tags); from `warn_at` every rise is told (`[ALERT]`), each level makes checks against guards
harder (`check_penalty`), quiet turns let it fall (`decay_turns`), and at `max` a manhunt
starts (`manhunt_event`) and the alert drops to `reset_to`. Every choice with a check shows
what failing costs (`risk`: health, coins, being sent away), in the API and in the studio.

**Reveals** (`consequences`, `memory`). What the player did shows later: beating someone
who mattered or making an ally is told a few turns on, at a place it belongs to
(`[CONSEQUENCE]`, with `coins` or `alert`). In real play (a profile) the next story
remembers the last: `[LAST TIME] Last time you completed ...`, and old friends and foes
recognise the player.

**Fitness** (`fitness.beats`, `fitness.term_weights`). A climax won, triumphs (by
importance), reveals and consequences add to a story's fitness, failures that showed another
way a little, dead ends cost; `term_weights` multiplies any term of the breakdown (written by
`evaluator/calibrate_fitness.py`).

## Coherence rules

Rules found by auditing generated stories, applied to every world:

- **Narration.** The location is told after a move (`Transferred from A to
  B`), never before it. Characters and items are named with an article:
  "Met Aurora" (an NPC with `article: ""`), "Met the Thief" (unique), "Met a
  Guard" (generic), "Nearby you see an Ancient Map: …", "some Gold Coins". An
  NPC's `article` sets it explicitly.
- **Escapes are for prisoners.** An action whose outcome carries the
  location's `escape_tags` is offered only while the player is locked there
  (`lock_when: sent` or `always`), and may be tried again until it works.
- **No small talk with an attacker.** A hostile NPC (one with an
  `encounter_event`) gets no generic conversation; authored `dialogue` and
  actions still apply.
- **Talking in circles stalls.** A conversation that only repeats what was
  said counts toward `narrative_end.max_consecutive_steps_in_location`.
- **Leads go somewhere.** An NPC lead never points to the place the player is
  in, and never takes them back where the turn started after the outcome
  moved them. Someone who just refused or beat the player (a `failure`
  outcome) gives no directions, and neither does a hostile NPC (one with an
  `encounter_event`) after an ambush.
- **A failed theft gets nothing.** An action that collects an item does not
  give it when its attempt fails (a failure outcome, or an arrest for it).
- **A way to move on is a real choice.** The director hides what would only
  repeat itself even down to one option, when that option is travel or (for a
  prisoner) an escape; otherwise at least `min_options` stay.
- **Written routes count.** Route planning (mission choice, starts, goal
  pursuit) also follows the routes a world's location actions open (`move_to`,
  `reveal_location`), not only the map, so worlds that connect places through
  their actions get every start their missions allow.
- **No way here while already here.** An action whose successes only bring
  the player where they are ("the officer waves you through to the command
  deck", on the deck) is not offered.
- **Goals with a direction.** A goal such as `variables: {reputation: {gte:
  1}}` is pursued through actions that *raise* the value; actions that lower
  it no longer count as a way to the goal.
- **Hints have their time.** An action or outcome with `until` (a condition)
  is no longer offered or told once it holds, e.g. `until: {action_done: Crack
  the main safe}` on a hint about the safe, or `until: {reactor_fixed: true}`
  across playthroughs. A numbered clue (`setup_N`) is not told once its payoff
  (`payoff_N`) has been, and an action that could only tell such clues is not
  offered.
- **A failure is not a free ride.** After a failed attempt, the places its
  success would have led to are barred from the engine's own moves (moving on
  after stalling, following a new path, a lead) for `director.barred_turns`;
  the player may still walk there.
- **Threads close.** As the story escalates, planting new clues is worth less
  and paying off open ones more (`director.close_threads`); in the finale no
  new clue is planted.
- **A prison costs turns, never the story.** Each failed escape makes the
  next attempt easier (`director.escape_bonus` on its skill check), and a
  player still locked in after `director.release_after` turns is released
  (`[RELEASED] …`) instead of the story ending as stalled.
- **Pacing.** Goal pursuit starts at `mission_settings.pursuit_start_step`
  (18) and reaches full strength at `pursuit_full_step` (40), and a win before
  `fitness.base.mission_win_min_steps` (25) counts as rushed: stories run
  about 28 turns instead of 21, with as many missions won.

## Gameplay rules: checks, people, encounters, Power, threads

- **Checks are explicit.** An outcome without a skill check succeeds by its own
  `success_prob`; the storyteller's traits no longer tip it unseen
  (`outcome_resolution.hidden_adjustments: false`). An option with a check shows
  it before the player chooses: the scene lists **Checks** (`Bribe with coins —
  persuasion 55%`), the studio labels the choice, and the play state's
  `choices` give `{stat, chance, bonus}`.
- **NPC goals work.** Helping someone with what they want (an action with them,
  not small talk or a failure, whose name, text or tags share a word with one of
  their `goals`) counts; after `npc_goals.progress_needed` (2) times they are an
  **ally** (`[ALLY] You helped a Gate Guard collect bribes; …`): they show the way
  to a place they know (`reward: lead`, or `item`, `none`), their own checks get
  `ally_check_bonus`, and the storyteller is drawn to helping people
  (`action_weight`). A goal can also be a mapping:
  `{goal: Protect the palace, tags: [palace, guards], needed: 1, reward: item}`.
  The play state shows how often each person was helped and who is an ally.
- **Mandatory encounters are resolvable.** An NPC's `encounter_event` must name an
  event with actions (the configuration is refused otherwise); while it lasts,
  only its choices are offered, with the authored fallback when none can be
  taken; and an outcome with `return_to_previous: true` flees back the way the
  player came (`[BACK] …`), as the bandit ambush's *Make a run for it* now does.
- **Power counts in a fight.** Power is the player's fighting strength (health and
  skills, weapons and armour included). In a fight (a check on `attack` or
  `defence`, or an action tagged `combat` or `threat`) every point of Power
  gained since the story began adds `power_model.check_weight` (6) to the check:
  a sword helps, wounds hurt.
- **Item labels** in an action's name come from the item: "Give him the
  {alcohol_desc}" becomes "Give him the Vintage Wine", never its description.
- **Setups before payoffs** (`story_rules.gate_payoffs`). A payoff (a `payoff_*`
  clue tag) is told only while a planted clue waits for it (with `clue_matching:
  numbered`, its own `setup_N`). Named threads: an outcome with `setup_id:
  palace_route` plants it, one with `requires_setup: palace_route` is told only
  after, and one with `payoff_id: palace_route` closes it for good (an id that no
  outcome plants is refused).

## Story rules

Simulated stories were measured for coherence, pacing, and variety (200
stories with an evolved storyteller, every start, mission, stat split, and die
roll varying). Each problem found is now handled by a rule in
`story_rules` (`hyperparameters.yaml`, **Engine logic → story_rules**,
[`core_engine/rules.py`](../core_engine/rules.py)), by a fitness loss, or by the
evaluation method above:

| Problem measured | Rule | Before | After |
| --- | --- | --- | --- |
| A failed check told a success text; every arrest printed the success it interrupted | `failure_tags`: outcomes tagged `failure` (or `on_check: fail`) are the failure branch. The storyteller attempts a success; a passed check tells a success, a failed one the failure branch or a plain failure. An arrest tells no outcome text and applies no effects of the attempt | 58 contradictions in 917 checks | 0 in 1,216 |
| An NPC said "hidden in the Vault" but a path to the Harbor opened | `follow_outcome_text`: a `spawn` or an NPC lead opens the place the outcome's text names; an outcome that routes by itself adds no extra lead | | text and route agree |
| Every procedural event paid "coins +8" (about 37 free coins a story) | `unexpected_events`: effects rotate (`effect_repeat_penalty`), coins come from `coin_budget` (10), at most `max_per_story` (3), `cooldown_steps` (4) apart; parts are not repeated and the text names the place | 4.7 per story, 1 effect | 3 per story, effects rotate, ≤ 10 coins |
| Missions won within a few steps | `early_goal_avoidance`: before `mission_settings.pursuit_start_step` the storyteller holds back from the mission; fitness `pacing` loss | 28.5% of wins before step 12 | 10% |
| Stories ended "stalled" | `on_stall: move`: after `max_consecutive_steps_in_location` the traveler moves on (`[MOVE ON] ...`) | 9.5% stalled | 0% (only an unescaped prisoner can still stall) |
| Clues were rarely paid off, and never scored | Clues are recorded; `payoff_weight` and `payoff_route_weight` pull toward a payoff while a clue is open; `max_open_clues` stops planting more; `clue_matching: any` | 4.5% (and 0 in the fitness) | 53% of stories |
| Items were rarely picked up | `collect_weight` (useful items × 1.5) | 243 item turns | 357 |
| The same action again and again; back-and-forth travel | `repetition_power` (each use costs more), `backtrack_penalty`; fitness `repetition` loss; one-shot content as `repeat: once` + `retry_on_failure` | 69 stories repeat an action 3+ times; 2.7 A→B→A loops | 32; 1.3 |
| One turn in five decided by a near-tie, broken by name | `tie_epsilon`: near-ties are ordered by fewer uses, then more unseen outcomes, then a per-story order (no dice) | 21% by name | 0% by name |
| Evolution scored stories cut at 60 steps | `evolution.full_length_stories`, `stratify_episodes`, `score_aggregate` (see [The evolutionary AI](#the-evolutionary-ai)) | 60-step stories, random situations | whole stories, every start × mission |

The same measurement also found more varied stories (distinct action
sequences 128 → 172 of 200, distinct paths 118 → 141, authored events in 98%
of stories instead of 78%), longer ones (24.5 → 29.5 actions), and more wins
(85% → 91%).

```yaml
story_rules:
  failure_tags: [failure]
  follow_outcome_text: true
  early_goal_avoidance: 1.0   # x mission_goal_weight, fading out by pursuit_start_step
  on_stall: move              # or end
  repetition_power: 1.5       # uses ** power x novelty_bias
  backtrack_penalty: 1.5      # x novelty_bias
  tie_epsilon: 0.1
  collect_weight: 1.0         # x discovery_bias
  payoff_weight: 2.0          # x setup_prob
  payoff_route_weight: 1.5    # x setup_prob
  max_open_clues: 2
  clue_matching: any          # or numbered (payoff_N closes setup_N)
```

**Content.** The rules work with what writers tag: tag an outcome `failure`
to make it what a failed attempt tells, tag a climactic outcome `payoff_clue`
(any `payoff_*`) to close an open clue, and make one-shot actions
`repeat: once` with `retry_on_failure: true`. The shipped world does this for
cracking the Vault's safe and infiltrating the Palace, and the bandit ambush
has a `cooldown` of 6 turns.

Every rule can be turned down or off (a weight of 0, `on_stall: end`,
`follow_outcome_text: false`), and invalid values are rejected when the world
is built, uploaded, or saved in the logic editor.
