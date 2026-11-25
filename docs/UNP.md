# The ANE AI and the UNP: integration guide

The **ANE AI** is an exported Adaptive Narrative engine: an evolved storyteller and its world,
packed with a standalone program that runs it. The **UNP** is the software that uses it: a game,
a game engine or a tool. This guide covers how the UNP downloads the ANE AI, starts it, talks to
it, saves and continues sessions, and stops it. Everything runs on the local computer, with no
internet connection.

- [Requirements and where they are met](#requirements-and-where-they-are-met)
- [Get the ANE AI](#get-the-ane-ai)
- [Lifecycle](#lifecycle)
- [Security](#security)
- [Endpoints](#endpoints)
- [ANE → UNP: parameters](#ane--unp-parameters)
- [UNP → ANE: parameters](#unp--ane-parameters)
- [Save game and load game](#save-game-and-load-game)
- [Events sent to the UNP](#events-sent-to-the-unp)
- [Errors](#errors)
- [Code: C#, Python](#code-c-python)
- [Unity](#unity)
- [Versions and compatibility](#versions-and-compatibility)
- [Not covered](#not-covered)

## Requirements and where they are met

| Requirement | How |
| --- | --- |
| 1.1 The ANE produces an ANE AI that can be downloaded | From a complete engine, once one exists: the studio's *Export the engine* or `POST /v1/exports {"platform": ...}`; a ZIP per operating system, or a `.unitypackage` ([Get the ANE AI](#get-the-ane-ai)) |
| 1.2 The UNP launches and runs it | `ane-ai serve --port 0 ...`; one JSON *ready* line on stdout ([Lifecycle](#lifecycle)) |
| 1.3 It runs locally, without an internet connection | A standalone program (Python and its libraries inside, no downloads); it listens on 127.0.0.1 only; the tag vectors it was trained with are in the package |
| 1.4 The UNP stops it fully | `POST /shutdown`; also when the UNP's process ends, when stdin closes, or on SIGTERM; it exits with code 0 ([Stop](#stop)) |
| 1.5 They talk after it starts | HTTP + JSON on 127.0.0.1 with a bearer token; events POSTed to the UNP's callback |
| 1.5.1 It sends parameters | `GET /world` (locations, NPCs, items, events, missions with names, tags, goals, descriptions, actions, encounters, event titles; stats; variables), `GET /games/<id>/state` (the session's parameters), `GET /games/<id>` (the scene) |
| 1.5.2 It receives parameters | `PATCH /games/<id>/player` (stats, items, coins, health, variables), `POST /games/<id>/sync` (what happened in the UNP), `POST /world/patch` (items, actions, tags, stats, any entity) |
| 1.5.3 It continues a session from the UNP's parameters (load game) | `POST /games {"savegame": ...}`: exactly from the snapshot when it fits, else rebuilt from the parameters (which may be the UNP's own) |
| 1.5.4 It sends the status of a session the UNP cancels (save game) | `POST /games/<id>/cancel`: the status (outcome `suspended`) and the save game, in the answer and in a `session.cancelled` event |

## Get the ANE AI

The ANE AI is made from a **complete engine**: the engine model a finished training run saves
(its world, storyteller, training state and tag vectors), or an engine loaded in the studio or
the API (also a continued or retrained one). It can be downloaded once one exists, from the studio
or the API:

- **Studio:** *📦 Export*: choose *Runs on* (a system or Unity),
  then the complete engine (newest first). Until a training has finished or an engine is loaded,
  the export says so and stays off.
- **API:** `GET /v1/exports/engines` lists the complete engines; `POST /v1/exports` with a
  `platform` and `source: "engine"` (`engine_id`, default the newest) or `source: "run"` (that
  run's engine; `run_id`, default the latest run). Anything else answers 409: no complete engine
  yet, a generation's champion, the remembered storyteller or a play session's storyteller.
  `GET /v1/exports/platforms` says which platforms this API can export now.

The ANE AI carries the engine's own world and tuning, whatever the studio has active when it is
exported. The Python version (`platform: python`, for developers) can still be made from any
storyteller.

| `platform` | What you download |
| --- | --- |
| `windows-x64`, `linux-x64`, `macos-arm64`, `macos-x64` | A ZIP with the program (`ane-ai` or `ane-ai.exe`, and `_internal/`), `runtime.json`, the engine package (`engine.json`, `config/engine.yaml`, `data/tag_vectors.json`, optionally `saves/evolution_memory.json`) and a `README.md`. Unzip it anywhere |
| `unity` | A `.unitypackage`: the C# library and, in `StreamingAssets/ANE-AI/`, the engine package and the runtimes this API has ([Unity](#unity)) |
| `python` | The engine as Python code (`run.py`); needs Python 3.10+ with `pyyaml` and `numpy`. For developers |

A platform is offered only when the API has a runtime for it that was built from the same
engine code (see [Versions](#versions-and-compatibility)); otherwise the export answers 409.
Every Docker image builds its own Linux runtime from its code, so a deployment always exports
`linux-x64` and `unity` (with the Linux runtime). CI builds all four on every push
(`.github/workflows/runtime.yml`) and attaches them to each release; put the Windows and macOS
zips in `runtimes/` before building the image (or in `ANE_RUNTIMES_DIR`) to export those too.

Finished runs are kept: in the records, or, when recording is off (a records folder the
container cannot write), with the saves (`ANE_SAVE_DIR/engines`, the newest 20), so a run can be
exported after a restart.

```bash
curl http://localhost:8005/v1/exports/engines
# -> {"engines": [{"engine_id": "3f2a...", "created_at": "...", "source": "studio run ...", "score": 71.4, ...}]}
curl -X POST http://localhost:8005/v1/exports -H 'Content-Type: application/json' \
     -d '{"source": "engine", "engine_id": "3f2a...", "platform": "windows-x64", "name": "my-ane-ai"}'
# -> {"download_url": "/v1/files/...", "platform": "windows-x64", "engine_id": "3f2a...", "code_version": "...", ...}
```

## Lifecycle

```text
 UNP                                              ANE AI
  |  start: ane-ai serve --port 0 --parent-pid P    |
  |        --exit-on-stdin-eof [--ready-file F]     |
  |        env ANE_RUNTIME_TOKEN=<token>            |
  |------------------------------------------------>|  loads the package, binds 127.0.0.1:<free port>
  |  stdout: {"event":"ready","url",...}            |
  |<------------------------------------------------|
  |  GET /world, POST /games, POST .../choose, ...  |
  |<===============================================>|  Authorization: Bearer <token>
  |                       POST <unp-url> (events)   |
  |<------------------------------------------------|
  |  POST /shutdown {"save": true}                  |
  |------------------------------------------------>|  saves every session, delivers events
  |  {"saved": [...]}; stdout: {"event":"stopped"}  |
  |<------------------------------------------------|  exits with code 0
```

### Start

```text
ane-ai [--package DIR] [--data-dir DIR] serve --port 0
       [--ready-file FILE] [--parent-pid PID] [--exit-on-stdin-eof]
       [--unp-url URL] [--allow-origin ORIGIN] [--allow-evolve] [--no-token]
```

| Option | Meaning |
| --- | --- |
| `--package` | The engine package folder. Default: the program's own folder |
| `--data-dir` | Where profiles, save games, suspended sessions and the UNP's world content are kept. Default for the standalone program: `%LOCALAPPDATA%\ANE-AI\<engine>` (Windows), `~/Library/Application Support/ANE-AI/<engine>` (macOS), `$XDG_DATA_HOME/ANE-AI/<engine>` or `~/.local/share/ANE-AI/<engine>` (Linux). It never writes next to the program |
| `--port 0` | Any free port; the ready line gives it |
| `--ready-file` | Also write the ready line to this file (atomically, readable only by the user) |
| `--parent-pid` | Stop when this process (the UNP) ends |
| `--exit-on-stdin-eof` | Stop when stdin closes (the UNP ended or closed the pipe) |
| `--unp-url` | The UNP's callback: session reports and [events](#events-sent-to-the-unp) are POSTed there |
| `--allow-origin` | Serve one web origin (CORS), e.g. a UNP that is a web page on localhost. None by default |
| `--allow-evolve` | Allow `POST /evolve` (offline training rewrites `engine.json`). Off by default |
| `--no-token` | No token. Local development only |

Environment: `ANE_RUNTIME_TOKEN` (the token; give it here, not on the command line, where other
users can see it), `ANE_UNP_TOKEN` (a bearer token the ANE AI sends to the UNP's callback).

**The ready line.** When it is ready, the program prints one JSON line on stdout and flushes it:

```json
{"event": "ready", "protocol": 1, "url": "http://127.0.0.1:53817", "port": 53817, "pid": 4242,
 "engine": "my-ane-ai", "code_version": "a21d4eb44a560a80", "data_dir": "/home/me/.local/share/ANE-AI/my-ane-ai"}
```

When the UNP gave no token, a random one is made and the line carries it (`"token"`); a token
the UNP gave is never echoed. stdout carries only these lifecycle lines (`ready`, `stopped`,
`error`); logs go to stderr. When it cannot start (no package, a broken world, a port in use)
it prints `{"event": "error", "error": "..."}` and exits with code 1.

### Stop

`POST /shutdown {"save": true, "end_sessions": false}`:

1. New requests get 503 (`GET /health` still answers, with `"status": "stopping"`).
2. Every session still going is saved: with `save` it is cancelled ([save game](#save-game-and-load-game)) and its
   save game written to `<data-dir>/suspended/<game id>.json`; with `end_sessions` it is ended
   instead (its progression counted, its report sent).
3. Events and reports on their way are delivered (`engine.stopping` last).
4. The answer lists what was saved: `{"saved": [{"game_id", "path", "status"}], "ended": [...]}`.
5. It prints `{"event": "stopped", "reason": "POST /shutdown", "pid": ...}` and exits with code 0.

Calling it twice is safe. The same happens on SIGTERM, SIGINT and SIGHUP (POSIX), Ctrl+Break
(Windows), when the `--parent-pid` process ends (also if the UNP crashes) and when stdin closes
with `--exit-on-stdin-eof`.

**The UNP's part:** call `/shutdown`, wait a few seconds for the process to exit, then kill it if
it is still running. Profiles and save games are written atomically, so a kill cannot damage
them. On Windows, put the process in a Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`:
then Windows ends it with the UNP whatever happens ([C#](#c-plain-net)).

## Security

The ANE AI serves only the UNP that started it:

- It listens on 127.0.0.1 only.
- Every request needs `Authorization: Bearer <token>` (compared in constant time); 401 otherwise.
- The `Host` header must be `127.0.0.1:<port>`, `localhost:<port>` or `[::1]:<port>`; 403
  otherwise (this stops DNS-rebinding attacks from web pages).
- A request with an `Origin` header (sent by browsers) gets 403, unless `--allow-origin` names that origin.
- Bodies are JSON objects (`Content-Type: application/json`, 415 otherwise) of at most 8 MB (413).
- `POST /evolve` gets 403 unless `--allow-evolve`.
- Save games are JSON, never code: loading one cannot run anything.

## Endpoints

All paths are relative to the ready line's `url`. Bodies and answers are JSON objects.

| Method and path | Body | Answer |
| --- | --- | --- |
| `GET /health` | | `{"status": "ok" \| "stopping", "pid", "engine", "protocol", "code_version", "games", "world_revision"}` |
| `GET /engine` | | The engine: name, storyteller, world summary, `code_version`, `protocol`, `data_dir`, `world_revision`, `games` |
| `POST /shutdown` | `{"save": true, "end_sessions": false}` | `{"saved": [...], "ended": [...]}`, then the process exits |
| `GET /world` | | The world catalogue (`ane.world/1`) |
| `GET /world/<kind>/<name>` | | One entity; `kind`: `locations`, `npcs`, `items`, `events`, `missions` |
| `POST /world/patch` | A [world patch](#world-content-post-worldpatch) | `{"revision", "persisted", "warnings", "patch_id"}`; 422 `{"errors": [...]}` |
| `POST /world/reset` | `{}` | `{"revision"}`: the UNP's content kept in the data folder is dropped |
| `GET /progression/<profile>` | | The profile's save game: goals achieved, sessions and their reports |
| `GET /games` | | `{"games": [{"game_id", "profile", ...}]}` |
| `POST /games` | `{"profile", "start", "mission", "seed", "stats_mode", "stats", "persist", "player"}` | 201: the [view](#the-view) |
| `POST /games` | `{"savegame": {...}, "profile", "seed", "persist"}` | 201: the view plus `"restored": "exact" \| "parameters"` and `"note"` |
| `GET /games/<id>` | | The view |
| `GET /games/<id>/state` | | The session's parameters (`ane.session_state/1`) and its `session` (cast, NPC roles and homes) |
| `PATCH /games/<id>/player` | [Player fields](#the-player-patch-gamesidplayer) | The view plus `"applied": [...]` |
| `POST /games/<id>/sync` | One [event](#what-happened-in-the-unp-post-gamesidsync) or `{"events": [...]}` | The view plus `"synced": [...]` |
| `POST /games/<id>/choose` | `{"action": "Approach the Merchant"}` or `{"action": 0}` | The view plus `"turn"`: this turn's story text. Approaching someone and leaving a conversation take no turn (`turn_number` stays); in a conversation the choices are that person's and "Leave the conversation", and the view's `talking_to` names them ([docs/WORLD.md](WORLD.md#conversations)) |
| `POST /games/<id>/save` | `{}` | The save game (`ane.savegame/1`); the session goes on |
| `POST /games/<id>/cancel` | `{}` | `{"game_id", "status", "savegame"}`; the session is released |
| `POST /games/<id>/end` | `{}` | The view: the session ended where it stands; its progression is saved and reported |
| `GET /games/<id>/report` | | The session report (`ane.session_report/1`); 409 while the session goes on |
| `GET /games/<id>/transcript` | | `{"game_id", "transcript"}`: the whole story so far |
| `DELETE /games/<id>` | | `{"deleted": "<id>"}`: forgotten without saving |
| `POST /evolve` | `{"generations", "pop_size", "episodes", "seed", "adopt", "resume"}` | Offline training (only with `--allow-evolve`) |

### The view

What the player sees now (`GET /games/<id>` and most answers about a game):

`game_id`, `location`, `start_location`, `scene`, `description`, `npcs`, `items`, `actions` (the
choices, in order), `choices` (each with its skill-check hint), `health`, `max_health`, `coins`,
`power`, `inventory`, `stats`, `last_check`, `mission`, `mission_description`, `mission_status`,
`quest`, `people`, `people_met`, `ways_on`, `stakes`, `cast`, `unique_actions`, `map`, `event`,
`variables`, `session_report`, `ended`, `won`, `end_reason`, `turn_number`, `talking_to` (the person
the player is talking with), `turn_summary` (the last turn as a player follows it: the choice, its
check, what came of it, what it changed, the quest steps it completed; see `GET
/v1/sessions/{id}/journal` in [API.md](API.md)); after a choice, `turn` (the story text of that turn).

## ANE → UNP: parameters

### The world: `GET /world`

```json
{"format": "ane.world/1", "engine": {...}, "revision": 3,
 "locations": [{"kind": "locations", "name": "Harbor", "tags": [...], "descriptions": [...], "actions": [...], ...}],
 "npcs":      [{"kind": "npcs", "name": "Merchant", "tags": [...], "goals": [...], "role": "...", "encounter_event": "...", "secret": {...}, "actions": [...], ...}],
 "items":     [{"kind": "items", "name": "Ancient Map", "associated_tags": [...], "stat_bonus": {...}, "actions": [...], ...}],
 "events":    [{"kind": "events", "name": "storm", "title": "A Storm Breaks", "trigger": {...}, "actions": [...], ...}],
 "missions":  [{"kind": "missions", "name": "find_the_map", "title": "...", "description": "...", "stages": [...], ...}],
 "stats": {"names": [...], "definitions": {...}, "allocation": {...}, "tag_checks": {...}},
 "variables": {...}, "routes": {...}, "starting_locations": [...], "npc_roles": [...]}
```

Every entity has its `kind`, its `name` and every field the author gave it, as in the world's
YAML. Actions carry their checks, repeat rule, conditions and outcomes. `revision` grows with
every world patch.

### A session: `GET /games/<id>/state`

The session's parameters (`ane.session_state/1`), the same object a save game carries:

| Field | What it is |
| --- | --- |
| `profile`, `session_id`, `start_location`, `current_location` | Who plays, and where |
| `location_counts`, `path`, `known_paths` | Visits per place, the way taken, the ways found (place → places) |
| `turns` | Actions taken |
| `health`, `max_health`, `coins` | The player's state |
| `stat_points`, `stat_changes` | The stats: allocated points, and changes in play |
| `inventory`, `used_items` | Items held; items used up or removed |
| `variables` | The persistent variables |
| `mission` | `{"key", "title", "stage", "won"}` or null |
| `actions_succeeded`, `unique_actions` | Actions that succeeded; one-time actions and every attempt at them |
| `events` | Events and their results |
| `people_met`, `removed_npcs`, `allies`, `defeated`, `alert` | The people of the story |
| `ended` | Whether the session has ended |

## UNP → ANE: parameters

### The player: `PATCH /games/<id>/player`

Also as `"player"` in `POST /games`. Every field is optional:

```json
{"stat_points": {"strength": 4, "wits": 3, "charm": 3},
 "stat_changes": {"strength": 1},
 "inventory": ["Rusty Sword", "Ancient Map"],
 "add_items": ["Lantern"], "remove_items": ["Rusty Sword"],
 "coins": 20, "health": 8, "max_health": 12,
 "variables": {"reputation": 3}}
```

`stat_points` is a whole allocation and must fit the stats' point budget; `inventory` sets the
items held exactly, `add_items` and `remove_items` change them. The values are set, not played:
nothing is narrated and no reward is paid. Unknown items, stats or variables give 400.

### What happened in the UNP: `POST /games/<id>/sync`

One event, or `{"events": [...]}`:

| `type` | Fields | Effect |
| --- | --- | --- |
| `action_done` | `action`, `owner`?, `where`?, `succeeded`? (true), `kept`? | The action counts as done (one-time actions are used up, goals that need it advance) |
| `item_gained`, `item_lost` | `item` | The player holds, or no longer holds, the item |
| `npc_removed` | `npc` | The NPC leaves the story, with its consequences |
| `location_visited` | `location` | The player is there now |
| `coins_changed`, `health_changed` | `amount` | Added (or taken, when negative) |

Goals are checked afterwards: what the player did in the UNP can win the session.

### World content: `POST /world/patch`

```json
{"upsert":      {"items": [{"name": "Lantern", "associated_tags": ["light"], "descriptions": ["A brass lantern."]}]},
 "update":      {"npcs": {"Merchant": {"goals": ["Sell the lantern"], "tags": ["trade"]}}},
 "delete":      {"items": ["Rusty Sword"]},
 "add_actions": {"locations": {"Harbor": [{"name": "Light the beacon", "outcomes": [{"desc": "It burns."}]}]}},
 "tags":        {"npcs": {"Merchant": {"add": ["smuggler"], "remove": ["guidance"]}}},
 "stats":       {"stats": {...}, "allocation": {...}, "tag_checks": {...}},
 "persist":     false}
```

Kinds: `locations`, `npcs`, `items`, `events`, `missions`. `upsert` inserts or replaces whole
entries; `update` changes fields of existing ones (`tags` stands for the kind's tag field; `null`
removes a field); `delete` removes entries; `add_actions` appends actions; `tags` adds or removes
tags (or sets them with a list); `stats` changes the character stats.

The patched world is checked like the studio checks a world (fields, references, actions and
outcomes, secrets, missions, routes, logic) and built once. Any problem gives 422 with every
error; nothing changes. An accepted patch is used by new sessions; sessions already going keep
their world. A tag the storyteller was not trained with gives a warning (it is compared as
unknown). Patches are kept in `<data-dir>/unp_patches.json` and applied again at every start;
`persist: true` writes the patched world into the package (`config/engine.yaml`) instead.
`POST /world/reset` drops the kept patches.

## Save game and load game

### The save game (`ane.savegame/1`)

```json
{"format": "ane.savegame/1",
 "engine": {"name": "my-ane-ai", "code_version": "a21d4eb44a560a80", "config_hash": "...", ...},
 "session_id": "...", "profile": "player-1", "saved_at": "2026-11-17T10:00:00+00:00",
 "parameters": {"format": "ane.session_state/1", ...},
 "snapshot": {"format": "ane.snapshot/1", ...},
 "checksum": "..."}
```

- `parameters` is the session's state as above: the UNP may read it, keep it and change it.
- `snapshot` is the exact state of the story (opaque to the UNP), with the engine code, world
  and storyteller it was made with. `checksum` covers it.

The UNP stores the document as it is (a file, its own save system), with any JSON library: one
that writes the number `1.0` as `1` (C#, JavaScript) does not change the snapshot or its checksum.

`POST /games/<id>/save` returns it and the session goes on. `POST /games/<id>/cancel` is the
UNP cancelling the session: it returns

```json
{"game_id": "...",
 "status": {"format": "ane.session_report/1", "outcome": "suspended", "savegame_id": "...", "goal": {...},
            "player": {...}, "progression": {...}, ...},
 "savegame": {"format": "ane.savegame/1", ...}}
```

keeps the status in the profile (a suspended session is not a finished playthrough), sends it
in a `session.cancelled` event, and releases the session.

### Loading: `POST /games {"savegame": ...}`

- **Exact** (`"restored": "exact"`): the snapshot is there, intact (checksum), and made with this
  engine's code, world and storyteller. The session continues exactly where it was: the same
  scene, the same next turns. Fields the UNP changed in `parameters` (`stat_points`,
  `stat_changes`, `inventory`, `coins`, `health`, `max_health`, `variables`) are applied on top;
  `note` lists them.
- **Rebuilt** (`"restored": "parameters"`): there is no snapshot, it is damaged, or another
  engine made it (`note` says which). The session is rebuilt from `parameters`: the place,
  ways found, visits, items, used items, health, coins, stats, variables, mission stage,
  one-time actions done, people met and removed, allies, the defeated and the alert. Nothing is
  narrated again and no reward is paid twice; the story says it goes on (`[RESUMED]`).

A UNP can therefore also start a session from its own parameters: send
`{"savegame": {"format": "ane.savegame/1", "parameters": {...}}}` without a snapshot.
`current_location` (or `start_location`) is required; every other field is optional, and
without a `mission` one is drawn as for a new session. An ended session is not continued (400):
start a new one.

## Events sent to the UNP

With `--unp-url` (or `ANE_UNP_URL`, or `session_report.url` in the package's configuration), the
ANE AI POSTs JSON to the UNP, with `Authorization: Bearer $ANE_UNP_TOKEN` when that is set:

```json
{"format": "ane.event/1", "event": "session.cancelled", "at": "...", "engine": {...},
 "session_id": "...", "data": {"status": {...}, "savegame": {...}}}
```

| `event` | When | `data` |
| --- | --- | --- |
| `session.started` | A session starts or is loaded | `game_id`, `profile`, `start`, `goal` (or `restored`) |
| `session.cancelled` | `POST /games/<id>/cancel`, or a shutdown that saves | `status`, `savegame` |
| `engine.stopping` | The ANE AI stops | `saved`, `ended` (game ids) |

When a session ends (won, lost or ended with `/end`), its report (`ane.session_report/1`) is
POSTed to the same URL. Delivery failures are noted and do not stop the game; the UNP can always
ask (`GET /games/<id>/report`, `GET /progression/<profile>`).

## Errors

Errors are `{"error": "..."}`, with `"errors": [...]` when there are several.

| Status | When |
| --- | --- |
| 400 | A bad request: unknown action, place, item, stat or variable; not a save game; an ended session |
| 401 | No token, or a wrong one |
| 403 | Not local (`Host`), a web page (`Origin`), or `/evolve` without `--allow-evolve` |
| 404 | Unknown path, game or entity |
| 405 | A method the path does not take |
| 409 | `GET .../report` while the session goes on |
| 413, 415 | The body is too large, or not JSON |
| 422 | A world patch that does not fit (every problem listed) |
| 503 | The ANE AI is stopping |

## Code: C#, Python

### C# (plain .NET)

The Unity library's [`AneAiProcess`](../unity/com.ane.ai/Runtime/AneAiProcess.cs) and
[`AneAiClient`](../unity/com.ane.ai/Runtime/AneAiClient.cs) are plain .NET (netstandard2.1, no
dependencies) and work outside Unity too:

```csharp
using Ane.AI;

var ane = await AneAiProcess.StartAsync(new AneAiLaunchOptions {
    Executable = @"C:\Games\MyGame\ane-ai\ane-ai.exe",       // its folder holds the package
    DataDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "MyGame", "ane"),
    UnpCallbackUrl = "http://127.0.0.1:9000/ane-events",     // optional
});
var client = ane.Client;                                     // token and URL from the ready line
var world = await client.WorldAsync();
var view = await client.NewGameAsync(new NewGameOptions { Profile = "player-1" });
view = await client.ChooseAsync(view.GameId, 0);
var cancelled = await client.CancelAsync(view.GameId);      // status + save game
var save = cancelled.GetObject("savegame");
File.WriteAllText("slot1.json", AneJson.Serialize(save));
view = await client.LoadGameAsync(AneJson.ParseObject(File.ReadAllText("slot1.json")));
await ane.StopAsync();                                       // /shutdown, wait 5 s, kill if needed
```

`StartAsync` passes `--parent-pid` and `--exit-on-stdin-eof`, so the ANE AI stops with the UNP.
To have Windows enforce it even if the UNP is killed, add the process to a Job Object:

```csharp
using System.Runtime.InteropServices;

static class Job
{
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr a, string name);
    [DllImport("kernel32.dll")] static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref EXTENDED info, int size);
    [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [StructLayout(LayoutKind.Sequential)] struct BASIC { public long a, b; public uint LimitFlags; public UIntPtr c, d; public uint e; public UIntPtr f; public uint g, h; }
    [StructLayout(LayoutKind.Sequential)] struct IO { public ulong a, b, c, d, e, f; }
    [StructLayout(LayoutKind.Sequential)] struct EXTENDED { public BASIC Basic; public IO Io; public UIntPtr g, h, i, j; }

    static IntPtr job;

    public static void KillWithMe(System.Diagnostics.Process process)
    {
        if (job == IntPtr.Zero)
        {
            job = CreateJobObject(IntPtr.Zero, null);
            var info = new EXTENDED { Basic = new BASIC { LimitFlags = 0x2000 } };  // KILL_ON_JOB_CLOSE
            SetInformationJobObject(job, 9, ref info, Marshal.SizeOf<EXTENDED>());  // ExtendedLimitInformation
        }
        AssignProcessToJobObject(job, process.Handle);
    }
}
```

Keep `job` open for the UNP's lifetime: when the UNP ends, Windows closes it and ends the ANE AI.

### Python

```python
import json, os, secrets, subprocess, urllib.request

token = secrets.token_urlsafe(24)
process = subprocess.Popen(
    ["./ane-ai/ane-ai", "--data-dir", "ane-data", "serve", "--port", "0",
     "--parent-pid", str(os.getpid()), "--exit-on-stdin-eof"],
    env={**os.environ, "ANE_RUNTIME_TOKEN": token},
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
ready = json.loads(process.stdout.readline())
assert ready["event"] == "ready", ready

def call(method, path, body=None):
    request = urllib.request.Request(
        ready["url"] + path, method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read())

world = call("GET", "/world")
view = call("POST", "/games", {"profile": "player-1", "player": {"coins": 20}})
view = call("POST", f"/games/{view['game_id']}/choose", {"action": 0})
save = call("POST", f"/games/{view['game_id']}/cancel", {})["savegame"]
view = call("POST", "/games", {"savegame": save})     # view["restored"] == "exact"
call("POST", "/shutdown", {"save": True})
try:
    process.wait(timeout=10)
except subprocess.TimeoutExpired:
    process.kill()
```

[`packaging/smoke_test.py`](../packaging/smoke_test.py) drives the whole protocol this way, and CI
runs it against every standalone build.

## Unity

[`unity/com.ane.ai`](../unity/com.ane.ai/README.md) is a Unity library (C#, Unity 2021.3 or
later, no dependencies) that installs, starts, drives and stops the ANE AI from a game, in the
Editor and in Windows, macOS and Linux players. Mobile, consoles and WebGL cannot start a process
and are not supported.

Get it into a project in one of two ways:

- **Export with `platform: unity`** and import the `.unitypackage` (*Assets > Import Package >
  Custom Package…*). It holds the library in `Assets/ANE-AI/` and, in
  `Assets/StreamingAssets/ANE-AI/`, the engine package (`package.zip`) and the runtimes
  (`runtimes/ane-runtime-<platform>.zip`). Asset GUIDs are fixed per path, so importing a newer
  export updates the same files.
- **Package Manager**: *Add package from git URL…* with `<repository>.git?path=unity/com.ane.ai`
  (or from disk). Then *Tools > ANE AI > Import ANE AI export (.zip)…* turns a standalone export
  into `StreamingAssets/ANE-AI/`.

Add the *ANE AI* component (`AneAiBehaviour`, or *Tools > ANE AI > Add the ANE AI to the scene*).
On first start it unpacks the runtime for the current system and the package into
`Application.persistentDataPath/ANE-AI` (once per version), starts it, and stops it when the
game quits (every session saved). `ane.Client` is an `AneAiClient` with a method per endpoint.
The *Text adventure* sample is a small complete game on it.

A Unity "Python plugin" is not an alternative: Unity's Python Scripting package runs in the
Editor only, and embedding CPython in a player breaks with IL2CPP and on every platform update.
The library keeps Python inside the standalone program, where Unity never sees it.

CI compiles the library outside Unity (`Tests~/Build`, against stubs of the Unity API) and
drives a real ANE AI through the C# client (`Tests~/Protocol`).

## Trying it in the studio

The studio and the REST API offer the same session operations on their own games, so an
integration can be tried before an export: *▶ Play* (*Save game*, *Continue a save game*), *⚙ Advanced →
🔌 Integration* (the game in ▶ Play as a UNP sees it: the parameters, set the player, what happened;
a session report sent again; the runtimes and the world catalogue), and
`GET /v1/sessions/{id}/state`, `PATCH /v1/sessions/{id}/player`, `POST /v1/sessions/{id}/sync`,
`GET /v1/sessions/{id}/savegame`, `POST /v1/sessions/load` and `GET /v1/world/catalogue`
([API.md](API.md)). Save games go both ways between the studio and the ANE AI.

## Versions and compatibility

- `protocol` (in the ready line, `/health` and `/engine`) is the version of this protocol: 1. A
  change that breaks UNPs raises it.
- `code_version` is a hash of the engine code. The API puts a runtime in an export only when it
  was built from the API's own engine code, so a story plays the same in the studio and in the
  ANE AI. `runtime.json` next to the program records it, with the target, the Python version and
  the build time.
- An exact save game needs the same `code_version`, world (`config_hash`) and storyteller;
  across versions, save games load by their parameters.

## Not covered

- **Code signing.** The programs are not signed. On macOS a ZIP downloaded with a browser is
  quarantined: the UNP can download it itself, or run `xattr -dr com.apple.quarantine <folder>`
  (the Unity library does this for the runtime it unpacks). A signed and notarized game should
  sign the runtime with its own identity. On Windows, SmartScreen may warn about an unsigned
  program that a user starts by hand; a UNP starting it is not affected.
- **Other systems.** Windows on ARM runs the x64 build under emulation; 32-bit systems, mobile
  and consoles are not supported.
