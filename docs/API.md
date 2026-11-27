# Adaptive Narrative Enabler HTTP API

The Compose stack runs the Gradio studio and the REST API as separate services
from the same image. The API holds the persistent configuration, artifact,
records and model cache volumes; the studio holds none.

**Public deployment:** <http://ane.hamlet.iti.gr> (interactive documentation at
<http://ane.hamlet.iti.gr/docs>, ReDoc at `/redoc`, health at `/healthz`). It is
the `api` service of `compose.yaml` (port 8005) behind the reverse proxy;
`ANE_PUBLIC_URL` (Compose: `http://ane.hamlet.iti.gr`) lists it in the
documentation's server list beside the server it is opened on.

The studio is a client of this API and does everything through it over HTTP:
every tab works through the endpoints below, and whatever it does, other
clients can do too. It finds the API at `ANE_API_URL` (Compose:
`http://hamlet-ane-api:8005`; any other API, e.g. the public one, works too) or,
when that is unset, at `http://127.0.0.1:8005`. It never serves the API itself,
so the API must be running before the studio starts.

**Building another frontend?** [FRONTEND.md](FRONTEND.md) describes every studio view as
requests and JSON fields: what each screen loads, its components, its actions, the client-side logic
to reimplement, and an index of which view uses which endpoint.

| URL | Purpose |
|---|---|
| `http://ane.hamlet.iti.gr/docs` | The public deployment's interactive documentation |
| `http://localhost:8003` | Adaptive Narrative Studio |
| `http://localhost:8005/docs` | Interactive Swagger documentation |
| `http://localhost:8005/redoc` | ReDoc API documentation |
| `http://localhost:8005/healthz` | API container health check |

## Run with Docker

```bash
docker compose up --build
```

Open the UI at <http://localhost:8003> and API documentation at
<http://localhost:8005/docs>. Edited configuration, exported simulation
artifacts, and Hugging Face models use persistent named volumes. Set
`ANE_CONFIG_VOLUME=./config` before starting Compose if you specifically want a
host bind mount (its files must be writable by container UID 10001). The service intentionally uses one Uvicorn worker because runtime
configuration overrides and active games live in memory.

To build and run without Compose:

```bash
docker build -t adaptive-narrative-enabler .
docker run --rm -p 8005:8005 adaptive-narrative-enabler
docker run --rm -p 8003:8003 \
  -e ANE_UI_PORT=8003 -e ANE_API_URL=http://<api-host>:8005 \
  adaptive-narrative-enabler python app.py
```

Without `ANE_API_URL` the studio looks for the API at `http://127.0.0.1:8005`;
`-e ANE_API_URL=http://ane.hamlet.iti.gr` runs a studio on the public API.

## How the API behaves

### One stateful process

