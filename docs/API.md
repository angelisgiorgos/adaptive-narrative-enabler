# Adaptive Narrative Enabler HTTP API

The Compose stack runs the complete Gradio UI and REST API as separate services
from the same image. They share persistent configuration, artifact, and model
cache volumes.

The studio is a client of this API: every tab works through the endpoints
below (`ANE_API_URL`, set to `http://hamlet-ane-api:8004` by Compose), and
whatever it does, other clients can do too. Without `ANE_API_URL`, `python
app.py` serves the same API inside the studio's process.

| URL | Purpose |
|---|---|
| `http://localhost:8003` | Adaptive Narrative Studio |
| `http://localhost:8004/docs` | Interactive Swagger documentation |
| `http://localhost:8004/redoc` | ReDoc API documentation |
| `http://localhost:8004/healthz` | API container health check |

## Run with Docker

```bash
docker compose up --build
```

Open the UI at <http://localhost:8003> and API documentation at
<http://localhost:8004/docs>. Edited configuration, exported simulation
artifacts, and Hugging Face models use persistent named volumes. Set
`ANE_CONFIG_VOLUME=./config` before starting Compose if you specifically want a
host bind mount (its files must be writable by container UID 10001). The service intentionally uses one Uvicorn worker because runtime
configuration overrides and active games live in memory.

To build and run without Compose:

```bash
docker build -t adaptive-narrative-enabler .
docker run --rm -p 8004:8004 adaptive-narrative-enabler
docker run --rm -p 8003:8003 \
  -e ANE_UI_PORT=8003 -e ANE_API_URL=http://<api-host>:8004 \
  adaptive-narrative-enabler python app.py
```

Leave out `ANE_API_URL` to run the studio with its own API inside.

## How the API behaves

### One stateful process

The API keeps games and recent work in its memory, so it runs as **one
process with one worker** (Compose does this). A second worker would not see
the first one's games, and a restart forgets everything that lives in memory:

| In memory (lost on restart) | On disk (kept) |
| --- | --- |
| Play sessions: the newest `max_web_sessions` (100); beyond that the oldest is dropped | Configuration: `config/*.yaml` and the app parameters `config/ui_settings.yml` |
| Recent runs that can be exported (the latest 20) | Player profiles' persistent variables and the learning memory (`ANE_SAVE_DIR`) |
| Background jobs (the latest 100) | The records database and engine model files (`ANE_RECORDS_DIR`) |
| Download links `/v1/artifacts/{id}` (the files stay in `ANE_ARTIFACT_DIR`, the links do not) | Generated configurations, engine models and exports (`ANE_ARTIFACT_DIR`) |
| An uploaded configuration (`POST /v1/config/upload`), until `POST /v1/config/reset` or a restart | |

The studio (`app.py`) is a client of this API. With `ANE_API_URL` it uses the
API service, and its games, runs and jobs are the API's. Without
`ANE_API_URL` it serves the API inside its own process, which then holds that
state.

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
- **Immediate.** After a save the configuration is reloaded, and new
  simulations and games use it at once. App parameters
  (`PUT /v1/app-settings`) apply when the API and the studio restart.

### Status codes

