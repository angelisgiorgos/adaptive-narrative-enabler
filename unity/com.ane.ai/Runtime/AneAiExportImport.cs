// Turns an ANE AI export (.zip from the studio or POST /v1/exports with a standalone platform), or a
// runtime zip (ane-runtime-<platform>.zip), into what the game ships in StreamingAssets/ANE-AI:
// package.zip and runtimes/ane-runtime-<platform>.zip. Used by the Editor menu; plain .NET.
using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Linq;

namespace Ane.AI
{
    public static class AneAiExportImport
    {
        static readonly string[] PackageParts = { "engine.json", "config/", "data/", "saves/" };
        static readonly System.Reflection.PropertyInfo ExternalAttributes =
            typeof(ZipArchiveEntry).GetProperty("ExternalAttributes");

        /// <summary>Import an export or a runtime zip into <paramref name="target"/>; returns what was done.</summary>
        public static string Import(string zipPath, string target)
        {
            Directory.CreateDirectory(Path.Combine(target, "runtimes"));
            using (var archive = ZipFile.OpenRead(zipPath))
            {
                var entries = archive.Entries.Where(e => !string.IsNullOrEmpty(e.Name)).ToList();
                var runtimeJson = entries.FirstOrDefault(e => Strip(e.FullName, out _) == "runtime.json");
                var engineJson = entries.FirstOrDefault(e => Strip(e.FullName, out _) == "engine.json");
                if (runtimeJson == null && engineJson != null)
                    throw new AneAiException("This is a Python export. Export the ANE AI for a standalone platform " +
                                             "(windows-x64, linux-x64, macos-arm64, macos-x64) or for unity.");
                if (runtimeJson == null)
                    throw new AneAiException("Not an ANE AI export: it has neither runtime.json nor engine.json.");
                string platform;
                using (var reader = new StreamReader(runtimeJson.Open()))
                    platform = AneJson.ParseObject(reader.ReadToEnd()).GetString("target");
                if (string.IsNullOrEmpty(platform)) throw new AneAiException("runtime.json names no target platform.");

                var runtimeEntries = new List<ZipArchiveEntry>();
                var packageEntries = new List<ZipArchiveEntry>();
                foreach (var entry in entries)
                {
                    var relative = Strip(entry.FullName, out _);
                    if (relative == "README.md") continue;
                    if (PackageParts.Any(part => part.EndsWith("/") ? relative.StartsWith(part) : relative == part))
                        packageEntries.Add(entry);
                    else
                        runtimeEntries.Add(entry);
                }
                var runtimeZip = Path.Combine(target, "runtimes", $"ane-runtime-{platform}.zip");
                Repack(runtimeEntries, runtimeZip);
                var done = $"Runtime for {platform} → {runtimeZip}";
                if (engineJson != null)
                {
                    var packageZip = Path.Combine(target, "package.zip");
                    Repack(packageEntries, packageZip);
                    done += $"\nEngine package → {packageZip}";
                }
                else if (!File.Exists(Path.Combine(target, "package.zip")))
                {
                    done += "\nNo engine package yet: import an ANE AI export too.";
                }
                return done;
            }
        }

        /// <summary>The path inside the export's top folder ("name/config/x" → "config/x"), if it has one.</summary>
        static string Strip(string fullName, out string top)
        {
            var name = fullName.Replace('\\', '/');
            var slash = name.IndexOf('/');
            top = null;
            if (slash > 0)
            {
                var first = name.Substring(0, slash);
                var rest = name.Substring(slash + 1);
                // A top folder (the export's name), unless the entry is already at the root.
                if (first != "config" && first != "data" && first != "saves" && first != "_internal")
                {
                    top = first;
                    return rest;
                }
            }
            return name;
        }

        static void Repack(IEnumerable<ZipArchiveEntry> entries, string zipPath)
        {
            var temporary = zipPath + ".tmp";
            if (File.Exists(temporary)) File.Delete(temporary);
            using (var output = ZipFile.Open(temporary, ZipArchiveMode.Create))
            {
                foreach (var entry in entries)
                {
                    var target = output.CreateEntry(Strip(entry.FullName, out _), CompressionLevel.Optimal);
                    if (ExternalAttributes != null)  // execute bits (.NET Standard 2.1; not in older profiles)
                        ExternalAttributes.SetValue(target, ExternalAttributes.GetValue(entry));
                    using (var from = entry.Open())
                    using (var to = target.Open())
                        from.CopyTo(to);
                }
            }
            if (File.Exists(zipPath)) File.Delete(zipPath);
            File.Move(temporary, zipPath);
        }
    }
}
