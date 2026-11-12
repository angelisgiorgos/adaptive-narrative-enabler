"""Gradio UI of the Adaptive Narrative Studio: a client of the REST API.

Every operation goes through an endpoint of the API (api.py). This file only
builds the interface and shows what the API returns; validation, storage,
authoring, evolution, play and records all live behind the API.

The connection (``StudioAPI``, below):

* ``ANE_API_URL`` set (e.g. ``http://hamlet-ane-api:8004``): HTTP to that API, so
  the studio and other clients share one engine (sessions, runs, records).
* not set: the same API, served inside this process (no separate server).

Every request says it comes from the studio (``X-ANE-Client: studio``), so runs and
stories are recorded with the studio as their origin. Errors arrive as ``APIError``
with the API's message and are shown as UI errors. Files the API offers for
download are fetched into a temporary folder, since the file widgets need local paths.

    Tab 1 · World editor           /v1/entities/..., /v1/documents/missions
    Tab 2 · Simulation settings    /v1/config/..., /v1/settings, /v1/documents/..., /v1/variables/...,
                                   /v1/rules, /v1/hyperparameters
    Tab 3 · Weights                /v1/weights
    Tab 4 · Simulate & play        /v1/jobs/..., /v1/engines/load, /v1/sessions/..., /v1/stats/...,
                                   /v1/learning, /v1/exports, /v1/records/...
    Tab 5 · Engine logic & app     /v1/logic/..., /v1/app-settings
"""

from __future__ import annotations

import atexit
import functools
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import gradio as gr
import pandas as pd
import yaml


# ---- the connection to the REST API ------------------------------------------------------------

CLIENT_HEADER = {"X-ANE-Client": "studio"}


