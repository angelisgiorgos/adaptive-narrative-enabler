# Authoring a world

The world is YAML under `config/`: places, NPCs, items, events, missions, stats, variables,
messages and the tuning. This guide covers every authoring feature; the studio's
*🌍 World* and *⚙ Advanced* destinations edit the same files
([STUDIO.md](STUDIO.md)).

## Configuration Files

- [`config/locations.yaml`](../config/locations.yaml): places, tags, descriptions, actions, outcomes, movement, reveals, and where stories may start. See [Starting locations](#starting-locations).
- [`config/items.yaml`](../config/items.yaml): objects, tags, descriptions, collectibility, item actions.
- [`config/npcs.yaml`](../config/npcs.yaml): characters, tags, descriptions, known locations/objects, NPC actions, and the `unique` flag. See [Unique and generic NPCs](#unique-and-generic-npcs).
- [`config/events.yaml`](../config/events.yaml): events, their exclusive actions, and outcomes. See [Events](#events).
- [`config/stats.yaml`](../config/stats.yaml): character stats. See [Character stats and skill checks](#character-stats-and-skill-checks).
- [`config/variables.yaml`](../config/variables.yaml): persistent variables. See [Persistent variables](#persistent-variables).
- [`config/messages.yaml`](../config/messages.yaml): every sentence the engine writes into a story. See [Story messages](#story-messages).
- [`config/missions.yaml`](../config/missions.yaml): missions and their goals, which are the story's win conditions. See [Missions and winning](#missions-and-winning).
- [`config/side_quests.yaml`](../config/side_quests.yaml): side quests, optional quests besides the mission. See [Side quests](#side-quests).
- [`config/hyperparameters.yaml`](../config/hyperparameters.yaml): evolution, simulation, fitness, LLM, editor, and augmentation settings.

The web studio also accepts a single combined `.yaml`/`.yml` file in the
**Simulation settings** tab. Combine the top-level contents of all four files
into one document. The studio checks every shipped section, nested parameter,
and value type before activating the upload; an invalid or incomplete file does
not change the active configuration. The upload is a runtime alternative and
does not overwrite the files under `config/`. Saving an editor entry or the
built-in settings restores the file-based configuration.

**When an edit takes effect.** A save (or an upload) is used at once by what starts afterwards:
new games, training runs and evaluations. A game already being played keeps the world and tuning
it started with until it ends, and a running training job keeps its own too (each runs with a
frozen copy: [docs/AI.md](AI.md#engine-contexts-which-configuration-a-game-reads-and-when-settings-take-effect)).

**A file may hold only a world.** Every tuning section it does not set is taken
from the built-in configuration (its own values win), tuning keyed by places
of the built-in world that it lacks is dropped, and the places the tuning must
name (`game_state.arrest_location`, `start_selection.default_location`) have
to be its own. [`examples/`](../examples/) holds two such worlds in
other genres, a frozen monastery and an orbital station, with staged missions,
`until` hints, a prison and remembered variables: upload one to play it.

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
    entry: sent           # one only arrives here by being sent (never a travel option)
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
`world_definition.events` in [`config/events.yaml`](../config/events.yaml):

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
- Event actions take `enabled_if` too, and an outcome's
  `effects: [{start_event: <id>}]` starts an event once the action is resolved.

**When an event may start.** These rules are checked in one place
(`GameState._enter_event`), so they hold for every way into an event: trigger
actions, NPC encounters, chains, `start_event` effects, and the random
schedule. While they block an event, its trigger actions are not offered, an
event action that would chain into it is not offered, and its encounters wait.

```yaml
# config/events.yaml
street_parade:
  enabled_if: {at_location: Market}   # eligibility: any condition
  repeat: per_visit                   # once (unique in the story) | per_location | per_visit | always (default)
  max_occurrences: 3                  # at most this many times per story
  cooldown: 4                         # turns after it ended before it can start again
  trigger: random                     # action (default): only when something starts it;
  chance: 0.15                        #   random: also by itself, with this chance per turn...
  weight: 2                           #   ...and this weight when several random events hit together
```

- **Random schedule.** Once per turn, before the player chooses, every
  `trigger: random` event that may start rolls its `chance` with the story's
  random generator (so a seed always gives the same story). If several hit,
  `weight` picks one. An NPC encounter goes first. This is separate from the
  procedural `unexpected_events`, which build an event from configured parts
  when the story's pressure passes a threshold.
- **The event log** (`GameState.event_log`) records each run: where, which
  visit, the NPC, the `cause` (action, encounter, chain, effect, random), the
  action chosen, its `result` (`success`, `failure`, or `abandoned` for the
  fallback option), and the turn it ended. Occurrences, `repeat`, and
  `cooldown` are counted from it.
- The shipped Memory Thief's event is `repeat: once`. The world ships no random
  event, so its stories keep the rule that only skill checks roll dice.
  Validation rejects an unknown `trigger` or `repeat`, a random event without
  a `chance` in (0, 1], `chance` or `weight` on a non-random event, a negative
  `cooldown`, and a `max_occurrences` below 1.
- During automated evolution, a trigger action is ranked by the content of its
  event, plus `genome.event_prob × action_selection.event_trigger_weight`.

Configurations uploaded before Events existed remain valid without an
`events` section. Existing Docker config volumes receive `events.yaml`
automatically (see [Docker config volumes](STUDIO.md#docker-config-volumes)).

### Missions and winning

**The shipped quests.** Nine missions, most of them in steps:

| Mission | Steps |
| --- | --- |
| Rid the market of the thief | Pick up the thief's trail (the Captain of the Watch's bounty, the Merchant, or a guard) → catch them in the act (mind their lookout, who fights you once you hunt them) → drive them out (beat them, hand them to the watch with the captain's warrant, or follow them to their hideout); the captain pays the bounty |
| The smuggler's tunnels | Get yourself thrown in the Dungeon → free the Smuggler (bribe the jailer, pick the lock, or overpower the jailer) → follow his tunnels to the Vault; freed, he becomes an ally and leads the way toward the quest |
| Champion of the back room | Win a brawl in the Tavern's back room → beat the Pit Champion (one rematch); in later playthroughs she remembers (`pit_champion`) |
| A face for the Palace | Buy an Official's Mask from the Maskwright → walk into the Palace (gate guards salute it) → unmask Aurora as the festival's patron |
| Expose the fence | Find out who buys the stolen goods (make the thief talk, or win the Merchant's trust) → get the fence's ledger (fight his bodyguard, name the thief, or buy it) |
| Break into the Vault, Buy your way into the Palace, Find a shield, Clear your name | as before |

Guards on duty take no bribes at a gate: the only bribe is the jailer's, offered to a prisoner
locked in the Dungeon (to open your cell, or to free the Smuggler, who knows the way).

**Missions in stages.** A mission may be a quest in steps: `stages` lists
`{title, description, goal, rewards}`, completed in order. Completing one is
told (`[QUEST] Make the thief talk: done. Next: Find the fence's ledger. …`),
applies its rewards, and the story then pursues the next stage (route planning,
the director's finale); the last one completes the mission. The score counts
each stage reached (`fitness.base.stage_bonus`), won or not.

```yaml
expose_the_fence:
  title: Expose the fence
  stages:
  - {title: Make the thief talk, goal: {action: Interrogate the thief}}
  - {title: Find the fence's ledger, description: The thief named someone in the Sewers., goal: {item: Fence's Ledger}}
```

Every story has a mission, a goal the player must achieve to win. Missions are
defined under `world_definition.missions` in
[`config/missions.yaml`](../config/missions.yaml). When a story starts, one mission
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
| `event` | The event was entered and resolved, whatever the result. |
| `event_succeeded` | The event was resolved by an event action that **succeeded**. A failed action, or stepping away with the fallback option, does not count. |
| `npc_removed` | The NPC was removed by an outcome's `remove_npc`. |
| `outcome_tag` | An outcome with this tag succeeded (e.g. `escape`). |
| `coins` | The player holds at least this many coins. |
| `variables` | Persistent variables compare as stated, e.g. `{reputation: {gte: 1}}`. |
| `condition` | Any [condition](#conditions-and-effects) holds, e.g. `{any: [{has_item: Heavy Shield}, {stat: defence, op: gte, value: 70}]}`. |

Each goal key is shorthand for a condition, so goals, `enabled_if`, and the
rest of the world are checked by the same evaluator. A mission's `rewards`
(an [effect list](#conditions-and-effects)) apply when it is completed, before
the story ends, and an effect `complete_mission: true` completes it directly.

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
  pursuit_start_step: 18   # goal steering is off before this step...
  pursuit_full_step: 40    # ...and at full strength from this step
action_selection:
  mission_goal_weight: 6.0 # bonus per goal condition an action or outcome advances (× goal_bias × pacing)
fitness:
  base:
    mission_win_bonus: 60      # fitness reward for winning...
    mission_win_min_steps: 25  # ...paid in full only for stories at least this long
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
starting location) are never chosen, and each mission is paired with a start
from which it can be reached (see [Starting locations](#starting-locations)).
A mission with an empty goal or an
unknown goal key raises a `ValueError` when the world is built. A combined
configuration upload rejects it with a clear message. Configurations uploaded
before missions existed stay valid and simply have no mission.

### Side quests

A side quest is an optional quest besides the story's mission
([`config/side_quests.yaml`](../config/side_quests.yaml),
[`core_engine/side_quests.py`](../core_engine/side_quests.py)). The mission is the story's
goal, and the story ends when it is complete. Side quests are things to do along the way:
- the player chooses whether to take one on;
- several can be under way at once, each with its own steps and rewards;
- none of them ends the story;
- completing one helps the mission;
- training rewards stories that see them through.

```yaml
world_definition:
  side_quests:
    charting_the_storm:
      title: Charting the storm
      description: The Storm Cartographer needs a chart older than the storm.
      giver: Storm Cartographer        # offered once the player has dealt with them
      start: {visited: Harbor}         # and/or a condition that must hold for it to be offered
      enabled_if: {reputation: {gte: 0}}   # offered only while this holds
      accept: choice                   # the player takes it on or turns it down; auto: it starts at once
      stages:                          # or a single `goal`, as for missions
      - title: Find an old map
        goal: {item: Ancient Map}
        rewards: [{change_coins: 2}]   # a step's own rewards
      - title: Bring it to the Storm Observatory
        goal:
          condition: {at_location: Storm Observatory}
      rewards: [{change_coins: 10}]    # when it is completed
      aids_mission: lead               # completing it opens the way toward the mission's next step
      turn_in: true                    # done, it is taken back to the giver to be completed
      within_turns: 20                 # it fails when not completed this many turns after it started
      follows: a_gift_for_the_chapel   # offered only once that side quest is completed
      fail_if: {npc_removed: Storm Cartographer}   # fails while under way when this holds
      on_fail: [{change_variable: {reputation: -1}}]
      on_decline: [{change_variable: {reputation: -1}}]   # turned down, or the offer let go
      repeatable: false                # true: offered again in the profile's later stories
      texts:                           # the story's own words for its moments
        offer: '{giver} unrolls a ragged chart: "Bring me one older than the storm."'
        complete: '{giver} spreads the old chart beside the new: "There. The island."'
```

| Field | Meaning |
| --- | --- |
| `title`, `description`, `goal` or `stages`, `rewards`, `enabled_if` | As for [missions](#missions-and-winning): the goal keys are the same, every step's goal must hold in order, each step's `rewards` apply when it is done and the quest's `rewards` when it is completed |
| `giver` | The NPC who offers it: it is offered once the player has dealt with them in this story (any action with them, talking too). A hostile NPC (an encounter) cannot be a giver |
| `start` | A condition that must hold for it to be offered. With neither `giver` nor `start` it is offered as soon as `enabled_if` holds |
| `accept` | `choice` (default, `side_quest_settings.accept`): the player takes it on or turns it down; `auto`: it starts at once |
| `aids_mission` | What completing it does for the story's mission (default `side_quest_settings.aids_mission`, `lead`): `lead` opens the way toward where the mission advances next (the route toward its next step); `ally` makes its giver an ally, who leads and helps like anyone whose goal the player fulfilled; `none` |
| `turn_in` | `true` (needs a `giver`): once its goal holds it is not complete yet. It is taken back to the giver: "Hand over: <title>" comes first among the choices wherever the giver is in the scene, and dealing with the giver in any other way hands it over too. It fails if the giver leaves the story; it is no longer done if what was brought is gone |
| `within_turns` | It fails when it is not completed this many turns after it started (once it is done, the way back to the giver does not count against it). The story says how long it has, and warns when two turns are left |
| `follows` | Another side quest: this one is offered only once that one is completed, in this story or one of the profile's before: a chain |
| `fail_if` | A condition that fails it while it is under way; then `on_fail` (an effect list) applies |
| `on_decline` | Effects when it is turned down, or the offer is let go (a disappointed giver) |
| `texts` | The story's own words for its moments, instead of the messages: `offer`, `accept`, `decline`, `ready`, `turn_in`, `complete`, `fail`, `remind`; they may use `{title}`, `{giver}`, `{reward}` (its rewards in words), `{todo}`, `{turns}` (turns left) |
| `tags` | What it is about. When several side quests could come, the story offers first the one that fits it now: tags shared with the place (1 each) and with what the last turns brought (0.5 each); a side quest on the mission's thread (it wants what the mission wants: 2; a place it names is where the mission advances next: 1) |
| `reactions` | What the story does to it: `[{when, then, effects, text, while}]`. When the condition `when` comes to hold while the quest is `active` (default), `offered` or `any`, the reaction happens once: its `text` is told (placeholders as in `texts`), its `effects` apply, and `then` does to the quest: `complete`, `fail` (reason `story`), `advance` (its next step, or done), `withdraw` (the offer, or the quest, is taken back) or `none` |
| `repeatable` | `false` (default): once a profile has completed it, it is not offered in that profile's later stories |

**Taking it on.** A side quest that can start is offered: its giver asks (`side_quest_given`), or
the story tells it (`side_quest_start`). Then the story tells what is in it for the player (its
rewards in words: "In it for you: 6 coins, reputation +1."), whether it could help the mission, and
how long it may take. The choices "Take on: <title>" and "Turn down: <title>" then
come first among the player's choices; their ids are `side-quest:<quest>:accept` and
`side-quest:<quest>:decline`. Answering takes no turn. An offer that stays unanswered for
`side_quest_settings.offer_turns` (3) turns lapses, like a turned-down one; turns in which an event
hides the offer do not count. A side quest turned down or let go is not offered again in that story.
With `accept: auto` it starts at once, without a choice.

**How a story runs them.** Side quests are checked at the end of every turn, and after a UNP sets
the player or reports what happened. They are never checked in the middle of a turn, and they are
taken in the order they are written:
1. Those under way fail (`fail_if`), complete a step, or are completed; unanswered offers age.
2. Then new ones are offered (or start), as long as fewer than `side_quest_settings.max_active` (3)
   are under way or on offer.

A side quest whose goal already holds when it starts is completed at once (the item already in
hand). When it starts, or moves on to its next step, the places its goal names that no known way
leads to are pointed out: the way opens from where the player stands, along the map ("[LEAD] the
Merchant points you the way to the Drowned Archive."); a hand-over points the way back to the
giver. A side quest that has not moved for `side_quest_settings.remind_after` (6) turns is recalled
to the player, with what it still needs ("[SIDE QUEST] Still open: Charting the storm - Have the
Ancient Map."), once per quiet spell.

Every moment is told in the story, in the quest's own words (`texts`) or by the
[Story messages](#story-messages):
- the offer (`side_quest_given`, `side_quest_start`, `side_quest_stakes`, `side_quest_helps`,
  `side_quest_deadline`) and the answer (`side_quest_accepted`, `side_quest_declined`);
- its course: the way pointed out (`side_quest_way`), a step done (`side_quest_stage`), a reminder
  (`side_quest_reminder`, `side_quest_remind_ready`), time running short (`side_quest_hurry`);
- its end: done and waiting (`side_quest_ready`), handed over (`side_quest_turned_in`), completed
  (`side_quest_complete`, then `side_quest_aid` or `side_quest_ally`), or failed
  (`side_quest_failed`, `side_quest_expired`, `side_quest_giver_gone`, `side_quest_item_gone`);
- the story's ending: `side_quest_epilogue`. None of this draws from the dice, so a seeded story stays
reproducible.

**The story's say.** Side quests live in the story, not beside it:
- **When a new one comes.** The story lets a new side quest in only at fitting moments
  (`side_quest_settings`): not before turn `offer_after` (1), at most one every `offer_cooldown` (1)
  turns, never during an event, never in a tense moment (the story's tension above
  `offer_max_tension`, 0.75: danger a moment ago, a prisoner, the watch hunting the player), and not
  once the finale has begun (`finale_offers: false`). When several could come, the one that fits
  the story now comes first (`fit`, `tags`).
- **What happens to it.** The quest's `reactions` let the world's events change it: a brawl won
  fills the purse, the fence exposed makes the Merchant's news worthless, a theft makes it urgent.
- **Continuity.** A side quest that can no longer be done ends with its reason (`impossible: fail`,
  or `keep` to leave it): its giver gone from the story (a quest under way fails, an offer is
  withdrawn), or a unique item its step needs gone for good ("[SIDE QUEST FAILED] Wine: a Vintage
  Wine is gone for good."). A deadline does not run while the player is held in a cell
  (`pause_when_held`). A quest written without a description is put to the player by what it
  needs, in words.
- **The ending.** The epilogue recalls them (`epilogue`): "[SIDE QUESTS] Along the way you saw A
  full purse through; failed at Rumours from the Archive; left Charting the storm unfinished."
  Conditions remember them too: `side_quest_failed: <key>`, `side_quest_declined: <key>`.

**The mission.** Completing a side quest helps the story's mission (`aids_mission`). With `lead`,
the way toward the place where the mission advances next opens from where the player stands. The
route follows the map, toward the place that pulls hardest for the quest. The story tells it:
"[LEAD] Charting the storm pays off for your quest: the way to the Sewers is open." With `ally`, its
giver becomes an ally, who then leads the player on and helps in checks (`npc_goals`). Nothing
happens when the story has no mission, the mission is won, or the way is known already.

**Training.** A story's fitness, the training objective, has a `side_quests` term
(`fitness.side_quests`; see [docs/AI.md](AI.md)):

```
completed x complete_bonus (10) + steps done x step_bonus (3)
  + those that helped the mission x mission_aid_bonus (6) - failed x fail_penalty (4)
  - (once the story has ended) those left under way x unfinished_penalty (2)
```

The storyteller's ranking also pulls toward the next step of a side quest under way: a step on the
way to a place it names counts too, a giver waiting for a hand-over pulls hardest, and the pull
grows as a deadline nears. It also pulls toward someone who offers one. The pull is
`side_quest_settings.rank_weight` (2.0) × the storyteller's own learned trait `side_quest_bias`
(a genome trait apart from the mission's `goal_bias`, bounds `genome_bounds.side_quest_bias`), so
training can tune the mission and the side quests apart. Every side-quest moment is also something
new for the director's sense of rhythm, and a development of its own (`side_quest`) for the
fitness's rhythm terms. Training therefore favours storytellers whose stories see side quests through;
like every fitness term it is tuned on the Weights page and can be calibrated to the judge
(`fitness.term_weights.side_quests`). In training the simulated players answer offers as players
would: most often by taking them on; the *quester* always prefers to, since a side quest helps the
quest. A storyteller playing a story itself (`evolution.player: storyteller`) answers no offers,
so its offers lapse; set `accept: auto` for such training.

**The player's choices.** With `side_quest_settings.guide` (on), one of the choices shown works
toward a side quest under way, or leads to someone who offers one, as one works toward the mission
(`priority_side_quest` in the author's decision trace; offers: `side_quest_offer`). The people in a
scene list the side quests they offer (`people[].side_quests`), and the studio marks them ❗.

**What follows from them.** Conditions can ask about side quests (`side_quest_active: <key>`,
`side_quest_done: <key>`, `talked_to: <NPC>`; see [Conditions and effects](#conditions-and-effects)).
A mission, an event, a dialogue line or another side quest can therefore follow from one.

**Where they show.**
- The play state lists the story's side quests (`side_quests`: key, title, description, giver,
  status (`offered`, `active`, `completed`, `failed`, `declined`), steps, what the current step
  still needs, the turns it was offered, started and ended, and `aided_mission`: `lead` or `ally`
  when it helped).
- Each turn of the journal says what happened to them (`side_quests`: offered, started, declined,
  lapsed, stage, completed, failed).
- The session report has `side_quests: {completed, failed, declined, unfinished, aided_mission}`,
  and the profile keeps the side quests it completed (`progression.side_quests_completed`).
- Save games, snapshots and the UNP's session parameters (`side_quests: {key: {status, stage}}`)
  carry them. A session rebuilt from its parameters stands where its side quests stood, and
  nothing is told or paid again.

**Checks and editing.** The side quests are checked like missions: goal keys, the names they refer
to, rewards and conditions. In addition:
- the giver must be an NPC of the world who is not hostile;
- `start` and `fail_if` must be valid conditions, and `on_fail` a valid effect list;
- `repeatable` must be true or false;
- `accept` must be `choice` or `auto`;
- `aids_mission` must be `lead`, `ally` or `none`, and `ally` needs a giver.

They are edited in 🌍 World → 🎯 Missions → *🧭 Side quests*, through
`PUT /v1/documents/side_quests`, or by a UNP with `POST /world/patch` (kind `side_quests`; see
[docs/UNP.md](UNP.md)). A change that breaks one of these rules is refused, and nothing is written.

### Starting locations

A writer decides where stories may begin. In
[`config/locations.yaml`](../config/locations.yaml), a location opts in with
`start_eligible` and sets its relative chance with `start_weight`:

```yaml
Harbor:
  start_eligible: true   # default false: a story never starts here
  start_weight: 1.5      # relative chance among the starting locations (default 1)
Festival of Masks:
  enabled_if: {reputation: {gte: -2}}
  start_eligible: true   # a starting location only while it is enabled
  start_weight: 0.5
```

The shipped world starts at the Harbor (1.5), the Market (1), the Tavern (1),
or the Festival of Masks (0.5, only for travelers with a good enough name).
Each new story picks its start in three steps:

1. **Eligible and enabled.** Only locations with `start_eligible: true` whose
   `enabled_if` holds for the player can be drawn. If every starting location
   is switched off for a profile, creating the story fails with a clear error
   instead of starting elsewhere.
2. **The mission can be reached.** The mission is drawn first, by `weight`,
   among the missions that some starting location can reach. The start is
   then drawn among the locations from which that mission can be reached:
   its goal does not already hold there, and the route planner (known routes
   plus routes that can still be discovered through spawn rules) leads from
   there to a place where the goal can be advanced. Start variation therefore
   never strands the player away from the goal. A requested mission
   (`mission=`) restricts the starts in the same way.
3. **Weighted, with recent starts discouraged.** The draw uses the session's
   random generator, so a seed always gives the same start. Each location's
   weight is its `start_weight`, multiplied by `start_selection.repeat_factor`
   once for every time it started one of the profile's last
   `start_selection.recent_starts` stories. Repeats are less likely but never
   impossible.

```yaml
# config/hyperparameters.yaml
start_selection:
  default_location: Harbor  # the single start of worlds where no location is start_eligible
  recent_starts: 3          # how many of a profile's latest starts count as recent...
  repeat_factor: 0.25       # ...each one multiplies that location's weight by this (0 < f <= 1)
```

With the shipped weights and no history, the Harbor starts 37.5% of stories.
Right after a story that started there, its weight drops to 1.5 × 0.25 and its
chance to 13%. With two equal starting locations, a profile's second story
repeats the first one's start 20% of the time instead of 50%.

**Start history.** Real play (a game with a player profile: the studio, the
REST API, and exported engines with `persist`) records each story's start
with the profile's persistent variables (`starts` in the profile's record,
the latest 20). It is recorded when the story is created, also with
`persistence.save_on: end`. Simulations and evolution never record starts and
use no history, unless `persistence.simulate_with: stored`. Resetting a
profile also forgets its starts.

**Choosing the start.** `NarrativeEngine.new_game(start="Tavern")`, the HTTP
server's `POST /games` with `{"start": "Tavern"}`, and
`python run.py play --start Tavern` start at a given starting location.
Asking for a location that is not one raises `ValueError` (HTTP 400). If the
requested start is switched off for the profile by its `enabled_if`, the start
is drawn as usual. `engine.info()["starting_locations"]` lists the choices, and
every view and play state includes `start_location`. In code,
`GameState(world, genome, start="Palace")` forces any enabled location, which
is useful for tests and debugging.

**Older worlds.** A world in which no location sets `start_eligible` keeps a
single start, `start_selection.default_location` (Harbor) or else its first
enabled location. No random number is drawn for it, so its stories replay as
before, except that a mission the route planner cannot reach from that start
is no longer chosen while another one can be reached. Configurations uploaded
without a `start_selection` section stay valid.

In the studio, **Create the world → Locations** has **Stories can start here**
and **Start weight**, and the entity API exposes `start_eligible` and
`start_weight` for locations. `start_selection` is editable in **Engine
logic**. `start_eligible` must be `true` or `false` and `start_weight` must be
above 0. `repeat_factor` must be above 0 and at most 1, since 0 would forbid
repeats. Invalid values are rejected when the world is built or uploaded.

### Unique and generic NPCs

Every NPC in [`config/npcs.yaml`](../config/npcs.yaml) declares whether it is
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
- **Studio:** **🌍 World → 📝 Entries** → content type **NPCs** → *Fields* → the
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

### Uniqueness of locations, items, and events

Each kind of entity has its own uniqueness rule:

| Entity | Rule |
| --- | --- |
| Location | Always unique: one named node of the world graph, visited any number of times. Visits are counted (`location_counts`), which is what `per_visit` rules use. There are no repeatable location instances. |
| NPC | `unique: true` lives in one home location for the whole story; a generic NPC appears wherever it fits. Removal is permanent for unique NPCs and lasts one visit for generic ones (see above). |
| Item | `unique: true` exists once per story (below); a generic item is shown wherever it fits, each location with its own copy. |
| Event | `repeat: once` happens at most once per story, whatever starts it; `per_location`, `per_visit`, `max_occurrences`, and `cooldown` limit it otherwise (see [Events](#events)). |

**Unique items.** The item in `config/items.yaml` is a definition. A unique
item gets one **instance** per story (`GameState.item_instances`), separate
from the definition and from the inventory:

```yaml
- name: Fence's Ledger
  enabled_if: {fence_exposed: true}
  unique: true
  location: Sewers     # where its instance lies (default: the location its tags fit best)
```

- **Placed once.** At the start of a story the instance is placed at its
  `location`, or at the location its tags fit best (shared tags, then tag
  similarity), so the same content always places it in the same spot. It is
  shown only there, always (it does not compete for
  `scene.max_visible_objects`), and `reveal_item` elsewhere does not show it.
- **One copy.** Collecting it (an action's `collects_object`, from any NPC or
  location) or receiving it (`give_item`) moves the instance into the
  inventory and out of the world. Using it (`consumes_object`) or losing it
  (`take_item`) makes it gone for good: it cannot be collected or received
  again. Instance states are `placed`, `held`, `gone`, and `unplaced` (no
  location fits; only an action or an effect can hand it over).
- **History.** `GameState.item_log` records, for every item, when it was
  placed, collected, received, used, or removed, independent of the current
  inventory. The condition `item_collected: X` reads it.
- The shipped Fence's Ledger is unique and lies in the Sewers. Its tags alone
  would also show it in the Bone Orchard and the Ghostline Station.
- Validation: `unique` must be true or false, and only a unique item may have
  a `location`, which must exist.

**Where to edit.** In the studio, **Create the world → Uniqueness & scheduling**
edits an event's start rules or an item's uniqueness as YAML (validated, and
nothing is written when invalid). The API offers
`GET|PUT /v1/entities/{Events|Items}/{name}/policy`.

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
- **Studio:** Create the world → **NPCs** → **Encounter event** (a dropdown of
  the events in `events.yaml`, or *None*) and **Repeat on every visit**.
- **REST API:** `encounter_event` and `encounter_repeat` on
  `GET|PUT /v1/entities/NPCs/{name}`. Omit a field to keep the stored value,
  and send `"encounter_event": ""` to remove the encounter.
- **Terminal editor:** edit an NPC → **6. Encounter event**.

An unknown event name is rejected when saving from the studio or the API. At
run time it is ignored with a note in the transcript.

### Conversations

The people present are approached one at a time (`conversations` in `hyperparameters.yaml`,
on by default). On the scene, each person is one choice, **Approach the Gate Guard**. Taking
it opens a conversation: the choices are then that person's (talk with them, ask, bribe,
fight, give an item...) and **Leave the conversation**.

- **No turn passes** when approaching someone or leaving a conversation. A person just left
  is not offered again until something happens. After `max_free_steps` of these choices in a
  row, every choice is on the scene again, so a story always moves on.
- **The conversation goes on** after choices that settle nothing: questions and small talk.
  A **decisive** choice ends it: an event or a fight (`triggers_event`), a payment
  (`required_coins`), an item given, taken or used, one of an exclusive choice, an outcome
  tagged `decisive_tags` (`combat`, `threat`, `guards`, `escape`), a move, or the person gone.
  It also ends when they have nothing left to offer.
- **Someone may start it**, by their tags (`npc_behaviour`, below): they come up to the player
  (*The Captain of the Watch comes up to you, wanting a word.*) and the conversation is open;
  the player may leave it. Hostile NPCs (`encounter_event`) still start their event.
- **The storyteller sees through it.** An approach is worth the best of what the person
  offers, so the quest's next step and the director's pacing still lead to the right
  person. The storyteller's own list (`ranked_available_actions`) always has every choice.
- **The option names and lines** are messages: `approach_npc`, `leave_conversation`,
  `approached`, `conversation_left` and `npc_approaches`.
- **Every client takes them the same way**: the studio's ▶ Play, the API's sessions, the
  standalone ANE AI (`GameState.take_option`) and the simulated players. The views say who
  the player is talking with (`talking_to`).

```yaml
conversations:
  enabled: true                 # false: every person's choices on the scene, as before
  decisive_tags: [combat, threat, guards, escape]
  approach_limit: 3             # at most this many "Approach" choices shown at once
  max_free_steps: 6
```

### What people and places do, by their tags

Who comes up to the player, how often someone is seen, which places draw the player in and how
often a place may be visited are decided by tags, not entry by entry: a rule in `npc_behaviour`
or `location_behaviour` (`hyperparameters.yaml`) says what everyone (or everywhere) with one of
its tags does. Give an NPC or a place the tag and it behaves that way; change the rule and
everything with the tag changes. For each behaviour, the first rule that sets it and shares a tag
decides; without one, `default`. In the studio, *🌍 World → 📝 Entries → Fields* shows each behaviour as a
choice of these tags for the entry (choosing one sets its tag), and *🌍 World → 🏷 Behaviour by tags* edits the
rules.

**People** (`npc_behaviour`, tags in `associated_tags`):

| Behaviour | Values |
| --- | --- |
| `starts_conversation` | `never`; `first_meeting`: they come up to the player the first time they meet; `every_visit`: whenever the player comes by |
| `appearances` | `always`: whenever they fit the scene; `once`: in one visit of a story; a number: in at most that many visits. Someone the quest needs now (a target of it, or someone whose choices or events advance its step) is always shown |

```yaml
npc_behaviour:
  default: {starts_conversation: never, appearances: always}
  rules:
  - {tags: [prisoner], starts_conversation: first_meeting}     # the Smuggler calls a new prisoner over
  - {tags: [quest_giver], starts_conversation: first_meeting}  # the Captain of the Watch
  - {tags: [hawker], starts_conversation: first_meeting}       # the Merchant
  - {tags: [challenger], starts_conversation: every_visit}     # the Pit Champion, on every visit
  - {tags: [wanderer], appearances: 2}                         # the Runaway Apprentice: two visits at most
  - {tags: [apparition], appearances: once}                    # the Pale Conductor: once in a story
```

**Places** (`location_behaviour`, tags in `tags`):

| Behaviour | Values |
| --- | --- |
| `draws_in` | `never`; `first_visit`: on arrival the place's own activities are offered first, with **Look around** for everything else (no turn passes; doing something else, or something decisive, ends it); `every_visit` |
| `visits` | `always`; `once`; a number: how many visits a story may have there. After that the place is no destination (no travel option, no lead, no automatic move), unless the quest's next step happens there or it is the only way on; a written `move_to` still goes there |

```yaml
location_behaviour:
  default: {draws_in: never, visits: always}
  rules:
  - {tags: [lively], draws_in: first_visit}       # the Tavern
  - {tags: [spectacle], draws_in: every_visit}    # the Festival of Masks
  - {tags: [secluded], visits: 2}                 # the Secret Garden
  - {tags: [fleeting], visits: once}              # the Bone Orchard
```

An unknown behaviour or value, a rule without `tags`, or a number of visits below 1 stops the
world build with a clear message (and a save of the rules in the studio).

### Player health and power

`HealthPlayer` ([`core_engine/health.py`](../core_engine/health.py)) owns
everything about the player's health and power. Every `GameState` has one as
`state.health_player`. Both stats have a hard maximum:

- **Health** is an integer from `0` to `game_state.max_health` (default `10`;
  the story starts at `initial_health: 8`). Healing never raises it above the
  maximum, and damage never takes it below 0. If `initial_health` is above the
  maximum, the story starts at the maximum.
- **Power** is recomputed after every change from the health ratio
  (`health / max_health`) and the player's average skill value
  (`power_model.stat_weight`; see [Character stats and skill checks](#character-stats-and-skill-checks)). It
  is clamped to `power_model.min_power` .. `power_model.max_power` (default
  `0` .. `5`).

| Member | Purpose |
| --- | --- |
| `heal(n)` / `damage(n)` / `change(delta)` / `set(value)` / `restore_full()` | Change health within bounds; each returns the change that really applied. |
| `effective_change(delta)` | Preview what a change would do, without applying it. |
| `is_alive`, `is_dead`, `is_low` (≤ `low_health_threshold`), `is_full`, `missing`, `ratio` | State checks used by the engine. |
| `calculate_power(stat_average)` | Recompute and clamp power. Combat damage and failure penalties are handled by `CombatSystem`. |
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
- name: Show him your official's face
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
- **Retry after failure.** `retry_on_failure: true` makes a failed attempt
  use nothing up. A `once` action then stays on offer (in this visit, on later
  visits, and in a saved game) until it **succeeds**, and only then disappears
  for good. A `per_visit` action stays on offer in the visit until it
  succeeds. "Failure" is the same as everywhere else: a failed skill check, or
  an outcome that does not succeed.

  ```yaml
  - name: Pick the lock
    repeat: once
    retry_on_failure: true   # try again after a snapped pick; gone once the door opens
    skill_check: {stat: stealth}
  ```

  | `repeat` | `retry_on_failure` | After a failure | After a success |
  | --- | --- | --- | --- |
  | once | false (default) | gone for the game | gone for the game |
  | once | true | still offered | gone for the game |
  | per_visit | false | gone for this visit | gone for this visit |
  | per_visit | true | still offered | gone for this visit |
  | always | (not allowed) | offered | offered |
- **Defaults.** Without `repeat`, `action_rules.default_repeat` in
  `hyperparameters.yaml` decides by source: `location: per_visit`,
  `npc: per_visit`, `item: once`, `event: always`. Travel and conversation
  options are always available.
- **Questions are asked once.** An action that only *tells* is offered once,
  unless it sets its own `repeat`: every outcome at most reveals a place, a
  person or an item, opens a path (`spawn`) or shows the way (`lead_to_known`),
  and nothing else changes (no coins, health, items, variables, stats, moves or
  events). "Ask about the crown" is answered once, not on every visit. An answer
  that told nothing ("the prisoner is too far gone to speak") does not use it
  up: it is offered again on the next visit. `action_rules.questions_once:
  false` turns this off.
- **Exclusive groups.** `exclusive_group: <name>` makes the actions with the
  same name one choice. When one of them **succeeds**, the whole group
  disappears for the rest of the game. After a failed attempt the other options
  stay (a failed bribe still lets you fight), while the failed option itself
  follows its `repeat` rule.
- **Scope.** One-time options and groups belong to their owner: a location, a
  unique NPC, an item, or an event. A generic NPC is a different person in each
  location, so getting past the Palace gate guard does not get you past the Vault's.
- **Guides lead toward the quest.** When an NPC leads the way
  (`lead_to_known`) or an ally shows a route (`npc_goals.reward: lead`), the
  places they know score `npc_leads.quest_weight / (1 + steps to where the
  quest can advance next)` on top of their tags: a prisoner you free takes you
  toward your quest, not just anywhere he knows.
- The mission planner respects these rules. A goal option that is used up no
  longer counts as a way to the goal.
- Set them in YAML, in the studio (**Create the world → Actions & outcomes →
  How often one action is offered**: pick the action, choose **Repeat**, tick
  **Retry after failure**), through `PUT /v1/entities/{kind}/{name}/actions`,
  or in the terminal editor: edit an action → **6. Repeat rule** and
  **7. Exclusive group**. An invalid `repeat`, a non-boolean
  `retry_on_failure`, or `retry_on_failure` with `repeat: always` (which never
  runs out) stops the world build, an upload, or a studio save with a clear
  error.
- Saved games (`Game.save`) keep what was used up and each action's retry
  rule; games saved before `retry_on_failure` existed load with it off.

The old name-based rules are gone. Actions starting with "Approach" used to
be always available, and "Move to" was how travel was recognised. Actions
now carry their `source` (location, npc, item, event, navigation,
conversation), so renaming anything, including travel options, is safe.

### Action identities

Every action has a stable **id** in play, independent of the text it is shown with: the player's
choices carry it (PlayState `choices[].id`) and a client chooses with it (`action_id`), so
rewriting an action's name, an item named in place of `{...}`, or two people offering an action of
the same name never mixes choices up. The exported ANE AI gives the same ids (its view's `choices[].id`,
`POST /games/<id>/choose {"action_id", "state_version"}`; [docs/UNP.md](UNP.md#the-view)).

```yaml
- id: ask_crown            # optional: the action's identity (its name is used when it has none)
  name: Ask about the crown
```

| Offered by | Id | Example |
| --- | --- | --- |
| a place, person, item, event | `<kind>:<owner>:<id or name>` (as slugs) | `npc:gate-guard:ask-crown` |
| talking with someone, approaching them, leaving them | `talk:` / `approach:` / `leave:<person>` | `approach:oracle` |
| travel, fast travel along a known way | `go:<place>` / `travel:<place>` | `go:market` |
| looking around after a place drew the player in; stepping away from an event | `look:<place>` / `fallback:<event>` | `look:harbor` |
| taking an offered [side quest](#side-quests) on, or turning it down | `side-quest:<quest>:accept` / `:decline` | `side-quest:charting-the-storm:accept` |

Give an action an `id` when its name may change and saves or clients should keep recognising it;
the ids of one entry's actions must differ (the editors refuse a duplicate). Without one, the
authored name is its identity, so renaming it gives it a new one. Two actions of one entry with the
same name and no `id` are told apart by where they are written (a short digest is added). An
author's decision trace (`GET /v1/sessions/{id}/decisions`) also gives each action's
`content_ref`, e.g. `world_definition.characters[Guard].actions[2]`.

### Story messages

Every sentence the engine adds to a story lives in
[`config/messages.yaml`](../config/messages.yaml), 130 messages in all: travel,
arrivals, discoveries, leads, events, encounters, failures, missions, side quests,
and endings. Rewrite any of them:

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
  [`core_engine/narration.py`](../core_engine/narration.py).
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
  `mission`, `epilogue`, `action_limit`, `stalled`, `dungeon`, `story_end`) that fitness uses instead of
  parsing `end_reason`. Previously, stalling *in* the Dungeon was penalised
  twice because the text contained both "stalled" and "Dungeon".

### The map: which places border which

A world's map says which places border which. Every move follows it: one
cannot go between two places that do not border each other (from the Dungeon
straight to the Palace, say).

- **`world_definition.routes`** (the city's, in `config/locations.yaml`):
  each place with the places it borders; a route holds both ways. With routes
  the spawn rules are the routes, so new paths only open between neighbours.
- **A far place is reached through the places between**, one turn each. An
  outcome that moves the player somewhere that does not border their place
  takes them to the next place on the way, told as `[ROUTE] No road runs
  straight from the Sewers to the Ghostline Station; you set out by way of the
  Market.` (`route_via`). The story then heads on toward it (the next step is
  favoured, `action_selection.heading_weight`, for `heading_turns`).
- **A far place revealed is the way toward it.** A discovery or a lead that
  names a far place opens the path to the next place on the way, told once as
  `[LEAD] The way to the Palace runs through the Market.` (`lead_via`).
- **People cannot take you where this place does not lead.** An NPC's action
  that would take the player to a place that does not border this one (a
  Gate Guard met in a cell "letting you into the Palace") is not offered there.
- **Places one is only sent to** (`location_constraints.<place>.entry: sent`,
  the Dungeon) are never a travel option, a discovery or a stop on the way;
  an arrest (`send_to`) is the only jump on the map.
- **Without routes**, every story draws its own map (`story_map`, below) and
  moves follow that, together with the places a location's own actions lead
  to; a place the drawn map cannot reach at all is opened directly when the
  world's own words name it. A world with neither (no spawn rules at all) has
  no map, and any move goes, as before.
- **The way to a far place becomes known** along the whole path, so the player
  who set out for it, or heard of it, can follow it.
- **One graph, both ways.** Every path found joins two places both ways (a place
  never leads to itself; two places whose names differ only in case or spacing
  are refused) and stays with the game: the play state's `map` lists the known
  places and paths, saved games keep them, and the studio shows them. The way
  back to the place just left is always among the player's choices
  (`player_options.way_back`).
- **Player-directed navigation** (`navigation.player_directed: play`). In real
  play a discovery or an NPC's hint opens a path but never moves the player, and
  lingering neither moves them on nor ends the story: they choose where to go.
  The storyteller's simulations keep moving by themselves (`always` / `never`
  set it for every story).
- **Locked exits.** A location with `exit_locked: true` cannot be left until an
  outcome with `unlock_exit: true` (or an escape) opens it (`[UNLOCKED] …`); the
  action that opens it is offered only while it is locked.
- **Discoveries led by tags.** An outcome with `target_tags: [nature, secluded]`
  opens the neighbour that fits them best instead of a named place, and
  `move_to_tags` moves to the best-fitting neighbour; both count as tag-led in
  the objective losses.

The spawn rules (`world_definition.spawn_strategy: similarity`) say which
places *may* open onto which; with `similarity_threshold: 0.5` that is almost
every pair. With `world_definition.story_map` every story draws its own
geography from them (or from the routes): each place opens onto `neighbors`
(3) of the places it fits, more fitting ones more often, joined into one
connected map. Discovery, route planning, mission choice and moving a stuck
traveler all follow it, so two stories of the same world find different paths
and each map stays consistent for the whole story. The map is drawn from the
story's seed, so storytellers compared on the same story share the same map.

```yaml
world_definition:
  story_map:
    enabled: true
    neighbors: 3
```

### Scenario templates

Places of one kind can share a template, with parameters for what differs.
Two places with the same template and the same parameters, or with the same
content, are refused as duplicate scenarios (also among places a language
model generates).

```yaml
world_definition:
  scenario_templates:
    district_market:
      tags: [urban, trade, "{district}"]
      entered_descriptions: ["The {district} market: {name}."]   # {name}: the place's own name
      actions:
      - name: Haggle in the {district} stalls
        outcomes: [{desc: A bargain., tags: [trade]}]
  locations:
    North Market: {template: district_market, parameters: {district: north}}
    South Market: {template: district_market, parameters: {district: south}, start_eligible: true}
```

A place's own fields win over its template's. A `template_id` without a
matching template only labels the scenario (as generated places do).

### Persistent variables

Persistent variables remember what happened in earlier playthroughs and change
the world of the next one. They are defined in
[`config/variables.yaml`](../config/variables.yaml):

```yaml
world_definition:
  variables:
    thief_caught: {type: bool, default: false}
    reputation:   {type: int, default: 0, min: -5, max: 5}
```

The types are `bool`, `int`, `float`, and `str`. `min` and `max` bound numbers.
The built-in, read-only `playthroughs` counts finished playthroughs.

**Change them with outcomes:**

```yaml
- desc: A short scuffle ends with the thief fleeing empty-handed.
  set_variables: {thief_caught: true}   # set a value
  change_variables: {reputation: -1}    # add to a number, clamped to min/max
```

**Switch things on or off** with `enabled_if` on a location, NPC, item, event,
action, dialogue line, or mission. In the shorthand below every comparison
must hold; [Conditions and effects](#conditions-and-effects) has the full
condition language (`any`, `not`, stats, items held, places visited, ...):

```yaml
- name: Thief
  enabled_if: {thief_caught: false}              # equals
- name: Memory Thief
  enabled_if: {playthroughs: {gte: 1}}           # only from the second playthrough on
Festival of Masks:
  enabled_if: {reputation: {gte: -2}}            # eq, ne, gt, gte, lt, lte, in, not_in
```

- **What a disabled entry means.** A disabled location cannot be reached by
  travel, discovery, spawning, leads, automatic transfer, or the mission
  planner. The story never starts in one. Disabled NPCs and items do not
  appear. Disabled missions are not drawn. Unique NPCs keep the same home
  across playthroughs.
- **Changes apply at once.** When an outcome changes a variable, the current
  scene is re-evaluated. A newly enabled NPC appears without leaving the
  location, and what the player already discovered stays.
- **Mission goals** can use them too:
  `goal: {variables: {reputation: {gte: 1}}}`. The planner routes the player
  to where an outcome changes that variable.
- The transcript notes each change as
  `[PERSISTENT] reputation: 0 -> 1` (message `variable_changed`; set it to
  `""` to hide changes).

**Storage.** Values are kept per player profile in a JSON file:
`persistence.path` (default `saves/persistent_variables.json`, which git
ignores), or `$ANE_SAVE_DIR/persistent_variables.json` (in Docker,
`/app/saves` on the `narrative-saves` volume). Writes are atomic.

- **Only real play saves.** That means the studio game (after a simulation or
  a loaded engine) and the terminal story at the end of `python main.py`.
  Changes are saved as they happen (`persistence.save_on: change`, or `end`),
  and a finished story counts as a playthrough.
- **Simulations and evolution never write.** By default they play with the
  default values, so runs stay reproducible
  (`persistence.simulate_with: defaults`). Set `stored` to evolve for the
  player's current world instead (read only).

**Shipped example**, across playthroughs:
- Beating the Market thief in a fight sets `thief_caught`. From then on the
  Thief is gone, together with the two missions that need him.
- Making him confess sets `fence_exposed`. A new **Fence's Ledger** then
  appears in the Sewers, and "Expose the fence" is no longer offered.
- Blessings raise `reputation`; theft, bribes, and fighting guards lower it.
  Below −2 the Festival of Masks turns the traveler away, and below 0 a
  **Clear your name** mission becomes available.
- The **Memory Thief**, who "remembers meeting you in futures that never
  happened", appears from the second playthrough on.

In a five-playthrough run with one profile, the first playthrough exposed the
fence. From the second on, the ledger and the Memory Thief appeared and the
fence mission was no longer drawn.

**Where to edit:**
- **Studio:** **Simulation settings → Persistent variables** has the
  definitions editor (validated, comments kept), the stored values of the
  profile, and **Reset stored values (new player)**. **Create the world → Enabled
  if** covers locations, NPCs, items, and events, and the scene lists the current values.
- **REST API:** `GET|PUT /v1/variables` (definitions), `GET /v1/variables/values`,
  `POST /v1/variables/reset` (optional `?profile=`), and `enabled_if` on entities.
- **Terminal editor:** **e. Enabled if** on any entry, and
  `set_variables` / `change_variables` in outcome effects.

Undefined variables, unknown comparisons, and values of the wrong type stop
the world build with a clear message. The studio and the API reject them when
saving.

### Conditions and effects

Everything in the world that can be switched on or off uses **one condition
language**, evaluated by one function
([`core_engine/conditions.py`](../core_engine/conditions.py)). The same condition
therefore gives the same answer wherever it is written. Everything a story
can change is written as **one effect list**
([`core_engine/effects.py`](../core_engine/effects.py)).

**Where conditions go** (`enabled_if` everywhere):

| On | While the condition does not hold |
| --- | --- |
| Location, NPC, item | It cannot be reached, does not appear, is not on show |
| Event | Its trigger actions are not offered, its encounters wait, nothing chains into it |
| Action (location, NPC, item, event) | It is not offered (`required_object`, `required_tag`, and `required_coins` still work) |
| NPC dialogue line | The next line is tried |
| Mission | It is not drawn |
| Side quest (`enabled_if`, `start`, `fail_if`) | It is not offered / does not start / does not fail |
| Mission goal (`goal.condition`) | The mission is not complete |

**A condition** is a tree. `all`, `any`, and `not` combine conditions; the
leaves read the story:

```yaml
enabled_if:
  all:
    - variable: palace_access          # persistent variable (also `playthroughs`)
      op: eq                           # eq (default), ne, gt, gte, lt, lte, in, not_in
      value: true
    - any:
        - {stat: persuasion, op: gte, value: 60}   # character stat, 1-100 with item bonuses
        - {has_item: Royal Pass}
    - not: {npc_removed: Gate Guard}
```

| Leaf | Holds when |
| --- | --- |
| `variable: x, op, value` | the persistent variable compares as stated |
| `stat: s, op, value` | the character stat (with item bonuses) compares as stated |
| `resource: coins` / `health` / `turn`, `op, value` | the player's coins, health, or actions taken |
| `has_item: X` | X is in the inventory |
| `item_collected: X` | X was collected or received in this story, even if it was used since |
| `visited: L` / `at_location: L` | L was reached in this story / the player is there now |
| `npc_removed: N` | N was removed from the story |
| `event_done: E` | E was entered and resolved (any result) |
| `event_succeeded: E` / `event_failed: E` | E was resolved by an event action that succeeded / failed (stepping away is neither) |
| `action_done: A` | A was taken successfully (with or without its `(NPC) ` prefix) |
| `outcome_tag: t` | an outcome tagged t succeeded |
| `mission: m` | m is the story's mission |
| `talked_to: N` | the player dealt with N in this story (any action with them, talking too) |
| `side_quest_active: q` / `side_quest_done: q` | the side quest q is under way / was completed in this story |
| `side_quest_failed: q` / `side_quest_declined: q` | the side quest q failed / was turned down, let go or withdrawn in this story |

**Shorthand.** Everything written before trees existed still works: a mapping
of variable names is `all` of its comparisons, and a list is `all` of its
items. `{thief_caught: false, reputation: {gte: 2}}` is the same condition as
the tree with two `variable` leaves. The mission goal keys (`action`,
`location`, `item`, `event`, `npc_removed`, `outcome_tag`, `coins`,
`variables`) are shorthand for leaves too. Condition keywords (`all`, `any`,
`not`, `variable`, `stat`, `visited`, ...) cannot name a variable.

**Effects** are lists, run in order:

```yaml
effects:
  - set_variable: {palace_access: true}    # persistent variables, clamped to min/max
  - change_variable: {reputation: 1}       # numbers only
  - set_stat: {persuasion: 60}             # character stats for this story (1-100)
  - change_stat: {persuasion: 5}
  - give_item: Royal Pass                  # a name or a list of names
  - take_item: Bribe Purse
  - change_coins: -5                       # never below 0
  - change_health: 1                       # between 0 and max_health
  - remove_npc: Thief                      # true: the NPC who acts
  - reveal_npc: Priest
  - reveal_item: Fence's Ledger
  - reveal_location: Vault                 # opens a route from the current location
  - unlock_location: Dungeon               # a confined player may leave
  - move_to: Palace
  - send_to: Dungeon                       # one way
  - start_event: bandit_ambush             # once the current action is resolved
  - complete_mission: true                 # or the mission's id
  - end_story: You sail into the sunrise.  # the story ends with this text
```

They go on outcomes (`effects:`, run before the outcome's other fields), on
NPC dialogue lines, and on missions (`rewards:`, applied when the mission is
completed). An outcome's `set_variables`, `change_variables`, and
`change_stats` are shorthand for the matching effects and go through the same
code. Game code can call `state.apply_effects([...])`.

```yaml
# config/npcs.yaml: what an NPC says when talked with is the first line whose
# enabled_if holds; without one, the NPC's usual descriptions.
- name: Priest
  dialogue:
    - enabled_if: {reputation: {gte: 2}}
      text: The whole city speaks well of you, child. Walk in peace.
    - enabled_if: {any: [{variable: reputation, op: lt, value: 0}, {has_item: "Fence's Ledger"}]}
      text: I hear troubling things about you. My door is open, should you wish to make amends.
    # A line may also carry `effects`, applied when it is said.

# config/missions.yaml
rid_the_market_of_the_thief:
  goal: {npc_removed: Thief}
  rewards: [{change_variable: {reputation: 1}}, {change_coins: 10}]
```

**Validation, and no partial changes.** Every condition is checked for its
structure, operators, references (variables, stats, items, locations, NPCs,
events, missions) and operand types: a `bool` variable cannot be ordered with
`gte`, an `int` cannot be compared with text, and `in` needs a list of the
variable's type. Every effect list is checked for references, value types, and
writable fields: `playthroughs` is read-only, only numbers change by a delta,
and stats are set within 1-100. Nothing is applied until the whole list has
passed:

- **World build and upload:** `build_world` checks every condition and effect
  list of the world before building anything, and a combined configuration
  upload is checked the same way before it is activated.
- **Studio and API saves:** actions, missions, and `enabled_if` are validated
  before the YAML file is written.
- **During play:** `apply_effects` and every outcome validate the list before
  the first effect. A bad list raises `EffectError` and leaves variables,
  stats, inventory, coins, health, location, history, and the save file as
  they were.

**What the story does with them.** Effects can matter for a mission goal: the
route planner sees what an outcome's effects write (variables, items, places,
NPCs, events, coins), and a `goal.condition` is planned toward any action
that changes something it reads. In worlds where a location, NPC, or item
depends on more than persistent variables (`has_item`, `visited`, ...), the
scene and the planner re-evaluate after every change of the story, not only
when a variable changes. The shipped world uses conditions only on variables
there, so it pays nothing for this.

**Shipped examples:** the Priest's dialogue above, and the reward for ridding
the market of the thief.

**Where to edit:** `enabled_if` in the studio's **Create the world** tab (any
condition as YAML, events included) and on the entity API; action
`enabled_if` and outcome `effects` in **Actions & outcomes** and
`PUT /v1/entities/{kind}/{name}/actions`; mission `rewards` and
`goal.condition` in **Missions** and `PUT /v1/missions`. Dialogue lines are
written in [`config/npcs.yaml`](../config/npcs.yaml) and checked when the world
is built or uploaded.

### Character stats and skill checks

The fixed combat model (attack and defence from weapon and armour tags, a
single power number, `combat_resolution` weights) is replaced by **character
stats the writer defines** and **skill checks** resolved by a combat system.

**Define the stats** in [`config/stats.yaml`](../config/stats.yaml), with any
names. The story decides:

```yaml
world_definition:
  character_stats:
    stats:                         # a combat story...
      attack: {description: Landing blows and winning fights.}
      defence: {description: Taking a hit; softens combat damage.}
      stealth: {description: Moving unseen.}
      persuasion: {description: Talking and bribing your way through.}
    # ...or a social one: stealth, persuasion, watchfulness, intimidation
    allocation:
      total_points: 50             # every player spends exactly this
      min_points: 5                # per stat
      max_points: 20
      value_per_point: 5           # points -> 1-100 skill value (5..20 -> 25..100)
    mode: random                   # random | defined | balanced
    defined_points: {attack: 13, defence: 13, stealth: 12, persuasion: 12}
    tag_checks: {combat: attack, stealth: stealth}   # outcome tag -> stat to check
    damage_reduction_stat: defence
```

**`CharacterStats`** ([`core_engine/stats.py`](../core_engine/stats.py)) holds
one player's stats. The player chooses their points (`defined`, validated
against the budget and the per-stat limits), or gets a `random` split within
the limits (seeded, so reproducible), or a `balanced` one. Held items add
`stat_bonus: {attack: 15}`, and outcomes change stats for the rest of the
playthrough with `change_stats: {persuasion: 5}`. Values always stay within
1–100.

**`CombatSystem`** ([`core_engine/combat.py`](../core_engine/combat.py)) resolves
every check and fight:

- **The check.** It rolls 1–100. The check passes when the roll is at or below
  the stat's value (with item bonuses), plus the action's modifier, minus the
  difficulty. The transcript shows each check, e.g. `[CHECK] persuasion: rolled
  34, needed 60 or less - passed.`
- **Which checks apply.** An action can require one, and its outcomes can be
  written for each result:

  ```yaml
  - name: Interrogate the thief
    skill_check: {stat: persuasion, modifier: -10}
    outcomes:
    - {desc: The thief talks., on_check: pass, set_variables: {fence_exposed: true}}
    - {desc: The thief slips away., on_check: fail, remove_npc: true}
  ```

  Without a `skill_check`, an outcome whose tag appears in `tag_checks` (e.g.
  `combat`) needs a check of that stat, so existing content uses stats without
  edits. A failed check plays the `on_check: fail` outcome when there is one.
  Otherwise the generic failure applies: a health penalty, or an arrest by
  guards.
- **Combat consequences.** Combat damage is reduced by 1 per
  `damage_mitigation_per_point` (25) of the `damage_reduction_stat`. At
  `failure_mitigation_threshold` (80) or above, failed dangerous actions cost
  no health.
- **The AI uses it.** Outcomes are scored with their real pass chance, and
  `action_selection.skill_fit_weight` makes the AI prefer checks its
  character is likely to pass. A stealthy character sneaks, and a strong one
  fights.

**Skill check resolution** replaces *Combat resolution* in the **Weights** tab
(`skill_check_resolution` in `hyperparameters.yaml`):

| Setting | Effect |
| --- | --- |
| `difficulty_penalty` | Added to every check. Positive makes checks harder, negative easier. |
| `difficulty_rate_per_step`, `difficulty_rate_start_step` | Difficulty change per story step from that step on. Positive gets harder over time, negative easier. |
| `min_chance`, `max_chance` | The pass chance never goes below or above these percentages (5 and 95). |
| `damage_mitigation_per_point`, `failure_mitigation_threshold` | How the damage-reduction stat softens combat. |

Shipped content:
- **Item bonuses.** The Steel Dagger gives +15 attack, the Heavy Shield +20
  defence, the Rusty Sword +5 attack, the Vintage Wine +5 persuasion, and the
  Fence's Ledger +10 persuasion.
- **Explicit checks.** Bribing the gate guard is a persuasion check,
  interrogating the thief a persuasion check at −10, and running from a bandit
  a stealth check.
- **Stat changes.** The priest's blessing gives +5 persuasion.

Over the same 100 stories, a stealth-and-persuasion build won 97%, a
balanced one 76%, and an attack-and-defence build 64%. The brawler made the
most attack checks: 80, against 42 for the balanced build. The shipped world
rewards stealth and talk more than fighting, because most fights first need
a weapon.

**Where to set it:**
- **Studio:** *🌍 World → 📜 Story rules → Character stats* (definitions);
  *▶ Play → Character* and *My own stat points* (story default, random,
  balanced, or your own points, with **Randomize**/**Balance** and a live
  points check); *Create the world* → item **Stat bonus while held**; and
  `skill_check`, `on_check`, and `change_stats` in the actions editor.
- **REST API:** `GET|PUT /v1/stats`, `POST /v1/stats/allocate` (check or
  preview points), `stats` / `stats_mode` on `POST /v1/simulations`,
  `stat_bonus` on items, and `stats`, `stat_points`, and `last_check` in the
  play state.
- **Terminal editor:** action **8. Skill check**, outcome effects `on_check` /
  `change_stats`, and item **s. Stat bonus**.

Undefined stats, invalid `on_check` values, impossible budgets, and invalid
points are rejected with a clear message.

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