The API keeps games and recent work in its memory, so it runs as **one
process with one worker** (Compose does this). A second worker would not see
the first one's games in memory. Play sessions are also saved as they are
played ([Saved games](#saved-games)), so a restart does not lose them; the
rest of what lives in memory is lost:

| In memory (lost on restart) | On disk (kept) |
| --- | --- |
| The play sessions being played: the newest `max_web_sessions` (100); beyond that the oldest is let go (it comes back from its save when it is used) | Play sessions, saved after every change, and save slots (`ANE_SESSIONS_DIR`, default `ANE_SAVE_DIR/sessions`) |
| | Configuration: `config/*.yaml` and the app parameters `config/ui_settings.yml` |
| Recent runs that can be exported (the latest 20) | Player profiles' persistent variables and the learning memory (`ANE_SAVE_DIR`) |
| Background jobs (the latest 100), including queued and running ones: a restart loses them, and no job is resumed | The records database and engine model files (`ANE_RECORDS_DIR`) |
| Download links `/v1/artifacts/{id}` (the files stay in `ANE_ARTIFACT_DIR`, the links do not) | Generated configurations, engine models and exports (`ANE_ARTIFACT_DIR`) |
| An uploaded configuration (`POST /v1/config/upload`), until `POST /v1/config/reset` or a restart | |

The studio (`app.py`) is a client of this API: its games, runs and jobs are the
API's, and it holds none of this state itself.

### Writes: checked first, then saved whole

- **Shape and ranges.** A request with a missing field, a wrong type, or a
  value out of range (for example `candidate_count: 401`) is refused with
  `422`. Its `detail` is a list of `{loc, msg}`.
- **Content.** A well-formed request that does not fit the world is refused
  with `400` and a message listing every problem, and nothing is written.
  Examples: an unknown location or event, a broken condition, invalid YAML, an
  unknown app parameter, or a run above the work limit.
- **Atomic.** Files are replaced in one step (a temporary file, then a
  rename) under one lock. Concurrent writes do not interleave, and an
  interrupted write leaves the previous file.
- **Immediate, for what starts afterwards.** After a save the configuration is reloaded (an uploaded
  configuration, or a trained engine's, in use until then is set aside, and the message says so). New
  games, training runs and evaluations, and jobs still queued (a job reads the configuration when it
  starts), use it. Nothing running changes: a game in progress or a running job keeps the configuration
  it started with (its own frozen engine context, [docs/AI.md](AI.md#engine-contexts-which-configuration-a-game-reads-and-when-settings-take-effect))
  until it ends; a game with a trained engine runs with that engine's world, tuning and tag vectors,
  and starting one never changes another session. Every save's `message` says this
  (`backend/activation.py`; the forms' note is `GET /v1/reference`'s `activation.configuration`). App
  parameters (`PUT /v1/app-settings`) apply only when the API, or the studio, restarts.

### Status codes

| Code | When |
| --- | --- |
| `200` | Done; the body is the result |
| `202` | A job was queued (`POST /v1/jobs/...`); poll `GET /v1/jobs/{id}` |
| `400` | Refused content (see above), a file of the wrong type, an engine file that cannot be loaded, a choice that is not on offer, a deterministic run without a seed |
| `404` | Unknown entry, document, action index, play session, job, artifact, or recorded run, world, engine or story (also when playing, continuing or retraining it, before any work starts). While no records database exists, the records endpoints answer `404` ("No records yet") |
| `403` | Author diagnostics switched off (`ANE_AUTHOR_DIAGNOSTICS=off`: `GET /v1/sessions/{id}/decisions`) |
| `409` | An export for a platform this API has no runtime for, or whose runtime was built from other engine code (`GET /v1/exports/platforms` says why); an ANE AI export without a complete engine (`GET /v1/exports/engines`); a choice from options the game has moved past (`stale_state`) or not offered now (`action_unavailable`); a saved session that cannot be restored here (`session_unrecoverable`); a `PUT /v1/weights` whose `expected` values are out of date; a restore or a report that conflicts with the game's state; augmentation switched off; cancelling a job of another kind |
| `413` | An upload above its limit: configuration 2 MB (`max_config_upload_bytes`), engine 20 MB (`max_engine_upload_bytes`), model parameters 100 KB |
| `422` | The request's shape, types or ranges |
| `503` | No language model available (the judge, world suggestions and augmentation) |

Errors are JSON: `{"detail": "..."}` (or the list of `422`). They never show the server's file
paths (where the records, saves and models are): only a file's name, when it matters.

### Runs: limits, work, and waiting

0. **Defaults and overrides.** A run's settings are the saved engine defaults (`evolution.pop_size`,
   `generations`, `episodes_per_genome`, `max_steps`, `warm_start`, `full_length_stories`;
   `reproducibility.deterministic` and `seed`; `narrative_end.max_actions`) with what the request sets
   for itself: `candidate_count`, `generations`, `episodes`, `simulation_steps`, `deterministic`, `seed`,
   `warm_start`, each optional (left out or `null`: the default). `POST /v1/simulations/resolve` with
   the same body answers, before anything runs, with what the run would execute: `defaults`,
   `overrides`, `sources` (each value: `override` or `default`), `effective` (`population`,
   `generations`, `stories_per_storyteller`, `requested_steps`, `story_turns`, `full_length_stories`,
   `story_length`, `deterministic`, `seed` (`null`: random), `warm_start`, `warm_start_mode`: memory,
   engine or off), `work` (as `GET /v1/simulations/work`), `notes` (values held to the app's limits,
   a seed a random run does not use), `errors` (a deterministic run without a seed), `ready` and
   `remembered`; with `engine_id`, a retraining of that recorded engine (its saved defaults, a warm
   start from it). `POST /v1/simulations`, the job, and a retraining run exactly `effective`, and
   their response's `training` gives `overrides`, `sources`, `resolved` and `executed` (read back
   from the evolution that ran). A request that left out `candidate_count`, `generations` or
   `simulation_steps` used to get 8, 5 and 16; it now gets the saved defaults.
1. **Maximums.** One run takes up to 400 storytellers
   (`candidate_count`), 1000 `generations`, 400 `simulation_steps`, and 10
   `episodes`; above that is `422`.
2. **Studio limits.** A run is then held to the app parameters
   `candidate_limit`, `generation_limit`, and `simulation_step_limit`
   (by default the maximums). A request above a lowered limit runs at the
   limit; it is not refused.
3. **Story length and work.** A story lasts `narrative_end.max_actions` turns at most (at least
   100; fast-travel turns, `travel.max_free_turns`, come on top). With
   `evolution.full_length_stories` (on by default) every training story is played to that end and
   the `simulation_steps` asked for do not change it; without it a story is played for
   `simulation_steps` turns, though none outlasts its end. Storytellers × generations × stories per
   storyteller × the turns a story can take must stay within `max_simulation_work` (1,000,000
   simulated turns by default, an app parameter), an upper bound since many stories end sooner. A
   run above it is refused with `400` before anything runs (the refusal shows the multiplication
   and why a story counts as many turns as it does).
   `GET /v1/simulations/work?candidate_count=&generations=&simulation_steps=&episodes=` answers
   beforehand with the same numbers, so a client never computes them itself:
   `requested_steps`, `effective_max_turns` (the most turns a story can take: its length plus
   `fast_travel_turns`, turns of fast travel that do not count toward its end (`travel.max_free_turns`,
   0 with fast travel off), at most the steps training plays; `story_turns` is the same, for older
   clients), `full_length_stories`, `story_length`, `fast_travel_turns`, `steps_decide` (whether the
   steps set `effective_max_turns`), `override` (`{setting, reason}` when they do not: the setting
   that does, `evolution.full_length_stories` or `narrative_end.max_actions`, and why), `factors`
   (`storytellers`, `generations`, `stories_per_storyteller`, `turns_per_story`), `work`, `limit`,
   `allowed` (exactly when the run would start) and `over_by`.
4. **Reproducible.** `deterministic: true` uses the seed of the settings; the
   same configuration and seed give the same evolution.
5. **A game after training, or not.** `POST /v1/simulations`, its job, and the records' `continue`
   and `retrain` (and their jobs) start a game with the best storyteller, as before, unless the
   request sets `start_game: false`: then the training only trains. No session is created and
   nothing is written to the player's profile (no start, no mission, no play story); the response
   has `game_started: false`, `session_id: null` and empty play fields, with the run, its engine
   (`engine_id`), its files, `learning` and `training` as usual. The studio's 🧠 Train asks this way;
   the engine is then played with `POST /v1/engines/{engine_id}/play`, judged with `engine_id` in
   the judge jobs (a finished run kept without the records too) and exported with
   `POST /v1/exports {"source": "engine", "engine_id": …}`.
6. **Waiting.** `POST /v1/simulations` and the records' `play`, `continue` and
   `retrain` answer when the work is done, which can take minutes, so give
   the client a long timeout. The job endpoints answer at once (`202`) and
   report progress per generation. Jobs run one at a time in the order they
   arrive; synchronous runs do not wait for them.

### Play

- A game is a session (`session_id`). `POST /v1/sessions/{id}/actions` takes
  one of the choices the last response offered: by its stable id,
  `{"action_id": "<choices[].id>", "state_version": <state_version>}`, or as before by
  its name, `{"action": "<one of actions>"}` (when two choices share a name the last
  listed is taken). With `state_version`, a choice made from options the game has moved
  past (another tab, a UNP update) is refused with `409` and `detail: {code: "stale_state",
  message, state_version, offered}`; an `action_id` not offered now is `409`
  `action_unavailable`; nothing is done either way. A name not offered is refused with
  `400`, and an unknown session is `404`.
- `choices` describes each offered choice: `id`, `label` (and `name`, as before), `category`
  (place, person, item, event, talk, approach, leave, look, travel, fast_travel, side_quest: taking
  an offered side quest on, turning it down or handing a finished one over to its giver, ids
  `side-quest:<quest>:accept`, `:decline`, `:turn-in`, no turn), `owner`,
  `source` `{kind, name}`, `location`, `requirements` (item, item_tag, item_used, coins,
  consumes_item, collects_item, triggers_event: what applies), `check` (as before),
  `skill_check` `{stat, chance, bonus}`, `failure_risk` `{health, coins, sent_to}`,
  `available`, `availability`, `repeat`, `one_time`, `takes_turn` and, for approaching
  someone, `members` (their choices' ids). `state_version` changes whenever the game moves.
  Ids: [docs/WORLD.md](WORLD.md#action-identities).
- **Author diagnostics:** `GET /v1/sessions/{id}/decisions` explains every action now: `status`
  (shown, hidden, blocked), `stage` and `reasons` (`code`, `message`, `detail`) for the source
  entity (absent, not visible, gone, out of the cast), conditions (`enabled_if`, `until`, with the
  failing parts and their values), missing items and coins, one-time and per-visit use and
  exclusive choices, events, encounters and conversations, the storyteller's `rank` and `score`,
  the relevance filter and the choice-count `limits`; `entities` says where every NPC, item and
  event stands, `encounters` says why a dynamic encounter could or could not come now (the
  director's `gate`, each template's status and chance, the factions' standing, and `codes`: what
  each gate and status code means; docs/WORLD.md "Dynamic encounters"), `side_story` says what the
  director weighs before letting side content in (`hold`: why none may come now, or null; the main
  quest's stage, idle turns, climax and finale; open threads; the player's taken and turned-down
  offers; danger and recent attacks; the storyteller's side-quest `pull`; every side quest and
  encounter template as a `candidate` with its `relevance` to the main story, `fit`, `appetite` and
  `score`; the last introductions and what optional threads led to; `codes`; docs/AI.md
  "Side-story orchestration"), `relationships` shows people and factions as only authors see
  them (every NPC's, or for a generic NPC every copy's (`place`), `trust`, `hostility`, `attitude`,
  `disposition`, `faction`, `goals` with how they advance and their progress, `memories`, `related`,
  `never`, `home`, `kin` and `haunts` (place: presence); every faction's
  `standing`, `status`, `members`, `relations` and `reacts_to`; the reactions `due` with why each
  is held back (`blocked`), those `fired`, and the latest `deeds` and transitions `log`; docs/WORLD.md
  "People, factions and reactions"; the code `npc_refuses` marks a hostile NPC's withheld action),
  and `codes` lists every reason code of the actions. The trace also
  has `turn`, `location`, `mode`, `event`, `talking_to`, `tension`, `limits`, `shown`, `counts`
  and `notes`, and each action its `id`, `label`, `category`, `key` and `content_ref` (the OpenAPI
  model `DecisionTrace` lists them all). It is made on a copy of the game: no turn,
  no dice drawn, no encounter started, nothing saved. Players never see it (it is not in
  PlayState); a server that gives players the API sets `ANE_AUTHOR_DIAGNOSTICS=off` (`403`).
- `actions` are the most relevant of the storyteller's options
  (`player_options`): what the moment is about first (a prisoner's way out,
  the quest's next step, a side quest's next step, the payoff of an open clue,
  a way on), then the rest
  unless they rank among the lowest, at most 3 ways on and 2 conversations.
  There are at most `limit` (6), and fewer in tense moments (`focus_limit`, 4),
  so their number varies from turn to turn. They are listed in the scene's
  order (`player_options.order: world`), not best first. An event's choices are
  all offered.
- Every start of a game (`POST /v1/simulations`, `/v1/jobs/simulations`, the
  records' `play`, `continue` and `retrain`, and `POST /v1/engines/load?mission=`)
  takes an optional `mission`: the key or title of the quest to play. Left out,
  the story draws one. `GET /v1/missions/playable?profile=` lists the world's
  quests with their stages and whether each is open to the profile now
  (`available`, from its `enabled_if`). An unknown quest, or one not open to
  the profile (for example one it already completed), is refused with `400`.
- Moves follow the world's map (`world_definition.routes`, or each story's
  own map): a far destination is reached through the places between, one
  turn each (`[ROUTE] …`).
- With the director (`director` in the configuration), the last
  `finale_actions` turns are a finale, and a story that reaches the action
  limit closes with an epilogue (its record's `end_kind` is `epilogue`) instead
  of being cut off. Damage grows as the story escalates, so a player can die.
- Every choice with a check says what is at stake before it is taken
  (`choices[].check`: `stat`, `chance`, `bonus`, and `risk`: `{health, coins,
  sent_to}` that failing costs, or `null`). `stakes` is the watch's alert level
  (`{alert, max, warn_at}`; at `max` a manhunt starts), `null` with
  `stakes.alert` off. `people` and `people_met` carry each NPC's `role` and
  `importance` (0-3) in this story (`npc_roles`); someone who matters may stay
  out of sight until a lead, the road or enough turns bring them in. They also
  carry `disposition` (`hostile`, `wary`, `neutral`, `friendly`, `ally`) and
  `faction` (`null` for a faction that is not public): what a player may know.
  Trust, hostility, memories and goal progress are for authors (the decision
  trace's `relationships`).
- A quest's last step is a showdown (`climax`): an event whose choices are
  offered like any other; the story is won when it is won (or, after setbacks,
  at a price). Fast travel (`Travel by known ways to …`) reaches a visited place
  in one turn that does not count toward the action limit (`travel`).
- **Sessions and the save game.** Every session starts with a start and a goal (`location`,
  `quest`); a profile's starts vary (`start_selection.recency_factors`: the latest start
  is the least likely) and an achieved goal leaves the profile's pool (each goal once per
  save game; `GET /v1/missions/playable` marks it `achieved`). `cast` says what this
  session leaves out (`session_scope`: what its goal does not need), `unique_actions` every
  attempt at a one-time action (`succeeded`, `kept`: a failed retryable attempt stays on
  offer, anything else is used up). When a session ends, by itself or with
  `POST /v1/sessions/{id}/end` (the player stops), the player's progression is saved and
  its `session_report` (goal and `outcome`: `achieved` or `failed`, the player's stats, inventory,
  health and coins, the goals achieved in the save game) is sent to the UNP:
  `session_report.url` or `ANE_UNP_URL` (POST, JSON; `ANE_UNP_TOKEN` adds a bearer token).
  `GET /v1/sessions/{id}/report` returns it (`409` while the session goes on),
  `GET /v1/progression?profile=` the save game (goals achieved, playthroughs,
  `side_quests_completed`, session reports with their `delivery`), and `POST /v1/progression/reports/{id}/send?profile=`
  sends a kept report again.
- `POST /v1/engines/{id}/play` (`{"profile", "mission", "stats_mode", "stats"}`) starts a game in
  the world a finished run was trained on, with its storyteller (`latest`: the newest; the
  finished runs are `GET /v1/exports/engines`, kept also without the records). The game runs in
  the engine's own context (see *Games with a saved engine* below); `409` before any run has
  finished. `GET /v1/sessions/{id}/transcript` is the story so far, and
  `GET /v1/sessions/{id}/journal` the game turn by turn as a player follows it: each turn's choice
  (`action`, `with` whom, `where`), its check (`stat`, `roll`, `needed`, `passed`), what came of it
  (`told`, `succeeded`), what it changed (`health_change`, `coin_change`, `items_gained`,
  `items_lost`, `moved_to`, `new_places`, `met`, `event`) and the quest (`quest_done`: the steps it
  completed, `quest_now`, `quest_complete`), with its `turn`, `health`, `coins`, `ended` and
  `end_reason`, the side quests' moments (`side_quests`) and the dynamic encounter that began that
  turn (`encounter`: `{event, category, faction, npc, result}` or null). Every play state says the last one (`turn_summary`)
  and who the player is talking with (`talking_to`), and where the player is, for a client's own
  place card: `scene_name` (the place, or an event at it), `description` (as the player sees it
  now) and `items_here` (`[{name, text}]`, the items to be seen).
- A session as a UNP sees it, as the exported ANE AI does ([UNP.md](UNP.md)), so an
  integration can be tried here: `GET /v1/sessions/{id}/state` (its parameters,
  `ane.session_state/1`), `PATCH /v1/sessions/{id}/player` (set `stat_points`,
  `stat_changes`, `inventory`, `add_items`, `remove_items`, `coins`, `health`, `max_health`,
  `variables`; set, not played), `POST /v1/sessions/{id}/sync` (what happened in the UNP's
  game: one event or `{"events": [...]}`: `action_done`, `item_gained`, `item_lost`,
  `npc_removed`, `location_visited`, `coins_changed`, `health_changed`), and save games:
  `GET /v1/sessions/{id}/savegame` (`ane.savegame/1`; the session goes on) and
  `POST /v1/sessions/load` (`{"savegame", "profile", "session_id"}`: a new session that goes
  on exactly, `restored: exact`, or is rebuilt from the parameters, `restored: parameters`
  with a `note`; a save game without a snapshot takes the storyteller of `session_id`). A save
  game made here names its engine and the configuration its session ran with (`engine.engine_id`,
  `engine.context_key`), and continues in that configuration, not in the studio's defaults. If
  this server does not keep that configuration, the save game continues in the engine's own, then
  in the configuration of the `session_id` session, and only then in the studio's defaults. A save
  game of the ANE AI loads here, and one from here in the ANE AI. Fields that do not fit the
  world answer `400`. `GET /v1/world/catalogue` is the world as the ANE AI sends it
  (`ane.world/1`: locations, NPCs with their `npc_roles`, items, events, missions,
  `side_quests` and `factions`). The parameters also carry `side_quests` (`{key: {status, stage, ready,
  turns_left}}`) and `encounters` (`{attacks, factions, rewards_paid}`: the dynamic encounters
  met, the factions' standing changes and the rewards already paid) and `relationships`
  (`{npcs: {npc or npc@place: {trust, hostility, disposition, memories}}, kin: {npc: {trust,
  hostility}}, goals: {npc: {progress, done}}, moved, haunts, fired, due}`: what people think of the
  player (a generic NPC per copy, and what word reached the others), their goals, where they moved,
  the reactions delivered and still due), so a session rebuilt from them keeps them.
- A finished story has `ended: true` and offers no more actions.
- Real play saves persistent variables to its `profile` (`save_on` in the
  story rules). Evaluated stories during evolution do not
  (`simulate_with`).

### Saved games

Every play session is saved after each change that matters (a turn, approaching or leaving
someone, `PATCH .../player`, `POST .../sync`, the end), so it survives a restart of the API and
can be continued, restored or restarted (`backend/persistence.py`).

- **The format.** Each save is an ordinary `ane.savegame/1` document (its parameters and an exact
  snapshot: the dice, the quest and its stage, NPC goals, allies and people met, the places and
  ways discovered, the event, encounter or conversation in progress, and the whole story). The
  configuration the session runs with (world, tuning and tag vectors) is kept once per engine
  beside it, so a session is restored under its own configuration whatever the API has loaded
  since. A save whose snapshot no longer fits (other engine code) is rebuilt from its parameters
  (`restored: parameters`, with a `note`); one that cannot be read at all answers `409`
  `{code: session_unrecoverable}` and is not played.
- **Restoring.** Any `/v1/sessions/{id}/...` route restores a session that is saved but not in
  memory (after a restart, or one let go beyond `max_web_sessions`).
  `POST /v1/sessions/{id}/continue` also says how it came back: `restored: live` (it was in
  memory), `exact` or `parameters`.
- **Whose it is.** A session belongs to the user who started it: `X-ANE-User` (an opaque id), or
  `test` for the studio (`X-ANE-Client: studio`). Only that user can use it; for anyone else it
  does not exist (`404`), and so do their saves. A session started without `X-ANE-User` is
  anonymous, as before: whoever has its id can use it, and it is never listed.
- **Listing.** `GET /v1/sessions?profile=&status=active|ended|abandoned|all&limit=` lists your
  sessions, the most recently played first: `{session_id, profile, status, location, turn,
  state_version, mission, won, engine_id, world_version, origin, created_at, updated_at, ended_at,
  in_memory, error}`. Listing needs `X-ANE-User` (or the studio): `400` without one.
- **Save slots.** `POST /v1/sessions/{id}/saves` `{"slot": "Before the gate"}` saves into your
  slot of that name (saving there again replaces it; 64 characters at most); without `slot`, a new
  manual save. `GET /v1/saves?profile=` lists your saves; `POST /v1/saves/{save_id}/restore`
  `{"profile"}` continues one as a new session (`LoadedState`; a save of a game that has ended
  answers `400`); `DELETE /v1/saves/{save_id}` deletes one. Slots need a user.
- **Restart.** `POST /v1/sessions/{id}/restart` starts the adventure again as a new session: the
  same engine (world, tuning, storyteller), profile, quest and character settings as they were
  asked (a quest that was drawn is drawn again). The old session becomes `abandoned` unless it has
  ended.
- **Writes.** Every save is written to a temporary file and renamed over the old one, so a reader
  never sees half a file. Saves of one session are serialised (a lock per session, and the index's
  write transaction across processes), and a newer state is never replaced by an older one
  (`state_version`). A failed write never stops the game: it is logged and kept in the session's
  `error`.
- **Retention.** Finished sessions (ended or abandoned) are removed once they are older than
  `ANE_SESSION_RETENTION_DAYS` (30), and beyond the `ANE_SESSION_KEEP_FINISHED` (20) most recent of
  each user. The policy runs by itself at most once an hour when a session ends, and on
  `POST /v1/sessions/cleanup` (`{"dry_run": true}` only says what it would remove). Active sessions
  and save slots are never removed by it.
- **What they return.** `GET /v1/sessions` answers `{user, sessions: [...]}` and `GET /v1/saves`
  `{user, saves: [SaveSlot]}`; a `SaveSlot` is `{save_id, slot, kind (named | manual), session_id,
  profile, engine_id, world_version, location, turn, state_version, mission, session_status,
  created_at, updated_at}`. `POST /v1/sessions/{id}/saves` answers the `SaveSlot`,
  `DELETE /v1/saves/{id}` `{deleted: SaveSlot}`, and `POST /v1/sessions/cleanup`
  `{policy: {retention_days, keep_finished}, removed, contexts_removed, dry_run}`. The clean-up
  applies the retention policy to every user's finished sessions (an operator's route: it needs no
  user).
- **Off.** `ANE_SESSION_PERSISTENCE=off` keeps sessions in memory only, as before; listing, save
  slots and restoring saves (`GET /v1/sessions`, `/v1/saves`, `POST .../saves`,
  `POST /v1/saves/{id}/restore`) then answer `400`. `POST /v1/sessions/{id}/continue` and
  `/restart` still work on a session held in memory.

### Records: who asked

When recording is on, every run, generation, engine, story, and turn is
recorded. The origin is `studio` for requests with the header
`X-ANE-Client: studio`, `api` for all other requests, and `terminal` for
`main.py`. Recording is best effort: if the records folder is not usable,
recording switches itself off and requests still succeed.

**Whose run it is.** Every run has a user (`runs.user_id`), the one it was made for:

- A request that starts a run through the API (`POST /v1/simulations`, `/v1/jobs/simulations`,
  `/v1/records/engines/{id}/continue|retrain`, `/v1/jobs/engines/{id}/continue|retrain`) must say
  whose it is in the header `X-ANE-User`: the id of the user as the backend that calls the API
  knows them. It is an opaque id (a UUID, an account number: 1-128 letters, digits and `. _ : -`),
  never a name or an e-mail address. Without one, or with another form, the request is refused
  with `400` and nothing runs.
- The studio's runs (`X-ANE-Client: studio`) and the terminal's are the user `test`, and so are
  the runs recorded before runs had a user.
- `GET /v1/records/runs?user_id=<id>` lists one user's runs; every run says its `user_id`.

```bash
curl -X POST http://localhost:8005/v1/simulations -H 'Content-Type: application/json' \
     -H 'X-ANE-User: 8f14e45f-ceea-467f-a8f5-b1c3e1c2d6aa' -d '{"candidate_count": 8, "generations": 5}'
```

### Files and browsers

- Generated files are offered as download links (`*_download_url`,
  `GET /v1/artifacts/{id}`), valid while the process runs.
- Uploads are recognised by their extension (`.yaml`/`.yml`, `.zip`) and
  read in chunks up to their limit.
- CORS is off unless `ANE_CORS_ORIGINS` lists the allowed origins (comma
  separated). It then allows `GET`, `PUT`, `PATCH`, `POST`, `DELETE` and the headers
  `Content-Type`, `Authorization`, `X-ANE-Client`, and `X-ANE-User`.

## Endpoint coverage

Every write is validated before anything is saved. Invalid requests return
`400` with the list of problems, and nothing is written.

Requests with the header `X-ANE-Client: studio` are recorded with the origin
`studio` (the studio sends it); all others with the origin `api`.

**Studio services** (what the studio is built on; any client can use them).
The studio (`app.py`) is a client of this API and reaches every endpoint, directly or
through an equivalent one (the YAML form of a JSON endpoint, the job form of a long
one); `tests/test_studio_coverage.py` checks it.
- `GET /v1/reference`: what an authoring UI needs: `entity_kinds`,
  `common_tags`, `action_reference`, `repeat_rules` and `repeat_default`,
  mission `goal_keys`, `rule_choices` and `rule_fields` (the story-rule form),
  `player_stat_choices`, the editable `documents`, the roles an NPC may play
  (`npc_roles`), the judge's criteria (`judge_criteria`), and the `defaults` of a new
  run (`episodes_per_genome`, `warm_start`, `profile`).
- `GET|PUT /v1/app-settings`: the studio's app parameters
  (`config/ui_settings.yml`: limits, defaults, queue, launch) with their
  `defaults`. A `PUT` (`{"settings": {...}}`) changes the parameters it names,
  validates all of them, and rejects unknown ones. They apply after a restart: the API reads its own
  when it starts, the studio its own. `running` is what this API process uses; `parameters` says, per
  parameter, `readers` (API, studio), `takes_effect`, `running_api`, `environment` (the studio's
  `ANE_UI_HOST`, `ANE_UI_PORT`, `ANE_UI_SHARE`, which set its launch settings instead of the saved
  values; only the studio sees them) and `pending` (the API runs another value than the saved one). A
  `PUT`'s `message` says that nothing running changes and what a restart of the API would use.
- `GET /v1/documents` lists the configuration documents edited as YAML text
  with their comments kept: `missions`, `side_quests`, `messages`, `variables`, `map` (the world's
  map, `world_definition.routes`: `{routes: {place: [neighbours]}}`, or `routes: null`
  for a map each story draws; every place must be a location), `stats`, and
  `stored_values` (a player profile's values, `?profile=`).
  `GET /v1/documents/{name}` returns `{"name", "yaml"}`;
  `PUT /v1/documents/{name}` with `{"yaml": "..."}` validates and saves it and
  returns the saved text and a message.
- `POST /v1/variables/reset/yaml?profile=` starts a profile over and returns its
  `stored_values` document.
- `GET|PUT /v1/entities/{kind}/{name}/actions/yaml`: the entry's actions as YAML
  text, validated like `.../actions`.
- `GET|PUT /v1/entities/{kind}/{name}/yaml`: every field of the entry as YAML, as
  stored: what `GET .../{name}` returns and everything else (an NPC's `dialogue`,
  `known_locations`, `known_objects`, `article`, `role`, `secret`; a location's
  distant views and exits; the actions). A `PUT` checks the whole world with the
  entry in place and writes nothing when it is invalid; the entry keeps its name
  (rename it with `PUT .../{name}`).
- `GET /v1/entities/{kind}/{name}/policy/yaml` and
  `PUT /v1/entities/{Events|Items}/{name}/policy/yaml`: an event's start rules or
  an item's uniqueness as YAML text, validated like `.../policy`.
- `GET|PUT /v1/entities/{kind}/{name}/actions/{index}/repetition`: how often
  action number `index` (from 0) is offered (`repeat`: `default`, `once`,
  `per_visit`, `always`) and whether a failure uses it up
  (`retry_on_failure`). The whole action list is validated before it is
  written; an unknown index is `404`.
- `GET /v1/entities/{kind}?fields=names` returns only the names.
- `GET /v1/conditions/reference` is what a condition can say in the current world, for a condition
  builder: `groups`, `operators`, and `facts` (`{fact: {refers_to, names}}`: `refers_to` is items,
  locations, npcs, events, missions or side_quests, with the world's `names` of that kind, or null for
  free text). It also gives `comparisons`: variables with their types, stats and resources.
- `POST /v1/conditions/validate` (`{condition, where}`; the condition may be its YAML text) checks a
  condition as every save checks it (structure, operators, references, operand types):
  `{valid, errors, empty}`. Nothing is saved.
- `POST /v1/entities/{kind}/{name}/preview` (the body of `PUT …/draft`; an empty body previews the
  stored entry; `?create`, `?simulate`, `?seed`) previews an entry as its unsaved draft makes it. It is
  read only: no configuration is written, no progression changes, nothing is recorded, no session
  advances. It returns `{name, sections, problems, static, simulated, simulated_error, saved: false}`.
  - `problems` lists what a save would refuse; the preview is still shown.
  - `static` is the content as written: `texts`, `facts`, `dialogue`, and `actions` with their
    `requirements` and outcomes (`text`, `effects`, `variants`).
  - `simulated` (with `simulate`) is a sample scene the engine plays in a separate process: `start`,
    `present`, `description`, `choices`, the entry's `actions` (each tried on its own copy of the
    state: `text`, `check`, `changes`), and `assumptions`, which spell out the sample state.
- `PUT /v1/entities/{kind}/{name}/draft` saves an entry's whole draft at once, as the studio's
  *Save changes* does. The sections are `fields` (as `PUT /v1/entities/{kind}/{name}` takes them; their
  `name` renames the entry), `entry_yaml`, `actions` (a list or YAML), `action` (`{index, action}`, where
  `index` equal to the number of actions adds one), `policy` (a mapping or YAML) and `details`.
  - They are applied, in order, to a copy of the stored entry; every field no section touches is kept.
  - The entry, and the world with it, are checked as each section's own save checks them, and the entry
    is written once.
  - When anything is wrong, `400` names every problem with its section and nothing is written: no
    section is saved without the others. Two sections that change the same data (`fields` and
    `entry_yaml`, or `actions` and `action`) are also `400`.
  - `?create=true` makes a new entry named `name` from its `fields`.
  - A rename (`fields.name`) renames what refers to the entry in the same save: every YAML key or
    string that is exactly its old name in the entry files, the missions, the variables and the
    tuning parameters (a place's `location_penalties`, `game_state.arrest_location`); the other files
    are edited in place, their comments kept. The reply's `references` counts them per file
    (`{"locations.yaml": 6, "npcs.yaml": 2}`), and `message` says so. When the old name also names
    something of another kind (an item and a place both called `Lantern`), nothing else is renamed,
    and a rename that would leave references dangling is `400`, saying why. `PUT
    /v1/entities/{kind}/{name}` with a new `name` renames the references the same way.
  - Returns `{name, saved, message, entity}`.
- `GET /v1/entities/{kind}?fields=summary` returns the names plus a light summary of each entry, in
  order: `{kind, names, entries: [{name, tags, summary, actions, facts}]}`. `summary` is the first
  description, shortened to 140 characters, and `actions` is how many it has. `facts` gives a few
  words on what it is: a start place, an NPC's role and whether it is unique or generic, a hostile
  encounter, an event's title, an item's stat bonus. The studio's content library is built from it,
  and the other `fields` values are unchanged.

**Jobs** (long work with progress)
- `POST /v1/jobs/simulations` (body as `POST /v1/simulations`),
  `POST /v1/jobs/engines/{id}/continue` and
  `POST /v1/jobs/engines/{id}/retrain` (bodies as the records endpoints) start
  the work in the background and return `202` with the job.
- `POST /v1/jobs/judge-benchmark` and `POST /v1/jobs/calibrate-fitness` (see
  **Evaluation**) run the judge in the background; their `progress` is
  `{generation: stories judged so far, total, label}`.
- `GET /v1/jobs/{id}`: `status` (`queued`, `running`, `cancel_requested`,
  `cancelled`, `finished`, `failed`),
  `progress` (`generation`, `total`, `label`: what is counted, `generation` or
  `stories judged`, plus a training's `best`, `mean`, `validation`, `improved`),
  `queue_ahead` (for a queued job, how many jobs run before it; null otherwise),
  and, when finished, `result` (what the synchronous endpoint returns: the
  playable game) or `error`. `GET /v1/jobs` lists the latest jobs without their
  results (`has_result` says whether there is one). Jobs run one at a time in the
  order they arrive; the latest 100 are kept (in memory). A submission identical
  to a job still queued or running (the same body, user and origin) is not queued
  again: it returns that job with `reused: true` (otherwise `reused: false`).
  `cancellable` says whether the job can still be cancelled. A `404` for a job
  that was submitted means it was lost: an API restart (jobs live in memory and
  are not persisted), or more than 100 newer jobs.
- `POST /v1/jobs/{id}/cancel` cancels a training (`simulation`, `continue`,
  `retrain`) or evaluation (`judge-benchmark`, `calibrate-fitness`) job; other
  kinds answer `409`, an unknown job `404`. Cancellation is cooperative:
  - a **queued** job is `cancelled` at once and never starts;
  - a **running** job becomes `cancel_requested` and stops at its next safe
    point: a training after its current generation, an evaluation before the
    next story is judged or once its stories are played (a play phase itself
    is not interrupted). It is then `cancelled`;
  - a cancelled job has no `result`. `partial` (`{progress, note}`) says how far
    it got; nothing partial is kept as a result. A cancelled training makes no
    engine, and its recorded run has the status `cancelled`;
  - if the work completes before it reaches a safe point, the job is `finished`
    with its result and a `cancel_note`;
  - an ended job is left as it is (`message` says so). The reply is the job,
    with a `message`. In every case the worker goes on to the next job.

**Configuration**
- `GET /v1/config` returns the complete merged active configuration.
- `POST /v1/config/upload` validates and activates a combined YAML upload
  (every section, plus every condition and effect list of its world; a
  rejected upload leaves the active configuration unchanged). A file may hold
  only a world: the sections it lacks come from the built-in configuration,
  and the message names them (see `examples/`). The upload is staged in a
  temporary file, validated, committed, then the staged file is removed. A
  `200` answer is `{message, settings, revision, accepted: true}`, where
  `revision` is the accepted world revision (the value `GET /v1/world/revision`
  now returns). Uploading the same file again is accepted again with the same
  `revision`, so a retry is safe. A rejected upload (`400` with the problems
  found, for example a `start_selection.default_location` that is not one of
  the world's places, or a route in `world_definition.routes` to a place the world does
  not have, named by both ends; the map is checked against the uploaded world's
  own places, never the built-in city's; `400` for a wrong file type; `413` for a file that is too
  large) leaves the active configuration and its revision unchanged. A
  temporary file that cannot be removed after the commit (for example while
  another process holds it open on Windows) does not fail the upload. The
  answer adds `cleanup_warning`, and the API logs it. A cleanup problem never
  replaces a rejected upload's error;
  `POST /v1/config/reset` restores the built-in file configuration.
- `GET|PUT /v1/settings`: basic simulation settings. `max_health` caps
  healing; omit it on `PUT` to keep the stored value.
- `GET|PUT /v1/rules`: story rules as one object. These are mission pacing
  (`missions_enabled`, `end_on_goal`, `pursuit_start_step`,
  `pursuit_full_step`, `route_weight`), default repeat rules
  (`repeat_location|npc|item|event`: `once`, `per_visit`, `always`), and
  arrests (`arrest_location`, `arrest_lock_when`: `sent`, `always`, `never`).
  They also cover discovery (`auto_travel_on_spawn`,
  `auto_travel_on_spawn_threshold`), persistence (`profile`, `save_on`,
  `simulate_with`), and the event fallback (`fallback_action`,
  `fallback_description`). Fields left out of a `PUT` keep their value.
- `GET|PUT /v1/hyperparameters` reads or validates the complete hyperparameter
  YAML; `GET|PUT /v1/weights` reads or updates every weight. `GET` answers `values`
  `{path: value}` and `parameters`, how each is presented: `[{path, value, section, label,
  explanation, unit, type: integer | float, minimum, maximum, default, source, described}]`.
  These come from the engine's code (`backend/tuning_parameters.yaml`): the unit on the
  parameter's own scale, `default` the value the engine uses when the parameter is left out,
  `minimum`/`maximum` only where the engine clamps or rejects values, `source` the code that
  reads it; `described: false` (a parameter without metadata) gives a label from its name and the
  type it is written with. A `PUT` (paths left out keep their value) is checked whole first: a
  fraction for an integer parameter is `400` (never cut down), as is a value outside the engine's
  bounds, and nothing is saved; values that do not change are kept exactly as written.
  `requires` names the true/false setting a parameter needs, and `inactive` says why it has no
  effect now. `basic` is the studio's Basic view, a chosen set of common gameplay controls:
  `[{key, title, summary, controls: [{path, effect}]}]`. Each control is one of the parameters
  (`backend/tuning_basic.yaml`; no presets); a control whose parameter the configuration lacks is
  left out. A `PUT` may send `expected` `{path: value}`, the values the form was loaded with. A
  changed parameter whose stored value has moved since is then refused with `409`, and nothing is saved.
  `shipped` (`{path: value}`, also `shipped` in each parameter) is what this release ships:
  `config/default_history.json`'s `shipped` record, written by `packaging/default_history.py`
  from the configuration being released. It is read from `ANE_CONFIG_DEFAULTS`, the image's
  pristine `config.defaults`, or `config/`. A Basic control the storyteller scales has
  `scaled_by` and `scaling`, which says how far each storyteller's learned traits move its value.
  `unexpected_events.cooldown_steps` and `max_per_story` are weights too.
- `GET /v1/logic` lists the parameter sections of the engine's logic, with what
  each controls. `GET|PUT /v1/logic/{section}` reads or changes one section as
  YAML text (`{"parameters": "evolution:\n  ..."}`). It must keep the same
  settings and value types, unknown settings are rejected, and comments are
  kept. There is no code-editing endpoint. Among the sections: `side_quest_settings` (side quests),
  `side_story` (how the director weaves side quests and attacks into the main story,
  [docs/AI.md](AI.md#side-story-orchestration)), `encounters` (the encounter director,
  [docs/WORLD.md](WORLD.md#dynamic-encounters)), `fitness` (with `version` and `side_story`, the
  objective terms) and `llm_sharing` (the judge and the augmenter on one GPU, the idle watcher). Their
  numbers that weigh, reward or penalise are on the Weights page too (`GET|PUT /v1/weights`).

**World authoring**
- `GET /v1/entities/{kind}` lists Locations, NPCs, Items, or Events.
- `GET|PUT|DELETE /v1/entities/{kind}/{name}` reads, creates, renames,
  updates, or deletes an entry. `goals` is a list of strings; a structured goal
  (`{goal, tags, needed, reward}`) is one line of YAML in it (`{goal: Get out of
  this cell, tags: [smuggler_freed], needed: 1, reward: lead}`), and such a line
  sent back on `PUT` is stored as the structured goal (it used to fail with `500`
  on `GET`). Optional fields, which keep their stored value when omitted on `PUT`:
  - `unique`, `encounter_event` (send `""` to remove it), and
    `encounter_repeat`: NPCs only.
  - `enabled_if`: locations, NPCs, items, and events (send `{}` to remove it).
    Any condition: the variable shorthand or a tree with `all` / `any` / `not`
    (see [WORLD.md](WORLD.md#conditions-and-effects)).
  - `start_eligible` and `start_weight` (above 0): locations only. Stories
    start at the eligible locations (see [WORLD.md](WORLD.md#starting-locations)).
  - `title`: events.
  - `role` (a key of `npc_roles.roles`; `""` removes it and the role is then
    inferred from the NPC's tags) and `secret` (`{text, talks, clues, effects}`,
    `{}` removes it): NPCs only. Actions a secret unlocks use
    `enabled_if: {secret_known: <NPC>}`.
- `GET|PUT /v1/entities/{Events|Items}/{name}/policy`: an event's start rules
  (`trigger`: action or random, `chance`, `weight`, `cooldown`,
  `max_occurrences`, `repeat`: once, per_location, per_visit or always) and its `encounter`
  block (a template of the encounter director: `category`, where and when, `on_win`, `on_loss`,
  `on_escape`; [docs/WORLD.md](WORLD.md#dynamic-encounters)), or an item's uniqueness (`unique`,
  `location`). A `PUT` replaces the whole policy: fields left out use their defaults, so an
  `encounter` block left out of the body is removed. An invalid policy writes nothing.
- `GET|PUT|DELETE /v1/entities/{kind}/{name}/actions/{index}`: one action (`index`: its place,
  from 0; a `PUT` with the number of actions adds one at the end), checked with the others as a
  whole save is. `GET|PUT /v1/entities/{kind}/{name}/details`: what the entry form does not
  show: an NPC's `known_locations`, `known_objects`, `article` (`the`, `a`, `an`, `none`: a
  proper name; empty: automatic) and `dialogue` (`[{text, enabled_if}]`); a place's
  `distant_descriptions`, `entered_descriptions`, `interact_prompts`, `exit_locked` and
  `interaction_points` (points of interest: `[{id, label, prompt, linked_action, linked_outcome,
  linked_location, linked_npc, linked_item, notes}]`, each with a label or a prompt) (an empty
  value removes the field; unknown places, people or items are `400`).
- `POST /v1/suggestions` `{kind, name, field}`: the language model's suggestions for a text field
  of an entry (`descriptions`, `goals`, `tags`, `distant_descriptions`, `entered_descriptions`,
  `interact_prompts`, `dialogue`), given the entry and the world's names: `{suggestions}`. Nothing
  is saved. `503` (with why and how to start it) without the language model, `404` for an unknown
  entry.
- `GET /v1/augmentation`: `{enabled, model}`: whether world augmentation is on (`augmentation.enabled`,
  the studio's tick, what `python main.py` reads) and its own language model's status (`augmentation.llm`:
  Qwen3.5-4B on vLLM; `{available, mode, model, server, reason, how}`, a server asked which models it
  serves). `PUT /v1/augmentation` `{enabled: true|false}` switches it (written into
  `hyperparameters.yaml`, its comments kept; `422` for anything but a boolean). While it is off,
  `POST /v1/jobs/world-proposal` is `409` and no job is queued; the proposal itself runs on Qwen3.5-4B,
  not on the judge's model.
- `GET /v1/world/revision`: `{revision}`, a fingerprint of the world's entries as stored (their
  files' size and time of change) and of an uploaded configuration in use. It changes whenever an
  entry is added, changed, renamed or deleted, by any client or by hand; the studio's content library
  asks for it every 10 seconds and lists the entries again when it changed.
- `GET /v1/world/size`: the world against the augmentation targets (`augmentation.min_*`):
  `{have, want, enough}` for locations, items, NPCs and outcomes. `POST /v1/jobs/world-proposal`:
  the language model proposes new places, people and items in the background (the job's result:
  `{locations, objects, characters}`, only new ones); nothing is written. `POST
  /v1/world/additions` `{locations, objects, characters}` adds them (e.g. the ones of a proposal
  kept, edited or not): each must be new and pass the checks of a saved entry, and the whole world
  is checked with them in place; otherwise `400` and nothing is written.
- `GET|PUT|DELETE /v1/missions/{key}`: one mission (`PUT` adds or replaces it; `rename` gives it a
  new key), checked with every other one.
- **Deleting, and undoing it.** `DELETE /v1/entities/{kind}/{name}` (at least one location, NPC and
  item remains: `400` for the last; events may all go), `DELETE
  /v1/entities/{kind}/{name}/actions/{index}` (optional `?expected_name=`: the name the action at
  `index` must have, else `409` and nothing is deleted, so an action list changed by someone else
  never loses the wrong action) and `DELETE /v1/missions/{key}` answer as before, with `undo`:
  `{token, type: entity | action | mission, name, kind, entry, position, of}`. The API keeps the
  deleted record whole (every field, as stored) with its place: its index, its neighbours and the
  list it left. `POST /v1/deletions/{token}/restore` puts back that record only (never a whole file
  or configuration): where it was when its list is as the deletion left it, otherwise next to its
  old neighbour; the answer says where (`position`, `was`, `moved`, `changed_since`, `message`).
  `409` when its name is taken again (an entry, an action of its entry, a mission key) or its entry
  is gone (an action): nothing newer is overwritten; `400` when it would now be invalid (it is
  checked as a save checks it, only against problems the world did not already have when it was
  deleted); `404` when it was undone already or is older than the most recent 200 (deletions are
  kept with the saves, `deletions.json` in `ANE_SAVE_DIR`, so they survive a restart of the API).
  `GET /v1/entities/{kind}/{name}/references` lists what refers to an entry and would no longer work
  without it (`{references: [...]}`, the engine's own check messages); deleting stays allowed. `GET /v1/map`: `{routes: {place: [borders]} or null,
  places}`; `PUT /v1/map/{place}` `{borders}` sets a place's borders, both ways; `PUT /v1/map`
  `{routes}` the whole map (null: every story draws its own).
- `GET /v1/behaviours/rules` and `PUT /v1/behaviours/rules/{npcs|locations}` `{default, rules}`:
  the rules that give people and places their behaviour by tags, as data (checked as rules).
- `GET|PUT /v1/entities/{kind}/{name}/actions`: the entry's actions and
  outcomes as stored. This covers `repeat`, `exclusive_group`,
  `triggers_event`, `enabled_if` (a condition), `retry_on_failure` (a failed
  attempt does not use a `once` or `per_visit` action up), requirements, and outcome
  fields, including `send_to`, `remove_npc`, `set_variables`,
  `change_variables`, and `effects` (an effect list). Unknown fields, unknown
  events, locations, NPCs, or items, invalid `repeat` values, invalid
  conditions, and invalid effects (wrong types, read-only or undefined
  targets) are rejected, and nothing is written.
- `GET|PUT /v1/missions`: missions (`title`, `description`, `goal`, `weight`,
  `enabled_if`, `rewards`). Goal keys (including `event_succeeded`, which needs
  the event to be resolved by a successful action, and `condition`, any
  condition tree), the rewards' effect list, and every referenced location, item, event,
  NPC, stat, and variable are checked.
- `POST /v1/records/stories/{id}/judge` and `POST /v1/sessions/{id}/judge`: Gemma 4
  as a game critic (`evaluator/game_judge.py`) scores the playthrough from 1 to 10 on
  `agency`, `pacing`, `surprise`, `payoff`, `exploration`, `stakes`, `coherence` and
  `replayability`, with `engagement`, `spread` (between its `llm_judge.samples`
  judgements), `verdict`, `best_moment` and `weakest_moment`. A playthrough with optional
  content (side quests, dynamic encounters) also gets `optional_content` `{score (1-10: what it
  added), meaningful, filler (threads of each kind), filler_share, note, samples}`: the judge tells
  optional content that connects to the main quest, changes what happens later or pays off from
  filler; `null` without optional content (the prompt is then the same as before). `404` for an
  unknown story or session (or someone else's), `503` when no language model is available (`vllm`).
- `GET /v1/missions/playable?profile=&engine_id=`: the quests a player may choose when
  starting a game (`key`, `title`, `description`, `stages` (their titles),
  `available` for that profile now, `achieved` in its save game).
  - With `engine_id` (a finished run or a recorded engine; `latest` is the newest), the quests
    come from the world that engine was trained on, read from its saved configuration. They are
    evaluated against that world's variable definitions and progression rules, with the
    profile's save game. The active configuration does not change, so the answer is what
    `POST /v1/engines/{id}/play` with a `mission` then accepts. Without `engine_id`, the
    active world is used.
  - `world` says which world was read: `source` (`engine` or `active`), `engine_id`, `digest`
    (the world alone), `id` (world and tuning, as in the engine's manifest), `summary` and
    `current` (whether it is the active configuration's world). An unknown engine answers `404`.
- `GET /v1/lint/failures`: the failures the current world leaves as dead ends,
  `{dead_ends: [{action, owner, why}]}`: actions that can fail (a skill check, or
  failure outcomes) where failing changes nothing but health or coins and no
  complication event (tagged `complication`) shares a tag with them. During play the
  engine still shows another way (`director.another_way`).

**Evaluation** (Gemma 4 as the judge; `503` when no language model is available)
- `GET /v1/behaviours`: what people and places do, by their tags (`npc_behaviour`,
  `location_behaviour`): `{npcs: {starts_conversation, appearances}, locations: {draws_in,
  visits}}`, each with its `values`, its `default` and every `{tag, value}` a rule gives it. An
  entry behaves as the first such tag it has; the rules are edited with `PUT /v1/logic/npc_behaviour`
  and `PUT /v1/logic/location_behaviour` (checked as rules: an unknown behaviour or value is `400`).
- `GET /v1/judge/status`: whether the judge can be used here: `{available, mode (server |
  embedded), model, server, reason, how, sleeping (its server's model is asleep, its GPU memory
  freed), sharing (llm_sharing: the judge and the augmenter take turns on one GPU)}`. While the API
  runs, a background watcher puts an idle, awake model to sleep every `llm_sharing.watch_seconds`
  (30; `ANE_LLM_WATCHER=off` switches it off;
  [docs/LLM.md](LLM.md)). A server is asked which models it serves (3 s at
  most). `ANE_JUDGE_URL` points the API at a vLLM server ([`docs/LLM.md`](LLM.md)).
- `POST /v1/judge/compare` with `{"a": {"session_id" | "story_id": ...}, "b": {...}}`:
  which of two stories (a game in progress or a recorded story each) is the better
  game. The pair is judged in both orders and a side wins only where both agree:
  `{winner: A | B | tie (null: unreadable), criteria: {criterion: A | B | tie},
  reason, agreed}`. `400` for an unknown story or session.
- `POST /v1/jobs/judge-benchmark` (`stories` 1-60, `seed`, `engine_id`, `pairwise`, `variants`,
  `workers`): the judge on the stories of the target.
  - **Target:** `engine_id` is the selected trained engine (a finished run or a recorded engine):
    its saved world, and its trained storyteller for every seed. Left out, the target is the
    baseline: the current configuration, with a random storyteller per seed.
  - **Absolute:** `{engagement, criteria, judged, rows}`.
  - **Pairwise:** A is the target as it is and B is the same target with `variants`
    (`{dotted.path: value}`, version B). Both are played on the same seeds, and each pair is
    judged in both orders: `{pairs, a_wins, b_wins, ties, b_win_rate, wilson_95, criteria, rows,
    a, b}`, where `a` and `b` give each side's `label` and `variants`.
  - Every result also has `target` and `seeds` (`[first, last]`), and the job carries `target`
    from its submission. The target is `{kind: engine | baseline, engine_id, label, world:
    {digest, id, summary, current}, storyteller}`.
  - The stories are played in a separate process, so the settings of games in progress never
    change.
- `GET /v1/evaluation/target?engine_id=`: the same `target`, before anything is submitted. `404`
  for an unknown engine.
- `POST /v1/jobs/calibrate-fitness` (`stories` 10-60, `seed`, `engine_id`, `alpha`,
  `apply`, `workers`): plays and judges the stories, fits the judged engagement on
  the fitness terms and suggests a multiplier per term: `{term_weights, terms,
  spearman, cv_spearman (agreement on stories left out), alpha, n, yaml,
  applied, engagement, stories, target, seeds}`; `apply: true` saves them. Its target is the same as the
  judge benchmark's, and its job carries it as well.
- `GET|PUT /v1/fitness/term-weights` (`{"weights": {term: multiplier}}`, `{}`
  removes them): `fitness.term_weights`, the multiplier of each fitness term.
- The judge in the loop of evolution is a setting: `evolution.judge_weight`
  (`PUT /v1/logic/evolution`).

- `GET|PUT /v1/messages`: story messages, with defaults and allowed
  placeholders.

**Records and saved engines**
- Earlier files (engine models, exports, older bundles, story configurations)
  are brought into the records with `python main.py --import-artifacts
  [FOLDER]` (see [ENGINES.md](ENGINES.md#records-database)), or through the API from the server's
  import folder (`ANE_ARTIFACT_DIR`, else `artifacts/`; a client cannot name another folder):
  `GET /v1/records/import` (`{folder, files}`) and `POST /v1/jobs/records-import` `{move (else
  copy), preview}`, a background job whose result is the import's report (`400` when the folder is
  empty). They then appear here with the origin `import`, their rebuilt runs and a preview story.
- `GET /v1/records`: the records folder and whether recording is on.
- `GET /v1/records/runs` (`?user_id=`: one user's) and `GET /v1/records/runs/{id}`: runs with
  every setting, every generation, their engines, stories, and files, and whose run each is
  (`user_id`).
- `GET /v1/records/worlds` and `GET /v1/records/worlds/{id}` (`?format=yaml`:
  a combined configuration to upload again).
- `GET /v1/records/engines` (`run_id`, `source`), `GET /v1/records/engines/{id}`,
  and `GET /v1/records/engines/{id}/model` (the engine model file).
- `POST /v1/records/engines/{id}/play`, `/continue` (`generations`: continue the
  training exactly), and `/retrain` (`candidate_count`, `generations`,
  `simulation_steps`, `episodes`, `deterministic`, and the game to start as for a simulation:
  `seed`, `start_game`, `profile`, `stats_mode`, `stats`, `mission`) start a game, like a
  simulation, and are recorded.
- `GET /v1/records/stories` (`run_id`, `engine_id`, `kind`, `origin`,
  `session_id`) and `GET /v1/records/stories/{id}` (with every turn). A story played in a session
  belongs to that session's user: someone else's is not listed and answers `404`, as the session
  itself does (a story whose session is gone is only the studio's to read; training and evaluation
  stories are everyone's). A page can therefore hold fewer than `limit` stories.
- The list routes take `limit` (200) and `offset` (0).
- Simulation and load responses carry `engine_id`; `engine_download_url` is the
  engine model file, which `POST /v1/engines/load` accepts. `POST /v1/exports`
  takes `source: engine` with `engine_id`.

**Character stats**
- `GET|PUT /v1/stats`: the stat definitions (`stats`, `allocation`, `mode`,
  `defined_points`, `tag_checks`, `damage_reduction_stat`).
- `POST /v1/stats/allocate` with `{"points": {...}}` checks a player's own
  points against the budget. With `{"mode": "random"|"balanced"}` it previews a
  split. It returns the points, the 1-100 values, and any problems.
- Items carry `stat_bonus` on the entity API. Actions and outcomes accept
  `skill_check`, `on_check`, and `change_stats` in the actions editor
  endpoint.

**Persistent variables**
- `GET|PUT /v1/variables`: variable definitions and the default profile's
  stored values.
- `GET /v1/variables/profiles` lists the player profiles.
- `GET|PUT /v1/variables/values?profile=` reads or overwrites a profile's
  stored `values` and `playthroughs`, e.g. to test a later playthrough.
  `POST /v1/variables/reset?profile=` starts the profile over as a new player.

**Evolution and play**
- `POST /v1/simulations` runs the evolutionary AI and begins a playable
  session with the best storyteller. `episodes` sets the stories per
  storyteller per generation, and `warm_start` continues from the learning
  memory. A run takes up to 400 `candidate_count` storytellers, 1000
  `generations`, and 400 `simulation_steps` (the same maximums apply to the
  jobs, `continue`, and `retrain`); the total work is also capped by the app
  parameter `max_simulation_work`. `simulation_steps` is the length of an evaluated story;
  with `evolution.full_length_stories` every story is played to its end, and
  the work limit counts whole stories. Storytellers are evaluated on the same
  spread of start × mission situations (`evolution.stratify_episodes`) and
  scored as mean minus a share of their spread (`evolution.score_aggregate`).
  The response's `learning` holds `history` (per generation: `best`,
  `mean`, `worst`, `spread`, `diversity`, `mutation_step`, `validation`),
  `learned_traits`, `best_genome`, `improvement`, `generations_run`,
  `early_stopped`, and `warm_started_from_memory`. `profile` chooses the
  player profile for persistent variables, and `stats` (the player's own
  points) or `stats_mode` sets the character's stats.
- `POST /v1/exports` exports the trained engine as a ZIP that runs offline and
  integrates in a game. `source` is `run` (with `run_id`, the latest if
  omitted, and an optional `generation` for that generation's champion),
  `memory` (the best remembered storyteller), or `game` (`session_id`). It
  also takes `include_memory`, `name` and `platform`, and returns `download_url`, `name`,
  `source`, `score`, `genome`, `world`, `platform` and `code_version`. `GET /v1/runs` lists the exportable
  runs, and simulation responses carry their `run_id`.
- `platform` says what runs the export: `python` (the default: the engine as
  Python code), `windows-x64`, `linux-x64`, `macos-arm64` or `macos-x64` (the
  **ANE AI**: a standalone program with the engine package, which a UNP
  launches, drives over HTTP on 127.0.0.1 and stops; see [UNP.md](UNP.md)), or
  `unity` (a `.unitypackage` with the Unity library, the engine package and
  every runtime available). `GET /v1/exports/platforms` lists each platform
  with `available` and, when it is not, the `reason`; a platform that is not
  available answers `409`. The API takes the runtimes from `ANE_RUNTIMES_DIR`
  (default `runtimes/`, `/app/runtimes` in the Docker image, filled by CI) and
  uses one only when its `code_version` is the API's own.
- **The ANE AI is made from a complete engine** (any platform but `python`): a
  finished run's engine model or a loaded (also continued or retrained) engine,
  with its own world, storyteller, training state and tag vectors.
  `GET /v1/exports/engines` lists them (the finished runs), newest first
  (`engine_id`, `created_at`, `source`, `score`, `run_id`, `resumable`,
  `generations`, `loaded`); it is empty until a training finishes or an engine is
  loaded. They are kept in the records, or, when recording is off, with the
  saves (`ANE_SAVE_DIR/engines`, the newest 20), so they survive a restart. A
  Python export with `source: engine` or `source: run` (no `generation`) is made
  from the engine the same way. Export with `source: engine` (`engine_id`,
  default the newest) or `source: run` (that run's engine, without
  `generation`). A generation's champion, the remembered storyteller, a play
  session's storyteller, or no complete engine yet answer `409`. The response
  carries the `engine_id`. Without a `name`, the export is named after the engine's
  name, if it has one; its manifest carries that name as `engine_name`.
- **Engine names and result cards.** Each entry of `GET /v1/exports/engines`, `GET /v1/runs`
  and `GET /v1/records/engines` also has `name` (null when unnamed) and `label`. The label is
  the name with the creation time, or, for an unnamed engine, what it is, when it was made, its
  generations and its training fitness. It always ends with the short id. The engine and run
  ids stay the identifiers.
  - `PUT /v1/engines/{engine_id}/name` with `{"name": "…"}` names an engine, and `""` or
    `null` removes the name. It answers `400` for a name over 80 characters or one with control
    characters, and `404` for an unknown engine.
  - `GET /v1/engines/{engine_id}/card` is the result card: `name`, `label`, `default_label`,
    `created_at`, `kind`, `run_id`, `parent_id`, `world` (`id`, `summary`, and `current`: trained
    on the active world), `generations`, `settings` (`main` and `other`: what its training ran
    with), `fitness` (`training`, the evolution's objective, plus `improvement`,
    `early_stopped`, `history` and `version`: the scoring it was trained with, `fitness.version`;
    engines trained before versions are 1, and scores of different versions are not comparable),
    `judge_in_training`, `evaluations`, `resumable` and
    `scaling_traits`.
  - `evaluations` lists, newest first, the judge benchmarks (`POST /v1/jobs/judge-benchmark`
    with an `engine_id`, neither pairwise nor with variants) and calibrations of this engine.
    Each gives the judge's mean `engagement` (1–10), kept apart from the training fitness, which
    is on its own scale.
  - Names and evaluations are kept with the saves (`ANE_SAVE_DIR/engine_meta.json`,
    `{"format": 1, "engines": {...}}`), so they work with recording on or off and survive a
    restart. An older bare `{engine_id: name}` file is read and rewritten in this format.
- `GET /v1/learning` returns the storytellers the AI remembers from earlier
  runs; `DELETE /v1/learning` forgets them.
- `POST /v1/engines/load?profile=&stats_mode=&stats=&mission=&activate=` loads an engine model
  (`.ane.zip`), an older engine ZIP, or a configuration YAML (form field
  `file`) and begins a session without evolving again. `parameters` (optional
  form field) is a model-parameters YAML for a configuration without
  `generated_run.genome`; `stats` is the player's own points as a JSON object
  (overrides `stats_mode`).
- **Games with a saved engine.** `POST /v1/engines/load`, `POST /v1/engines/{id}/play` and
  `POST /v1/records/engines/{id}/play` start the game in the engine's own context: its world,
  tuning, tag vectors, default profile and stat rules, frozen when the game starts. Nothing
  process-wide changes. The editors' world (`GET /v1/world/revision`), `GET /v1/config`, other
  sessions and the next default game or training are unaffected. The answer says what the game
  runs with: `engine` (`{engine_id, name, source}`), `world` (`{source, engine_id, digest, id,
  summary, current}`), `world_revision` (the revision of the frozen configuration) and
  `activated`.
- `POST /v1/engines/{id}/activate` (`latest`: the newest finished run) is the explicit operation
  that makes an engine's world and tuning, with its tag vectors, the studio's defaults. Those are
  what the editors show and what starts next without an engine of its own. Games and jobs already
  going keep their own contexts. It answers `{engine_id, activated: true, revision}` (the editors'
  revision now), or `404` for an unknown engine.
- **Migration.** Until this change, starting a game with an engine also made its world the active
  one. Callers that relied on that (an editor expected to show the engine's world after playing it)
  either call `POST /v1/engines/{id}/activate`, or pass `activate: true` (a body field of the
  `/play` requests, a query parameter of `/v1/engines/load`), which gives the earlier behaviour.
  Requests without it start isolated games; their request shapes are unchanged, and the response
  only gains fields.
- `GET /v1/sessions/{id}` and `POST /v1/sessions/{id}/actions` return the
  scene (Markdown), the available `actions`, and the last turn. They also give
  a structured state: `location`, `start_location`, `health`, `max_health`, `coins`, `power`,
  `inventory`, `mission`, `mission_status`, `won`, `event`, `ended`,
  `end_reason`, `profile`, `variables`, `stats` (1-100 values with item bonuses),
  `stat_points`, and `last_check` (`stat`, `roll`, `target`, `passed`, `step`). A won story has `ended: true`
  and offers no more actions.
- The structured state also has what a game's own interface shows beside the
  scene:
  - `quest`: `key`, `title`, `description`, `status` (in progress, completed,
    failed), `stages` (each `title`, `description`, `state`: done, current or
    todo) and `todo` (what the current step still needs, in words, such as
    "Reach the Vault" or "Have 20 coins (you have 12)").
  - `side_quests`: the story's side quests, optional quests besides the mission
    ([docs/WORLD.md](WORLD.md#side-quests)), in the order they were offered or started: `key`,
    `title`, `description`, `giver`, `status` (offered, active, completed, failed, declined),
    `reason` (why it failed: condition, time, giver_gone, item_gone), `stages` (each `title`,
    `description`, `state`), `todo`, `ready` (done, to hand over to the giver), `rewards` (in
    words), `deadline` and `turns_left` (with `within_turns`), `turn_in`, `aids_mission` (lead,
    ally, none), `aided_mission` (what it did for the mission once completed), `follows`, and the
    turns it was `offered`, `started` and `ended`. The journal's turns (`turn_summary` too) say what
    happened to them (`side_quests`, each `what`: offered, started, declined, lapsed, stage, ready,
    reminded, hurry, completed, failed, reacted, withdrawn), and the session report has
    `side_quests: {completed, failed, declined, unfinished, aided_mission}`. They are edited as the
    document `side_quests` (`GET`/`PUT /v1/documents/side_quests`, checked before it is saved).
    How the director weaves them into the main story (offers only when the main story has room,
    what bears on the quest first, reveals and callbacks near the climax) is `side_story`
    ([docs/AI.md](AI.md#side-story-orchestration)).
  - `people`: the people here (`name`, `called` with its article,
    `description`, `wants`, `hostile`, `talks` (times spoken with), `quest`
    (they can help with the mission now), `helped` (times helped with their
    goal), `ally` (their goal is fulfilled), `side_quests` (the titles of the side
    quests they offer now), `disposition` (how they feel about the player) and
    `faction` (who they stand with, `null` when it is not public)).
  - `people_met`: everyone met in the story (`name`, `called`, `where`,
    `talks`, `hostile`, `gone`, `ally`, `disposition`, `faction`).
  - `ways_on`: every place one can travel to from here now (`name`,
    `visited`, `toward_quest`); `actions` offer the most relevant of them.
  - `map`: the map as the player knows it, kept with the game: `current`,
    `previous`, `places` (`name`, `visited`, `locked`), `paths` (pairs open
    both ways) and `one_way` (`[from, to]`, such as an arrest's).
  - `choices`: the offered `actions` with the check each involves (`check`:
    `stat`, `chance` 0-100 and `bonus` from Power or an ally, or `null`).
- In real play a discovery or a hint never moves the player by itself, and
  lingering does not end the story (`navigation.player_directed: play`); the
  way back to the place just left is always among the `actions`.
- `GET /v1/artifacts/{id}` downloads generated config or engine bundles.

The schemas and validation constraints are available in `/docs`.

## Examples

List authored locations:

```bash
curl http://localhost:8005/v1/entities/Locations
```

Run a short evolution:

```bash
curl -X POST http://localhost:8005/v1/simulations \
  -H 'content-type: application/json' \
  -d '{"candidate_count":8,"generations":5,"simulation_steps":16,"deterministic":true}'
```

Use the returned `session_id`, one of its `choices[].id` and its `state_version`:

```bash
curl -X POST http://localhost:8005/v1/sessions/SESSION_ID/actions \
  -H 'content-type: application/json' \
  -d '{"action_id":"CHOICE_ID_FROM_THE_RESPONSE","state_version":STATE_VERSION}'
```

(Older clients send `{"action":"ACTION_FROM_THE_RESPONSE"}`, one of its exact `actions` values.)

Upload a complete configuration or saved engine:

```bash
curl -F file=@config.yaml http://localhost:8005/v1/config/upload
curl -F file=@narrative-engine.zip http://localhost:8005/v1/engines/load
```
