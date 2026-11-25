# Building a frontend on the API
# Building a frontend on the API

This guide is for frontend developers who are building a new client for the Adaptive Narrative
Studio, for example in React. It also works as input to a code generator. It describes every screen
of the Gradio studio (`app.py`) in terms of HTTP requests and JSON fields. With this guide and the
OpenAPI schema you can rebuild the whole studio without reading `app.py`.

- **What the studio does** is in [Views](#views): each screen's components, the requests it
  makes, what it shows from each response, and the logic the studio runs in the browser, which you
  have to reimplement.
- **What the API accepts** is in the OpenAPI schema: `GET /openapi.json`, browsable at `/docs`.
  It has every request body, every query and path parameter, and the constraints on each. Many
  responses are untyped (`object`) in the schema. Their fields are given here, in
  [Shared shapes](#shared-shapes) and in the view that uses them.
- **Which view calls which endpoint** is in the [Endpoint index](#endpoint-index). It lists every
  route of `api.py`. `tests/test_studio_coverage.py` checks that the index matches the API
  exactly, so this guide fails the test suite when the two drift apart.

[API.md](API.md) describes the endpoints one by one, [STUDIO.md](STUDIO.md) describes the studio
for its users, and [WORLD.md](WORLD.md) describes the world format (conditions, effects, actions).

## The contract

### Connection

| Setting | Value |
| --- | --- |
| Base URL | The public deployment `http://ane.hamlet.iti.gr` (documentation at `/docs`), or `http://<host>:8005` for your own (Compose). Every route is under `/v1`, except `/healthz`, `/docs`, `/redoc` and `/openapi.json`. |
| Typed client | Generate one from `GET /openapi.json`, for example with `npx openapi-typescript http://ane.hamlet.iti.gr/openapi.json -o api.d.ts`. |
| Headers | `X-ANE-Client: studio` marks the request as coming from the studio. The records then say `origin: studio`, and runs belong to the user `test`. Any other client sends `X-ANE-User: <opaque id>` when it starts a run (see below). |
| CORS | Off by default. Set `ANE_CORS_ORIGINS=https://my-frontend.example,…` on the API to enable it. It then allows `GET PUT PATCH POST DELETE` and the headers `Content-Type`, `Authorization`, `X-ANE-Client` and `X-ANE-User`, without credentials. |
| Timeouts | Most calls return in under a second. The synchronous runs (`POST /v1/simulations`, and `play`, `continue` and `retrain` under `/v1/records/engines/{id}/`) can take minutes, and the studio allows 900 s. A frontend should use the job form of those endpoints instead (see [Long work: jobs](#long-work-jobs)). |
| One process | The API holds games, jobs, recent runs and download links in memory, in a single worker. A restart loses them. Treat a `404` for a session, job or artifact as "gone", and offer to start again. |

**Whose run it is.** Requests that start training (`POST /v1/simulations`, `POST /v1/jobs/simulations`,
and `continue` or `retrain` under `/v1/records/engines/{id}/` and `/v1/jobs/engines/{id}/`) must
identify the user. A frontend that is not the studio sends `X-ANE-User`, set to the id its backend
uses for the logged-in user. The id is opaque: 1-128 characters from letters, digits and `. _ : -`,
never a name or an e-mail address. Without the header the API answers `400` and nothing runs.
`GET /v1/records/runs?user_id=` lists one user's runs.

### Errors

Every error is JSON with a `detail` field.

| Status | `detail` | Meaning, and what the studio does |
| --- | --- | --- |
| `400` | a string, often several sentences or a Markdown list of problems | The content was refused, and nothing was written. The studio shows `detail` as the error message. |
| `404` | a string | Unknown entry, session, job, artifact, recorded item or undo token. |
| `409` | a string | A conflict: the value changed since you loaded it (`expected`), a name is already taken, an export platform is unavailable, or a report was asked for while the session goes on. Show the message and offer to reload. |
| `413` | a string | An upload is over its limit: configuration 2 MB, engine 20 MB, model parameters 100 KB. |
| `422` | a list `[{type, loc, msg, input, ctx}]` | The request has the wrong shape or type, or a value is out of range. The studio joins the list into `field.path: msg; …`, where the path is `loc` without its first element (`body` or `query`). |
| `503` | a string that says why and how to fix it | The language model or judge is not available (suggestions, world proposal, judging). |

```json
{"detail": [{"type": "less_than_equal", "loc": ["body", "candidate_count"],
             "msg": "Input should be less than or equal to 400", "input": 401, "ctx": {"le": 400}}]}
```

Messages never include server file paths. Show `detail` as it is; it is written for the user.
In the studio, every handler shows errors this way (`_error_message` and the `ui` decorator in
`app.py`).

### Long work: jobs

Training, retraining, judging, calibration, world proposals and records imports run as jobs.

1. A `POST` to one of the job endpoints (the `/v1/jobs/...` routes in the
   [Endpoint index](#endpoint-index)) answers `202` with the job. If an identical request is
   still `queued` or `running`, the API returns that job instead of queuing another, with
   `reused: true`. Otherwise `reused` is `false`.
2. Poll `GET /v1/jobs/{id}` until the job has **ended**: `finished`, `failed` or `cancelled`. The
   studio polls every **0.5 s**. The job is not ended while it is `queued`, `running` or
   `cancel_requested` (stopping), so keep polling. Jobs run one at a time, in the order they
   arrive. While a job is `queued`, `queue_ahead` says how many jobs run before it.
3. Show the ending:
   - `finished`: `result` is what the synchronous endpoint would have returned. For a training
     job it is the play state of the new game plus the training fields. If a cancellation came
     after the work had already completed, the job still finishes, with its result and a
     `cancel_note` to show.
   - `failed`: show `error`, which is a string.
   - `cancelled`: there is no `result`. `partial` is `{progress, note}`: the last progress, and a
     note saying what was kept. A cancelled training makes no engine, and a cancelled evaluation
     keeps no judgements.
4. A `404` while polling means the job was lost: jobs live only in memory, so an API restart
   loses them. Say so and offer to start again.

**Cancelling.** `POST /v1/jobs/{job_id}/cancel` works on `simulation`, `continue`, `retrain`,
`judge-benchmark` and `calibrate-fitness` jobs; `cancellable` on a job says whether it can be
cancelled now. A `queued` job becomes `cancelled` at once and never starts. A `running` job
becomes `cancel_requested` and stops at its next safe point: a training at the end of the
generation being played, an evaluation before the next story is judged. Then it is `cancelled`,
or `finished` if its work completed first. An ended job is unchanged. The answer is the job,
without its result, plus a `message`. The API answers `409` for a job of another kind, and `404`
for an unknown job.

```json
{"id": "50e2c14a…", "kind": "simulation", "description": "2 generation(s) of 2 storytellers",
 "status": "running", "created_at": "2026-11-20T10:17:18+00:00", "started_at": "2026-11-20T10:17:18+00:00",
 "finished_at": null, "result": null, "error": null, "partial": null, "cancellable": true, "queue_ahead": null,
 "progress": {"generation": 1, "total": 2, "label": "generation", "best": 601.071, "mean": 492.433,
              "validation": 519.32, "improved": true}}
```

`progress` comes in two forms:

- **Training** (`simulation`, `continue`, `retrain`): `{generation, total, label: "generation",
  best, mean, validation, improved}`. Draw a progress bar from `generation / total` and a chart
  of `best`, `mean` and `validation`.
- **Counting jobs** (judge benchmark, calibration, imports): `{generation, total, label}`. Here
  `generation` is the number of steps done so far, and `label` names them, for example
  "stories judged".

`GET /v1/jobs` lists the latest 100 jobs, newest first, without `result`. Each job carries
`has_result`, `queue_ahead` and `cancellable`. A job lives only in memory, so after an API
restart its id answers `404`.

### Downloads and uploads

- Generated files come as relative URLs in fields named `*_download_url` or `download_url`, such
  as `/v1/artifacts/<id>`. Fetch the URL against the API base and save the body under the name in
  `Content-Disposition`. A link stays valid while the API process runs.
- Some downloads are plain `GET` endpoints instead: `GET /v1/records/engines/{id}/model`,
  `GET /v1/records/worlds/{id}?format=yaml` and `GET /v1/sessions/{id}/savegame`. The savegame
  endpoint returns JSON, which the studio saves as a file.
- Uploads are `multipart/form-data`, and the API recognises a file by its extension:
  - `POST /v1/config/upload`: field `file`, `.yaml` or `.yml`.
  - `POST /v1/engines/load`: field `file` (`.ane.zip`, `.zip`, `.yaml` or `.yml`) and an optional
    field `parameters` (a `.yaml` file). The options go in the query string: `profile`,
    `stats_mode`, `stats` (a JSON object as text) and `mission`.

### Editing: drafts, conflicts and undo

- **Writes are checked as a whole.** `PUT` or `POST` validates the whole world with the change in
  place. A refusal (`400` or `422`) writes nothing, so keep the user's draft and show the message.
  A success answers the saved value, often with a `message` in Markdown to show. Reload anything
  that depends on the change; each view lists what to reload.
- **Fields left out of a `PUT` keep their stored value**, unless the endpoint replaces the whole
  object (`…/policy`, the documents). To remove a value, send it empty: `""` for a string, `{}` for
  an object. Each field's description in the OpenAPI schema says which applies.
- **Optimistic concurrency** works in two places:
  - `PUT /v1/weights`: send `expected`, a map `{path: value}` of the values as you loaded them.
    The API answers `409` if any of those parameters has changed since.
  - `DELETE /v1/entities/{kind}/{name}/actions/{index}?expected_name=`: the API answers `409` if
    the action at that index is no longer the one you meant.
- **Undo after delete.** Deleting an entry, an action or a mission returns
  `undo: {token, type, name, kind, entry, position, of}`. Offer an Undo button that calls
  `POST /v1/deletions/{token}/restore`. The answer says where the item went back (`position`,
  `was`, `moved`, `changed_since`, `message`). A `409` means the name has been taken again or the
  entry is gone, a `400` means the item no longer fits the world, and a `404` means it was undone
  already or is older than the 200 most recent deletions.
- **Unsaved changes.** The studio tracks a draft per form and asks before discarding it:
  when the user switches entry or section, leaves the page, or reloads. It also keeps drafts in
  browser storage so they survive a reload. The view sections give the details for each form.

### Names in paths

Entry names contain spaces and apostrophes, for example `Thieves' Den`. Percent-encode every path
segment that holds a name or key, as `encodeURIComponent` does. `kind` is one of `Locations`,
`NPCs`, `Items` or `Events`, matching the `entity_kinds` from `GET /v1/reference`.

### Start-up data

Before building its forms, the studio loads these reference endpoints. A frontend should cache
them and refresh them after a configuration upload or reset.

| Request | Gives |
| --- | --- |
| `GET /v1/reference` | `entity_kinds`, `common_tags`, `action_reference`, `repeat_rules`, `repeat_default`, `goal_keys`, `rule_choices`, `rule_fields` (the story-rule form), `player_stat_choices`, `documents`, `npc_roles`, `judge_criteria`, `defaults` (for a new run) |
| `GET /v1/entities/{kind}?fields=names` | `{kind, names: [...]}`, the choices for every entry picker |
| `GET /v1/judge/status` | `{available, mode, model, server, reason, how}`: whether to enable the language-model features or show `reason` and `how` |
| `GET /v1/exports/platforms` | `{platforms: [{…, available, reason}], code_version, protocol}` |
| `GET /v1/app-settings` | `{settings, defaults}`: limits and defaults that bound the training form |

## Shared shapes

### Play state

`GET /v1/sessions/{id}`, `POST /v1/sessions/{id}/actions` (body `{"action": "<one of actions>"}`),
`POST /v1/sessions/{id}/end`, the result of a training job, and every endpoint that starts a game
all answer with the same object. Excerpt from a real response:

```json
{
 "session_id": "208293b8…", "ended": false, "won": false, "end_reason": null,
 "scene": "## Tavern\n\n**Quest:** A face for the Palace · stage 1/3: …",
 "actions": ["(Merchant) Ask about forbidden merchandise", "(Merchant) Talk with Merchant", "…"],
 "choices": [{"name": "(Merchant) Ask about forbidden merchandise", "check": null}, "…"],
 "last_turn": "> (Merchant) Ask about the market thief\n\nRESULT: \"Patched cloak, …\"",
 "turn_summary": {"turn": 1, "action": "(Merchant) Ask about the market thief", "with": "Merchant",
                  "where": "Tavern", "moved_to": null, "check": null, "succeeded": true, "told": "…",
                  "health": 8, "health_change": 0, "coins": 10, "coin_change": 0, "items_gained": [],
                  "items_lost": [], "met": [], "new_places": [], "event": null, "quest_done": [],
                  "quest_now": "Commission a mask from the Maskwright", "quest_complete": false,
                  "ended": false, "end_reason": null},
 "talking_to": "Merchant", "event": null,
 "location": "Tavern", "scene_name": "Tavern", "start_location": "Tavern",
 "description": "A lively, smoke-filled establishment …",
 "items_here": [{"name": "Vintage Wine", "text": "A dusty bottle of vintage red wine."}],
 "health": 8, "max_health": 10, "coins": 10, "power": 2.138, "inventory": [],
 "stats": {"attack": 55, "defence": 70, "stealth": 80, "persuasion": 45},
 "stat_points": {"attack": 11, "defence": 14, "stealth": 16, "persuasion": 9}, "last_check": null,
 "mission": "A face for the Palace",
 "mission_status": "A face for the Palace (stage 1/3: …): in progress",
 "quest": {"key": "a_face_for_the_palace", "title": "A face for the Palace", "description": "…",
           "status": "in progress",
           "stages": [{"title": "Commission a mask from the Maskwright", "description": "…", "state": "current"},
                      {"title": "Walk into the Palace", "description": "…", "state": "todo"}],
           "todo": ["Get the Official's Mask"]},
 "people": [{"name": "Merchant", "called": "a Merchant", "description": "…", "wants": null, "hostile": false,
             "talks": 1, "quest": false, "helped": 0, "ally": false, "role": "bystander", "importance": 0}],
 "people_met": [{"name": "Merchant", "called": "a Merchant", "where": ["Tavern"], "talks": 1, "hostile": false,
                 "gone": false, "ally": false, "role": "bystander", "importance": 0}],
 "map": {"current": "Tavern", "previous": null,
         "places": [{"name": "Market", "visited": false, "locked": false}, {"name": "Tavern", "visited": true, "locked": false}],
         "paths": [["Market", "Tavern"]], "one_way": []},
 "ways_on": [{"name": "Market", "visited": false, "toward_quest": true}],
 "stakes": {"alert": 0, "max": 6, "warn_at": 4},
 "cast": {"left_out": {"locations": ["Bone Orchard"], "npcs": ["Thief", "…"], "items": ["…"], "events": []},
          "kept": {"locations": 15, "npcs": 15, "items": 7, "events": 8}},
 "unique_actions": [{"turn": 1, "action": "(Merchant) Ask about the market thief", "source": "npc",
                     "owner": "Merchant", "where": "Tavern", "succeeded": true, "kept": false}],
 "session_report": null,
 "profile": "default", "variables": {"thief_caught": false, "reputation": 0, "playthroughs": 0}
}
```

- Send back **one of `actions` exactly as given**. Any other string is refused with `400` ("That
  choice is no longer available"), so after that error, reload the session.
- `scene` and `last_turn` are Markdown, ready to render. The structured fields are there for a
  client that draws its own panels, as the studio's ▶ Play view does.
- The game is over when `ended` is true. It then offers no `actions`, and
  `GET /v1/sessions/{id}/report` returns its report (`409` before then).
- The [▶ Play](#-play) views list every field and where it is shown.

### Training result

A training's result (from the job or the synchronous endpoint) is a play state with these extra
fields: `game_started` (false when the request set `start_game: false`; `session_id` is then
`null`), `report`, `run_id`, `engine_id`, `learning`
(`history[]: {best, mean, worst, spread, diversity, mutation_step, validation}`, `learned_traits`,
`best_genome`, `improvement`, `generations_run`, `early_stopped`, `warm_started_from_memory`),
`training` (`overrides`, `sources`, `resolved`, `executed`), and the downloads
`config_download_url` and `engine_download_url`.

### Engine card

`GET /v1/engines/{engine_id}/card` describes a trained engine wherever the studio lets the user
pick one (Train, Play, Evaluate, Export): `engine_id`, `run_id`, `name`, `label`,
`default_label`, `created_at`, `kind`, `source`, `parent_id`, `world`, `generations`,
`trained_generations`, `settings`, `fitness`, `judge_in_training`, `evaluations` and `resumable`.
`PUT /v1/engines/{engine_id}/name` with `{"name": "…"}` renames the engine; `null` or `""` goes
back to `default_label`. Pickers show `label`.

### Undo token

Excerpt of `DELETE /v1/missions/{key}`:
`{"missions": {…}, "message": "…", "undo": {"token": "…", "type": "mission", "name": "expose_the_fence", …}}`.
Then `POST /v1/deletions/{token}/restore` answers:

```json
{"type": "mission", "name": "expose_the_fence", "kind": null, "entry": null, "position": 1, "was": 1,
 "moved": false, "changed_since": false, "message": "Restored the mission **expose_the_fence**, where it was (1st)."}
```

### Parameter metadata (weights and tuning)

`GET /v1/weights` answers `values` (`{path: value}`), `parameters` and `basic`.

- Each item of `parameters` describes one parameter:
  `{path, value, section, label, explanation, unit, type, minimum, maximum, default, source,
  described, requires, inactive}`. `type` is `integer` or `float`.
- `basic` lists the Basic view's areas: `[{key, title, summary, controls: [{path, effect}]}]`.

Generate the number inputs from the metadata: `type` gives the step, and `minimum` and `maximum`
give the bounds. When a parameter has `inactive` set, show that reason and disable the input.

# Views

The studio has five destinations in workflow order, then Advanced: 🌍 World → 🧠 Train → ▶ Play →
⚖ Evaluate → 📦 Export, and ⚙ Advanced. Each view below begins with `view: <id>`, a stable id
that a router or code generator can use. For each view the guide gives what it loads on open, its
components, its actions as requests, the logic the studio runs in the browser (which you must
reimplement), and where its buttons navigate.


## App shell

### App shell › API client & request conventions
<a id="api-client"></a>`view: api-client` (not a screen: rules every view follows)

**Purpose.** How the studio talks to the API, so that every view below can be rebuilt on plain `fetch`.

**On open.** Nothing by itself (see *Global loads*).

**Components.** None.

**Actions.** None of its own. Conventions (StudioAPI in studio_client.py, used by app.py):

| Concern | Rule |
|---|---|
| Base URL | env `ANE_API_URL` (a trailing `/` is dropped; Compose: `http://hamlet-ane-api:8005`, or e.g. `http://ane.hamlet.iti.gr`); unset or empty → `http://127.0.0.1:8005`. Always HTTP: the studio never serves the API itself. Timeout 900 s. |
| Header | every request sends `X-ANE-Client: studio` (the API records origin `studio`; runs are user `test`). A React client should send it too to keep the same records attribution. |
| Query params | params whose value is `null`/`None` are left out of the query string. |
| Bodies | JSON for PUT/PATCH/POST; uploads are `multipart/form-data`, one field per file (field names given per action). |
| Responses | `Content-Type: application/json*` → parsed JSON; anything else is a file. |
| Downloads | `GET <path>`; the saved file name is taken from `Content-Disposition: filename="…"`, else a name the view supplies, else the last path segment. Shown as a downloadable file link. |
| Jobs | A `POST` to a job endpoint (`/v1/jobs/...`; 202) returns a job `{id, kind, description, status, created_at, started_at, finished_at, progress, result, error}`. Poll `GET /v1/jobs/{job_id}` every **0.5 s** until `status` is `finished` (use `result`) or `failed` (raise `error`, or "The job failed." when empty). `progress` is `{generation, total, best, mean, validation, improved}` for trainings, or `{generation, total, label}` for jobs counting their own steps (import, judging). |
| Path escaping | every path segment after the first that is a name/id is URL-encoded with no safe characters (`encodeURIComponent`) (`_path`). |

### App shell › Errors
<a id="errors"></a>`view: errors` (convention)

**Purpose.** One way to show every failure: the API's own message, as an error notification.

**On open.** —

**Components.** | Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Error notification | status-line (toast / modal error) | the message below | hidden | Gradio shows it as a red "Error" toast with the text; it does not replace any view content. |

**Actions.** —

**Client-side logic to reimplement.**
- Turning a failed response into text (`_error_message(response)`):
  - Network failure / timeout → `The API at {base_url} could not be reached: {exception text}`.
  - HTTP status ≥ 400, body is JSON with `detail`:
    - `detail` is a **list** (FastAPI 422 request validation): for each item `"{loc[1:] joined with '.'}: {msg}"`, items joined with `"; "`. The first `loc` element (`body`/`query`/`path`) is dropped, e.g. `[{"loc":["body","pop_size"],"msg":"Input should be greater than or equal to 1"}]` → `pop_size: Input should be greater than or equal to 1`.
    - `detail` is a **string** (400 validation/authoring errors, 404 not found, 409 conflict, 413 too large, 503 judge unavailable) → shown verbatim. The API already strips server file paths from it (`public_message`).
    - anything else (dict) → its string form (unclear: Python `repr`; a React client should `JSON.stringify` it).
  - Body not JSON → `"{status}: {first 500 chars of the body}"`.
  - A failed job → its `error` (or `The job failed.`).
  - Bad YAML parsed on the client → `"{what} is not valid YAML: {parser error}"`.
- **`ui` decorator.** Every handler marked `@ui` catches the API error and re-raises it as a UI error with the same message: the error notification is shown, **none of the action's outputs change**, and chained follow-up steps that run only on success (e.g. "then show the session parameters") do not run. Client-side guards inside handlers raise the same kind of UI error with their own text (listed per action below as "error: …"). Handlers **not** marked `@ui` (`integration_session_text`, `studio_status_ui`, `export_platforms_table`) catch errors themselves (log, and show less). A rebuild should show the API's message wherever an error reaches the user.
- `_recorded(path, key)` (record lists): any API error → empty list; a 404 is silent (no records yet), others are only logged.

**Navigation.** —

### App shell › Header & status line
<a id="header"></a>`view: header`

**Purpose.** Title, one-line subtitle, and a live summary of the studio's state (world size, trained AIs, recording).

**On open.** (page load) `studio_status_ui`:
- `GET /v1/world/catalogue` → `locations[]`, `npcs[]`, `items[]`, `events[]`, `missions[]` (counted).
- `GET /v1/exports/engines` → `engines[]` (counted).
- `GET /v1/records` → `enabled` (bool).
Each request is independent; a failing one just leaves its part out.

Re-fetched (same three requests) after: a training finishes or a recorded engine is continued/retrained (🧠 Train); an engine file is loaded in ▶ Play; a proposal is added in 🌍 World → Grow. Not refreshed after anything in ⚙ Advanced.

**Components.**
| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Page title | text | "Adaptive Narrative Studio" | — | browser `<title>` too |
| Hero | markdown | `# Adaptive Narrative Studio` + "Create a world, train an AI storyteller on it, then play it as a game." | — | gradient banner (`#17233f`→`#4a244f`, white text) |
| Status line | status-line (markdown) | parts below joined with ` · ` | empty | left border 4px violet `#8b5cf6` |

**Client-side logic to reimplement.** Status line parts, in order, each only when its request succeeded:
1. `🌍 **World:** {n locations} places · {n npcs} people · {n items} items · {n events} events · {n missions} quests`
2. `🧠 **Trained AIs:** {n engines}` + (` (train one in 🧠 Train)` when 0)
3. `🗂 Recording on` / `🗂 Recording off` (from `enabled`)

**Navigation.** —

### App shell › Navigation
<a id="nav"></a>`view: nav`

**Purpose.** Choose one of six destinations; every destination keeps its state when left (nothing is re-rendered/unmounted).

**On open.** (page load) `open_requested`: reads the page's query string `?section=<id>` (trimmed, lower-cased); a known destination id is opened, anything else (or none) opens `world`. No request.

**Components.**
| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Workflow banner | button ×6 (`#ane-banner`) | `DESTINATIONS` (hard-coded, below) | `world` highlighted | current = primary variant, others secondary. Prefixed by a "Workflow" caption; each of the first 5 (workflow) buttons except the last shows a trailing `→`; ⚙ Advanced is pushed to the right (`margin-left:auto`). Button ids `#nav-<id>`. Hidden below 820 px width. |
| Menu | radio (`#ane-menu`), label "☰ Menu: go to a section" | same list, by label | `world` | Only shown below 820 px width (replaces the banner). |
| Destination tabs | tabs (`#ane-nav`), each tab `id=<id>`, `elem_id=dest-<id>` | — | `world` | The tab bar itself is hidden (CSS); only the banner/menu switch tabs. |

Hard-coded `DESTINATIONS` (id → label): `world` 🌍 World, `train` 🧠 Train, `play` ▶ Play, `evaluate` ⚖ Evaluate, `export` 📦 Export, `advanced` ⚙ Advanced. Workflow = first five.

Sub-tab ids inside destinations (not deep-linkable: `?section=` accepts destination ids only): World sections `entries`, `missions`, `story-rules`, `map`, `behaviour`, `grow`, `checks`; Train `train-run`, `train-records`, `train-engines`; Advanced `advanced-records`, `advanced-integration` (Simulation, Weights, Engine logic, App & jobs have no id).

**Actions.**
| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Banner button / menu choice (`nav_choice_ui` → `navigate`) | none | target id; current destination (`dest_now`), current World section, the World editors' unsaved parts (`drafts_dirty`: `{editor: [parts]}`) | select the target tab, highlight its banner button, set the menu to it, remember it as `dest_now`. | **Leave guard:** when the current destination is `world`, the target is not `world`, and any World editor among `entries`, `missions`, `rules`, `map` (checked in that order) has unsaved parts → stay on World, open that editor's section (`entries`/`missions`/`story-rules`/`map`), and show that editor's prompt: `### ● Unsaved changes` + "{who} has unsaved changes in **{parts}**. Save them before leaving the editor for {target label}? **Discard** drops them; **Stay** keeps editing." (Save/Discard/Stay handled by the World editor; on Save/Discard it then navigates to the target.) |
| A destination tab selected by a link inside a view (e.g. Train's "▶ Play this result") (`mark_destination`) | none | target id | banner + menu follow, `dest_now` set | unknown id → `world` |
| Page load (`open_requested`) | none | `?section` | as `navigate` | no leave guard |

Cross-view refreshes run on navigation (all after the tab switch):
- Banner "⚙ Advanced" **or any menu choice** → refresh 🔌 Integration's session note (`integration_session_text`, no request).
- Banner "🧠 Train" **or any menu choice** → Train's run summary (`run_form_ui`, see Train doc).

**Client-side logic to reimplement.**
- Navigation never unmounts a destination (forms, a running game, a running job keep their state). In React: keep all six mounted and toggle visibility, or lift their state.
- Responsive switch at 820 px: banner ↔ menu.

**Navigation.** This is it. Links inside views that move to a destination: Train → Play/Evaluate/Export (carry an engine id; see Train doc).

### App shell › Global loads & browser storage
<a id="global-loads"></a>`view: global-loads`

**Purpose.** What the app fetches once, and what it keeps in the browser.

**On open.**
- At server start (UI build, once per server process — values then frozen into the page until the server restarts, unless a view reloads them): `GET /v1/reference` (`entity_kinds`, `rule_fields`, … used by World), `GET /v1/app-settings` (`settings`, `defaults` → App & jobs), `GET /v1/settings` (→ Simulation form), `GET /v1/variables/profiles` (`profiles[]`, `default` → profile pickers incl. Integration), `GET /v1/stats`, `GET /v1/entities/{kind}` (Locations names, first entry), `GET /v1/hyperparameters`, `GET /v1/weights`, `GET /v1/logic`, `GET /v1/logic/{section}` (`evolution`), `GET /v1/records`, `GET /v1/records/engines|stories|runs|worlds`, `GET /v1/exports/platforms`, plus others owned by World/Train/Play/Export. A React client should fetch these when the view opens.
- On every page load: header status line; `?section` navigation; ⚙ Advanced → Weights (`reload_weights_ui`); ⚙ Advanced → 🗂 Records import folder note (`import_folder_ui`); plus loads owned by other views (World entry/forms/behaviours/world size/judge status; Train restored result & run summary; Play runs/engine card/judge status; Evaluate judge status; Export form/cards; the three Undo histories).

**Components.** None.

**Client-side logic to reimplement.** localStorage keys (Gradio BrowserState; owned by the named views):
| Key | Holds | Owner |
|---|---|---|
| `ane-entry-draft` | the 🌍 World entry editor's unsaved draft | World → Entries |
| `ane-train-result` | 🧠 Train's completed engine (restored on reload) | Train → Run |
| `ane-play-session` | ▶ Play's game (its `session_id`; reopened on reload while the API has it) | Play → game |
| `ane-undo-entry`, `ane-undo-action`, `ane-undo-mission` | each delete panel's Undo history (deletion tokens) | World |

**Browser leave prompt.** On `beforeunload`, if the page contains any element matching `.draft-status[data-dirty="1"]` the browser's "Leave site?" prompt is shown. Two producers: the World entry draft indicator and ⚙ Advanced → Weights draft status. (Navigating between destinations inside the app does not ask for Weights; only World editors are guarded in-app.)

**Navigation.** —

## 🌍 World

The 🌍 World destination (`view: world`, the default destination) authors the world. It has seven sections, as tabs, in this order (ids → labels, hard-coded `WORLD_SECTIONS`): `entries` "📝 Entries", `missions` "🎯 Missions", `story-rules` "📜 Story rules", `map` "🗺 Map", `behaviour` "🏷 Behaviour by tags", `grow` "✨ Grow the world", `checks` "🔍 Checks". `entries` is open first. Four sections are **editors with a draft** (unsaved-change tracking): `entries` (editor key `entries`), `missions` (`missions`), `story-rules` (`rules`), `map` (`map`). The client keeps one shared map `drafts_dirty = {editor: [unsaved part ids]}` (e.g. `{"entries": ["fields","scheduling"], "map": []}`) and the id of the open section (`section_now`). Leaving a section, or leaving 🌍 World through the banner/menu, with unsaved parts asks first (Save / Discard / Stay) inside the editor being left; closing or reloading the browser tab with any unsaved draft triggers the browser's native "leave page?" dialog. This file documents the section switching/leave guard and everything in 📝 Entries; the other sections' own forms are documented elsewhere (they reuse the same guard).

**Dropdowns of the world's names are fetched when opened** (`fresh_choices_ui`, on each dropdown's focus): the action editor's skill check, event, item and place/person/item pickers, Scheduling's "Lies at", More details' known places and items, 🗺 Map's Place, 📜 Story rules' Arrest location and Persistent variables' Player profile. Each asks `GET /v1/entities/{kind}?fields=names` (`/v1/stats`, `/v1/variables/profiles`) and keeps its current value among the choices, so a form that refers to an entry deleted meanwhile still sends it and the API says what is wrong.

Global, not part of this view: the one-line **studio status** above all destinations (`studio_status_ui`): on app load `GET /v1/world/catalogue` → `"🌍 **World:** {len(locations)} places · {len(npcs)} people · {len(items)} items · {len(events)} events · {len(missions)} quests"`, `GET /v1/exports/engines` → `"🧠 **Trained AIs:** {len(engines)}"` (+ `" (train one in 🧠 Train)"` when 0), `GET /v1/records` → `"🗂 Recording on|off"` from `enabled`; parts joined with `" · "`, a failing request just drops its part. It is re-fetched after ✨ Grow adds entries (and after Play events), **not** after entry saves/deletes in 📝 Entries.

### 🌍 World › Section switching and the leave guard
<a id="world"></a>`view: world` (section tabs; ids above)

**Purpose.** Switch between the World's sections, and never silently leave an editor that has unsaved changes.

**On open.** No request. `section_now = "entries"`.

**Components.**

| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Section tabs | radio (tab bar) | `WORLD_SECTIONS` (hard-coded, above) | `entries` | Selecting one runs the guard below |
| Banner buttons / "☰ Menu: go to a section" | button ×6 / radio | destinations (global) | `world` | Leaving `world` runs the guard below |
| Each editor's "Unsaved changes" prompt | markdown + 3 buttons | set by the guard | hidden | For 📝 Entries: the entry prompt (see 📝 Entries); the other editors have their own |

**Actions.**

| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Select a section tab (`section_choice_ui` via `leave_emit`) | none | `section` chosen, `section_now`, `drafts_dirty`, the entry being edited (`loaded`) | If the editor of `section_now` (map: `entries→entries`, `missions→missions`, `story-rules→rules`, `map→map`; other sections have no editor) has unsaved parts and `section != section_now`: re-select `section_now` (the switch is undone; `section_now` unchanged) and open that editor's prompt with pending target `{type:"section", id: section}` and the message below. Else `section_now = section`. | Only the editor of the section being left is checked (unsaved Entries do not block going Missions → Grow). |
| Banner button / menu (`nav_choice_ui` via `leave_emit`) | none | destination, current destination (`dest_now`), `section_now`, `loaded`, `drafts_dirty` | If current destination is `world`, target is not `world`, and any editor has unsaved parts (first in order `entries, missions, rules, map`): stay on World, select that editor's section (`section_now` = it), open its prompt with pending `{type:"destination", id}`. Else navigate. | — |
| Prompt **💾 Save / Discard / Stay** | see each editor | — | Save: save the unsaved parts, then carry out the pending target (navigate / select the section). Discard: reload the editor as stored, then carry out. Stay: close the prompt. | A failed save keeps the prompt open with the error. |
| Browser tab closed/reloaded | none | — | — | `beforeunload` handler: if any element `.draft-status[data-dirty="1"]` exists (any editor's indicator says unsaved), call `event.preventDefault(); event.returnValue = ""` (native dialog). |

**Client-side logic to reimplement.**
- Prompt message (`_leave_question`): `"### ● Unsaved changes\n{who} has unsaved changes in **{parts}**. Save them before {going}? **Discard** drops them; **Stay** keeps editing."` — for Entries `who` = `**{entity}** ({kind})` or `the new {kind without trailing s} entry` (e.g. "the new NPC entry"), `parts` = labels of the unsaved parts joined by ", " (labels in 📝 Entries). For other editors `who` is the editor title ("🎯 Missions", "📜 Story rules", "🗺 Map").
- `going` (`_going`): section → `leaving the editor for 🌍 World → {section label}`; destination → `leaving the editor for {destination label}` (e.g. "🧠 Train"); entry → `opening {entity or 'the content type'} ({kind})`; new entry → `starting a new entry`; action → `opening another action` / `starting a new action`.
- The guard reads only `drafts_dirty` (which parts are unsaved), never the draft values.

**Navigation.** Leaving with Save/Discard goes to the target section/destination; nothing else is carried.

### 🌍 World › 📝 Entries
<a id="entries"></a>`view: entries`

**Purpose.** Pick a content type and an entry (place, person, item, event), or start a new one; edit it in six inner tabs, all sharing one **draft** with unsaved-change tracking, a Save/Discard/Stay prompt, and a draft kept in browser storage across reloads.

**On open.** On app load (Entries is the default view), with `kind = "Locations"` and `entity` = its first name (`load_selection_ui`, then baseline capture):
- Once at app start: `GET /v1/reference` → `entity_kinds` (radio choices, order `Locations, NPCs, Items, Events`), `common_tags`, `repeat_default` (`"default"`), `repeat_rules` (`["once","per_visit","always"]`), `npc_roles` (list of role keys), `action_reference` (markdown).
- Choice lists for the action/outcome/scheduling/details forms: `GET /v1/entities/{kind}?fields=names` for `Locations`, `NPCs`, `Items`, `Events` → `names: [str]`; `GET /v1/stats` → keys of `character_stats.stats` (stat names). (app.py builds these once when the server starts and never refreshes them; a reimplementation should fetch them on open and after entries are created/renamed/deleted.)
- The full entry load (`_entry_load(kind, name)`), each part detailed in its inner tab:
  - `GET /v1/entities/{kind}?fields=names` → Entry dropdown choices.
  - Fields: `GET /v1/entities/{kind}/{name}` + `GET /v1/entities/Events?fields=names` (encounter-event choices).
  - Every field (YAML): `GET /v1/entities/{kind}/{name}/yaml` → `yaml`.
  - Actions YAML: `GET /v1/entities/{kind}/{name}/actions/yaml` → `yaml`.
  - Scheduling YAML: `GET /v1/entities/{kind}/{name}/policy/yaml` → `yaml`.
  - Actions: `GET /v1/entities/{kind}/{name}/actions` → `actions[]` (picker choices; blank action form).
  - Scheduling: `GET /v1/entities/{kind}/{name}/policy` → `policy` (only for `Events`, `Items`).
  - More details: `GET /v1/entities/{kind}/{name}/details` → `details` (only for `NPCs`, `Locations`) + names of `Locations` and `Items` (choices).
  - For a new entry (`name` null) nothing is fetched per entry: blank values (see each tab).
- `GET /v1/behaviours` (behaviour dropdowns in Fields).
- Browser storage read: `ane-entry-draft` (kept draft → offer), `ane-undo-entry`, `ane-undo-action` (Undo histories).

Cross-view refreshes into this view:
- ✨ Grow the world, after adding proposed entries: `GET /v1/entities/{kind}?fields=names` → Entry dropdown choices, current value kept (`refresh_entry_names_ui`); nothing reloaded.
- 🏷 Behaviour by tags, after saving a rules table: `GET /v1/behaviours` → the two behaviour dropdowns of Fields recomputed.

**Components.**

| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| "Content type" | radio | `reference.entity_kinds` | `Locations` | Guarded choice (below) |
| "Entry" | dropdown | `GET /v1/entities/{kind}?fields=names` → `names` | first name | Guarded; empty value = new entry |
| "New entry" | button | — | — | Guarded |
| Draft indicator | status-line (html) | client draft state | "✓ No unsaved changes" | Dirty: `● Unsaved changes in {entity or "the new entry"}: {part labels}`; carries `data-dirty="1"` (used by `beforeunload`) |
| "Unsaved changes" prompt | markdown + buttons "💾 Save", "Discard", "Stay" | client | hidden | Shown by guarded choices and the World leave guard |
| "Unsaved changes from your last visit" offer | markdown + buttons "↺ Restore", "Discard" | browser storage `ane-entry-draft` | hidden | Shown on app load if a kept draft exists |
| Inner tabs | tabs | "Fields", "Actions & outcomes", "Scheduling", "More details", "✨ Suggest", "Every field (YAML)" | Fields | Switching inner tabs is not guarded (same draft) |

**Actions.**

| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Change "Content type" (`entry_choice_ui`) | `GET /v1/entities/{kind}?fields=names` then, if it goes ahead, the full entry load | new kind; its first name (none: new entry) | Loads that entry; all parts become the baseline; draft clean | If kind/entity equal what is loaded: nothing. If any part unsaved: radio and dropdown are reset to the loaded kind/entity, prompt opens with pending `{type:"entry", kind, entity}` |
| Change "Entry" (`entry_choice_ui`) | full entry load | `kind`, `entity` | as above | same guard |
| "New entry" (`new_entry_choice_ui`) | `GET /v1/entities/{kind}?fields=names`, `GET /v1/entities/Events?fields=names`, `GET /v1/entities/{Locations,Items}?fields=names` (no per-entry request) | — | Blank form for the loaded kind (`loaded.entity = null`, Entry dropdown keeps choices; value cleared); baseline = blank values | Guarded (any unsaved part) with pending `{type:"new_entry"}` |
| Prompt "💾 Save" (`resolve_draft_ui("save")`) | the per-part saves of each unsaved part (see inner tabs), in order | scope: only the `action` part when pending is an action switch, else all unsaved parts | All saved → carry out the pending target (load entry / blank / action / navigate / select section); for entry/new-entry/section/destination targets the current entry is first reloaded as stored with its (possibly new) name | **Conflict** (both parts of a pair unsaved: `entry_yaml` with any other part; `actions_yaml` with `action`; `policy_yaml` with `scheduling`): nothing saved; prompt shows `⚠ **Nothing was saved:** {A} and {B} both change the same data. Choose **Stay** and save the one to keep with its own button, or **Discard** both.` **A part fails**: stop; prompt stays with `⚠ **{part} could not be saved** ({parts} saved before it): {error}  \nNothing was discarded: correct it and **Save** again, **Stay**, or **Discard**.`; if a new entry got created before the failure, select it in the Entry dropdown and treat it as loaded |
| Prompt "Discard" (`resolve_draft_ui("discard")`) | full entry load (current entry, as stored) or the target load | — | Draft dropped; carry out the pending target | — |
| Prompt "Stay" | none | — | Prompt closed, pending cleared | — |
| Offer "↺ Restore" (`restore_kept_draft_ui`) | `GET /v1/entities/{kind}?fields=names`, full entry load of the kept `kind`/`entity` | kept draft | Entry opens as stored (that becomes the baseline), then each kept part's values are written over the form (unsaved). For a kept `action` part, the action picker shows `state.index` (choices from `GET .../actions`) and the outcome picker shows `state.action.outcomes` / `state.outcome`. Unsaved parts = kept parts that differ from the new baseline. Offer hidden | If anything is unsaved now: error toast "Save or discard the changes you made since first." If the kept entity no longer exists: offer stays with `⚠ **{name}** is no longer in {kind}: its kept changes cannot be restored.` and the kept draft is cleared |
| Offer "Discard" (`discard_kept_draft_ui`) | none | — | Kept draft cleared (`ane-entry-draft = {}`), offer hidden | — |

**Client-side logic to reimplement.**
- **Draft parts** (ids → labels shown to the user, `DRAFT_PARTS`): `fields` "Fields", `action` "Actions & outcomes", `scheduling` "Scheduling", `details` "More details", `entry_yaml` "Every field (YAML)", `actions_yaml` "Actions as YAML", `policy_yaml` "Scheduling as YAML". Lists of parts are always shown in this order.
  - `fields`: the 14 Fields components (name, tags, descriptions, goals, unique, encounter_event, encounter_repeat, enabled_if, event title, stat_bonus, start_eligible, start_weight, role, secret).
  - `action`: the hidden action state `{kind, name, index, action, outcome}` + 14 action fields + 17 outcome fields (not the two pickers).
  - `scheduling`: 8 fields (trigger, chance, weight, cooldown, at most, how often, unique, lies at). `details`: 9 fields. The three YAML parts: their text.
- **Baseline**: `draft_base = {part: canonical value}` set from exactly the values a load or save puts into the form, in the same step (never from a later read-back), for the parts that load/save touched. A part is **unsaved** when its canonical current value ≠ its baseline (a part never loaded has nothing unsaved). Each edit (`input` event of any component of a part; also after outcome switch/add/delete, after behaviour choice, after "Add to the form") recomputes only that part (`entry_part_status_ui`) and updates `drafts_dirty.entries`, the indicator and the kept draft. Loading an entry/action/outcome is never an edit; typing a value back to the baseline clears it.
- **Canonical form** (`_plain`): null → `""`; booleans as is; numbers: NaN → `""`, whole → int, else rounded to 9 decimals; strings: trimmed, trailing spaces removed per line; tables: rows of cell values (strings trimmed), empty rows dropped; dicts: sorted key/value pairs; lists: element-wise. The `action` part compares the action **as it would be saved**: the action rebuilt from the state's stored action + action fields, with the current outcome's fields merged into `outcomes[state.outcome]` → `("action", index, canonical(action))`; if a field cannot be parsed yet (bad YAML), compare the raw typed values `("typed", index, values)`.
- **What a save makes stale** (`STALE_AFTER`, reloaded after a part's own save unless that part has unsaved changes of its own): `fields → entry_yaml`; `action → actions_yaml, entry_yaml`; `scheduling → policy_yaml, entry_yaml`; `details → entry_yaml`; `entry_yaml → fields, action, scheduling, details, actions_yaml, policy_yaml`; `actions_yaml → action, entry_yaml`; `policy_yaml → scheduling, entry_yaml`. Parts not reloaded because unsaved are appended to the save message: `\n\n⚠ Not reloaded, to keep their unsaved changes (they may no longer match what is stored): {labels}.`
- **Per-part save** (`save_part_ui`, used by every save button): save that part (a failure: error toast, nothing changes); then reload that part + its stale parts (as above) and make them the baseline. If the part is `fields` and the name changed (rename) or the entry was new (created): `loaded.entity` = new name, Entry dropdown choices re-fetched with value = new name, every part **without** unsaved changes is reloaded under the new name, unsaved ones are kept (and named in the message); if the `action` part is unsaved its state's `name` is set to the new name.
- **Save-all order** (prompt Save, `_save_draft`): `action, scheduling, details, actions_yaml, policy_yaml, entry_yaml, fields` (fields last so a rename comes last); for a new entry (`entity` null) `fields` first (it creates the entry).
- (unclear: after a prompt Save fails part-way, app.py's baseline capture receives no form values, so the parts saved before the failure probably stay marked unsaved; docs/STUDIO.md and tests say they stop being unsaved. Reimplement the documented behaviour: a successfully saved part's current values become its baseline.)
- **Guarded choices** (`_choose`): changing kind/entry, New entry → guard over all parts; picking another action / New action → guard over the `action` part only. With unsaved parts the choice is undone in the UI (radio/dropdown/action picker reset to what is loaded) and the prompt opens with `### ● Unsaved changes\n{label} has unsaved changes in **{parts}**. Save them before {going}? **Discard** drops them; **Stay** keeps editing.` (`label`/`going` as in the World leave guard). The pending target is kept until Save/Discard/Stay.
- **Kept draft** (browser storage key `ane-entry-draft`, value `{kind, entity, parts: {part: [values…]}}`; `{}` = none): on each edit, if the stored `kind/entity` differ from the loaded entry it is replaced by a fresh one for the loaded entry; the part's values are stored when it is unsaved and removed when not; empty `parts` → `{}`. After a load/save the captured parts are removed; if nothing is unsaved any more it becomes `{}`. Tables are stored as `{"__table__": {headers: [...], data: [[...]]}}`. The initial load on app open does not overwrite it (so it can be offered). On app open, if it holds any known part, show the offer: `### ↺ Unsaved changes from your last visit\n**{entity or 'A new entry'}** ({kind}) had unsaved changes in **{labels}** when the page was closed. **Restore** opens it with them (still unsaved); **Discard** drops them.` Only one entry's draft is kept.
- Errors are shown as toasts with the API's `detail` text (422 validation errors as `field: msg; …`).

**Navigation.** None besides the World leave guard.

### 🌍 World › 📝 Entries › Fields
<a id="entries-fields"></a>`view: entries-fields`

**Purpose.** The entry's basic fields (name, tags, texts, per-kind options), its behaviour-by-tag shortcuts, Save, Delete (asked first) and Undo.

**On open.** Part of the entry load: `GET /v1/entities/{kind}/{name}` → `Entity` `{name, tags[], descriptions[], goals[], unique, encounter_event, encounter_repeat, enabled_if, title, stat_bonus, start_eligible, start_weight, role, secret}`; `GET /v1/entities/Events?fields=names` → encounter-event choices; `GET /v1/behaviours` → `{npcs|locations}.{behaviour}.{default, tags: [{tag, value}]}` (behaviour dropdowns, recomputed whenever the kind or the tags change, including on load). New entry: blank values (`name ""`, `tags []`, texts `""`, checkboxes false, encounter event "None", start weight `1.0`, role "inferred from its tags", other texts `""`).

**Components.**

| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| "Name" | text | `name` | — | Required (client check "Name is required.") |
| "Tags" | multiselect (custom values allowed) | choices `reference.common_tags`; value `tags` | `[]` | Info: "Type a new tag and press Enter to add it." |
| "Behaviour" ×2 (labels per kind) | dropdown | `GET /v1/behaviours` (below) | tag in effect | Visible for NPCs (labels "Comes up to the player", "How often they are seen") and Locations ("Draws the player in", "How many visits a story may have there") only. Not part of the draft (derived from tags) |
| "Descriptions" | textarea (5 lines) | `descriptions` joined by `\n` | — | One per line |
| "Goals" | textarea (3 lines) | `goals` joined by `\n` | — | One per line |
| "Unique NPC" | checkbox | `unique` | false | NPCs only |
| "Encounter event" | dropdown | `[("None",""), *Events names]`; value `encounter_event or ""` | "None" | NPCs only |
| "Repeat on every visit" | checkbox | `encounter_repeat` | false | NPCs only |
| "Enabled if (condition)" | text | `enabled_if` as one-line (flow) YAML; `""` when null/empty | `""` | All kinds. Placeholder `e.g. {thief_caught: false, reputation: {gte: 1}} or {any: [{has_item: Ancient Map}, {stat: persuasion, op: gte, value: 60}]}` |
| "Event title" | text | `title` | `""` | Events only |
| "Stat bonus while held" | text | `stat_bonus` as flow YAML | `""` | Items only; placeholder `e.g. {attack: 15}` |
| "Stories can start here" | checkbox | `start_eligible` | false | Locations only |
| "Start weight" | number (min 0.01) | `start_weight` (null → 1.0) | 1.0 | Locations only |
| "Role in the story" | dropdown | `["inferred from its tags", *reference.npc_roles]`; value `role` or the first | "inferred from its tags" | NPCs only |
| "Secret" | text | `secret` as flow YAML | `""` | NPCs only; placeholder `e.g. {text: '{npc} fences the stolen goods.', talks: 2, effects: [{reveal_location: Sewers}]}` |
| "Save entry" | button | — | — | — |
| "Delete entry" | button (danger) | — | — | — |
| Status | markdown | save/delete/undo messages | — | — |
| Delete question | markdown + "🗑 Delete", "Cancel" | — | hidden | — |
| Undo bar | markdown + "↶ Undo" | Undo history `ane-undo-entry` | hidden unless history | — |

**Actions.**

| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| "Save entry" (`save_entity_ui` via `save_part_ui("fields")`) | `PUT /v1/entities/{kind}/{path name}` where path name = loaded `entity`, or the typed name for a new entry; then `GET /v1/entities/{kind}?fields=names` | `Entity` JSON (below) | Response `Entity` (`name`); message `Saved **{name}**.`; reload Fields + `entry_yaml` (stale) or, on create/rename, every clean part under the new name (see 📝 Entries) | Client: empty name → "Name is required."; unparsable YAML → "`Enabled if` is not valid YAML: …" (same for `Stat bonus`, `Secret`). API 400 (e.g. "An entry named `X` already exists. Choose a unique name." on rename), 404, 422. New entry: the typed name is checked against `GET /v1/entities/{kind}?fields=names` first; a taken name → "An entry named `X` already exists. Choose a unique name, or pick it from the list to edit it." (the PUT itself would update that entry) |
| Pick a "Behaviour" (`apply_behaviour_ui`) | none (choices from `GET /v1/behaviours`) | chosen tag | Tags := tags minus every tag offered by that behaviour (case-insensitive) + the chosen tag (if not "none"); then Fields part re-checked | Kept only after Save entry |
| "Delete entry" (`ask_delete_entry_ui`) | `GET /v1/entities/{kind}?fields=names`, `GET /v1/entities/{kind}/{name}/actions`, `GET /v1/entities/{kind}/{name}/references` | loaded kind/entity | Question shown (text below); pending `{type:"entity", kind, name}` | New entry → "Choose an entry to delete (a new entry is not saved: there is nothing to delete)."; name not in names → "`{name}` is no longer in {kind}: choose it again from the list." |
| Question "🗑 Delete" (`confirm_delete_entry_ui`) | `DELETE /v1/entities/{kind}/{name}` (pending's kind/name, even if the form moved on) | — | Response `{message, entities: [names], undo: {token, type, name, kind, entry, position, of}}`. If the deleted entry is the loaded one: load `entities[0]` (or a blank new entry) fully; else if same kind: Entry dropdown choices := `entities` (value kept). Status `Deleted the {word} **{name}** ({kind}).` (`word`: place / person / item / event). Question closed, pending cleared; push to Undo history `{undo, said: "🗑 Deleted the {word} **{name}** ({kind}, {ordinal(undo.position)} of {undo.of})."}` | API refusal (400 "At least one location must remain in the configuration.", 404): question stays with `⚠ **Not deleted:** {error}` and its Delete button hidden; nothing else changes |
| Question "Cancel" (`cancel_delete_ui`) | none | — | Question closed, pending cleared, Delete button visible again | — |
| "↶ Undo" (`undo_delete_entry_ui`) | `POST /v1/deletions/{token}/restore` (token of the newest history item) | — | Response `{type, name, kind, entry, position, was, moved, changed_since, message}`; pop it from history. If nothing is unsaved: load the restored `kind`/`name` fully; status `message`. Else keep the draft, refresh the Entry dropdown choices (value kept) if same kind; status `message + " Your unsaved changes were kept: open it from the list."` | Empty history → "Nothing to undo." Failure: bar shows `⚠ **Not restored:** {error}` above the history; on 404 the item is dropped from history, on 409/400 it stays (retry later) |

`Entity` PUT body (`save_entity_ui`; "per kind" fields are `null` when not applicable):
- `name` (trimmed), `tags`, `descriptions` / `goals` = non-empty trimmed lines, `enabled_if` = YAML-parsed text or `{}` (removes).
- NPCs: `unique` bool, `encounter_event` (`""` = none), `encounter_repeat` bool, `role` (`""` when "inferred from its tags"), `secret` = YAML-parsed or `{}`. Others: `unique/encounter_event/encounter_repeat: null`, no `role`/`secret` keys.
- Events: `title` (string). Items: `stat_bonus` = YAML-parsed or `{}`. Locations: `start_eligible` bool, `start_weight` number. Otherwise `null`.

**Client-side logic to reimplement.**
- Field visibility by kind as in the table; values shown as one-line flow YAML (`enabled_if`, `stat_bonus`, `secret`), parsed with a full YAML parser (braces required for mappings) on save.
- Behaviour dropdown (`behaviour_fields_ui`): from `GET /v1/behaviours` → `{group}.{key}` where (NPCs) `npcs.starts_conversation`, `npcs.appearances`; (Locations) `locations.draws_in`, `locations.visits`. Choices: `("— none: {words(default)} (the default)", "")` then for each `{tag, value}`: `("{tag} · {words(value)}", tag)`. `words`: integer → `"{n} visits"`; `never→never, always→always, once→once, first_meeting→"the first time they meet", every_visit→"on every visit", first_visit→"on the first visit"`, else the value. Value = the first option (in rules order) whose tag the entry has (case-insensitive), else `""`. Hidden for Items/Events.
- Delete question text: `### 🗑 Delete the {word} **{name}**?\n{kind}, {ordinal(index+1)} of {len(names)}: every field of the entry, its {n} action(s) and its details.` + (if unsaved) ` Its unsaved changes ({labels}) go with it.` + ` **Undo** brings it back, where it was.` If `references` non-empty: `\n\n⚠ **{n} thing(s) refer to it** and would no longer work (fix them, or undo the deletion):` + up to 8 bullets (HTML-escaped) + `- … and {n-8} more`. Ordinal: 1st, 2nd, 3rd, 4th…, 11th–20th "th".
- Undo history (shared logic with actions/missions): list `[{undo, said}]`, newest last, stored in browser storage `ane-undo-entry` after every delete/undo and restored on app load (items without `undo` dropped). Bar text: optional note (last undo's failure) then the newest `said` + (if more) `  \n*{n} earlier deletion(s) can be undone after it.*`; bar hidden when there is nothing to show.

**Navigation.** None.

### 🌍 World › 📝 Entries › Actions & outcomes
<a id="entries-actions"></a>`view: entries-actions`

**Purpose.** Edit one action of the entry at a time (its fields, its outcomes one by one, how often it is offered and whether a failure uses it up); add, delete (asked first, Undo) actions; edit all actions as YAML.

**On open.** Part of the entry load: `GET /v1/entities/{kind}/{name}/actions` → `actions[]` → picker choices `"{i+1}. {action.name or 'Action'}"` (value = index from 0), no action picked, blank form (state `{kind, name, index: null, action: {name: "", outcomes: []}, outcome: null}`); `GET .../actions/yaml` → YAML editor. Choice lists (stats, events, items, places, people) as in 📝 Entries › On open. Intro markdown: "Pick one of the entry's actions to edit it, or add one. An action's **outcomes** are what can happen when it is taken; pick one to edit it. **Save action** checks the whole entry (fields, events, places, people, items, variables) before writing."

**Components.**

| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| "Action" | dropdown | `actions[]` labels / indexes | none | Guarded (action part only) |
| "➕ New action" | button | — | — | Guarded (action part only) |
| "🗑 Delete action" | button (danger) | — | — | Delete question + Undo bar (`ane-undo-action`) |
| "Name" | text | `name` | `""` | Required on save |
| "Offered" | radio | `[reference.repeat_default, *reference.repeat_rules]` = `default, once, per_visit, always` | `default` | Info "once per game · again on each visit · every turn" |
| "Retry after failure" | checkbox | `retry_on_failure` | false | Info "A failure uses nothing up." |
| "Skill check" | dropdown | `["(none)", *stat names]`; value `skill_check.stat` | "(none)" | |
| "Check modifier" | number (integer) | `skill_check.modifier` | 0 | "+ easier, - harder" |
| "Exclusive group" | text | `exclusive_group` | `""` | "Actions sharing it are one choice." |
| "Starts the event" | dropdown | `["(none)", *Events names]`; `triggers_event` | "(none)" | |
| "Costs coins" | number (integer, min 0) | `required_coins` | 0 | |
| "Needs the item" | dropdown | `["(none)", *Items names]`; `required_object` | "(none)" | |
| "Needs an item tagged" | text | `required_tag` | `""` | |
| "Uses the item up" | checkbox | `consumes_object` | false | |
| "Takes the item" | dropdown | `["(none)", *Items names]`; `collects_object` | "(none)" | |
| "Offered only if" | text | `enabled_if` as flow YAML | `""` | Placeholder `e.g. reputation: {gte: 1}` |
| "No longer offered once" | text | `until` as flow YAML | `""` | Placeholder `e.g. action_done: Crack the main safe` |
| "Outcome" | dropdown | `outcomes[]` → `"{i+1}. {desc[:60] or '(nothing told)'}"`, value index | first outcome, or none | Not guarded; switching is not an edit |
| "➕ New outcome" / "🗑 Delete outcome" | button | — | — | Client-side only |
| "What is told" | textarea (2 lines) | `outcome.desc` | `""` | |
| "When (with a skill check)" | radio | `any, pass, fail` | `any` | `on_check` |
| "Tags" | multiselect (custom) | `reference.common_tags`; `outcome.tags` | `[]` | |
| "Health ±" / "Coins ±" | number (integer) | `health_change` / `coin_change` | 0 | |
| "Moves to" / "Sends to (one way)" / "Reveals a place" | dropdown | `["(none)", *Locations names]`; `move_to` / `send_to` / `reveal_location` | "(none)" | |
| "Reveals a person" | dropdown | `["(none)", *NPCs names]`; `reveal_npc` | "(none)" | |
| "Reveals an item" | dropdown | `["(none)", *Items names]`; `reveal_object` | "(none)" | |
| "Removes a person" | dropdown | `["(none)", "(the person acting)", *NPCs names]`; `remove_npc` (`true` → "(the person acting)") | "(none)" | |
| "Opens a new path" / "Someone shows the way" | checkbox | `spawn` / `lead_to_known` | false | |
| "Sets variables" / "Changes variables" / "Changes stats" | text | `set_variables` / `change_variables` / `change_stats` as flow YAML | `""` | Placeholders `thief_caught: true`, `reputation: 1`, `persuasion: 5` |
| "Other effects (YAML list, optional)" | yaml-editor | `effects` as YAML | `""` | |
| "Save action" / "Discard changes" | button | — | — | |
| Status | markdown | messages | — | Also used by the YAML accordion and action delete/undo |
| Accordion "All of this entry's actions as YAML (advanced)" | markdown + yaml-editor "actions" + "Validate & save actions", "Reload from disk" | `reference.action_reference`; `GET .../actions/yaml` → `yaml` | collapsed; new entry `"[]\n"` | Draft part `actions_yaml` |

**Actions.**

| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Pick an action (`action_choice_ui` → `load_action_form_ui`) | `GET /v1/entities/{kind}/{name}/actions/{index}` → `{index, action}` | index | Form filled from `action` (state `{kind, name, index, action, outcome: 0 or null}`), outcome picker filled, first outcome shown; action part baseline set | Same index: nothing. Unsaved action part: picker reset to state's index, prompt (pending `{type:"action", index}`); Save there saves only the action, then reloads `actions_yaml`/`entry_yaml` (unless unsaved) and opens the picked action |
| "➕ New action" (`new_action_choice_ui` → `new_action_ui`) | `GET /v1/entities/{kind}/{name}/actions` (count) | — | Form for index = count with action `{name: "New action", outcomes: [{desc: "", tags: ["neutral"]}]}`; this blank becomes the baseline (an untouched new action is not "unsaved") | New entry → "Select an entry first."; guarded (pending `{type:"action", new: true}`) |
| Pick an outcome (`pick_outcome_ui`) | none | — | Current outcome fields written into `state.action.outcomes[state.outcome]`; `state.outcome` := picked; its fields shown | — |
| "➕ New outcome" (`new_outcome_ui`) | none | — | Current outcome kept into state; append `{desc: "", tags: ["neutral"]}`, select it | — |
| "🗑 Delete outcome" (`delete_outcome_ui`) | none | — | Remove `outcomes[state.outcome]`; select outcome 0 (or none, blank fields) | Not asked; undone with "Discard changes" |
| "Save action" (`save_action_form_ui` via `save_part_ui("action")`) | `PUT /v1/entities/{kind}/{name}/actions/{index}` (index = count appends); then `GET .../actions`, `GET .../actions/{index}`, and stale reloads `GET .../actions/yaml`, `GET .../yaml` | `{action}` (below) | Response `{actions}`; message `Saved the action **{action.name}** of {name}.`; picker choices re-fetched with value = index; form reloaded from the store | Client: no state name or index → "Pick an action, or add one, first."; empty name → "The action needs a name."; unparsable flow YAML → "{Label}: could not read `{text}` ({problem})."; bad effects YAML → "Other effects: could not read the YAML (…)." API 400 (the whole entry is validated), 404 |
| "Discard changes" (`reload_part_ui("action")`) | `GET .../actions`; if the state's index < count: `GET .../actions/{index}` | — | Action part reloaded as stored (same action if it still exists, else no action picked, blank form) | — |
| "🗑 Delete action" (`ask_delete_action_ui`) | `GET /v1/entities/{kind}/{entry}/actions` | state `kind`, `name`, `index` | Question `### 🗑 Delete the action **{index+1}. {stored name}** of **{entry}** ({kind})?\nIt goes with its {n} outcome(s)` + (action part unsaved) `, and the changes not saved in the form` + `. **Undo** brings it back, where it was, in this session.`; pending `{type:"action", kind, entry, index, name}` | No saved action picked (`index` null or ≥ count, or no entry) → "Pick a saved action first (a new action is not saved: *Discard changes* drops it)." |
| Question "🗑 Delete" (`confirm_delete_action_ui`) | `DELETE /v1/entities/{kind}/{entry}/actions/{index}?expected_name={name}` | pending | Response `{actions, message, undo}`; status `message`; if that entry is loaded: action part reloaded (picker with fresh choices, no action picked, blank form) + `actions_yaml`, `entry_yaml` unless unsaved; push history `"🗑 Deleted the action **{index+1}. {name}** of **{entry}** ({kind})."` | 409 (the action at that index is now named differently: "…nothing was deleted. Reload them and try again."), 400, 404: question stays with `⚠ **Not deleted:** {error}`, Delete button hidden |
| Question "Cancel" | none | — | closed | — |
| "↶ Undo" (`undo_delete_action_ui`) | `POST /v1/deletions/{token}/restore` | — | Pop from history; status `message`. If `kind`/`entry` is the loaded entry: reload `actions_yaml`, `entry_yaml` (unless unsaved); if the action form is unsaved only refresh the picker's choices, else open the restored action (`index = position - 1`) | As for entries (404 drops the item, others keep it; `⚠ **Not restored:** …`) |
| "Validate & save actions" (`save_entity_actions_ui` via `save_part_ui("actions_yaml")`) | `PUT /v1/entities/{kind}/{name}/actions/yaml` | `{yaml}` (editor text) | Response `{yaml, message}`; status `message`; reload `actions_yaml` + `action` (blank list form) + `entry_yaml` unless unsaved | New entry → "Select an entry first."; 400 with the validation errors |
| "Reload from disk" (`reload_part_ui("actions_yaml")`) | `GET /v1/entities/{kind}/{name}/actions/yaml` | — | Editor := `yaml`; baseline | — |

`{action}` body (`_action_from`, starting from the stored action dict in the state so fields the form does not show are kept; a key is **removed** when its value is null, `""`, "(none)", `[]`, `{}`, `false`, or `0` for `required_coins`/`health_change`/`coin_change`):
- `name` (trimmed); `repeat` (removed when `default`); `retry_on_failure`; `skill_check = {stat, modifier?}` (modifier only when ≠ 0; removed when stat "(none)"); `exclusive_group`; `triggers_event`; `required_coins` (int); `required_object`; `required_tag`; `consumes_object`; `collects_object`; `enabled_if`, `until` (flow YAML).
- `outcomes`: the state's outcomes with the current one rebuilt (`_outcome_from`): `desc` (always kept, trimmed), `on_check` (removed for `any`), `tags`, `health_change`, `coin_change` (int), `move_to`, `send_to`, `reveal_location`, `reveal_npc`, `reveal_object`, `remove_npc` (`true` for "(the person acting)"), `spawn`, `lead_to_known`, `set_variables`, `change_variables`, `change_stats` (flow YAML), `effects` (YAML). No outcomes → `outcomes` key removed (an action that only starts an event).

**Client-side logic to reimplement.**
- Flow-YAML fields (`_parse_flow`): empty → absent; text not starting with `{` or `[` is wrapped in `{…}` before parsing (so `reputation: 1, coins: 2` works). Shown with `_flow`: one-line YAML, empty for null/""/{}/[].
- The hidden action state `{kind, name, index, action, outcome}` holds the whole action being edited including outcomes switched away from; outcome switching/adding/deleting is client-side and only saved with "Save action".
- Repetition: "Offered" and "Retry after failure" are saved inside the action (`repeat`, `retry_on_failure`) with "Save action". The dedicated endpoints `GET/PUT /v1/entities/{kind}/{name}/actions/{index}/repetition` exist in api.py but this view does not use them (their app.py helpers `load_action_repetition`/`save_repetition_ui` are dead code).
- Undo history for actions: browser storage `ane-undo-action`, same rules as entries.

**Navigation.** None.

### 🌍 World › 📝 Entries › Scheduling
<a id="entries-scheduling"></a>`view: entries-scheduling`

**Purpose.** When an event starts (trigger, chance, weight, cooldown, limit, repetition), or an item's uniqueness and place; plus the same as YAML.

**On open.** Part of the entry load (`policy_form_ui`): for `Events`/`Items` with a name, `GET /v1/entities/{kind}/{name}/policy` → `policy {trigger, chance, weight, cooldown, max_occurrences, repeat, unique, location}`; else `{}`. `GET /v1/entities/{kind}/{name}/policy/yaml` → `yaml` (any kind; for places/people the API returns `"# Events and items only.\n"`; new entry: the same text, no request). Event group visible for Events, item group for Items; note for Locations/NPCs: "Places and people: their behaviour comes from their tags (the choices above, and *Behaviour by tags*)."

**Components.**

| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Note | markdown | kind | — | |
| "Starts" | radio | `action, random` | `policy.trigger` or `action` | Events. Info "action: something in the story starts it · random: also by itself" |
| "Chance each turn (random)" | slider(0..1, 0.01) | `policy.chance` | 0 | Events |
| "Weight among random events" | number (min 0) | `policy.weight` | 1.0 | Events |
| "Cooldown (turns)" | number (integer, min 0) | `policy.cooldown` | 0 | Events |
| "At most (times; 0: no limit)" | number (integer, min 0) | `policy.max_occurrences` | 0 | Events |
| "How often" | radio | `once, per_location, per_visit, always` | `policy.repeat` or `always` | Events |
| "Unique: one in each story" | checkbox | `policy.unique` | false | Items |
| "Lies at (none: where it fits best)" | dropdown | `["(none)", *Locations names]`; `policy.location` | "(none)" | Items |
| "Save" | button | — | — | Visible for every kind |
| Status | markdown | messages | — | |
| Accordion "As YAML (advanced)" | yaml-editor "rules" + "Validate & save rules", "Reload from disk" | `GET .../policy/yaml` | collapsed | Draft part `policy_yaml`. Shown, with the Save button, for Events and Items only (`policy_controls_ui` on the content type) |

**Actions.**

| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| "Save" (`save_policy_form_ui` via `save_part_ui("scheduling")`) | `PUT /v1/entities/{kind}/{name}/policy` | Events: `{policy: {trigger, repeat, chance?, weight?, cooldown?, max_occurrences?}}` — `chance` (float) and `weight` (float, empty → 1.0) only when trigger is `random`; `cooldown`/`max_occurrences` (int) only when > 0. Items: `{policy: {unique, location?}}` — `location` only when unique and not "(none)" | Response `{policy}`; message `Saved when **{name}** happens.` (Events) / `Saved **{name}**'s uniqueness.` (Items); reload Scheduling + `policy_yaml` + `entry_yaml` unless unsaved | Client: not Events/Items or no name → "Pick an event or an item."; API 400 |
| "Validate & save rules" (`save_entity_policy_ui` via `save_part_ui("policy_yaml")`) | `PUT /v1/entities/{kind}/{name}/policy/yaml` | `{yaml}` | Response `{yaml, message}`; status `message`; reload `policy_yaml` + Scheduling + `entry_yaml` unless unsaved | New entry → "Select an entry first."; Locations/NPCs (the route's `kind` accepts only `Events`/`Items`) → refused client-side: "Only events and items have scheduling rules; …" |
| "Reload from disk" (`reload_part_ui("policy_yaml")`) | `GET /v1/entities/{kind}/{name}/policy/yaml` | — | Editor := `yaml`; baseline | — |

**Client-side logic to reimplement.** Form defaults and the conditional body building above; draft parts `scheduling` (8 values in the order trigger, chance, weight, cooldown, at most, how often, unique, lies at) and `policy_yaml` (conflict pair: both unsaved → prompt Save refuses).

**Navigation.** None (the note names 🏷 Behaviour by tags, not a link).

### 🌍 World › 📝 Entries › More details
<a id="entries-details"></a>`view: entries-details`

**Purpose.** The fields the entry form does not show: a person's known places/items, article and dialogue; a place's distant/arrival texts, ways in, locked exits and points of interest.

**On open.** Part of the entry load (`details_form_ui`): for NPCs/Locations with a name, `GET /v1/entities/{kind}/{name}/details` → `details` (`fields` unused); NPC fields `known_locations[]`, `known_objects[]`, `article` (`null` automatic | `""` proper name | `the|a|an`), `dialogue[{text: str|[str], enabled_if}]`; Location fields `distant_descriptions[]`, `entered_descriptions[]`, `interact_prompts[]`, `exit_locked`, `interaction_points[{id, label, prompt, linked_location, linked_npc, linked_item, linked_action, linked_outcome, notes}]`. Choices: `GET /v1/entities/Locations?fields=names`, `GET /v1/entities/Items?fields=names` (on every details load). NPC group visible for NPCs, place group for Locations; for Items/Events the note "Items and events have no more details than the entry's fields show."

**Components.**

| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Note | markdown | kind | — | |
| "Knows these places" | multiselect | Locations names; `known_locations` | `[]` | NPCs |
| "Knows these items" | multiselect | Items names; `known_objects` | `[]` | NPCs |
| "The story calls them" | dropdown | hard-coded `[("automatic",""), ("the","the"), ("a","a"), ("an","an"), ("none: a proper name","none")]` | automatic | NPCs. Stored `""` shows "none"; stored null shows "automatic" |
| "What they say when talked with (the first line whose condition holds)" | table(Said when (condition, empty: always), Says) | `dialogue[]`: `[flow(enabled_if), text]` (a list text joined with `" / "`) | empty | NPCs; editable, rows addable |
| "Seen from afar" / "On arrival" / "Ways to go there" | textarea (3 lines) | `distant_descriptions` / `entered_descriptions` / `interact_prompts` joined by `\n` | `""` | Locations; one per line |
| "Its exits are locked until an outcome opens them" | checkbox | `exit_locked` | false | Locations |
| "Points of interest: …" | table(Label, Prompt, Leads to place, Person, Item, Action, Outcome, Notes, Id) | `interaction_points[]` → `label, prompt, linked_location, linked_npc, linked_item, linked_action, linked_outcome, notes, id` | empty | Locations; all string cells |
| "Save the details" | button | — | — | |
| Status | markdown | messages | — | |

**Actions.**

| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| "Save the details" (`save_details_ui` via `save_part_ui("details")`) | `PUT /v1/entities/{kind}/{name}/details` | NPCs: `{details: {known_locations, known_objects, article: (value or null), dialogue: [{text, enabled_if?}]}}` — a dialogue row is sent when its "Says" is non-empty; `enabled_if` only when the condition cell is non-empty (flow-YAML parsed, braces optional). Locations: `{details: {distant_descriptions, entered_descriptions, interact_prompts (non-empty trimmed lines), exit_locked, interaction_points}}` — each row → object of its non-empty trimmed cells (cells `"nan"` ignored), empty rows dropped | Response `{details, message}` (message ignored); status `Saved the details of **{name}**.`; reload More details + `entry_yaml` unless unsaved | Client: not NPCs/Locations or no name → "Pick a person or a place."; bad condition → "Said when: could not read …". API 400 "The details are invalid:\n- …" (unknown place/item, bad article, line without text, point without label or prompt, unknown linked place/person/item) |

**Client-side logic to reimplement.** Table ↔ list conversions above; article mapping (`"none"` is sent as `"none"`, the API stores it as `""`). Dialogue lines whose stored `text` is a list of variants are shown joined with `" / "`. On save (which first re-reads `GET .../details`), a row equal to a stored variants line is sent as that list; a row containing `" / "` whose stored line at the same position was a list (and the row count is unchanged) is split at `" / "` into a list; anything else is one string.

**Navigation.** None.

### 🌍 World › 📝 Entries › ✨ Suggest
<a id="entries-suggest"></a>`view: entries-suggest`

**Purpose.** Ask the language model for text in the world's voice for one field of the entry; tick suggestions and add them to the form (not saved until the form is saved).

**On open.** No request. Intro: "Ask the language model (Gemma, as the judge) for text in the world's voice for this entry. Tick what you want and add it to the form; nothing is saved until you save the entry (*Fields*) or its details (*More details*)."

**Components.**

| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| "Suggest" | dropdown | hard-coded (label → field): Descriptions → `descriptions`, Goals → `goals`, Tags → `tags`, "Seen from afar (place)" → `distant_descriptions`, "On arrival (place)" → `entered_descriptions`, "Ways to go there (place)" → `interact_prompts`, "What they say (person)" → `dialogue` | `descriptions` | |
| "✨ Suggest" | button | — | — | One language-model request at a time server-wide (shared with ✨ Grow); disable while running |
| "Suggestions" | multiselect (checkbox group) | response `suggestions` | all ticked | |
| "Add the ticked ones to the form" | button | — | — | |
| Status | markdown | messages | — | |

**Actions.**

| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| "✨ Suggest" (`suggest_ui`) | `POST /v1/suggestions` | `{kind, name: entity, field}` | `suggestions: [str]` → checkbox choices, all ticked; status `{n} suggestion(s) for **{label}**: untick those you do not want and press *Add to the form*.` | No entry → "Pick an entry first."; 503 without the language model; 404 unknown entry |
| "Add the ticked ones to the form" (`add_suggestions_ui`) | none | field, ticked values | Line fields (`descriptions`, `goals`, `distant_descriptions`, `entered_descriptions`, `interact_prompts`): append ticked values not already a line. `tags`: append ticked values not already tags. `dialogue`: append a row `["", value]` per ticked value. Status `Added {n} to the form: check it and press {where} to keep them.` (`where` = "*Save the details*" for the place/dialogue fields, else "*Save entry*"). Then re-check the `fields` and `details` draft parts | — |

**Client-side logic to reimplement.** The merge rules above (they edit the Fields and More details forms, making those parts unsaved).

**Navigation.** None.

### 🌍 World › 📝 Entries › Every field (YAML)
<a id="entries-yaml"></a>`view: entries-yaml`

**Purpose.** The whole entry as stored YAML (everything the forms show and more), validated against the whole world on save.

**On open.** Part of the entry load: `GET /v1/entities/{kind}/{name}/yaml` → `yaml` (new entry: `"{}\n"`, no request). Intro: "The whole entry as stored: what the forms show and everything else. The whole world is checked with the entry in place before it is saved; rename an entry with the form above."

**Components.**

| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| "entry" | yaml-editor | `yaml` | — | Draft part `entry_yaml`; conflicts with every other part |
| "Validate & save the entry" | button | — | — | |
| "Reload from disk" | button | — | — | |
| Status | markdown | messages | — | |

**Actions.**

| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| "Validate & save the entry" (`save_entity_yaml_ui` via `save_part_ui("entry_yaml")`) | `PUT /v1/entities/{kind}/{name}/yaml` | `{yaml}` | Response `{yaml, message}`; status `message`; reload `entry_yaml` and every other part (fields, action, scheduling, details, actions_yaml, policy_yaml) that has no unsaved changes | New entry → "Choose an entry first."; API 400 (invalid; nothing written). The entry keeps its name |
| "Reload from disk" (`reload_part_ui("entry_yaml")`) | `GET /v1/entities/{kind}/{name}/yaml` | — | Editor := `yaml`; baseline | — |

**Client-side logic to reimplement.** Comparison as trimmed text (trailing spaces per line ignored).

**Navigation.** None.

### 🌍 World › 🎯 Missions
<a id="missions"></a>`view: missions`

**Purpose.** Create, edit, rename and delete (with undo) the world's missions (quests made of ordered steps), through a form or the whole `missions.yaml` document.

**On open.** Everything is fetched at app load. Selecting the tab makes no request; it only runs the World leave-guard (see "Form-editor draft model" below).
- `GET /v1/missions` gives the keys of `missions` (an object keyed by mission key, in file order). They fill the **Mission** dropdown, with nothing selected.
- The form starts empty with no request: Key `""`, Title `""`, Description `""`, Weight `1.0`, Offered only if `""`, Rewards `""`, Steps empty. Form state is `{key: null, staged: false, mission: {}}`.
- `GET /v1/documents/missions` gives `yaml` (the raw file text, comments kept), shown in the **missions.yaml** editor.
- Baselines are set for both parts (`mission`, `missions_yaml`). The indicator shows "✓ No unsaved changes".
- The undo history comes from localStorage `ane-undo-mission`, a list of `{undo, said}`. When it is not empty, the Undo bar is shown (see Client-side logic).
- No cross-view refresh: the Mission dropdown and the YAML change only through this view's own actions. Adding entries elsewhere does not reload them.

**Components.**
| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Help text (goal / rewards syntax) | markdown | hard-coded | — | Lists goal keys `location`, `item`, `action`, `npc_removed`, `event`, `outcome_tag`, `coins`, `condition` |
| Draft indicator | html | client | "✓ No unsaved changes" | See draft model |
| Unsaved-changes prompt (text, **💾 Save**, **Discard**, **Stay**) | markdown + 3 buttons | client | hidden | See draft model |
| Mission | dropdown | keys of `GET /v1/missions` → `missions` | none | Picking is guarded by unsaved changes in the Mission form |
| ➕ New mission | button | — | — | Guarded the same way |
| 🗑 Delete mission | button (danger) | — | — | Asks first |
| Delete question (text, **🗑 Delete**, **Cancel**) | markdown + 2 buttons | client | hidden | — |
| Undo bar (text, **↶ Undo**) | markdown + button | client history | hidden | — |
| Key | text | `key` | `""` | Info text "letters, digits and _". Checked only by the API (`[A-Za-z0-9_]+`) |
| Title | text | `mission.title` | `""` | — |
| Weight | number | `mission.weight` | `1.0` | min 0 |
| Description | textarea (2 lines) | `mission.description` | `""` | — |
| Offered only if | text | `mission.enabled_if`, as one-line YAML flow (e.g. `{thief_caught: false}`) | `""` | Placeholder `e.g. thief_caught: false` |
| Rewards when complete | text | `mission.rewards[]`, written as `k: v; k: v` | `""` | Placeholder `change_coins: 10; change_variable: {reputation: 1}` |
| Steps (one row each; one row: a mission with one goal) | table(Step, What the player is told, Goal, Rewards) | `mission.stages[]` → `title`, `description`, `goal` (as `k: v; …`), `rewards` (as `k: v; …`) | empty | 4 fixed string columns, rows can be added or removed, wrap |
| Save mission | button (primary) | — | — | — |
| Status | status-line | messages | — | — |
| Every mission as YAML (advanced) › missions.yaml | yaml-editor (accordion, closed) | `GET /v1/documents/missions` → `yaml` | — | Must be `world_definition: {missions: …}` (checked by the API) |
| Validate & save missions | button | — | — | — |
| Reload from disk | button | — | — | — |

**Actions.**
| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Pick a Mission `(form_pick_ui → mission_form_ui)` | `GET /v1/missions/{key}` | key = the picked one | Fills the form from `mission` (see mapping below). State becomes `{key, staged: bool(mission.stages), mission}`. The baseline of `mission` is reset | If the Mission form has unsaved changes: no request, the picker goes back to the current key, and the prompt asks "…Save them before opening <key>?". 404 → toast |
| ➕ New mission `(form_pick_ui → new_mission_ui)` | none | — | Picker cleared. Form becomes Key `new_mission`, Title `A new quest`, Weight 1.0, Steps one row `["First step", "", "location: Market", ""]`. State `{key: null, staged: true, mission: {}}`. The baseline is reset to these values | Guarded like a pick, but the picker is not reverted. Prompt says "…before starting a new mission?" |
| Save mission `(form_save_ui → save_mission_form_ui)` | `PUT /v1/missions/{target}`, where target = state.key if set, else the typed Key | `{mission, rename}`. `mission` is built as described below. `rename` = typed Key when state.key is set and differs from it, else `null` | Response `missions` (object) and `message`. Picker choices become `keys(missions)`, value = new key. State key = new key. Then `GET /v1/missions/{key}` reloads the form and resets its baseline. `GET /v1/documents/missions` reloads the YAML unless the YAML has unsaved changes. Status = `message` | Client: no row with a non-empty Goal → "A mission needs a goal: at least one step with a Goal.". A `k: v` part without `:` → "<what>: write `key: value` …". Unparsable YAML in a value → error. API 400 (bad key, key taken on rename, invalid mission) → toast; nothing changes. Client, before any request: empty Key → "Give the mission a key: …"; a Key (new mission or rename) that is already a mission (`GET /v1/missions`) → "A mission `k` exists already: …" (`PUT /v1/missions/{key}` itself adds or replaces) |
| 🗑 Delete mission `(ask_delete_mission_ui)` | `GET /v1/missions` | — | Shows the question "### 🗑 Delete the mission **<title or key>** (`<key>`)?" followed by "<Nth> of <count>: its <n> step(s) / its goal, rewards and conditions. **Undo** brings it back, where it was, in this session." Pending = `{type: "mission", key, title}` | No state.key → "Pick a mission first.". Key not in `missions` → "The mission `<key>` is no longer there: pick it again from the list." |
| Delete question › Cancel `(cancel_delete_ui)` | none | — | Hides the question; pending = null | — |
| Delete question › 🗑 Delete `(confirm_delete_mission_ui)` | `DELETE /v1/missions/{key}` | key = pending.key (not the form's current key) | Response `missions`, `message`, `undo{token,type,name,kind,entry,position,of}`. Picker choices = `keys(missions)`, value cleared. Status = `message`. YAML reloaded with `GET /v1/documents/missions`. The question is hidden. `{undo, said: "🗑 Deleted the mission **<title>** (`<key>`)."}` is pushed onto the history and saved to localStorage. The Undo bar is shown | No pending → "Nothing to delete: press *Delete mission* first.". API error → the question stays open with "⚠ **Not deleted:** <detail>" and its Delete button is hidden. When the deleted mission is the one in the form: without unsaved form changes the form is cleared (state `{key: null}`, baseline reset); with them, they stay and the state's key becomes `null`, so Save adds a new mission (message: "Your unsaved changes to it stay in the form, as a new mission."). The YAML is reloaded only when it has no unsaved changes |
| ↶ Undo `(undo_delete_mission_ui)` | `POST /v1/deletions/{token}/restore` | token = newest history item's `undo.token` | Response `name` (key) and `message`. Pops the history (saved to localStorage). If the Mission form has no unsaved changes: `GET /v1/missions/{name}` loads it, the picker selects it, and the baseline is reset. Otherwise only the picker choices are refreshed (`GET /v1/missions`) and the message gets " Your unsaved mission changes were kept: pick it to edit.". The YAML is reloaded (`GET /v1/documents/missions`) unless it is dirty. Status = message | Empty history → "Nothing to undo.". 404 → item dropped, bar shows "⚠ **Not restored:** <detail>". 409/400 → item kept (try again), same note |
| Validate & save missions `(form_save_ui → save_document)` | `PUT /v1/documents/missions` | `{yaml}` from the editor | Response `yaml` and `message`. Picker choices refreshed (`GET /v1/missions`; the value is kept). The YAML is reloaded (`GET /v1/documents/missions`) and its baseline reset. The Mission form is reloaded (`GET /v1/missions/{state.key}`) unless it is dirty. Status = message | 400 (invalid YAML / missions) → toast; nothing changes. If the YAML removed or renamed the mission open in a clean form, the reload's `GET /v1/missions/{key}` returns 404. The save has already happened, but the step fails with a toast and the view is not updated |
| Reload from disk `(form_reload_ui)` | `GET /v1/documents/missions` | — | The YAML replaces the draft; its baseline is reset | — |
| Prompt › 💾 Save / Discard / Stay `(form_resolve_ui)` | per part as above | — | See draft model | — |

**Client-side logic to reimplement.**
- **Form ⇄ mission mapping** (`mission_form_ui` / `save_mission_form_ui`):
  - **Load.**
    - If `mission.stages` exists, there is one row per stage.
    - Otherwise, if the mission exists, there is one row `[mission.title, "", pairs(mission.goal), ""]`.
    - `pairs(obj)` = `"; ".join("k: v")`. A dict or list value is written as one-line YAML flow.
    - The rewards text is the pairs of each effect dict, joined by `"; "`.
    - `weight` shows `mission.weight`, or 1.0 when it is not set (0 shows as 0).
  - **Save.**
    - Start from a copy of `state.mission`, so fields the form does not show are kept.
    - Set `title` and `description` (trimmed).
    - Set `weight`, but omit it when it equals 1.0 (empty counts as 1.0). `0` is kept: a mission that is never drawn.
    - Set `enabled_if` = the text parsed as YAML, wrapped in `{…}` unless it starts with `{` or `[`. Omit it when empty.
    - Set `rewards` = the text split on `;`. Each `k: v` (v parsed as YAML) becomes `{k: v}`. Omit it when empty.
    - Rows with an empty Goal are dropped.
    - Each kept row becomes `{title: cell or "Step N", description, goal: parse(cell), rewards?}`. `rewards` is included only when non-empty.
    - If there is more than one row or `state.staged` is true: set `mission.stages` and delete `goal`.
    - Otherwise, when the one row holds nothing but its goal (no description, no Rewards, and a title that is empty, `Step 1` or the mission's title): set `mission.goal` = the row's goal and delete `stages`. A single row with anything more is saved as `stages`, so nothing in the table is dropped.
- **Undo history.**
  - The history is a list of `{undo, said}`, newest last, saved to localStorage key `ane-undo-mission` after every delete or undo, and restored at load.
  - The bar text is the newest item's `said`, plus "  \n*N earlier deletion(s) can be undone after it.*" when N > 0, after any note from the last Undo.
  - The bar is hidden when there is no text.
- **Form-editor draft model** (shared by 🎯 Missions, 📜 Story rules and 🗺 Map; `form_part_status_ui`, `form_pick_ui`, `form_resolve_ui`, `section_choice_ui`, `nav_choice_ui`):
  - **State.** `forms_base = {editor: {part: canonical}}`. `drafts_dirty = {editor: [parts]}`; this object is shared with 📝 Entries under the key `entries`. Editors and their parts (label in parentheses):
    - `missions`: `mission` ("Mission form": Key, Title, Description, Weight, Offered only if, Rewards, Steps) and `missions_yaml` ("Missions as YAML").
    - `rules`: `rules` ("Story rules", the 18 rule fields), `messages` ("Story messages"), `variables` ("Persistent variables"), `values` ("Stored values of a profile") and `stats` ("Character stats").
    - `map`: `borders` ("Borders") and `map_yaml` ("Map as YAML").
  - **Canonical value** (`_plain`):
    - Strings are trimmed, with trailing spaces removed on every line.
    - Whole numbers become ints and other numbers are rounded to 9 digits. NaN and null become `""`.
    - Tables become a tuple of rows with every cell trimmed, keeping only rows that have some non-empty cell.
  - **On every user edit** of a part, the part is dirty when its canonical value differs from its baseline; nothing is dirty until a baseline exists. The indicator then shows:
    - `<div class="draft-status dirty" data-dirty="1">● Unsaved changes: <labels, comma-joined></div>` when something is dirty;
    - `<div class="draft-status" data-dirty="0">✓ No unsaved changes</div>` otherwise.
  - **Every load or save of a part** sets its baseline from the values just shown and clears its dirty flag, in the same step.
  - **Scope part.** This is the part the picker replaces: missions → `mission`, rules → `values`, map → `borders`. When the scope part is dirty and the picker changes:
    - the picker reverts (except for New mission);
    - the prompt shows "### ● Unsaved changes\n<editor title> has unsaved changes in **<scope label>**. Save them before <opening X | starting a new mission>? **Discard** drops them; **Stay** keeps editing.";
    - pending = `{type: "pick", value}`.
  - **Leaving a section.** Selecting another World section while the current section's editor has dirty parts keeps the current section selected and shows "… Save them before leaving the editor for 🌍 World → <section title>?". Pending = `{type: "section", id}`.
  - **Leaving 🌍 World.** Going to another destination (banner or menu) while any editor is dirty (checked in the order entries, missions, rules, map) navigates back to World and opens that editor's section. The prompt says "…before leaving the editor for <destination label>?". Pending = `{type: "destination", id}`.
  - **Prompt answers.**
    - **Stay** hides the prompt.
    - **Discard** reloads the dirty parts in scope from the API, then carries on. For a pick, only the scope part is in scope; for a section or destination, every dirty part is.
    - **💾 Save** first checks the conflict pairs: missions (`mission`, `missions_yaml`) and map (`borders`, `map_yaml`). If both parts of a pair are dirty, nothing is saved and the prompt shows "⚠ **Nothing was saved:** <A> and <B> both change the same data. Choose **Stay** and save the one to keep with its own button, or **Discard** both.".
    - Otherwise **💾 Save** saves each dirty part in scope with its own request (as its own Save button does). At the first failure it stops: parts saved so far get new baselines, and the prompt shows "⚠ **<label> could not be saved:** <error>  \nNothing was discarded: correct it and **Save** again, **Stay**, or **Discard**.".
    - On success, the saved parts and their stale parts are reloaded, then the action carries on: the pick (loads the target), the section switch, or the navigation.
  - **Stale parts after a save.** These are reloaded only when they have no unsaved changes of their own:
    - `mission` → `missions_yaml`, and `missions_yaml` → `mission`;
    - `variables` → `values`;
    - `borders` → `map_yaml`, and `map_yaml` → `borders`.
  - **Closing the page.** A `beforeunload` handler asks the browser to confirm when any element `.draft-status[data-dirty="1"]` exists.
  - Drafts of these editors are not kept in browser storage.

**Navigation.** None. Prompts can switch section or destination (draft model above).

### 🌍 World › 📜 Story rules › Story rules
<a id="story-rules"></a>`view: story-rules` (accordion "Story rules", open by default; the section's tab id is `story-rules`)

**Purpose.** Edit the flat story rules: mission pacing, how often options repeat, arrests, discovery, persistence and the event fallback.

**On open.** At build, `GET /v1/reference` gives `rule_fields` (the order of the 18 fields) and `rule_choices` (`repeat`, `arrest_lock_when`, `save_on`, `simulate_with` lists). `GET /v1/entities/Locations?fields=names` gives `names`, the Arrest location choices; they are never refreshed. At load, `GET /v1/rules` fills every field and resets the `rules` baseline. The section's draft indicator and prompt are shared by all five accordions (editor `rules`).

**Components.** Each one is bound to the same-named field of `GET /v1/rules`.
| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Missions enabled | checkbox | `missions_enabled` | — | Heading "**Missions and goal pursuit**" |
| End the story when the goal is met | checkbox | `end_on_goal` | — | — |
| Pursuit starts at step | number | `pursuit_start_step` | — | Integer, min 0 |
| Full pursuit from step | number | `pursuit_full_step` | — | Integer, min 1 (API: must be greater than start) |
| Route weight | number | `route_weight` | — | Float |
| Location / Npc / Item / Event actions | dropdown ×4 | `repeat_location`, `repeat_npc`, `repeat_item`, `repeat_event`; choices `rule_choices.repeat` = `once, per_visit, always` | — | Heading "**How often options are offered** (unless an action sets `repeat`)" |
| Arrest location | dropdown | `arrest_location`; choices = location names | — | Heading "**Arrests and discovery**" |
| Confine until escape | dropdown | `arrest_lock_when`; `sent, always, never` | — | Info "sent: only when sent there; always: everyone; never: no escape needed" |
| Follow compelling new paths | checkbox | `auto_travel_on_spawn` | — | — |
| Commitment threshold | number | `auto_travel_on_spawn_threshold` | — | Float |
| Default player profile | text | `profile` | — | API: `[A-Za-z0-9_.-]{1,64}`. Heading "**Persistent variables and events**" |
| Save variables on | dropdown | `save_on`; `change, end` | — | — |
| Evolution plays with | dropdown | `simulate_with`; `defaults, stored` | — | — |
| Event fallback option | text | `fallback_action` | — | Must be non-empty (API) |
| Event fallback text | text | `fallback_description` | — | — |
| Save story rules | button (primary) | — | — | — |
| Reload | button | — | — | — |
| Status | status-line | — | — | Also receives the prompt's Save message |

**Actions.**
| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Save story rules `(form_save_ui → save_rules_ui)` | `PUT /v1/rules` | All 18 fields `{missions_enabled, end_on_goal, pursuit_start_step, pursuit_full_step, route_weight, repeat_location, repeat_npc, repeat_item, repeat_event, arrest_location, arrest_lock_when, auto_travel_on_spawn, auto_travel_on_spawn_threshold, profile, save_on, simulate_with, fallback_action, fallback_description}` | The response is ignored. Status = "Saved the story rules.". Then `GET /v1/rules` reloads the form and resets its baseline | 400/422 → toast; nothing changes. A cleared number is sent as `null`, which the API ignores (keeps the old value) |
| Reload `(form_reload_ui)` | `GET /v1/rules` | — | Form replaced; baseline reset | — |

**Client-side logic to reimplement.** The draft model (see 🎯 Missions), part `rules`. The section headings and the info text above are hard-coded.

**Navigation.** None.

### 🌍 World › 📜 Story rules › Story messages
<a id="story-rules.messages"></a>`view: story-rules.messages` (accordion, closed)

**Purpose.** Rewrite any sentence the engine adds to a story (`messages.yaml`).

**On open.** At load, `GET /v1/documents/messages` → `yaml` fills the editor and resets the `messages` baseline.

**Components.**
| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Help text | markdown | hard-coded | — | Example `voyage: "You head towards the {location}."`; unknown messages or placeholders are rejected |
| messages.yaml | yaml-editor | `GET /v1/documents/messages` → `yaml` | — | — |
| Validate & save messages | button (primary) | — | — | — |
| Reload from disk | button | — | — | — |
| Status | status-line | — | — | — |

**Actions.**
| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Validate & save messages `(form_save_ui → save_document)` | `PUT /v1/documents/messages` | `{yaml}` | Status = `message`. Then `GET /v1/documents/messages` reloads the editor and resets the baseline | 400 → toast; nothing written |
| Reload from disk `(form_reload_ui)` | `GET /v1/documents/messages` | — | Editor replaced; baseline reset | — |

**Client-side logic to reimplement.** The draft model, part `messages`.

**Navigation.** None.

### 🌍 World › 📜 Story rules › Persistent variables
<a id="story-rules.variables"></a>`view: story-rules.variables` (accordion, closed)

**Purpose.** Define persistent variables (`variables.yaml`), and view, edit or reset the stored values of a player profile (to test later playthroughs).

**On open.**
- At build, `GET /v1/variables/profiles` gives `profiles[]` (the dropdown choices) and `default` (the initial value and the initial profile context).
- At load, `GET /v1/documents/variables` fills the definitions editor.
- At load, `GET /v1/documents/stored_values?profile=<default>` fills Stored values. That document has the shape `playthroughs: <int>\nvalues: {<variable>: <value>}`.
- Both baselines are reset.
- The profile choices are never refreshed, e.g. after a game creates a new profile; a custom value can be typed.

**Components.**
| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Help text | markdown | hard-coded | — | Mentions `set_variables` / `change_variables` / `enabled_if`; only real play saves values |
| variables.yaml | yaml-editor | `GET /v1/documents/variables` → `yaml` | — | Shape `world_definition: {variables: …}` |
| Validate & save variable definitions | button (primary) | — | — | No reload button for this part |
| Status (definitions) | status-line | — | — | — |
| "#### Stored values of a player profile" | markdown | hard-coded | — | — |
| Player profile | dropdown, custom value allowed | `profiles` | `default` | This is the picker of the `rules` editor (scope part `values`) |
| Stored values | yaml-editor | `GET /v1/documents/stored_values?profile=` → `yaml` | — | — |
| Save stored values | button (primary) | — | — | — |
| Refresh | button | — | — | — |
| Reset profile (new player) | button (danger) | — | — | Asks first: a panel with the question, "Reset this profile" (danger) and "Cancel" |
| Status (values) | status-line | — | — | — |

**Actions.**
| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Validate & save variable definitions `(form_save_ui → save_document)` | `PUT /v1/documents/variables` | `{yaml}` | Status = `message`. The editor is reloaded (`GET /v1/documents/variables`) and its baseline reset. Stored values are reloaded (`GET /v1/documents/stored_values?profile=<context>`) unless they have unsaved changes | 400 → toast |
| Pick or type a Player profile `(form_pick_ui)` | `GET /v1/documents/stored_values` | `profile` = the chosen value | Stored values replaced; context = profile; baseline reset | If Stored values are dirty: the picker reverts and the prompt asks "…before opening <profile>?". An invalid profile name → 400 toast |
| Save stored values `(form_save_ui → save_document)` | `PUT /v1/documents/stored_values` | query `profile` = context (empty → omitted → the API's default profile); body `{yaml}` | Status = "Saved the stored values of profile `<p>`.". Then the GET reloads the editor and resets the baseline | 400: unknown keys (only `playthroughs` and `values` are allowed), invalid values, or playthroughs < 0 |
| Refresh `(form_reload_ui)` | `GET /v1/documents/stored_values?profile=<context>` | — | Editor replaced; baseline reset | — |
| Reset profile (new player) `(reset_question_ui)`, then "Reset this profile" `(reset_stored_values_ui, then form_reload_ui)` | none; then `POST /v1/variables/reset/yaml` | query `profile` = the dropdown's value | The question: "### ⚠ Reset the profile **p**? It starts over as a new player: its stored values, playthroughs, goals achieved and session reports are lost, and **this cannot be undone**. Unsaved changes in *Stored values* are lost too." On confirm: response `yaml` and `message` ("Profile `<p>` starts over as a new player.") shown; then `GET /v1/documents/stored_values?profile=<context>` resets the baseline | Irreversible (the API removes the whole profile); Cancel closes the question |

**Client-side logic to reimplement.** The draft model, parts `variables` and `values`. The profile dropdown is the editor's picker, and the current profile is the `rules` editor's context.

**Navigation.** None.

### 🌍 World › 📜 Story rules › Character stats
<a id="story-rules.stats"></a>`view: story-rules.stats` (accordion, closed)

**Purpose.** Edit `stats.yaml`: the stats, the points budget, how players get points, `tag_checks`, and the stat that softens combat damage.

**On open.** At load, `GET /v1/documents/stats` → `yaml` fills the editor and resets the `stats` baseline.

**Components.**
| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Help text | markdown | hard-coded | — | Points to Weights → **Skill check resolution** for difficulty |
| stats.yaml | yaml-editor | `GET /v1/documents/stats` → `yaml` | — | Default document `world_definition: {character_stats: {stats: {}}}` |
| Validate & save character stats | button (primary) | — | — | — |
| Reload from disk | button | — | — | — |
| Status | status-line | — | — | — |

**Actions.**
| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Validate & save character stats `(form_save_ui → save_document)` | `PUT /v1/documents/stats` | `{yaml}` | Status = `message`. The editor is reloaded and its baseline reset | 400 → toast |
| Reload from disk `(form_reload_ui)` | `GET /v1/documents/stats` | — | Editor replaced; baseline reset | — |

**Client-side logic to reimplement.** The draft model, part `stats`. There is no allocation preview in this accordion. The points-allocation text, Randomize/Balance (`POST /v1/stats/allocate`) and the "Stat points" editor are in ▶ Play › "🎲 My own stat points". That text is built once at app start from `GET /v1/stats` and is not refreshed after a save here.

**Navigation.** None.

### 🌍 World › 🗺 Map
<a id="map"></a>`view: map`

**Purpose.** Set which places border which (routes hold both ways), see the whole map drawn as SVG, or edit the map document as YAML.

**On open.**
- At build, the Place choices come from `GET /v1/entities/Locations?fields=names`; the value is the first name. They are never refreshed.
- At load, `GET /v1/map` returns `routes` (an object `place → [neighbours]`, or null) and `places` (every location name, sorted).
  - If the context place is not in `places`, the first place is used.
  - Borders = `sorted(routes[place] ∪ {a | place ∈ routes[a]})`.
  - The Borders checkbox choices are `places` minus `place`, labelled "The places <place> borders".
  - The SVG is drawn and the `borders` baseline is reset.
- At load, `GET /v1/documents/map` → `yaml` fills the map editor and resets its baseline.
- No cross-view refresh: new or renamed places in 📝 Entries do not reach the Place dropdown until the page reloads. They do reach the SVG and the checkbox choices on the next `GET /v1/map`.

**Components.**
| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Draft indicator and prompt | html + markdown + 3 buttons | client | — | Editor `map` |
| Help text | markdown | hard-coded | — | — |
| Place | dropdown | location names | first | The picker (scope part `borders`) |
| Borders | multiselect (checkbox group) | `places` minus the place | ticked = its borders | Label "The places <place> borders" |
| Save the borders | button (primary) | — | — | — |
| Status | status-line | — | — | — |
| Map view | svg (html) | drawn client-side | — | See Client-side logic |
| The map as YAML (advanced) › map | yaml-editor (accordion, closed) | `GET /v1/documents/map` → `yaml` | — | Comment header plus `routes: {place: [neighbours]}` or `routes: null` |
| Validate & save the map | button | — | — | — |
| Reload from disk | button | — | — | — |

**Actions.**
| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Pick a Place `(form_pick_ui → map_place_ui)` | `GET /v1/map` | — | Borders and SVG for that place; context = place; baseline reset | If Borders are dirty: the picker reverts and the prompt asks "…before opening <place>?" |
| Save the borders `(form_save_ui → save_borders_ui)` | `PUT /v1/map/{place}` | `{borders: [ticked names]}`; place = context | Response `routes`, `places` and `message`. The SVG is redrawn and Status = `message`. Then `GET /v1/map` reloads Borders and the SVG and resets the baseline. The YAML is reloaded (`GET /v1/documents/map`) unless dirty | No place → "Pick a place."; a place no longer in `GET /v1/map` `places` → "The place `X` is no longer in the world: pick a place from the list.". 400 (unknown borders) → toast. Whenever Borders reload, the Place list is refreshed and a place that is gone falls back to the first one (picker and context) |
| Validate & save the map `(form_save_ui → save_document)` | `PUT /v1/documents/map` | `{yaml}` | Status = `message`. The YAML is reloaded (`GET /v1/documents/map`). Borders and the SVG are reloaded (`GET /v1/map`) unless Borders are dirty; in that case the SVG is not redrawn either | 400 → toast |
| Reload from disk `(form_reload_ui)` | `GET /v1/documents/map` | — | YAML replaced; baseline reset | — |
| Prompt buttons `(form_resolve_ui)` | — | — | Draft model. Conflict pair: `borders` + `map_yaml` | — |

**Client-side logic to reimplement.**
- **SVG map** (`_map_svg(routes, places, selected)`):
  - With no places, show `<p>No places yet.</p>`.
  - Canvas: `size = 640`, `radius = 230`, `viewBox="-110 0 860 640"`, `style="width:100%;max-width:820px;height:auto"`.
  - Place i of n sits at `(320 + 230·cos(2πi/n − π/2), 320 + 230·sin(2πi/n − π/2))`, in the order of `places`.
  - **Edges.** Edges are the unique unordered pairs `{a, b}` with `b ∈ routes[a]`, both in `places`, drawn as `<line>`.
    - An edge touching `selected` is stroke `#8b5cf6`, width 3.
    - Any other edge is stroke `#94a3b8`, width 1.5, with opacity .35 when something is selected (1 otherwise).
  - **Nodes.** Each place is a circle r=9 with stroke `#475569`.
    - Fill `#8b5cf6` for the selected place, `#c4b5fd` for its neighbours (either direction), and `#e2e8f0` otherwise.
  - **Labels.** Each node has a label at font-size 13 with `fill="currentColor"`, bold when selected, with `&` and `<` escaped.
    - Anchor `start` (dx +12) when x > 325, `end` (dx −12) when x < 315, else `middle`.
    - dy +18 when y > 320 + 207, −12 when y < 320 − 207, else +4.
  - When `routes` is empty or null, a centered text reads "No authored map: every story draws its own.".
- **Borders.** Borders are symmetric on display: the union of the place's own list and every place that lists it.
- The draft model, parts `borders` and `map_yaml`.

**Navigation.** None.

### 🌍 World › 🏷 Behaviour by tags
<a id="behaviour"></a>`view: behaviour`

**Purpose.** Edit the tag-based rules that decide how people come up to the player and how often they are seen, and how places draw the player in and how often they may be visited. Separate tables for people (`npcs`) and places (`locations`).

**On open.**
- At load, `GET /v1/behaviours/rules` returns `{npcs: {default, rules[]}, locations: {default, rules[]}}`. Each rule is `{tags: [..], <key1>?: v, <key2>?: v}`.
  - For npcs the keys are `starts_conversation` and `appearances`; for locations they are `draws_in` and `visits`.
  - Each table row = `[tags.join(", "), str(rule[key1] ?? ""), str(rule[key2] ?? "")]`.
  - The default dropdowns = `str(default.starts_conversation ?? "never")`, `str(default.appearances ?? "always")`, `str(default.draws_in ?? "never")`, `str(default.visits ?? "always")`.
- Each table (with its two defaults) is a draft of its own: an edit compares it with the table as loaded or saved, and an indicator says which have unsaved changes (`behaviour_status_markup`, a `.draft-status` element, so closing or reloading the page asks first). There is no in-app Save/Discard prompt for these tables.

**Components.**
| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Help text | markdown | hard-coded | — | Explains values and "first matching row decides" |
| People › Without a rule: comes up | dropdown | `never, first_meeting, every_visit` (hard-coded) | `never` | — |
| People › Without a rule: seen | dropdown, custom value allowed | `always, once, 2, 3, 5` (hard-coded) | `always` | A number = that many visits |
| People table | table(Tags (comma separated), Comes up to the player, How often seen) | npcs.rules | — | 3 fixed string columns, editable rows |
| Save the people's rules | button (primary) | — | — | — |
| Places › Without a rule: draws in | dropdown | `never, first_visit, every_visit` | `never` | — |
| Places › Without a rule: visits | dropdown, custom value allowed | `always, once, 2, 3, 5` | `always` | — |
| Places table | table(Tags (comma separated), Draws the player in, How many visits) | locations.rules | — | — |
| Save the places' rules | button (primary) | — | — | — |
| Status | status-line | — | — | Shared by both saves |

**Actions.**
| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Save the people's rules `(save_behaviour_table_ui("npcs"))` | `PUT /v1/behaviours/rules/npcs` | `{default: {starts_conversation, appearances}, rules: [...]}` (building rules below) | Status = `message`. Then the 📝 Entries behaviour dropdowns are recomputed (`GET /v1/behaviours`, see below). Then only this table and its defaults are reloaded (`GET /v1/behaviours/rules`), as its new baseline; the other table keeps its edits | 400 (invalid rule) → toast; the follow-up refreshes do not run, the edits stay |
| Save the places' rules `(save_behaviour_table_ui("locations"))` | `PUT /v1/behaviours/rules/locations` | `{default: {draws_in, visits}, rules}` | Same | Same |

**Client-side logic to reimplement.**
- **Building rules.**
  - Each table row becomes `{tags: cell0.split(",").trim().filter(nonEmpty)}`.
  - Add `key1: cell(cell1)` and `key2: cell(cell2)` when the cell is non-empty. `cell(v)` = int when the trimmed text is all digits, else the trimmed string.
  - A row is kept if it has tags or any value.
  - Defaults = `cell(value) || "never"` (first key) and `cell(value) || "always"` (second key).
- **Related per-entry behaviour fields** (they live in 📝 Entries, next to Tags; `behaviour_fields_ui`, `apply_behaviour_ui`):
  - For kind `NPCs` there are two dropdowns: "Comes up to the player" (`npcs.starts_conversation`) and "How often they are seen" (`npcs.appearances`).
  - For `Locations` they are "Draws the player in" (`locations.draws_in`) and "How many visits a story may have there" (`locations.visits`). For other kinds both are hidden.
  - Choices come from `GET /v1/behaviours` → `<group>.<key>.{default, tags: [{tag, value}]}`:
    - first `("— none: <words(default)> (the default)", "")`;
    - then `("<tag> · <words(value)>", tag)` for each tag.
  - `words(v)`: an int becomes "<n> visits". Strings map as never → never, always → always, once → once, first_meeting → "the first time they meet", every_visit → "on every visit", first_visit → "on the first visit".
  - Selected = the first option tag found (case-insensitive) in the entry's tags, else `""`.
  - Choosing one removes from Tags every tag of that behaviour's options and appends the chosen tag (when non-empty). It marks the Entries `fields` part dirty; it is saved with the entry's Save.
  - They are recomputed when Kind or Tags change, at load, and after either table save.

**Navigation.** None.

### 🌍 World › ✨ Grow the world
<a id="grow"></a>`view: grow`

**Purpose.** Compare the world's size with the augmentation targets, have the language model propose new places, people and items, edit the proposal, and add the chosen entries.

**On open.**
- At load, `GET /v1/world/size` returns `{have: {locations, items, npcs, outcomes}, want: {same keys}, enough}`. It becomes the table rows `{what: key, "the world has": have[key], target: want[key], "": "✓" if have ≥ want else "below"}`, one per key of `want`.
- At load, `GET /v1/judge/status` returns `{available, model, server, reason, how}`. It is shown as:
  - "✅ **The judge is ready:** <model> at `<server>`." (or " (in the API's process)." when there is no server);
  - "⚠ **The judge is not available here.** <reason>\n\n<how>" when it is not available.
- Neither is refreshed by other views (only "Add" refreshes the size).

**Components.**
| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Help text | markdown | hard-coded | — | Targets are `augmentation.min_*` (⚙ Advanced → Engine logic) |
| The world's size | table(what, the world has, target, "") | `GET /v1/world/size` | — | Read-only |
| LLM status | markdown | `GET /v1/judge/status` | — | — |
| ✨ Propose new entries | button (primary) | — | — | One language-model call at a time, studio-wide; this queue is shared with Entries' suggestions |
| Add these | multiselect (checkbox group) | the proposal | empty | Labels `Place: <name>`, `Person: <name>`, `Item: <name>` |
| The proposal (edit it before adding) | yaml-editor (12 lines) | the job result | empty | — |
| Add the ticked ones to the world | button (primary) | — | — | — |
| Status | status-line | — | — | — |

**Actions.**
| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| ✨ Propose new entries `(propose_world_ui)` | `POST /v1/jobs/world-proposal` (202 → job `{id, status, …}`), then poll `GET /v1/jobs/{job_id}` every 0.5 s until `status` is `finished` or `failed` | — | Shows a fixed progress text "The language model is writing new places, people and items…" (no progress fields are used). `result` = `{locations: {name: entry}, objects: [entry{name,…}], characters: [entry{name,…}]}`. Choices (all ticked) = `Place: <each locations key>`, `Person: <characters[].name>`, `Item: <objects[].name>`. The editor gets the result as block YAML (keys unsorted). Status = "Proposed <n> new entries. Read and edit them below, untick those you do not want, then press *Add the ticked ones*." | `failed` → toast with `error` (e.g. language model not available; "proposed nothing usable; try again") |
| Add the ticked ones to the world `(add_proposal_ui)` | `POST /v1/world/additions` | `{locations: {name: entry} for ticked "Place: name", characters: [entry for ticked "Person: entry.name"], objects: [entry for ticked "Item: entry.name"]}`, taken from the edited YAML | Status = `message` ("Added A, B, …."). The size table is refreshed (`GET /v1/world/size`). Then the 📝 Entries entry dropdown is refreshed (`GET /v1/entities/{current kind}?fields=names`, keeping the current entry). Then the studio status line is refreshed (`GET /v1/world/catalogue`, `GET /v1/exports/engines`, `GET /v1/records`). The proposal and ticks are not cleared | Client checks: invalid YAML → "The proposal is not valid YAML: …"; not a mapping → "The proposal must be a mapping with locations, characters and objects."; nothing matched → "Tick at least one entry to add.". 400 (already exists / world invalid) → toast; nothing written |

**Client-side logic to reimplement.**
- The ticks follow the YAML (`sync_proposal_ui`, on every change of the proposal text, with the entries last offered kept client-side): an entry still there keeps its tick; an entry new to the text (added, or renamed) is offered ticked; YAML that does not parse leaves the ticks as they are. On *Add the ticked ones*, a tick whose entry is no longer in the YAML is refused: "Ticked but no longer in the proposal (renamed or removed in the YAML?): …".
- The size table formula is given above.

**Navigation.** None. Added entries appear in 📝 Entries through the refreshed dropdown; every World dropdown of names refreshes its choices when it is opened (see [🌍 World](#-world)).

### 🌍 World › 🔍 Checks
<a id="checks"></a>`view: checks`

**Purpose.** List the actions whose failure leads nowhere (a skill check or failure outcomes that change only health or coins, with no fitting `complication` event).

**On open.** Nothing is fetched. The report is empty until the button is pressed.

**Components.**
| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Help text | markdown | hard-coded | — | — |
| Check the world's failures | button | — | — | — |
| Report | markdown | `GET /v1/lint/failures` | empty | — |

**Actions.**
| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Check the world's failures `(failure_lint_ui)` | `GET /v1/lint/failures` | — | `dead_ends: [{owner, action, why}]` is rendered (below) | API error → toast |

**Client-side logic to reimplement.** Rendering:
- **Empty `dead_ends`:** "No dead ends: every action that can fail leads somewhere when it fails (a consequence, or a complication event that fits it). During play the engine still shows another way (`director.another_way`)."
- **Otherwise:** "**<n> failure(s) lead nowhere.** Give them a consequence (a move, a reveal, a setup, an effect) or write an event tagged `complication` that shares a tag with them.", followed by a table with the columns where | action | why, one row per item (`owner`, `action`, `why`).

**Navigation.** None.

## 🧠 Train

Top tab `id="train"` (banner button `#nav-train`, address `?section=train`). Three sub-tabs: **Run** (`train-run`, selected by default), **From a recorded engine** (`train-records`), **🏷 Finished engines** (`train-engines`).

Shared notes for every Train/Play request:
- The studio sends `X-ANE-Client: studio` on **every** request. Runs started without it need `X-ANE-User: <opaque id>` (else 400 on `POST /v1/jobs/simulations`, `/v1/jobs/engines/{engine_id}/continue|retrain`). A rebuilt frontend should send `X-ANE-Client: studio` (runs are then recorded as user `test`, origin `studio`).
- Errors: the API's `detail` (string, or a list of `{loc, msg}` joined as `loc: msg; …`) is shown as an error toast. Nothing else changes on error.
- Global data read once at app load and used here: `GET /v1/app-settings` → `settings.candidate_limit`, `settings.generation_limit`, `settings.simulation_step_limit` (slider maxima); `GET /v1/variables/profiles` → `{profiles: string[], default: string}`; `GET /v1/stats` → `character_stats` (`stats` {name: …}, `allocation` {total_points, min_points, max_points}, `defined_points` {stat: int}).
- One training at a time: the three training buttons (Evolve, Continue, Retrain) share one client-side queue (concurrency 1); the server also runs jobs on one worker.

### 🧠 Train › Run
<a id="train-run"></a>`view: train-run`

**Purpose.** Configure one evolution run (overrides of the saved engine defaults), see what it will execute as the API resolves it, start it as a background job, follow its progress, then show the completed engine (result card, optional name, actions to Play / Evaluate / Export) and its learning results.

**On open.**
- At app load: `POST /v1/simulations/resolve` with `{}` (the "first plan") → fills slider values/infos and the summary (see `resolved_markdown` below). Then again `run_form_ui` on load with the current controls.
- Every time the user goes to Train via the banner button or the narrow-screen menu: `POST /v1/simulations/resolve` with the current overrides (`run_form_ui`) — so the summary follows saved defaults changed in ⚙ Advanced.
- At app load, restore from browser storage (`restore_result_ui`): localStorage key `ane-train-result` holds the last completed engine object `done` (shape below). If present: `GET /v1/exports/engines` → if `done.engine_id` is not among `engines[].engine_id`, card shows `NO_RESULT` + "*The engine trained earlier, `<id>`, is no longer among the finished runs here.*", buttons disabled, `done` cleared in memory (not in storage). Else card = completed view (`GET /v1/engines/{engine_id}/card`) + "\n\n*Trained earlier in this browser.*", buttons enabled.
- Whenever `done` changes (restore, training finished): name box refresh (`result_name_ui`) → `GET /v1/engines/{engine_id}/card` → `name`.
- `GET /v1/learning` at load for the Learning memory accordion (see that view).

`POST /v1/simulations/resolve` response used (ResolvedTraining):
- `defaults.candidate_count|generations|episodes|simulation_steps` (int), `defaults.deterministic` (bool), `defaults.seed` (int|null), `defaults.warm_start` (bool)
- `effective.population|generations|stories_per_storyteller|requested_steps|story_turns|story_length` (int), `effective.full_length_stories|deterministic|warm_start` (bool), `effective.seed` (int|null), `effective.warm_start_mode` ("memory"|"engine"|"off")
- `sources.{population,generations,stories_per_storyteller,requested_steps,deterministic,seed,warm_start}`: "override" | "default" | "engine <id>"
- `work.work|limit|over_by|effective_max_turns|story_length` (int), `work.allowed|steps_decide|full_length_stories` (bool), `work.override` ({setting, reason}|null), `work.factors.{storytellers,generations,stories_per_storyteller,turns_per_story}` (int)
- `notes[]`, `errors[]` (string), `ready` (bool), `remembered` (int|null)

**Components.**

| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Lead text | markdown | static | "Evolve AI storytellers on this world… A run uses the saved engine defaults (⚙ Advanced → Simulation) except what you set here…" | |
| "Set the population for this run" | checkbox | — | off | enables the slider below |
| "Population (storytellers)" | slider(1..`candidate_limit`, 1) | `effective.population` | first plan | disabled unless its checkbox is on; info "Saved default: {defaults.candidate_count} · used unless set here" / "· set for this run" |
| "Set the generations for this run" | checkbox | — | off | |
| "Generations" | slider(1..`generation_limit`, 1) | `effective.generations` | | same pattern, default `defaults.generations` |
| "Set the stories for this run" | checkbox | — | off | |
| "Stories per storyteller" | slider(1..10, 1) | `effective.stories_per_storyteller` | | default `defaults.episodes` |
| "Set the steps for this run" | checkbox | — | off | |
| Steps slider | slider(1..`simulation_step_limit`, 1) | `effective.requested_steps` | | label/info/style from `work` (see client logic); info ends "Saved default: {defaults.simulation_steps}." |
| "Reproducibility" | radio | `[("Saved default (deterministic|random)","default"),("Deterministic","on"),("Random","off")]` — label from `defaults.deterministic` | "default" | |
| "Set the seed for this run" | checkbox | — | off | |
| "Seed" | number (integer) | `defaults.seed` when unchecked | `defaults.seed` | disabled unless checked; info "Saved default: {defaults.seed}" |
| "Warm start" | radio | `[("Saved default (on|off)","default"),("Continue from earlier runs","on"),("Start fresh","off")]` — label from `defaults.warm_start` | "default" | info "Seed the population with the best storytellers the AI remembers." |
| Run summary | markdown | `resolved_markdown(plan)` | first plan | |
| "🧠 Evolve storytellers" | button (primary) | — | | `#train-run-button` |
| Result card ("2 · Progress and the completed engine") | markdown | completed view | `NO_RESULT` text | `#train-result`; while a job runs it carries the progress bar |
| "▶ Play this result" | button | — | disabled | enabled once `done.engine_id` exists |
| "⚖ Evaluate this result" | button | — | disabled | |
| "📦 Export this result" | button | — | disabled | |
| "Name (optional)" | text | card `name` | "" | max 80 chars; disabled until a card exists; placeholder = card `default_label` (else "Name this engine"); info "Shown instead of its date in Train, Play, Evaluate and Export; its id stays the same. Empty: no name." |
| "🏷 Save name" | button | — | | |
| Name status | status-line | rename message | "" | |
| "Learning curve" ("3 · Learning results") | line chart (x generation, y "Fitness", colour series) | `learning.history` | empty | height 260 |
| "What the evolved storyteller learned" | table(trait, meaning, evolved, population, tendency) | `learning.learned_traits` | empty | read-only |
| Simulation report | markdown | `report` | "" | |

**Actions.**

| Trigger | Request | Body / params (from which components) | On success (response fields → where shown; what else is refreshed) | Errors & guards |
|---|---|---|---|---|
| Any run control changed by the user (12 controls) (`run_form_ui`) | `POST /v1/simulations/resolve` | overrides (see client logic) | sliders/seed/radios/summary updated; plan kept as "shown" plan. Also, if a recorded engine is chosen in *From a recorded engine*, its retrain summary is recomputed (`retrain_form_ui`) | on error: summary = "⚠ The run's settings could not be resolved: {msg}", plan = null, controls untouched |
| "🧠 Evolve storytellers" (`run_simulation_ui`) | `POST /v1/jobs/simulations` → 202 job `{id, status, progress, result, error,…}`; then poll `GET /v1/jobs/{job_id}` every 0.5 s until `status` is "finished" or "failed" | overrides + `"start_game": false` (no profile/stats/mission) | while running: progress bar on result card, fraction `progress.generation/progress.total`, text "Generation {generation}/{total}: benchmark {validation:.0f}" (benchmark part only if `validation` numeric). On finish, `job.result` (SimulationResponse, see below) → `report` → report markdown; `config_download_url` and `engine_download_url` → fetched via `GET /v1/artifacts/{artifact_id}` into the files accordion; `learning` → curve + traits; `GET /v1/learning` → memory text; `run_id` → Export's run; generations list → Export's "Generation" choices (last selected); `done = {engine_id, run_id, kind:"run", score: learning.best_score, generations: learning.generations_run, check: executed_markdown(training, shownPlan)}` → result card (`GET /v1/engines/{engine_id}/card`), 3 buttons enabled, name box (`GET …/card`). Then: `done` saved to localStorage `ane-train-result`; records lists refreshed (`GET /v1/records/engines`, `GET /v1/records/stories`, `GET /v1/records/runs`, `GET /v1/records`) incl. *From a recorded engine*'s picker; Play's "Trained AI" re-listed (`GET /v1/exports/engines`) with the newest selected; studio status line refreshed | job `status:"failed"` → toast with `job.error` (e.g. refused: "A deterministic run needs a seed…", or work over `max_simulation_work`); 422 on out-of-range overrides; 400 without client/user header. No confirmation. Disabled-while-running via the shared queue |
| "🏷 Save name" (`rename_result_ui`) | `PUT /v1/engines/{engine_id}/name` | `{"name": text or null}` (`engine_id` = `done.engine_id`) | response `{engine_id, name, label}` → status "Named **{name}**; its id stays `{engine_id}`." or (name null) "Name removed: it is shown as *{label}*."; name box re-read (`GET …/card`); result card re-rendered (`GET …/card`); Finished engines card refreshed only if its picker shows this engine; every engine picker re-labelled (`GET /v1/exports/engines`, `GET /v1/records/engines`): Finished engines, Play "Trained AI", Play "Recorded engine", Train "Recorded engine", Evaluate's two storytellers, Export's finished runs and recorded-engine pickers (values = ids unchanged) | no `done` → "No training has finished in this session yet: run one in 🧠 Train first."; 400 (> 80 chars, control chars); 404 unknown engine |
| "▶ Play this result" (`play_result_ui`) | `GET /v1/exports/engines`; `POST /v1/engines/{engine_id}/play`; then `GET /v1/sessions/{session_id}/journal`, `GET /v1/sessions/{session_id}/transcript` | engine = `done.engine_id`; body = player from Play's New-game form (profile, quest, character, stat points; see Play) | navigates to Play; Play's "Trained AI" set to this engine (choices refreshed); New-game accordion folded; game screen filled (Play › Game screen); Play status = "**Playing the engine just trained, {label}.**  \n{report}". Then ⚙ Advanced → Integration's session line updated | engine not in `engines[]` → "Engine {id} is no longer offered in ▶ Play (finished runs are kept up to a limit, or the API restarted without its records). Nothing was chosen in its place."; stat-points YAML errors; 400 mission not open |
| "⚖ Evaluate this result" (`evaluate_result_ui`) | `GET /v1/exports/engines`, `GET /v1/records/engines` (choices) | — | navigates to Evaluate with both storyteller pickers (judge benchmark, calibration) = this engine; Evaluate note "**Storyteller:** the engine just trained, {label}. Press **Play and judge** …". Nothing starts | same "no longer offered in ⚖ Evaluate" guard |
| "📦 Export this result" (`export_result_ui`) | `GET /v1/exports/engines` | Export's current platform | navigates to Export: source = "The chosen finished run(s)" (radio visible only for non-ANE-AI platforms), generation picker hidden, finished-runs multiselect = `[engine_id]`, note "**Chosen:** the engine just trained, {label}, with its own world, storyteller, training state and tag vectors.", export button enabled. Nothing exported | same guard |

Job result = `SimulationResponse` fields used: `report` (markdown string), `run_id` (string|null), `engine_id` (string|null), `config_download_url`, `engine_download_url` (string, `/v1/artifacts/{id}`), `learning.best_score` (float), `learning.generations_run` (int), `learning.history[]` ({generation, best, mean, worst, spread, diversity, mutation_step, validation, improved}), `learning.learned_traits[]` ({trait, meaning, value, population_mean, bounds, tendency "high|moderate|low"}), `training.resolved` / `training.executed` (dicts with keys of `effective` + `executed.warm_started` int), `training.remembered`. With `start_game:false`: `session_id` null, `game_started` false.

`GET /v1/engines/{engine_id}/card` fields used: `engine_id`, `run_id`, `name` (string|null), `label`, `default_label`, `created_at` (ISO), `kind` ("Run"|"Continued run"|"Retrained run"|"Generation champion"|"Loaded engine"), `world.summary.{locations,npcs,items,events,missions}` (int), `world.id`, `world.current` (bool|null), `generations` (int|null), `generation` (int|null, champion only), `resumable` (bool), `settings.main` / `settings.other` ({key: scalar}), `fitness.training` (float|null), `fitness.improvement` (float|null), `fitness.early_stopped` (bool|null), `judge_in_training` (float|null), `evaluations[]` (newest first: {kind "judge"|…, engagement, story_fitness, judged, stories, at}). 404 = no card.

**Client-side logic to reimplement.**
- Overrides body (`_run_overrides`): include `candidate_count`=population, `generations`, `episodes`=stories, `simulation_steps`=steps, `seed` — each only when its "Set … for this run" checkbox is on and the value is non-empty (as int). `deterministic` = true/false only when Reproducibility is "on"/"off" (omit for "default"). `warm_start` = true/false only for "on"/"off".
- After each resolve: for each of population/generations/stories/steps: `interactive = checkbox`; info "Saved default: {d} · set for this run" if checked else "· used unless set here"; if unchecked, value := `effective.*`. Seed: interactive = checkbox; if unchecked value := `defaults.seed`. Radios' "Saved default (…)" labels recomputed from `defaults`.
- Steps slider look (`_steps_look(work)`): if `work.steps_decide` → label "Steps per simulation", info "The most turns each story is played."; elif `work.full_length_stories` → label "Steps per simulation · not used: full-length stories", info "Every story is played to its end: up to {effective_max_turns} turns.", dashed-purple "unused" style; else label "Steps per simulation · capped at {effective_max_turns}", info "Stories end by themselves after {story_length} turns; steps above it are not played.", unused style.
- Summary (`resolved_markdown(plan, title="This run, resolved")`): heading `#### {title}` and a table | Setting | This run | From | with rows: Population "**{population:,}** storytellers"; Generations; Stories per storyteller; Story length (full-length: "**Full-length stories enabled: up to {story_turns} turns**" from "evolution.full_length_stories (steps N: not used)"; steps decide: "**Bounded stories: up to N turns**" from "the steps, {origin}"; else bounded, from "the story's end (steps N: not all played)"); Seed ("**{seed}** (deterministic)" / "**none** (deterministic, no seed)" / "**random** (not deterministic)"); Warm start (memory: "**on**: from {remembered or 0} remembered storyteller(s)", engine: "**from the recorded engine**", off: "**off**: random storytellers"). "From" column: source "override" → "set for this run (saved default X)", "default" → "saved default", other → "from {source}". Then: `work.override` → "ℹ️ *Steps per simulation (N) does not set the story length.* {reason}"; "🧮 **Work:** {storytellers} storytellers × {generations} generations × {stories} stories each × {turns} turns = **{work}** simulated turns at most, of the **{limit}** allowed."; each `notes[]` as "ℹ️ …"; if `!work.allowed` "⚠ **It will be refused:** {over_by} turns over the limit. Set a smaller population…"; each `errors[]` as "⚠ **It will be refused:** …". Numbers with thousands separators.
- Executed check (`executed_markdown(training, shown)`), appended to the result card: compare keys `population, generations, stories_per_storyteller, requested_steps, story_turns, full_length_stories, deterministic, seed, warm_start, warm_start_mode` between `training.resolved` and `training.executed`, and between the plan shown before the run (`shown.effective`) and `executed`. None differ → "✅ **Ran exactly as summarised before it started:** {pop} storytellers × {gen} generations × {stories} stories × up to {story_turns} turns, seed S|random, warm start {mode} (started from {warm_started} remembered storyteller(s)[; the summary counted {remembered}])." Else "⚠ **The run differs from the summary shown before it started** (key: shown X, ran Y; …): the saved defaults changed in between. It ran with: …".
- `done` object (kept in memory and localStorage `ane-train-result`): `{engine_id, run_id, kind: "run"|"continue"|"retrain", score, generations, check}`. Kind labels: run "New run", continue "Training continued", retrain "Retrained".
- Result card (`completed_view_ui`): no `done.engine_id` → `NO_RESULT` ("No training has finished here yet. Configure a run above and press **🧠 Evolve storytellers** (or continue or retrain one in *From a recorded engine*): its completed engine appears here, ready to play, evaluate or export."), buttons disabled. Else with card: "### ✅ Completed engine: {name or default_label}\n{kind label}. {card body without heading}"; card unreadable: "### ✅ Completed engine `{id}`\n{kind label} · run `{run_id}` · training fitness **{score:.2f}|not benchmarked** … · **{generations}** generation(s)". Then `\n\n{check}` (if any) + "Play, evaluate or export **this** engine: each button opens its destination with this exact engine chosen. No game has been started."
- Engine card markdown (`engine_card_markdown`, shared with Play and Finished engines): heading "### 🏷 {name or default_label or engine_id}" + " *(unnamed)*" if no name; line "{kind} made **{created_at[:16] with T→space}** · engine `{id}` · run `{run_id}`"; "- **World:** {n} places · {n} people · {n} items · {n} events · {n} quests · world `{id[:12]}` · ✓ the active world | ⚠ not the active world (playing it makes its world active)" (or "not recorded"); "- **Training:** champion of generation G | N generation(s) · its training can be continued · {main settings as 'population 40, generations 10, …'}" (or "no training state (a loaded or recorded storyteller)"); "- **Training fitness:** **{training:.2f}** (…) · {improvement:+.1f} over the run, stopped early" or "not benchmarked"; "- **Judge engagement:** **{evaluations[0].engagement:.2f} / 10** … over {judged or stories} stories, judge benchmark|stories judged to fit the fitness of {at}[ (mean story fitness X)][; N earlier evaluation(s)]" or "not evaluated yet (⚖ Evaluate → *Play and judge*…)"; "- **Judge in training:** X / 10" if present; `settings.other` in a collapsible "Every training setting". Setting display names: pop_size→population, generations, episodes_per_genome→stories per storyteller, max_steps→turns per story, validation_episodes→benchmark stories, warm_start→from the learning memory, crossover_rate→crossover, mutation_rate→mutation, elitism_rate→elitism, player→simulated player; others: underscores → spaces; bools yes/no.
- Learning curve rows: per `history[]` entry three points: (generation, `validation`, "benchmark (champion)"), (generation, `best`, "best this generation"), (generation, `mean`, "population average").
- Traits table: trait←`trait`, meaning←`meaning`, evolved←`value`, population←`population_mean`, tendency←`tendency`.
- Engine picker labels everywhere: use the API's `label`; fallback (no label) `"{created_at[:16]} · loaded engine|run · N generation(s) · fitness X.X · {id[:8]}"`.

**Navigation.**
- "▶ Play this result" → Play (starts a game with `done.engine_id`, setup folded).
- "⚖ Evaluate this result" → Evaluate (both storyteller pickers = engine id).
- "📦 Export this result" → Export (finished-run selection = [engine id]).
- Continue/Retrain in *From a recorded engine* switch back to this sub-tab when they finish.

### 🧠 Train › Run › The run's files
<a id="train-run-files"></a>`view: train-run-files` (accordion "The run's files: its story configuration and engine model", closed)

**Purpose.** Download the last finished training's story configuration and engine model.

**On open.** Nothing; filled when a training (run, continue, retrain) finishes.

**Components.**

| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| "Story configuration" | file-download | `GET {config_download_url}` (= `/v1/artifacts/{artifact_id}`) | empty | file name from `Content-Disposition` (`<id>.yaml`) |
| "Engine model (world, storyteller, training state, memory, tag vectors)" | file-download | `GET {engine_download_url}` | empty | `.ane.zip` |

**Actions.** None besides downloading (the studio fetches both files right after the job finishes; a web client can just link the two URLs).

**Client-side logic to reimplement.** None. Artifact URLs live as long as the API process (404 "Artifact not found." after a restart).

**Navigation.** None.

### 🧠 Train › Run › Learning memory
<a id="train-learning-memory"></a>`view: train-learning-memory` (accordion "Learning memory", closed)

**Purpose.** Show how many storytellers the AI remembers from earlier runs (warm start source) and forget them.

**On open.** `GET /v1/learning` at load → `{hall_of_fame: [{score, …}], size: int, path}`. Re-read after every successful Evolve run.

**Components.**

| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Memory text | markdown | `size`, `max(hall_of_fame[].score)` | | |
| "Forget what the AI learned" | button (danger) | — | | no confirmation |

**Actions.**

| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| "Forget what the AI learned" (`forget_learning_ui`) | `DELETE /v1/learning` then `GET /v1/learning` | — | memory text re-rendered | none (no confirm). The run summary is not re-resolved (its "remembered" count stays stale until the next control change) |

**Client-side logic to reimplement.** Text: `size == 0` → "The AI has not learned anything yet: the next run starts from random storytellers."; else "The AI remembers **{size}** storyteller(s) from earlier runs (best fitness {max score:.1f}). New runs continue from them when *Continue learning* is on."

**Navigation.** None.

### 🧠 Train › From a recorded engine
<a id="train-records"></a>`view: train-records`

**Purpose.** Continue a recorded engine's training exactly where it stopped, or retrain (new evolution seeded with its storyteller, on its world, with its saved defaults + the Run form's overrides); preview the retraining's resolved settings.

**On open.**
- At app load: `GET /v1/records/engines` → picker choices (empty list on 404 = no records yet).
- Re-listed after every successful training and via "↻ Refresh" (also by ⚙ Advanced → Records refreshes/imports).

**Components.**

| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| "Recorded engine" | dropdown | `GET /v1/records/engines` → `engines[]`: (label, `id`) | none | label = `label` + " · resumable" if `resumable`; fallback "{created_at[:16]} · {source} · gen {generation} · fitness {score:.1f} · resumable · {id[:8]}"; info "Every run's best, every generation's champion, loaded, continued and retrained engines…" |
| "↻ Refresh" | button | — | | |
| "Further generations" | slider(1..`generation_limit`, 1) | — | 3 | |
| "Continue its training" | button | — | | |
| "Retrain from it" | button | — | | |
| Help text | markdown | static | "*Continue* trains it further exactly where it stopped (its own settings); *Retrain* starts a new evolution seeded with its storyteller, with its saved defaults and what *Run* sets for a run. Either opens *Run* with its completed engine when it finishes." | |
| Retrain summary | markdown | `resolved_markdown(plan, "Retraining it, resolved (its saved defaults + the Train form)")` | "Choose a recorded engine to see what retraining it would run with." | |

**Actions.**

| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Engine chosen, "Further generations" moved, or any Run-form control changed (`retrain_form_ui`) | `POST /v1/simulations/resolve` | Run-form overrides **minus `warm_start`**, `generations` = Further generations, `engine_id` | retrain summary + kept "shown" retrain plan | no engine → placeholder text, plan null; error → "⚠ The retraining's settings could not be resolved: {msg}"; 404 unknown engine |
| "↻ Refresh" (`refresh_records_ui`) | `GET /v1/records/engines`, `GET /v1/records/stories`, `GET /v1/records/runs`, `GET /v1/records`, then `GET /v1/records/stories`, `GET /v1/records/runs`, `GET /v1/records/worlds` | — | this picker + Export/Evaluate/Advanced record lists re-listed | 404 → empty lists |
| "Continue its training" (`continue_recorded_ui`) | `POST /v1/jobs/engines/{engine_id}/continue` → job; poll `GET /v1/jobs/{job_id}` every 0.5 s | `{"generations": int(Further generations), "start_game": false}` | same outputs as Evolve (report, files, curve, traits, run id, Export generations, `done` with `kind:"continue"` and **no** check line) — Learning memory text is **not** refreshed; then sub-tab switches to **Run**; `done` → localStorage; records & Play pickers refreshed as for Evolve | "Choose a recorded engine first."; job failed: e.g. "Engine `x` has no saved training state to continue; retrain it instead.", work over limit; 404 engine not in records |
| "Retrain from it" (`retrain_recorded_ui`) | `POST /v1/jobs/engines/{engine_id}/retrain` → job; poll | Run-form overrides minus `warm_start`, `generations` = Further generations, `"start_game": false` | as Continue, `done.kind:"retrain"`, check = `executed_markdown(result.training, shown retrain plan)`; sub-tab → Run | "Choose a recorded engine first."; job failed (deterministic without seed, work over limit) |

**Client-side logic to reimplement.** Retrain overrides = `_run_overrides(...)` with `warm_start` removed and `generations` forced to the "Further generations" value (so the Run form's own generations override is ignored). Progress text as in Run.

**Navigation.** On success of Continue/Retrain → Train › Run sub-tab with the new completed engine.

### 🧠 Train › 🏷 Finished engines
<a id="train-engines"></a>`view: train-engines`

**Purpose.** Browse every finished/loaded/recorded engine's result card and give it an optional display name (ids unchanged).

**On open.** At app load: choices = finished runs (`GET /v1/exports/engines`) + other recorded engines (`GET /v1/records/engines`, those not already listed); first selected. Then (load and every picker change) `GET /v1/engines/{engine_id}/card` twice (card + name box).

**Components.**

| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Intro | markdown | static | "Every finished run and loaded engine (and, with the records, the other recorded engines): its result card, and an optional name…" | |
| "Engine" | dropdown | finished runs (API `label`, `engine_id`) then other recorded engines (recorded label, `id`) | first | `#engines-picker` |
| "↻ Refresh" | button | — | | re-lists, keeps the value |
| "Name (optional)" | text | card `name` | "" | max 80; disabled when no card; placeholder `default_label` |
| "🏷 Save name" | button | — | | |
| Name status | status-line | | | |
| Card | markdown | `engine_card_markdown(card)` | | `#engines-card`; no card → "*No card for engine `{id}`.*"; nothing chosen → "*Choose an engine to see its card.*" |

**Actions.**

| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Engine changed (`engine_card_ui`, `engine_name_ui`) | `GET /v1/engines/{engine_id}/card` | — | card markdown; name box (value `name`, enabled iff card) | 404 → "No card" text, box disabled |
| "↻ Refresh" | `GET /v1/exports/engines`, `GET /v1/records/engines` | — | choices replaced, value kept | |
| "🏷 Save name" (`rename_engine_ui`) | `PUT /v1/engines/{engine_id}/name` | `{"name": text or null}` | message (as in Run); card + name box re-read; Train › Run's result card re-rendered if `done.engine_id` is this engine; all engine pickers re-labelled (as in Run) | "Choose an engine first."; 400 / 404 |

**Client-side logic to reimplement.** Same card renderer as Train › Run.

**Navigation.** None.

## ▶ Play

Top tab `id="play"` (banner `#nav-play`, address `?section=play`). The play session id (`session_id`, "token") is kept in page memory and in browser storage (localStorage key `ane-play-session`, written whenever it changes). On page load (`restore_game_ui`) a kept id is reopened with `GET /v1/sessions/{session_id}` (the game screen and the story are drawn, the New game panel folds); a `404` (the API restarted) clears the kept id, any other error keeps it for the next load. The same token is what ⚙ Advanced → 🔌 Integration and 📦 Export ("This game's storyteller") act on.

### ▶ Play › 🎮 New game
<a id="play-setup"></a>`view: play-setup` (accordion "🎮 New game", open; folds after any successful game start)

**Purpose.** Choose a trained engine, player profile, quest and character stats, then start a game (or continue a save game file).

**On open.**
- App load: `GET /v1/exports/engines` → "Trained AI" (newest selected, current value kept if still listed); `GET /v1/missions/playable?profile={default profile}` → quest choices + overview; `GET /v1/engines/{engine_id}/card` for the selected engine (card accordion); `GET /v1/judge/status` (judge accordion).
- Cross-view: after every successful training (Train) and after "Load & play" → "Trained AI" re-listed with the newest selected (card re-read on change). Name changes anywhere re-label "Trained AI" and "Recorded engine". "▶ Play this result" (Train) sets "Trained AI" to that engine and starts a game.

**Components.**

| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| "Trained AI" | dropdown | `GET /v1/exports/engines` → (`label`, `engine_id`) | newest | info "Finished runs and loaded engines, newest first." |
| "Player profile" | dropdown (free text allowed) | `GET /v1/variables/profiles` → `profiles` | `default` | info "Its progression and persistent variables are kept."; a typed new name is a new profile |
| "Quest" | dropdown | `[("Any quest (the story draws one)", "")]` + playable missions with `available` true: (`title`, `key`) | "" | info "Or let the story draw one." |
| "Character" | dropdown | hard-coded `[("Story default","story default"),("Random","random"),("Balanced","balanced"),("My own points","defined")]` | "story default" | info "My own points: set them below." |
| "▶ New game" | button (primary) | — | | |
| "📂 Continue a save game" | file-upload(.json) button | — | | upload triggers immediately |
| "↻ Refresh" | button | — | | |
| "🏷 About this engine: its result card" | markdown (accordion, closed) | `GET /v1/engines/{engine_id}/card` | | `#play-engine-card`; same renderer as Train |
| Play status | status-line (below the setup) | start/continue/save messages | "" | |

**Actions.**

| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| "Trained AI" changed (`engine_card_ui`) | `GET /v1/engines/{engine_id}/card` | — | card markdown | 404 → "*No card for engine `{id}`.*" |
| "Player profile" changed (`playable_quests_ui`) | `GET /v1/missions/playable?profile={profile}` | profile (omitted if empty) | Quest choices replaced, value reset to ""; quests overview refreshed | |
| "▶ New game" (`play_new_game_ui`) | `POST /v1/engines/{engine_id}/play` (`engine_id` = "latest" when nothing chosen); then `GET /v1/sessions/{session_id}/journal`, `GET /v1/sessions/{session_id}/transcript` | player body (see client logic) | `session_id` → token; game screen from the response (Play › Game screen); story; status = `report` ("Playing the finished run of … (engine `xxxxxxxx`): its world, with its storyteller."); setup folds; Integration's session line updated | 409 no finished run ("No finished run yet: train the AI first…"); 400 mission not open / unknown; 400 invalid stat points ("Your stat points are invalid: …"); client: points YAML must be a mapping ("The stat points must be a mapping of stat names to points.") |
| "📂 Continue a save game" (`play_continue_ui`) | `POST /v1/sessions/load`, then journal + transcript | `{"savegame": <parsed JSON file>, "session_id": <current token or null>}` | new `session_id` → token; game screen; story; status "Continued exactly where it was saved." (`restored:"exact"`) or "Rebuilt from its parameters ({note or 'no usable snapshot'})."; setup folds | client: no file → "Choose a save game (.json) to continue."; unreadable JSON → "Not a JSON save game: …"; 400 from API |
| "↻ Refresh" (`play_runs_ui`) | `GET /v1/exports/engines`, `GET /v1/records/engines` | — | "Trained AI" re-listed (value kept if present, else newest); "Recorded engine" (Another engine) re-listed | |

**Client-side logic to reimplement.**
- Player body (`_player`): `{"profile": profile or null, "mission": questKey or null}`; Character "defined" → parse the stat-points YAML; must be an object → `"stats": {stat: int}`; Character "random"/"balanced" → `"stats_mode": value`; "story default" → neither.
- Starting a game always folds this accordion; the game screen then renders the response (PlayState fields + `report`).

**Navigation.** Reached from Train's "▶ Play this result" (game already started, engine preselected, setup folded).

### ▶ Play › 🎲 My own stat points
<a id="play-stat-points"></a>`view: play-stat-points` (accordion inside New game, closed)

**Purpose.** Edit/preview the player's own stat allocation used when Character = "My own points".

**On open.** Intro text and editor from `GET /v1/stats` (read at app load): "Spread **{allocation.total_points or 50} points** over `stat1`, `stat2`…; each stat takes {min_points or 5}-{max_points or 20} points. *Character: My own points* plays with them." (or "no stats"). Editor = YAML of `character_stats.defined_points`. The check line is computed whenever the editor changes (including programmatic changes).

**Components.**

| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| "Randomize" | button (small) | — | | |
| "Balance" | button (small) | — | | |
| "Stat points" | yaml-editor (4 lines) | YAML mapping stat → points | `defined_points` as YAML | |
| Check line | status-line | `POST /v1/stats/allocate` | | |

**Actions.**

| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| "Randomize" / "Balance" (`stats_points_text`) | `POST /v1/stats/allocate` | `{"mode": "random"}` / `{"mode": "balanced"}` | `points` → editor as YAML; Character dropdown set to "defined" ("My own points"); editor change triggers the check | toast on API error |
| Editor changed (`check_points_text`) | `POST /v1/stats/allocate` | `{"points": parsed YAML if it is a mapping else {}}` | `errors[]` non-empty → "⚠️ {errors joined by space}"; else "✅ {sum(points)} of {budget.total_points} points · values: {stat value, …}" (from `values`) | YAML parse error → "⚠️ {message}" |

Response of `/v1/stats/allocate`: `{mode, points: {stat:int}, values: {stat:int 1-100}, errors: string[], stats: string[], budget: {total_points, min_points, max_points, value_per_point}}` (`budget` absent when `errors` came from an invalid allocation).

**Client-side logic to reimplement.** YAML ↔ object for the editor; empty YAML = `{}`.

**Navigation.** None.

### ▶ Play › 🗂 Another engine: a recorded one, or a file
<a id="play-other-engine"></a>`view: play-other-engine` (accordion inside New game, closed)

**Purpose.** Start a game with any recorded engine (a generation's champion, …) or with an uploaded engine file (kept afterwards as a finished engine).

**On open.** `GET /v1/records/engines` at app load (choices); re-listed by Play's "↻ Refresh", after trainings and imports, and re-labelled after a rename.

**Components.**

| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| "Recorded engine" | dropdown | as Train's recorded-engine picker | none | info "Any run's best, a generation's champion, ..." |
| "▶ Play with it" | button | — | | |
| Help | markdown | static | "Or an engine file: an engine model (`.ane.zip`), an older engine ZIP, or a complete configuration YAML (with its model-parameters YAML when it has no `generated_run.genome`). It is kept: play it again, or export it." | |
| "Engine model or configuration" | file-upload(.zip, .yaml, .yml) | — | | server limit `store.MAX_ENGINE_UPLOAD_BYTES` (413) |
| "Model parameters (optional)" | file-upload(.yaml, .yml) | — | | ≤ 100 000 bytes |
| "▶ Load & play" | button | — | | |

**Actions.**

| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| "▶ Play with it" (`play_recorded_ui`) | `POST /v1/records/engines/{engine_id}/play`, then journal + transcript | player body (from New game form) | as "▶ New game"; status = `report` ("Recorded engine `id` loaded. Fitness on the benchmark …") | "Choose a recorded engine first."; 404 not recorded; 400 |
| "▶ Load & play" (`play_engine_file_ui`) | `POST /v1/engines/load` (multipart), then journal + transcript | multipart fields `file` (required), `parameters` (optional); query `profile`, `mission`, `stats_mode` (random/balanced), `stats` (JSON string of own points, only for "My own points") — each omitted when empty | as "▶ New game"; then "Trained AI" re-listed with the newest (this engine) selected; studio status refreshed | "Choose an engine model, an engine ZIP or a configuration YAML first."; 400 wrong type ("Allowed file types: …"), 413 too large, 400 bad `stats` JSON |

**Client-side logic to reimplement.** Same player-body builder, but sent as query params for the multipart load.

**Navigation.** None.

### ▶ Play › 🎯 The world's quests, and this profile's save game
<a id="play-quests-savegame"></a>`view: play-quests-savegame` (accordion inside New game, closed)

**Purpose.** List the world's quests for the chosen profile (which are open now) and show the profile's save game (progression: goals achieved, past session reports).

**On open.** Quest overview from `GET /v1/missions/playable?profile=…` (app load with the default profile; refreshed on profile change). Save game: nothing until "Show the save game".

`GET /v1/missions/playable` → `missions[]`: `{key, title, description, stages: string[], available: bool, achieved: bool}`.
`GET /v1/progression?profile=` → `{profile, playthroughs: int, goals_achieved: string[], sessions: [report…], values, updated}`; each session report: `report_id`, `ended_at`, `start_location`, `goal` ({title,…}|null), `outcome` ("achieved"|"failed"|"free"), `turns`, `player.inventory[]`, `delivery` ({sent, status, error, at}|absent).

**Components.**

| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Quests overview | markdown | playable missions | | |
| Help | markdown | static | "Every session saves the player's progression … *Reset profile* (🌍 World → 📜 Story rules → Persistent variables) starts a new save game." | |
| "Refresh the quests" | button (small) | — | | |
| "Show the save game" | button (small) | — | | |
| Save game text | markdown | progression | "" | |
| "Sessions" | table(ended, start, goal, outcome, turns, inventory, sent to UNP, report) | `sessions` newest first | empty | read-only |

**Actions.**

| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| "Refresh the quests" (`playable_quests_ui`) | `GET /v1/missions/playable` | `profile` = Player profile | overview + Quest dropdown choices (value reset to "") | |
| "Show the save game" (`progression_ui`) | `GET /v1/progression` | `profile` = Player profile (omitted if empty) | text "**Save game `{profile}`:** {playthroughs} session(s) played · goals achieved (each once per save game): {goals or 'none yet'}."; table rows | 400 toast |

**Client-side logic to reimplement.**
- Overview: "**Quests in this world** (for profile `{profile or 'default'}`):" then per quest "- **Title**" (available) or "- ~~Title~~ (achieved in this save game | not open now)", + " — {description}" + " *Stages:* A → B → C".
- Table row: ended = `ended_at[:19]`; start = `start_location`; goal = `goal.title` or "free play"; outcome; turns; inventory joined ", "; sent to UNP = "yes" if `delivery.sent` else `delivery.error` or "no"; report = `report_id`. Rows reversed (newest first).

**Navigation.** Text points to 🌍 World → Story rules and ⚙ Advanced → Integration (no buttons).

### ▶ Play › Game screen
<a id="play-game"></a>`view: play-game`

**Purpose.** Play the open session: status bar, where you are, what your last choice did, one button per choice (with its check), quest log, character panel; save or end the game.

**On open.** Placeholder until a game starts: scene "Choose a trained AI and press **▶ New game**.", quest "### 🎯 Quest\nStart a game to see its quest.", you "### 🧍 You\nStart a game to see your character.", ending/HUD/turn card hidden, no choice buttons. Every state-changing response (start, action, end, load, and ⚙ Advanced → Integration's player PATCH / sync) is followed by `GET /v1/sessions/{session_id}/journal` (→ `journal[]`) and `GET /v1/sessions/{session_id}/transcript` (→ `transcript` string); failures of these two are ignored (empty).

**Components.**

| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Ending banner | markdown | `ended`, `won`, `end_reason` | hidden | visible only when `ended` |
| HUD | markdown | `_hud(state)` | hidden | visible once a game exists |
| Scene card | markdown | `place_markdown(state)` | placeholder | |
| Turn card ("What happened") | markdown | `turn_summary`, `last_turn` | hidden | visible when non-empty |
| Choice buttons ×12 | button | `actions[i]` + check from `choices[]` | hidden | the server offers ≤ `player_options.limit` (default 6); beyond 12 the HUD says "⚠ N more choice(s) than this screen shows (12): lower `player_options.limit` …". A rebuild should render every item of `actions` |
| "💾 Save game" | button (small) | — | | |
| "End the game" | button (small, danger) | — | | no confirmation |
| "Save game (JSON)" | file-download | `GET /v1/sessions/{session_id}/savegame` | hidden | shown after a save |
| Quest card (right column) | markdown | `quest_markdown(quest, journal)` | placeholder | |
| You card (right column) | markdown | `you_markdown(state)` | placeholder | |

**Actions.**

| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Choice button i (`play_choose_ui`) | `POST /v1/sessions/{session_id}/actions`, then journal + transcript | `{"action": actions[i]}` (`actions` = the list from the last state) | whole screen + People/Journey/Story re-rendered from the PlayState | no token → "Start a new game first (choose a trained AI and press New game)."; i ≥ len(actions) → "That choice is no longer there."; 400 action not offered; 404 session gone |
| "End the game" (`play_end_ui`) | `POST /v1/sessions/{session_id}/end`, then journal + transcript | — | screen re-rendered (ending banner, no choices; `session_report` shown in People & map) | no token → "Start a new game first."; 404 |
| "💾 Save game" (`download_savegame_ui`) | `GET /v1/sessions/{session_id}/savegame` | — | JSON (`ane.savegame/1`) offered as file `savegame-{session_id[:8]}.json`; download made visible; Play status "Saved after {parameters.turns or 0} turn(s) at **{parameters.current_location}** (`{format}`). *Continue a save game* goes on from it, here or in the ANE AI." The session goes on | no token → "Start a game first in ▶ Play (New game, another engine, or continue a save game)." |

**Client-side logic to reimplement** (`_play_screen`):
- Ending: `ended` → "## 🏆 Quest complete\n{end_reason}" if `won` else "## The end\n{end_reason}".
- HUD (`_hud`), parts joined by "  ·  ": "📍 **{location or '?'}**"; if `health` int and `max_health`: "❤ `{'█'×f}{'░'×(10−f)}` {health}/{max_health}" with f = clamp(round(10·max(0,health)/max_health), 0, 10); "🪙 {coins}" if not null; if `quest`: "🎯 {quest.title}" + " · now: {title of stage with state 'current'}"; "⚡ {event}"; "💬 talking with {talking_to}".
- Scene (`place_markdown`): if the state has no `description` key (old API) show `scene`; else "### 📍 {scene_name or location}", `description`, "👥 " + people here joined " · " as "**{name}**" + " ★" (quest) + " ⚠" (hostile) + " ♥" (ally) + " · {role}" when `importance ≥ 2` and `role`; "👁 You see: {items_here[].name joined}".
- Turn card: if `turn_summary` and `last_turn` contains a line starting with "> " → `turn_card_markdown`; elif `last_turn` → "### What happened\n" + `story_markdown(last_turn)`; else hidden. `turn_card_markdown(s, last_turn)`: "### What happened"; "**▶ Turn {s.turn}: you chose _{action without leading '(…) '}_**" + " — with {s.with}" (unless already in the text) + " · at {s.where}"; if `s.check`: "🎲 **{stat}** check: rolled {roll}, needed {needed} or less → **passed ✓** / **failed ✗**"; if `s.told`: "> " + ("✗ " if not succeeded and no check) + told; change chips (below) + "⚡ {event}" joined " · "; "🏆 **Quest complete!**" if `quest_complete` else "🎯 **Quest step done:** {quest_done joined ' · '} → **now:** {quest_now}"; then up to 6 further story lines of `last_turn` (parsed by `_story_line`, skipping kinds action/result/arrival/met and tags QUEST / MISSION COMPLETE) in `<small>`.
- Change chips (`_changes`): "❤ +n/−n" (`health_change`), "🪙 ±n" (`coin_change`), "🎒 got {item}" per `items_gained`, "🎒 {item} gone" per `items_lost`, "📍 {moved_to}" + " (new place)" if in `new_places`, "👤 met {met joined}". Minus sign is U+2212.
- Choice button label: "{i+1}.  {action}" + "   ·   🎲 {check text}" when `choices[name==action].check`; check text = "{stat} {chance}%" + " (fail: {risk.health} health, {risk.coins} coins, sent to the {risk.sent_to})" (only non-zero/non-null parts; parenthesis omitted when none).
- Quest card (`quest_markdown(quest, journal)`): no quest → "### 🎯 Quest\nThis story has no quest: explore as you like."; else "### 🎯 {title}", "*{description}*", progress "`{'▰'×done}{'▱'×rest}` **{done} of {n} steps**", per stage "✅|▶️|▫️ **{title}**" (+ " · turn T" for done stages, T = first journal turn whose `quest_done` contains the title; + "  \n{description}" for the current stage); status "completed" → "🏆 **Completed** at turn T." (T = first journal entry with `quest_complete`); other non-"in progress" status → "**{Status}.**"; in progress with `todo` → "**To do now:** {todo joined '; '}".
- You card (`you_markdown`): "### 🧍 You"; "⚡ power {power:.2f}"; "🎒 {inventory joined or '*nothing yet*'}"; "📊 {stat} **{value}** (+{value − stat_points[stat]} when larger)" joined " · "; "💾 {k} {v}" for `variables` except `playthroughs`; if `stakes.alert`: "🚨 alert {alert}/{max}" + " — the watch is looking for you" when alert ≥ `warn_at`; if `last_check`: "🎲 last check: {stat} {roll} vs {target} — passed|failed".
- `actions` from the latest state are kept client-side; buttons send exactly those strings.

**Navigation.** None (⚙ Advanced → Integration acts on this session).

### ▶ Play › 👥 People met & the map
<a id="play-people-map"></a>`view: play-people-map` (accordion in the right column, closed)

**Purpose.** Everyone met, the map as known, the session's cast, one-time actions tried, and the session report once ended.

**On open.** Re-rendered from every PlayState (no own request). Default "### People met\nNobody yet.".

**Components.**

| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| People/map card | markdown | `people_met`, `people`, `map`, `cast`, `unique_actions`, `session_report` | "### People met\nNobody yet." | |

**Actions.** None.

**Client-side logic to reimplement** (`people_markdown` + `map_markdown` + `session_markdown`):
- "### People met" (or "Nobody yet."); per `people_met[]`: "- **{name}** · met at {where joined ', '}" + notes joined " · ": "here now" (name in `people[]`), "★ can help with your quest" (that `people[]` entry's `quest`), "⚠ hostile", "gone from the story", "spoken with {talks}×", "♥ ally", "◆ {role}" when `importance ≥ 2`.
- "### Map": build adjacency from `map.paths` (both ways) and `map.one_way` (from → "to (one way)"); per `map.places[]`: "- " + "📍 " if name == `map.current` + "**{name}**" + " 🔒" if locked + " *(not visited)*" if not visited + " → {sorted neighbours joined ', ' or 'no known way on'}".
- "### This session's cast": if any non-empty `cast.left_out[kind]`: "Not in this session (its goal does not need them): {kind}: a, b; …."
- "### One-time actions": per `unique_actions[]`: "- {action} ({where}, turn {turn}): done | failed, still on offer (`kept`) | failed, used up".
- "### Session report" (when `session_report`): "**{goal.title or 'Free play'}: {outcome}** ({end_kind}, {turns} turns) · health {player.health}/{player.max_health} · coins {player.coins} · inventory: {… or 'empty'} · stats: {k v, …} · goals achieved in this save game: {progression.goals_achieved or 'none'}" + the delivery, read from the save game (`GET /v1/progression?profile=<report.profile>`, the session whose `report_id` matches; the play state never carries it): " · sent to the UNP" when `delivery.sent`, " · not sent to the UNP (<error or HTTP status>)" when it failed, nothing when there is no delivery (no UNP configured, or still being sent).

**Navigation.** None.

### ▶ Play › 🧭 Your journey
<a id="play-journey"></a>`view: play-journey` (accordion "🧭 Your journey: every choice and what came of it", closed)

**Purpose.** Table of the turns so far.

**On open.** Re-rendered after each state change from `GET /v1/sessions/{session_id}/journal` → `journal[]` (same entry shape as `turn_summary`, oldest first). Empty → "*Your journey starts with your first choice.*".

**Components.**

| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Journey | markdown table(#, Where, You chose, What happened, Changes, Quest) | last 15 `journal` entries | placeholder | "*… N earlier turn(s)*" above when more |

**Actions.** None.

**Client-side logic to reimplement.** Per entry: # = `turn`; Where = `where` (cut to 24 chars); You chose = `action` without leading "(…) " (40) + " · {with}" (24); What happened = mark + " " + `told` (70), mark = "✓"/"✗" by `succeeded`, or with a `check` "✓|✗ 🎲{check.stat}" by `check.passed`; Changes = chips joined " · " (60); Quest = "🏆 complete" if `quest_complete` else "✅ {title}" per `quest_done` joined " · " (60). Cell text: `|`→`/`, newlines→space, truncated with "…".

**Navigation.** None.

### ▶ Play › 📖 The story so far
<a id="play-story"></a>`view: play-story` (accordion, closed; scrollable, max height 480 px)

**Purpose.** The full transcript, formatted for reading.

**On open.** After each state change: `GET /v1/sessions/{session_id}/transcript` → `transcript` (string, lines separated by "\n"). Error → empty.

**Components.**

| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Story | markdown | `story_markdown(transcript)` | "" | "*The story has not started yet.*" when nothing remains |

**Actions.** None.

**Client-side logic to reimplement** (`_story_line` per line, joined by blank lines):
- skip empty lines and lines starting "Continuing your activities"; "> X" → "**▶ X**" (and a "---" rule before it unless first); "RESULT: X" → X; "You are in the {P}. {rest}" → "#### 📍 The {P}\n{rest}"; "Met …" → "👤 Met …"; "[TAG] text": TAG "LOCATION" skipped; else "{mark} {text}" with text in bold when TAG ∈ {MISSION, MISSION COMPLETE, QUEST, TRIUMPH, END, EPILOGUE, FINALE, CLIMAX}; other lines verbatim.
- Marks: MISSION 🎯, MISSION COMPLETE 🏆, QUEST ✅, TRIUMPH 🏆, CLIMAX ⚔, FINALE ⏳, EVENT ⚡, EVENT END ⚡, UNEXPECTED EVENT ✨, TWIST ✨, ENCOUNTER ⚔, EVENT EFFECT ·, EVENT DISCOVERY 🗺, SPAWN 🗺, DISCOVERY 🗺, ROUTE 🧭, VOYAGE 🧭, LEAD 🧭, ON THE WAY 🧭, BACK ↩, MOVE ON 🧭, AUTO TRANSFER 🧭, REVEAL 👁, REVELATION 👁, SECRET 🤫, CALLBACK 💡, LAST TIME 🕰, CHECK 🎲, STAT 📈, HEALTH ❤, RESOURCES 🪙, REFUND 🪙, PERSISTENT 💾, FAILURE ✗, SETBACK ✗, ANOTHER WAY ↪, ALERT 🚨, MANHUNT 🚨, SENT ⛓, ESCAPE 🔓, RELEASED 🔓, UNLOCKED 🔓, NPC REMOVED 👋, ALLY ♥, CONSEQUENCE ↻, END 🏁, EPILOGUE 🏁, RESUMED ▶; unknown tag •. Tag regex `^\[([A-Z ]+)\]\s*(.*)$`.

**Navigation.** None.

### ▶ Play › ⚖ Judge this playthrough
<a id="play-judge"></a>`view: play-judge` (accordion "⚖ Judge this playthrough (Gemma 4 as a game critic)", closed)

**Purpose.** Ask the LLM judge to rate the game played so far.

**On open.** App load: `GET /v1/judge/status` → `{available, mode, model, server, reason, how}`.

**Components.**

| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Judge status | markdown | status | | available → "✅ **The judge is ready:** {model} at `{server}`" (or " (in the API's process)"); else "⚠ **The judge is not available here.** {reason}\n\n{how}" |
| "Judge the game played so far" | button | — | | not disabled when unavailable |
| Judgement | markdown | response | "" | |

**Actions.**

| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| "Judge the game played so far" (`judge_session_ui`) | `POST /v1/sessions/{session_id}/judge` | — | response `{engagement: float|null, scores: {agency, pacing, surprise, payoff, exploration, stakes, coherence, replayability}, spread: float|null, verdict, best_moment, weakest_moment}` → judgement markdown | no token → "Start a game first."; 404; 503 judge unavailable |

**Client-side logic to reimplement.** `engagement` null → "### This playthrough\nThe judge's reply could not be read; try again."; else "### This playthrough: engagement {e:.1f}/10" + " (spread {s:.1f})", scores "name **v.v**" joined " · ", "**Best moment:** …", "**Weakest moment:** …", "> {verdict}" ("-" when missing).

**Navigation.** None.

### ▶ Play › Reference: the play-state response
<a id="play-state-reference"></a>`view: play-state-reference` (not a screen)

`GET /v1/sessions/{session_id}`, `POST /v1/sessions/{session_id}/actions`, `POST /v1/sessions/{session_id}/end`, `PATCH /v1/sessions/{session_id}/player`, `POST /v1/sessions/{session_id}/sync` return **PlayState** (api.py `PlayState`, built by `_play_state` from `sessions._scene` + `sessions.play_state`). `POST /v1/engines/{engine_id}/play`, `POST /v1/records/engines/{engine_id}/play`, `POST /v1/engines/load` and the training jobs' `result` return **SimulationResponse** (PlayState + extras); `POST /v1/sessions/load` returns **LoadedState** (PlayState + extras). The studio never calls `GET /v1/sessions/{session_id}` itself.

| Field | Type | Meaning | Shown in the studio |
|---|---|---|---|
| `session_id` | string | the session token | kept as the open game (all session calls) |
| `scene` | string (markdown) | server-rendered scene (heading, quest line, health/coins/power, inventory, people, stats, checks, alert, last check, remembered, visible items, end) | only as fallback when `description` is missing (never with this API) |
| `actions` | string[] | offered choices (names to send back) | choice buttons (value + label) |
| `last_turn` | string | story text of the turn just played ("" for GET/end; "[RESUMED] The story goes on." on parameter load; "Player set by the UNP: …" / "From the UNP's game: …" after PATCH/sync) | turn card |
| `ended` | bool | story over | ending banner; buttons gone |
| `event` | string\|null | active event title | HUD "⚡" |
| `talking_to` | string\|null | person in conversation | HUD "💬" |
| `turn_summary` | object\|null | last journal entry (see journal entry below) | turn card |
| `mission` | string\|null | mission title | — (quest used instead) |
| `won` | bool | goal achieved | ending banner title |
| `location` | string\|null | current place | HUD "📍", scene heading fallback |
| `scene_name` | string\|null | place/event name as told | scene heading |
| `description` | string\|null | place/event as seen now | scene card |
| `items_here` | [{name: string, text: string}] | visible items (empty when ended) | scene "👁 You see:" (names) |
| `start_location` | string\|null | where the story started | — |
| `health`, `max_health` | int\|null | | HUD bar |
| `coins` | int\|null | | HUD "🪙" |
| `power` | float\|null | 3 decimals | You "⚡ power" |
| `inventory` | string[] | sorted | You "🎒" |
| `mission_status` | string\|null | engine's mission status text | — |
| `end_reason` | string\|null | | ending banner |
| `profile` | string\|null | save-game profile (null when not saved) | — |
| `variables` | {name: any} | persistent variables (+ `playthroughs`) | You "💾" (without `playthroughs`) |
| `stats` | {stat: int 1-100} | values incl. item bonuses | You "📊" |
| `stat_points` | {stat: int} | allocated points | You (bonus "+n" = stats − stat_points) |
| `last_check` | {stat, roll: int, target: int, passed: bool, step: int}\|null | | You "🎲 last check" |
| `quest` | {key, title, description, status: "in progress"\|"completed"\|"failed", stages: [{title, description, state: "done"\|"current"\|"todo"}], todo: string[]}\|null | | quest card; HUD "🎯 … now:" |
| `people` | [{name, called, description, wants: string\|null, hostile: bool, talks: int, quest: bool, helped: int, ally: bool, role: string\|null, importance: int 0-3}] (empty when ended) | people here | scene "👥"; People card "here now", "★" |
| `people_met` | [{name, called, where: string[], talks: int, hostile: bool, gone: bool, ally: bool, role, importance: int}] | | People card |
| `map` | {current: string, previous: string\|null, places: [{name, visited: bool, locked: bool}], paths: [[a, b]], one_way: [[from, to]]}\|null | | People card "### Map" |
| `choices` | [{name: string, check: {stat: string, chance: int 0-100, bonus: int, risk: {health: int≤0, coins: int≤0, sent_to: string\|null}\|null}\|null}] (empty when ended) | | choice-button labels |
| `ways_on` | [{name, visited: bool, toward_quest: bool}] (empty when ended) | travel targets | — |
| `cast` | {left_out: {locations, npcs, items, events: string[]}, kept: {kind: int}}\|null | | People card "This session's cast" |
| `unique_actions` | [{turn: int, action, owner, where, succeeded: bool, kept: bool}] | | People card "One-time actions" |
| `session_report` | object\|null (`ane.session_report/1`): {format, report_id, session_id, profile, started_at, ended_at, start_location, goal: {key, title, description, stages: string[], stages_done: int}\|null, outcome: "achieved"\|"failed"\|"free", end_kind, end_reason, turns: int, player: {health, max_health, coins, power, inventory[], stats{}, stat_points{}, stat_changes{}}, story: {places_visited[], people_met[], allies[], defeated[], unique_actions[], cast}, progression: {goals_achieved[], playthroughs: int, variables{}}} | after the session ended | People card "Session report" |
| `stakes` | {alert: int, max: int, warn_at: int}\|null | null when alert is off | You "🚨 alert" |

Journal / `turn_summary` entry: `{turn: int, action: string, with: string|null, where: string, moved_to: string|null, check: {stat, roll, needed, passed}|null, succeeded: bool, told: string|null, health: int, health_change: int, coins: int, coin_change: int, items_gained: string[], items_lost: string[], met: string[], new_places: string[], event: string|null, quest_done: string[], quest_now: string|null, quest_complete: bool, ended: bool, end_reason: string|null}` — used by turn card, quest card (turn numbers) and journey.

SimulationResponse extras (start/training responses): `session_id` string|null, `game_started` bool (false for training-only), `report` string (markdown; shown in Play status / Train report), `run_id`, `engine_id`, `learning`, `config_download_url`, `engine_download_url`, `training` (see Train). In a training-only response PlayState fields are empty defaults.

LoadedState extras: `restored` "exact"|"parameters", `note` string|null → Play status.

## ⚖ Evaluate

Top tab `id="evaluate"` (open directly with `?section=evaluate`). One screen with a header, the judge status, and three
accordions. Each accordion is a view of its own below. All judge work uses an LLM judge (Gemma 4 via vLLM). Errors from the
API are shown as an error toast with the API's `detail` text. For a 422 validation list, the text is
`loc[1:]: msg` items joined with "; ".

### Shared: engine pickers, engine labels and the engine card

Used by ⚖ Evaluate (storyteller pickers) and 📦 Export (finished runs, recorded engines). The endpoints and
`backend/engine_meta.py` are new and still being edited. This describes them as they are now.

**Engine id vs label.** The value of every engine picker is the `engine_id`. The label shown is the API's `label`. Engines are
named in 🧠 Train (`PUT /v1/engines/{engine_id}/name`, body `{name: string|null}`, max 80 chars, no control characters, 400
otherwise, 404 unknown engine. Response `{engine_id, name, label}`). Server label format: named
`"<name> · <YYYY-MM-DD HH:MM> · <id[:8]>"`, unnamed `"<Kind> of <YYYY-MM-DD HH:MM>[ · generation N | · N generation(s)][ · fitness X.X] · <id[:8]>"`
where Kind ∈ `Run | Continued run | Retrained run | Generation champion | Loaded engine`.

**Cross-view refresh after a rename (in Train).** The choices of Evaluate's two storyteller pickers, Export's "Finished runs"
picker and Export's "Recorded engine" picker are re-fetched (labels change, values do not). Use the same requests as on open:
`GET /v1/exports/engines` and `GET /v1/records/engines`.

**Card request.** `GET /v1/engines/{engine_id}/card` (404 for an unknown engine. The client then shows
`*No card for engine \`<id>\`.*` and logs it). Response fields used:
- `engine_id` str, `run_id` str|null, `name` str|null, `default_label` str, `label` str
- `kind` str, `created_at` ISO str
- `world.id` str|null, `world.summary` {`locations`,`npcs`,`items`,`events`,`missions`: int} (may be `{}`), `world.current` bool|null
- `generations` int|null, `generation` int (present only for a generation champion without a model file)
- `resumable` bool
- `settings.main` {key: scalar}, `settings.other` {key: scalar}
- `fitness.training` number|null, `fitness.improvement` number|null, `fitness.early_stopped` bool|null (`fitness.history` [number] is not drawn)
- `judge_in_training` number|null
- `evaluations[]` (newest first, max 10): `kind` ("judge"|"calibration"), `at` ISO, `stories` int, `judged` int,
  `engagement` number|null, `criteria` {name: number}?, `story_fitness` number?, `cv_spearman` number? (calibration)

**Card rendering (client-side, `engine_card_markdown`).** Markdown:
1. Heading `### 🏷 <name || default_label || engine_id>`, plus ` *(unnamed)*` when `name` is null.
2. `<kind || "Engine"> made **<date>** · engine \`<engine_id>\`[ · run \`<run_id>\`]`. date = `created_at[:16]` with `T`→space, or "unknown date".
3. `- **World:** ` + parts joined " · ": counts from `world.summary` in the order locations "places", npcs "people", items "items",
   events "events", missions "quests" (only keys present, e.g. "12 places · 5 people"). Then `world \`<world.id[:12]>\``. Then
   `world.current` true → "✓ the active world", false → "⚠ not the active world (playing it makes its world active)", null → nothing. Empty → "not recorded".
4. `- **Training:** ` parts joined " · ": if `generation` is set and `generations` null, "champion of generation N". Else if
   `generations`, "N generation(s)". Add "its training can be continued" if `resumable`. Add the `settings.main` items as
   "<name> <value>" joined ", ". Empty → "no training state (a loaded or recorded storyteller)". Setting display names:
   pop_size→population, generations→generations, episodes_per_genome→stories per storyteller, max_steps→turns per story,
   validation_episodes→benchmark stories, warm_start→from the learning memory, crossover_rate→crossover,
   mutation_rate→mutation, elitism_rate→elitism, player→simulated player. Any other key: underscores→spaces. Booleans
   show as yes/no.
5. `- **Training fitness:** **<fitness.training:.2f>** (the evolution's objective: …)`, plus `· <improvement:+.1f> over the run` and/or
   "stopped early". When `fitness.training` is not a number: "not benchmarked".
6. Judge engagement: take `evaluations` with numeric `engagement`, and use the first one (latest):
   `- **Judge engagement:** **<engagement:.2f> / 10** … over <judged||stories> stories, <"judge benchmark" if kind=="judge" else "stories judged to fit the fitness"> of <date(at)>[ (mean story fitness <story_fitness:.1f>)][; N earlier evaluation(s)]`.
   None → "not evaluated yet (⚖ Evaluate → *Play and judge*; …)".
7. If `judge_in_training` is a number: `- **Judge in training:** <:.2f> / 10 …`.
8. If `settings.other` is not empty: a collapsible `<details><summary>Every training setting</summary>` holding the items as "name value" joined ", ".

Training fitness and judge engagement are deliberately kept as separate lines (different scales).

---

### ⚖ Evaluate › Header and judge status
<a id="evaluate"></a>`view: evaluate`

**Purpose.** Says whether the LLM judge can be used here. If it cannot, says why and how to make it available.

**On open.** At page load: `GET /v1/judge/status` → `{available: bool, mode: "server"|"embedded", model: str, server: str|null, reason: str|null, how: str|null}` (judge_status_ui).

**Components.**
| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Intro text | markdown | static: "Gemma 4 as a game critic (`vllm` in the engine logic…). Stories are played in a separate process… The judge also runs in the loop of evolution when `evolution.judge_weight` > 0 (Engine logic)." | | |
| Evaluate note | markdown | set by 🧠 Train's "⚖ Evaluate this result" | empty | see Navigation |
| Judge status | markdown | `GET /v1/judge/status` | | |
| Check the judge again | button | | | small |

**Actions.**
| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Check the judge again (judge_status_ui) | `GET /v1/judge/status` | none | replaces the status text | no error wrapper in app.py: a failed request becomes a generic error |

**Client-side logic to reimplement.**
- `available` true → `✅ **The judge is ready:** <model>` + (` at \`<server>\`` if `server` else ` (in the API's process)`) + ".".
- `available` false → `⚠ **The judge is not available here.** <reason>` then a blank line, then `<how>`.

**Navigation.** Arriving from 🧠 Train "⚖ Evaluate this result" (evaluate_result_ui). That button carries the completed engine id. Both
storyteller pickers (benchmark and calibration) get that `engine_id` as their value, with fresh choices. The note becomes
`**Storyteller:** the engine just trained, <label>. Press **Play and judge** (or *Play, judge and fit*) to evaluate it.`
Nothing starts automatically. Guard: if the id is not among the picker's choices, an error appears ("Engine <id> is no longer offered in ⚖ Evaluate (…). Nothing was chosen in its place.") and the view does not change.

---

### ⚖ Evaluate › Judge the engine's stories
<a id="evaluate-benchmark"></a>`view: evaluate-benchmark` (accordion "Judge the engine's stories", open by default)

**Purpose.** Plays N stories with a storyteller and has the judge rate each one (absolute). In pairwise mode it plays the same seeds with and without
"version B" settings and has the judge pick the better story of each pair, judged in both orders.

**On open.**
- Storyteller choices are built in this order:
  1. `("Current settings, random storytellers", "")`
  2. every `GET /v1/exports/engines` → `engines[]` as `(label, engine_id)`, newest first
  3. every `GET /v1/records/engines` → `engines[]` whose `id` is not already listed, as `(label + (" · resumable" if resumable), id)`

  A 404 from the records (no records yet) gives an empty list, and other errors are logged. In app.py this list is built when the
  studio process starts, not on each page load. A rebuild should fetch it when the view opens.
- The card area stays empty until the picker changes.

Fallback labels when an engine has no `label`:
- `/v1/exports/engines`: `"<created_at[:16]> · <'loaded engine' if loaded else 'run'>[ · N generation(s)][ · fitness X.X] · <id[:8]>"`
- `/v1/records/engines`: `"<created_at[:16]> · <source>[ · gen N][ · fitness X.X][ · resumable] · <id[:8]>"`

**Components.**
| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Stories | slider(1..60,step 1) | | 10 | API: 1..60 (`evaluation.MAX_STORIES`) |
| First seed | number (integer) | | 0 | API: ≥0. Stories use seed, seed+1, … |
| Judgements at once | slider(1..8,step 1) | | 4 | `workers` |
| Storyteller | dropdown | see On open | `""` (current settings, random storytellers) | info: "Finished runs (newest first), then other recorded engines." |
| 🏷 About this storyteller: its result card | accordion (closed) containing markdown | `GET /v1/engines/{engine_id}/card` | empty | |
| Compare with version B (pairwise A/B on the same seeds, each pair judged in both orders) | checkbox | | false | |
| Version B's settings | yaml-editor | | `# version B: dotted.path: value\nclimax.enabled: false\n` | must be a mapping `{dotted.path: value}` |
| Play and judge | button (primary) | | | |
| Summary | markdown | job result | empty | |
| Stories | table (columns depend on mode, see below) | job result | empty | read-only, wrapped |

**Actions.**
| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Storyteller changes (engine_card_ui) | `GET /v1/engines/{engine_id}/card` (not sent for `""` or null) | picker value | card markdown, see Shared. `""` → `*The current settings with random storytellers: no trained engine, no card.*`. null → `*Choose an engine to see its card.*` | 404 → `*No card for engine \`<id>\`.*` |
| Play and judge (judge_benchmark_ui) | `POST /v1/jobs/judge-benchmark` (202, returns a job `{id,…}`), then poll `GET /v1/jobs/{id}` every 0.5 s until `status` is `finished` (use `result`) or `failed` (show `error`) | `{stories:int(Stories), seed:int(First seed or 0), engine_id: Storyteller or null, pairwise: bool, variants: (pairwise ? parsed YAML : {}), workers:int}` | Summary and table, see Client-side logic. Progress bar from `job.progress.generation` / `job.progress.total`, labelled `Judged <generation>/<total>`. For pairwise jobs, progress appears only once both sets of stories have been played. With an `engine_id` and not pairwise, the server adds the result to the engine's card `evaluations`. The card shown is NOT re-fetched by app.py, so a rebuild should re-fetch it. | Client: when pairwise, the YAML must parse and be a mapping, else "Version B's settings must be a mapping of dotted.path: value." (or a YAML syntax error). API: 400 when pairwise and `variants` is empty. 404 for an engine that is neither a finished run nor recorded. 422 for out-of-range values. A failed job's `error` is e.g. 503-like "language model judge is not available". Only one judge job (benchmark or calibration) runs at a time from this studio (shared queue "judge"), so disable both start buttons while one runs. |

**Client-side logic to reimplement.**
- **Absolute** result fields: `engagement` number|null, `judged` int, `stories` int, `criteria` {name: mean 1–10}, `rows[]` {`seed`, `engagement`, `scores`, `fitness`, `turns`, `won`, `verdict`}.
  - Summary: `### Engagement <engagement:.2f>/10 over <judged> judged stories`, then on a new line the criteria as `"<name> <value:.1f>"` joined " · ". If `engagement` is null: `### No judgement could be read.`
  - Table columns: `seed`, `engagement`, `fitness` (rounded to 1 decimal), `turns`, `won`, `verdict`.
- **Pairwise** result fields: `pairs`, `a_wins`, `b_wins`, `ties`, `b_win_rate` number|null, `wilson_95` [low, high] (of B's rate. [0,1] when nothing was decided), `criteria` {name: B's win rate | null}, `rows[]` {`seed`, `winner` "A"|"B"|"tie"|null, `criteria`, `reason`, `agreed`, `fitness_a`, `fitness_b`}.
  - The client reports A's view: `a_rate = 1 - b_win_rate`, interval `1-high .. 1-low`, per criterion `1 - value` (null criteria skipped).
  - Summary: `### Current settings (A) vs version B: A won <a_wins>, B won <b_wins>, <ties> tie(s)`, then on a new line
    `A's win rate **<a_rate:.0%>** of decided pairs (95% interval <1-high:.0%>-<1-low:.0%>).` or "No pair was decided.",
    then `A's win rate per criterion: <name> <1-v:.0%> · …` when any.
  - Table columns: `seed`, `winner`, `agreed`, `fitness A` (round 1), `fitness B` (round 1), `reason`.
- Version B YAML parsing happens only when the checkbox is on. Otherwise `variants` is sent as `{}`.

**Navigation.** None.

---

### ⚖ Evaluate › Fit the fitness to the judge
<a id="evaluate-calibration"></a>`view: evaluate-calibration` (accordion "Fit the fitness to the judge", closed by default)

**Purpose.** Plays and judges stories, then fits judged engagement against the fitness terms. It suggests a multiplier per term
(`fitness.term_weights`), which can be saved now or edited and saved by hand.

**On open.** `GET /v1/fitness/term-weights` → `{weights: {term: number}}`, shown as YAML in the editor (empty mapping → empty text). In app.py this
is fetched once when the studio process starts. A rebuild should fetch it when the view opens. Storyteller choices are the same list as the
benchmark's (see above).

**Components.**
| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Intro | markdown | static: "Plays and judges stories, fits the judged engagement on the fitness terms and suggests a multiplier per term (`fitness.term_weights`). Trust it as far as its agreement on stories left out goes." | | |
| Stories | slider(10..60,step 1) | | 30 | API: 10..60 |
| First seed | number (integer) | | 0 | ≥0 |
| Storyteller | dropdown | same as benchmark | `""` | no card shown for this picker |
| Save the suggested weights at once | checkbox | | false | `apply` |
| Play, judge and fit | button (primary) | | | |
| Summary | markdown | job result | empty | |
| fitness.term_weights (term: multiplier) | yaml-editor | `GET /v1/fitness/term-weights` → `weights`, or the calibration's `term_weights` | current weights | mapping term → number ≥ 0 |
| Save term weights | button (primary) | | | |
| Reload | button | | | |
| Term weights status | markdown | `PUT` response `message` | empty | |

**Actions.**
| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Play, judge and fit (calibrate_fitness_ui) | `POST /v1/jobs/calibrate-fitness` (202, job), then poll `GET /v1/jobs/{id}` every 0.5 s | `{stories:int, seed:int(or 0), engine_id: value or null, apply: bool}` (`alpha` and `workers` are not sent, so the API defaults apply: alpha auto, workers 4) | Result fields: `n` int, `spearman` number, `cv_spearman` number\|null, `alpha` number, `applied` bool, `term_weights` {term: number} (also `terms`, `yaml`, `stories`, `engagement`, unused). Summary (see below). The editor is replaced with `term_weights` as YAML. Progress shows `Judged <generation>/<total>`. With an `engine_id`, the server adds a `calibration` evaluation to the engine's card. | 404 unknown engine. 422 range. The job fails with "Calibrating needs at least 10 judged stories." or "Only N judgements could be read; at least 10 are needed." or judge unavailable. Shares the one-at-a-time "judge" queue with the benchmark. |
| Save term weights (save_term_weights_ui) | `PUT /v1/fitness/term-weights` | `{weights: parsed editor YAML (empty → {})}` | Response `{weights, message}`. The editor is replaced with `weights` as YAML (the server rounds to 3 decimals and sorts). The status shows `message`. | Client: the YAML must be a mapping, else "Term weights must be a mapping of fitness term: multiplier.". API 400: non-numeric or negative values. `{}` removes every multiplier. |
| Reload (load_term_weights_ui) | `GET /v1/fitness/term-weights` | | editor ← `weights` as YAML | |

**Client-side logic to reimplement.**
- Calibration summary: `### Fitted on <n> judged stories`, then on a new line `Agreement with the judge (Spearman): <spearman:+.2f> on the stories fitted, ` +
  (`**<cv_spearman:+.2f> on stories left out**` or "not cross-validated (too few stories)") + ` (ridge strength <alpha:g>). ` +
  ("The weights were saved as `fitness.term_weights`." if `applied` else "Apply them below, or edit them first.").
- YAML output uses block style, keys in insertion order. An empty mapping shows empty text.

**Navigation.** Train's "⚖ Evaluate this result" sets this Storyteller too (see header view).

---

### ⚖ Evaluate › Recorded stories: judge one, or compare two
<a id="evaluate-stories"></a>`view: evaluate-stories` (accordion, closed by default)

**Purpose.** Pick any recorded story (training previews, games played, imports) to see its turns and transcript. The judge can rate the story, or
say which of two stories is the better game.

**On open.** `GET /v1/records/stories` → `stories[]` for both pickers (a 404 means no records, so the list is empty). Fields: `id`, `created_at`, `origin`,
`kind`, `turns`, `status`, `mission_status`. In app.py this is fetched when the studio process starts. A rebuild should fetch it when the view opens.

**Cross-view refreshes.**
- "Recorded story" is re-fetched after any training (or continue or retrain) finishes in 🧠 Train, and on either "↻ Refresh" button (Train → From a recorded engine, and Export → Recorded files).
- "Compare with another recorded story" is re-fetched only on those two Refresh buttons.

**Components.**
| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Recorded story | dropdown | stories, label below | none | info: "Every story recorded: training previews, games played, imports." |
| Judge this story (Gemma 4) | button | | | |
| Compare with another recorded story | dropdown | same list | none | |
| Which is the better game? | button | | | |
| Judgement | markdown | judge / compare result | empty | shared output for both buttons |
| Story view | markdown | `GET /v1/records/stories/{story_id}` | empty | |

**Actions.**
| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Recorded story changes (story_view_ui) | `GET /v1/records/stories/{story_id}` | | Story view (see below). Empty selection → empty. | 404 unknown story |
| Judge this story (judge_story_ui) | `POST /v1/records/stories/{story_id}/judge` (synchronous, can take long) | none | Judgement markdown with title "This story" | Client: no story → "Choose a recorded story first.". API 503 judge unavailable, 404 story. |
| Which is the better game? (compare_stories_ui) | `POST /v1/judge/compare` (synchronous) | `{a:{story_id: Recorded story}, b:{story_id: Compare with}}` | Comparison markdown with a="the first story", b="the second story" | Client: both chosen and different, else "Choose two different recorded stories.". API 400 unknown story, 503 no judge. |

**Client-side logic to reimplement.**
- Story label: `"<created_at[:16]> · <origin> <kind> · <turns> turns · <status>[ · <mission_status>] · <id[:8]>"`.
- Story view, from these fields: `origin`, `kind`, `status`, `turns`, `mission_status`, `fitness`, `start_location`, `seed`, `engine_id`, `run_id`, `transcript`,
  `turn_records[]` {`turn`, `action`, `location_before`, `location_after`, `succeeded`, `outcome`}:
  `**<origin> <kind>** · <status> · <turns> turns · <mission_status || "no mission"> · fitness <(fitness||0):.1f>`, then on a new line
  `Start: <start_location> · seed: <seed> · engine: \`<engine_id>\` · run: \`<run_id>\``. Then a table `| turn | action | where | success | outcome |`
  with where = `location_before → location_after`, success = ✓/✗, and `|` in the outcome replaced by `/`. Then
  `<details><summary>Transcript</summary>` holding a code block of `transcript`.
- Judgement (judgement_markdown), from `engagement` number|null, `spread` number?, `scores` {criterion: number}, `best_moment`, `weakest_moment`, `verdict`:
  if `engagement` is null: `### <title>\nThe judge's reply could not be read; try again.`. Otherwise these paragraphs:
  `### <title>: engagement <e:.1f>/10[ (spread <s:.1f>)]`, `"<name> **<v:.1f>**"` joined " · ",
  `**Best moment:** <best_moment||"-">`, `**Weakest moment:** <weakest_moment||"-">`, `> <verdict>`.
- Comparison (comparison_markdown), from `winner` "A"|"B"|"tie"|null, `criteria` {name: "A"|"B"|"tie"}, `reason`, `agreed` bool:
  `### The better game: <A→a label | B→b label | tie→"neither: a tie" | null→"unreadable">`, then
  "both readings agreed" or "the two readings did not agree (a tie)", then the criteria as `"<name> → <side label>"` joined " · ", then `> <reason>`.

**Navigation.** None.

---

## 📦 Export

Top tab `id="export"` (open directly with `?section=export`). One main screen (the export form) plus the accordion "Recorded engines, runs and worlds:
their files". A static line also points to ⚙ Advanced → 🔌 Integration ("Which systems this API has runtimes for, and the world as
a UNP receives it"). That line is text, not a link. (The module header comment of app.py lists `/v1/world/catalogue` for Export, but the
Export view does not call it. It is only called by the global status line.)

### 📦 Export › Export an engine
<a id="export"></a>`view: export`

**Purpose.** Download an engine to run offline. The **ANE AI** platforms (`windows-x64`, `linux-x64`, `macos-arm64`, `macos-x64`,
`unity`) are made only from finished runs (complete engines), one download per chosen run. The **Python** version can also be made from a
generation's champion, the best remembered storyteller, or the storyteller of the game open in ▶ Play.

**On open.**
- `GET /v1/exports/platforms` → `{platforms[], code_version, protocol}`. Each item: `platform` str, `available` bool, `reason` str? (why not),
  `description` str?, `code_version`?, `built_at`?, `includes` [str]? (unity only). Fallback when the request fails: only Python.
  In app.py this is fetched when the studio process starts.
- `GET /v1/exports/engines` → `engines[]` with `engine_id`, `label`, `name`, `created_at`, `source`, `score`, `run_id`, `resumable`,
  `generations`, `loaded` (newest first). This fills "Finished runs". The first one is selected on build. On every page load the form logic
  (export_form_ui) runs again: it re-fetches this list, keeps the chosen ids that still exist, and otherwise selects the newest.
- On page load, the result cards of the chosen engines (engine_cards_ui) are fetched with one `GET /v1/engines/{engine_id}/card` per chosen id.

**Cross-view refreshes.**
- A training that finishes in 🧠 Train sets the "Generation" choices: the `learning.history[].generation` values as strings, with the last one selected. It also
  sets the run id used by the "A generation of this run" source (the session's last `run_id`).
- The "Finished runs" choices are re-fetched only by: a platform or source change, page load, Train's "📦 Export this result", or a rename in Train.
  They are not refreshed right after a training finishes.
- A rename in Train refreshes the labels.

**Components.**
| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Intro | markdown | static text (ANE AI vs Python version, finished runs, README in each download) | | |
| Runs on | dropdown | `platforms[]` → `(label, platform)` | `"python"` | info: "A standalone program for that system (the ANE AI, no Python needed), the Unity package, or Python." Unavailable platforms stay selectable. |
| What to export | radio | hard-coded: "The chosen finished run(s)", "A generation of this run", "Best remembered storyteller", "This game's storyteller" | "The chosen finished run(s)" | visible only when platform is `python`. info: "The Python version only; the ANE AI is made from finished runs." |
| Generation (for a generation's champion) | dropdown | generation numbers from the last training in this browser session | empty, hidden | visible only when platform is `python` and source is "A generation of this run" |
| Finished runs | multiselect | `GET /v1/exports/engines` → `(label, engine_id)` | newest one | visible when an ANE AI platform is chosen or source is "The chosen finished run(s)". info: "Newest first: the latest is chosen. Choose several to export each." |
| Engine note | markdown | computed | | see logic |
| 🏷 The chosen engines' result cards | accordion (closed) containing markdown | one card per chosen id | | cards joined by `---` |
| Include the learning memory (keep training offline) | checkbox | | true | `include_memory` |
| Name | text | | empty, placeholder "narrative-engine" | folder or package name |
| Export | button (primary) | | | disabled when the form uses finished runs and there are none |
| Downloads (ZIP, or .unitypackage for Unity) | file-download (several) | downloaded artifacts | | |
| Export status | markdown | computed from responses | | |

**Actions.**
| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Runs on or What to export changes, and page load (export_form_ui) | `GET /v1/exports/engines` | | Applies the form rules below. The Finished runs choices are replaced, keeping the chosen ids that still exist, otherwise the newest. | If the request fails: no choices, note = NO_COMPLETE_ENGINE, Export disabled (when from runs). |
| Finished runs changes (engine_cards_ui) | `GET /v1/engines/{engine_id}/card` for each chosen id | | Cards (Shared rendering) joined by `\n\n---\n\n`. None chosen → `*Choose a finished run to see its card.*` | Per-card 404 → `*No card for engine \`<id>\`.*`. The engine note count is NOT recomputed on this change (quirk). |
| Export, finished runs: an ANE AI platform, or source "The chosen finished run(s)" (export_engine_ui) | for each chosen id, in order: `POST /v1/exports`, then `GET <download_url>` (= `GET /v1/artifacts/{artifact_id}`) | `{source:"engine", engine_id, platform, include_memory: bool, name}`. name = Name if a single run is chosen. With several runs and a Name: `"<Name>-<engine_id[:8]>"`. With no Name: null. | Each file is offered for download, using the filename from `Content-Disposition`. The status shows one paragraph per export (see logic). | Client: no run chosen → error "No finished run yet. An export is made from a finished run …". API 409: runtime missing or built from other engine code (message includes the platform's `reason`), or no complete engine (e.g. a generation champion id). 400: other failures. A failure stops the loop: earlier files in the same click are not shown. |
| Export, Python with another source (export_engine_ui) | `POST /v1/exports`, then `GET <download_url>` | `{source: "run"\|"memory"\|"game", run_id: session's last training run id or null (null = latest run), generation: int(Generation) only for "A generation of this run" and when one is chosen (else null = the run's best), session_id: the ▶ Play game's session id or null, include_memory, name: Name or null, platform: "python"}` | one file, plus a status paragraph | API 400: "No evolution run to export yet: run a simulation first (or export from memory).", "The run has generations 0-N, not G.", "The AI has not learned anything yet: run a simulation first." (memory), "That game session no longer exists." (game; also with no session id — unclear: exact behaviour when `session_id` is null) |

`POST /v1/exports` response fields used: `download_url`, `name`, `source`, `score` number|null, `engine_id` str|null, `platform`,
`world.locations`, `world.npcs`, `world.missions` (also returned: `genome`, `code_version`, `world.items`, `world.events`, `world.stats`).

**Client-side logic to reimplement.**
- Platform label: `python` → "Python (3.10+ with pyyaml and numpy)", `unity` → "Unity (.unitypackage: library and runtimes)", otherwise
  "Standalone <platform>". Add " (not available here)" when `available` is false. The `reason` is not shown in the picker. It reaches the user
  only as the 409 message on export. (A rebuild may show `reason` as a hint.)
- Source to API `source` (hard-coded): "The chosen finished run(s)"→`engine`, "A generation of this run"→`run`,
  "Best remembered storyteller"→`memory`, "This game's storyteller"→`game`.
- Form rules: `ane_ai = platform ∈ {windows-x64, linux-x64, macos-arm64, macos-x64, unity}`. `from_runs = ane_ai || source == "The chosen finished run(s)"`.
  - What to export is visible when `!ane_ai`. Generation is visible when `!ane_ai && source == "A generation of this run"`. Finished runs are visible when `from_runs`.
  - Note: if `from_runs` and no finished runs, NO_COMPLETE_ENGINE = "**No finished run yet.** An export is made from a finished run (its engine: world, storyteller, training state): run a training (*Evolve storytellers & start game*), or load an engine, then export it here.".
    Otherwise, if `from_runs`: `"<count> finished run(s) chosen: each is exported with its own world, storyteller, training state and tag vectors."`,
    where count = the number of kept chosen ids, or 1 (the newest) when none are kept. Otherwise the note is empty.
  - Export is enabled when `!from_runs || there is at least one finished run`.
- Finished-run label fallback (no `label`): `"<created_at[:16] with T→space> · <'loaded engine' if loaded else 'run'>[ · N generation(s)][ · fitness X.X] · <engine_id[:8]>"`.
- Status paragraph per export: `Exported **<name>**: <source> (fitness <score:.1f | "not benchmarked">[, engine \`<engine_id[:8]>\`], the training objective). World: <locations> locations, <npcs> NPCs, <missions> missions. `, followed by text that depends on `platform`:
  - python: "Unzip it and run `python run.py play` or `python run.py serve`."
  - unity: "Import it in Unity (Assets > Import Package > Custom Package), then Tools > ANE AI > Add the ANE AI to the scene."
  - other: "Unzip it on <platform> and start `ane-ai serve` (no Python needed); the README explains how a UNP launches, drives and stops it."

  Paragraphs are separated by blank lines.

**Navigation.**
- In: 🧠 Train "📦 Export this result" (export_result_ui) carries the completed `engine_id` and keeps the current platform. The tab opens with:
  - What to export = "The chosen finished run(s)", visible only for `python`
  - Generation hidden
  - Finished runs re-fetched and set to exactly `[engine_id]`
  - note `**Chosen:** the engine just trained, <label>, with its own world, storyteller, training state and tag vectors.`
  - Export enabled

  Nothing is exported until the user clicks. Guard: if the id is not among the finished runs, an error appears ("Engine <id> is no longer offered in 📦 Export …") and nothing changes.
- The "This game's storyteller" source uses the session id of the game open in ▶ Play (none → null).

---

### 📦 Export › Recorded engines, runs and worlds: their files
<a id="export-records"></a>`view: export-records` (accordion, closed by default)

**Purpose.** Browse the records: download any recorded engine's model file (`.ane.zip`) and its world as YAML, list the recorded runs with each run's settings and
generations, and download any recorded world as a configuration that can be uploaded again.

**On open.** All of these are fetched when the studio process starts in app.py. A rebuild should fetch them when the view opens. A 404 (no records yet) gives empty lists.
- `GET /v1/records/engines` → `engines[]` {`id`, `label`, `created_at`, `source`, `generation`, `score`, `resumable`, …} → "Recorded engine".
- `GET /v1/records/runs` → `runs[]` {`id`, `created_at`, `user_id`, `origin`, `kind`, `status`, `generations_run`, `best_score`, `best_engine_id`} → table and "A run" picker.
- `GET /v1/records/worlds` → `worlds[]` {`id`, `created_at`, `locations`, `npcs`, `missions`} → "Recorded world".

**Cross-view refreshes.**
- After any training (run, continue, retrain) finishes in 🧠 Train: the Recorded engine choices and the runs table are re-fetched (the A run and Recorded world pickers are not).
- After a records import in ⚙ Advanced → 🗂 Records: the same.
- On either "↻ Refresh" (here, or Train → From a recorded engine): everything here is re-fetched, together with Evaluate's two story pickers, Train's recorded engine picker
  and the records status in ⚙ Advanced.
- A rename in Train refreshes the Recorded engine labels.

**Components.**
| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Recorded engine | dropdown | `GET /v1/records/engines` → `(label[+" · resumable"], id)` | none | info: "Any run's best, a generation's champion, a loaded, continued or retrained engine." |
| Download its model file and world | button | | | |
| ↻ Refresh | button | | | |
| Engine model (.ane.zip) | file-download | | | |
| World and tuning (YAML) | file-download | | | the engine's world |
| Recorded runs | table(run, created, user, origin, kind, status, generations, best score, engine) | `GET /v1/records/runs` | | read-only |
| A run: its settings and generations | dropdown | `GET /v1/records/runs` → `(label, id)` | none | |
| Run detail | markdown | `GET /v1/records/runs/{run_id}` | empty | |
| Generations | table(generation, best, mean, worst, validation, diversity, mutation_step, improved) | same | empty | read-only |
| Recorded world | dropdown | `GET /v1/records/worlds` → `(label, id)` | none | |
| Download it as a configuration to upload | button | | | |
| World and tuning (YAML) | file-download | | | the chosen world |

**Actions.**
| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Download its model file and world (download_recorded_ui) | 1) `GET /v1/records/engines/{engine_id}` → `world_id`. 2) `GET /v1/records/engines/{engine_id}/model` (file `engine-<id>.ane.zip`. A generation's champion is assembled from its run). 3) `GET /v1/records/worlds/{world_id}?format=yaml` (file `world-<world_id>.yaml`) | Recorded engine | the two files fill the two download slots | Client: none chosen → "Choose a recorded engine first.". API 404: unknown engine, or the model file is missing or unreadable. 400: model file changed since recorded. |
| ↻ Refresh (refresh_records_ui, then the pickers) | `GET /v1/records/engines`, `GET /v1/records/stories`, `GET /v1/records/runs`, `GET /v1/records` (status for ⚙ Advanced), then `GET /v1/records/stories`, `GET /v1/records/runs`, `GET /v1/records/worlds` | | refills Recorded engine (here and in Train), the Evaluate story pickers, the runs table, A run, Recorded world | |
| A run changes (run_detail_ui) | `GET /v1/records/runs/{run_id}` | | Detail markdown and generations table (see below). Empty selection clears both. | 404 |
| Download it as a configuration to upload (download_world_ui) | `GET /v1/records/worlds/{world_id}?format=yaml` | Recorded world | a YAML file `world-<id>.yaml` that `POST /v1/config/upload` accepts | Client: none chosen → "Choose a recorded world first.". 404. |

**Client-side logic to reimplement.**
- Runs table rows: `run`=`id[:8]`, `created`=`created_at[:16]`, `user`=`user_id||"test"`, `origin`, `kind`, `status`,
  `generations`=`generations_run`, `best score`=`best_score`, `engine`=`(best_engine_id||"")[:8]`.
- Run label: `"<created_at[:16]> · <origin> <kind> · <status>[ · best <best_score:.1f>] · <id[:8]>"`.
- World label: `"<created_at[:16]> · <locations> locations, <npcs> NPCs, <missions> missions · <id[:8]>"`.
- Recorded engine label fallback (no `label`): `"<created_at[:16]> · <source>[ · gen N][ · fitness X.X][ · resumable] · <id[:8]>"`.
- Run detail, from these fields: `origin`, `kind`, `status`, `best_score`, `generations_run`, `world_id`, `engines[]`, `stories[]`, `error`, `settings`, `parameters`, `generations[]`:
  `**<origin> <kind>** · <status> · best fitness <best_score:.1f | "-"> · <generations_run||0> generation(s) · world \`<world_id>\` · <len(engines)> engine(s), <len(stories)> story(ies)`.
  Add `\n\n**Error:** <error>` when `error` is set. Then, if either is non-empty, a ```yaml block of `{settings, parameters}` (only the keys present).
- Generations table: one row per `generations[]` with the 8 columns above.

**Navigation.** None (the files' other uses are in 🧠 Train → From a recorded engine, and ⚙ Advanced → 🗂 Records).

## ⚙ Advanced

Destination `id=advanced` (`#dest-advanced`). Lead text: "Technical settings: the simulation, every weight, the engine's logic, the app and its background jobs. The story's own rules are in 🌍 World → 📜 Story rules." Six sub-tabs, in order: Simulation, Weights, Engine logic, App & jobs, 🗂 Records, 🔌 Integration. Default open: Simulation.

### ⚙ Advanced › Simulation
<a id="advanced-simulation"></a>`view: advanced-simulation`

**Purpose.** The saved training defaults and game settings, replacing/resetting the whole runtime configuration, every hyperparameter as YAML, and the active merged configuration.

**On open.** (fetched once at build; a rebuild should fetch on open)
- `GET /v1/settings` → `pop_size`, `generations`, `episodes_per_genome`, `max_steps`, `deterministic`, `seed`, `initial_health`, `max_health`, `initial_coins`, `max_actions` → the matching fields.
- `GET /v1/hyperparameters` → `yaml` (string) → the YAML editor.
- Nothing else; the active configuration is fetched on "Show".

**Components.**
| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| "### Complete configuration file" note | markdown | static: "Upload one combined YAML file containing all sections from the four files in `config/`. It is checked completely before it can replace the built-in runtime configuration." | — | |
| Combined YAML configuration | file-upload(.yaml, .yml) | — | empty | |
| Validate & use uploaded config | button (primary) | — | — | |
| Use built-in config | button | — | — | |
| "#### Training defaults" note | markdown | static (what a run uses for what it does not set; Train sets its own; warm start / full-length are `evolution.warm_start` / `evolution.full_length_stories` in Engine logic) | — | |
| Default population (storytellers) | number (integer) | `pop_size` | — | min 1; hint "evolution.pop_size" |
| Default generations | number (integer) | `generations` | — | min 1; "evolution.generations" |
| Default stories per storyteller | number (integer) | `episodes_per_genome` | — | min 1; "evolution.episodes_per_genome" |
| Default steps per simulation | number (integer) | `max_steps` | — | min 1; "evolution.max_steps" |
| Deterministic runs by default | checkbox | `deterministic` | — | info "A deterministic run repeats the same evolution with the same configuration and seed (reproducibility.deterministic)." |
| Default seed | number (integer) | `seed` | — | may be empty (null); "reproducibility.seed" |
| "#### Game" heading | markdown | — | — | |
| Starting health | number (integer) | `initial_health` | — | min 1 |
| Maximum health | number (integer) | `max_health` | — | min 1; may be empty (null = keep stored); info "Healing never raises health above this value." |
| Starting coins | number (integer) | `initial_coins` | — | min 0 |
| Story length (game action limit) | number (integer) | `max_actions` | — | min 100; info "Turns before a story ends (narrative_end.max_actions, at least 100); full-length training stories are this long." |
| Save settings | button (primary) | — | — | |
| config status | markdown | messages of the 3 actions above | empty | |
| **Accordion "Advanced · all hyperparameters"** (closed) | | | | note "Edit every hyperparameter as YAML. The complete structure and value types are validated before saving." |
| hyperparameters.yaml | yaml-editor | `GET /v1/hyperparameters` `.yaml` | — | |
| Validate & save all hyperparameters | button | — | — | |
| advanced status | markdown | message | empty | |
| **Accordion "The active configuration (read only)"** (closed) | | | | note "Every configuration file merged (or the uploaded configuration): what new games and runs use now." |
| Show | button | — | — | |
| config source | markdown | see logic | empty | |
| active configuration | yaml-editor (read-only) | `GET /v1/config` `.config` | empty | |

**Actions.**
| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Validate & use uploaded config (`upload_config`) | `POST /v1/config/upload` | multipart field **`file`** = the chosen file | `settings` (same shape as `GET /v1/settings`) → the 10 fields; `"✅ " + message` → config status | error "Choose a YAML configuration file first." when no file; 400 wrong extension ("Allowed file types: .yaml, .yml.") or invalid config; 413 "Uploaded file is too large." (limit = app parameter `max_config_upload_bytes`). Hyperparameters YAML, Weights, Engine logic are **not** refreshed. |
| Use built-in config (`use_builtin_config`) | `POST /v1/config/reset` | — | `settings` → the 10 fields; `message` ("Built-in configuration restored.") → config status | — (no confirmation) |
| Save settings (`save_settings`) | `PUT /v1/settings` | `{pop_size, generations, episodes_per_genome, max_steps, initial_health, initial_coins, max_actions, deterministic, seed, max_health}` from the fields; numbers sent as integers; `seed`, `max_health` may be `null` | `message` → config status (response `settings` ignored) | client guard: any field other than `seed`/`max_health` empty → error "Every simulation setting needs a numeric value."; 422 bounds (`ge` 1/0/100) → "field: msg"; 400 from the store |
| Validate & save all hyperparameters (`save_all_hyperparameters`) | `PUT /v1/hyperparameters` | `{yaml: <editor text>}` | `settings` → the 10 fields; `yaml` → editor (normalised text); `message` → advanced status | 400 structure/type errors (message) |
| Show (`active_config_ui`) | `GET /v1/config` | — | `using_override` (bool) → "Active now: **an uploaded configuration**." / "Active now: **the configuration files**."; `config` (object) → dumped as YAML (keys in order, unicode) → read-only editor | — |

**Client-side logic to reimplement.**
- Field order for the settings payload is fixed: `SETTING_FIELDS = [pop_size, generations, episodes_per_genome, max_steps, initial_health, initial_coins, max_actions, deterministic, seed, max_health]`.
- YAML dump of `config`: block style, keys unsorted, empty value → empty text.

**Navigation.** Text references only (🧠 Train; Engine logic).

### ⚙ Advanced › Weights
<a id="advanced-weights"></a>`view: advanced-weights`

**Purpose.** Edit every numeric tuning parameter (weights, bonuses, penalties, thresholds, chances, rates) through two views — **Basic** (chosen gameplay controls) and **Advanced** (every parameter, by section) — of **one draft**, saved together with conflict detection.

**On open.**
- Build: `GET /v1/weights` → builds the fields (the set of parameters and the Basic layout are fixed from this response):
  - `values` `{path: number}`
  - `parameters[]`: `path`, `value`, `section` (title), `label`, `explanation` (string|null), `unit` (string|null), `type` (`integer`|`float`), `minimum`, `maximum`, `default` (number|null), `source[]`, `described` (bool), `requires` (string|null: the true/false setting it needs), `inactive` (string|null: why it has no effect now)
  - `basic[]`: `key`, `title`, `summary`, `controls[]`: `path`, `effect` (text: what raising/lowering it does)
- Every page load, and Discard: `GET /v1/weights` → `values` fills both views and becomes the saved baseline (`reload_weights_ui`).

**Components.**
| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Intro | markdown | static (Basic vs Advanced, units, defaults, bounds, whole numbers, only changed values saved) | — | |
| View (`#weights-view`) | radio ["Basic", "Advanced"] | hard-coded | "Basic" | info "Basic: common gameplay controls. Advanced: every parameter." Switching only shows/hides the two field groups; values unchanged. |
| Draft status (`#weights-draft-status`) | html | computed (see logic) | "✓ No unsaved changes…" | above both views; carries `class="draft-status …" data-dirty="0|1" data-advanced="<n>"` (drives the browser leave prompt) |
| Basic view (`#weights-basic`) | per `basic[]` area: markdown `#### {title}\n{summary}`, then its controls 3 per row | `basic` | visible | each control = number field (below), id `weight-basic-<path>` |
| Advanced view (`#weights-advanced`) | one closed accordion per section (in order of first appearance in `parameters`), title `"{section} ({described count} of {n} described)"`; fields 3 per row | `parameters` grouped by `section` | hidden | each field id `weight-advanced-<path>` |
| Weight field (both views) | number | `value` | — | label = `label`; integer → whole numbers only (precision 0), float → decimal; `minimum`/`maximum` enforced when present; `inactive` → greyed (class `weight-inactive`); help text below (see logic) |
| Save changes (`#weights-save`) | button (primary) | — | — | |
| Discard changes (reload the saved values) (`#weights-discard`) | button | — | — | no confirmation |
| Message (`#weights-message`) | markdown | save message / reload note | empty | |

`elem id` rule: `weight-{basic|advanced}-` + path with every char not in `[A-Za-z0-9_-]` replaced by `-`.

**Actions.**
| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Edit a Basic field | none | — | same value copied into its Advanced twin (same `path`); draft status recomputed | — |
| Edit an Advanced field | none | — | copied into its Basic twin if the path is in Basic; draft status recomputed | — |
| View radio | none | — | show Basic xor Advanced | — |
| Save changes (`save_weights`) | `PUT /v1/weights` | `{values: {path: draftValue} for changed paths only, expected: {path: savedValue} for those changed paths whose saved value is not null}` | response `values` → new saved baseline; `message` → Message; draft status recomputed (normally "no unsaved changes") | no changes → no request, Message "Nothing to save: every value is the saved one."; a changed field left empty → error "Nothing was saved: give a number to {label, label…}." (no request); **409** (someone changed a parameter since load): error text "Changed since it was loaded, so nothing was saved; reload to see the newer values: `path` is now X (it was Y when loaded); …" — draft and baseline kept (user resolves by Discard = reload, then re-edit); **400** unknown path / not finite / fraction for integer / out of bounds — nothing saved, each problem named; draft kept. |
| Discard changes (`reload_weights_ui`) | `GET /v1/weights` | — | `values` → every field in both views (a path missing from `values` leaves its field as is), → baseline; draft status recomputed; Message = note (below) or empty | — |
| Page load | same as Discard | | | |

**Client-side logic to reimplement.**
- **One draft.** Keep one map `draft[path]`; both views edit it. Baseline `saved[path]` = last `values` from GET/PUT.
- **Changes** (`weight_changes`): paths in `parameters` order where not `sameNumber(draft, saved)`; `sameNumber`: both null → same; one null → different; else `Number(a) === Number(b)`.
- **Draft status markup** (`weights_draft_markup`):
  - no changes → `✓ No unsaved changes: Basic and Advanced show the saved values.` (`data-dirty="0"`)
  - else (`class="draft-status dirty weights-draft" data-dirty="1"`, amber):
    - `● Unsaved changes ({n}), one draft for both views; Save saves them all.`
    - if any change is on a Basic path: `In Basic and Advanced ({k}):` + list
    - if any change is on a path not in Basic: amber-bordered box `⚠ Advanced only, not shown in Basic ({m}):` + list (so a Basic user sees hidden changes)
    - list item: `{label} <code>{path}</code>: {old} → {new}`; number shown with `%g` (floats) / as is (ints); null → `(empty)`. HTML-escape all text.
- **Help text, Advanced field** (`weight_help`):
  - not `described` → `No description yet ({whole|decimal} number, as written). Parameter: {path}`
  - else `[{inactive note}][Also in Basic → {area title}. ]{explanation} {facts}. Parameter: {path}`
- **Help text, Basic field** (`basic_weight_field`): `[{inactive note}]{control.effect} {facts}. Parameter: {path}`
- facts (`_weight_facts`), joined ` · `: `Unit: {unit}` (if any); `engine default {default}` (if not null); bounds: `from {min} to {max}` | `at least {min}` | `at most {max}`; `whole number` (integer) or `decimal number`. Numbers formatted `%g`.
- inactive note: `Inactive now: {requires} is off (it takes effect when that is on). ` when `inactive` is set.
- Area of a Basic path = the `title` of the `basic[]` area containing it.
- **Reload note**: paths in `values` that are not among the built fields → Message `The configuration now has parameters this page does not show ({paths, sorted}): reload the page.`
- **Leave prompt**: the draft status element has `data-dirty="1"` while unsaved → browser `beforeunload` prompt. No in-app prompt when switching tabs; keep the draft alive across tab switches.
- After a save, `parameters[].inactive` in the response is not used (greying is from build time) (unclear: a rebuild may refresh it).

**Navigation.** —

### ⚙ Advanced › Engine logic
<a id="advanced-logic"></a>`view: advanced-logic`

**Purpose.** Edit the engine's logic parameters, one section of `hyperparameters.yaml` at a time, as YAML (comments kept).

**On open.**
- `GET /v1/logic` → `sections[]`: `section` (name), `description` (text) → dropdown choices and description map.
- `GET /v1/logic/evolution` → `parameters` (YAML text starting with `evolution:`) → editor. (Initial section is hard-coded `evolution`.)

**Components.**
| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Intro | markdown | static: logic runs in backend scripts; change how it behaves through parameters; each section checked (same settings, same value types) before saving; new simulations and games use it at once | — | |
| Logic section | dropdown | `sections[].section` | `evolution` | |
| Description | markdown | `description` of the chosen section | evolution's | |
| Parameters | yaml-editor | `GET /v1/logic/{section}` `.parameters` | — | |
| Validate & save parameters | button (primary) | — | — | |
| Reload | button | — | — | |
| logic status | markdown | message | empty | |

**Actions.**
| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Choose a section (`load_logic_section`) | `GET /v1/logic/{section}` | section name | `parameters` → editor; description from the cached map (no request) | 404 unknown section |
| Reload (`load_logic_section`) | `GET /v1/logic/{section}` | current section | `parameters` → editor (unsaved edits lost, no confirmation) | 404 |
| Validate & save parameters (`save_logic_section`) | `PUT /v1/logic/{section}` | `{parameters: <editor text>}` | `message` → logic status | 400 (other keys / value types / YAML error) |

**Client-side logic to reimplement.** No dirty tracking: changing section or Reload discards unsaved text silently. Saving does not refresh Simulation's hyperparameters YAML or Weights.

**Navigation.** —

### ⚙ Advanced › App & jobs
<a id="advanced-app-jobs"></a>`view: advanced-app-jobs`

**Purpose.** The studio's app parameters (limits, queue, launch), applied on restart; and the list of background jobs.

**On open.**
- `GET /v1/app-settings` → `settings` `{key: value}` (current) and `defaults` `{key: value}` (types). One field per key of `settings`, in its order.
- Jobs: nothing until Refresh (accordion closed).

**Components.**
| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| **Accordion "App parameters"** (open) | | | | note "Saved in config/ui_settings.yml. Upload limits are in bytes. A training's crossover is evolution.crossover_rate (⚙ Advanced → Weights → Training)." |
| one field per `settings` key | checkbox (bool value) / text (string value) / number (other; integer when `defaults[key]` is an integer) | `settings[key]` | — | label = key with `_` → space, Title Case (e.g. `max_web_sessions` → "Max Web Sessions"). Current keys: `max_web_sessions`, `max_simulation_work`, `max_config_upload_bytes`, `max_engine_upload_bytes`, `candidate_limit`, `generation_limit`, `simulation_step_limit`, `queue_concurrency`, `queue_max_size`, `server_host` (text), `server_port`, `share` (checkbox). Server rules: ints > 0, strings non-empty, `server_port` ≤ 65535. |
| Save app parameters | button (primary) | — | — | |
| parameters status | markdown | message | empty | |
| **Accordion "Background jobs"** (closed) | | | | note "Evolutions, continued trainings and judge runs started from here or by any client of the API, newest first." |
| Refresh | button | — | — | |
| Jobs | table(job, kind, what, status, created, progress, error) | `GET /v1/jobs` | empty | read-only, wrapped |

**Actions.**
| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Save app parameters (`save_app_parameters`) | `PUT /v1/app-settings` | `{settings: {key: value for every key}}` | `message` + " ANE_UI_HOST, ANE_UI_PORT and ANE_UI_SHARE environment variables override launch settings." → status. The API message is "App parameters saved. Restart the studio and the API to apply them." (+ an "Ignored …" note when retired keys were sent) | 400 "Unknown app parameters: …" or "Invalid value for {key}." / "Server port must be between 1 and 65535." |
| Refresh (`jobs_table_ui`) | `GET /v1/jobs` | — | `jobs[]` (newest first, no `result`) → rows | — |

**Client-side logic to reimplement.** Job row: `job` = `id[:8]`; `kind`; `what` = `description`; `status` (`queued|running|finished|failed`); `created` = `created_at[:19]`; `progress` = `"{progress.generation}/{progress.total}"` when `progress` is set, else empty; `error` or empty. No auto-refresh.

**Navigation.** —

### ⚙ Advanced › 🗂 Records
<a id="advanced-records"></a>`view: advanced-records` (tab `id=advanced-records`)

**Purpose.** Whether recording is on, and importing earlier engines/games from the server's import folder into the records. The record **lists and details** (engines, runs + generations, worlds, stories + story view) are rendered in 📦 Export → "Recorded engines, runs and worlds: their files", ⚖ Evaluate → "Recorded stories", 🧠 Train → "From a recorded engine" and ▶ Play → "Another engine"; they share one refresh with this view, described here.

**On open.**
- Records status: `GET /v1/records` (at build, then on every records refresh) → `enabled` (bool), `reason` (string|null), `readable` (bool).
- Every page load: `GET /v1/records/import` → `folder` (string path), `files[]` (names) → import note (`import_folder_ui`).
- Cross-view: the status (and all record lists) are refreshed after a training finishes / a recorded engine is continued or retrained (🧠 Train), after ↻ Refresh in 🧠 Train → From a recorded engine or 📦 Export → Recorded engines, and after an import here (`refresh_records_ui`).

**Components.**
| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Records status | markdown | `GET /v1/records` | — | text below |
| **Accordion "Import earlier engines and games (the server's import folder)"** (open) | | | | note: engine models, offline exports, older engine ZIPs and story configurations are read, completed (tag vectors, score, run, preview story) and recorded; files that are not engines/games or have an invalid world stay where they are |
| Import note | markdown | `GET /v1/records/import` | — | text below |
| Move the files into the records (else copy) | checkbox | — | false | |
| Play a preview story of each new engine | checkbox | — | true | |
| Import | button (primary) | — | — | one at a time (concurrency 1) |
| import status | markdown | job report / progress | empty | |

**Actions.**
| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Import (`import_records_ui`) | `POST /v1/jobs/records-import` (202) then poll `GET /v1/jobs/{job_id}` every 0.5 s | `{move: bool, preview: bool}` | while running: progress indicator text = `progress.label` (e.g. "[IMPORT] 3/7: 2 new engines, 1 linked, 0 not imported") or "Importing…", drawn at a fixed 50%; finished: `result` → import status (below); then refresh all record lists + records status (`refresh_records_ui`), then the import note again (`GET /v1/records/import`) | 400 "There is nothing to import in {folder}." when empty; job `failed` → its `error` |

Record lists refresh (`refresh_records_ui`, no button in this tab): `GET /v1/records/engines` (→ the three "Recorded engine" pickers), `GET /v1/records/stories` (→ ⚖ Evaluate's story picker), `GET /v1/records/runs` (→ 📦 Export's runs table), `GET /v1/records` (→ status here). The ↻ Refresh buttons additionally re-fetch `GET /v1/records/stories` (compare picker), `GET /v1/records/runs` (run picker), `GET /v1/records/worlds` (world picker).

Record details (in 📦 Export / ⚖ Evaluate; summarised for completeness):
| Trigger | Request | On success |
|---|---|---|
| Choose a run (`run_detail_ui`) | `GET /v1/records/runs/{run_id}` | header markdown `**{origin} {kind}** · {status} · best fitness {best_score %.1f or -} · {generations_run or 0} generation(s) · world `{world_id}` · {len(engines)} engine(s), {len(stories)} story(ies)` + `**Error:** {error}` when set + a YAML block of `{settings, parameters}` (those present); table Generations(generation, best, mean, worst, validation, diversity, mutation_step, improved) from `generations[]`. Empty choice → clears both. |
| Choose a story (`story_view_ui`) | `GET /v1/records/stories/{story_id}` | markdown: `**{origin} {kind}** · {status} · {turns} turns · {mission_status or "no mission"} · fitness {fitness or 0 %.1f}`, line `Start: {start_location} · seed: {seed} · engine: `{engine_id}` · run: `{run_id}``, table (turn, action, where = `location_before → location_after`, success ✓/✗ from `succeeded`, outcome with `|` → `/`) from `turn_records[]`, collapsible "Transcript" with `transcript` in a code block. |
| Download a recorded world (`download_world_ui`) | `GET /v1/records/worlds/{world_id}?format=yaml` (file `world-{id}.yaml`, uploadable to Simulation) | file link. Error "Choose a recorded world first." |
| Download a recorded engine (`download_recorded_ui`) | `GET /v1/records/engines/{engine_id}` (`world_id`), `GET /v1/records/engines/{engine_id}/model` (`.ane.zip`), `GET /v1/records/worlds/{world_id}?format=yaml` | two file links. Error "Choose a recorded engine first." |

**Client-side logic to reimplement.**
- Records status text: `enabled` → "Recording is on: every run, engine, world and story generated here, in the API or in the terminal is kept in the records." Else `"Recording is off: {reason or 'ANE_RECORDS=off'}."` + (" The records already there are shown." if `readable`) + " Finished runs are kept with the saves, so they can still be exported." Request error → "The records cannot be reached: {message}".
- Import note: `**{n}** engine or game file(s) in the server's import folder `{basename(folder)}/`` + (if files: `": " + first 12 names joined ", " + " …" when more`; else ` (set `ANE_ARTIFACT_DIR`, or put `.ane.zip`, engine ZIPs and configurations in `artifacts/`).`).
- Import report (job `result`): `Imported **{engines}** new engine(s), linked {linked}, {runs} run(s), {previews} preview stor(ies) from {files} file(s)` + (if `failed[]`: `; **{len} not imported**: ` + first 5 `"{file} ({error})"` joined `; `) + `.`
- Picker labels (value = `id`):
  - engine: `label` (from API) + ` · resumable` if `resumable`; fallback `"{created_at[:16]} · {source}[ · gen {generation}][ · fitness {score %.1f}][ · resumable] · {id[:8]}"`.
  - story: `"{created_at[:16]} · {origin} {kind} · {turns} turns · {status}[ · {mission_status}] · {id[:8]}"`.
  - run: `"{created_at[:16]} · {origin} {kind} · {status}[ · best {best_score %.1f}] · {id[:8]}"`.
  - world: `"{created_at[:16]} · {locations} locations, {npcs} NPCs, {missions} missions · {id[:8]}"`.
- Runs table columns: run=`id[:8]`, created=`created_at[:16]`, user=`user_id` or "test", origin, kind, status, generations=`generations_run`, best score=`best_score`, engine=`best_engine_id[:8]`.
- List request failures → empty lists (404 silent).

**Navigation.** Text only (records used in 🧠 Train, ⚖ Evaluate, 📦 Export).

### ⚙ Advanced › 🔌 Integration
<a id="advanced-integration"></a>`view: advanced-integration` (tab `id=advanced-integration`)

**Purpose.** UNP diagnostics: drive the game open in ▶ Play the way a UNP drives the exported ANE AI (set the player, report events, read `ane.session_state/1`); resend a session report to the UNP; list the runtimes the API can export and download the world catalogue (`ane.world/1`).

**On open.**
- Session note (no request) from the shared play session token (the game open in ▶ Play). Recomputed after: a game is started in ▶ Play (New game, recorded engine, continue a save game), an engine file is loaded there, 🧠 Train's "▶ Play this result", the banner's ⚙ Advanced button, any menu choice.
- Report profile choices: `GET /v1/variables/profiles` → `profiles[]`, `default` (at build).
- Platforms table: `GET /v1/exports/platforms` (at build) → `platforms[]`: `platform`, `available` (bool), `reason?`, `description?`, `code_version?`, `built_at?`.

**Components.**
| Component | Kind | Value / choices from | Default | Constraints & notes |
|---|---|---|---|---|
| Lead | markdown | static (diagnostics for a UNP; `docs/UNP.md`) | — | |
| **Accordion "The game in ▶ Play as a UNP sees it: the player, events, parameters"** (open) | | | | |
| Session note | markdown | play session token | — | no token: "**No game is open in ▶ Play.** Start one there; these diagnostics act on it."; else "Acting on the game open in **▶ Play**: session `{token}`." |
| Explanation | markdown | static (set the player, tell what happened, read parameters `ane.session_state/1`; ▶ Play's *Save game* / *Continue a save game*) | — | |
| Set the player (stat_points, stat_changes, inventory, add_items, remove_items, coins, health, max_health, variables) | json-editor (4 lines) | — | `{"coins": 20}` | |
| What happened in the UNP's game | dropdown | hard-coded `SYNC_EVENTS` keys | `item_gained` | |
| Its action, item, NPC, location or amount | text | — | empty | |
| Set the player | button | — | — | one at a time |
| Tell the session | button | — | — | one at a time |
| Show the session's parameters (ane.session_state/1) | button | — | — | |
| Session parameters | json-editor (read-only) | `GET /v1/sessions/{id}/state` | empty | pretty-printed, 2-space indent, unicode |
| **Accordion "Session reports sent to the UNP"** (closed) | | | | note: a report goes to the UNP when a session ends (`session_report` in engine logic); ▶ Play's save game shows whether it was sent |
| Player profile (save game) | dropdown, custom values allowed | `profiles[]` | `default` | |
| Send its latest report to the UNP again | button | — | — | |
| save game status | markdown | result text | empty | |
| **Accordion "Where the ANE AI runs, and the world it sends"** (closed) | | | | note: standalone runtimes (built by CI) and the world as the ANE AI sends it (`GET /world`) |
| platforms | table(runs on, available, details) | `GET /v1/exports/platforms` | — | read-only |
| Refresh | button | — | — | |
| Download the world as a UNP receives it (JSON) | button | — | — | |
| World catalogue (ane.world/1) | file-download | — | empty | |
| catalogue status | markdown | — | empty | |

Hard-coded `SYNC_EVENTS` (event type → body field it names): `action_done`→`action`, `item_gained`→`item`, `item_lost`→`item`, `npc_removed`→`npc`, `location_visited`→`location`, `coins_changed`→`amount`, `health_changed`→`amount`.

**Actions.**
| Trigger | Request | Body / params | On success | Errors & guards |
|---|---|---|---|---|
| Set the player (`update_player_ui`) | `PATCH /v1/sessions/{session_id}/player`, then `GET /v1/sessions/{session_id}/journal`, `GET /v1/sessions/{session_id}/transcript`, then `GET /v1/sessions/{session_id}/state` | JSON object from the editor | PlayState response (+ journal, transcript) re-renders ▶ Play's game screen and story (see Play doc; `last_turn` = "Player set by the UNP: {fields}."); then state → Session parameters | no game → error "Start a game first in ▶ Play (New game, another engine, or continue a save game)."; not JSON → "The player's state is not JSON: {err}"; not an object → `Write a JSON object, e.g. {"coins": 20, "add_items": ["Ancient Map"]}.`; 404 "Play session not found."; 400 bad fields. On error the parameters are not re-fetched. |
| Tell the session (`sync_session_ui`) | `POST /v1/sessions/{session_id}/sync`, then journal + transcript, then `GET /v1/sessions/{session_id}/state` | `{type: <event>, <field>: <value>}` (one event; `amount` as integer) | as above (`last_turn` = "From the UNP's game: …") | no game → same error; no event → "Choose what happened."; amount not an integer → "Give the amount as a whole number (negative for a loss)."; empty name → "Name the {field}."; 404/400 |
| Show the session's parameters (`session_parameters_ui`) | `GET /v1/sessions/{session_id}/state` | — | whole JSON → Session parameters | no game → same error; 404 |
| Send its latest report again (`resend_report_ui`) | `GET /v1/progression?profile=` then `POST /v1/progression/reports/{report_id}/send?profile=` | profile (omitted when empty); `report_id` = `sessions[last].report_id` (the latest) | delivery `{sent, status, error, at}` → "Sent to the UNP." or "Not sent: {error or 'HTTP ' + status}." | no sessions → error "This save game has no session report yet."; 400 (unknown report/profile) |
| Refresh (`export_platforms_table`) | `GET /v1/exports/platforms` | — | rows (logic below) | request error → empty table (logged) |
| Download the world (`world_catalogue_ui`) | `GET /v1/world/catalogue` (saved as `world-catalogue.json`) | — | file → download; status "The world as a UNP receives it (`{format}`): {n} locations, {n} npcs, {n} items, {n} events, {n} missions." | — |

Related actions living in ▶ Play (documented there; listed because the Integration text refers to them): *💾 Save game* → `GET /v1/sessions/{session_id}/savegame` (file `savegame-{token[:8]}.json`, `ane.savegame/1`); *Continue a save game* (upload .json) → `POST /v1/sessions/load` `{savegame, session_id}` → `restored` (`exact`|`parameters`), `note`; *Show the save game* → `GET /v1/progression?profile=` (playthroughs, goals_achieved, sessions table incl. "sent to UNP" from `delivery.sent`/`delivery.error`).

**Client-side logic to reimplement.**
- The session acted on is the shared ▶ Play token (one per browser tab); Integration and Play must share it, and Integration's actions update Play's screen.
- Platform row: `runs on` = `platform`; `available` = "yes"/"no"; `details` = `reason` || `description` || (`code_version` ? `"runtime {code_version}, built {built_at[:10]}"` : "the engine as Python code").
- Catalogue counts: lengths of `locations`, `npcs`, `items`, `events`, `missions`.

**Navigation.** Text references to ▶ Play (no buttons).

<!-- Endpoints used by these views (verified in api.py): GET /v1/world/catalogue, GET /v1/exports/engines, GET /v1/records, GET /v1/config, POST /v1/config/upload, POST /v1/config/reset, GET/PUT /v1/settings, GET/PUT /v1/hyperparameters, GET/PUT /v1/weights, GET /v1/logic, GET/PUT /v1/logic/{section}, GET/PUT /v1/app-settings, GET /v1/jobs, GET /v1/jobs/{job_id}, GET /v1/records/import, POST /v1/jobs/records-import, GET /v1/records/engines, GET /v1/records/engines/{engine_id}, GET /v1/records/engines/{engine_id}/model, GET /v1/records/stories, GET /v1/records/stories/{story_id}, GET /v1/records/runs, GET /v1/records/runs/{run_id}, GET /v1/records/worlds, GET /v1/records/worlds/{world_id}, GET /v1/variables/profiles, GET /v1/sessions/{session_id}/state, PATCH /v1/sessions/{session_id}/player, POST /v1/sessions/{session_id}/sync, GET /v1/sessions/{session_id}/journal, GET /v1/sessions/{session_id}/transcript, GET /v1/sessions/{session_id}/savegame, POST /v1/sessions/load, GET /v1/progression, POST /v1/progression/reports/{report_id}/send, GET /v1/exports/platforms, GET /v1/reference -->

# Known gaps in today's studio

These came up while writing this guide, from reading `app.py` against `api.py`. A new frontend should
**not** copy them; the API already supports the right behaviour.

**Stale choices.** Some lists are still fetched once, when the studio's server starts. Fetch them
when the view opens, and again after anything that changes them:

- the Simulation form, the hyperparameters YAML, the app parameters and the first Engine logic section
- the export platforms, the record lists, and the storyteller and engine pickers in Evaluate and Export
- the engine card after a judge benchmark or a calibration adds an evaluation

(The World tab's dropdowns of names now refresh when opened: see [🌍 World](#-world).)

**Cross-view refreshes missing in ⚙ Advanced.** Uploading or resetting the configuration, or saving
settings, Weights or Engine logic, does not reload the hyperparameters YAML; saving every
hyperparameter does not reload Weights or Engine logic.

**Smaller items.** Import progress is drawn at a fixed 50% (the job reports `{generation: 0, total: 1,
label}`); the Integration tab's game note is not refreshed when a game ends; the pairwise benchmark's
summary always calls side A "current settings", even with an engine chosen.

**Fixed in the studio** (the views above describe the new behaviour): a new entry or mission could
replace one with the same name or key; an empty mission key; mission weight 0 saved as 1; a one-goal
mission's step text and rewards dropped; a deleted mission recreated by Save; mission delete reloading
the missions YAML over unsaved edits; Scheduling's Save and YAML editor offered for places and people;
dialogue variants collapsed into one string; a cleared Story rules number kept silently; Grow dropping
entries renamed in the YAML; Map saving to a place that is gone; the behaviour tables' unsaved edits
lost (one table's save reloaded both) and unguarded; Reset profile without a confirmation; at most 8
choice buttons; the game lost on reload; "sent to the UNP" never shown; two handlers that lost the
API's error message (`import_folder_ui`, `refresh_records_ui`). The World dropdowns of names went stale
too.

**Unused endpoints.** The studio saves an action's repetition inside the action's own `PUT`, so
`GET|PUT /v1/entities/{kind}/{name}/actions/{index}/repetition` remain only in unused helpers.

**Doc mismatch.** The `GET /v1/behaviours` docstring says the rules are edited with
`PUT /v1/logic/{section}`. The studio uses `PUT /v1/behaviours/rules/{group}`. Both work.

# Endpoint index

Every `/v1` route of `api.py`, in the order the API declares them. **Views** are the views that
call the route. "studio: …" names the endpoint the studio uses instead, and the route itself is
for other clients. `tests/test_studio_coverage.py` (`FrontendGuideTests`) fails when this table and
the API disagree, or when this guide names a request the API does not have.

| Method | Path | Views | What it does |
| --- | --- | --- | --- |
| `GET` | `/v1/config` | [advanced-integration](#advanced-integration), [advanced-simulation](#advanced-simulation) |  |
| `POST` | `/v1/config/upload` | [advanced-integration](#advanced-integration), [advanced-simulation](#advanced-simulation), [export-records](#export-records) |  |
| `POST` | `/v1/config/reset` | [advanced-integration](#advanced-integration), [advanced-simulation](#advanced-simulation) |  |
| `GET` | `/v1/settings` | [advanced-simulation](#advanced-simulation), [global-loads](#global-loads) |  |
| `PUT` | `/v1/settings` | [advanced-integration](#advanced-integration), [advanced-simulation](#advanced-simulation) |  |
| `GET` | `/v1/variables` | — (studio: the studio edits the definitions as YAML: /v1/documents/variables) | Variable definitions and the stored values of the configured player profile. |
| `PUT` | `/v1/variables` | — (studio: /v1/documents/variables) |  |
| `GET` | `/v1/variables/profiles` | [advanced-integration](#advanced-integration), [checks](#checks), [global-loads](#global-loads), [play-setup](#play-setup), [story-rules.variables](#story-rules.variables) |  |
| `GET` | `/v1/variables/values` | — (studio: /v1/documents/stored_values) |  |
| `PUT` | `/v1/variables/values` | — (studio: /v1/documents/stored_values) | Overwrite stored values of a profile, e.g. |
| `POST` | `/v1/variables/reset` | — (studio: /v1/variables/reset/yaml (the reset profile as YAML)) | Forget a profile's stored values and playthroughs (a new player). |
| `GET` | `/v1/messages` | — (studio: /v1/documents/messages) | Every story message, its default, and the placeholders it may use. |
| `PUT` | `/v1/messages` | — (studio: /v1/documents/messages) |  |
| `GET` | `/v1/hyperparameters` | [advanced-simulation](#advanced-simulation), [global-loads](#global-loads) |  |
| `PUT` | `/v1/hyperparameters` | [advanced-integration](#advanced-integration), [advanced-simulation](#advanced-simulation) |  |
| `GET` | `/v1/weights` | [advanced-weights](#advanced-weights), [global-loads](#global-loads) | The tuning parameters (every weight, bonus, penalty, threshold, chance, rate, ... |
| `PUT` | `/v1/weights` | [advanced-integration](#advanced-integration), [advanced-weights](#advanced-weights) | Change tuning parameters (paths left out keep their value). |
| `GET` | `/v1/entities/{kind}` | [entries](#entries), [entries-details](#entries-details), [entries-fields](#entries-fields), [global-loads](#global-loads), [grow](#grow), [map](#map), [story-rules](#story-rules) | The entries of a kind (``fields=names``: |
| `GET` | `/v1/entities/{kind}/{name}` | [entries](#entries), [entries-fields](#entries-fields) |  |
| `PUT` | `/v1/entities/{kind}/{name}` | [entries-fields](#entries-fields) |  |
| `GET` | `/v1/entities/{kind}/{name}/references` | [entries-fields](#entries-fields) | What refers to the entry and would break if it were deleted (missions, actions, conditions, dialogue,… |
| `DELETE` | `/v1/entities/{kind}/{name}` | [entries-fields](#entries-fields) | Delete an entry (at least one location, NPC and item remains: |
| `GET` | `/v1/entities/{kind}/{name}/actions` | [entries](#entries), [entries-actions](#entries-actions), [entries-fields](#entries-fields) | The entry's actions and outcomes, exactly as stored. |
| `PUT` | `/v1/entities/{kind}/{name}/actions` | — (studio: /v1/entities/{kind}/{name}/actions/yaml) | Replace the entry's actions after checking fields, references and variables. |
| `GET` | `/v1/entities/{kind}/{name}/policy` | [entries](#entries), [entries-scheduling](#entries-scheduling) | An event's start rules or an item's uniqueness, as stored. |
| `PUT` | `/v1/entities/{kind}/{name}/policy` | [entries-scheduling](#entries-scheduling) | Replace an event's start rules or an item's uniqueness after validation. |
| `GET` | `/v1/logic` | [advanced-integration](#advanced-integration), [advanced-logic](#advanced-logic), [global-loads](#global-loads) | The parameter sections that control the engine's logic, with what each controls. |
| `GET` | `/v1/logic/{section}` | [advanced-logic](#advanced-logic), [global-loads](#global-loads) |  |
| `PUT` | `/v1/logic/{section}` | [advanced-integration](#advanced-integration), [advanced-logic](#advanced-logic) | Change the engine's logic through its parameters (same keys and value types required). |
| `GET` | `/v1/behaviours` | [behaviour](#behaviour), [entries](#entries), [entries-fields](#entries-fields) | What people and places do, by their tags (`npc_behaviour`, `location_behaviour`): |
| `POST` | `/v1/exports` | [export](#export) | Export a trained engine (code, world, storyteller, tag vectors, learning memory) as a ZIP that runs… |
| `GET` | `/v1/exports/platforms` | [advanced-integration](#advanced-integration), [export](#export), [global-loads](#global-loads) | What an ANE AI export can run on: |
| `GET` | `/v1/exports/engines` | [advanced-integration](#advanced-integration), [evaluate-benchmark](#evaluate-benchmark), [export](#export), [global-loads](#global-loads), [grow](#grow), [header](#header), [play-setup](#play-setup), [play-state-reference](#play-state-reference), [train-engines](#train-engines), [train-run](#train-run) | The complete engines an ANE AI (a standalone or Unity export) can be made from, newest first: |
| `GET` | `/v1/runs` | — (studio: the studio keeps its run; earlier runs: /v1/records/runs) | Recent evolution runs that can be exported (best or any generation), each with its engine's id, name (None: |
| `GET` | `/v1/engines/{engine_id}/card` | [evaluate-benchmark](#evaluate-benchmark), [export](#export), [play-setup](#play-setup), [play-state-reference](#play-state-reference), [train-engines](#train-engines), [train-run](#train-run) | A completed engine's result card, recorded or kept with the saves (recording off): |
| `PUT` | `/v1/engines/{engine_id}/name` | [play-state-reference](#play-state-reference), [train-engines](#train-engines), [train-run](#train-run) | Name a completed (or recorded) engine, or remove its name. |
| `GET` | `/v1/learning` | [train-learning-memory](#train-learning-memory), [train-run](#train-run) | The storytellers the evolutionary AI remembers from earlier runs. |
| `DELETE` | `/v1/learning` | [train-learning-memory](#train-learning-memory) | Forget what the AI learned; the next run starts from random storytellers. |
| `GET` | `/v1/stats` | [checks](#checks), [entries](#entries), [global-loads](#global-loads), [play-stat-points](#play-stat-points), [story-rules.stats](#story-rules.stats) | The story's character stat definitions, budget, mode, tag checks and damage-reduction stat. |
| `PUT` | `/v1/stats` | — (studio: /v1/documents/stats) |  |
| `POST` | `/v1/stats/allocate` | [play-stat-points](#play-stat-points), [story-rules.stats](#story-rules.stats) | Check the player's own points, or preview a random/balanced split, with the 1-100 values. |
| `GET` | `/v1/missions` | [missions](#missions) |  |
| `GET` | `/v1/missions/playable` | [missions](#missions), [play-quests-savegame](#play-quests-savegame), [play-setup](#play-setup) | The missions a player may choose when starting a game (``mission`` in the play requests): |
| `GET` | `/v1/lint/failures` | [checks](#checks) | The failures this world leaves as dead ends: |
| `PUT` | `/v1/missions` | [missions](#missions) |  |
| `GET` | `/v1/rules` | [story-rules](#story-rules) | Story rules: |
| `PUT` | `/v1/rules` | [story-rules](#story-rules) | Change any story rules; fields left out keep their current value. |
| `POST` | `/v1/simulations` | — (studio: /v1/jobs/simulations (with progress)) |  |
| `POST` | `/v1/engines/load` | [play-other-engine](#play-other-engine), [play-state-reference](#play-state-reference) | Start a game with a saved engine: |
| `GET` | `/v1/sessions/{session_id}` | [play-state-reference](#play-state-reference), [train-engines](#train-engines) |  |
| `POST` | `/v1/sessions/{session_id}/actions` | [play-game](#play-game), [play-state-reference](#play-state-reference) | Take one of the actions the last response offered; anything else is refused (400). |
| `POST` | `/v1/sessions/{session_id}/end` | [play-game](#play-game), [play-state-reference](#play-state-reference) | End the session where it stands (the player stops). |
| `GET` | `/v1/simulations/work` | — (studio: /v1/simulations/resolve (its `work`, with every other resolved setting)) | What a run would ask for before it starts, as the run itself counts it (each value left out: |
| `POST` | `/v1/simulations/resolve` | [train-records](#train-records), [train-run](#train-run) | What a run with these settings would execute, before it starts: |
| `GET` | `/v1/sessions/{session_id}/state` | [advanced-integration](#advanced-integration) | The session's parameters (ane.session_state/1), as the exported ANE AI sends them to a UNP: |
| `PATCH` | `/v1/sessions/{session_id}/player` | [advanced-integration](#advanced-integration), [play-state-reference](#play-state-reference) | Set the player's state as a UNP does: |
| `POST` | `/v1/sessions/{session_id}/sync` | [advanced-integration](#advanced-integration), [play-state-reference](#play-state-reference) | What happened in the UNP's game: |
| `GET` | `/v1/sessions/{session_id}/savegame` | [advanced-integration](#advanced-integration), [play-game](#play-game) | The session's save game (ane.savegame/1): |
| `POST` | `/v1/sessions/load` | [advanced-integration](#advanced-integration), [play-setup](#play-setup), [play-state-reference](#play-state-reference) | Continue a save game as a new play session: |
| `GET` | `/v1/world/catalogue` | [advanced-integration](#advanced-integration), [global-loads](#global-loads), [grow](#grow), [header](#header) | The world as the exported ANE AI sends it to a UNP (``GET /world``, ane.world/1): |
| `GET` | `/v1/sessions/{session_id}/report` | — (studio: the play state carries the report: /v1/sessions/{id}/end) | The report of an ended session (as sent to the UNP); 409 while the session goes on. |
| `GET` | `/v1/progression` | [advanced-integration](#advanced-integration), [play-quests-savegame](#play-quests-savegame) | A player's save game: |
| `POST` | `/v1/progression/reports/{report_id}/send` | [advanced-integration](#advanced-integration) | Send a kept session report to the UNP again (session_report.url or ANE_UNP_URL): |
| `GET` | `/v1/records` | [advanced-integration](#advanced-integration), [advanced-records](#advanced-records), [export-records](#export-records), [global-loads](#global-loads), [grow](#grow), [header](#header), [train-records](#train-records), [train-run](#train-run) | Where the records database is, whether recording is on, and why not (``reason``). |
| `GET` | `/v1/records/runs` | [advanced-integration](#advanced-integration), [advanced-records](#advanced-records), [export-records](#export-records), [train-records](#train-records), [train-run](#train-run) | The recorded runs, newest first, each with the user it is for (``user_id``: |
| `GET` | `/v1/records/runs/{run_id}` | [advanced-integration](#advanced-integration), [advanced-records](#advanced-records), [export-records](#export-records) | A run with every setting, every generation, its engines, stories and files. |
| `GET` | `/v1/records/worlds` | [advanced-integration](#advanced-integration), [advanced-records](#advanced-records), [export-records](#export-records), [train-records](#train-records) |  |
| `GET` | `/v1/records/worlds/{world_id}` | [advanced-integration](#advanced-integration), [advanced-records](#advanced-records), [export-records](#export-records) | A recorded world with all its tuning: |
| `GET` | `/v1/records/engines` | [advanced-integration](#advanced-integration), [advanced-records](#advanced-records), [evaluate-benchmark](#evaluate-benchmark), [export-records](#export-records), [global-loads](#global-loads), [play-other-engine](#play-other-engine), [play-setup](#play-setup), [play-state-reference](#play-state-reference), [train-engines](#train-engines), [train-records](#train-records), [train-run](#train-run) | The recorded engines, newest first, each with its name (None: |
| `GET` | `/v1/records/engines/{engine_id}` | [advanced-integration](#advanced-integration), [advanced-records](#advanced-records), [export-records](#export-records) |  |
| `GET` | `/v1/records/engines/{engine_id}/model` | [advanced-integration](#advanced-integration), [advanced-records](#advanced-records), [export-records](#export-records) | The engine model file (for a generation's champion, assembled from its run). |
| `POST` | `/v1/engines/{engine_id}/play` | [play-setup](#play-setup), [play-state-reference](#play-state-reference), [train-run](#train-run) | Play the world a finished run was trained on, with its storyteller (``latest``: |
| `GET` | `/v1/sessions/{session_id}/journal` | [advanced-integration](#advanced-integration), [play-game](#play-game), [play-journey](#play-journey), [play-setup](#play-setup), [train-run](#train-run) | A play session turn by turn, as a player follows it: |
| `GET` | `/v1/sessions/{session_id}/transcript` | [advanced-integration](#advanced-integration), [play-game](#play-game), [play-setup](#play-setup), [play-story](#play-story), [train-run](#train-run) | The story of a play session so far, as text. |
| `POST` | `/v1/records/engines/{engine_id}/play` | [play-other-engine](#play-other-engine), [play-state-reference](#play-state-reference) | Start a game with a recorded engine (its own world, tuning and storyteller). |
| `POST` | `/v1/records/engines/{engine_id}/continue` | — (studio: /v1/jobs/engines/{engine_id}/continue) | Continue the engine's training exactly where it stopped; records the run and a new engine. |
| `POST` | `/v1/records/engines/{engine_id}/retrain` | — (studio: /v1/jobs/engines/{engine_id}/retrain) | A new run that starts from the engine's storyteller and memory, on its world. |
| `GET` | `/v1/records/stories` | [advanced-integration](#advanced-integration), [advanced-records](#advanced-records), [evaluate-stories](#evaluate-stories), [export-records](#export-records), [train-records](#train-records), [train-run](#train-run) |  |
| `GET` | `/v1/records/stories/{story_id}` | [advanced-integration](#advanced-integration), [advanced-records](#advanced-records), [evaluate-stories](#evaluate-stories) | A story with its parameters, result, transcript and every turn. |
| `POST` | `/v1/records/stories/{story_id}/judge` | [evaluate-stories](#evaluate-stories) | Gemma 4 as a game critic: |
| `POST` | `/v1/sessions/{session_id}/judge` | [play-judge](#play-judge) | The LLM judge on the game played so far in this session (as for a recorded story). |
| `GET` | `/v1/conditions/reference` | [entries](#entries) | What a condition can say in the current world, for a condition builder. |
| `POST` | `/v1/conditions/validate` | [entries](#entries) | Check a condition as every save checks it (nothing is saved). |
| `GET` | `/v1/reference` | [advanced-integration](#advanced-integration), [entries](#entries), [global-loads](#global-loads), [story-rules](#story-rules) | What a client needs to build an authoring UI: |
| `GET` | `/v1/app-settings` | [advanced-app-jobs](#advanced-app-jobs), [checks](#checks), [global-loads](#global-loads) | The studio's app parameters (config/ui_settings.yml): |
| `PUT` | `/v1/app-settings` | [advanced-app-jobs](#advanced-app-jobs), [advanced-integration](#advanced-integration) | Validate and save app parameters; they apply when the studio and the API restart. |
| `GET` | `/v1/documents` | — (studio: /v1/reference lists the documents) |  |
| `GET` | `/v1/documents/{name}` | [map](#map), [missions](#missions), [story-rules.messages](#story-rules.messages), [story-rules.stats](#story-rules.stats), [story-rules.variables](#story-rules.variables) | A configuration document as YAML text, comments included. |
| `PUT` | `/v1/documents/{name}` | [map](#map), [missions](#missions), [story-rules.messages](#story-rules.messages), [story-rules.stats](#story-rules.stats), [story-rules.variables](#story-rules.variables) | Validate and save a configuration document (comments kept); nothing is written when it is invalid. |
| `POST` | `/v1/variables/reset/yaml` | [story-rules.variables](#story-rules.variables) | Forget a profile's stored values (a new player); returns the stored-values document. |
| `GET` | `/v1/entities/{kind}/{name}/actions/yaml` | [entries](#entries), [entries-actions](#entries-actions) |  |
| `PUT` | `/v1/entities/{kind}/{name}/actions/yaml` | [entries-actions](#entries-actions) | Replace the entry's actions from YAML text (validated like PUT .../actions). |
| `GET` | `/v1/entities/{kind}/{name}/yaml` | [entries](#entries), [entries-yaml](#entries-yaml) | Every field of the entry as YAML, as stored: |
| `PUT` | `/v1/entities/{kind}/{name}/yaml` | [entries-yaml](#entries-yaml) | Replace every field of the entry from YAML (the entry keeps its name). |
| `GET` | `/v1/entities/{kind}/{name}/policy/yaml` | [entries](#entries), [entries-scheduling](#entries-scheduling) |  |
| `PUT` | `/v1/entities/{kind}/{name}/policy/yaml` | [entries-scheduling](#entries-scheduling) |  |
| `GET` | `/v1/entities/{kind}/{name}/actions/{index}/repetition` | — (studio: unused: the studio saves `repeat` and `retry_on_failure` with the action's own PUT) | How often the entry's action number ``index`` (from 0) is offered, and whether a failure uses it up. |
| `PUT` | `/v1/entities/{kind}/{name}/actions/{index}/repetition` | — (studio: unused: the studio saves `repeat` and `retry_on_failure` with the action's own PUT) |  |
| `GET` | `/v1/entities/{kind}/{name}/actions/{index}` | [entries](#entries), [entries-actions](#entries-actions) | One action of an entry (``index``: |
| `PUT` | `/v1/entities/{kind}/{name}/actions/{index}` | [entries-actions](#entries-actions) | Replace one action (``index`` = the number of actions: |
| `DELETE` | `/v1/entities/{kind}/{name}/actions/{index}` | [entries-actions](#entries-actions) | Delete one action (the others are checked as a save checks them). |
| `GET` | `/v1/entities/{kind}/{name}/details` | [entries](#entries), [entries-details](#entries-details) | The fields of an entry the entry form does not show: |
| `PUT` | `/v1/entities/{kind}/{name}/details` | [entries-details](#entries-details) |  |
| `PUT` | `/v1/entities/{kind}/{name}/draft` | [entries](#entries) | Save an entry's whole draft at once (every section checked whole, written once or not at all). |
| `POST` | `/v1/entities/{kind}/{name}/preview` | [entries](#entries) | Preview an entry as its unsaved draft makes it (static, and a simulated sample scene); read only. |
| `GET` | `/v1/missions/{key}` | [missions](#missions), [play-quests-savegame](#play-quests-savegame), [play-setup](#play-setup) |  |
| `PUT` | `/v1/missions/{key}` | [missions](#missions) | Add or replace one mission (``rename``: |
| `DELETE` | `/v1/missions/{key}` | [missions](#missions) | Delete a mission (the others are checked as a save checks them). |
| `POST` | `/v1/deletions/{token}/restore` | [entries-actions](#entries-actions), [entries-fields](#entries-fields), [missions](#missions) | Undo a deletion: |
| `GET` | `/v1/map` | [map](#map) | The world's map as data: |
| `PUT` | `/v1/map` | — (studio: /v1/documents/map (the whole map; the studio's form sets one place's borders: /v1/map/{place})) |  |
| `PUT` | `/v1/map/{place}` | [map](#map) | The places ``place`` borders: |
| `GET` | `/v1/behaviours/rules` | [behaviour](#behaviour) | The rules that give people and places their behaviour by tags, as data: |
| `PUT` | `/v1/behaviours/rules/{group}` | [behaviour](#behaviour) | Replace the people's or the places' rules, checked as rules (400 says what is wrong). |
| `POST` | `/v1/suggestions` | [entries-suggest](#entries-suggest) | Gemma's suggestions for a text field of an entry, in the world's voice (the terminal editor's suggestions): |
| `GET` | `/v1/augmentation` | [grow](#grow) | World augmentation: whether it is on (the tick) and its own model's status (Qwen3.5-4B on vLLM). |
| `PUT` | `/v1/augmentation` | [grow](#grow) | Switch world augmentation on or off (augmentation.enabled). |
| `GET` | `/v1/world/revision` | [entries](#entries) | A fingerprint of the world's entries as stored; the content library lists them again when it changes. |
| `GET` | `/v1/world/size` | [grow](#grow) | How big the world is against the augmentation targets (augmentation.min_*): |
| `POST` | `/v1/jobs/world-proposal` | [grow](#grow) | Gemma proposes new places, people and items for this world, in the background (poll GET /v1/jobs/{id}; the… |
| `POST` | `/v1/world/additions` | [grow](#grow) | Add new places, people and items (e.g. |
| `POST` | `/v1/jobs/simulations` | [checks](#checks), [train-run](#train-run) | Run an evolution in the background (as POST /v1/simulations); poll GET /v1/jobs/{id} for its progress per… |
| `POST` | `/v1/jobs/engines/{engine_id}/continue` | [train-records](#train-records) | Continue a recorded engine's training in the background (as POST /v1/records/engines/{id}/continue). |
| `POST` | `/v1/jobs/engines/{engine_id}/retrain` | [train-records](#train-records) | Retrain from a recorded engine in the background (as POST /v1/records/engines/{id}/retrain). |
| `GET` | `/v1/judge/status` | [evaluate](#evaluate), [grow](#grow), [play-judge](#play-judge), [play-setup](#play-setup) | Whether the language model judge (Gemma 4 through vLLM) can be used: |
| `POST` | `/v1/judge/compare` | [evaluate-stories](#evaluate-stories) | Gemma 4 as a game critic on two stories (each a game in progress or a recorded story): |
| `POST` | `/v1/jobs/judge-benchmark` | [evaluate-benchmark](#evaluate-benchmark) | The judge on the engine's stories, in the background (poll GET /v1/jobs/{id}). |
| `GET` | `/v1/evaluation/target` | [evaluate-benchmark](#evaluate-benchmark) | What a judge benchmark or calibration with this engine_id plays, before it is submitted. |
| `POST` | `/v1/jobs/calibrate-fitness` | [evaluate-calibration](#evaluate-calibration) | Fit the fitness to the judge in the background: |
| `GET` | `/v1/fitness/term-weights` | [evaluate-calibration](#evaluate-calibration) | fitness.term_weights: |
| `PUT` | `/v1/fitness/term-weights` | [evaluate-calibration](#evaluate-calibration) | Save fitness.term_weights (e.g. |
| `GET` | `/v1/records/import` | [advanced-integration](#advanced-integration), [advanced-records](#advanced-records) | The server's folder of earlier engines and games (ANE_ARTIFACT_DIR, else artifacts/): |
| `POST` | `/v1/jobs/records-import` | [advanced-integration](#advanced-integration), [advanced-records](#advanced-records) | Import every engine and game in the server's import folder into the records, in the background (as `python… |
| `GET` | `/v1/jobs` | [advanced-app-jobs](#advanced-app-jobs), [advanced-integration](#advanced-integration) |  |
| `GET` | `/v1/jobs/{job_id}` | [advanced-integration](#advanced-integration), [advanced-records](#advanced-records), [api-client](#api-client), [evaluate-benchmark](#evaluate-benchmark), [evaluate-calibration](#evaluate-calibration), [grow](#grow), [train-records](#train-records), [train-run](#train-run) | status (queued, running, finished, failed), progress (generation, total, best, mean, validation), and the… |
| `POST` | `/v1/jobs/{job_id}/cancel` | [train-run](#train-run), [evaluate-benchmark](#evaluate-benchmark) | Cancel a training or evaluation job (queued: never starts; running: stops at its next safe point). |
| `GET` | `/v1/artifacts/{artifact_id}` | [export](#export), [train-run](#train-run) |  |