class APIError(Exception):
    """A request the API refused or could not serve; the message is the API's."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class StudioAPI:
    def __init__(self, base_url=None, timeout=900):
        base_url = base_url if base_url is not None else os.environ.get("ANE_API_URL", "").strip()
        if base_url:
            import httpx

            self.mode = "http"
            self.base_url = base_url.rstrip("/")
            self._client = httpx.Client(base_url=self.base_url, timeout=timeout, headers=CLIENT_HEADER)
        else:
            from fastapi.testclient import TestClient

            import api  # served inside this process (the only import of the API's code)

            self.mode = "in-process"
            self.base_url = "in-process"
            self._client = TestClient(api.api, raise_server_exceptions=False, headers=CLIENT_HEADER)
        self._downloads = Path(tempfile.mkdtemp(prefix="ane-studio-"))
        atexit.register(shutil.rmtree, self._downloads, ignore_errors=True)

    def close(self):
        self._client.close()

    # ---- requests --------------------------------------------------------------------------

    def _send(self, method, path, **kwargs):
        """The response of a request; APIError when it fails or the API refuses it."""
        try:
            response = self._client.request(method, path, **kwargs)
        except Exception as exc:  # connection refused, timeout, ...
            raise APIError(f"The API at {self.base_url} could not be reached: {exc}") from exc
        if response.status_code >= 400:
            raise APIError(_error_message(response), response.status_code)
        return response

    def request(self, method, path, **kwargs):
        """The JSON a request returns (or the response, for anything else)."""
        response = self._send(method, path, **kwargs)
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response

    def get(self, path, **params):
        return self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def put(self, path, body=None, **params):
        return self.request("PUT", path, json=body, params={k: v for k, v in params.items() if v is not None})

    def post(self, path, body=None, files=None, **params):
        params = {k: v for k, v in params.items() if v is not None}
        if files is not None:
            return self.request("POST", path, files=files, params=params)
        return self.request("POST", path, json=body, params=params)

    def delete(self, path, **params):
        return self.request("DELETE", path, params={k: v for k, v in params.items() if v is not None})

    def upload(self, path, files, **params):
        """POST local files: ``files`` maps a form field to a local path (None: left out)."""
        handles = {field: open(local, "rb") for field, local in files.items() if local}
        try:
            return self.post(path, files={field: (Path(handle.name).name, handle) for field, handle in handles.items()},
                             **params)
        finally:
            for handle in handles.values():
                handle.close()

    def download(self, url, filename=None):
        """Fetch a file the API offers (a path such as /v1/artifacts/<id>) into a local file."""
        if not url:
            return None
        response = self._send("GET", url)
        name = filename or _filename(response) or Path(url.split("?")[0]).name or "download"
        target = Path(tempfile.mkdtemp(dir=self._downloads)) / name  # the API's file name, never overwritten
        target.write_bytes(response.content)
        return str(target)

    def wait(self, job, on_progress=None, interval=0.5):
        """Poll a job (POST /v1/jobs/...) until it ends; returns its result or raises its error."""
        while True:
            job = self.get(f"/v1/jobs/{job['id']}")
            if on_progress is not None and job.get("progress"):
                on_progress(job["progress"])
            if job["status"] == "finished":
                return job["result"]
            if job["status"] == "failed":
                raise APIError(job["error"] or "The job failed.")
            time.sleep(interval)


# ---- YAML for the studio's code editors -------------------------------------------------------

def to_yaml(value, flow=False):
    """YAML text of a value (``flow``: on one line, e.g. a condition)."""
    if value in (None, {}, []):
        return ""
    return yaml.safe_dump(value, default_flow_style=flow, sort_keys=False, allow_unicode=True).strip() + ("" if flow else "\n")


def from_yaml(text, what="the YAML"):
    """The value of YAML text, or APIError with a readable message."""
    try:
        return yaml.safe_load(text or "")
    except yaml.YAMLError as exc:
        raise APIError(f"{what} is not valid YAML: {exc}") from exc


def _error_message(response):
    try:
        detail = response.json().get("detail")
    except ValueError:
        return f"{response.status_code}: {response.text[:500]}"
    if isinstance(detail, list):  # request validation errors
        return "; ".join(f"{'.'.join(str(p) for p in item.get('loc', [])[1:])}: {item.get('msg')}" for item in detail)
    return str(detail)


def _filename(response):
    match = re.search(r'filename="?([^";]+)"?', response.headers.get("content-disposition", ""))
    return match.group(1) if match else None


API = StudioAPI()


def ui(function):
    """Show the API's errors as UI error messages."""
    @functools.wraps(function)
    def handler(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except APIError as exc:
            raise gr.Error(str(exc)) from exc
    return handler


def _path(*parts):
    """An API path whose name parts (entries with spaces, apostrophes, ...) are escaped."""
    return "/".join(quote(str(part), safe="") if index else str(part) for index, part in enumerate(parts))


CSS = """
.hero {padding: 1.25rem; border-radius: 18px; background: linear-gradient(120deg,#17233f,#4a244f); color:#fff}
.status {border-left: 4px solid #8b5cf6; padding-left: 1rem}
"""
EMPTY_ENTITY = {"name": "", "tags": [], "descriptions": [], "goals": [], "unique": False, "encounter_event": None,
                "encounter_repeat": False, "enabled_if": None, "title": None, "stat_bonus": None,
                "start_eligible": False, "start_weight": 1.0}


# ---- tab 1 · world editor (/v1/entities, /v1/documents/missions) -----------------------------

def _entity_names(kind):
    return API.get(f"/v1/entities/{kind}", fields="names")["names"]


def _entity(kind, name):
    return API.get(_path(f"/v1/entities/{kind}", name)) if name else dict(EMPTY_ENTITY)


def _event_choices():
    return [("None", "")] + [(name, name) for name in _entity_names("Events")]


def _entry_values(kind, entity):
    """The editor fields of an entry: name, tags, descriptions, goals and the per-kind options."""
    npc, location = kind == "NPCs", kind == "Locations"
    start_weight = entity.get("start_weight")
    return (
        entity["name"], entity["tags"], "\n".join(entity["descriptions"]), "\n".join(entity["goals"]),
        gr.Checkbox(value=bool(entity.get("unique")), visible=npc),
        gr.Dropdown(choices=_event_choices(), value=entity.get("encounter_event") or "", visible=npc),
        gr.Checkbox(value=bool(entity.get("encounter_repeat")), visible=npc),
        gr.Textbox(value=to_yaml(entity.get("enabled_if"), flow=True), visible=True),
        gr.Textbox(value=entity.get("title") or "", visible=kind == "Events"),
        gr.Textbox(value=to_yaml(entity.get("stat_bonus"), flow=True), visible=kind == "Items"),
        gr.Checkbox(value=bool(entity.get("start_eligible")), visible=location),
        gr.Number(value=1.0 if start_weight is None else start_weight, visible=location),
    )


@ui
def entity_names(kind):
    names = _entity_names(kind)
    value = names[0] if names else None
    return gr.Dropdown(choices=names, value=value), *_entry_values(kind, _entity(kind, value))


@ui
def load_entity_ui(kind, name):
    return _entry_values(kind, _entity(kind, name))


def new_entity():
    return gr.Dropdown(value=None), "", [], "", "", False, "", False, "", "", "", False, 1.0


def _lines(text):
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


@ui
def save_entity_ui(kind, selected_name, name, tags, descriptions, goals, unique, encounter_event, encounter_repeat,
                   enabled_if, title, stat_bonus, start_eligible, start_weight):
    name = (name or "").strip()
    if not name:
        raise gr.Error("Name is required.")
    npc, location = kind == "NPCs", kind == "Locations"
    body = {
        "name": name, "tags": list(tags or []), "descriptions": _lines(descriptions), "goals": _lines(goals),
        "enabled_if": from_yaml(enabled_if, "`Enabled if`") or {},  # {} removes it
        "unique": bool(unique) if npc else None,
        "encounter_event": (encounter_event or "") if npc else None,
        "encounter_repeat": bool(encounter_repeat) if npc else None,
        "title": (title or "") if kind == "Events" else None,
        "stat_bonus": (from_yaml(stat_bonus, "`Stat bonus`") or {}) if kind == "Items" else None,
        "start_eligible": bool(start_eligible) if location else None,
        "start_weight": start_weight if location else None,
    }
    saved = API.put(_path(f"/v1/entities/{kind}", selected_name or name), body)
    return gr.Dropdown(choices=_entity_names(kind), value=saved["name"]), f"Saved **{saved['name']}**."


@ui
def delete_entity_ui(kind, selected_name):
    if not selected_name:
        raise gr.Error("Choose an entry to delete.")
    result = API.delete(_path(f"/v1/entities/{kind}", selected_name))
    names = result["entities"]
    value = names[0] if names else None
    return gr.Dropdown(choices=names, value=value), *_entry_values(kind, _entity(kind, value)), result["message"]


@ui
def load_entity_actions_yaml(kind, name):
    return API.get(_path(f"/v1/entities/{kind}", name, "actions", "yaml"))["yaml"] if name else "[]\n"


@ui
def save_entity_actions_ui(kind, name, yaml_text):
    if not name:
        raise gr.Error("Select an entry first.")
    result = API.put(_path(f"/v1/entities/{kind}", name, "actions", "yaml"), {"yaml": yaml_text})
    return result["yaml"], result["message"]


@ui
def load_entity_policy_yaml(kind, name):
    return API.get(_path(f"/v1/entities/{kind}", name, "policy", "yaml"))["yaml"] if name else "# Events and items only.\n"


@ui
def save_entity_policy_ui(kind, name, yaml_text):
    if not name:
        raise gr.Error("Select an entry first.")
    result = API.put(_path(f"/v1/entities/{kind}", name, "policy", "yaml"), {"yaml": yaml_text})
    return result["yaml"], result["message"]


def _action_choices(kind, name):
    actions = API.get(_path(f"/v1/entities/{kind}", name, "actions"))["actions"] if name else []
    return [(f"{index + 1}. {action.get('name', 'Action')}", index) for index, action in enumerate(actions)]


@ui
def action_picker(kind, name):
    """The entry's actions to pick from, with the repetition controls reset."""
    return gr.Dropdown(choices=_action_choices(kind, name), value=None), REFERENCE["repeat_default"], False


@ui
def load_action_repetition(kind, name, index):
    if not name or index is None:
        return REFERENCE["repeat_default"], False
    repetition = API.get(_path(f"/v1/entities/{kind}", name, "actions", index, "repetition"))
    return repetition["repeat"], repetition["retry_on_failure"]


@ui
def save_repetition_ui(kind, name, index, repeat, retry):
    if not name or index is None:
        raise gr.Error("Select an action first.")
    result = API.put(_path(f"/v1/entities/{kind}", name, "actions", index, "repetition"),
                     {"repeat": repeat or REFERENCE["repeat_default"], "retry_on_failure": bool(retry)})
    return result["yaml"], result["message"]


# ---- configuration documents (/v1/documents/{name}) -------------------------------------------

@ui
def load_document(name, profile=None):
    return API.get(f"/v1/documents/{name}", profile=profile)["yaml"]


@ui
def save_document(name, yaml_text, profile=None):
    result = API.put(f"/v1/documents/{name}", {"yaml": yaml_text}, profile=profile)
    return result["yaml"], result["message"]


def load_missions_yaml():
    return load_document("missions")


def save_missions_yaml(text):
    return save_document("missions", text)


def load_messages_yaml():
    return load_document("messages")


def save_messages_yaml(text):
    return save_document("messages", text)


def load_variables_yaml():
    return load_document("variables")


def save_variables_yaml(text):
    return save_document("variables", text)


def load_stats_yaml():
    return load_document("stats")


def save_stats_yaml(text):
    return save_document("stats", text)


def stored_values_yaml(profile):
    return load_document("stored_values", profile or None)


def save_stored_values_yaml(profile, text):
    return save_document("stored_values", text, profile or None)


@ui
def reset_stored_values_ui(profile):
    result = API.post("/v1/variables/reset/yaml", profile=profile or None)
    return result["yaml"], result["message"]


# ---- tab 2 · settings (/v1/config, /v1/settings, /v1/rules, /v1/hyperparameters) -------------

SETTING_FIELDS = ["pop_size", "generations", "episodes_per_genome", "max_steps", "initial_health", "initial_coins",
                  "max_actions", "deterministic", "seed", "max_health"]


def _setting_values(settings):
    return tuple(settings[field] for field in SETTING_FIELDS)


@ui
def upload_config(path):
    if not path:
        raise gr.Error("Choose a YAML configuration file first.")
    result = API.upload("/v1/config/upload", {"file": path})
    return (*_setting_values(result["settings"]), "✅ " + result["message"])


@ui
def use_builtin_config():
    result = API.post("/v1/config/reset")
    return (*_setting_values(result["settings"]), result["message"])


@ui
def save_settings(*values):
    body = dict(zip(SETTING_FIELDS, values))
    if any(body[field] is None for field in SETTING_FIELDS if field not in ("seed", "max_health")):
        raise gr.Error("Every simulation setting needs a numeric value.")
    body = {key: (int(value) if isinstance(value, float) and key not in ("deterministic",) else value)
            for key, value in body.items()}
    return API.put("/v1/settings", body)["message"]


@ui
def save_all_hyperparameters(yaml_text):
    result = API.put("/v1/hyperparameters", {"yaml": yaml_text})
    return (*_setting_values(result["settings"]), result["yaml"], result["message"])


@ui
def load_rules_ui():
    rules = API.get("/v1/rules")
    return tuple(rules[field] for field in REFERENCE["rule_fields"])


@ui
def save_rules_ui(*values):
    API.put("/v1/rules", dict(zip(REFERENCE["rule_fields"], values)))
    return "Saved the story rules."


@ui
def save_weights(paths, *values):
    result = API.put("/v1/weights", {"values": dict(zip(paths, values))})
    return result["message"]


# ---- tab 4 · evolution and play (/v1/jobs, /v1/sessions, /v1/engines, /v1/exports, /v1/records) -

def _learning_outputs(learning):
    """Learning curve rows and the learned-traits table of a learning summary."""
    if not learning:
        return pd.DataFrame(columns=["generation", "score", "series"]), pd.DataFrame(
            columns=["trait", "meaning", "evolved", "population", "tendency"])
    rows = []
    for stats in learning.get("history", []):
        rows += [
            {"generation": stats["generation"], "score": stats["validation"], "series": "benchmark (champion)"},
            {"generation": stats["generation"], "score": stats["best"], "series": "best this generation"},
            {"generation": stats["generation"], "score": stats["mean"], "series": "population average"},
        ]
    traits = [
        {"trait": t["trait"], "meaning": t["meaning"], "evolved": t["value"],
         "population": t["population_mean"], "tendency": t["tendency"]}
        for t in learning.get("learned_traits", [])
    ]
    return pd.DataFrame(rows), pd.DataFrame(traits)


def _choices(actions, details=None):
    """The choices as radio options: each labelled with the check it involves (the value stays
    the action's name, which the API takes)."""
    checks = {item["name"]: item.get("check") for item in details or []}
    options = [(name + (f"  ·  {checks[name]['stat']} {checks[name]['chance']}%" if checks.get(name) else ""), name)
               for name in actions]
    return gr.Radio(choices=options, value=actions[0] if actions else None, interactive=bool(actions))


ANY_QUEST = "Any quest (the story draws one)"


def _player(profile, stats_choice, stats_points, mission=None):
    """The player part of a request: profile, the quest to play (else drawn by the story),
    and stats (story default, a mode, or own points)."""
    player = {"profile": profile or None, "mission": mission or None}
    if stats_choice == "defined":
        points = from_yaml(stats_points, "The stat points")
        if not isinstance(points, dict):
            raise APIError("The stat points must be a mapping of stat names to points.")
        player["stats"] = points
    elif stats_choice not in (None, "", "story default"):
        player["stats_mode"] = stats_choice
    return player


def quest_markdown(quest):
    """The quest log of a game: the quest, its stages (done, now, still to come) and what to do now."""
    if not quest:
        return "### Quest\nThis story has no quest: explore as you like."
    marks = {"done": "✅", "current": "▶️", "todo": "▫️"}
    lines = [f"### Quest: {quest['title']}", quest.get("description") or ""]
    for stage in quest.get("stages") or []:
        line = f"{marks.get(stage['state'], '▫️')} **{stage['title']}**"
        if stage["state"] == "current" and stage.get("description"):
            line += f" — {stage['description']}"
        lines.append(line)
    if quest["status"] != "in progress":
        lines.append(f"**{quest['status'].capitalize()}.**")
    elif quest.get("todo"):
        lines.append("**To do now:** " + "; ".join(quest["todo"]))
    return "\n\n".join(line for line in lines if line)


def people_markdown(game):
    """Everyone met in the story: where, how often spoken with, and who is hostile or gone."""
    met = game.get("people_met") or []
    here = {person["name"]: person for person in game.get("people") or []}
    lines = ["### People met"] + ([] if met else ["Nobody yet."])
    for person in met:
        notes = []
        if person["name"] in here:
            notes.append("here now")
            if here[person["name"]].get("quest"):
                notes.append("★ can help with your quest")
        if person["hostile"]:
            notes.append("⚠ hostile")
        if person["gone"]:
            notes.append("gone from the story")
        if person["talks"]:
            notes.append(f"spoken with {person['talks']}×")
        if person.get("ally"):
            notes.append("♥ ally")
        lines.append(f"- **{person['name']}** · met at {', '.join(person['where'])}"
                     + (f" · {' · '.join(notes)}" if notes else ""))
    return "\n".join(lines) + "\n\n" + map_markdown(game.get("map"))


def map_markdown(known):
    """The map as the player knows it: each known place with where its paths lead."""
    if not known:
        return ""
    paths = {}
    for a, b in known.get("paths", []):
        paths.setdefault(a, []).append(b)
        paths.setdefault(b, []).append(a)
    for a, b in known.get("one_way", []):
        paths.setdefault(a, []).append(f"{b} (one way)")
    lines = ["### Map"]
    for place in known.get("places", []):
        name = place["name"]
        mark = "📍 " if name == known.get("current") else ""
        state = "" if place["visited"] else " *(not visited)*"
        locked = " 🔒" if place["locked"] else ""
        lines.append(f"- {mark}**{name}**{locked}{state} → {', '.join(sorted(paths.get(name, []))) or 'no known way on'}")
    return "\n".join(lines)


def _game_outputs(game):
    """What the play tab shows of a game the API started (simulation, loaded or recorded engine)."""
    curve, traits = _learning_outputs(game.get("learning"))
    generations = [str(stats["generation"]) for stats in (game.get("learning") or {}).get("history", [])]
    return (game["session_id"], game["report"], game["scene"], _choices(game["actions"], game.get("choices")),
            game.get("last_turn", ""),
            API.download(game["config_download_url"]), API.download(game["engine_download_url"]),
            quest_markdown(game.get("quest")), people_markdown(game), curve, traits,
            game.get("run_id"), gr.Dropdown(choices=generations, value=generations[-1] if generations else None))


def _job_progress(progress, label="Generation"):
    def report(state):
        benchmark = state.get("validation")
        progress((state["generation"], state["total"]), desc=f"{label} {state['generation']}/{state['total']}"
                 + (f": benchmark {benchmark:.0f}" if isinstance(benchmark, (int, float)) else ""))
    return report


@ui
def learning_memory_text():
    memory = API.get("/v1/learning")
    if not memory["size"]:
        return "The AI has not learned anything yet: the next run starts from random storytellers."
    best = max(entry["score"] for entry in memory["hall_of_fame"])
    return (f"The AI remembers **{memory['size']}** storyteller(s) from earlier runs (best score {best:.1f}). "
            "New runs continue from them when *Continue learning* is on.")


@ui
def forget_learning_ui():
    API.delete("/v1/learning")
    return learning_memory_text()


@ui
def run_simulation_ui(candidate_count, generations, simulation_steps, deterministic, profile, stats_choice,
                      stats_points, episodes, warm_start, mission=None, progress=gr.Progress()):
    job = API.post("/v1/jobs/simulations", {
        "candidate_count": int(candidate_count), "generations": int(generations),
        "simulation_steps": int(simulation_steps), "deterministic": bool(deterministic),
        "episodes": int(episodes) if episodes else None, "warm_start": bool(warm_start),
        **_player(profile, stats_choice, stats_points, mission),
    })
    outputs = _game_outputs(API.wait(job, _job_progress(progress)))
    return (*outputs[:11], learning_memory_text(), *outputs[11:])


@ui
def load_best_engine_ui(uploaded_file, model_parameters_file, profile, stats_choice, stats_points, mission=None):
    if not uploaded_file:
        raise gr.Error("Choose an engine model, an engine ZIP or a configuration YAML first.")
    player = _player(profile, stats_choice, stats_points, mission)
    game = API.upload("/v1/engines/load", {"file": uploaded_file, "parameters": model_parameters_file},
                      profile=player["profile"], stats_mode=player.get("stats_mode"), mission=player["mission"],
                      stats=json.dumps(player["stats"]) if "stats" in player else None)
    return _game_outputs(game)[:11]


@ui
def play_action_ui(token, selected_action):
    if not token:
        raise gr.Error("Run a simulation first to start a game.")
    state = API.post(_path("/v1/sessions", token, "actions"), {"action": selected_action or ""})
    return state["scene"], _choices(state["actions"], state.get("choices")), state["last_turn"], \
        quest_markdown(state.get("quest")), people_markdown(state)


def quest_overview(profile):
    """(choices of the quest picker, Markdown overview of the world's quests) for this profile:
    the quests open to it now can be chosen; the others are listed, struck through."""
    quests = API.get("/v1/missions/playable", profile=profile or None)["missions"]
    choices = [(ANY_QUEST, "")] + [(q["title"], q["key"]) for q in quests if q["available"]]
    lines = [f"**Quests in this world** (for profile `{profile or 'default'}`):"]
    for quest in quests:
        stages = " → ".join(quest["stages"])
        lines.append(f"- {'**' + quest['title'] + '**' if quest['available'] else '~~' + quest['title'] + '~~ (not open now)'}"
                     + (f" — {quest['description']}" if quest["description"] else "")
                     + (f" *Stages:* {stages}" if stages else ""))
    return choices, "\n".join(lines)


@ui
def playable_quests_ui(profile):
    choices, text = quest_overview(profile)
    return gr.Dropdown(choices=choices, value=""), text


@ui
def stats_points_text(mode):
    return to_yaml(API.post("/v1/stats/allocate", {"mode": mode})["points"])


def check_points_text(points_text):
    try:
        points = from_yaml(points_text) or {}
        result = API.post("/v1/stats/allocate", {"points": points if isinstance(points, dict) else {}})
    except APIError as exc:
        return f"⚠️ {exc}"
    if result["errors"]:
        return "⚠️ " + " ".join(result["errors"])
    values = ", ".join(f"{k} {v}" for k, v in result["values"].items())
    return f"✅ {sum(result['points'].values())} of {result['budget']['total_points']} points · values: {values}"


EXPORT_SOURCES = {
    "This run's best": "run",
    "A generation of this run": "run",
    "Best remembered storyteller": "memory",
    "This game's storyteller": "game",
}


@ui
def export_engine_ui(source_label, run_id, generation, token, include_memory, name):
    source = EXPORT_SOURCES.get(source_label, "run")
    result = API.post("/v1/exports", {
        "source": source, "run_id": run_id or None,
        "generation": int(generation) if source_label == "A generation of this run" and generation not in (None, "") else None,
        "session_id": token or None, "include_memory": bool(include_memory), "name": name or None,
    })
    score = f"{result['score']:.1f}" if isinstance(result["score"], (int, float)) else "not benchmarked"
    status = (f"Exported **{result['name']}**: {result['source']} (benchmark score {score}). "
              f"World: {result['world']['locations']} locations, {result['world']['npcs']} NPCs, "
              f"{result['world']['missions']} missions. Unzip it and run `python run.py play` or `python run.py serve`.")
    return API.download(result["download_url"]), status


# ---- records (/v1/records) -------------------------------------------------------------------

def _recorded(path, key, **params):
    try:
        return API.get(path, **params)[key]
    except APIError as exc:
        if exc.status == 404:  # no records yet
            return []
        raise


@ui
def records_status_text():
    info = API.get("/v1/records")
    status = (f"Recording to `{info['directory']}`." if info["enabled"]
              else f"Recording is off (`{info['directory']}` is not usable, or ANE_RECORDS=off).")
    return status + " Every run, engine, world and story generated here, in the API or in the terminal is kept there."


@ui
def recorded_engines():
    return [(f"{e['created_at'][:16]} · {e['source']}" + (f" · gen {e['generation']}" if e["generation"] is not None else "")
             + (f" · score {e['score']:.1f}" if e["score"] is not None else "") + (" · resumable" if e["resumable"] else "")
             + f" · {e['id'][:8]}", e["id"]) for e in _recorded("/v1/records/engines", "engines")]


@ui
def recorded_stories():
    return [(f"{s['created_at'][:16]} · {s['origin']} {s['kind']} · {s['turns']} turns · {s['status']}"
             + (f" · {s['mission_status']}" if s["mission_status"] else "") + f" · {s['id'][:8]}", s["id"])
            for s in _recorded("/v1/records/stories", "stories")]


@ui
def recorded_runs():
    return pd.DataFrame([
        {"run": r["id"][:8], "created": r["created_at"][:16], "origin": r["origin"], "kind": r["kind"],
         "status": r["status"], "generations": r["generations_run"], "best score": r["best_score"],
         "engine": (r["best_engine_id"] or "")[:8]}
        for r in _recorded("/v1/records/runs", "runs")
    ])


def refresh_records_ui():
    return (gr.Dropdown(choices=recorded_engines()), gr.Dropdown(choices=recorded_stories()), recorded_runs(),
            records_status_text())


@ui
def play_recorded_ui(engine_id, profile, stats_choice, stats_points, mission=None):
    if not engine_id:
        raise gr.Error("Choose a recorded engine first.")
    return _game_outputs(API.post(_path("/v1/records/engines", engine_id, "play"),
                                  _player(profile, stats_choice, stats_points, mission)))


@ui
def continue_recorded_ui(engine_id, more_generations, profile, stats_choice, stats_points, mission=None,
                         progress=gr.Progress()):
    if not engine_id:
        raise gr.Error("Choose a recorded engine first.")
    job = API.post(_path("/v1/jobs/engines", engine_id, "continue"),
                   {"generations": int(more_generations), **_player(profile, stats_choice, stats_points, mission)})
    return _game_outputs(API.wait(job, _job_progress(progress)))


@ui
def retrain_recorded_ui(engine_id, candidate_count, more_generations, simulation_steps, episodes, deterministic,
                        profile, stats_choice, stats_points, mission=None, progress=gr.Progress()):
    if not engine_id:
        raise gr.Error("Choose a recorded engine first.")
    job = API.post(_path("/v1/jobs/engines", engine_id, "retrain"), {
        "candidate_count": int(candidate_count), "generations": int(more_generations),
        "simulation_steps": int(simulation_steps), "episodes": int(episodes) if episodes else None,
        "deterministic": bool(deterministic), **_player(profile, stats_choice, stats_points, mission),
    })
    return _game_outputs(API.wait(job, _job_progress(progress)))


@ui
def download_recorded_ui(engine_id):
    if not engine_id:
        raise gr.Error("Choose a recorded engine first.")
    engine = API.get(_path("/v1/records/engines", engine_id))
    model = API.download(_path("/v1/records/engines", engine_id, "model"))
    world = API.download(_path("/v1/records/worlds", engine["world_id"]) + "?format=yaml")
    return model, world


@ui
def story_view_ui(story_id):
    if not story_id:
        return ""
    story = API.get(_path("/v1/records/stories", story_id))
    turns = "\n".join(
        f"| {t['turn']} | {t['action']} | {t['location_before']} → {t['location_after']} | "
        f"{'✓' if t['succeeded'] else '✗'} | {(t['outcome'] or '').replace('|', '/')} |"
        for t in story["turn_records"]
    )
    return (f"**{story['origin']} {story['kind']}** · {story['status']} · {story['turns']} turns · "
            f"{story['mission_status'] or 'no mission'} · fitness {story['fitness'] or 0:.1f}  \n"
            f"Start: {story['start_location']} · seed: {story['seed']} · engine: `{story['engine_id']}` · "
            f"run: `{story['run_id']}`\n\n| turn | action | where | success | outcome |\n|---|---|---|---|---|\n"
            f"{turns}\n\n<details><summary>Transcript</summary>\n\n```\n{story['transcript']}\n```\n</details>")


# ---- tab 5 · engine logic and app parameters (/v1/logic, /v1/app-settings) ---------------------

@ui
def load_logic_section(section):
    return API.get(_path("/v1/logic", section))["parameters"]


@ui
def save_logic_section(section, text):
    return API.put(_path("/v1/logic", section), {"parameters": text})["message"]


@ui
def save_app_parameters(*values):
    message = API.put("/v1/app-settings", {"settings": dict(zip(APP_PARAMETER_KEYS, values))})["message"]
    return message + " ANE_UI_HOST, ANE_UI_PORT and ANE_UI_SHARE environment variables override launch settings."


APP_PARAMETER_KEYS = []
REFERENCE = {}


def build_app():
    global REFERENCE, APP_PARAMETER_KEYS
    REFERENCE = API.get("/v1/reference")
    app_settings = API.get("/v1/app-settings")
    limits, app_defaults = app_settings["settings"], app_settings["defaults"]
    APP_PARAMETER_KEYS = list(limits)
    settings = API.get("/v1/settings")
    defaults = REFERENCE["defaults"]
    profiles = API.get("/v1/variables/profiles")
    character_stats = API.get("/v1/stats")["character_stats"]
    names = _entity_names("Locations")
    selected_name = names[0] if names else None
    initial = _entity("Locations", selected_name)

    with gr.Blocks(title="Adaptive Narrative Studio") as demo:
        session_token = gr.State("")
        gr.Markdown(
            "# Adaptive Narrative Studio\n"
            "Author the world, test its narrative behavior, then play the generated game.",
            elem_classes="hero",
        )

        with gr.Tabs():
            with gr.Tab("1 · World editor"):
                with gr.Row():
                    kind = gr.Radio(REFERENCE["entity_kinds"], value="Locations", label="Content type")
                    entity = gr.Dropdown(label="Entry", choices=names, value=selected_name)
                    add_entry = gr.Button("New entry")
                name = gr.Textbox(label="Name", value=initial["name"])
                tags = gr.Dropdown(
                    REFERENCE["common_tags"],
                    value=initial["tags"],
                    multiselect=True,
                    allow_custom_value=True,
                    label="Tags",
                    info="Type a new tag and press Enter to add it.",
                )
                descriptions = gr.Textbox(
                    label="Descriptions", value="\n".join(initial["descriptions"]), lines=5,
                    info="One description per line.",
                )
                goals = gr.Textbox(label="Goals", value="\n".join(initial["goals"]), lines=3, info="One goal per line.")
                unique = gr.Checkbox(
                    label="Unique NPC",
                    visible=False,
                    info="Unique: lives in one location for the whole game. "
                    "Unchecked (generic, e.g. guards): may appear in every location that fits its tags.",
                )
                with gr.Row():
                    encounter_event = gr.Dropdown(
                        _event_choices(),
                        value="",
                        label="Encounter event",
                        visible=False,
                        info="Hostile NPCs: this event starts by itself when the NPC is in the scene, "
                        "before the player can act or leave.",
                    )
                    encounter_repeat = gr.Checkbox(
                        label="Repeat on every visit",
                        visible=False,
                        info="Unchecked: only the first encounter in a game.",
                    )
                enabled_if = gr.Textbox(
                    label="Enabled if (condition)",
                    value=to_yaml(initial.get("enabled_if"), flow=True),
                    placeholder="e.g. {thief_caught: false, reputation: {gte: 1}} or "
                    "{any: [{has_item: Ancient Map}, {stat: persuasion, op: gte, value: 60}]}",
                    info="Leave empty to always enable. Variables in shorthand, or a tree with all / any / not and "
                    "variable, stat, resource, has_item, visited, at_location, npc_removed, event_done, action_done, "
                    "outcome_tag, mission. Comparisons: eq, ne, gt, gte, lt, lte, in, not_in.",
                )
                event_title = gr.Textbox(label="Event title", visible=False, info="Shown while the event is active.")
                stat_bonus = gr.Textbox(
                    label="Stat bonus while held", visible=False,
                    placeholder="e.g. {attack: 15}", info="Added to the character's stats while the item is in the inventory.",
                )
                with gr.Row():
                    start_eligible = gr.Checkbox(
                        label="Stories can start here", value=bool(initial.get("start_eligible")),
                        info="Each story starts at one of these locations, drawn at random (only where its "
                        "mission can be reached). Unchecked: a story never starts here.",
                    )
                    start_weight = gr.Number(
                        label="Start weight", value=initial.get("start_weight") or 1.0, minimum=0.01,
                        info="Relative chance among the starting locations. A player's recent starts are drawn "
                        "less often (`start_selection` in the engine logic).",
                    )
                with gr.Row():
                    save_entry = gr.Button("Save entry", variant="primary")
                    delete_entry = gr.Button("Delete entry", variant="stop")
                editor_status = gr.Markdown()

                with gr.Accordion("Actions & outcomes of this entry", open=False):
                    gr.Markdown(
                        "Edit the entry's actions as YAML: one-time (`repeat: once`) and exclusive "
                        "(`exclusive_group`) options, events (`triggers_event`), one-way moves (`send_to`), "
                        "persistent variables (`set_variables` / `change_variables`) and more. Everything is "
                        "checked (fields, events, locations, NPCs, items, variables) before saving."
                        + REFERENCE["action_reference"]
                    )
                    actions_editor = gr.Code(
                        value=load_entity_actions_yaml("Locations", selected_name), language="yaml", label="actions",
                    )
                    with gr.Row():
                        save_actions_button = gr.Button("Validate & save actions", variant="primary")
                        reload_actions_button = gr.Button("Reload from disk")
                    actions_status = gr.Markdown()
                    gr.Markdown("**How often one action is offered**")
                    with gr.Row():
                        action_pick = gr.Dropdown(label="Action", choices=_action_choices("Locations", selected_name),
                                                  value=None)
                        action_repeat = gr.Radio(
                            [REFERENCE["repeat_default"], *REFERENCE["repeat_rules"]], value=REFERENCE["repeat_default"],
                            label="Repeat",
                            info="once: per game · per_visit: again on each visit · always: every turn · "
                            "default: action_rules.default_repeat for its source.",
                        )
                        action_retry = gr.Checkbox(
                            label="Retry after failure",
                            info="A failed attempt does not use the action up: a once action disappears only "
                            "after it succeeds, a per_visit one stays on offer in the visit until it succeeds.",
                        )
                    save_repetition_button = gr.Button("Save repetition")

                with gr.Accordion("Uniqueness & scheduling (events and items)", open=False):
                    gr.Markdown(
                        "**Events:** when the event may start, enforced for every way in (trigger actions, "
                        "encounters, chains, effects, random schedule): `trigger` (action / random), `chance` and "
                        "`weight` (random), `cooldown`, `max_occurrences`, `repeat` (once / per_location / "
                        "per_visit / always). **Items:** `unique: true` makes one instance per story, placed at "
                        "`location` (or where it fits best)."
                    )
                    policy_editor = gr.Code(value=load_entity_policy_yaml("Locations", selected_name),
                                            language="yaml", label="rules")
                    with gr.Row():
                        save_policy_button = gr.Button("Validate & save rules", variant="primary")
                        reload_policy_button = gr.Button("Reload from disk")
                    policy_status = gr.Markdown()

                with gr.Accordion("Missions", open=False):
                    gr.Markdown(
                        "Each story draws one mission (weighted by `weight`) whose `enabled_if` holds. Goal keys: "
                        + ", ".join(f"`{key}`" for key in REFERENCE["goal_keys"])
                        + "; all must be met. References and conditions are checked before saving."
                    )
                    missions_editor = gr.Code(value=load_missions_yaml(), language="yaml", label="missions.yaml")
                    with gr.Row():
                        save_missions_button = gr.Button("Validate & save missions", variant="primary")
                        reload_missions_button = gr.Button("Reload from disk")
                    missions_status = gr.Markdown()
                    save_missions_button.click(save_missions_yaml, missions_editor, [missions_editor, missions_status])
                    reload_missions_button.click(load_missions_yaml, None, missions_editor)

                npc_fields = [unique, encounter_event, encounter_repeat, enabled_if, event_title, stat_bonus,
                              start_eligible, start_weight]
                entry_fields = [name, tags, descriptions, goals, *npc_fields]
                repetition_fields = [action_pick, action_repeat, action_retry]
                kind.change(entity_names, kind, [entity, *entry_fields]).then(
                    load_entity_actions_yaml, [kind, entity], actions_editor,
                ).then(load_entity_policy_yaml, [kind, entity], policy_editor).then(
                    action_picker, [kind, entity], repetition_fields,
                )
                entity.change(action_picker, [kind, entity], repetition_fields)
                action_pick.change(load_action_repetition, [kind, entity, action_pick], [action_repeat, action_retry])
                save_repetition_button.click(
                    save_repetition_ui, [kind, entity, *repetition_fields], [actions_editor, actions_status],
                )
                entity.change(load_entity_ui, [kind, entity], entry_fields)
                entity.change(load_entity_actions_yaml, [kind, entity], actions_editor)
                entity.change(load_entity_policy_yaml, [kind, entity], policy_editor)
                save_policy_button.click(save_entity_policy_ui, [kind, entity, policy_editor], [policy_editor, policy_status])
                reload_policy_button.click(load_entity_policy_yaml, [kind, entity], policy_editor)
                save_actions_button.click(
                    save_entity_actions_ui, [kind, entity, actions_editor], [actions_editor, actions_status],
                ).then(action_picker, [kind, entity], repetition_fields)
                reload_actions_button.click(load_entity_actions_yaml, [kind, entity], actions_editor)
                add_entry.click(new_entity, None, [entity, *entry_fields])
                save_entry.click(save_entity_ui, [kind, entity, *entry_fields], [entity, editor_status])
                delete_entry.click(delete_entity_ui, [kind, entity], [entity, *entry_fields, editor_status])

            with gr.Tab("2 · Simulation settings"):
                gr.Markdown(
                    "### Complete configuration file\n"
                    "Upload one combined YAML file containing all sections from the four files in `config/`. "
                    "It is checked completely before it can replace the built-in runtime configuration."
                )
                with gr.Row():
                    config_upload = gr.File(label="Combined YAML configuration", file_types=[".yaml", ".yml"],
                                            type="filepath")
                    with gr.Column():
                        activate_config = gr.Button("Validate & use uploaded config", variant="primary")
                        restore_config = gr.Button("Use built-in config")
                with gr.Row():
                    pop_size = gr.Number(settings["pop_size"], label="Evolution population", precision=0, minimum=1)
                    generations = gr.Number(settings["generations"], label="Generations", precision=0, minimum=1)
                    episodes = gr.Number(settings["episodes_per_genome"], label="Episodes / genome", precision=0, minimum=1)
                    max_steps = gr.Number(settings["max_steps"], label="Maximum simulation steps", precision=0, minimum=1)
                with gr.Row():
                    health = gr.Number(settings["initial_health"], label="Starting health", precision=0, minimum=1)
                    max_health = gr.Number(
                        settings["max_health"], label="Maximum health", precision=0, minimum=1,
                        info="Healing never raises health above this value.",
                    )
                    coins = gr.Number(settings["initial_coins"], label="Starting coins", precision=0, minimum=0)
                    max_actions = gr.Number(
                        settings["max_actions"], label="Game action limit", precision=0, minimum=100,
                        info="The action-limit cutoff cannot be set below 100; other ending conditions still apply.",
                    )
                with gr.Row():
                    deterministic = gr.Checkbox(
                        value=settings["deterministic"], label="Deterministic reproducibility",
                        info="Repeat the same evolution when the configuration and seed are unchanged.",
                    )
                    seed = gr.Number(settings["seed"], label="Reproducibility seed", precision=0)
                save_config = gr.Button("Save settings", variant="primary")
                config_status = gr.Markdown()
                setting_components = [pop_size, generations, episodes, max_steps, health, coins, max_actions,
                                      deterministic, seed, max_health]
                activate_config.click(upload_config, config_upload, [*setting_components, config_status])
                restore_config.click(use_builtin_config, None, [*setting_components, config_status])
                save_config.click(save_settings, setting_components, config_status)

                with gr.Accordion("Persistent variables", open=False):
                    gr.Markdown(
                        "Variables that carry over between playthroughs. Outcomes change them with "
                        "`set_variables` / `change_variables`; locations, NPCs, items and missions use "
                        "`enabled_if`. Only real play (the game on the Play tab) saves them."
                    )
                    variables_editor = gr.Code(value=load_variables_yaml(), language="yaml", label="variables.yaml")
                    save_variables_button = gr.Button("Validate & save variable definitions", variant="primary")
                    variables_status = gr.Markdown()
                    gr.Markdown(
                        "#### Stored values of a player profile\n"
                        "Edit them to test later playthroughs (e.g. set `playthroughs: 1` or `reputation: -3`)."
                    )
                    values_profile = gr.Dropdown(profiles["profiles"], value=profiles["default"],
                                                 allow_custom_value=True, label="Player profile")
                    stored_values = gr.Code(value=stored_values_yaml(profiles["default"]), language="yaml",
                                            label="Stored values")
                    with gr.Row():
                        save_values_button = gr.Button("Save stored values", variant="primary")
                        refresh_values_button = gr.Button("Refresh")
                        reset_values_button = gr.Button("Reset profile (new player)", variant="stop")
                    values_status = gr.Markdown()
                    save_variables_button.click(
                        save_variables_yaml, variables_editor, [variables_editor, variables_status],
                    ).then(stored_values_yaml, values_profile, stored_values)
                    values_profile.change(stored_values_yaml, values_profile, stored_values)
                    refresh_values_button.click(stored_values_yaml, values_profile, stored_values)
                    save_values_button.click(save_stored_values_yaml, [values_profile, stored_values],
                                             [stored_values, values_status])
                    reset_values_button.click(reset_stored_values_ui, values_profile, [stored_values, values_status])
                with gr.Accordion("Character stats", open=False):
                    gr.Markdown(
                        "Define the stats of this story (any names), the points budget, how players get their "
                        "points, which outcome tags need a check (`tag_checks`) and which stat softens combat "
                        "damage. Difficulty lives in the Weights tab (**Skill check resolution**)."
                    )
                    stats_editor = gr.Code(value=load_stats_yaml(), language="yaml", label="stats.yaml")
                    with gr.Row():
                        save_stats_button = gr.Button("Validate & save character stats", variant="primary")
                        reload_stats_button = gr.Button("Reload from disk")
                    stats_status = gr.Markdown()
                    save_stats_button.click(save_stats_yaml, stats_editor, [stats_editor, stats_status])
                    reload_stats_button.click(load_stats_yaml, None, stats_editor)
                with gr.Accordion("Story rules", open=False):
                    rules = dict(zip(REFERENCE["rule_fields"], load_rules_ui()))
                    rule_choices = REFERENCE["rule_choices"]
                    locations = _entity_names("Locations")
                    gr.Markdown("**Missions and goal pursuit**")
                    with gr.Row():
                        rule_missions = gr.Checkbox(rules["missions_enabled"], label="Missions enabled")
                        rule_end_on_goal = gr.Checkbox(rules["end_on_goal"], label="End the story when the goal is met")
                        rule_start = gr.Number(rules["pursuit_start_step"], precision=0, minimum=0, label="Pursuit starts at step")
                        rule_full = gr.Number(rules["pursuit_full_step"], precision=0, minimum=1, label="Full pursuit from step")
                        rule_route = gr.Number(rules["route_weight"], label="Route weight")
                    gr.Markdown("**How often options are offered** (unless an action sets `repeat`)")
                    with gr.Row():
                        rule_repeat = [
                            gr.Dropdown(rule_choices["repeat"], value=rules[f"repeat_{source}"], label=f"{source.title()} actions")
                            for source in ("location", "npc", "item", "event")
                        ]
                    gr.Markdown("**Arrests and discovery**")
                    with gr.Row():
                        rule_arrest = gr.Dropdown(locations, value=rules["arrest_location"], label="Arrest location")
                        rule_lock = gr.Dropdown(
                            rule_choices["arrest_lock_when"], value=rules["arrest_lock_when"], label="Confine until escape",
                            info="sent: only when sent there; always: everyone; never: no escape needed",
                        )
                        rule_travel = gr.Checkbox(rules["auto_travel_on_spawn"], label="Follow compelling new paths")
                        rule_threshold = gr.Number(rules["auto_travel_on_spawn_threshold"], label="Commitment threshold")
                    gr.Markdown("**Persistent variables and events**")
                    with gr.Row():
                        rule_profile = gr.Textbox(rules["profile"], label="Default player profile")
                        rule_save_on = gr.Dropdown(rule_choices["save_on"], value=rules["save_on"], label="Save variables on")
                        rule_simulate = gr.Dropdown(rule_choices["simulate_with"], value=rules["simulate_with"],
                                                    label="Evolution plays with")
                    with gr.Row():
                        rule_fallback = gr.Textbox(rules["fallback_action"], label="Event fallback option")
                        rule_fallback_desc = gr.Textbox(rules["fallback_description"], label="Event fallback text")
                    rule_by_field = {
                        "missions_enabled": rule_missions, "end_on_goal": rule_end_on_goal,
                        "pursuit_start_step": rule_start, "pursuit_full_step": rule_full, "route_weight": rule_route,
                        "repeat_location": rule_repeat[0], "repeat_npc": rule_repeat[1], "repeat_item": rule_repeat[2],
                        "repeat_event": rule_repeat[3], "arrest_location": rule_arrest, "arrest_lock_when": rule_lock,
                        "auto_travel_on_spawn": rule_travel, "auto_travel_on_spawn_threshold": rule_threshold,
                        "profile": rule_profile, "save_on": rule_save_on, "simulate_with": rule_simulate,
                        "fallback_action": rule_fallback, "fallback_description": rule_fallback_desc,
                    }
                    rule_inputs = [rule_by_field[field] for field in REFERENCE["rule_fields"]]
                    with gr.Row():
                        save_rules_button = gr.Button("Save story rules", variant="primary")
                        reload_rules_button = gr.Button("Reload")
                    rules_status = gr.Markdown()
                    save_rules_button.click(save_rules_ui, rule_inputs, rules_status)
                    reload_rules_button.click(load_rules_ui, None, rule_inputs)
                with gr.Accordion("Story messages", open=False):
                    gr.Markdown(
                        "Rewrite any sentence the engine adds to a story, e.g. `voyage: \"You head towards "
                        "the {location}.\"`. Each entry lists its `{placeholders}`; a list of texts is used "
                        "in turn. Unknown messages or placeholders are rejected."
                    )
                    messages_editor = gr.Code(value=load_messages_yaml(), language="yaml", label="messages.yaml")
                    with gr.Row():
                        save_messages_button = gr.Button("Validate & save messages", variant="primary")
                        reload_messages_button = gr.Button("Reload from disk")
                    messages_status = gr.Markdown()
                    save_messages_button.click(save_messages_yaml, messages_editor, [messages_editor, messages_status])
                    reload_messages_button.click(load_messages_yaml, None, messages_editor)
                with gr.Accordion("Advanced · all hyperparameters", open=False):
                    gr.Markdown(
                        "Edit every hyperparameter as YAML. The complete structure and value types "
                        "are validated before saving."
                    )
                    hyperparameters_editor = gr.Code(value=API.get("/v1/hyperparameters")["yaml"], language="yaml",
                                                     label="hyperparameters.yaml")
                    save_all_config = gr.Button("Validate & save all hyperparameters")
                    advanced_status = gr.Markdown()
                    save_all_config.click(save_all_hyperparameters, hyperparameters_editor,
                                          [*setting_components, hyperparameters_editor, advanced_status])

            with gr.Tab("3 · Weights"):
                gr.Markdown(
                    "Configure every numeric weight, bonus, penalty, threshold, chance, rate, "
                    "mitigation, floor, and power control used by scoring and the narrative engine."
                )
                weight_entries = list(API.get("/v1/weights")["values"].items())
                weight_paths = [path for path, _ in weight_entries]
                weight_components = []
                grouped_weights = {}
                for weight_path, value in weight_entries:
                    grouped_weights.setdefault(weight_path.split(".")[0], []).append((weight_path, value))
                for section, section_entries in grouped_weights.items():
                    with gr.Accordion(section.replace("_", " ").title(), open=False):
                        for index in range(0, len(section_entries), 3):
                            with gr.Row():
                                for weight_path, value in section_entries[index:index + 3]:
                                    weight_components.append(gr.Number(value=value, label=weight_path))
                save_weights_button = gr.Button("Save all weights", variant="primary")
                weights_status = gr.Markdown()
                save_weights_button.click(save_weights, [gr.State(weight_paths), *weight_components], weights_status)

            with gr.Tab("4 · Simulate & play"):
                gr.Markdown("Run a quick tournament of narrative genomes. The strongest result becomes your playable game.")
                with gr.Accordion("Load a previous best engine", open=False):
                    gr.Markdown(
                        "Upload an engine model (`.ane.zip`: world, storyteller, training state, memory and tag "
                        "vectors), an older engine ZIP, or a complete configuration YAML with its model-parameters "
                        "YAML. If the configuration already contains a `generated_run.genome`, the separate "
                        "parameters file is optional."
                    )
                    with gr.Row():
                        engine_upload = gr.File(label="Engine bundle or configuration", file_types=[".zip", ".yaml", ".yml"],
                                                type="filepath")
                        model_parameters_upload = gr.File(label="Model parameters (optional)", file_types=[".yaml", ".yml"],
                                                          type="filepath")
                        load_engine_button = gr.Button("Load best engine & start game", variant="primary")
                with gr.Row():
                    candidates = gr.Slider(1, limits["candidate_limit"], value=limits["default_candidates"], step=1,
                                           label="Candidate genomes")
                    generations = gr.Slider(1, limits["generation_limit"], value=limits["default_generations"], step=1,
                                            label="Evolution generations")
                    sim_steps = gr.Slider(1, limits["simulation_step_limit"], value=limits["default_simulation_steps"],
                                          step=1, label="Steps per simulation")
                    episodes = gr.Slider(
                        1, 10, value=min(10, int(defaults["episodes_per_genome"])), step=1,
                        label="Stories per storyteller", info="More stories per genome: fairer scores, slower learning.",
                    )
                warm_start = gr.Checkbox(
                    value=bool(defaults["warm_start"]), label="Continue learning from earlier runs",
                    info="Seed the population with the best storytellers the AI remembers.",
                )
                with gr.Row():
                    deterministic_run = gr.Checkbox(
                        value=settings["deterministic"], label="Run deterministically",
                        info="Uses the reproducibility seed saved in Simulation settings.",
                    )
                    play_profile = gr.Dropdown(
                        profiles["profiles"], value=profiles["default"], allow_custom_value=True, label="Player profile",
                        info="Persistent variables are saved to this profile. Type a new name for a new player.",
                    )
                    first_quests, quest_overview_text = quest_overview(profiles["default"])
                    play_mission = gr.Dropdown(
                        first_quests, value="", label="Quest to play",
                        info="Choose the quest of the next game (every way of starting one), or let the story draw it.",
                    )
                with gr.Accordion("Quests in this world", open=False):
                    quest_overview_md = gr.Markdown(quest_overview_text)
                    refresh_quests = gr.Button("Refresh quests for this profile")
                with gr.Accordion("Character stats of the player", open=False):
                    allocation = character_stats.get("allocation") or {}
                    gr.Markdown(
                        f"Spread **{allocation.get('total_points', 50)} points** over "
                        f"{', '.join(f'`{stat}`' for stat in (character_stats.get('stats') or {})) or 'no stats'}; "
                        f"each stat takes {allocation.get('min_points', 5)}-{allocation.get('max_points', 20)} points. "
                        "Choose `defined` to play with the points below."
                    )
                    with gr.Row():
                        stats_choice = gr.Dropdown(REFERENCE["player_stat_choices"], value="story default", label="Stats")
                        randomize_stats = gr.Button("Randomize")
                        balance_stats = gr.Button("Balance")
                    stats_points = gr.Code(value=to_yaml(character_stats.get("defined_points") or {}), language="yaml",
                                           label="Stat points")
                    stats_check = gr.Markdown()
                    randomize_stats.click(lambda: stats_points_text("random"), None, stats_points).then(
                        lambda: "defined", None, stats_choice,
                    )
                    balance_stats.click(lambda: stats_points_text("balanced"), None, stats_points).then(
                        lambda: "defined", None, stats_choice,
                    )
                    stats_points.change(check_points_text, stats_points, stats_check)
                run_button = gr.Button("Evolve storytellers & start game", variant="primary")
                simulation_report = gr.Markdown(elem_classes="status")
                with gr.Row():
                    learning_plot = gr.LinePlot(
                        x="generation", y="score", color="series", label="Learning curve",
                        x_title="Generation", y_title="Fitness", height=280,
                    )
                    learned_traits = gr.Dataframe(
                        headers=["trait", "meaning", "evolved", "population", "tendency"],
                        label="What the evolved storyteller learned", interactive=False, wrap=True,
                    )
                with gr.Accordion("Learning memory", open=False):
                    memory_status = gr.Markdown(learning_memory_text())
                    forget_button = gr.Button("Forget what the AI learned", variant="stop")
                    forget_button.click(forget_learning_ui, None, memory_status)
                run_state = gr.State(None)
                with gr.Accordion("Export the engine (offline, for a game)", open=False):
                    gr.Markdown(
                        "Download a self-contained engine: the engine code, this world and its tuning, the chosen "
                        "evolved storyteller, the tag vectors it was trained with and (optionally) the learning "
                        "memory. It runs offline with only `pyyaml` and `numpy`: embed it in a Python game "
                        "(`NarrativeEngine`), run `python run.py serve` for Unity/Godot/web games over HTTP, "
                        "`play` it in a terminal, or keep training it with `evolve`. The ZIP's README explains how."
                    )
                    with gr.Row():
                        export_source = gr.Radio(list(EXPORT_SOURCES), value="This run's best", label="Storyteller")
                        export_generation = gr.Dropdown([], label="Generation (for a generation's champion)")
                    with gr.Row():
                        export_memory = gr.Checkbox(True, label="Include the learning memory (keep training offline)")
                        export_name = gr.Textbox(label="Name", placeholder="narrative-engine")
                    export_button = gr.Button("Export offline engine", variant="primary")
                    export_file = gr.File(label="Offline engine (ZIP)", interactive=False)
                    export_status = gr.Markdown()
                    export_button.click(
                        export_engine_ui,
                        [export_source, run_state, export_generation, session_token, export_memory, export_name],
                        [export_file, export_status],
                    )
                with gr.Accordion("Records: recorded engines, runs and stories", open=False):
                    records_status = gr.Markdown(records_status_text())
                    with gr.Row():
                        record_engine = gr.Dropdown(recorded_engines(), label="Recorded engine",
                                                    info="Every run's best, every generation's champion, loaded, "
                                                    "continued and retrained engines.")
                        refresh_records = gr.Button("Refresh")
                    with gr.Row():
                        play_record = gr.Button("Play with this engine", variant="primary")
                        more_generations = gr.Slider(1, limits["generation_limit"], value=3, step=1,
                                                     label="Further generations")
                        continue_record = gr.Button("Continue its training")
                        retrain_record = gr.Button("Retrain from it")
                    download_record = gr.Button("Download its model file and world")
                    with gr.Row():
                        record_model_file = gr.File(label="Engine model (.ane.zip)", interactive=False)
                        record_world_file = gr.File(label="World and tuning (YAML)", interactive=False)
                    record_story = gr.Dropdown(recorded_stories(), label="Recorded story")
                    story_view = gr.Markdown()
                    runs_table = gr.Dataframe(recorded_runs(), label="Recorded runs", interactive=False)
                with gr.Row():
                    with gr.Column(scale=3):
                        scene = gr.Markdown("Run a simulation to begin.")
                        choices = gr.Radio([], label="What do you do?", interactive=True,
                                           info="The most relevant choices now: fewer in tense moments.")
                        choose = gr.Button("Take action", variant="primary")
                        turn_result = gr.Textbox(label="Last turn", lines=7, interactive=False)
                    with gr.Column(scale=2):
                        quest_panel = gr.Markdown("### Quest\nStart a game to see its quest.")
                        people_panel = gr.Markdown("### People met\nNobody yet.")
                generated_config = gr.File(label="Generated story configuration", interactive=False, visible=True)
                generated_engine = gr.File(
                    label="Engine model (world, storyteller, training state, memory, tag vectors)",
                    interactive=False, visible=True,
                )

                play_outputs = [
                    session_token, simulation_report, scene, choices, turn_result,
                    generated_config, generated_engine, quest_panel, people_panel,
                ]
                run_button.click(
                    run_simulation_ui,
                    [candidates, generations, sim_steps, deterministic_run, play_profile, stats_choice, stats_points,
                     episodes, warm_start, play_mission],
                    [*play_outputs, learning_plot, learned_traits, memory_status, run_state, export_generation],
                    concurrency_limit=1,
                    concurrency_id="narrative_evolution",
                )
                load_engine_button.click(
                    load_best_engine_ui,
                    [engine_upload, model_parameters_upload, play_profile, stats_choice, stats_points, play_mission],
                    [*play_outputs, learning_plot, learned_traits],
                    concurrency_limit=1,
                )
                choose.click(play_action_ui, [session_token, choices],
                             [scene, choices, turn_result, quest_panel, people_panel], concurrency_limit=1)
                play_profile.change(playable_quests_ui, play_profile, [play_mission, quest_overview_md])
                refresh_quests.click(playable_quests_ui, play_profile, [play_mission, quest_overview_md])
                record_outputs = [*play_outputs, learning_plot, learned_traits, run_state, export_generation]
                player_inputs = [play_profile, stats_choice, stats_points, play_mission]
                record_lists = [record_engine, record_story, runs_table, records_status]
                refresh_records.click(refresh_records_ui, None, record_lists)
                play_record.click(play_recorded_ui, [record_engine, *player_inputs], record_outputs, concurrency_limit=1)
                continue_record.click(continue_recorded_ui, [record_engine, more_generations, *player_inputs],
                                      record_outputs, concurrency_limit=1, concurrency_id="narrative_evolution")
                retrain_record.click(retrain_recorded_ui,
                                     [record_engine, candidates, more_generations, sim_steps, episodes,
                                      deterministic_run, *player_inputs],
                                     record_outputs, concurrency_limit=1, concurrency_id="narrative_evolution")
                download_record.click(download_recorded_ui, record_engine, [record_model_file, record_world_file])
                record_story.change(story_view_ui, record_story, story_view)
                for button in (run_button, load_engine_button):
                    button.click(refresh_records_ui, None, record_lists)

            with gr.Tab("5 · Engine logic & app parameters"):
                gr.Markdown(
                    "The engine's logic runs in the backend scripts; here you change **how it behaves** "
                    "through its parameters, section by section. Every section is checked (same settings, "
                    "same value types) before it is saved, and new simulations and games use it at once."
                )
                with gr.Accordion("Engine logic", open=True):
                    sections = API.get("/v1/logic")["sections"]
                    descriptions_by_section = {item["section"]: item["description"] for item in sections}
                    logic_section = gr.Dropdown(list(descriptions_by_section), value="evolution", label="Logic section")
                    logic_description = gr.Markdown(descriptions_by_section.get("evolution", ""))
                    logic_editor = gr.Code(value=load_logic_section("evolution"), language="yaml", label="Parameters")
                    with gr.Row():
                        save_logic_button = gr.Button("Validate & save parameters", variant="primary")
                        reload_logic_button = gr.Button("Reload")
                    logic_status = gr.Markdown()
                    logic_section.change(load_logic_section, logic_section, logic_editor)
                    logic_section.change(lambda section: descriptions_by_section.get(section, ""), logic_section,
                                         logic_description)
                    reload_logic_button.click(load_logic_section, logic_section, logic_editor)
                    save_logic_button.click(save_logic_section, [logic_section, logic_editor], logic_status)
                with gr.Accordion("App parameters", open=True):
                    gr.Markdown("Saved in config/ui_settings.yml. Upload limits are in bytes; crossover probability is between 0 and 1.")
                    parameter_inputs = []
                    for key, value in limits.items():
                        label = key.replace("_", " ").title()
                        if isinstance(value, bool):
                            component = gr.Checkbox(value=value, label=label)
                        elif isinstance(value, str):
                            component = gr.Textbox(value=value, label=label)
                        else:
                            component = gr.Number(value=value, label=label,
                                                  precision=0 if isinstance(app_defaults[key], int) else None)
                        parameter_inputs.append(component)
                    save_parameters = gr.Button("Save app parameters", variant="primary")
                    parameters_status = gr.Markdown()
                    save_parameters.click(save_app_parameters, parameter_inputs, parameters_status)

    demo.launch_settings = limits
    return demo


demo = build_app()


if __name__ == "__main__":
    launch = demo.launch_settings
    demo.queue(default_concurrency_limit=launch["queue_concurrency"], max_size=launch["queue_max_size"]).launch(
        server_name=os.getenv("ANE_UI_HOST", launch["server_host"]),
        server_port=int(os.getenv("ANE_UI_PORT", str(launch["server_port"]))),
        share=os.getenv("ANE_UI_SHARE", str(launch["share"])).lower() in {"1", "true", "yes"},
        css=CSS,
    )
