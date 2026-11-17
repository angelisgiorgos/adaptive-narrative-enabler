// Starting, watching and stopping the ANE AI process, and installing it from StreamingAssets.
// Plain .NET (no UnityEngine).
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace Ane.AI
{
    /// <summary>Which standalone runtime runs on this machine.</summary>
    public static class AneAiPlatform
    {
        public static string Current
        {
            get
            {
                if (RuntimeInformation.IsOSPlatform(OSPlatform.Windows)) return "windows-x64";
                if (RuntimeInformation.IsOSPlatform(OSPlatform.OSX))
                    return RuntimeInformation.ProcessArchitecture == Architecture.Arm64 ? "macos-arm64" : "macos-x64";
                if (RuntimeInformation.IsOSPlatform(OSPlatform.Linux)) return "linux-x64";
                throw new PlatformNotSupportedException("The ANE AI runs on Windows, macOS and Linux.");
            }
        }

        public static string ExecutableName(string platform) =>
            platform.StartsWith("windows", StringComparison.Ordinal) ? "ane-ai.exe" : "ane-ai";

        public static bool IsWindows => RuntimeInformation.IsOSPlatform(OSPlatform.Windows);
    }

    /// <summary>
    /// Installs an ANE AI from the folder the export put in StreamingAssets (package.zip and
    /// runtimes/ane-runtime-&lt;platform&gt;.zip) into a writable folder, once per version: zips keep
    /// the runtime whole (hidden files, execute bits, no plugin import) until it is needed.
    /// </summary>
    public static class AneAiInstaller
    {
        public sealed class Installation
        {
            public string Platform;
            public string Executable;
            public string RuntimeDir;
            public string PackageDir;
        }

        public static Installation Install(string sourceDir, string installRoot, string platform = null)
        {
            platform = platform ?? AneAiPlatform.Current;
            var runtimeZip = Path.Combine(sourceDir, "runtimes", $"ane-runtime-{platform}.zip");
            var packageZip = Path.Combine(sourceDir, "package.zip");
            if (!File.Exists(runtimeZip))
                throw new AneAiException($"No ANE AI runtime for {platform}: {runtimeZip} is missing (export it for {platform}).");
            if (!File.Exists(packageZip))
                throw new AneAiException($"No ANE AI package: {packageZip} is missing.");
            var runtimeDir = Path.Combine(installRoot, $"runtime-{platform}-{ShortHash(runtimeZip)}");
            var packageDir = Path.Combine(installRoot, $"package-{ShortHash(packageZip)}");
            ExtractOnce(runtimeZip, runtimeDir);
            ExtractOnce(packageZip, packageDir);
            var executable = Path.Combine(runtimeDir, AneAiPlatform.ExecutableName(platform));
            if (!File.Exists(executable)) throw new AneAiException($"The runtime has no program {executable}.");
            if (!AneAiPlatform.IsWindows)
            {
                Run("/bin/chmod", $"+x \"{executable}\"");
                if (RuntimeInformation.IsOSPlatform(OSPlatform.OSX))
                    Run("/usr/bin/xattr", $"-dr com.apple.quarantine \"{runtimeDir}\"");  // unpacked by us: not a download
            }
            return new Installation { Platform = platform, Executable = executable, RuntimeDir = runtimeDir, PackageDir = packageDir };
        }

        static void ExtractOnce(string zip, string target)
        {
            if (File.Exists(Path.Combine(target, ".complete"))) return;
            var partial = target + ".partial-" + Guid.NewGuid().ToString("N");
            ZipFile.ExtractToDirectory(zip, partial);
            File.WriteAllText(Path.Combine(partial, ".complete"), DateTime.UtcNow.ToString("o"));
            if (Directory.Exists(target)) Directory.Delete(target, true);
            Directory.Move(partial, target);
        }

        static string ShortHash(string path)
        {
            using (var sha = SHA256.Create())
            using (var stream = File.OpenRead(path))
            {
                var hash = sha.ComputeHash(stream);
                var builder = new StringBuilder();
                for (var i = 0; i < 6; i++) builder.Append(hash[i].ToString("x2"));
                return builder.ToString();
            }
        }

        static void Run(string file, string arguments)
        {
            try
            {
                using (var process = Process.Start(new ProcessStartInfo(file, arguments)
                       { UseShellExecute = false, CreateNoWindow = true, RedirectStandardError = true }))
                    process?.WaitForExit(10000);
            }
            catch (Exception) { /* best effort */ }
        }
    }

    public sealed class AneAiLaunchOptions
    {
        /// <summary>The program: ane-ai.exe (Windows) or ane-ai (or python, with LauncherArguments "run.py").</summary>
        public string Executable;
        /// <summary>Arguments before the runtime's own (e.g. "run.py" when Executable is python).</summary>
        public string LauncherArguments;
        /// <summary>The engine package folder (engine.json, config/, data/).</summary>
        public string PackageDir;
        /// <summary>Where profiles, save games and the UNP's content are kept.</summary>
        public string DataDir;
        /// <summary>The UNP's callback: session reports and events are POSTed there (optional).</summary>
        public string UnpCallbackUrl;
        /// <summary>The token every request needs (default: a new random one).</summary>
        public string Token;
        public TimeSpan StartTimeout = TimeSpan.FromSeconds(60);
        /// <summary>Where it runs (default: the program's folder).</summary>
        public string WorkingDirectory;
    }

    /// <summary>
    /// The ANE AI process: started with a random token on a free local port, stopped when this
    /// process ends (it watches our pid and its stdin), on <see cref="StopAsync"/> or Dispose.
    /// </summary>
    public sealed class AneAiProcess : IDisposable
    {
        readonly Process process;
        readonly StringBuilder errors;  // the latest stderr lines (bounded)

        public string Url { get; }
        public int Port { get; }
        public string Token { get; }
        /// <summary>The ready line: event, protocol, url, port, pid, engine, code_version, data_dir.</summary>
        public Dictionary<string, object> Ready { get; }
        public AneAiClient Client { get; }
        /// <summary>Lifecycle events after start ("stopped", ...); raised on a background thread.</summary>
        public event Action<Dictionary<string, object>> Lifecycle;

        public bool HasExited { get { try { return process.HasExited; } catch (InvalidOperationException) { return true; } } }
        public string ErrorOutput { get { lock (errors) return errors.ToString(); } }

        AneAiProcess(Process process, Dictionary<string, object> ready, string token, StringBuilder errors)
        {
            this.process = process;
            this.errors = errors;
            Ready = ready;
            Token = token;
            Url = ready.GetString("url");
            Port = ready.GetInt("port");
            Client = new AneAiClient(Url, token);
        }

        public static async Task<AneAiProcess> StartAsync(AneAiLaunchOptions options, CancellationToken cancellation = default)
        {
            if (options == null || string.IsNullOrEmpty(options.Executable)) throw new ArgumentException("Executable is required.");
            var token = options.Token ?? NewToken();
            var arguments = new StringBuilder();
            if (!string.IsNullOrEmpty(options.LauncherArguments)) arguments.Append(options.LauncherArguments).Append(' ');
            if (!string.IsNullOrEmpty(options.PackageDir)) arguments.Append("--package ").Append(Quote(options.PackageDir)).Append(' ');
            if (!string.IsNullOrEmpty(options.DataDir)) arguments.Append("--data-dir ").Append(Quote(options.DataDir)).Append(' ');
            arguments.Append("serve --port 0 --exit-on-stdin-eof --parent-pid ").Append(Process.GetCurrentProcess().Id);
            if (!string.IsNullOrEmpty(options.UnpCallbackUrl)) arguments.Append(" --unp-url ").Append(Quote(options.UnpCallbackUrl));
            var start = new ProcessStartInfo(options.Executable, arguments.ToString())
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
                WorkingDirectory = options.WorkingDirectory ?? Path.GetDirectoryName(options.Executable) ?? "",
            };
            start.EnvironmentVariables["ANE_RUNTIME_TOKEN"] = token;
            var process = Process.Start(start) ?? throw new AneAiException($"Could not start {options.Executable}.");
            var errorText = new StringBuilder();
            process.ErrorDataReceived += (_, e) =>
            {
                if (e.Data == null) return;
                lock (errorText)
                {
                    errorText.AppendLine(e.Data);
                    if (errorText.Length > 64 * 1024) errorText.Remove(0, errorText.Length - 32 * 1024);  // the latest only
                }
            };
            process.BeginErrorReadLine();

            var readLine = process.StandardOutput.ReadLineAsync();
            var finished = await Task.WhenAny(readLine, Task.Delay(options.StartTimeout, cancellation)).ConfigureAwait(false);
            if (finished != readLine)
            {
                TryKill(process);
                throw new AneAiException($"The ANE AI did not get ready within {options.StartTimeout.TotalSeconds:0} s. {Tail(errorText)}");
            }
            var line = await readLine.ConfigureAwait(false);
            if (line == null)
            {
                process.WaitForExit(5000);
                throw new AneAiException($"The ANE AI stopped before it was ready. {Tail(errorText)}");
            }
            Dictionary<string, object> ready;
            try { ready = AneJson.ParseObject(line); }
            catch (FormatException) { TryKill(process); throw new AneAiException($"Unexpected output from the ANE AI: {line}"); }
            if (ready.GetString("event") == "error")
            {
                TryKill(process);
                throw new AneAiException($"The ANE AI did not start: {ready.GetString("error")}");
            }
            if (ready.GetString("event") != "ready") { TryKill(process); throw new AneAiException($"Unexpected first event: {line}"); }

            var running = new AneAiProcess(process, ready, token, errorText);
            _ = Task.Run(running.ReadLifecycleAsync);
            return running;
        }

        async Task ReadLifecycleAsync()
        {
            try
            {
                string line;
                while ((line = await process.StandardOutput.ReadLineAsync().ConfigureAwait(false)) != null)
                {
                    try { Lifecycle?.Invoke(AneJson.ParseObject(line)); } catch (FormatException) { }
                }
            }
            catch (Exception) { /* the process ended */ }
        }

        /// <summary>
        /// Stop it: POST /shutdown (every session saved, everything delivered) with up to
        /// <paramref name="timeout"/> (default 5 s) for its answer, then up to <paramref name="timeout"/>
        /// for the process to exit; it is killed if it still runs. Returns the shutdown's answer (null
        /// when there was none). Never throws.
        /// </summary>
        public async Task<Dictionary<string, object>> StopAsync(bool save = true, TimeSpan? timeout = null)
        {
            var limit = timeout ?? TimeSpan.FromSeconds(5);
            Dictionary<string, object> result = null;
            if (!HasExited)
            {
                using (var answered = new CancellationTokenSource(limit))
                {
                    try { result = await Client.ShutdownAsync(save, false, answered.Token).ConfigureAwait(false); }
                    catch (Exception) { /* already stopping, gone or too slow: it is stopped below */ }
                }
            }
            try { process.StandardInput.Close(); } catch (Exception) { }
            var wait = (int)limit.TotalMilliseconds;
            var exited = await Task.Run(() => { try { return process.WaitForExit(wait); } catch (Exception) { return true; } })
                                   .ConfigureAwait(false);
            if (!exited) TryKill(process);
            return result;
        }

        /// <summary><see cref="StopAsync"/>, blocking (e.g. from OnApplicationQuit).</summary>
        public Dictionary<string, object> Stop(bool save = true, TimeSpan? timeout = null) =>
            Task.Run(() => StopAsync(save, timeout)).GetAwaiter().GetResult();

        public void Dispose()
        {
            if (!HasExited) TryKill(process);
            Client.Dispose();
            process.Dispose();
        }

        static void TryKill(Process process)
        {
            try { if (!process.HasExited) process.Kill(); } catch (Exception) { }
        }

        static string NewToken()
        {
            var bytes = new byte[24];
            using (var random = RandomNumberGenerator.Create()) random.GetBytes(bytes);
            return Convert.ToBase64String(bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_');
        }

        /// <summary>One argument, quoted for the command line (.NET splits it with the Windows rules on every
        /// system): backslashes before a quote or at the end are doubled, so "C:\pkg\" stays one path.</summary>
        static string Quote(string value)
        {
            var quoted = new StringBuilder("\"");
            var slashes = 0;
            foreach (var c in value)
            {
                if (c == '\\') { slashes++; continue; }
                if (c == '"') quoted.Append('\\', slashes * 2 + 1).Append('"');
                else quoted.Append('\\', slashes).Append(c);
                slashes = 0;
            }
            return quoted.Append('\\', slashes * 2).Append('"').ToString();
        }

        static string Tail(StringBuilder text)
        {
            lock (text)
            {
                var all = text.ToString().Trim();
                return all.Length > 1500 ? "…" + all.Substring(all.Length - 1500) : all;
            }
        }
    }
}
