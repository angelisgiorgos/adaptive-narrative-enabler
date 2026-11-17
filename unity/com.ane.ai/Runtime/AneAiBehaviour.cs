// The ANE AI as a component: add it to a GameObject in the first scene. It installs the ANE AI
// from StreamingAssets/<folder> (package.zip + runtimes/ane-runtime-<platform>.zip) into
// Application.persistentDataPath, starts it, and stops it (every session saved) when the game quits.
using System;
using System.IO;
using System.Threading.Tasks;
using UnityEngine;

namespace Ane.AI
{
    [DisallowMultipleComponent]
    [AddComponentMenu("ANE AI/ANE AI")]
    public sealed class AneAiBehaviour : MonoBehaviour
    {
        [Tooltip("The folder under StreamingAssets that holds package.zip and runtimes/.")]
        public string folder = "ANE-AI";

        [Tooltip("Start the ANE AI in Start(); otherwise call StartEngineAsync().")]
        public bool startOnStart = true;

        [Tooltip("Keep it (and the ANE AI) across scene loads.")]
        public bool dontDestroyOnLoad = true;

        [Tooltip("Save every open session when the game quits (they can be continued later).")]
        public bool saveOnQuit = true;

        [Tooltip("Optional: an HTTP endpoint of yours that receives session reports and events.")]
        public string unpCallbackUrl = "";

        [Tooltip("Seconds to wait for the ANE AI to stop before it is killed.")]
        public float stopTimeoutSeconds = 5f;

        AneAiProcess process;
        Task<AneAiProcess> starting;
        int generation;  // StopEngine() moves it on: a start begun before is not kept

        /// <summary>The running ANE AI (null before it is ready).</summary>
        public AneAiProcess Process => process;
        /// <summary>The protocol client (null before it is ready).</summary>
        public AneAiClient Client => process?.Client;
        public bool IsReady => process != null && !process.HasExited;

        /// <summary>Raised on the main thread when the ANE AI is ready.</summary>
        public event Action<AneAiBehaviour> Ready;
        /// <summary>Raised on the main thread when it could not be started.</summary>
        public event Action<Exception> Failed;

        void Awake()
        {
            if (dontDestroyOnLoad) DontDestroyOnLoad(gameObject);
        }

        async void Start()
        {
            if (!startOnStart) return;
            try { await StartEngineAsync(); }
            catch (Exception) { /* reported through Failed and the console */ }
        }

        /// <summary>Install (once) and start the ANE AI; every caller gets the same start.</summary>
        public Task<AneAiProcess> StartEngineAsync()
        {
            if (starting == null || starting.IsFaulted || starting.IsCanceled) starting = StartInternalAsync();
            return starting;
        }

        async Task<AneAiProcess> StartInternalAsync()
        {
            var mine = generation;
            try
            {
                var source = Path.Combine(Application.streamingAssetsPath, folder);
                var root = Path.Combine(Application.persistentDataPath, "ANE-AI");
                var installation = await Task.Run(() => AneAiInstaller.Install(source, Path.Combine(root, "install")));
                var started = await AneAiProcess.StartAsync(new AneAiLaunchOptions
                {
                    Executable = installation.Executable,
                    PackageDir = installation.PackageDir,
                    DataDir = Path.Combine(root, "data"),
                    UnpCallbackUrl = string.IsNullOrEmpty(unpCallbackUrl) ? null : unpCallbackUrl,
                });
                if (mine != generation)  // stopped (or destroyed) while it was starting
                {
                    await started.StopAsync(false, TimeSpan.FromSeconds(stopTimeoutSeconds));
                    started.Dispose();
                    throw new OperationCanceledException("The ANE AI was stopped before it was ready.");
                }
                process = started;
                Debug.Log($"ANE AI ready at {process.Url} (engine {process.Ready.GetString("engine")}, {installation.Platform}).");
                Ready?.Invoke(this);
                return process;
            }
            catch (OperationCanceledException)
            {
                throw;
            }
            catch (Exception exception)
            {
                Debug.LogError($"The ANE AI could not be started: {exception.Message}");
                Failed?.Invoke(exception);
                throw;
            }
        }

        /// <summary>Stop the ANE AI now (every session saved when saveOnQuit).</summary>
        public void StopEngine()
        {
            var running = process;
            process = null;
            starting = null;
            generation++;
            if (running == null) return;
            try { running.Stop(saveOnQuit, TimeSpan.FromSeconds(stopTimeoutSeconds)); }
            catch (Exception exception) { Debug.LogWarning($"Stopping the ANE AI: {exception.Message}"); }
            finally { running.Dispose(); }
        }

        void OnApplicationQuit() => StopEngine();

        void OnDestroy() => StopEngine();
    }
}
