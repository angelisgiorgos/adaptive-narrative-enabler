// A minimal text adventure on the ANE AI: put it next to an AneAiBehaviour and press Play.
// It starts a story, shows its scene and choices, and can save, cancel and load it.
// Desktop players and the Editor only (the ANE AI starts a process).
#if UNITY_EDITOR || UNITY_STANDALONE
using System;
using System.Collections.Generic;
using System.Text;
using Ane.AI;
using UnityEngine;

public sealed class AneAiTextAdventure : MonoBehaviour
{
    public AneAiBehaviour ane;
    public string profile = "player-1";

    AneView view;
    Dictionary<string, object> lastSave;
    string message = "Starting the ANE AI…";
    Vector2 scroll;
    bool busy;

    void Awake()
    {
        if (ane == null) ane = FindObjectOfType<AneAiBehaviour>();
    }

    async void Start()
    {
        try
        {
            await ane.StartEngineAsync();
            view = await ane.Client.NewGameAsync(new NewGameOptions { Profile = profile });
            message = view.Mission != null ? $"Quest: {view.Mission}" : "Explore as you like.";
        }
        catch (Exception exception)
        {
            message = exception.Message;
        }
    }

    async void Run(Func<System.Threading.Tasks.Task> work)
    {
        if (busy) return;
        busy = true;
        try { await work(); }
        catch (Exception exception) { message = exception.Message; }
        finally { busy = false; }
    }

    void OnGUI()
    {
        GUILayout.BeginArea(new Rect(10, 10, Screen.width - 20, Screen.height - 20));
        GUILayout.Label(message);
        if (view != null)
        {
            GUILayout.Label($"{view.Scene} · health {view.Health}/{view.MaxHealth} · coins {view.Coins} · turn {view.Turn}");
            scroll = GUILayout.BeginScrollView(scroll, GUILayout.Height(Screen.height * 0.4f));
            GUILayout.Label(string.IsNullOrEmpty(view.TurnText) ? view.Description : view.TurnText);
            GUILayout.EndScrollView();
            if (view.Ended)
            {
                GUILayout.Label(view.Won ? $"Quest complete: {view.EndReason}" : $"The end: {view.EndReason}");
            }
            else
            {
                var actions = view.Actions;
                for (var i = 0; i < actions.Count; i++)
                {
                    var index = i;
                    if (GUILayout.Button(actions[i]))
                        Run(async () => view = await ane.Client.ChooseAsync(view.GameId, index));
                }
            }
            GUILayout.BeginHorizontal();
            if (GUILayout.Button("Save"))
                Run(async () => { lastSave = await ane.Client.SaveAsync(view.GameId); message = "Saved."; });
            if (GUILayout.Button("Cancel (save and stop)"))
                Run(async () =>
                {
                    var cancelled = await ane.Client.CancelAsync(view.GameId);
                    lastSave = cancelled.GetObject("savegame");
                    view = null;
                    message = "Cancelled: the session was saved; Load continues it.";
                });
            GUILayout.EndHorizontal();
        }
        if (lastSave != null && GUILayout.Button("Load the last save"))
            Run(async () =>
            {
                view = await ane.Client.LoadGameAsync(lastSave);
                message = $"Loaded ({view.Restored}).";
            });
        GUILayout.EndArea();
    }
}
#endif
