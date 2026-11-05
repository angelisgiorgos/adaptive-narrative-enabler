# Adaptive Narrative Enabler HTTP API

The Compose stack runs the complete Gradio UI and REST API as separate services
from the same image. They share persistent configuration, artifact, and model
cache volumes.

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
  -e ANE_UI_PORT=8003 adaptive-narrative-enabler python app.py
```

## Endpoint coverage

Every write is validated before anything is saved. Invalid requests return
`400` with the list of problems, and nothing is written.

**Configuration**
- `GET /v1/config` returns the complete merged active configuration.
- `POST /v1/config/upload` validates and activates a combined YAML upload;
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
  - `enabled_if`: locations, NPCs, and items (send `{}` to remove it).
  - `title`: events.
- `GET|PUT /v1/entities/{kind}/{name}/actions`: the entry's actions and
  outcomes as stored. This covers `repeat`, `exclusive_group`,
  `triggers_event`, requirements, and outcome effects, including `send_to`,
  `remove_npc`, `set_variables`, and `change_variables`. Unknown fields,
  unknown events, locations, NPCs, or items, invalid `repeat` values, and
  wrongly typed variable changes are rejected.
- `GET|PUT /v1/missions`: missions (`title`, `description`, `goal`, `weight`,
  `enabled_if`). Goal keys and every referenced location, item, event, NPC,
  and variable are checked.
- `GET|PUT /v1/messages`: story messages, with defaults and allowed
  placeholders.

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
  memory. The response's `learning` holds `history` (per generation: `best`,
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
- `POST /v1/engines/load?profile=&stats_mode=` loads a prior YAML or ZIP engine
  and begins a session without evolving again.
- `GET /v1/sessions/{id}` and `POST /v1/sessions/{id}/actions` return the
  scene (Markdown), the available `actions`, and the last turn. They also give
  a structured state: `location`, `health`, `max_health`, `coins`, `power`,
  `inventory`, `mission`, `mission_status`, `won`, `event`, `ended`,
  `end_reason`, `profile`, `variables`, `stats` (1-100 values with item bonuses),
  `stat_points`, and `last_check` (`stat`, `roll`, `target`, `passed`, `step`). A won story has `ended: true`
  and offers no more actions.
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
