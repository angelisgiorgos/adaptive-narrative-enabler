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

    Tab 1 · Create the world           /v1/entities/... (form, actions, rules, every field as YAML),
                                   /v1/documents/missions, /v1/documents/map, /v1/lint/failures
    Tab 2 · Simulation settings    /v1/config/..., /v1/settings, /v1/documents/..., /v1/variables/...,
                                   /v1/rules, /v1/hyperparameters
    Tab 3 · Weights                /v1/weights
    Tab 4 · Train the AI        /v1/jobs/..., /v1/engines/load, /v1/sessions/... (and its judge),
                                   /v1/stats/..., /v1/learning, /v1/exports, /v1/records/... (and their
                                   judge), /v1/judge/compare
    Tab 5 · Engine logic & app     /v1/logic/..., /v1/app-settings
    Tab 6 · Evaluate with judge    /v1/jobs/judge-benchmark, /v1/jobs/calibrate-fitness, /v1/fitness/term-weights

tests/test_studio_api.py checks that every endpoint of the API is used here, or is named there
as one other clients use (the studio reaches the same function through another endpoint).
"""

from __future__ import annotations

import atexit
import functools
import json
import logging
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

LOG = logging.getLogger("ane.studio")


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

    def patch(self, path, body=None, **params):
        return self.request("PATCH", path, json=body, params={k: v for k, v in params.items() if v is not None})

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
.hero {padding: 1.1rem 1.4rem; border-radius: 18px; background: linear-gradient(120deg,#17233f,#4a244f); color:#fff}
.hero h1 {margin: 0 0 .2rem 0; color: #fff}
.hero p {margin: 0; opacity: .9; color: #fff}
.workflow {padding: .35rem .9rem; border-radius: 12px; border: 1px solid var(--border-color-primary);
           background: var(--background-fill-secondary); font-size: .95rem}
.status {border-left: 4px solid #8b5cf6; padding-left: 1rem}
.play-hud {padding: .55rem .95rem; border-radius: 12px; border: 1px solid var(--border-color-primary);
           background: var(--background-fill-secondary); font-size: 1.02rem}
.scene-card {padding: .9rem 1.2rem; border-radius: 14px; border: 1px solid var(--border-color-primary);
             background: var(--block-background-fill); line-height: 1.55}
.turn-card {padding: .2rem 1.2rem; border-left: 4px solid #8b5cf6}
.ending {padding: .6rem 1rem; border-radius: 12px; border-left: 6px solid #22c55e;
         background: var(--background-fill-secondary)}
button.choice {justify-content: flex-start !important; text-align: left !important; white-space: normal !important;
               font-weight: 500}
"""
THEME = gr.themes.Soft(primary_hue="violet", secondary_hue="indigo", neutral_hue="slate")
WORKFLOW = ("**① Create the world** → **② Simulation settings** → **③ Weights** → **④ Train the AI** → "
            "**▶ Play** your world → **Export** it for a game (in ④) → **⑥ Evaluate** with the judge")
EMPTY_ENTITY = {"name": "", "tags": [], "descriptions": [], "goals": [], "unique": False, "encounter_event": None,
                "encounter_repeat": False, "enabled_if": None, "title": None, "stat_bonus": None,
                "start_eligible": False, "start_weight": 1.0, "role": None, "secret": None}
INFERRED_ROLE = "inferred from its tags"


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
        gr.Dropdown(choices=[INFERRED_ROLE, *REFERENCE.get("npc_roles", [])], value=entity.get("role") or INFERRED_ROLE,
                    visible=npc),
        gr.Textbox(value=to_yaml(entity.get("secret"), flow=True), visible=npc),
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
    return gr.Dropdown(value=None), "", [], "", "", False, "", False, "", "", "", False, 1.0, INFERRED_ROLE, ""


def _lines(text):
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


@ui
def save_entity_ui(kind, selected_name, name, tags, descriptions, goals, unique, encounter_event, encounter_repeat,
                   enabled_if, title, stat_bonus, start_eligible, start_weight, role=None, secret=None):
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
    if npc and role is not None:  # the NPC's own role ("" removes it: inferred from its tags)
        body["role"] = "" if role == INFERRED_ROLE else role
    if npc and secret is not None:
        body["secret"] = from_yaml(secret, "`Secret`") or {}  # {} removes it
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


@ui
def load_entity_yaml_ui(kind, name):
    return API.get(_path(f"/v1/entities/{kind}", name, "yaml"))["yaml"] if name else "{}\n"


@ui
def save_entity_yaml_ui(kind, name, yaml_text):
    if not name:
        raise gr.Error("Choose an entry first.")
    result = API.put(_path(f"/v1/entities/{kind}", name, "yaml"), {"yaml": yaml_text})
    return result["yaml"], result["message"]


@ui
def failure_lint_ui():
    """The failures the world leaves as dead ends (GET /v1/lint/failures)."""
    found = API.get("/v1/lint/failures")["dead_ends"]
    if not found:
        return ("No dead ends: every action that can fail leads somewhere when it fails (a consequence, or a "
                "complication event that fits it). During play the engine still shows another way "
                "(`director.another_way`).")
    rows = "\n".join(f"| {d['owner']} | {d['action']} | {d['why']} |" for d in found)
    return (f"**{len(found)} failure(s) lead nowhere.** Give them a consequence (a move, a reveal, a setup, an "
            "effect) or write an event tagged `complication` that shares a tag with them.\n\n"
            f"| where | action | why |\n|---|---|---|\n{rows}")


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


def _check_text(check):
    """A choice's check in a few words: "stealth 45% (fail: -2 health, sent to the Dungeon)"."""
    risk = check.get("risk") or {}
    parts = [f"{risk[key]} {key}" for key in ("health", "coins") if risk.get(key)]
    if risk.get("sent_to"):
        parts.append(f"sent to the {risk['sent_to']}")
    return f"{check['stat']} {check['chance']}%" + (f" (fail: {', '.join(parts)})" if parts else "")


