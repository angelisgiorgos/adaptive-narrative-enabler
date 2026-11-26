// The ANE AI's local HTTP protocol (protocol 1), from C#: every call of narrative_runtime/server.py.
// Plain .NET (no UnityEngine): usable in the Editor, in builds and in tools.
using System;
using System.Collections.Generic;
using System.IO;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace Ane.AI
{
    /// <summary>A request the ANE AI refused or could not serve (Status 0: it could not be reached).</summary>
    public sealed class AneAiException : Exception
    {
        public int Status { get; }
        public IReadOnlyList<string> Errors { get; }
        /// <summary>Why a choice was refused (409): "stale_state" or "action_unavailable"; else null.</summary>
        public string Code { get; }
        /// <summary>A refused choice: the game's state version now (null otherwise).</summary>
        public long? StateVersion { get; }
        /// <summary>A refused choice: the ids of the choices offered now.</summary>
        public IReadOnlyList<string> Offered { get; }
        /// <summary>The choice came from a view the game has moved past: nothing was done; show the current view.</summary>
        public bool IsStale => Code == "stale_state";
        /// <summary>The choice's id is not among those offered now: nothing was done.</summary>
        public bool IsUnavailable => Code == "action_unavailable";

        public AneAiException(string message, int status = 0, IReadOnlyList<string> errors = null, Exception inner = null,
                              string code = null, long? stateVersion = null, IReadOnlyList<string> offered = null)
            : base(message, inner)
        {
            Status = status;
            Errors = errors ?? Array.Empty<string>();
            Code = code;
            StateVersion = stateVersion;
            Offered = offered ?? Array.Empty<string>();
        }
    }

    /// <summary>What to start a session with (POST /games).</summary>
    public sealed class NewGameOptions
    {
        public string Profile;
        public string Start;
        public string Mission;
        public int? Seed;
        public bool Persist = true;
        public string StatsMode;
        public Dictionary<string, object> Stats;
        /// <summary>The player's state from the UNP: stat_points, stat_changes, inventory, coins, health, max_health, variables.</summary>
        public Dictionary<string, object> Player;

        public Dictionary<string, object> ToJson()
        {
            var body = new Dictionary<string, object> { ["persist"] = Persist };
            if (Profile != null) body["profile"] = Profile;
            if (Start != null) body["start"] = Start;
            if (Mission != null) body["mission"] = Mission;
            if (Seed.HasValue) body["seed"] = Seed.Value;
            if (StatsMode != null) body["stats_mode"] = StatsMode;
            if (Stats != null) body["stats"] = Stats;
            if (Player != null) body["player"] = Player;
            return body;
        }
    }

    /// <summary>One choice the player sees (the view's "choices"): take it with
    /// <see cref="AneAiClient.ChooseAsync(AneView, AneChoice)"/>. Its <see cref="Id"/> is stable: it names what
    /// offers the choice ("npc:merchant:haggle", "go:harbor", "approach:merchant"), not its text or position.</summary>
    public sealed class AneChoice
    {
        public readonly Dictionary<string, object> Raw;
        public AneChoice(Dictionary<string, object> raw) { Raw = raw ?? new Dictionary<string, object>(); }

        /// <summary>The stable id (null from an ANE AI older than action ids: choose by <see cref="Name"/> there).</summary>
        public string Id => Raw.GetString("id");
        /// <summary>The text to show (the same as the view's "actions" entry).</summary>
        public string Name => Raw.GetString("name");
        public string Label => Raw.GetString("label") ?? Name;
        /// <summary>place, person, item, event, talk, approach, leave, look, travel, fast_travel.</summary>
        public string Category => Raw.GetString("category");
        public string Owner => Raw.GetString("owner");
        /// <summary>What offers it: npc, location, item or event, and its name.</summary>
        public string SourceKind => Raw.GetObject("source").GetString("kind");
        public string SourceName => Raw.GetObject("source").GetString("name");
        public string Location => Raw.GetString("location");
        /// <summary>Approaching someone and leaving a conversation take no turn.</summary>
        public bool TakesTurn => Raw.GetBool("takes_turn", true);
        /// <summary>once, per_visit or always.</summary>
        public string Repeat => Raw.GetString("repeat");
        public bool OneTime => Raw.GetBool("one_time");
        /// <summary>{item, item_tag, coins, consumes_item, collects_item, triggers_event}: only what applies.</summary>
        public Dictionary<string, object> Requirements => Raw.GetObject("requirements");
        /// <summary>{stat, chance (0-100), bonus}, or null without a skill check.</summary>
        public Dictionary<string, object> SkillCheck => Raw.GetObject("skill_check");
        /// <summary>What failing costs: {health, coins, sent_to}, or null.</summary>
        public Dictionary<string, object> FailureRisk => Raw.GetObject("failure_risk");
    }

    /// <summary>The scene as the player sees it (GET /games/{id}); <see cref="Raw"/> holds every field.</summary>
    public sealed class AneView
    {
        public readonly Dictionary<string, object> Raw;
        public AneView(Dictionary<string, object> raw) { Raw = raw ?? new Dictionary<string, object>(); }

        public string GameId => Raw.GetString("game_id");
        public string Location => Raw.GetString("location");
        public string Scene => Raw.GetString("scene");
        public string Description => Raw.GetString("description");
        public List<string> Actions => Raw.GetStrings("actions");
        /// <summary>The same choices as <see cref="Actions"/>, each with its stable id and what it involves.</summary>
        public List<AneChoice> Choices
        {
            get
            {
                var choices = new List<AneChoice>();
                foreach (var item in Raw.GetList("choices")) choices.Add(new AneChoice(item as Dictionary<string, object>));
                return choices;
            }
        }
        /// <summary>Changes with every turn, player update and sync; sent back with a choice so that a choice from
        /// an older view is refused (null from an ANE AI older than action ids).</summary>
        public long? StateVersion => Raw.TryGetValue("state_version", out var value) && value != null
            ? Raw.GetLong("state_version") : (long?)null;
        public List<string> Npcs => Raw.GetStrings("npcs");
        public List<string> Items => Raw.GetStrings("items");
        public List<string> Inventory => Raw.GetStrings("inventory");
        public int Health => Raw.GetInt("health");
        public int MaxHealth => Raw.GetInt("max_health");
        public int Coins => Raw.GetInt("coins");
        public string Mission => Raw.GetString("mission");
        public string MissionStatus => Raw.GetString("mission_status");
        public bool Ended => Raw.GetBool("ended");
        public bool Won => Raw.GetBool("won");
        public string EndReason => Raw.GetString("end_reason");
        public int Turn => Raw.GetInt("turn_number");
        /// <summary>The story text of the last turn (after a choice).</summary>
        public string TurnText => Raw.GetString("turn");
        /// <summary>After loading a save game: "exact" or "parameters".</summary>
        public string Restored => Raw.GetString("restored");
    }

    public sealed class AneAiClient : IDisposable
    {
        readonly HttpClient http;

        public string Url { get; }

        public AneAiClient(string url, string token, TimeSpan? timeout = null)
        {
            Url = url.TrimEnd('/');
            http = new HttpClient { BaseAddress = new Uri(Url + "/"), Timeout = timeout ?? TimeSpan.FromMinutes(5) };
            // The ANE AI answers one request per connection (HTTP/1.0, Connection: close). Asking for that too
            // keeps HttpClient from pooling a connection the ANE AI is closing: a request sent on it right after
            // the last answer would fail ("An error occurred while sending the request").
            http.DefaultRequestHeaders.ConnectionClose = true;
            if (!string.IsNullOrEmpty(token))
                http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
        }

        public void Dispose() => http.Dispose();

        public async Task<Dictionary<string, object>> SendAsync(string method, string path, object body = null,
                                                              CancellationToken cancellation = default)
        {
            var request = new HttpRequestMessage(new HttpMethod(method), path.TrimStart('/'));
            if (body != null)
                request.Content = new StringContent(AneJson.Serialize(body), Encoding.UTF8, "application/json");
            HttpResponseMessage response;
            string text;
            try
            {
                response = await http.SendAsync(request, cancellation).ConfigureAwait(false);
                text = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
            }
            catch (HttpRequestException exception)
            {
                throw new AneAiException($"The ANE AI at {Url} could not be reached: {exception.Message}", 0, null, exception);
            }
            catch (IOException exception)
            {
                throw new AneAiException($"The ANE AI at {Url} closed the connection: {exception.Message}", 0, null, exception);
            }
            catch (TaskCanceledException exception) when (!cancellation.IsCancellationRequested)
            {
                throw new AneAiException($"The ANE AI at {Url} did not answer within {http.Timeout.TotalSeconds:0} s.", 0, null, exception);
            }
            Dictionary<string, object> json;
            try { json = AneJson.ParseObject(text); }
            catch (FormatException) { json = new Dictionary<string, object> { ["error"] = text }; }
            if (!response.IsSuccessStatusCode)
            {
                long? version = json.TryGetValue("state_version", out var sent) && sent != null
                    ? json.GetLong("state_version") : (long?)null;
                throw new AneAiException(json.GetString("error", response.ReasonPhrase), (int)response.StatusCode,
                                         json.GetStrings("errors"), null, json.GetString("code"), version,
                                         json.GetStrings("offered"));
            }
            return json;
        }

        static string Part(string value) => Uri.EscapeDataString(value);

        // ---- the engine ----------------------------------------------------------------------
        public Task<Dictionary<string, object>> HealthAsync() => SendAsync("GET", "health");
        public Task<Dictionary<string, object>> EngineAsync() => SendAsync("GET", "engine");
        /// <summary>Save (or end) every session, deliver everything and stop the process.</summary>
        public Task<Dictionary<string, object>> ShutdownAsync(bool save = true, bool endSessions = false,
                                                              CancellationToken cancellation = default) =>
            SendAsync("POST", "shutdown", new Dictionary<string, object> { ["save"] = save, ["end_sessions"] = endSessions },
                      cancellation);

        // ---- the world as parameters, and content from the UNP --------------------------------
        /// <summary>Locations, npcs, items, events, missions, side_quests, factions, stats, variables, routes (ane.world/1).</summary>
        public Task<Dictionary<string, object>> WorldAsync() => SendAsync("GET", "world");
        public Task<Dictionary<string, object>> EntityAsync(string kind, string name) =>
            SendAsync("GET", $"world/{Part(kind)}/{Part(name)}");
        /// <summary>World content from the UNP: upsert, update, delete, add_actions, tags, stats, persist (422: Errors lists the problems).</summary>
        public Task<Dictionary<string, object>> PatchWorldAsync(Dictionary<string, object> patch) => SendAsync("POST", "world/patch", patch);
        public Task<Dictionary<string, object>> ResetWorldAsync() => SendAsync("POST", "world/reset", new Dictionary<string, object>());
        public Task<Dictionary<string, object>> ProgressionAsync(string profile) => SendAsync("GET", $"progression/{Part(profile)}");

        // ---- sessions ----------------------------------------------------------------------------
        public Task<Dictionary<string, object>> GamesAsync() => SendAsync("GET", "games");

        public async Task<AneView> NewGameAsync(NewGameOptions options = null) =>
            new AneView(await SendAsync("POST", "games", (options ?? new NewGameOptions()).ToJson()).ConfigureAwait(false));

        /// <summary>Continue a session from a save game (ane.savegame/1): exactly, or rebuilt from its parameters.</summary>
        public async Task<AneView> LoadGameAsync(Dictionary<string, object> savegame, string profile = null)
        {
            var body = new Dictionary<string, object> { ["savegame"] = savegame };
            if (profile != null) body["profile"] = profile;
            return new AneView(await SendAsync("POST", "games", body).ConfigureAwait(false));
        }

        public async Task<AneView> GameAsync(string gameId) =>
            new AneView(await SendAsync("GET", $"games/{Part(gameId)}").ConfigureAwait(false));

        /// <summary>The session's parameters (ane.session_state/1).</summary>
        public Task<Dictionary<string, object>> StateAsync(string gameId) => SendAsync("GET", $"games/{Part(gameId)}/state");

        /// <summary>Take a choice of a view: by its id, with the view's state version (refused, and nothing done, if
        /// the game has moved past that view: <see cref="AneAiException.IsStale"/>). From an ANE AI older than
        /// action ids: by its name.</summary>
        public Task<AneView> ChooseAsync(AneView view, AneChoice choice) =>
            string.IsNullOrEmpty(choice.Id) ? ChooseAsync(view.GameId, choice.Name)
                                            : ChooseIdAsync(view.GameId, choice.Id, view.StateVersion);

        /// <summary>Take the choice with this stable id (a view's Choices[i].Id). With stateVersion (that view's
        /// StateVersion), a choice from a view the game has moved past is refused: 409, IsStale. An id not offered
        /// now: 409, IsUnavailable. Nothing is done then; the exception's Offered lists the current ids.</summary>
        public async Task<AneView> ChooseIdAsync(string gameId, string actionId, long? stateVersion = null)
        {
            var body = new Dictionary<string, object> { ["action_id"] = actionId };
            if (stateVersion.HasValue) body["state_version"] = stateVersion.Value;
            return new AneView(await SendAsync("POST", $"games/{Part(gameId)}/choose", body).ConfigureAwait(false));
        }

        /// <summary>Take a choice by its name (two with the same name: the first; prefer ids).</summary>
        public async Task<AneView> ChooseAsync(string gameId, string action) =>
            new AneView(await SendAsync("POST", $"games/{Part(gameId)}/choose",
                new Dictionary<string, object> { ["action"] = action }).ConfigureAwait(false));

        /// <summary>Take a choice by its index in the view's Actions.</summary>
        public async Task<AneView> ChooseAsync(string gameId, int index) =>
            new AneView(await SendAsync("POST", $"games/{Part(gameId)}/choose",
                new Dictionary<string, object> { ["action"] = index }).ConfigureAwait(false));

        /// <summary>Set the player's stat_points, stat_changes, inventory, add_items, remove_items, coins, health, max_health or variables.</summary>
        public async Task<AneView> UpdatePlayerAsync(string gameId, Dictionary<string, object> player) =>
            new AneView(await SendAsync("PATCH", $"games/{Part(gameId)}/player", player).ConfigureAwait(false));

        /// <summary>What happened in the UNP: {"type": action_done | item_gained | item_lost | npc_removed | location_visited | coins_changed | health_changed, ...}.</summary>
        public async Task<AneView> SyncAsync(string gameId, params Dictionary<string, object>[] events) =>
            new AneView(await SendAsync("POST", $"games/{Part(gameId)}/sync",
                new Dictionary<string, object> { ["events"] = events }).ConfigureAwait(false));

        /// <summary>The save game (ane.savegame/1); the session goes on.</summary>
        public Task<Dictionary<string, object>> SaveAsync(string gameId) =>
            SendAsync("POST", $"games/{Part(gameId)}/save", new Dictionary<string, object>());

        /// <summary>Cancel the session: {status (outcome suspended), savegame}; the session is released.</summary>
        public Task<Dictionary<string, object>> CancelAsync(string gameId) =>
            SendAsync("POST", $"games/{Part(gameId)}/cancel", new Dictionary<string, object>());

        /// <summary>End the session (it counts: the player's progression is saved and reported).</summary>
        public async Task<AneView> EndAsync(string gameId) =>
            new AneView(await SendAsync("POST", $"games/{Part(gameId)}/end", new Dictionary<string, object>()).ConfigureAwait(false));

        public Task<Dictionary<string, object>> ReportAsync(string gameId) => SendAsync("GET", $"games/{Part(gameId)}/report");

        public async Task<string> TranscriptAsync(string gameId) =>
            (await SendAsync("GET", $"games/{Part(gameId)}/transcript").ConfigureAwait(false)).GetString("transcript");

        public Task<Dictionary<string, object>> ForgetAsync(string gameId) => SendAsync("DELETE", $"games/{Part(gameId)}");
    }
}