| Code | When |
| --- | --- |
| `200` | Done; the body is the result |
| `202` | A job was queued (`POST /v1/jobs/...`); poll `GET /v1/jobs/{id}` |
| `400` | Refused content (see above), a file of the wrong type, an engine file that cannot be loaded, a choice that is not on offer, a deterministic run without a seed |
| `404` | Unknown entry, document, action index, play session, job, artifact, or recorded run, world, engine or story (also when playing, continuing or retraining it, before any work starts). While no records database exists, the records endpoints answer `404` ("No records yet") |
| `409` | An export for a platform this API has no runtime for, or whose runtime was built from other engine code (`GET /v1/exports/platforms` says why); an ANE AI export without a complete engine (`GET /v1/exports/engines`) |
| `413` | An upload above its limit: configuration 2 MB (`max_config_upload_bytes`), engine 20 MB (`max_engine_upload_bytes`), model parameters 100 KB |
| `422` | The request's shape, types or ranges |

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
   `requested_steps`, `effective_max_turns` (the most turns a story can take; `story_turns` is the
   same, for older clients), `full_length_stories`, `story_length`, `steps_decide` (whether the
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
  one of the `actions` the last response offered; any other choice is refused
  with `400`, and an unknown session is `404`.
- `actions` are the most relevant of the storyteller's options
  (`player_options`): what the moment is about first (a prisoner's way out,
  the quest's next step, the payoff of an open clue, a way on), then the rest
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
  out of sight until a lead, the road or enough turns bring them in.
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
  `GET /v1/progression?profile=` the save game (goals achieved, playthroughs, session
  reports with their `delivery`), and `POST /v1/progression/reports/{id}/send?profile=`
  sends a kept report again.
- `POST /v1/engines/{id}/play` (`{"profile", "mission", "stats_mode", "stats"}`) starts a game in
  the world a finished run was trained on, with its storyteller (`latest`: the newest; the
  finished runs are `GET /v1/exports/engines`, kept also without the records); its world and
  tuning become the active ones, as when an engine is loaded; `409` before any run has
  finished. `GET /v1/sessions/{id}/transcript` is the story so far, and
  `GET /v1/sessions/{id}/journal` the game turn by turn as a player follows it: each turn's choice
  (`action`, `with` whom, `where`), its check (`stat`, `roll`, `needed`, `passed`), what came of it
  (`told`, `succeeded`), what it changed (`health_change`, `coin_change`, `items_gained`,
  `items_lost`, `moved_to`, `new_places`, `met`, `event`) and the quest (`quest_done`: the steps it
  completed, `quest_now`, `quest_complete`). Every play state says the last one (`turn_summary`)
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
  game of the ANE AI loads here, and one from here in the ANE AI. Fields that do not fit the
  world answer `400`. `GET /v1/world/catalogue` is the world as the ANE AI sends it
  (`ane.world/1`).
- A finished story has `ended: true` and offers no more actions.
- Real play saves persistent variables to its `profile` (`save_on` in the
  story rules). Evaluated stories during evolution do not
  (`simulate_with`).

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
curl -X POST http://localhost:8004/v1/simulations -H 'Content-Type: application/json' \
     -H 'X-ANE-User: 8f14e45f-ceea-467f-a8f5-b1c3e1c2d6aa' -d '{"candidate_count": 8, "generations": 5}'
```

### Files and browsers

- Generated files are offered as download links (`*_download_url`,
  `GET /v1/artifacts/{id}`), valid while the process runs.
- Uploads are recognised by their extension (`.yaml`/`.yml`, `.zip`) and
  read in chunks up to their limit.
- CORS is off unless `ANE_CORS_ORIGINS` lists the allowed origins (comma
  separated). It then allows `GET`, `PUT`, `POST`, `DELETE` and the headers
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
  validates all of them, and rejects unknown ones. They apply after a restart.
- `GET /v1/documents` lists the configuration documents edited as YAML text
  with their comments kept: `missions`, `messages`, `variables`, `map` (the world's
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

**Jobs** (long work with progress)
- `POST /v1/jobs/simulations` (body as `POST /v1/simulations`),
  `POST /v1/jobs/engines/{id}/continue` and
  `POST /v1/jobs/engines/{id}/retrain` (bodies as the records endpoints) start
  the work in the background and return `202` with the job.
- `POST /v1/jobs/judge-benchmark` and `POST /v1/jobs/calibrate-fitness` (see
  **Evaluation**) run the judge in the background; their `progress` is
  `{generation: stories judged so far, total, label}`.
- `GET /v1/jobs/{id}`: `status` (`queued`, `running`, `finished`, `failed`),
  `progress` (`generation`, `total`, `best`, `mean`, `validation`, `improved`),
  and, when finished, `result` (what the synchronous endpoint returns: the
  playable game) or `error`. `GET /v1/jobs` lists the latest jobs without their
  results. Jobs run one at a time in the order they arrive; the latest 100 are
  kept (in memory).

**Configuration**
- `GET /v1/config` returns the complete merged active configuration.
- `POST /v1/config/upload` validates and activates a combined YAML upload
  (every section, plus every condition and effect list of its world; a
  rejected upload leaves the active configuration unchanged). A file may hold
  only a world: the sections it lacks come from the built-in configuration,
  and the message names them (see `examples/`);
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
  YAML; `GET|PUT /v1/weights` reads or updates every weight.
- `GET /v1/logic` lists the parameter sections of the engine's logic, with what
  each controls. `GET|PUT /v1/logic/{section}` reads or changes one section as
  YAML text (`{"parameters": "evolution:\n  ..."}`). It must keep the same
  settings and value types, unknown settings are rejected, and comments are
  kept. There is no code-editing endpoint.

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
  `max_occurrences`, `repeat`: once, per_location, per_visit or always) or an
  item's uniqueness (`unique`, `location`). A `PUT` replaces the whole policy,
  fields left out use their defaults, and an invalid policy writes nothing.
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
  deleted); `404` when it was undone already or the API restarted since (deletions are kept in
  memory, the most recent 200). `GET /v1/map`: `{routes: {place: [borders]} or null,
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
  judgements), `verdict`, `best_moment` and `weakest_moment`. `404` for an unknown story
  or session, `503` when no language model is available (`vllm`).
- `GET /v1/missions/playable?profile=`: the quests a player may choose when
  starting a game (`key`, `title`, `description`, `stages` (their titles),
  `available` for that profile now).
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
  embedded), model, server, reason, how}`. A server is asked which models it serves (3 s at
  most). `ANE_JUDGE_URL` points the API at a vLLM server ([`docs/LLM.md`](LLM.md)).
- `POST /v1/judge/compare` with `{"a": {"session_id" | "story_id": ...}, "b": {...}}`:
  which of two stories (a game in progress or a recorded story each) is the better
  game. The pair is judged in both orders and a side wins only where both agree:
  `{winner: A | B | tie (null: unreadable), criteria: {criterion: A | B | tie},
  reason, agreed}`. `400` for an unknown story or session.
- `POST /v1/jobs/judge-benchmark` (`stories` 1-60, `seed`, `engine_id` (a recorded
  engine; left out: the current settings with random storytellers), `pairwise`,
  `variants` (`{dotted.path: value}`: version B), `workers`): the judge on the
  engine's stories. Absolute: `{engagement, criteria, judged, rows}`. Pairwise: the
  same seeds with the current settings (A) and with `variants` (B), each pair judged
  in both orders: `{pairs, a_wins, b_wins, ties, b_win_rate, wilson_95, criteria,
  rows}`. The stories are played in a separate process, so the settings of games
  in progress never change.
- `POST /v1/jobs/calibrate-fitness` (`stories` 10-60, `seed`, `engine_id`, `alpha`,
  `apply`, `workers`): plays and judges the stories, fits the judged engagement on
  the fitness terms and suggests a multiplier per term: `{term_weights, terms,
  spearman, cv_spearman (agreement on stories left out), alpha, n, yaml,
  applied}`; `apply: true` saves them.
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
  `simulation_steps`, `episodes`, `deterministic`) start a game, like a
  simulation, and are recorded.
- `GET /v1/records/stories` (`run_id`, `engine_id`, `kind`, `origin`,
  `session_id`) and `GET /v1/records/stories/{id}` (with every turn).
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
  carries the `engine_id`.
- `GET /v1/learning` returns the storytellers the AI remembers from earlier
  runs; `DELETE /v1/learning` forgets them.
- `POST /v1/engines/load?profile=&stats_mode=&stats=&mission=` loads an engine model
  (`.ane.zip`), an older engine ZIP, or a configuration YAML (form field
  `file`) and begins a session without evolving again. `parameters` (optional
  form field) is a model-parameters YAML for a configuration without
  `generated_run.genome`; `stats` is the player's own points as a JSON object
  (overrides `stats_mode`).
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
  - `people`: the people here (`name`, `called` with its article,
    `description`, `wants`, `hostile`, `talks` (times spoken with), `quest`
    (they can help with the mission now), `helped` (times helped with their
    goal) and `ally` (their goal is fulfilled)).
  - `people_met`: everyone met in the story (`name`, `called`, `where`,
    `talks`, `hostile`, `gone`, `ally`).
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
curl http://localhost:8004/v1/entities/Locations
```

Run a short evolution:

```bash
curl -X POST http://localhost:8004/v1/simulations \
  -H 'content-type: application/json' \
  -d '{"candidate_count":8,"generations":5,"simulation_steps":16,"deterministic":true}'
```

Use the returned `session_id` and one of its exact `actions` values:

```bash
curl -X POST http://localhost:8004/v1/sessions/SESSION_ID/actions \
  -H 'content-type: application/json' \
  -d '{"action":"ACTION_FROM_THE_RESPONSE"}'
```

Upload a complete configuration or saved engine:

```bash
curl -F file=@config.yaml http://localhost:8004/v1/config/upload
curl -F file=@narrative-engine.zip http://localhost:8004/v1/engines/load
```