def _choices(actions, details=None):
    """The choices as radio options: each labelled with the check it involves (the value stays
    the action's name, which the API takes)."""
    checks = {item["name"]: item.get("check") for item in details or []}
    options = [(name + (f"  ·  {_check_text(checks[name])}" if checks.get(name) else ""), name)
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
        if (person.get("importance") or 0) >= 2:
            notes.append(f"◆ {person.get('role')}")  # someone who matters in this story (npc_roles)
        lines.append(f"- **{person['name']}** · met at {', '.join(person['where'])}"
                     + (f" · {' · '.join(notes)}" if notes else ""))
    return "\n".join(lines) + "\n\n" + map_markdown(game.get("map")) + session_markdown(game)


def session_markdown(game):
    """The session: what its cast leaves out, the one-time actions tried, and its report once it ended."""
    lines = []
    cast = game.get("cast") or {}
    left_out = {kind: names for kind, names in (cast.get("left_out") or {}).items() if names}
    if left_out:
        lines += ["\n\n### This session's cast", "Not in this session (its goal does not need them): "
                  + "; ".join(f"{kind}: {', '.join(names)}" for kind, names in left_out.items()) + "."]
    tried = game.get("unique_actions") or []
    if tried:
        lines.append("\n\n### One-time actions")
        for entry in tried:
            state = "done" if entry["succeeded"] else ("failed, still on offer" if entry["kept"] else "failed, used up")
            lines.append(f"- {entry['action']} ({entry['where']}, turn {entry['turn']}): {state}")
    report = game.get("session_report")
    if report:
        goal = report.get("goal") or {}
        player = report["player"]
        delivered = (report.get("delivery") or {}).get("sent")
        lines += ["\n\n### Session report", f"**{(goal.get('title') or 'Free play')}: {report['outcome']}** "
                  f"({report['end_kind']}, {report['turns']} turns) · health {player['health']}/{player['max_health']} · "
                  f"coins {player['coins']} · inventory: {', '.join(player['inventory']) or 'empty'} · stats: "
                  + ", ".join(f"{k} {v}" for k, v in player["stats"].items())
                  + f" · goals achieved in this save game: {', '.join(report['progression']['goals_achieved']) or 'none'}"
                  + (" · sent to the UNP" if delivered else "")]
    return "\n".join(lines)


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
    return (f"The AI remembers **{memory['size']}** storyteller(s) from earlier runs (best fitness {best:.1f}). "
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


def run_work_ui(candidate_count, generations, simulation_steps, episodes):
    """How much simulation a run asks for, against the limit, before it starts."""
    try:
        asked = API.get("/v1/simulations/work", candidate_count=int(candidate_count or 1),
                        generations=int(generations or 1), simulation_steps=int(simulation_steps or 1),
                        episodes=int(episodes or 1))
    except (APIError, TypeError, ValueError):
        return ""
    line = (f"This run: up to **{asked['work']:,}** simulated turns ({asked['story_turns']} per story) of the "
            f"**{asked['limit']:,}** allowed.")
    if not asked["allowed"]:
        line = ("⚠ " + line + " It will be refused: use fewer storytellers, generations or stories, or raise "
                "`max_simulation_work` in *5 · App parameters*.")
    return line


def _play_view(state):
    """The play area for a play state: scene, choices, last turn, quest, people."""
    return state["scene"], _choices(state["actions"], state.get("choices")), state["last_turn"], \
        quest_markdown(state.get("quest")), people_markdown(state)


# ---- the session as a UNP sees it (as the exported ANE AI does): save games, player, events ---------

SYNC_EVENTS = {  # what happened in the UNP's game -> the field it names
    "action_done": "action", "item_gained": "item", "item_lost": "item", "npc_removed": "npc",
    "location_visited": "location", "coins_changed": "amount", "health_changed": "amount",
}


def _need_game(token):
    if not token:
        raise gr.Error("Start a game first (run a simulation, load an engine, or continue a save game).")


@ui
def session_parameters_ui(token):
    """The session's parameters (ane.session_state/1), as the ANE AI sends them to a UNP."""
    _need_game(token)
    return json.dumps(API.get(_path("/v1/sessions", token, "state")), indent=2, ensure_ascii=False)


@ui
def update_player_ui(token, player_json):
    """Set the player's state as a UNP does (PATCH .../player)."""
    _need_game(token)
    try:
        player = json.loads(player_json or "{}")
    except ValueError as exc:
        raise gr.Error(f"The player's state is not JSON: {exc}") from exc
    if not isinstance(player, dict):
        raise gr.Error('Write a JSON object, e.g. {"coins": 20, "add_items": ["Ancient Map"]}.')
    return _play_view(API.patch(_path("/v1/sessions", token, "player"), player))


@ui
def sync_session_ui(token, kind, value):
    """Something happened in the UNP's game (POST .../sync)."""
    _need_game(token)
    field = SYNC_EVENTS.get(kind)
    if field is None:
        raise gr.Error("Choose what happened.")
    if field == "amount":
        try:
            value = int(str(value).strip())
        except ValueError as exc:
            raise gr.Error("Give the amount as a whole number (negative for a loss).") from exc
    elif not str(value or "").strip():
        raise gr.Error(f"Name the {field}.")
    return _play_view(API.post(_path("/v1/sessions", token, "sync"), {"type": kind, field: value}))


@ui
def download_savegame_ui(token):
    """The session's save game (ane.savegame/1) as a file; the session goes on."""
    _need_game(token)
    path = API.download(_path("/v1/sessions", token, "savegame"), f"savegame-{token[:8]}.json")
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    params = doc.get("parameters") or {}
    return path, (f"Saved after {params.get('turns', 0)} turn(s) at **{params.get('current_location')}** "
                       f"(`{doc['format']}`). *Continue a save game* goes on from it, here or in the ANE AI.")


@ui
def load_savegame_ui(upload, token):
    """Continue a save game (from the studio or the ANE AI) as a new session."""
    if not upload:
        raise gr.Error("Choose a save game (.json).")
    try:
        doc = json.loads(Path(upload).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise gr.Error(f"Not a JSON save game: {exc}") from exc
    state = API.post("/v1/sessions/load", {"savegame": doc, "session_id": token or None})
    status = ("**Continued exactly** where it was saved." if state["restored"] == "exact"
              else f"**Rebuilt from its parameters** ({state.get('note') or 'no usable snapshot'}).")
    if state["restored"] == "exact" and state.get("note"):
        status += f" {state['note'].capitalize()}."
    return (state["session_id"], *_play_view(state), status)


@ui
def world_catalogue_ui():
    """The world as the ANE AI sends it to a UNP (ane.world/1), as a file."""
    path = API.download("/v1/world/catalogue", "world-catalogue.json")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    counts = ", ".join(f"{len(data[kind])} {kind}" for kind in ("locations", "npcs", "items", "events", "missions"))
    return path, f"The world as a UNP receives it (`{data['format']}`): {counts}."


def export_platforms_table():
    """What an export can run on here, and why a platform is not available (GET /v1/exports/platforms)."""
    try:
        listed = API.get("/v1/exports/platforms")["platforms"]
    except APIError as exc:
        LOG.warning("The export platforms could not be read: %s", exc)
        listed = []
    rows = [{"runs on": p["platform"], "available": "yes" if p["available"] else "no",
             "details": p.get("reason") or p.get("description") or
             (f"runtime {p['code_version']}, built {(p.get('built_at') or '')[:10]}" if p.get("code_version") else
              "the engine as Python code")} for p in listed]
    return pd.DataFrame(rows, columns=["runs on", "available", "details"])


@ui
def end_session_ui(token):
    """End the session where it stands: the progression is saved and the session reported."""
    if not token:
        raise gr.Error("Start a game first.")
    state = API.post(_path("/v1/sessions", token, "end"))
    return state["scene"], _choices(state["actions"], state.get("choices")), state["last_turn"], \
        quest_markdown(state.get("quest")), people_markdown(state)


@ui
def progression_ui(profile):
    """A player's save game (GET /v1/progression): goals achieved, playthroughs, sessions."""
    data = API.get("/v1/progression", profile=profile or None)
    goals = ", ".join(data["goals_achieved"]) or "none yet"
    text = (f"**Save game `{data['profile']}`:** {data['playthroughs']} session(s) played · goals achieved "
            f"(each once per save game): {goals}.")
    rows = [{"ended": (r.get("ended_at") or "")[:19], "start": r.get("start_location"),
             "goal": (r.get("goal") or {}).get("title") or "free play", "outcome": r.get("outcome"),
             "turns": r.get("turns"), "inventory": ", ".join((r.get("player") or {}).get("inventory") or []),
             "sent to UNP": ("yes" if (r.get("delivery") or {}).get("sent") else (r.get("delivery") or {}).get("error") or "no"),
             "report": r.get("report_id")} for r in reversed(data["sessions"])]
    return text, pd.DataFrame(rows, columns=["ended", "start", "goal", "outcome", "turns", "inventory", "sent to UNP",
                                             "report"])


@ui
def resend_report_ui(profile):
    """Send the save game's latest session report to the UNP again."""
    sessions = API.get("/v1/progression", profile=profile or None)["sessions"]
    if not sessions:
        raise gr.Error("This save game has no session report yet.")
    delivery = API.post(_path("/v1/progression/reports", sessions[-1]["report_id"], "send"), profile=profile or None)
    return ("Sent to the UNP." if delivery["sent"]
            else f"Not sent: {delivery['error'] or 'HTTP ' + str(delivery['status'])}.")


def quest_overview(profile):
    """(choices of the quest picker, Markdown overview of the world's quests) for this profile:
    the quests open to it now can be chosen; the others are listed, struck through."""
    quests = API.get("/v1/missions/playable", profile=profile or None)["missions"]
    choices = [(ANY_QUEST, "")] + [(q["title"], q["key"]) for q in quests if q["available"]]
    lines = [f"**Quests in this world** (for profile `{profile or 'default'}`):"]
    for quest in quests:
        stages = " → ".join(quest["stages"])
        closed = "achieved in this save game" if quest.get("achieved") else "not open now"
        lines.append(f"- {'**' + quest['title'] + '**' if quest['available'] else '~~' + quest['title'] + f'~~ ({closed})'}"
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


FINISHED_RUNS = "The chosen finished run(s)"
EXPORT_SOURCES = {  # what the Python version can be made from; the ANE AI only from finished runs
    FINISHED_RUNS: "engine",
    "A generation of this run": "run",
    "Best remembered storyteller": "memory",
    "This game's storyteller": "game",
}


def export_platform_choices():
    """(label, platform) of what an export can run on (GET /v1/exports/platforms)."""
    try:
        listed = API.get("/v1/exports/platforms")["platforms"]
    except APIError:
        return [("Python (3.10+ with pyyaml and numpy)", "python")]
    names = {"python": "Python (3.10+ with pyyaml and numpy)", "unity": "Unity (.unitypackage: library and runtimes)"}
    return [((names.get(p["platform"], f"Standalone {p['platform']}")
              + ("" if p["available"] else " (not available here)")), p["platform"]) for p in listed]


# What an ANE AI (a standalone program or the Unity package) is made from: a finished run (its engine).
ANE_AI_PLATFORMS = ("windows-x64", "linux-x64", "macos-arm64", "macos-x64", "unity")
NO_COMPLETE_ENGINE = ("**No finished run yet.** An export is made from a finished run (its engine: world, "
                      "storyteller, training state): run a training (*Evolve storytellers & start game*), or load an "
                      "engine, then export it here.")


def finished_run_choices():
    """(label, engine id) of the finished runs (complete engines) to export, newest first
    (GET /v1/exports/engines)."""
    try:
        engines = API.get("/v1/exports/engines")["engines"]
    except APIError as exc:
        LOG.warning("The finished runs could not be read: %s", exc)
        return []

    def label(e):
        parts = [(e.get("created_at") or "")[:16].replace("T", " "), "loaded engine" if e.get("loaded") else "run"]
        if e.get("generations"):
            parts.append(f"{e['generations']} generation(s)")
        if isinstance(e.get("score"), (int, float)):
            parts.append(f"best {e['score']:.1f}")
        return " · ".join(parts + [e["engine_id"][:8]])

    return [(label(e), e["engine_id"]) for e in engines]


def export_form_ui(platform, source_label=FINISHED_RUNS, chosen=None):
    """The export form: the ANE AI is made from finished runs (the latest is chosen; several can
    be); the Python version also from a generation's champion, the remembered storyteller or this
    game's storyteller. It cannot be exported from a finished run before one exists."""
    ane_ai = platform in ANE_AI_PLATFORMS
    from_runs = ane_ai or source_label == FINISHED_RUNS
    choices = finished_run_choices()
    ids = [value for _, value in choices]
    kept = [engine for engine in (chosen or []) if engine in ids]
    note = (NO_COMPLETE_ENGINE if from_runs and not choices else
            f"{len(kept or ids[:1])} finished run(s) chosen: each is exported with its own world, storyteller, "
            "training state and tag vectors." if from_runs else "")
    return (gr.Radio(visible=not ane_ai),
            gr.Dropdown(visible=not ane_ai and source_label == "A generation of this run"),
            gr.Dropdown(choices=choices, value=kept or ids[:1], visible=from_runs),
            gr.Markdown(note), gr.Button(interactive=not from_runs or bool(choices)))


def _export_status(result):
    score = f"{result['score']:.1f}" if isinstance(result["score"], (int, float)) else "not benchmarked"
    if result.get("engine_id"):
        score += f", engine `{result['engine_id'][:8]}`"
    return (f"Exported **{result['name']}**: {result['source']} (fitness {score}, the training objective). "
            f"World: {result['world']['locations']} locations, {result['world']['npcs']} NPCs, "
            f"{result['world']['missions']} missions. "
            + ("Unzip it and run `python run.py play` or `python run.py serve`." if result.get("platform", "python") == "python"
               else "Import it in Unity (Assets > Import Package > Custom Package), then Tools > ANE AI > Add the ANE AI "
                    "to the scene." if result.get("platform") == "unity"
               else f"Unzip it on {result['platform']} and start `ane-ai serve` (no Python needed); the README explains "
                    "how a UNP launches, drives and stops it."))


@ui
def export_engine_ui(source_label, run_id, generation, token, include_memory, name, platform="python", engine_ids=None):
    """Export: from the chosen finished runs (one download each), or, for the Python version, from a
    generation's champion, the remembered storyteller or this game's storyteller. Returns
    ([downloaded files], status)."""
    platform = platform or "python"
    if platform in ANE_AI_PLATFORMS or source_label == FINISHED_RUNS:
        engine_ids = [engine_ids] if isinstance(engine_ids, str) else list(engine_ids or [])
        if not engine_ids:
            raise gr.Error(NO_COMPLETE_ENGINE.replace("**", ""))
        files, lines = [], []
        for engine_id in engine_ids:
            label = f"{name}-{engine_id[:8]}" if name and len(engine_ids) > 1 else (name or None)
            result = API.post("/v1/exports", {"source": "engine", "engine_id": engine_id, "platform": platform,
                                              "include_memory": bool(include_memory), "name": label})
            files.append(API.download(result["download_url"]))
            lines.append(_export_status(result))
        return files, "\n\n".join(lines)
    source = EXPORT_SOURCES.get(source_label, "run")
    result = API.post("/v1/exports", {
        "source": source, "run_id": run_id or None,
        "generation": int(generation) if source_label == "A generation of this run" and generation not in (None, "") else None,
        "session_id": token or None, "include_memory": bool(include_memory), "name": name or None,
        "platform": platform,
    })
    return [API.download(result["download_url"])], _export_status(result)


# ---- ▶ Play: the world of a finished run, as a game ---------------------------------------------

CHOICE_BUTTONS = 8
PLAY_STATS = ["story default", "random", "balanced"]


def studio_status_ui():
    """One line on the studio's state: the active world, the trained AIs, recording."""
    parts = []
    try:
        world = API.get("/v1/world/catalogue")
        parts.append(f"🌍 **World:** {len(world['locations'])} places · {len(world['npcs'])} people · "
                     f"{len(world['items'])} items · {len(world['events'])} events · {len(world['missions'])} quests")
    except APIError as exc:
        LOG.warning("The world could not be read: %s", exc)
    try:
        runs = API.get("/v1/exports/engines")["engines"]
        parts.append(f"🧠 **Trained AIs:** {len(runs)}" + ("" if runs else " (train one in ④)"))
    except APIError as exc:
        LOG.warning("The trained AIs could not be read: %s", exc)
    try:
        parts.append("🗂 Recording " + ("on" if API.get("/v1/records")["enabled"] else "off"))
    except APIError:
        pass
    return " · ".join(parts)


def _hud(state):
    """The game's status bar: where, health (a bar), coins, the quest's current step, an event."""
    health, most = state.get("health"), state.get("max_health")
    parts = [f"📍 **{state.get('location') or '?'}**"]
    if isinstance(health, int) and most:
        filled = max(0, min(10, round(10 * max(0, health) / most)))
        parts.append(f"❤ `{'█' * filled}{'░' * (10 - filled)}` {health}/{most}")
    if state.get("coins") is not None:
        parts.append(f"🪙 {state['coins']}")
    quest = state.get("quest") or {}
    if quest:
        now = next((stage["title"] for stage in quest.get("stages") or [] if stage.get("state") == "current"), None)
        parts.append(f"🎯 {quest.get('title')}" + (f" · now: {now}" if now else ""))
    if state.get("event"):
        parts.append(f"⚡ {state['event']}")
    if state.get("talking_to"):
        parts.append(f"💬 talking with {state['talking_to']}")
    return "  ·  ".join(parts)


def _play_screen(state):
    """The game screen for a play state: how the story ended (when it has), the status bar, the
    scene, what just happened, a button per choice (with the check it involves), the quest log,
    the people and the map, and the choices' names (what the buttons take)."""
    actions = list(state.get("actions") or [])
    checks = {item["name"]: item.get("check") for item in state.get("choices") or []}
    buttons = [gr.Button(value=f"{i + 1}.  {actions[i]}"
                         + (f"   ·   🎲 {_check_text(checks[actions[i]])}" if checks.get(actions[i]) else ""),
                         visible=True) if i < len(actions) else gr.Button(visible=False)
               for i in range(CHOICE_BUTTONS)]
    ending = ""
    if state.get("ended"):
        ending = ("## 🏆 Quest complete\n" if state.get("won") else "## The end\n") + (state.get("end_reason") or "")
    turn = state.get("last_turn") or ""
    return (gr.Markdown(value=ending, visible=bool(ending)), gr.Markdown(value=_hud(state), visible=True),
            state["scene"], gr.Markdown(value=f"### What happened\n{turn}" if turn else "", visible=bool(turn)),
            *buttons, quest_markdown(state.get("quest")), people_markdown(state), actions)


def _story_so_far(token):
    try:
        return API.get(_path("/v1/sessions", token, "transcript"))["transcript"]
    except APIError:
        return ""


def play_runs_ui(chosen=None):
    """The trained AIs to play (finished runs, newest first; the newest chosen)."""
    choices = finished_run_choices()
    ids = [value for _, value in choices]
    return gr.Dropdown(choices=choices, value=chosen if chosen in ids else (ids[0] if ids else None))


def play_quests_ui(profile):
    choices, _ = quest_overview(profile)
    return gr.Dropdown(choices=choices, value="")


@ui
def play_new_game_ui(engine_id, profile, mission, stats_choice):
    """A new game in the world of the chosen finished run (the newest when none is chosen)."""
    body = {"profile": profile or None, "mission": mission or None}
    if stats_choice in ("random", "balanced"):
        body["stats_mode"] = stats_choice
    game = API.post(_path("/v1/engines", engine_id or "latest", "play"), body)
    return (game["session_id"], *_play_screen(game), _story_so_far(game["session_id"]), game.get("report") or "")


@ui
def play_choose_ui(token, actions, index):
    """The player takes the choice of button ``index``."""
    if not token:
        raise gr.Error("Start a new game first (choose a trained AI and press New game).")
    if index >= len(actions or []):
        raise gr.Error("That choice is no longer there.")
    state = API.post(_path("/v1/sessions", token, "actions"), {"action": actions[index]})
    return (*_play_screen(state), _story_so_far(token))


@ui
def play_end_ui(token):
    """Stop here: the progression is saved and the session reported."""
    if not token:
        raise gr.Error("Start a new game first.")
    state = API.post(_path("/v1/sessions", token, "end"))
    return (*_play_screen(state), _story_so_far(token))


@ui
def play_continue_ui(upload, token):
    """Continue a save game (from here, tab 4 or the ANE AI)."""
    if not upload:
        raise gr.Error("Choose a save game (.json) to continue.")
    try:
        doc = json.loads(Path(upload).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise gr.Error(f"Not a JSON save game: {exc}") from exc
    state = API.post("/v1/sessions/load", {"savegame": doc, "session_id": token or None})
    status = ("Continued exactly where it was saved." if state["restored"] == "exact"
              else f"Rebuilt from its parameters ({state.get('note') or 'no usable snapshot'}).")
    return (state["session_id"], *_play_screen(state), _story_so_far(state["session_id"]), status)


# ---- records (/v1/records) -------------------------------------------------------------------

def _recorded(path, key, **params):
    """Recorded items; none when there are no records yet or they cannot be read (the studio must
    start and work without them; the records status says why)."""
    try:
        return API.get(path, **params)[key]
    except APIError as exc:
        if exc.status != 404:  # not "no records yet": say so in the log, keep the studio running
            LOG.warning("The records could not be read (%s): %s", path, exc)
        return []


@ui
def records_status_text():
    try:
        info = API.get("/v1/records")
    except APIError as exc:
        return f"The records cannot be reached: {exc}"
    if info["enabled"]:
        return ("Recording is on: every run, engine, world and story generated here, in the API or in the terminal "
                "is kept in the records.")
    return (f"Recording is off: {info.get('reason') or 'ANE_RECORDS=off'}."
            + (" The records already there are shown." if info.get("readable") else "")
            + " Finished runs are kept with the saves, so they can still be exported.")


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
        {"run": r["id"][:8], "created": r["created_at"][:16], "user": r.get("user_id") or "test", "origin": r["origin"],
         "kind": r["kind"], "status": r["status"], "generations": r["generations_run"], "best score": r["best_score"],
         "engine": (r["best_engine_id"] or "")[:8]}
        for r in _recorded("/v1/records/runs", "runs")
    ])


@ui
def recorded_run_choices():
    return [(f"{r['created_at'][:16]} · {r['origin']} {r['kind']} · {r['status']}"
             + (f" · best {r['best_score']:.1f}" if r["best_score"] is not None else "") + f" · {r['id'][:8]}", r["id"])
            for r in _recorded("/v1/records/runs", "runs")]


@ui
def recorded_world_choices():
    return [(f"{w['created_at'][:16]} · {w['locations']} locations, {w['npcs']} NPCs, {w['missions']} missions"
             f" · {w['id'][:8]}", w["id"]) for w in _recorded("/v1/records/worlds", "worlds")]


@ui
def run_detail_ui(run_id):
    """A recorded run: its settings and every generation (GET /v1/records/runs/{id})."""
    if not run_id:
        return "", pd.DataFrame()
    run = API.get(_path("/v1/records/runs", run_id))
    best = f"{run['best_score']:.1f}" if isinstance(run.get("best_score"), (int, float)) else "-"
    head = (f"**{run['origin']} {run['kind']}** · {run['status']} · best fitness {best} · "
            f"{run.get('generations_run') or 0} generation(s) · world `{run.get('world_id')}` · "
            f"{len(run.get('engines') or [])} engine(s), {len(run.get('stories') or [])} story(ies)"
            + (f"\n\n**Error:** {run['error']}" if run.get("error") else ""))
    settings = {key: run.get(key) for key in ("settings", "parameters") if run.get(key)}
    generations = pd.DataFrame([{key: g.get(key) for key in ("generation", "best", "mean", "worst", "validation",
                                                             "diversity", "mutation_step", "improved")}
                                for g in run.get("generations") or []])
    return head + (f"\n\n```yaml\n{to_yaml(settings)}```" if settings else ""), generations


@ui
def download_world_ui(world_id):
    if not world_id:
        raise gr.Error("Choose a recorded world first.")
    return API.download(_path("/v1/records/worlds", world_id) + "?format=yaml")


@ui
def jobs_table_ui():
    """The background jobs the API keeps (GET /v1/jobs): evolutions, continued trainings, judge runs."""
    rows = []
    for job in API.get("/v1/jobs")["jobs"]:
        progress = job.get("progress") or {}
        rows.append({"job": job["id"][:8], "kind": job["kind"], "what": job["description"], "status": job["status"],
                     "created": (job.get("created_at") or "")[:19],
                     "progress": f"{progress['generation']}/{progress['total']}" if progress else "",
                     "error": job.get("error") or ""})
    return pd.DataFrame(rows, columns=["job", "kind", "what", "status", "created", "progress", "error"])


@ui
def active_config_ui():
    """The configuration games and runs use now (GET /v1/config): every file merged, or an upload."""
    data = API.get("/v1/config")
    source = "an uploaded configuration" if data["using_override"] else "the configuration files"
    return f"Active now: **{source}**.", to_yaml(data["config"])


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


# ---- the judge (Gemma 4 as a game critic: /v1/sessions/{id}/judge, /v1/records/stories/{id}/judge,
# /v1/judge/compare) -------------------------------------------------------------------------------

def judge_status_ui():
    """Whether the judge can be used here and, if not, why and how to make it available
    (GET /v1/judge/status)."""
    status = API.get("/v1/judge/status")
    if status.get("available"):
        where = f" at `{status['server']}`" if status.get("server") else " (in the API's process)"
        return f"✅ **The judge is ready:** {status.get('model')}{where}."
    return (f"⚠ **The judge is not available here.** {status.get('reason') or ''}\n\n"
            f"{status.get('how') or ''}")


def judgement_markdown(result, title="Judgement"):
    """A judgement of one story: engagement, the criteria, the best and weakest moments, the verdict."""
    if result.get("engagement") is None:
        return f"### {title}\nThe judge's reply could not be read; try again."
    scores = " · ".join(f"{name} **{value:.1f}**" for name, value in (result.get("scores") or {}).items())
    spread = f" (spread {result['spread']:.1f})" if isinstance(result.get("spread"), (int, float)) else ""
    return "\n\n".join(line for line in (
        f"### {title}: engagement {result['engagement']:.1f}/10{spread}", scores,
        f"**Best moment:** {result.get('best_moment') or '-'}", f"**Weakest moment:** {result.get('weakest_moment') or '-'}",
        f"> {result.get('verdict') or ''}",
    ) if line)


@ui
def judge_session_ui(token):
    if not token:
        raise gr.Error("Start a game first.")
    return judgement_markdown(API.post(_path("/v1/sessions", token, "judge")), "This playthrough")


@ui
def judge_story_ui(story_id):
    if not story_id:
        raise gr.Error("Choose a recorded story first.")
    return judgement_markdown(API.post(_path("/v1/records/stories", story_id, "judge")), "This story")


def comparison_markdown(result, a="A", b="B"):
    winner = {"A": a, "B": b, "tie": "neither: a tie"}.get(result.get("winner"), "unreadable")
    criteria = " · ".join(f"{name} → {({'A': a, 'B': b}).get(side, side)}" for name, side in (result.get("criteria") or {}).items())
    agreed = "both readings agreed" if result.get("agreed") else "the two readings did not agree (a tie)"
    return f"### The better game: {winner}\n{agreed}\n\n{criteria}\n\n> {result.get('reason') or ''}"


@ui
def compare_stories_ui(story_a, story_b):
    if not story_a or not story_b or story_a == story_b:
        raise gr.Error("Choose two different recorded stories.")
    result = API.post("/v1/judge/compare", {"a": {"story_id": story_a}, "b": {"story_id": story_b}})
    return comparison_markdown(result, "the first story", "the second story")


# ---- tab 6 · evaluation with the judge (/v1/jobs/judge-benchmark, /v1/jobs/calibrate-fitness,
# /v1/fitness/term-weights) --------------------------------------------------------------------------

def _variants(text):
    variants = from_yaml(text, "Version B's settings") or {}
    if not isinstance(variants, dict):
        raise gr.Error("Version B's settings must be a mapping of dotted.path: value.")
    return variants


@ui
def judge_benchmark_ui(stories, seed, engine_id, pairwise, variants_text, workers, progress=gr.Progress()):
    variants = _variants(variants_text) if pairwise else {}
    job = API.post("/v1/jobs/judge-benchmark", {
        "stories": int(stories), "seed": int(seed or 0), "engine_id": engine_id or None, "pairwise": bool(pairwise),
        "variants": variants, "workers": int(workers),
    })
    result = API.wait(job, _job_progress(progress, "Judged"))
    if pairwise:
        rate = result.get("b_win_rate")
        low, high = result["wilson_95"]
        a_rate = None if rate is None else 1 - rate
        criteria = " · ".join(f"{name} {1 - value:.0%}" for name, value in result["criteria"].items() if value is not None)
        summary = (f"### Current settings (A) vs version B: A won {result['a_wins']}, B won {result['b_wins']}, "
                   f"{result['ties']} tie(s)\n"
                   + (f"A's win rate **{a_rate:.0%}** of decided pairs (95% interval {1 - high:.0%}-{1 - low:.0%})."
                      if a_rate is not None else "No pair was decided.")
                   + (f"\n\nA's win rate per criterion: {criteria}" if criteria else ""))
        table = pd.DataFrame([{"seed": r["seed"], "winner": r["winner"], "agreed": r["agreed"],
                               "fitness A": round(r["fitness_a"], 1), "fitness B": round(r["fitness_b"], 1),
                               "reason": r["reason"]} for r in result["rows"]])
    else:
        engagement = result.get("engagement")
        criteria = " · ".join(f"{name} {value:.1f}" for name, value in result["criteria"].items())
        summary = (f"### Engagement {engagement:.2f}/10 over {result['judged']} judged stories\n{criteria}"
                   if engagement is not None else "### No judgement could be read.")
        table = pd.DataFrame([{"seed": r["seed"], "engagement": r["engagement"], "fitness": round(r["fitness"], 1),
                               "turns": r["turns"], "won": r["won"], "verdict": r["verdict"]} for r in result["rows"]])
    return summary, table


@ui
def calibrate_fitness_ui(stories, seed, engine_id, apply, progress=gr.Progress()):
    job = API.post("/v1/jobs/calibrate-fitness", {"stories": int(stories), "seed": int(seed or 0),
                                                  "engine_id": engine_id or None, "apply": bool(apply)})
    result = API.wait(job, _job_progress(progress, "Judged"))
    cv = result.get("cv_spearman")
    summary = (f"### Fitted on {result['n']} judged stories\nAgreement with the judge (Spearman): "
               f"{result['spearman']:+.2f} on the stories fitted, "
               + (f"**{cv:+.2f} on stories left out**" if cv is not None else "not cross-validated (too few stories)")
               + f" (ridge strength {result['alpha']:g}). "
               + ("The weights were saved as `fitness.term_weights`." if result["applied"]
                  else "Apply them below, or edit them first."))
    return summary, to_yaml(result["term_weights"])


@ui
def load_term_weights_ui():
    return to_yaml(API.get("/v1/fitness/term-weights")["weights"])


@ui
def save_term_weights_ui(text):
    weights = from_yaml(text, "Term weights") or {}
    if not isinstance(weights, dict):
        raise gr.Error("Term weights must be a mapping of fitness term: multiplier.")
    result = API.put("/v1/fitness/term-weights", {"weights": weights})
    return to_yaml(result["weights"]), result["message"]


# ---- what people and places do, by their tags (/v1/behaviours, /v1/logic/npc_behaviour, ...) -------

# The two behaviours of each kind of entry: (the API's group, the behaviour, its label in the editor).
BEHAVIOUR_FIELDS = {
    "NPCs": (("npcs", "starts_conversation", "Comes up to the player"),
             ("npcs", "appearances", "How often they are seen")),
    "Locations": (("locations", "draws_in", "Draws the player in"),
                  ("locations", "visits", "How many visits a story may have there")),
}
BEHAVIOUR_WORDS = {"never": "never", "always": "always", "once": "once", "first_meeting": "the first time they meet",
                   "every_visit": "on every visit", "first_visit": "on the first visit"}


def _behaviour_words(value):
    return f"{value} visits" if isinstance(value, int) else BEHAVIOUR_WORDS.get(value, str(value))


def _behaviour_spec(kind_value, slot):
    specs = BEHAVIOUR_FIELDS.get(kind_value)
    if not specs:
        return None, None
    group, key, label = specs[slot]
    return (API.get("/v1/behaviours").get(group) or {}).get(key) or {}, label


def behaviour_fields_ui(kind_value, tags_value):
    """The behaviour choices of an NPC or a place: every tag a rule gives the behaviour, and the one in
    effect among the entry's tags (the first in the rules' order)."""
    updates = []
    have = {str(tag).lower() for tag in tags_value or []}
    for slot in (0, 1):
        spec, label = _behaviour_spec(kind_value, slot)
        if spec is None:
            updates.append(gr.update(visible=False))
            continue
        choices = [(f"— none: {_behaviour_words(spec.get('default'))} (the default)", "")] + [
            (f"{option['tag']} · {_behaviour_words(option['value'])}", option["tag"]) for option in spec.get("tags", [])]
        current = next((option["tag"] for option in spec.get("tags", []) if option["tag"].lower() in have), "")
        updates.append(gr.update(visible=True, label=label, choices=choices, value=current))
    return tuple(updates)


def apply_behaviour_ui(kind_value, slot, chosen, tags_value):
    """Choosing a behaviour sets its tag on the entry: the other tags of that behaviour leave its tags."""
    spec, _ = _behaviour_spec(kind_value, slot)
    if spec is None:
        return gr.update()
    others = {option["tag"].lower() for option in spec.get("tags", [])}
    tags = [tag for tag in tags_value or [] if str(tag).lower() not in others] + ([chosen] if chosen else [])
    return gr.update(value=tags)


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
            "Create a world, train an AI storyteller on it, then play it as a game.",
            elem_classes="hero",
        )
        gr.Markdown(WORKFLOW, elem_classes="workflow")
        studio_status = gr.Markdown(elem_classes="status")
        demo.load(studio_status_ui, None, studio_status)

        with gr.Tabs():
            with gr.Tab("1 · Create the world"):
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
                with gr.Row():  # what an NPC or a place does, given by its tags (npc_behaviour, location_behaviour)
                    behaviour_a = gr.Dropdown([], label="Behaviour", visible=False, interactive=True,
                                              info="Sets the tag that gives it (Save entry to keep it).")
                    behaviour_b = gr.Dropdown([], label="Behaviour", visible=False, interactive=True,
                                              info="Sets the tag that gives it (Save entry to keep it).")
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
                    npc_role = gr.Dropdown(
                        [INFERRED_ROLE, *REFERENCE.get("npc_roles", [])], value=INFERRED_ROLE, label="Role in the story",
                        visible=False,
                        info="How much the NPC matters: importance, how hard they are to find and to beat "
                        "(`npc_roles` in the engine logic). Inferred: the first `npc_roles.infer` rule its tags match "
                        "(the quest's hostile target is its nemesis).",
                    )
                    npc_secret = gr.Textbox(
                        label="Secret", visible=False,
                        placeholder="e.g. {text: '{npc} fences the stolen goods.', talks: 2, effects: [{reveal_location: Sewers}]}",
                        info="Comes out once spoken with `talks` times (and `clues` things learned): [SECRET] text, its "
                        "effects, and actions with `enabled_if: {secret_known: <this NPC>}`. Empty: no secret.",
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

                with gr.Accordion("Behaviour by tags (people and places)", open=False):
                    gr.Markdown(
                        "What people and places do comes from their **tags**: a rule gives everyone with one of its "
                        "tags a behaviour, and the choices above set those tags. **People**: `starts_conversation` "
                        "(never, first_meeting, every_visit) and `appearances` (always, once, a number of visits). "
                        "**Places**: `draws_in` (never, first_visit, every_visit: on arrival their own activities "
                        "first) and `visits` (always, once, a number). The first rule that sets a behaviour and "
                        "shares a tag decides; someone or somewhere the quest needs now is always there. Add a rule "
                        "with a new tag and it is a new choice above."
                    )
                    with gr.Row():
                        npc_rules = gr.Code(language="yaml", label="People (npc_behaviour)", lines=12)
                        place_rules = gr.Code(language="yaml", label="Places (location_behaviour)", lines=12)
                    with gr.Row():
                        save_npc_rules = gr.Button("Save the people's rules")
                        save_place_rules = gr.Button("Save the places' rules")
                    rules_status = gr.Markdown()
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

                with gr.Accordion("Every field of this entry (YAML)", open=False):
                    gr.Markdown(
                        "The whole entry as stored: what the form shows and everything else (an NPC's role, secret, "
                        "dialogue, known places and items; a location's distant views, exits and prompts; its "
                        "actions). The whole world is checked with the entry in place before it is saved; rename "
                        "an entry with the form above."
                    )
                    entity_yaml_editor = gr.Code(value=load_entity_yaml_ui("Locations", selected_name), language="yaml",
                                                 label="entry")
                    with gr.Row():
                        save_entity_yaml_button = gr.Button("Validate & save the entry", variant="primary")
                        reload_entity_yaml_button = gr.Button("Reload from disk")
                    entity_yaml_status = gr.Markdown()

                with gr.Accordion("Map: which places border which", open=False):
                    gr.Markdown(REFERENCE["documents"].get("map", "") + " A route holds both ways; a far place is "
                                "reached through the places between. `routes: null`: every story draws its own map.")
                    map_editor = gr.Code(value=load_document("map"), language="yaml", label="map")
                    with gr.Row():
                        save_map_button = gr.Button("Validate & save the map", variant="primary")
                        reload_map_button = gr.Button("Reload from disk")
                    map_status = gr.Markdown()
                    save_map_button.click(lambda text: save_document("map", text), map_editor, [map_editor, map_status])
                    reload_map_button.click(lambda: load_document("map"), None, map_editor)

                with gr.Accordion("Failures that lead nowhere", open=False):
                    gr.Markdown(
                        "Actions that can fail (a skill check, or failure outcomes) where failing changes nothing but "
                        "health or coins and no complication event fits: the failures worth writing a consequence for."
                    )
                    lint_button = gr.Button("Check the world's failures")
                    lint_report = gr.Markdown()
                    lint_button.click(failure_lint_ui, None, lint_report)

                npc_fields = [unique, encounter_event, encounter_repeat, enabled_if, event_title, stat_bonus,
                              start_eligible, start_weight, npc_role, npc_secret]
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
                entity.change(load_entity_yaml_ui, [kind, entity], entity_yaml_editor)
                save_entity_yaml_button.click(save_entity_yaml_ui, [kind, entity, entity_yaml_editor],
                                              [entity_yaml_editor, entity_yaml_status]).then(
                    load_entity_ui, [kind, entity], entry_fields,
                ).then(load_entity_actions_yaml, [kind, entity], actions_editor)
                reload_entity_yaml_button.click(load_entity_yaml_ui, [kind, entity], entity_yaml_editor)
                entity.change(load_entity_actions_yaml, [kind, entity], actions_editor)
                entity.change(load_entity_policy_yaml, [kind, entity], policy_editor)
                save_policy_button.click(save_entity_policy_ui, [kind, entity, policy_editor], [policy_editor, policy_status])
                reload_policy_button.click(load_entity_policy_yaml, [kind, entity], policy_editor)
                save_actions_button.click(
                    save_entity_actions_ui, [kind, entity, actions_editor], [actions_editor, actions_status],
                ).then(action_picker, [kind, entity], repetition_fields)
                reload_actions_button.click(load_entity_actions_yaml, [kind, entity], actions_editor)
                add_entry.click(new_entity, None, [entity, *entry_fields])
                save_entry.click(save_entity_ui, [kind, entity, *entry_fields], [entity, editor_status]).then(
                    load_entity_yaml_ui, [kind, entity], entity_yaml_editor,
                )
                delete_entry.click(delete_entity_ui, [kind, entity], [entity, *entry_fields, editor_status])
                # What an NPC or a place does follows its tags: the choices show the tag in effect, and
                # choosing one sets it (an entry loaded, a tag typed: the choices follow).
                behaviours = [behaviour_a, behaviour_b]
                kind.change(behaviour_fields_ui, [kind, tags], behaviours)
                tags.change(behaviour_fields_ui, [kind, tags], behaviours)
                demo.load(behaviour_fields_ui, [kind, tags], behaviours)
                behaviour_a.input(lambda k, c, t: apply_behaviour_ui(k, 0, c, t), [kind, behaviour_a, tags], tags)
                behaviour_b.input(lambda k, c, t: apply_behaviour_ui(k, 1, c, t), [kind, behaviour_b, tags], tags)
                demo.load(lambda: (load_logic_section("npc_behaviour"), load_logic_section("location_behaviour")),
                          None, [npc_rules, place_rules])
                save_npc_rules.click(lambda text: save_logic_section("npc_behaviour", text), npc_rules, rules_status).then(
                    behaviour_fields_ui, [kind, tags], behaviours)
                save_place_rules.click(lambda text: save_logic_section("location_behaviour", text), place_rules,
                                       rules_status).then(behaviour_fields_ui, [kind, tags], behaviours)

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
                with gr.Accordion("The active configuration (read only)", open=False):
                    gr.Markdown("Every configuration file merged (or the uploaded configuration): what new games "
                                "and runs use now.")
                    show_config = gr.Button("Show")
                    config_source = gr.Markdown()
                    active_config = gr.Code(language="yaml", label="active configuration", interactive=False)
                    show_config.click(active_config_ui, None, [config_source, active_config])

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

            with gr.Tab("4 · Train the AI"):
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
                run_work = gr.Markdown(run_work_ui(limits["default_candidates"], limits["default_generations"],
                                                   limits["default_simulation_steps"],
                                                   min(10, int(defaults["episodes_per_genome"]))))
                for slider in (candidates, generations, sim_steps, episodes):
                    slider.change(run_work_ui, [candidates, generations, sim_steps, episodes], run_work,
                                  show_progress="hidden")
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
                with gr.Accordion("Save game: this profile's progression", open=False):
                    gr.Markdown("Every session saves the player's progression: its report (goal achieved or mission "
                                "failed, stats, inventory) and, when achieved, its goal, which then leaves the goal "
                                "pool (each goal once per save game). The report goes to the UNP "
                                "(`session_report` in the engine logic). *Reset profile* (Simulation settings) starts "
                                "a new save game.")
                    with gr.Row():
                        save_game_refresh = gr.Button("Show the save game")
                        resend_report_button = gr.Button("Send the latest report to the UNP again")
                    save_game_text = gr.Markdown()
                    save_game_status = gr.Markdown()
                    save_game_table = gr.Dataframe(label="Sessions", interactive=False, wrap=True)
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
                with gr.Accordion("Export the engine (offline, for a game): the ANE AI", open=False):
                    gr.Markdown(
                        "Download the engine to run offline in a game. **The ANE AI** (*Runs on* Windows, macOS, "
                        "Linux or Unity) is a standalone program that a UNP launches, drives over HTTP and stops, "
                        "no Python needed. An export is made from a **finished run**: its engine (world, "
                        "storyteller, training state, tag vectors). The latest is chosen; choose several to export "
                        "each. Finished runs are kept (in the records, or with the saves) and can be exported later. "
                        "**The Python version** (for developers: embed it with `NarrativeEngine`, `python run.py "
                        "serve`, `play`, `evolve`) can also be made from a generation's champion, the remembered "
                        "storyteller or this game's storyteller. Each download's README explains how to use it."
                    )
                    with gr.Row():
                        export_platform = gr.Dropdown(
                            export_platform_choices(), value="python", label="Runs on",
                            info="A standalone program for that system (the ANE AI, no Python needed), the Unity "
                            "package, or Python.")
                        export_source = gr.Radio(list(EXPORT_SOURCES), value=FINISHED_RUNS, label="What to export",
                                                 info="The Python version only; the ANE AI is made from finished runs.")
                    finished = finished_run_choices()
                    export_engine = gr.Dropdown(finished, value=[v for _, v in finished[:1]], multiselect=True,
                                                label="Finished runs", info="Newest first: the latest is chosen. "
                                                "Choose several to export each.")
                    export_generation = gr.Dropdown([], label="Generation (for a generation's champion)", visible=False)
                    export_engine_note = gr.Markdown()
                    with gr.Row():
                        export_memory = gr.Checkbox(True, label="Include the learning memory (keep training offline)")
                        export_name = gr.Textbox(label="Name", placeholder="narrative-engine")
                    export_button = gr.Button("Export", variant="primary")
                    export_file = gr.File(label="Downloads (ZIP, or .unitypackage for Unity)", interactive=False,
                                          file_count="multiple")
                    export_status = gr.Markdown()
                    with gr.Accordion("Where the ANE AI runs, and the world it sends", open=False):
                        gr.Markdown("The standalone runtimes this API has (built by CI for each system, from the same "
                                    "engine code), and the world as the ANE AI sends it to a UNP (`GET /world`). "
                                    "How a UNP uses the ANE AI: `docs/UNP.md`.")
                        export_platforms = gr.Dataframe(export_platforms_table(), interactive=False, wrap=True)
                        with gr.Row():
                            refresh_platforms = gr.Button("Refresh")
                            catalogue_button = gr.Button("Download the world as a UNP receives it (JSON)")
                        catalogue_file = gr.File(label="World catalogue (ane.world/1)", interactive=False)
                        catalogue_status = gr.Markdown()
                        refresh_platforms.click(export_platforms_table, None, export_platforms)
                        catalogue_button.click(world_catalogue_ui, None, [catalogue_file, catalogue_status])
                    export_form = [export_source, export_generation, export_engine, export_engine_note, export_button]
                    for control in (export_platform, export_source):
                        control.change(export_form_ui, [export_platform, export_source, export_engine], export_form)
                    demo.load(export_form_ui, [export_platform, export_source, export_engine], export_form)
                    export_button.click(
                        export_engine_ui,
                        [export_source, run_state, export_generation, session_token, export_memory, export_name,
                         export_platform, export_engine],
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
                    with gr.Row():
                        judge_story_button = gr.Button("Judge this story (Gemma 4)")
                        compare_story = gr.Dropdown(recorded_stories(), label="Compare with another recorded story")
                        compare_button = gr.Button("Which is the better game?")
                    story_judgement = gr.Markdown()
                    story_view = gr.Markdown()
                    runs_table = gr.Dataframe(recorded_runs(), label="Recorded runs", interactive=False)
                    record_run = gr.Dropdown(recorded_run_choices(), label="Recorded run: its settings and generations")
                    run_detail = gr.Markdown()
                    run_generations = gr.Dataframe(label="Generations", interactive=False)
                    with gr.Row():
                        record_world = gr.Dropdown(recorded_world_choices(), label="Recorded world")
                        download_world = gr.Button("Download it as a configuration to upload")
                    record_world_download = gr.File(label="World and tuning (YAML)", interactive=False)
                with gr.Row():
                    with gr.Column(scale=3):
                        scene = gr.Markdown("Run a simulation to begin.")
                        choices = gr.Radio([], label="What do you do?", interactive=True,
                                           info="The most relevant choices now: fewer in tense moments.")
                        with gr.Row():
                            choose = gr.Button("Take action", variant="primary")
                            end_session_button = gr.Button("End this session", variant="stop")
                        turn_result = gr.Textbox(label="Last turn", lines=7, interactive=False)
                        with gr.Accordion("Judge this playthrough (Gemma 4 as a game critic)", open=False):
                            game_judge_status = gr.Markdown()
                            judge_game_button = gr.Button("Judge the game played so far")
                            game_judgement = gr.Markdown()
                            demo.load(judge_status_ui, None, game_judge_status)
                    with gr.Column(scale=2):
                        quest_panel = gr.Markdown("### Quest\nStart a game to see its quest.")
                        people_panel = gr.Markdown("### People met\nNobody yet.")
                with gr.Accordion("This session as a UNP sees it: save and continue, the player, events", open=False):
                    gr.Markdown(
                        "What the exported ANE AI does for a UNP, tried on this game: download its **save game** "
                        "(`ane.savegame/1`) and **continue** one (exactly, or rebuilt from its parameters), read its "
                        "**parameters**, **set the player** and tell it **what happened** in the UNP's game. "
                        "Save games of the ANE AI continue here too."
                    )
                    with gr.Row():
                        with gr.Column():
                            savegame_button = gr.Button("Download this session's save game")
                            savegame_file = gr.File(label="Save game (JSON)", interactive=False)
                        with gr.Column():
                            savegame_upload = gr.File(label="Continue a save game (.json)", file_types=[".json"],
                                                      type="filepath")
                            load_savegame_button = gr.Button("Continue it", variant="primary")
                    savegame_status = gr.Markdown()
                    with gr.Row():
                        player_json = gr.Code(value='{"coins": 20}', language="json", lines=4,
                                              label="Set the player (stat_points, stat_changes, inventory, add_items, "
                                                    "remove_items, coins, health, max_health, variables)")
                        with gr.Column():
                            sync_kind = gr.Dropdown(list(SYNC_EVENTS), value="item_gained",
                                                    label="What happened in the UNP's game")
                            sync_value = gr.Textbox(label="Its action, item, NPC, location or amount")
                            with gr.Row():
                                player_button = gr.Button("Set the player")
                                sync_button = gr.Button("Tell the session")
                    parameters_button = gr.Button("Show the session's parameters (ane.session_state/1)")
                    parameters_view = gr.Code(language="json", label="Session parameters", interactive=False)
                generated_config = gr.File(label="Generated story configuration", interactive=False, visible=True)
                generated_engine = gr.File(
                    label="Engine model (world, storyteller, training state, memory, tag vectors)",
                    interactive=False, visible=True,
                )

                play_outputs = [
                    session_token, simulation_report, scene, choices, turn_result,
                    generated_config, generated_engine, quest_panel, people_panel,
                ]
                engine_events = [run_button.click(
                    run_simulation_ui,
                    [candidates, generations, sim_steps, deterministic_run, play_profile, stats_choice, stats_points,
                     episodes, warm_start, play_mission],
                    [*play_outputs, learning_plot, learned_traits, memory_status, run_state, export_generation],
                    concurrency_limit=1,
                    concurrency_id="narrative_evolution",
                )]
                engine_events.append(load_engine_button.click(
                    load_best_engine_ui,
                    [engine_upload, model_parameters_upload, play_profile, stats_choice, stats_points, play_mission],
                    [*play_outputs, learning_plot, learned_traits],
                    concurrency_limit=1,
                ))
                choose.click(play_action_ui, [session_token, choices],
                             [scene, choices, turn_result, quest_panel, people_panel], concurrency_limit=1)
                end_session_button.click(end_session_ui, session_token,
                                         [scene, choices, turn_result, quest_panel, people_panel], concurrency_limit=1)
                save_game_refresh.click(progression_ui, play_profile, [save_game_text, save_game_table])
                game_view = [scene, choices, turn_result, quest_panel, people_panel]
                savegame_button.click(download_savegame_ui, session_token, [savegame_file, savegame_status])
                load_savegame_button.click(load_savegame_ui, [savegame_upload, session_token],
                                           [session_token, *game_view, savegame_status], concurrency_limit=1)
                player_button.click(update_player_ui, [session_token, player_json], game_view, concurrency_limit=1)
                sync_button.click(sync_session_ui, [session_token, sync_kind, sync_value], game_view,
                                  concurrency_limit=1)
                parameters_button.click(session_parameters_ui, session_token, parameters_view)
                resend_report_button.click(resend_report_ui, play_profile, save_game_status)
                play_profile.change(playable_quests_ui, play_profile, [play_mission, quest_overview_md])
                refresh_quests.click(playable_quests_ui, play_profile, [play_mission, quest_overview_md])
                record_outputs = [*play_outputs, learning_plot, learned_traits, run_state, export_generation]
                player_inputs = [play_profile, stats_choice, stats_points, play_mission]
                record_lists = [record_engine, record_story, runs_table, records_status]
                refresh_records.click(refresh_records_ui, None, record_lists).then(
                    lambda: gr.Dropdown(choices=recorded_stories()), None, compare_story,
                )
                judge_game_button.click(judge_session_ui, session_token, game_judgement)
                refresh_records.click(lambda: (gr.Dropdown(choices=recorded_run_choices()),
                                               gr.Dropdown(choices=recorded_world_choices())), None,
                                      [record_run, record_world])
                record_run.change(run_detail_ui, record_run, [run_detail, run_generations])
                download_world.click(download_world_ui, record_world, record_world_download)
                judge_story_button.click(judge_story_ui, record_story, story_judgement)
                compare_button.click(compare_stories_ui, [record_story, compare_story], story_judgement)
                play_record.click(play_recorded_ui, [record_engine, *player_inputs], record_outputs, concurrency_limit=1)
                engine_events.append(continue_record.click(
                    continue_recorded_ui, [record_engine, more_generations, *player_inputs],
                    record_outputs, concurrency_limit=1, concurrency_id="narrative_evolution"))
                engine_events.append(retrain_record.click(
                    retrain_recorded_ui,
                    [record_engine, candidates, more_generations, sim_steps, episodes, deterministic_run,
                     *player_inputs],
                    record_outputs, concurrency_limit=1, concurrency_id="narrative_evolution"))
                for event in engine_events:  # a finished run: it is chosen for the export (the newest)
                    event.then(lambda platform, source: export_form_ui(platform, source), [export_platform, export_source],
                               export_form)
                download_record.click(download_recorded_ui, record_engine, [record_model_file, record_world_file])
                record_story.change(story_view_ui, record_story, story_view)
                for button in (run_button, load_engine_button):
                    button.click(refresh_records_ui, None, record_lists)

            with gr.Tab("▶ Play"):
                gr.Markdown(
                    "### Play the world after training\n"
                    "Choose a **trained AI** (a finished run: the newest is chosen), who plays and the quest, and "
                    "press **New game**: you play the world it was trained on, and its storyteller directs the "
                    "story (what is offered, who is where, events, how the story answers). Each button is a "
                    "choice, with its skill check. Save the game to continue it later, here or in the ANE AI."
                )
                play_token = gr.State("")
                play_actions = gr.State([])
                play_finished = finished_run_choices()
                play_first_quests, _ = quest_overview(profiles["default"])
                with gr.Row():
                    play_run = gr.Dropdown(play_finished, value=play_finished[0][1] if play_finished else None,
                                           label="Trained AI (finished run)", scale=3,
                                           info="Newest first. Train one in 4 · Train the AI.")
                    play_refresh = gr.Button("↻ Refresh", scale=0, min_width=110)
                    play_who = gr.Dropdown(profiles["profiles"], value=profiles["default"], allow_custom_value=True,
                                           label="Player profile", scale=2,
                                           info="Its progression and persistent variables are kept.")
                    play_quest = gr.Dropdown(play_first_quests, value="", label="Quest", scale=2)
                    play_stats = gr.Radio(PLAY_STATS, value="story default", label="Character", scale=2)
                with gr.Row():
                    play_new = gr.Button("New game", variant="primary", scale=2)
                    play_upload = gr.File(label="…or continue a save game (.json)", file_types=[".json"],
                                          type="filepath", scale=2)
                    play_continue = gr.Button("Continue it", scale=1)
                play_status = gr.Markdown()
                with gr.Row():
                    with gr.Column(scale=3):
                        play_ending = gr.Markdown(elem_classes="ending", visible=False)
                        play_hud = gr.Markdown(elem_classes="play-hud", visible=False)
                        play_scene = gr.Markdown("Choose a trained AI and press **New game**.", elem_classes="scene-card")
                        play_turn = gr.Markdown(elem_classes="turn-card", visible=False)
                        play_buttons = [gr.Button(visible=False, elem_classes="choice", size="lg")
                                        for _ in range(CHOICE_BUTTONS)]
                        with gr.Row():
                            play_save = gr.Button("Save game")
                            play_stop = gr.Button("End the game", variant="stop")
                        play_save_file = gr.File(label="Save game (JSON)", interactive=False)
                    with gr.Column(scale=2):
                        play_quest_log = gr.Markdown("### Quest\nStart a game to see its quest.")
                        play_people = gr.Markdown("### People met\nNobody yet.")
                with gr.Accordion("The story so far", open=False):
                    play_story = gr.Textbox(lines=16, max_lines=40, interactive=False, show_label=False,
                                            autoscroll=True)
                play_screen = [play_ending, play_hud, play_scene, play_turn, *play_buttons, play_quest_log,
                               play_people, play_actions]
                play_refresh.click(play_runs_ui, play_run, play_run)
                demo.load(play_runs_ui, play_run, play_run)
                for event in engine_events:  # a run has finished: it is the newest to play
                    event.then(lambda: play_runs_ui(), None, play_run).then(studio_status_ui, None, studio_status)
                play_who.change(play_quests_ui, play_who, play_quest)
                play_new.click(play_new_game_ui, [play_run, play_who, play_quest, play_stats],
                               [play_token, *play_screen, play_story, play_status], concurrency_limit=1)
                play_continue.click(play_continue_ui, [play_upload, play_token],
                                    [play_token, *play_screen, play_story, play_status], concurrency_limit=1)
                for index, button in enumerate(play_buttons):
                    button.click(lambda token, actions, index=index: play_choose_ui(token, actions, index),
                                 [play_token, play_actions], [*play_screen, play_story], concurrency_limit=1)
                play_stop.click(play_end_ui, play_token, [*play_screen, play_story], concurrency_limit=1)
                play_save.click(download_savegame_ui, play_token, [play_save_file, play_status])

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
                with gr.Accordion("Background jobs", open=False):
                    gr.Markdown("Evolutions, continued trainings and judge runs started from here or by any client "
                                "of the API, newest first.")
                    refresh_jobs = gr.Button("Refresh")
                    jobs_table = gr.Dataframe(label="Jobs", interactive=False, wrap=True)
                    refresh_jobs.click(jobs_table_ui, None, jobs_table)

            with gr.Tab("6 · Evaluate with the judge"):
                gr.Markdown(
                    "Gemma 4 as a game critic (`vllm` in the engine logic: an embedded model or a vLLM server). "
                    "Stories are played in a separate process, so the games in progress keep their settings. "
                    "The judge also runs in the loop of evolution when `evolution.judge_weight` > 0 (Engine logic)."
                )
                with gr.Row():
                    judge_status = gr.Markdown()
                    judge_status_button = gr.Button("Check the judge again", size="sm", scale=0)
                demo.load(judge_status_ui, None, judge_status)
                judge_status_button.click(judge_status_ui, None, judge_status)
                with gr.Accordion("Judge the engine's stories", open=True):
                    with gr.Row():
                        bench_stories = gr.Slider(1, 60, value=10, step=1, label="Stories")
                        bench_seed = gr.Number(value=0, precision=0, label="First seed")
                        bench_workers = gr.Slider(1, 8, value=4, step=1, label="Judgements at once")
                    bench_engine = gr.Dropdown([("Current settings, random storytellers", "")] + recorded_engines(),
                                               value="", label="Storyteller")
                    bench_pairwise = gr.Checkbox(
                        label="Compare with version B (pairwise A/B on the same seeds, each pair judged in both orders)")
                    bench_variants = gr.Code(value="# version B: dotted.path: value\nclimax.enabled: false\n",
                                             language="yaml", label="Version B's settings")
                    bench_button = gr.Button("Play and judge", variant="primary")
                    bench_summary = gr.Markdown()
                    bench_table = gr.Dataframe(label="Stories", interactive=False, wrap=True)
                    bench_button.click(judge_benchmark_ui, [bench_stories, bench_seed, bench_engine, bench_pairwise,
                                                            bench_variants, bench_workers], [bench_summary, bench_table],
                                       concurrency_limit=1, concurrency_id="judge")
                with gr.Accordion("Fit the fitness to the judge", open=False):
                    gr.Markdown(
                        "Plays and judges stories, fits the judged engagement on the fitness terms and suggests a "
                        "multiplier per term (`fitness.term_weights`). Trust it as far as its agreement on stories "
                        "left out goes."
                    )
                    with gr.Row():
                        calib_stories = gr.Slider(10, 60, value=30, step=1, label="Stories")
                        calib_seed = gr.Number(value=0, precision=0, label="First seed")
                        calib_engine = gr.Dropdown([("Current settings, random storytellers", "")] + recorded_engines(),
                                                   value="", label="Storyteller")
                        calib_apply = gr.Checkbox(label="Save the suggested weights at once")
                    calib_button = gr.Button("Play, judge and fit", variant="primary")
                    calib_summary = gr.Markdown()
                    term_weights_editor = gr.Code(value=load_term_weights_ui(), language="yaml",
                                                  label="fitness.term_weights (term: multiplier)")
                    with gr.Row():
                        save_weights_terms = gr.Button("Save term weights", variant="primary")
                        reload_weights_terms = gr.Button("Reload")
                    term_weights_status = gr.Markdown()
                    calib_button.click(calibrate_fitness_ui, [calib_stories, calib_seed, calib_engine, calib_apply],
                                       [calib_summary, term_weights_editor], concurrency_limit=1, concurrency_id="judge")
                    save_weights_terms.click(save_term_weights_ui, term_weights_editor,
                                             [term_weights_editor, term_weights_status])
                    reload_weights_terms.click(load_term_weights_ui, None, term_weights_editor)

    demo.launch_settings = limits
    return demo


# The engine's worker processes (training stories, evaluations) are spawned, and a spawned
# process imports the script that started the studio again, as __mp_main__: it needs no UI.
demo = build_app() if __name__ != "__mp_main__" else None


if __name__ == "__main__":
    launch = demo.launch_settings
    demo.queue(default_concurrency_limit=launch["queue_concurrency"], max_size=launch["queue_max_size"]).launch(
        server_name=os.getenv("ANE_UI_HOST", launch["server_host"]),
        server_port=int(os.getenv("ANE_UI_PORT", str(launch["server_port"]))),
        share=os.getenv("ANE_UI_SHARE", str(launch["share"])).lower() in {"1", "true", "yes"},
        css=CSS,
        theme=THEME,
    )
