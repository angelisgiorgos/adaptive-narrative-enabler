# ANE AI for Unity

Run an exported ANE AI (Adaptive Narrative Enabler) next to your Unity game: the library starts its
standalone runtime (no Python needed), drives stories over its local HTTP protocol and stops it
with the game. Windows, macOS and Linux players and the Editor. Not for mobile, consoles or WebGL,
which cannot start a process.

## Get it into your project

Either:

- **One import with everything** — once a training has finished (or an engine is loaded), export
  that complete engine from the studio (*Runs on: Unity*) or with `POST /v1/exports`
  (`platform: unity`, `source: engine`), then *Assets > Import Package > Custom Package…* and pick the
  `.unitypackage`. It holds this library (`Assets/ANE-AI/`) and, in
  `Assets/StreamingAssets/ANE-AI/`, the engine package (`package.zip`) and the runtimes
  (`runtimes/ane-runtime-<platform>.zip`). Importing a newer export updates the same files.
- **The library from the Package Manager** — *Add package from git URL…* with
  `<repository>.git?path=unity/com.ane.ai`, or *Add package from disk…* with this folder's
  `package.json`. Then *Tools > ANE AI > Import ANE AI export (.zip)…* turns an export for a
  standalone platform (or a runtime zip) into `StreamingAssets/ANE-AI/`.

Nothing else is needed: the library has no dependencies.

## Use it

*Tools > ANE AI > Add the ANE AI to the scene* (or add the `ANE AI` component to a GameObject).
It installs the ANE AI for this system into `Application.persistentDataPath/ANE-AI` on first
start, starts it, and on quit stops it: every open session is saved and can be continued.

```csharp
using Ane.AI;

var ane = FindObjectOfType<AneAiBehaviour>();
await ane.StartEngineAsync();
var client = ane.Client;

var world = await client.WorldAsync();                     // locations, npcs, items, events, missions, factions, stats
var view = await client.NewGameAsync(new NewGameOptions {
    Profile = "player-1",
    Player = new Dictionary<string, object> { ["coins"] = 20, ["add_items"] = new[] { "Rusty Sword" } },
});
view = await client.ChooseAsync(view, view.Choices[0]);    // by its stable id; view.TurnText is the story
await client.SyncAsync(view.GameId, new Dictionary<string, object> {
    ["type"] = "item_gained", ["item"] = "Ancient Map" });   // what happened in your game
var save = await client.SaveAsync(view.GameId);            // ane.savegame/1, keep it as JSON
var cancelled = await client.CancelAsync(view.GameId);     // save + release the session
view = await client.LoadGameAsync(save);                   // continues exactly (view.Restored)
```

`view.Choices` are the choices to show (`AneChoice`: `Id`, `Label`, `Category`, `SourceKind`,
`SourceName`, `TakesTurn`, `SkillCheck`, `FailureRisk`, `Requirements`). `ChooseAsync(view, choice)`
sends the choice's stable `Id` with the view's `StateVersion`: if the game has moved past that view
(another update, a sync), nothing is done and an `AneAiException` with `IsStale` (409) is thrown, and
`IsUnavailable` for an id not offered now; `Offered` lists the current ids. Show `GameAsync(gameId)`
then. `ChooseIdAsync(gameId, id, stateVersion)` chooses by id alone; `ChooseAsync(gameId, index)` and
`ChooseAsync(gameId, name)` work as before. With an ANE AI exported before action ids, choices have
no `Id` and `ChooseAsync(view, choice)` chooses by name.

`AneJson.Serialize` / `AneJson.ParseObject` turn save games into text and back (e.g. a file in
`Application.persistentDataPath`). The sample *Text adventure* (Package Manager > Samples, or
`Assets/ANE-AI/Samples/`) is a complete little game on top of it.

Without the component (tools, tests, another engine): `AneAiInstaller.Install(...)`,
`AneAiProcess.StartAsync(...)` and `AneAiClient` are plain .NET.

## What happens underneath

- The runtime is unpacked from its zip once per version (zips keep it whole: Unity would skip
  hidden files, treat libraries as plugins and drop execute bits).
- It listens on 127.0.0.1 only, on a free port, with a random token for every request; it stops
  when the game process ends, even if the game crashes.
- Profiles, save games and the content your game sends stay in `persistentDataPath/ANE-AI/data`.

## Notes

- macOS: the library removes the quarantine flag from the runtime it unpacks. For a signed and
  notarized game, sign the runtime with your identity too.
- The protocol is in the Adaptive Narrative Enabler's `docs/UNP.md`.
