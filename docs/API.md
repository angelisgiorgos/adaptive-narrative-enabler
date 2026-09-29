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

- `GET /v1/config` returns the complete merged active configuration.
- `POST /v1/config/upload` validates and activates a combined YAML upload.
- `POST /v1/config/reset` restores the built-in file configuration.
- `GET|PUT /v1/settings` reads or changes the basic simulation settings.
  `max_health` caps healing; omit it on `PUT` to keep the stored value.
- `GET|PUT /v1/hyperparameters` reads or validates the complete hyperparameter YAML.
- `GET|PUT /v1/actions` reads or validates and saves `core_engine/actions.py` (authoring bearer token required).
- `GET|PUT /v1/weights` reads or updates every weight exposed by the UI.
- `GET|PUT /v1/messages` reads (with defaults and allowed placeholders) or
  validates and saves the story messages in `config/messages.yaml`.
- `GET /v1/entities/{kind}` lists Locations, NPCs, Items, or Events.
- `GET|PUT /v1/entities/{kind}/{name}` reads, creates, renames, or updates an entry.
  NPC entries carry `unique` (`true`: lives in one location for the whole game;
  `false`: generic, appears wherever it fits). Omit it on `PUT` to keep the stored
  value. It is `null` for other kinds.
  NPC entries also carry `encounter_event` (an event that starts by itself when
  the NPC is in the scene; send `""` to remove it) and `encounter_repeat`
  (`true`: on every visit; `false`: once per game).
- `POST /v1/simulations` evolves a model and begins a playable session.
- `POST /v1/engines/load` loads a prior YAML or ZIP engine and begins a session.
- `GET /v1/sessions/{id}` reads the current scene and choices; `event` holds the
  title of the active event, or `null` outside events.
  `mission` holds the story's mission title and `won` becomes `true` once the
  goal is achieved. A won story has `ended: true` and offers no more actions.
- `POST /v1/sessions/{id}/actions` takes a selected action.
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

## Edit action logic

Set `ANE_AUTHORING_TOKEN` in the API server environment (Compose forwards this
variable), then use the same value as a bearer token. These two endpoints return
503 when the token is not configured and 401 when credentials are missing or
incorrect. Swagger's **Authorize** button also accepts the token.

Fetch the current source and revision:

```bash
curl http://localhost:8004/v1/actions \
  -H "Authorization: Bearer $ANE_AUTHORING_TOKEN" > actions-document.json
```

Edit the `source` string in `actions-document.json`, preserving its `revision`,
then save the complete document:

```bash
curl -X PUT http://localhost:8004/v1/actions \
  -H "Authorization: Bearer $ANE_AUTHORING_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @actions-document.json
```

The save response contains the new `revision`, a message, and
`restart_required: true`. Invalid Python or missing `Action`/`Outcome` classes
returns 400; a stale revision returns 409. Reload and reconcile your changes
before saving again. Saves keep the previous source in `actions.py.bak` and do
not execute the submitted code during validation. Restart engine processes to
apply it; syntax validation does not verify runtime behavior.

The UI exposes the same editor in **5 · Actions & app parameters**, loading the
latest source when the page opens. Local UI/API processes in the same checkout
edit the same file. Docker source edits belong to the service container that
saved them, so copy the edited file back to the project and rebuild both services
to retain and share those changes. Give the authoring token only to trusted
code authors, since their changes execute with the engine's permissions.
