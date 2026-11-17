// Drives a real ANE AI through the package's C# client, outside Unity (CI):
//     dotnet run -- <package folder with run.py> <python>          (the runtime started with Python)
//     dotnet run -- <folder with ane-ai[.exe] and the package>      (a standalone runtime)
using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Threading.Tasks;
using Ane.AI;

static class Program
{
    static int failures;

    static void Check(string label, bool condition, string detail = "")
    {
        Console.WriteLine($"{(condition ? "ok  " : "FAIL")} {label}" + (condition ? "" : $": {detail}"));
        if (!condition) failures++;
    }

    static async Task<int> Main(string[] args)
    {
        if (args.Length < 1) { Console.Error.WriteLine("usage: <package folder> [python]"); return 2; }
        var folder = Path.GetFullPath(args[0]);
        var data = Path.Combine(Path.GetTempPath(), "ane-cs-" + Guid.NewGuid().ToString("N"));

        // JSON
        var parsed = AneJson.ParseObject("{\"a\": [1, 2.5, \"x\\n\\u00e9\", true, null], \"b\": {\"c\": -3}}");
        Check("json", parsed.GetList("a").Count == 5 && (long)parsed.GetList("a")[0] == 1 && parsed.GetObject("b").GetInt("c") == -3
              && (string)parsed.GetList("a")[2] == "x\né");
        Check("json round trip", AneJson.Serialize(AneJson.Parse(AneJson.Serialize(parsed))) == AneJson.Serialize(parsed));

        var options = args.Length > 1
            ? new AneAiLaunchOptions { Executable = args[1], LauncherArguments = "run.py", WorkingDirectory = folder, PackageDir = folder, DataDir = data }
            : new AneAiLaunchOptions { Executable = Path.Combine(folder, AneAiPlatform.ExecutableName(AneAiPlatform.Current)), PackageDir = folder, DataDir = data };

        using (var ane = await AneAiProcess.StartAsync(options))
        {
            var client = ane.Client;
            Check("ready", ane.Port > 0 && ane.Ready.GetString("event") == "ready");
            Check("health", (await client.HealthAsync()).GetString("status") == "ok");
            var world = await client.WorldAsync();
            Check("world", world.GetList("locations").Count > 0 && world.GetList("npcs").Count > 0, AneJson.Serialize(world.Keys));
            var firstItem = ((Dictionary<string, object>)world.GetList("items")[0]).GetString("name");
            var view = await client.NewGameAsync(new NewGameOptions
            {
                Profile = "unity", Seed = 3,
                Player = new Dictionary<string, object> { ["coins"] = 33, ["add_items"] = new[] { firstItem } },
            });
            Check("new game with the player's state", view.Coins == 33 && view.Inventory.Contains(firstItem) && view.Actions.Count > 0);
            view = await client.UpdatePlayerAsync(view.GameId, new Dictionary<string, object> { ["health"] = 4 });
            Check("player update", view.Health == 4);
            view = await client.SyncAsync(view.GameId, new Dictionary<string, object> { ["type"] = "coins_changed", ["amount"] = -3 });
            Check("sync", view.Coins == 30);
            view = await client.ChooseAsync(view.GameId, 0);
            Check("a turn", view.Turn == 1 && !string.IsNullOrEmpty(view.TurnText));
            var state = await client.StateAsync(view.GameId);
            Check("state", state.GetString("format") == "ane.session_state/1");
            try
            {
                await client.PatchWorldAsync(new Dictionary<string, object>
                {
                    ["upsert"] = new Dictionary<string, object>
                    {
                        ["items"] = new List<object> { new Dictionary<string, object> { ["name"] = "Lamp", ["actions"] = new List<object> {
                            new Dictionary<string, object> { ["name"] = "Go", ["outcomes"] = new List<object> {
                                new Dictionary<string, object> { ["desc"] = "Off.", ["move_to"] = "Nowhere" } } } } } },
                    },
                });
                Check("refused patch", false, "accepted");
            }
            catch (AneAiException refused)
            {
                Check("refused patch", refused.Status == 422 && refused.Errors.Count > 0, refused.Message);
            }
            var cancelled = await client.CancelAsync(view.GameId);
            var savegame = cancelled.GetObject("savegame");
            Check("cancel (save game)", cancelled.GetObject("status").GetString("outcome") == "suspended"
                  && savegame.GetString("format") == "ane.savegame/1");
            var loaded = await client.LoadGameAsync(savegame);
            Check("load (exact)", loaded.Restored == "exact" && loaded.Turn == 1);
            try { await client.GameAsync("nope"); Check("unknown game", false); }
            catch (AneAiException missing) { Check("unknown game", missing.Status == 404); }

            var stopped = await ane.StopAsync(save: true, timeout: TimeSpan.FromSeconds(60));
            Check("stop (sessions saved)", stopped != null && stopped.GetList("saved").Count == 1, AneJson.Serialize(stopped));
            Check("exited", ane.HasExited);
        }

        // The importer: an export laid out like the studio's, into StreamingAssets/ANE-AI.
        var export = Path.Combine(data, "export.zip");
        using (var zip = ZipFile.Open(export, ZipArchiveMode.Create))
        {
            void Add(string name, string text)
            {
                using (var writer = new StreamWriter(zip.CreateEntry(name).Open())) writer.Write(text);
            }
            Add("my-ai/runtime.json", "{\"target\": \"linux-x64\", \"code_version\": \"x\"}");
            Add("my-ai/ane-ai", "#!/bin/sh");
            Add("my-ai/_internal/lib.so", "x");
            Add("my-ai/engine.json", "{}");
            Add("my-ai/config/engine.yaml", "x: 1");
            Add("my-ai/data/tag_vectors.json", "{}");
            Add("my-ai/README.md", "readme");
        }
        var target = Path.Combine(data, "StreamingAssets", "ANE-AI");
        AneAiExportImport.Import(export, target);
        using (var runtime = ZipFile.OpenRead(Path.Combine(target, "runtimes", "ane-runtime-linux-x64.zip")))
        using (var package = ZipFile.OpenRead(Path.Combine(target, "package.zip")))
        {
            var runtimeNames = new HashSet<string>(); foreach (var e in runtime.Entries) runtimeNames.Add(e.FullName);
            var packageNames = new HashSet<string>(); foreach (var e in package.Entries) packageNames.Add(e.FullName);
            Check("import: runtime", runtimeNames.SetEquals(new[] { "runtime.json", "ane-ai", "_internal/lib.so" }), string.Join(",", runtimeNames));
            Check("import: package", packageNames.SetEquals(new[] { "engine.json", "config/engine.yaml", "data/tag_vectors.json" }), string.Join(",", packageNames));
        }

        // Quoting: arguments with spaces, quotes and trailing backslashes reach the program whole.
        var quote = typeof(AneAiProcess).GetMethod("Quote", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static);
        Check("quoting", (string)quote.Invoke(null, new object[] { @"C:\my pkg\" }) == "\"C:\\my pkg\\\\\""
              && (string)quote.Invoke(null, new object[] { "a\"b" }) == "\"a\\\"b\"");
        Check("json: no NaN", AneJson.Serialize(new List<object> { double.NaN, 1.5 }) == "[null,1.5]", AneJson.Serialize(new List<object> { double.NaN, 1.5 }));

        try { Directory.Delete(data, true); } catch (Exception) { }
        Console.WriteLine(failures == 0 ? "C# client test passed." : $"{failures} check(s) failed.");
        return failures == 0 ? 0 : 1;
    }
}
