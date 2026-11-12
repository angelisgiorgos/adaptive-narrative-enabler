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
| `413` | An upload above its limit: configuration 2 MB (`max_config_upload_bytes`), engine 20 MB (`max_engine_upload_bytes`), model parameters 100 KB |
| `422` | The request's shape, types or ranges |

Errors are JSON: `{"detail": "..."}` (or the list of `422`).

### Runs: limits, work, and waiting

1. **Maximums.** One run takes up to 400 storytellers
   (`candidate_count`), 1000 `generations`, 400 `simulation_steps`, and 10
   `episodes`; above that is `422`.
2. **Studio limits.** A run is then held to the app parameters
   `candidate_limit`, `generation_limit`, and `simulation_step_limit`
   (by default the maximums). A request above a lowered limit runs at the
   limit; it is not refused.
3. **Work.** Storytellers × generations × story length × stories per
   storyteller must stay within `max_simulation_work` (60,000 simulated
   turns). With `evolution.full_length_stories`, a story counts as at least
   100 turns (`narrative_end.max_actions`). A run above this is refused with
   `400` before anything runs.
4. **Reproducible.** `deterministic: true` uses the seed of the settings; the
   same configuration and seed give the same evolution.
5. **Waiting.** `POST /v1/simulations` and the records' `play`, `continue` and
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

### Files and browsers

- Generated files are offered as download links (`*_download_url`,
  `GET /v1/artifacts/{id}`), valid while the process runs.
- Uploads are recognised by their extension (`.yaml`/`.yml`, `.zip`) and
  read in chunks up to their limit.
- CORS is off unless `ANE_CORS_ORIGINS` lists the allowed origins (comma
  separated). It then allows `GET`, `PUT`, `POST`, `DELETE` and the headers
  `Content-Type`, `Authorization`, and `X-ANE-Client`.

## Endpoint coverage

Every write is validated before anything is saved. Invalid requests return
`400` with the list of problems, and nothing is written.

Requests with the header `X-ANE-Client: studio` are recorded with the origin
`studio` (the studio sends it); all others with the origin `api`.

**Studio services** (what the studio is built on; any client can use them)
- `GET /v1/reference`: what an authoring UI needs: `entity_kinds`,
  `common_tags`, `action_reference`, `repeat_rules` and `repeat_default`,
  mission `goal_keys`, `rule_choices` and `rule_fields` (the story-rule form),
  `player_stat_choices`, the editable `documents`, and the `defaults` of a new
  run (`episodes_per_genome`, `warm_start`, `profile`).
- `GET|PUT /v1/app-settings`: the studio's app parameters
  (`config/ui_settings.yml`: limits, defaults, queue, launch) with their
  `defaults`. A `PUT` (`{"settings": {...}}`) changes the parameters it names,
  validates all of them, and rejects unknown ones. They apply after a restart.
- `GET /v1/documents` lists the configuration documents edited as YAML text
  with their comments kept: `missions`, `messages`, `variables`, `stats`, and
  `stored_values` (a player profile's values, `?profile=`).
  `GET /v1/documents/{name}` returns `{"name", "yaml"}`;
  `PUT /v1/documents/{name}` with `{"yaml": "..."}` validates and saves it and
  returns the saved text and a message.
- `POST /v1/variables/reset/yaml?profile=` starts a profile over and returns its
  `stored_values` document.
- `GET|PUT /v1/entities/{kind}/{name}/actions/yaml`: the entry's actions as YAML
  text, validated like `.../actions`.
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
  and the message names them (see `examples/worlds/`);
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
  updates, or deletes an entry. Optional fields, which keep their stored value
  when omitted on `PUT`:
  - `unique`, `encounter_event` (send `""` to remove it), and
    `encounter_repeat`: NPCs only.
  - `enabled_if`: locations, NPCs, items, and events (send `{}` to remove it).
    Any condition: the variable shorthand or a tree with `all` / `any` / `not`
    (see the README, Conditions and effects).
  - `start_eligible` and `start_weight` (above 0): locations only. Stories
    start at the eligible locations (see the README, Starting locations).
  - `title`: events.
- `GET|PUT /v1/entities/{Events|Items}/{name}/policy`: an event's start rules
  (`trigger`: action or random, `chance`, `weight`, `cooldown`,
  `max_occurrences`, `repeat`: once, per_location, per_visit or always) or an
  item's uniqueness (`unique`, `location`). A `PUT` replaces the whole policy,
  fields left out use their defaults, and an invalid policy writes nothing.
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
- `GET /v1/missions/playable?profile=`: the quests a player may choose when
  starting a game (`key`, `title`, `description`, `stages` (their titles),
  `available` for that profile now).
- `GET|PUT /v1/messages`: story messages, with defaults and allowed
  placeholders.

**Records and saved engines**
- Earlier files (engine models, exports, older bundles, story configurations)
  are brought into the records with `python main.py --import-artifacts
  [FOLDER]` (see the README, Records database); they then appear here with the
  origin `import`, their rebuilt runs and a preview story.
- `GET /v1/records`: the records folder and whether recording is on.
- `GET /v1/records/runs` and `GET /v1/records/runs/{id}`: runs with every
  setting, every generation, their engines, stories, and files.
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
  also takes `include_memory` and `name`, and returns `download_url`, `name`,
  `source`, `score`, `genome`, and `world`. `GET /v1/runs` lists the exportable
  runs, and simulation responses carry their `run_id`.
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
    `description`, `wants`, `hostile`, `talks` (times spoken with) and `quest`
    (they can help with the mission now)).
  - `people_met`: everyone met in the story (`name`, `called`, `where`,
    `talks`, `hostile`, `gone`).
  - `ways_on`: every place one can travel to from here now, for a map
    (`name`, `visited`, `toward_quest`); `actions` offer the most relevant of
    them.
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
