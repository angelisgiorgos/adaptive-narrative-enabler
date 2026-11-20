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

The destinations (DESTINATIONS; chosen with the workflow banner, or the menu on a narrow screen):

    🌍 World      entries (fields, actions & outcomes, scheduling, details, suggestions, YAML):
                  /v1/entities/..., /v1/suggestions; missions, story rules (rules, messages, variables,
                  stats), the map, behaviour by tags, growing the world, checks: /v1/missions/...,
                  /v1/rules, /v1/documents/..., /v1/variables/..., /v1/map/..., /v1/behaviours/...,
                  /v1/world/..., /v1/jobs/world-proposal, /v1/lint/failures
    🧠 Train      train (/v1/jobs/simulations, /v1/learning), records (/v1/records/..., continue and
                  retrain jobs, import)
    ▶ Play        a game with a trained AI, a recorded engine or a file: /v1/engines/..., /v1/sessions/...
                  (choices, save and continue, the player, events, the judge), /v1/progression
    ⚖ Evaluate    /v1/jobs/judge-benchmark, /v1/jobs/calibrate-fitness, /v1/fitness/term-weights
    📦 Export     /v1/exports/..., /v1/world/catalogue
    ⚙ Advanced    simulation, weights, engine logic, app and jobs: /v1/config/..., /v1/settings,
                  /v1/hyperparameters, /v1/weights, /v1/logic/..., /v1/app-settings, /v1/jobs

tests/test_studio_api.py checks that every endpoint of the API is used here, or is named there
as one other clients use (the studio reaches the same function through another endpoint).
"""

from __future__ import annotations

import atexit
import functools
import html
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
.hero {padding: .5rem 1.1rem; border-radius: 14px; background: linear-gradient(120deg,#17233f,#4a244f); color:#fff}
.hero h1 {margin: 0; color: #fff; font-size: 1.35rem}
.hero p {margin: 0; opacity: .9; color: #fff; font-size: .9rem}
/* Navigation. The destinations are chosen with the workflow banner (wide screens) or the menu (narrow
   screens), so the destinations' own tab bar is not shown: no destination hides behind an overflow button. */
#ane-nav > .tab-wrapper {display: none !important}
#ane-banner {gap: .35rem; align-items: center; flex-wrap: wrap; padding: .3rem .6rem; border-radius: 12px;
             border: 1px solid var(--border-color-primary); background: var(--background-fill-secondary)}
#ane-banner::before {content: "Workflow"; font-size: .8rem; font-weight: 600; opacity: .7; margin-right: .15rem}
#ane-banner > button {flex: 0 0 auto !important; width: auto !important; min-width: max-content !important;
                      white-space: nowrap !important; font-size: .9rem !important; padding: .3rem .8rem !important}
#ane-banner > button.nav-step:not(.nav-last)::after {content: "→"; margin-left: .55rem; opacity: .55}
#ane-banner > button.nav-advanced {margin-left: auto !important}
#ane-menu {display: none !important}
@media (max-width: 820px) {
  #ane-banner {display: none !important}
  #ane-menu {display: block !important}
}
/* Sections inside a destination: compact tabs; when they do not fit, Gradio's overflow button says "More". */
button[role="tab"] {font-size: .88rem !important; padding: .3rem .75rem !important; min-height: 0 !important}
.tab-wrapper .overflow-menu > button::after {content: "More"; margin-left: .3rem; font-size: .85rem}
.lead p {font-size: .9rem; opacity: .85; margin: .1rem 0}
/* Train: the steps slider when it does not set a story's length (full-length or capped stories). */
.steps-unused {border: 1px dashed #8b5cf6 !important; border-radius: 10px; background: var(--background-fill-secondary)}
.steps-unused input[type="range"] {opacity: .55}
.run-work p {font-size: .9rem; margin: .15rem 0}
.status {border-left: 4px solid #8b5cf6; padding-left: 1rem}
/* 🌍 World → 📝 Entries: the draft's indicator, and the question asked before unsaved changes are left. */
.draft-status {font-size: .85rem; color: var(--body-text-color-subdued); padding: .1rem .2rem}
.draft-status.dirty {color: #b45309; font-weight: 600}
.draft-prompt {border: 2px solid #f59e0b !important; border-radius: 12px; padding: .2rem .6rem;
               background: var(--background-fill-secondary)}
.draft-prompt h3 {font-size: 1rem; margin: .2rem 0}
/* ⚙ Advanced → Weights: a parameter that has no effect while the setting it needs is off. */
.weight-inactive {opacity: .6}
.weight-inactive input {background: var(--background-fill-secondary)}
/* Deleting: the question first (naming what goes), then the Undo bar. */
.delete-confirm {border: 2px solid #ef4444 !important; border-radius: 12px; padding: .2rem .6rem}
.delete-confirm h3 {font-size: 1rem; margin: .2rem 0}
.undo-bar {border: 1px solid var(--border-color-primary) !important; border-left: 5px solid #64748b !important;
           border-radius: 10px; padding: .1rem .6rem; background: var(--background-fill-secondary)}
/* 🧠 Train's completed engine: its card, then the actions that carry its id to Play, Evaluate and Export. */
.result-card {padding: .3rem .9rem; border-radius: 12px; border: 1px solid var(--border-color-primary);
              border-left: 5px solid #10b981; background: var(--background-fill-secondary); min-height: 3.2rem}
.result-card p {font-size: .92rem; margin: .2rem 0}
.result-card h3 {font-size: 1.02rem; margin: .15rem 0 .25rem 0; word-break: break-all}
.result-actions button {white-space: nowrap}
.play-hud {padding: .3rem .8rem; border-radius: 10px; border: 1px solid var(--border-color-primary);
           background: var(--background-fill-secondary)}
.play-hud p {font-size: .92rem; margin: .1rem 0}
.scene-card {padding: .35rem .9rem; border-radius: 12px; border: 1px solid var(--border-color-primary);
             background: var(--block-background-fill); line-height: 1.45}
.scene-card p, .scene-card li {font-size: .93rem; margin: .25rem 0}
.scene-card h3 {font-size: 1.08rem; margin: .15rem 0 .25rem 0}
.turn-card {padding: .3rem .9rem; border-radius: 12px; border: 1px solid var(--border-color-primary);
            border-left: 5px solid #8b5cf6; background: var(--background-fill-secondary); line-height: 1.45}
.turn-card p, .turn-card li {font-size: .9rem; margin: .2rem 0}
.turn-card h3 {font-size: .98rem; margin: .1rem 0 .2rem 0}
.turn-card blockquote {margin: .25rem 0; border-left-color: #c4b5fd}
.turn-card blockquote p {font-size: .96rem}
.quest-card, .you-card {padding: .25rem .9rem; border-radius: 12px; border: 1px solid var(--border-color-primary);
                        background: var(--block-background-fill)}
.quest-card p, .quest-card li, .you-card p, .you-card li, .people-card p, .people-card li {font-size: .88rem;
                                                                                         margin: .2rem 0}
.quest-card h3, .you-card h3, .people-card h3 {font-size: 1rem; margin: .2rem 0}
.journey table {font-size: .85rem}
.story-book {max-height: 480px; overflow-y: auto; padding: .2rem 1rem; line-height: 1.55}
.ending {padding: .4rem 1rem; border-radius: 12px; border-left: 6px solid #22c55e;
         background: var(--background-fill-secondary)}
.ending h2 {font-size: 1.15rem; margin: .1rem 0}
button.choice {justify-content: flex-start !important; text-align: left !important; white-space: normal !important;
               font-weight: 500; font-size: .9rem !important; padding: .32rem .8rem !important;
               min-height: 0 !important; line-height: 1.3 !important}
"""
THEME = gr.themes.Soft(primary_hue="violet", secondary_hue="indigo", neutral_hue="slate")
# Where the studio goes: a destination per step of the work, then the technical settings. The ids are
# stable: the tabs, the banner's buttons (#nav-<id>), the narrow-screen menu and the address
# (?section=<id>) all use them.
DESTINATIONS = [("world", "🌍 World"), ("train", "🧠 Train"), ("play", "▶ Play"), ("evaluate", "⚖ Evaluate"),
                ("export", "📦 Export"), ("advanced", "⚙ Advanced")]
DESTINATION_IDS = [key for key, _ in DESTINATIONS]
WORKFLOW_IDS = DESTINATION_IDS[:5]  # the workflow, in order; Advanced stands apart
EMPTY_ENTITY = {"name": "", "tags": [], "descriptions": [], "goals": [], "unique": False, "encounter_event": None,
                "encounter_repeat": False, "enabled_if": None, "title": None, "stat_bonus": None,
                "start_eligible": False, "start_weight": 1.0, "role": None, "secret": None}
INFERRED_ROLE = "inferred from its tags"


# ---- navigation: the destinations, the workflow banner, the narrow-screen menu --------------------------

def _highlight(destination):
    """The banner's buttons with ``destination`` highlighted, and the menu naming it."""
    return (*[gr.Button(variant="primary" if key == destination else "secondary") for key in DESTINATION_IDS],
            gr.Radio(value=destination))


def navigate(destination):
    """Go to a destination: its tab is selected (the others keep their state), the banner highlights it,
    the menu names it, and it is remembered as the one open (the outputs ``nav_outputs`` in build_app).
    An unknown destination goes to the World."""
    destination = destination if destination in DESTINATION_IDS else "world"
    return (gr.Tabs(selected=destination), *_highlight(destination), destination)


def mark_destination(destination):
    """The banner and the menu follow a destination whose tab was selected another way (and it is
    remembered as the one open)."""
    destination = destination if destination in DESTINATION_IDS else "world"
    return (*_highlight(destination), destination)


def open_requested(request: gr.Request = None):
    """The destination the address asks for (``?section=play``), else the World."""
    params = getattr(request, "query_params", None) or {}
    return navigate(str(params.get("section") or "world").strip().lower())


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


def _number(value):
    return f"{value:g}" if isinstance(value, float) else str(value)


def weight_help(info):
    """A tuning parameter's help, under its label: what the engine does with it, its unit, the engine's
    default, the bounds the engine defines and whether it is a whole number, then its path (a
    parameter without a description says so, with its path)."""
    kind = "whole number" if info["type"] == "integer" else "decimal number"
    if not info["described"]:
        return f"No description yet ({kind}, as written). Parameter: {info['path']}"
    facts = [f"Unit: {info['unit']}"] if info.get("unit") else []
    if info.get("default") is not None:
        facts.append(f"engine default {_number(info['default'])}")
    low, high = info.get("minimum"), info.get("maximum")
    if low is not None and high is not None:
        facts.append(f"from {_number(low)} to {_number(high)}")
    elif low is not None:
        facts.append(f"at least {_number(low)}")
    elif high is not None:
        facts.append(f"at most {_number(high)}")
    facts.append(kind)
    text = f"{info.get('explanation') or ''} {' · '.join(facts)}. Parameter: {info['path']}".strip()
    if info.get("inactive"):  # it has no effect while the setting it needs is off
        text = f"Inactive now: {info['requires']} is off (it takes effect when that is on). {text}"
    return text


def weight_field(info):
    """A tuning parameter's input (⚙ Advanced → Weights): its readable label and help; an integer
    parameter takes whole numbers only; the engine's bounds, where it defines them."""
    integer = info["type"] == "integer"
    return gr.Number(value=info["value"], label=info["label"], info=weight_help(info), precision=0 if integer else None,
                     minimum=info.get("minimum"), maximum=info.get("maximum"),
                     elem_classes=["weight", "weight-described" if info["described"] else "weight-plain",
                                   *(["weight-inactive"] if info.get("inactive") else [])])


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


def quest_markdown(quest, journal=None):
    """The quest log of a game: the quest, how far it is (a bar), its steps (done, with the turn;
    now; still to come) and what to do now."""
    if not quest:
        return "### 🎯 Quest\nThis story has no quest: explore as you like."
    marks = {"done": "✅", "current": "▶️", "todo": "▫️"}
    stages = quest.get("stages") or []
    done_at = {}
    for entry in journal or []:
        for title in entry.get("quest_done") or []:
            done_at.setdefault(title, entry.get("turn"))
    lines = [f"### 🎯 {quest['title']}", f"*{quest.get('description')}*" if quest.get("description") else ""]
    if stages:
        done = sum(1 for stage in stages if stage["state"] == "done")
        lines.append(f"`{'▰' * done}{'▱' * (len(stages) - done)}` **{done} of {len(stages)} steps**")
    for stage in stages:
        line = f"{marks.get(stage['state'], '▫️')} **{stage['title']}**"
        if stage["state"] == "done" and done_at.get(stage["title"]):
            line += f" · turn {done_at[stage['title']]}"
        if stage["state"] == "current" and stage.get("description"):
            line += f"  \n{stage['description']}"
        lines.append(line)
    if quest["status"] == "completed":
        finished = next((entry.get("turn") for entry in journal or [] if entry.get("quest_complete")), None)
        lines.append("🏆 **Completed**" + (f" at turn {finished}." if finished else "."))
    elif quest["status"] != "in progress":
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


def _training_outputs(result):
    """What 🧠 Train shows of a finished training (an evolution, a continued or retrained
    engine): the report, its files, the learning curve, what the storyteller learned, the run and its
    generations (for the export). A training from the studio starts no game: its completed engine
    is played, evaluated or exported with Train's follow-up actions."""
    curve, traits = _learning_outputs(result.get("learning"))
    generations = [str(stats["generation"]) for stats in (result.get("learning") or {}).get("history", [])]
    return (result.get("report") or "", API.download(result["config_download_url"]),
            API.download(result["engine_download_url"]), curve, traits, result.get("run_id"),
            gr.Dropdown(choices=generations, value=generations[-1] if generations else None))


# ---- 🧠 Train: the completed engine, and where it goes next (▶ Play, ⚖ Evaluate, 📦 Export) -------

# A training asked for from the studio only trains: the game is started in ▶ Play when the user asks.
TRAIN_ONLY = {"start_game": False}
TRAINING_KINDS = {"run": "New run", "continue": "Training continued", "retrain": "Retrained"}
NO_RESULT = ("No training has finished here yet. Configure a run above and press **🧠 Evolve storytellers** (or "
             "continue or retrain one in *From a recorded engine*): its completed engine appears here, ready to "
             "play, evaluate or export.")


def completed_engine(result, kind, check=""):
    """The completed engine of a finished training, as Train keeps it for its follow-up actions:
    its exact id (what ▶ Play, ⚖ Evaluate and 📦 Export are given), its run, how it scored, and
    whether the run executed what its summary showed (``check``)."""
    learning = result.get("learning") or {}
    return {"engine_id": result.get("engine_id"), "run_id": result.get("run_id"), "kind": kind,
            "score": learning.get("best_score"), "generations": learning.get("generations_run"), "check": check}


def completed_view_ui(done):
    """Train's result card and its three actions (enabled once a training has completed)."""
    ready = bool(done and done.get("engine_id"))
    if not ready:
        text = NO_RESULT
    else:
        score = f"{done['score']:.2f}" if isinstance(done.get("score"), (int, float)) else "not benchmarked"
        text = (f"### ✅ Completed engine `{done['engine_id']}`\n"
                f"{TRAINING_KINDS.get(done['kind'], done['kind'])} · run `{done['run_id']}` · best fitness on the "
                f"benchmark **{score}** (the training objective) · **{done.get('generations') or 0}** generation(s)"
                + (f"\n\n{done['check']}" if done.get("check") else "")
                + "\n\nPlay, evaluate or export **this** engine: each button opens its destination with this exact "
                "engine chosen. No game has been started.")
    return (text, *[gr.Button(interactive=ready) for _ in range(3)])


@ui
def restore_result_ui(saved):
    """The completed engine this browser trained last (kept across reloads), shown again on its card
    with its actions, if it is still a finished run here; else the card says it is gone. Returns
    (the completed engine, the card, its three actions)."""
    if not saved or not saved.get("engine_id"):
        return (None, *completed_view_ui(None))
    if saved["engine_id"] not in [value for _, value in finished_run_choices()]:
        return (None, NO_RESULT + f"\n\n*The engine trained earlier, `{saved['engine_id']}`, is no longer among the "
                f"finished runs here.*", *completed_view_ui(None)[1:])
    text, *buttons = completed_view_ui(saved)
    return (saved, text + "\n\n*Trained earlier in this browser.*", *buttons)


def _training_done(result, kind, check=""):
    """What a finished training adds to Train's outputs: the completed engine, its card and actions."""
    done = completed_engine(result, kind, check)
    return (done, *completed_view_ui(done))


def _result_engine(done):
    if not done or not done.get("engine_id"):
        raise gr.Error("No training has finished in this session yet: run one in 🧠 Train first.")
    return done["engine_id"]


def _offered(engine_id, choices, where):
    """The completed engine must be among a destination's choices; it is never replaced by another."""
    if engine_id not in [value for _, value in choices]:
        raise gr.Error(f"Engine {engine_id} is no longer offered in {where} (finished runs are kept up to a limit, or "
                       "the API restarted without its records). Nothing was chosen in its place.")


@ui
def play_result_ui(done, profile, mission, stats_choice, stats_points=None):
    """▶ Play this result: a new game with exactly the completed engine starts in ▶ Play, with the
    player, quest and character chosen there (the user asked to play it: this is the game's start).
    Returns the navigation, the trained-AI picker (that engine), the setup panel (folded) and the new
    game (play_new_game_ui's outputs: the session, the screen, the story, the status)."""
    engine_id = _result_engine(done)
    choices = finished_run_choices()
    _offered(engine_id, choices, "▶ Play")
    token, *game, status = play_new_game_ui(engine_id, profile, mission, stats_choice, stats_points)
    return (*navigate("play"), gr.Dropdown(choices=choices, value=engine_id), gr.Accordion(open=False), token,
            *game, f"**Playing the engine just trained, `{engine_id}`.**  \n{status}")


def evaluation_engine_choices():
    """(label, engine id) of the storytellers the judge can play: the current settings with random
    storytellers, the finished runs (newest first, kept with or without the records), then the
    other recorded engines (a generation's champion, ...)."""
    finished = finished_run_choices()
    listed = {value for _, value in finished}
    return ([("Current settings, random storytellers", "")] + finished
            + [(label, value) for label, value in recorded_engines() if value not in listed])


@ui
def evaluate_result_ui(done):
    """⚖ Evaluate this result: Evaluate opens with the completed engine as the storyteller of both
    the judge benchmark and the fitness calibration (nothing is judged until the user starts it)."""
    engine_id = _result_engine(done)
    choices = evaluation_engine_choices()
    _offered(engine_id, choices, "⚖ Evaluate")
    return (*navigate("evaluate"), gr.Dropdown(choices=choices, value=engine_id),
            gr.Dropdown(choices=choices, value=engine_id),
            f"**Storyteller:** the engine just trained, `{engine_id}`. Press **Play and judge** (or *Play, judge "
            "and fit*) to evaluate it.")


@ui
def export_result_ui(done, platform):
    """📦 Export this result: Export opens with the completed engine as the one finished run chosen
    (for any platform). Returns the navigation and the export form."""
    engine_id = _result_engine(done)
    choices = finished_run_choices()
    _offered(engine_id, choices, "📦 Export")
    return (*navigate("export"), gr.Radio(value=FINISHED_RUNS, visible=platform not in ANE_AI_PLATFORMS),
            gr.Dropdown(visible=False),
            gr.Dropdown(choices=choices, value=[engine_id], multiselect=True, visible=True),
            gr.Markdown(f"**Chosen:** the engine just trained, `{engine_id}`, with its own world, storyteller, "
                        "training state and tag vectors."), gr.Button(interactive=True))


def integration_session_text(token):
    """Which game the UNP diagnostics (⚙ Advanced → 🔌 Integration) act on: the one open in ▶ Play."""
    if not token:
        return "**No game is open in ▶ Play.** Start one there; these diagnostics act on it."
    return f"Acting on the game open in **▶ Play**: session `{token}`."


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
def run_simulation_ui(use_population, population, use_generations, generations, use_episodes, episodes, use_steps,
                      steps, reproducibility, use_seed, seed, warm_start, shown=None, progress=gr.Progress()):
    """Evolve storytellers (a background job) with the saved engine defaults and what the form sets
    for this run (RUN_CONTROLS; ``shown``: the resolution its summary showed): the report (with how
    the run compares with that summary), files, learning curve and traits, the learning memory, the
    run (for the export), and the completed engine with its card and follow-up actions. No game is
    started: the engine is played in ▶ Play when the user asks (Play this result)."""
    controls = (use_population, population, use_generations, generations, use_episodes, episodes, use_steps, steps,
                reproducibility, use_seed, seed, warm_start)
    job = API.post("/v1/jobs/simulations", {**_run_overrides(*controls), **TRAIN_ONLY})
    result = API.wait(job, _job_progress(progress))
    report, *outputs = _training_outputs(result)
    check = executed_markdown(result.get("training"), shown)
    return (report, *outputs[:4], learning_memory_text(), *outputs[4:], *_training_done(result, "run", check))


STEPS_LABEL = "Steps per simulation"
# A run's controls in 🧠 Train, in this order (run_controls in build_app): a value is used only when set for the run.
RUN_CONTROLS = ("use_population", "population", "use_generations", "generations", "use_episodes", "episodes",
                "use_steps", "steps", "reproducibility", "use_seed", "seed", "warm_start")
# What the summary shown, the resolution and the run itself are compared on.
RUN_COMPARED = ("population", "generations", "stories_per_storyteller", "requested_steps", "story_turns",
                "full_length_stories", "deterministic", "seed", "warm_start", "warm_start_mode")


def _run_overrides(use_population, population, use_generations, generations, use_episodes, episodes, use_steps,
                   steps, reproducibility, use_seed, seed, warm_start):
    """What a run sets for itself (the body of POST /v1/simulations and /resolve): only what is
    ticked or chosen; everything else is the saved engine default."""
    body = {}
    for use, field, value in ((use_population, "candidate_count", population), (use_generations, "generations", generations),
                              (use_episodes, "episodes", episodes), (use_steps, "simulation_steps", steps),
                              (use_seed, "seed", seed)):
        if use and value not in (None, ""):
            body[field] = int(value)
    if reproducibility in ("on", "off"):
        body["deterministic"] = reproducibility == "on"
    if warm_start in ("on", "off"):
        body["warm_start"] = warm_start == "on"
    return body


def _steps_look(work):
    """The steps slider as the API's answer has it: marked when the steps do not set a story's length."""
    if work is None or work["steps_decide"]:
        return {"label": STEPS_LABEL, "info": "The most turns each story is played.", "elem_classes": ["steps"]}
    if work["full_length_stories"]:
        return {"label": f"{STEPS_LABEL} · not used: full-length stories",
                "info": f"Every story is played to its end: up to {work['effective_max_turns']} turns.",
                "elem_classes": ["steps", "steps-unused"]}
    return {"label": f"{STEPS_LABEL} · capped at {work['effective_max_turns']}",
            "info": f"Stories end by themselves after {work['story_length']} turns; steps above it are not played.",
            "elem_classes": ["steps", "steps-unused"]}


def _reproducibility_choices(defaults):
    return [(f"Saved default ({'deterministic' if defaults['deterministic'] else 'random'})", "default"),
            ("Deterministic", "on"), ("Random", "off")]


def _warm_start_choices(defaults):
    return [(f"Saved default ({'on' if defaults['warm_start'] else 'off'})", "default"),
            ("Continue from earlier runs", "on"), ("Start fresh", "off")]


def resolved_markdown(plan, title="This run, resolved"):
    """What a run will execute, as POST /v1/simulations/resolve answers: each value and where it comes
    from (set for this run, or the saved default), how long a story is and why, the work as its
    multiplication against the limit, and why the run would be refused."""
    e, d, src, w = plan["effective"], plan["defaults"], plan["sources"], plan["work"]

    def origin(name, default):
        source = src.get(name, "default")
        if source == "override":
            return f"set for this run (saved default {default})"
        return "saved default" if source == "default" else f"from {source}"

    if e["full_length_stories"]:
        length = f"**Full-length stories enabled: up to {e['story_turns']} turns**"
        length_from = f"evolution.full_length_stories (steps {e['requested_steps']}: not used)"
    elif w["steps_decide"]:
        length = f"**Bounded stories: up to {e['story_turns']} turns**"
        length_from = f"the steps, {origin('requested_steps', d['simulation_steps'])}"
    else:
        length = f"**Bounded stories: up to {e['story_turns']} turns**"
        length_from = f"the story's end (steps {e['requested_steps']}: not all played)"
    if e["deterministic"]:
        seed = f"**{e['seed']}** (deterministic)" if e["seed"] is not None else "**none** (deterministic, no seed)"
    else:
        seed = "**random** (not deterministic)"
    seed_from = " · ".join(dict.fromkeys(
        origin(name, default) for name, default in (("deterministic", "deterministic" if d["deterministic"] else "random"),
                                                    ("seed", d["seed"]))))
    warm = {"memory": f"**on**: from {plan.get('remembered') or 0} remembered storyteller(s)",
            "engine": "**from the recorded engine**", "off": "**off**: random storytellers"}[e["warm_start_mode"]]
    rows = [("Population", f"**{e['population']:,}** storytellers", origin("population", d["candidate_count"])),
            ("Generations", f"**{e['generations']:,}**", origin("generations", d["generations"])),
            ("Stories per storyteller", f"**{e['stories_per_storyteller']}**",
             origin("stories_per_storyteller", d["episodes"])),
            ("Story length", length, length_from), ("Seed", seed, seed_from),
            ("Warm start", warm, origin("warm_start", "on" if d["warm_start"] else "off"))]
    table = "\n".join([f"#### {title}", "| Setting | This run | From |", "|---|---|---|"]
                      + [f"| {name} | {value} | {source} |" for name, value, source in rows])
    lines = [table]
    if w["override"]:
        lines.append(f"ℹ️ *{STEPS_LABEL} ({e['requested_steps']}) does not set the story length.* "
                     f"{w['override']['reason']}")
    f = w["factors"]
    lines.append(f"🧮 **Work:** {f['storytellers']:,} storytellers × {f['generations']:,} generations × "
                 f"{f['stories_per_storyteller']:,} stories each × {f['turns_per_story']:,} turns = "
                 f"**{w['work']:,}** simulated turns at most, of the **{w['limit']:,}** allowed.")
    lines += [f"ℹ️ {note}" for note in plan.get("notes") or []]
    if not w["allowed"]:
        lines.append(f"⚠ **It will be refused:** {w['over_by']:,} turns over the limit. Set a smaller population, "
                     "fewer generations or stories for this run, lower the saved defaults (⚙ Advanced → Simulation), "
                     "or raise `max_simulation_work` (⚙ Advanced → App & jobs).")
    lines += [f"⚠ **It will be refused:** {error}" for error in plan.get("errors") or []]
    return "\n\n".join(lines)


def run_form_ui(*controls):
    """The Train form after a change: what the run resolves to (POST /v1/simulations/resolve), each
    control showing its saved default (and, unless set for this run, the value it will use), the steps
    slider marked when it does not set the story length, and the summary. Returns updates for
    (population, generations, episodes, steps, seed, reproducibility, warm start, summary, plan)."""
    values = dict(zip(RUN_CONTROLS, controls))
    try:
        plan = API.post("/v1/simulations/resolve", _run_overrides(*controls))
    except (APIError, TypeError, ValueError) as exc:
        return (*[gr.update()] * 7, f"⚠ The run's settings could not be resolved: {exc}", None)
    e, d = plan["effective"], plan["defaults"]

    def slider(use, effective, default):
        update = {"interactive": bool(values[use]),
                  "info": f"Saved default: {default}" + (" · set for this run" if values[use] else " · used unless set here")}
        if not values[use]:
            update["value"] = effective
        return update

    steps = {**slider("use_steps", e["requested_steps"], d["simulation_steps"]), **_steps_look(plan["work"])}
    steps["info"] = f"{steps['info']} Saved default: {d['simulation_steps']}."
    seed = {"interactive": bool(values["use_seed"]), "info": f"Saved default: {d['seed']}"}
    if not values["use_seed"]:
        seed["value"] = d["seed"]
    return (gr.update(**slider("use_population", e["population"], d["candidate_count"])),
            gr.update(**slider("use_generations", e["generations"], d["generations"])),
            gr.update(**slider("use_episodes", e["stories_per_storyteller"], d["episodes"])),
            gr.update(**steps), gr.update(**seed), gr.update(choices=_reproducibility_choices(d)),
            gr.update(choices=_warm_start_choices(d)), resolved_markdown(plan), plan)


def executed_markdown(training, shown=None):
    """After a run: whether it ran with what the summary showed before it started (``shown``, a
    resolution), checked against what it was resolved to and what its evolution ran with."""
    if not training:
        return ""
    executed, resolved = training["executed"], training["resolved"]
    differs = [key for key in RUN_COMPARED if resolved.get(key) != executed.get(key)]
    if shown:
        differs += [key for key in RUN_COMPARED
                    if shown["effective"].get(key) != executed.get(key) and key not in differs]
    seed = f"seed {executed['seed']}" if executed["seed"] is not None else "random"
    warm = f"warm start {executed['warm_start_mode']}"
    if executed["warm_start_mode"] != "off" and executed.get("warm_started") is not None:
        # The summary counted the memory before the run; this is what the run really started from.
        counted = (shown or {}).get("remembered") if shown else training.get("remembered")
        warm += f" (started from {executed['warm_started']} remembered storyteller(s)" + (
            f"; the summary counted {counted}" if counted is not None and counted != executed["warm_started"] else "") + ")"
    ran = (f"{executed['population']:,} storytellers × {executed['generations']:,} generations × "
           f"{executed['stories_per_storyteller']} stories × up to {executed['story_turns']} turns, {seed}, {warm}")
    if not differs:
        return f"✅ **Ran exactly as summarised before it started:** {ran}."
    changes = "; ".join(f"{key}: shown {(shown or {'effective': resolved})['effective'].get(key)}, ran {executed.get(key)}"
                        for key in differs)
    return (f"⚠ **The run differs from the summary shown before it started** ({changes}): the saved defaults "
            f"changed in between. It ran with: {ran}.")


# ---- the session as a UNP sees it (as the exported ANE AI does): save games, player, events ---------

SYNC_EVENTS = {  # what happened in the UNP's game -> the field it names
    "action_done": "action", "item_gained": "item", "item_lost": "item", "npc_removed": "npc",
    "location_visited": "location", "coins_changed": "amount", "health_changed": "amount",
}


def _need_game(token):
    if not token:
        raise gr.Error("Start a game first in ▶ Play (New game, another engine, or continue a save game).")


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
    return _play_update(API.patch(_path("/v1/sessions", token, "player"), player), token)


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
    return _play_update(API.post(_path("/v1/sessions", token, "sync"), {"type": kind, field: value}), token)


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
PLAY_STATS = [("Story default", "story default"), ("Random", "random"), ("Balanced", "balanced"),
              ("My own points", "defined")]
# What the game screen shows, in the order _play_screen returns it (play_part picks one by name).
PLAY_PARTS = ("ending", "hud", "place", "turn", *(f"choice {i + 1}" for i in range(CHOICE_BUTTONS)), "quest", "you",
              "people", "actions", "journey")


def play_part(outputs, name, offset=0):
    """One part of a game screen's outputs (``offset``: the outputs before the screen, e.g. the session)."""
    return outputs[offset + PLAY_PARTS.index(name)]


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
        parts.append(f"🧠 **Trained AIs:** {len(runs)}" + ("" if runs else " (train one in 🧠 Train)"))
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


def place_markdown(state):
    """Where the player is, compactly: the place (or event), who is here and what can be seen."""
    if "description" not in state:  # an older API: its scene as it is
        return state.get("scene") or ""
    lines = [f"### 📍 {state.get('scene_name') or state.get('location') or ''}"]
    if state.get("description"):
        lines.append(state["description"])
    people = []
    for person in state.get("people") or []:
        marks = "".join(mark for flag, mark in ((person.get("quest"), " ★"), (person.get("hostile"), " ⚠"),
                                                (person.get("ally"), " ♥")) if flag)
        role = f" · {person['role']}" if (person.get("importance") or 0) >= 2 and person.get("role") else ""
        people.append(f"**{person['name']}**{marks}{role}")
    if people:
        lines.append("👥 " + " · ".join(people))
    items = [item["name"] for item in state.get("items_here") or []]
    if items:
        lines.append("👁 You see: " + ", ".join(items))
    return "\n\n".join(lines)


def you_markdown(state):
    """The player: inventory, stats (with what items add), what is remembered, the alert, the last check."""
    lines = ["### 🧍 You", f"⚡ power {state['power']:.2f}" if isinstance(state.get("power"), (int, float)) else ""]
    lines.append("🎒 " + (", ".join(state.get("inventory") or []) or "*nothing yet*"))
    stats, points = state.get("stats") or {}, state.get("stat_points") or {}
    if stats:
        lines.append("📊 " + " · ".join(f"{name} **{value}**" + (f" (+{value - points[name]})"
                                                                   if isinstance(points.get(name), int)
                                                                   and value > points[name] else "")
                                         for name, value in stats.items()))
    remembered = {k: v for k, v in (state.get("variables") or {}).items() if k != "playthroughs"}
    if remembered:
        lines.append("💾 " + " · ".join(f"{k} {v}" for k, v in remembered.items()))
    stakes = state.get("stakes") or {}
    if stakes.get("alert"):
        lines.append(f"🚨 alert {stakes['alert']}/{stakes.get('max')}"
                     + (" — the watch is looking for you" if stakes["alert"] >= stakes.get("warn_at", 99) else ""))
    last = state.get("last_check")
    if last:
        lines.append(f"🎲 last check: {last.get('stat')} {last.get('roll')} vs {last.get('target')} — "
                     + ("passed" if last.get("passed") else "failed"))
    return "\n\n".join(line for line in lines if line)


# ---- how a game reads: the last turn, the journey, the quest, the story ---------------------------

STORY_MARKS = {
    "MISSION": "🎯", "MISSION COMPLETE": "🏆", "QUEST": "✅", "TRIUMPH": "🏆", "CLIMAX": "⚔", "FINALE": "⏳",
    "EVENT": "⚡", "EVENT END": "⚡", "UNEXPECTED EVENT": "✨", "TWIST": "✨", "ENCOUNTER": "⚔", "EVENT EFFECT": "·",
    "EVENT DISCOVERY": "🗺", "SPAWN": "🗺", "DISCOVERY": "🗺", "ROUTE": "🧭", "VOYAGE": "🧭", "LEAD": "🧭",
    "ON THE WAY": "🧭", "BACK": "↩", "MOVE ON": "🧭", "AUTO TRANSFER": "🧭", "REVEAL": "👁", "REVELATION": "👁",
    "SECRET": "🤫", "CALLBACK": "💡", "LAST TIME": "🕰", "CHECK": "🎲", "STAT": "📈", "HEALTH": "❤",
    "RESOURCES": "🪙", "REFUND": "🪙", "PERSISTENT": "💾", "FAILURE": "✗", "SETBACK": "✗", "ANOTHER WAY": "↪",
    "ALERT": "🚨", "MANHUNT": "🚨", "SENT": "⛓", "ESCAPE": "🔓", "RELEASED": "🔓", "UNLOCKED": "🔓",
    "NPC REMOVED": "👋", "ALLY": "♥", "CONSEQUENCE": "↻", "END": "🏁", "EPILOGUE": "🏁", "RESUMED": "▶",
}
STRONG_MARKS = {"MISSION", "MISSION COMPLETE", "QUEST", "TRIUMPH", "END", "EPILOGUE", "FINALE", "CLIMAX"}
_TAGGED = re.compile(r"^\[([A-Z ]+)\]\s*(.*)$", re.S)
_ARRIVAL = re.compile(r"^You are in the (.+?)\.\s*(.*)$", re.S)


def _story_line(line):
    """One line of a story as the player reads it: (kind, Markdown), or None for engine bookkeeping
    ("Continuing your activities…", "[LOCATION] Still at…"). Kinds: action, result, arrival, met, tagged
    (with the tag), text."""
    line = str(line or "").strip()
    if not line or line.startswith("Continuing your activities"):
        return None
    if line.startswith("> "):
        return "action", f"**▶ {line[2:].strip()}**"
    if line.startswith("RESULT:"):
        return "result", line[len("RESULT:"):].strip()
    arrival = _ARRIVAL.match(line)
    if arrival:
        return "arrival", f"#### 📍 The {arrival.group(1)}\n{arrival.group(2)}".rstrip()
    if line.startswith("Met "):
        return "met", f"👤 {line}"
    tagged = _TAGGED.match(line)
    if tagged:
        tag, text = tagged.group(1).strip(), tagged.group(2).strip()
        if tag == "LOCATION":
            return None  # the arrival says it, or nothing changed
        mark = STORY_MARKS.get(tag, "•")
        return f"tagged:{tag}", f"{mark} **{text}**" if tag in STRONG_MARKS else f"{mark} {text}"
    return "text", line


def story_markdown(text):
    """A story (its transcript) as the player reads it: each turn's choice in bold after a thin rule,
    places as headings, events, discoveries and the quest marked; engine bookkeeping left out."""
    parts = []
    for line in str(text or "").splitlines():
        shown = _story_line(line)
        if shown is None:
            continue
        kind, markdown = shown
        if kind == "action" and parts:
            parts.append("---")
        parts.append(markdown)
    return "\n\n".join(parts) or "*The story has not started yet.*"


def _signed(value):
    return f"+{value}" if value > 0 else f"−{-value}"


def _changes(entry):
    """What a turn changed, as short chips: ❤ −2 · 🪙 +5 · 🎒 Heavy Shield · 📍 Sewers (new) · 👤 the Thief."""
    chips = []
    if entry.get("health_change"):
        chips.append(f"❤ {_signed(entry['health_change'])}")
    if entry.get("coin_change"):
        chips.append(f"🪙 {_signed(entry['coin_change'])}")
    chips += [f"🎒 got {item}" for item in entry.get("items_gained") or []]
    chips += [f"🎒 {item} gone" for item in entry.get("items_lost") or []]
    if entry.get("moved_to"):
        chips.append(f"📍 {entry['moved_to']}" + (" (new place)" if entry["moved_to"] in (entry.get("new_places") or [])
                                                  else ""))
    if entry.get("met"):
        chips.append("👤 met " + ", ".join(entry["met"]))
    return chips


def turn_card_markdown(summary, turn_text=""):
    """What just happened, at a glance: the choice, the check, what came of it, what it changed, the
    quest's progress; then the rest of the turn's story."""
    if not summary:
        return ""
    choice = re.sub(r"^\([^)]*\)\s*", "", str(summary.get("action") or ""))
    who = summary.get("with")
    lines = ["### What happened", f"**▶ Turn {summary.get('turn')}: you chose _{choice}_**"
             + (f" — with {who}" if who and who.lower() not in choice.lower() else "")
             + (f" · at {summary['where']}" if summary.get("where") else "")]
    check = summary.get("check")
    if check:
        lines.append(f"🎲 **{check.get('stat')}** check: rolled {check.get('roll')}, needed {check.get('needed')} or "
                     f"less → " + ("**passed ✓**" if check.get("passed") else "**failed ✗**"))
    if summary.get("told"):
        failed = not summary.get("succeeded", True) and not check  # a check already says how it went
        lines.append(f"> {'✗ ' if failed else ''}{summary['told']}")
    chips = _changes(summary) + ([f"⚡ {summary['event']}"] if summary.get("event") else [])
    if chips:
        lines.append(" · ".join(chips))
    if summary.get("quest_complete"):
        lines.append("🏆 **Quest complete!**")
    elif summary.get("quest_done"):
        lines.append("🎯 **Quest step done:** " + " · ".join(summary["quest_done"])
                     + (f" → **now:** {summary['quest_now']}" if summary.get("quest_now") else ""))
    more = []
    for line in str(turn_text or "").splitlines():
        shown = _story_line(line)
        if shown is None or shown[0] in ("action", "result", "arrival", "met") \
                or shown[0] in ("tagged:QUEST", "tagged:MISSION COMPLETE"):
            continue
        more.append(shown[1])
    if more:
        lines.append("\n".join(f"<small>{line}</small>  " for line in more[:6]))
    return "\n\n".join(lines)


def _cell_text(text, size=70):
    text = str(text or "").replace("|", "/").replace("\n", " ").strip()
    return text if len(text) <= size else text[:size - 1].rstrip() + "…"


def journey_markdown(journal, show=15):
    """The turns so far as a table (the last ``show``): where, the choice, what came of it, what it
    changed, the quest steps done."""
    if not journal:
        return "*Your journey starts with your first choice.*"
    rows = ["| # | Where | You chose | What happened | Changes | Quest |", "|---|---|---|---|---|---|"]
    for entry in journal[-show:]:
        mark = "✓" if entry.get("succeeded", True) else "✗"
        if entry.get("check"):
            mark = ("✓" if entry["check"].get("passed") else "✗") + f" 🎲{entry['check'].get('stat')}"
        quest = "🏆 complete" if entry.get("quest_complete") else \
            " · ".join(f"✅ {title}" for title in entry.get("quest_done") or [])
        choice = re.sub(r"^\([^)]*\)\s*", "", str(entry.get("action") or ""))
        rows.append(f"| {entry.get('turn')} | {_cell_text(entry.get('where'), 24)} | "
                    f"{_cell_text(choice, 40)}{' · ' + _cell_text(entry['with'], 24) if entry.get('with') else ''} | "
                    f"{mark} {_cell_text(entry.get('told'))} | {_cell_text(' · '.join(_changes(entry)), 60)} | "
                    f"{_cell_text(quest, 60)} |")
    earlier = len(journal) - show
    return (f"*… {earlier} earlier turn(s)*\n\n" if earlier > 0 else "") + "\n".join(rows)


def _play_screen(state, journal=None):
    """The game screen for a play state (PLAY_PARTS): how the story ended (when it has), the status
    bar, the place, what just happened, a button per choice (with the check it involves), the quest
    log, the player, the people and the map, the choices' names (what the buttons take), the journey."""
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
    took_a_turn = any(line.startswith("> ") for line in turn.splitlines())  # not approaching or leaving someone
    card = turn_card_markdown(state.get("turn_summary"), turn) if state.get("turn_summary") and took_a_turn else \
        (f"### What happened\n{story_markdown(turn)}" if turn else "")
    return (gr.Markdown(value=ending, visible=bool(ending)), gr.Markdown(value=_hud(state), visible=True),
            place_markdown(state), gr.Markdown(value=card, visible=bool(card)),
            *buttons, quest_markdown(state.get("quest"), journal), you_markdown(state), people_markdown(state),
            actions, journey_markdown(journal))


def _story_so_far(token):
    """The story so far, as the player reads it (story_markdown)."""
    try:
        return story_markdown(API.get(_path("/v1/sessions", token, "transcript"))["transcript"])
    except APIError:
        return ""


def _journal(token):
    try:
        return API.get(_path("/v1/sessions", token, "journal"))["journal"]
    except APIError:
        return []


def play_runs_ui(chosen=None):
    """The trained AIs to play (finished runs, newest first; the newest chosen)."""
    choices = finished_run_choices()
    ids = [value for _, value in choices]
    return gr.Dropdown(choices=choices, value=chosen if chosen in ids else (ids[0] if ids else None))


def _new_game(game):
    """A game just started: (its session, the game screen, the story so far, the report)."""
    return (game["session_id"], *_play_screen(game, _journal(game["session_id"])), _story_so_far(game["session_id"]),
            game.get("report") or "")


def _play_update(state, token):
    """The game screen and the story after something changed the session (the player set, an event)."""
    return (*_play_screen(state, _journal(token)), _story_so_far(token))


@ui
def play_new_game_ui(engine_id, profile, mission, stats_choice, stats_points=None):
    """A new game in the world of the chosen finished run (the newest when none is chosen)."""
    return _new_game(API.post(_path("/v1/engines", engine_id or "latest", "play"),
                              _player(profile, stats_choice, stats_points, mission)))


@ui
def play_recorded_ui(engine_id, profile, mission, stats_choice, stats_points=None):
    """A new game with a recorded engine (any run's best, a generation's champion, ...)."""
    if not engine_id:
        raise gr.Error("Choose a recorded engine first.")
    return _new_game(API.post(_path("/v1/records/engines", engine_id, "play"),
                              _player(profile, stats_choice, stats_points, mission)))


@ui
def play_engine_file_ui(uploaded_file, model_parameters_file, profile, mission, stats_choice, stats_points=None):
    """A new game with an engine file (an engine model, an older engine ZIP, or a configuration with
    its model parameters); the engine is kept, so it can be played again and exported."""
    if not uploaded_file:
        raise gr.Error("Choose an engine model, an engine ZIP or a configuration YAML first.")
    player = _player(profile, stats_choice, stats_points, mission)
    return _new_game(API.upload("/v1/engines/load", {"file": uploaded_file, "parameters": model_parameters_file},
                                profile=player["profile"], stats_mode=player.get("stats_mode"),
                                mission=player["mission"],
                                stats=json.dumps(player["stats"]) if "stats" in player else None))


@ui
def play_choose_ui(token, actions, index):
    """The player takes the choice of button ``index``."""
    if not token:
        raise gr.Error("Start a new game first (choose a trained AI and press New game).")
    if index >= len(actions or []):
        raise gr.Error("That choice is no longer there.")
    state = API.post(_path("/v1/sessions", token, "actions"), {"action": actions[index]})
    return (*_play_screen(state, _journal(token)), _story_so_far(token))


@ui
def play_end_ui(token):
    """Stop here: the progression is saved and the session reported."""
    if not token:
        raise gr.Error("Start a new game first.")
    state = API.post(_path("/v1/sessions", token, "end"))
    return (*_play_screen(state, _journal(token)), _story_so_far(token))


@ui
def play_continue_ui(upload, token):
    """Continue a save game (saved here or by the ANE AI)."""
    if not upload:
        raise gr.Error("Choose a save game (.json) to continue.")
    try:
        doc = json.loads(Path(upload).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise gr.Error(f"Not a JSON save game: {exc}") from exc
    state = API.post("/v1/sessions/load", {"savegame": doc, "session_id": token or None})
    status = ("Continued exactly where it was saved." if state["restored"] == "exact"
              else f"Rebuilt from its parameters ({state.get('note') or 'no usable snapshot'}).")
    return (state["session_id"], *_play_screen(state, _journal(state["session_id"])),
            _story_so_far(state["session_id"]), status)


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


def import_folder_ui():
    """What waits in the server's import folder (GET /v1/records/import)."""
    found = API.get("/v1/records/import")
    files = found["files"]
    return (f"**{len(files)}** engine or game file(s) in the server's import folder `{Path(found['folder']).name}/`"
            + (": " + ", ".join(files[:12]) + (" …" if len(files) > 12 else "") if files else
               " (set `ANE_ARTIFACT_DIR`, or put `.ane.zip`, engine ZIPs and configurations in `artifacts/`)."))


@ui
def import_records_ui(move, preview, progress=gr.Progress()):
    """Import every engine and game in the server's import folder into the records (a background job)."""
    job = API.post("/v1/jobs/records-import", {"move": bool(move), "preview": bool(preview)})
    report = API.wait(job, lambda state: progress(0.5, desc=str((state or {}).get("label") or "Importing…")))
    failed = report.get("failed") or []
    return (f"Imported **{report['engines']}** new engine(s), linked {report['linked']}, {report['runs']} run(s), "
            f"{report['previews']} preview stor(ies) from {report['files']} file(s)"
            + (f"; **{len(failed)} not imported**: " + "; ".join(f"{f['file']} ({f['error']})" for f in failed[:5])
               if failed else "") + ".")


def refresh_records_ui():
    """The records, listed again wherever they are used: the recorded engines (🧠 Train to continue or
    retrain, 📦 Export to download), the recorded stories (⚖ Evaluate), the runs (📦 Export) and
    whether recording is on (⚙ Advanced → 🗂 Records)."""
    engines = recorded_engines()
    return (gr.Dropdown(choices=engines), gr.Dropdown(choices=engines), gr.Dropdown(choices=recorded_stories()),
            recorded_runs(), records_status_text())


@ui
def continue_recorded_ui(engine_id, more_generations, progress=gr.Progress()):
    """Train a recorded engine further, exactly where it stopped (a background job)."""
    if not engine_id:
        raise gr.Error("Choose a recorded engine first.")
    job = API.post(_path("/v1/jobs/engines", engine_id, "continue"), {"generations": int(more_generations), **TRAIN_ONLY})
    result = API.wait(job, _job_progress(progress))
    return (*_training_outputs(result), *_training_done(result, "continue"))


def _retrain_overrides(more_generations, *controls):
    """A retraining's own settings: the Train form's (a warm start is always from the engine) and
    the further generations."""
    body = _run_overrides(*controls)
    body.pop("warm_start", None)
    body["generations"] = int(more_generations)
    return body


def retrain_form_ui(engine_id, more_generations, *controls):
    """What retraining the chosen engine would execute: its saved defaults with the Train form's
    settings and the further generations (POST /v1/simulations/resolve with engine_id)."""
    if not engine_id:
        return "Choose a recorded engine to see what retraining it would run with.", None
    try:
        plan = API.post("/v1/simulations/resolve", {**_retrain_overrides(more_generations, *controls),
                                                    "engine_id": engine_id})
    except APIError as exc:
        return f"⚠ The retraining's settings could not be resolved: {exc}", None
    return resolved_markdown(plan, "Retraining it, resolved (its saved defaults + the Train form)"), plan


@ui
def retrain_recorded_ui(engine_id, more_generations, use_population, population, use_generations, generations,
                        use_episodes, episodes, use_steps, steps, reproducibility, use_seed, seed, warm_start,
                        shown=None, progress=gr.Progress()):
    """A new evolution seeded with a recorded engine's storyteller (a background job), with that
    engine's saved defaults and what the Train form sets (``shown``: the resolution shown)."""
    controls = (use_population, population, use_generations, generations, use_episodes, episodes, use_steps, steps,
                reproducibility, use_seed, seed, warm_start)
    if not engine_id:
        raise gr.Error("Choose a recorded engine first.")
    job = API.post(_path("/v1/jobs/engines", engine_id, "retrain"),
                   {**_retrain_overrides(more_generations, *controls), **TRAIN_ONLY})
    result = API.wait(job, _job_progress(progress))
    return (*_training_outputs(result),
            *_training_done(result, "retrain", executed_markdown(result.get("training"), shown)))


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


# ---- the world editor's forms (one action, one outcome, scheduling, details, missions, map) ------

NONE = "(none)"
ACTING = "(the person acting)"


def _names_of(kind):
    try:
        return _entity_names(kind)
    except APIError:
        return []


def _stat_names():
    try:
        return list((API.get("/v1/stats")["character_stats"].get("stats") or {}))
    except APIError:
        return []


def _flow(value):
    """A value on one line, as YAML (a condition, a mapping of variables)."""
    return to_yaml(value, flow=True) if value not in (None, "", {}, []) else ""


def _parse_flow(text, what):
    """One-line YAML: a mapping written with or without its braces ("reputation: 1, coins: 2")."""
    text = str(text or "").strip()
    if not text:
        return None
    try:
        value = yaml.safe_load(text if text.startswith(("{", "[")) else "{" + text + "}")
    except yaml.YAMLError as exc:
        raise gr.Error(f"{what}: could not read `{text}` ({exc.problem if hasattr(exc, 'problem') else exc}).")
    return value


def _set(record, key, value):
    """Set a field, or leave it out when it is empty (the defaults apply)."""
    if value in (None, "", NONE, [], {}) or value is False or (isinstance(value, (int, float)) and value == 0
                                                                 and key in ("health_change", "coin_change",
                                                                             "required_coins")):
        record.pop(key, None)
    else:
        record[key] = value


def _outcome_fields(outcome):
    """An outcome's form values (the order of OUTCOME_FIELDS)."""
    remove = outcome.get("remove_npc")
    return (outcome.get("desc", ""), outcome.get("on_check") or "any", list(outcome.get("tags") or []),
            outcome.get("health_change") or 0, outcome.get("coin_change") or 0,
            outcome.get("move_to") or NONE, outcome.get("send_to") or NONE,
            outcome.get("reveal_location") or NONE, outcome.get("reveal_npc") or NONE,
            outcome.get("reveal_object") or NONE, ACTING if remove is True else (remove or NONE),
            bool(outcome.get("spawn")), bool(outcome.get("lead_to_known")),
            _flow(outcome.get("set_variables")), _flow(outcome.get("change_variables")),
            _flow(outcome.get("change_stats")), to_yaml(outcome.get("effects")))


def _outcome_from(outcome, values):
    """The outcome with the form's values (fields the form does not show are kept)."""
    (text, when, tags, health, coins, move, send, place, person, item, remove, spawn, lead, sets, changes, stats,
     effects) = values
    outcome = dict(outcome or {})
    outcome["desc"] = str(text or "").strip()
    _set(outcome, "on_check", None if when == "any" else when)
    _set(outcome, "tags", list(tags or []))
    _set(outcome, "health_change", int(health or 0))
    _set(outcome, "coin_change", int(coins or 0))
    for key, value in (("move_to", move), ("send_to", send), ("reveal_location", place), ("reveal_npc", person),
                       ("reveal_object", item)):
        _set(outcome, key, value)
    _set(outcome, "remove_npc", True if remove == ACTING else remove)
    _set(outcome, "spawn", bool(spawn))
    _set(outcome, "lead_to_known", bool(lead))
    _set(outcome, "set_variables", _parse_flow(sets, "Sets variables"))
    _set(outcome, "change_variables", _parse_flow(changes, "Changes variables"))
    _set(outcome, "change_stats", _parse_flow(stats, "Changes stats"))
    try:
        _set(outcome, "effects", yaml.safe_load(effects) if str(effects or "").strip() else None)
    except yaml.YAMLError as exc:
        raise gr.Error(f"Other effects: could not read the YAML ({exc}).")
    return outcome


def _action_fields(action):
    """An action's form values (the order of ACTION_FIELDS)."""
    check = action.get("skill_check") or {}
    return (action.get("name", ""), action.get("repeat") or REFERENCE["repeat_default"],
            bool(action.get("retry_on_failure")), check.get("stat") or NONE, check.get("modifier") or 0,
            action.get("exclusive_group") or "", action.get("triggers_event") or NONE,
            action.get("required_coins") or 0, action.get("required_object") or NONE, action.get("required_tag") or "",
            bool(action.get("consumes_object")), action.get("collects_object") or NONE,
            _flow(action.get("enabled_if")), _flow(action.get("until")))


def _action_from(action, values):
    (name, repeat, retry, stat, modifier, group, event, coins, item, tag, consumes, collects, enabled, until) = values
    action = dict(action or {})
    action["name"] = str(name or "").strip()
    _set(action, "repeat", None if repeat == REFERENCE["repeat_default"] else repeat)
    _set(action, "retry_on_failure", bool(retry))
    _set(action, "skill_check", None if stat in (None, NONE) else
         {"stat": stat, **({"modifier": int(modifier)} if modifier else {})})
    _set(action, "exclusive_group", str(group or "").strip())
    _set(action, "triggers_event", event)
    _set(action, "required_coins", int(coins or 0))
    _set(action, "required_object", item)
    _set(action, "required_tag", str(tag or "").strip())
    _set(action, "consumes_object", bool(consumes))
    _set(action, "collects_object", collects)
    _set(action, "enabled_if", _parse_flow(enabled, "Offered only if"))
    _set(action, "until", _parse_flow(until, "No longer offered once"))
    return action


def _outcome_choices(action):
    return [(f"{index + 1}. {(outcome.get('desc') or '(nothing told)')[:60]}", index)
            for index, outcome in enumerate(action.get("outcomes") or [])]


def _action_form(kind, name, index, action):
    """Everything the action form shows for ``action`` (index ``index`` of the entry)."""
    state = {"kind": kind, "name": name, "index": index, "action": action, "outcome": 0 if action.get("outcomes") else None}
    outcome = (action.get("outcomes") or [{}])[0]
    return (state, *_action_fields(action), gr.Dropdown(choices=_outcome_choices(action), value=state["outcome"]),
            *_outcome_fields(outcome))


@ui
def action_list_ui(kind, name):
    """The entry's actions, and an empty form."""
    blank = _action_form(kind, name, None, {"name": "", "outcomes": []})
    return (gr.Dropdown(choices=_action_choices(kind, name), value=None), *blank)


@ui
def load_action_form_ui(kind, name, index):
    if index is None or not name:
        return _action_form(kind, name, None, {"name": "", "outcomes": []})
    action = API.get(_path(f"/v1/entities/{kind}", name, "actions", int(index)))["action"]
    return _action_form(kind, name, int(index), action)


@ui
def new_action_ui(kind, name):
    if not name:
        raise gr.Error("Select an entry first.")
    count = len(API.get(_path(f"/v1/entities/{kind}", name, "actions"))["actions"])
    return _action_form(kind, name, count, {"name": "New action", "outcomes": [{"desc": "", "tags": ["neutral"]}]})


def pick_outcome_ui(state, index, *values):
    """Switching outcomes keeps what was typed in the one left."""
    state = dict(state or {})
    action = dict(state.get("action") or {"outcomes": []})
    outcomes = list(action.get("outcomes") or [])
    previous = state.get("outcome")
    if previous is not None and previous < len(outcomes):
        outcomes[previous] = _outcome_from(outcomes[previous], values)
    action["outcomes"] = outcomes
    state.update(action=action, outcome=index)
    shown = outcomes[index] if index is not None and index < len(outcomes) else {}
    return (state, *_outcome_fields(shown))


def new_outcome_ui(state, *values):
    state = dict(state or {})
    action = dict(state.get("action") or {"outcomes": []})
    outcomes = list(action.get("outcomes") or [])
    if state.get("outcome") is not None and state["outcome"] < len(outcomes):
        outcomes[state["outcome"]] = _outcome_from(outcomes[state["outcome"]], values)
    outcomes.append({"desc": "", "tags": ["neutral"]})
    action["outcomes"] = outcomes
    state.update(action=action, outcome=len(outcomes) - 1)
    return (state, gr.Dropdown(choices=_outcome_choices(action), value=state["outcome"]), *_outcome_fields(outcomes[-1]))


def delete_outcome_ui(state):
    state = dict(state or {})
    action = dict(state.get("action") or {"outcomes": []})
    outcomes = list(action.get("outcomes") or [])
    if state.get("outcome") is not None and state["outcome"] < len(outcomes):
        outcomes.pop(state["outcome"])
    action["outcomes"] = outcomes
    state.update(action=action, outcome=0 if outcomes else None)
    return (state, gr.Dropdown(choices=_outcome_choices(action), value=state["outcome"]),
            *_outcome_fields(outcomes[0] if outcomes else {}))


@ui
def save_action_form_ui(state, *values):
    """Save the action in the form (with the outcome being edited); the whole list is checked."""
    state = dict(state or {})
    if not state.get("name") or state.get("index") is None:
        raise gr.Error("Pick an action, or add one, first.")
    action_values, outcome_values = values[:len(ACTION_FIELD_NAMES)], values[len(ACTION_FIELD_NAMES):]
    action = _action_from(state.get("action"), action_values)
    outcomes = list(action.get("outcomes") or [])
    if state.get("outcome") is not None and state["outcome"] < len(outcomes):
        outcomes[state["outcome"]] = _outcome_from(outcomes[state["outcome"]], outcome_values)
    if outcomes:
        action["outcomes"] = outcomes
    else:
        action.pop("outcomes", None)  # an action that only starts an event
    if not action["name"]:
        raise gr.Error("The action needs a name.")
    kind, name, index = state["kind"], state["name"], state["index"]
    API.put(_path(f"/v1/entities/{kind}", name, "actions", index), {"action": action})
    form = load_action_form_ui(kind, name, index)
    return (gr.Dropdown(choices=_action_choices(kind, name), value=index), *form,
            f"Saved the action **{action['name']}** of {name}.", load_entity_actions_yaml(kind, name))


ACTION_FIELD_NAMES = ("name", "repeat", "retry", "stat", "modifier", "group", "event", "coins", "item", "tag",
                      "consumes", "collects", "enabled_if", "until")


# Scheduling: an event's start rules, an item's uniqueness (/v1/entities/{Events|Items}/{name}/policy).

@ui
def policy_form_ui(kind, name):
    """The scheduling form of an event or an item (hidden for places and people)."""
    event, item = kind == "Events", kind == "Items"
    policy = API.get(_path(f"/v1/entities/{kind}", name, "policy"))["policy"] if name and (event or item) else {}
    note = ("" if event or item else
            "Places and people: their behaviour comes from their tags (the choices above, and *Behaviour by tags*).")
    return (gr.update(visible=event), gr.update(visible=item), note,
            policy.get("trigger") or "action", float(policy.get("chance") or 0.0), float(policy.get("weight") or 1.0),
            int(policy.get("cooldown") or 0), int(policy.get("max_occurrences") or 0), policy.get("repeat") or "always",
            bool(policy.get("unique")), policy.get("location") or NONE)


@ui
def save_policy_form_ui(kind, name, trigger, chance, weight, cooldown, most, repeat, unique, location):
    if kind not in ("Events", "Items") or not name:
        raise gr.Error("Pick an event or an item.")
    if kind == "Events":
        policy = {"trigger": trigger, "repeat": repeat}
        if trigger == "random":
            policy.update(chance=float(chance), weight=float(weight or 1.0))
        if cooldown:
            policy["cooldown"] = int(cooldown)
        if most:
            policy["max_occurrences"] = int(most)
    else:
        policy = {"unique": bool(unique)}
        if unique and location not in (None, NONE):
            policy["location"] = location
    API.put(_path(f"/v1/entities/{kind}", name, "policy"), {"policy": policy})
    return f"Saved when **{name}** happens." if kind == "Events" else f"Saved **{name}**'s uniqueness.", \
        load_entity_policy_yaml(kind, name)


# An entry's details (/v1/entities/{kind}/{name}/details): what the entry form does not show.

ARTICLES = [("automatic", ""), ("the", "the"), ("a", "a"), ("an", "an"), ("none: a proper name", "none")]
# A place's points of interest (interaction_points): a row each.
POINT_COLUMNS = ["Label", "Prompt", "Leads to place", "Person", "Item", "Action", "Outcome", "Notes", "Id"]
POINT_FIELDS = ["label", "prompt", "linked_location", "linked_npc", "linked_item", "linked_action", "linked_outcome",
                "notes", "id"]


def _points_table(points):
    return pd.DataFrame([[str(point.get(field) or "") for field in POINT_FIELDS] for point in points or []
                         if isinstance(point, dict)], columns=POINT_COLUMNS)


def _parse_points(table):
    points = []
    for _, row in (table if table is not None else pd.DataFrame()).iterrows():
        point = {field: str(value).strip() for field, value in zip(POINT_FIELDS, row.tolist())
                 if value is not None and str(value).strip() and str(value).strip().lower() != "nan"}
        if point:
            points.append(point)
    return points


@ui
def details_form_ui(kind, name):
    npc, place = kind == "NPCs", kind == "Locations"
    details = API.get(_path(f"/v1/entities/{kind}", name, "details"))["details"] if name and (npc or place) else {}
    places, items = _names_of("Locations"), _names_of("Items")
    article = details.get("article")
    dialogue = pd.DataFrame([[_flow(line.get("enabled_if")), line.get("text") if isinstance(line.get("text"), str)
                              else " / ".join(line.get("text") or [])] for line in details.get("dialogue") or []],
                            columns=["Said when (condition, empty: always)", "Says"])
    return (gr.update(visible=npc), gr.update(visible=place),
            "" if npc or place else "Items and events have no more details than the entry's fields show.",
            gr.update(choices=places, value=details.get("known_locations") or []),
            gr.update(choices=items, value=details.get("known_objects") or []),
            "none" if article == "" else (article or ""), dialogue,
            "\n".join(details.get("distant_descriptions") or []), "\n".join(details.get("entered_descriptions") or []),
            "\n".join(details.get("interact_prompts") or []), bool(details.get("exit_locked")),
            _points_table(details.get("interaction_points")))


@ui
def save_details_ui(kind, name, known_places, known_items, article, dialogue, distant, entered, prompts, locked,
                    points=None):
    if kind not in ("NPCs", "Locations") or not name:
        raise gr.Error("Pick a person or a place.")
    if kind == "NPCs":
        lines = []
        for _, row in (dialogue if dialogue is not None else pd.DataFrame()).iterrows():
            condition, text = str(row.iloc[0] or "").strip(), str(row.iloc[1] or "").strip()
            if text:
                lines.append({"text": text, **({"enabled_if": _parse_flow(condition, "Said when")} if condition else {})})
        details = {"known_locations": list(known_places or []), "known_objects": list(known_items or []),
                   "article": article or None, "dialogue": lines}
    else:
        details = {"distant_descriptions": _lines(distant), "entered_descriptions": _lines(entered),
                   "interact_prompts": _lines(prompts), "exit_locked": bool(locked),
                   "interaction_points": _parse_points(points)}
    API.put(_path(f"/v1/entities/{kind}", name, "details"), {"details": details})
    return f"Saved the details of **{name}**.", load_entity_yaml_ui(kind, name)


# The language model in the editor (/v1/suggestions, /v1/world/size, /v1/jobs/world-proposal,
# /v1/world/additions): suggestions for an entry's text, and new places, people and items.

SUGGESTION_TARGETS = {"descriptions": "Descriptions", "goals": "Goals", "tags": "Tags",
                      "distant_descriptions": "Seen from afar (place)", "entered_descriptions": "On arrival (place)",
                      "interact_prompts": "Ways to go there (place)", "dialogue": "What they say (person)"}


@ui
def suggest_ui(kind, name, field):
    """Gemma's suggestions for a field of the entry, to tick and add to the form."""
    if not name:
        raise gr.Error("Pick an entry first.")
    found = API.post("/v1/suggestions", {"kind": kind, "name": name, "field": field})["suggestions"]
    return gr.update(choices=found, value=found), f"{len(found)} suggestion(s) for **{SUGGESTION_TARGETS[field]}**: " \
        "untick those you do not want and press *Add to the form*."


def add_suggestions_ui(field, ticked, descriptions, goals, tags, distant, entered, prompts, dialogue):
    """The ticked suggestions added to the form's field (saved with *Save entry* or *Save the details*)."""
    ticked = [str(value) for value in ticked or []]
    lines = {"descriptions": descriptions, "goals": goals, "distant_descriptions": distant,
             "entered_descriptions": entered, "interact_prompts": prompts}
    values = dict(lines)
    if field in lines:
        values[field] = "\n".join([*_lines(lines[field]), *(v for v in ticked if v not in _lines(lines[field]))])
    tags = list(tags or []) + ([v for v in ticked if v not in (tags or [])] if field == "tags" else [])
    if field == "dialogue":
        rows = dialogue if dialogue is not None else pd.DataFrame(columns=["Said when (condition, empty: always)", "Says"])
        dialogue = pd.concat([rows, pd.DataFrame([["", v] for v in ticked], columns=rows.columns)], ignore_index=True)
    where = "*Save the details*" if field in ("distant_descriptions", "entered_descriptions", "interact_prompts",
                                               "dialogue") else "*Save entry*"
    return (values["descriptions"], values["goals"], gr.update(value=tags), values["distant_descriptions"],
            values["entered_descriptions"], values["interact_prompts"], dialogue,
            f"Added {len(ticked)} to the form: check it and press {where} to keep them.")


# ---- 🌍 World → 📝 Entries: the draft (what is being edited, against what was last loaded or saved) --
#
# An entry's draft is in parts, each saved by its own button. Its baseline is what the forms held right
# after the last load or successful save, read back from the browser (so a load never looks like an
# edit); the draft has unsaved changes where it differs. Changing the entry, the content type or the
# action, starting a new one, or leaving the editor with unsaved changes undoes the choice and offers
# Save, Discard or Stay; a failed save changes nothing.

DRAFT_PARTS = {"fields": "Fields", "action": "Actions & outcomes", "scheduling": "Scheduling",
               "details": "More details", "entry_yaml": "Every field (YAML)", "actions_yaml": "Actions as YAML",
               "policy_yaml": "Scheduling as YAML"}
ENTRY_FIELD_COUNT, OUTCOME_FIELD_COUNT = 14, 17
# The draft's components (draft_inputs in build_app), part by part in this order.
DRAFT_SIZES = {"fields": ENTRY_FIELD_COUNT, "action": 1 + len(ACTION_FIELD_NAMES) + OUTCOME_FIELD_COUNT,
               "scheduling": 8, "details": 9, "entry_yaml": 1, "actions_yaml": 1, "policy_yaml": 1}
# What the entry editor's loads and saves update (entry_outputs in build_app), segment by segment.
ENTRY_SEGMENTS = {"selection": 2, "fields": ENTRY_FIELD_COUNT, "entry_yaml": 1, "actions_yaml": 1, "policy_yaml": 1,
                  "action": 3 + len(ACTION_FIELD_NAMES) + OUTCOME_FIELD_COUNT, "scheduling": 11, "details": 12,
                  "loaded": 1}
ENTRY_OUTPUTS = sum(ENTRY_SEGMENTS.values())
# What a save makes out of date: reloaded from the store, unless it has unsaved changes of its own.
STALE_AFTER = {"fields": ("entry_yaml",), "action": ("actions_yaml", "entry_yaml"),
               "scheduling": ("policy_yaml", "entry_yaml"), "details": ("entry_yaml",),
               "entry_yaml": ("fields", "action", "scheduling", "details", "actions_yaml", "policy_yaml"),
               "actions_yaml": ("action", "entry_yaml"), "policy_yaml": ("scheduling", "entry_yaml")}
# Two parts editing the same data: with both changed, neither is saved over the other.
DRAFT_CONFLICTS = tuple(("entry_yaml", part) for part in DRAFT_PARTS if part != "entry_yaml") + (
    ("actions_yaml", "action"), ("policy_yaml", "scheduling"))
# The order the parts of a draft are saved in (a rename last; a new entry's fields first).
DRAFT_SAVE_ORDER = ("action", "scheduling", "details", "actions_yaml", "policy_yaml", "entry_yaml", "fields")
WORLD_SECTIONS = {"entries": "📝 Entries", "missions": "🎯 Missions", "story-rules": "📜 Story rules", "map": "🗺 Map",
                  "behaviour": "🏷 Behaviour by tags", "grow": "✨ Grow the world", "checks": "🔍 Checks"}


def _plain(value):
    """A form value as drafts compare it: text without trailing spaces, whole numbers as integers,
    tables as their non-empty rows of text, nothing as ''."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value != value:  # NaN: an empty cell
            return ""
        return int(value) if float(value).is_integer() else round(float(value), 9)
    if isinstance(value, str):
        return "\n".join(line.rstrip() for line in value.strip().splitlines())
    if isinstance(value, pd.DataFrame):
        rows = (tuple(_plain(cell) if not isinstance(cell, str) else cell.strip() for cell in row)
                for row in value.astype(object).values.tolist())
        return tuple(row for row in rows if any(cell != "" for cell in row))
    if isinstance(value, dict):
        if set(value) >= {"headers", "data"}:  # a table as the browser sends it
            return _plain(pd.DataFrame(value["data"] or [], columns=value["headers"] or None))
        return tuple(sorted((str(key), _plain(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_plain(item) for item in value)
    return str(value)


def _split_draft(values):
    parts, at = {}, 0
    for part, size in DRAFT_SIZES.items():
        parts[part] = tuple(values[at:at + size])
        at += size
    if at != len(values):
        raise ValueError(f"A draft has {sum(DRAFT_SIZES.values())} values, not {len(values)}.")
    return parts


def _action_draft(state, values):
    """The action being edited as it would be saved: its fields, every outcome (those switched away from
    are kept in the state) and the one in the form; switching outcomes is not an edit."""
    state = state or {}
    try:
        action = _action_from(state.get("action"), values[:len(ACTION_FIELD_NAMES)])
        outcomes = list(action.get("outcomes") or [])
        at = state.get("outcome")
        if at is not None and at < len(outcomes):
            outcomes[at] = _outcome_from(outcomes[at], values[len(ACTION_FIELD_NAMES):])
        action["outcomes"] = outcomes
        return ("action", state.get("index"), _plain(action))
    except (gr.Error, ValueError, TypeError):  # a value that cannot be read yet: compared as typed
        return ("typed", state.get("index"), _plain(list(values)))


def draft_parts(*values):
    """The draft (draft_inputs' values), part by part, as it compares."""
    split = _split_draft(values)
    return {"fields": _plain(split["fields"]), "action": _action_draft(split["action"][0], split["action"][1:]),
            "scheduling": _plain(split["scheduling"]), "details": _plain(split["details"]),
            **{part: _plain(split[part][0]) for part in ("entry_yaml", "actions_yaml", "policy_yaml")}}


def unsaved_parts(base, *values, scope=None):
    """The parts of the draft that differ from what was last loaded or saved (``base``); a part never
    loaded yet has nothing unsaved."""
    now = draft_parts(*values)
    return [part for part in DRAFT_PARTS
            if (scope is None or part in scope) and part in (base or {}) and now[part] != base[part]]


def _entry_label(loaded):
    loaded = loaded or {}
    return (f"**{loaded['entity']}** ({loaded.get('kind')})" if loaded.get("entity")
            else f"the new {str(loaded.get('kind') or 'entry').rstrip('s')} entry")


def draft_status_markup(dirty, loaded):
    """The editor's draft indicator (data-dirty: the page asks before it is closed or reloaded)."""
    if dirty:
        what = ", ".join(DRAFT_PARTS[part] for part in dirty)
        return (f'<div class="draft-status dirty" data-dirty="1">● Unsaved changes in '
                f'{html.escape((loaded or {}).get("entity") or "the new entry")}: {html.escape(what)}</div>')
    return '<div class="draft-status" data-dirty="0">✓ No unsaved changes</div>'


def draft_status_ui(base, loaded, *values):
    """The indicator after an edit: the draft compared with what was last loaded or saved."""
    return draft_status_markup(unsaved_parts(base, *values), loaded)


def capture_draft_ui(parts, base, loaded, *values):
    """After a load or a save: the parts it loaded or saved (``parts``) become the baseline, as the
    browser now holds them. Returns (baseline, indicator, no parts left to capture)."""
    base = dict(base or {})
    if parts:
        now = draft_parts(*values)
        base.update({part: now[part] for part in parts})
    return base, draft_status_markup(unsaved_parts(base, *values), loaded), []


def _entry_update(**segments):
    """An update of the entry editor (entry_outputs, ENTRY_SEGMENTS): a segment not given is untouched."""
    out = []
    for name, size in ENTRY_SEGMENTS.items():
        value = segments.get(name)
        if value is None:
            out += [gr.skip()] * size
        else:
            if len(value) != size:
                raise ValueError(f"The {name} segment has {size} outputs, not {len(value)}.")
            out += list(value)
    return tuple(out)


def _part_load(part, kind, name, index=None):
    """A part of the entry editor as stored (``index``: the action to show; none: the list, a blank form)."""
    if part == "fields":
        return _entry_values(kind, _entity(kind, name))
    if part == "entry_yaml":
        return (load_entity_yaml_ui(kind, name),)
    if part == "actions_yaml":
        return (load_entity_actions_yaml(kind, name),)
    if part == "policy_yaml":
        return (load_entity_policy_yaml(kind, name),)
    if part == "action":
        if index is None or not name:
            return action_list_ui(kind, name)
        return (gr.Dropdown(choices=_action_choices(kind, name), value=index), *load_action_form_ui(kind, name, index))
    if part == "scheduling":
        return policy_form_ui(kind, name)
    if part == "details":
        return details_form_ui(kind, name)
    raise ValueError(part)


def _entry_load(kind, name, names=None):
    """Everything the entry editor shows for an entry (or a new one: ``name`` None), as stored."""
    names = _entity_names(kind) if names is None else names
    return _entry_update(selection=(gr.update(value=kind), gr.Dropdown(choices=names, value=name)),
                         loaded=({"kind": kind, "entity": name},),
                         **{part: _part_load(part, kind, name) for part in DRAFT_PARTS})


@ui
def load_selection_ui(kind, name):
    """The entry chosen, loaded (the studio opening): the editor, and every part to capture."""
    return (*_entry_load(kind, name), list(DRAFT_PARTS))


# The guarded choices: what they output (draft_outputs in build_app) is the entry editor, the
# navigation (navigate), the World's sections, the pending choice, the prompt and its message, and the
# parts to capture once the browser shows them.

def _draft_update(entry=None, nav=None, section=None, pending=gr.skip, prompt=None, message=None, capture=()):
    return (*(entry or _entry_update()), *(nav or (gr.skip(),) * (len(DESTINATIONS) + 3)),
            gr.skip() if section is None else gr.Tabs(selected=section),
            gr.skip() if pending is gr.skip else pending,
            gr.skip() if prompt is None else gr.update(visible=prompt),
            gr.skip() if message is None else message, list(capture))


def _going(target):
    kind = target["type"]
    if kind == "entry":
        return f"opening {target.get('entity') or 'the content type'} ({target['kind']})"
    if kind == "new_entry":
        return "starting a new entry"
    if kind == "action":
        return "starting a new action" if target.get("new") else "opening another action"
    if kind == "destination":
        return f"leaving the editor for {dict(DESTINATIONS)[target['id']]}"
    return f"leaving the editor for 🌍 World → {WORLD_SECTIONS[target['id']]}"


def _ask(target, loaded, dirty, values, note=""):
    """The choice undone, and Save, Discard or Stay offered."""
    loaded = loaded or {}
    segments = {}
    if target["type"] == "entry":
        segments["selection"] = (gr.update(value=loaded.get("kind")), gr.Dropdown(value=loaded.get("entity")))
    elif target["type"] == "action":
        state = _split_draft(values)["action"][0] or {}
        segments["action"] = (gr.Dropdown(value=state.get("index")),
                              *[gr.skip()] * (ENTRY_SEGMENTS["action"] - 1))
    what = ", ".join(DRAFT_PARTS[part] for part in dirty)
    message = (f"### ● Unsaved changes\n{_entry_label(loaded)} has unsaved changes in **{what}**. Save them before "
               f"{_going(target)}? **Discard** drops them; **Stay** keeps editing." + (f"\n\n{note}" if note else ""))
    return _draft_update(entry=_entry_update(**segments),
                         nav=navigate("world") if target["type"] == "destination" else None,
                         section="entries" if target["type"] in ("destination", "section") else None,
                         pending=target, prompt=True, message=message)


def _perform(target, loaded, values, reload_current=False, refresh=()):
    """Carry out the choice: open the entry, the new entry or the action, or leave the editor (with
    ``reload_current``: the entry is reloaded as stored first, its draft gone); ``refresh``: parts made
    out of date by a save, reloaded too."""
    loaded = loaded or {}
    kind, name = loaded.get("kind"), loaded.get("entity")
    done = {"pending": None, "prompt": False, "message": ""}
    if target["type"] == "entry":
        return _draft_update(entry=_entry_load(target["kind"], target.get("entity")), capture=DRAFT_PARTS, **done)
    if target["type"] == "new_entry":
        return _draft_update(entry=_entry_load(kind, None), capture=DRAFT_PARTS, **done)
    if target["type"] == "action":
        if target.get("new"):
            action = (gr.skip(), *new_action_ui(kind, name))
        else:
            action = (gr.Dropdown(choices=_action_choices(kind, name), value=target.get("index")),
                      *load_action_form_ui(kind, name, target.get("index")))
        reloads = {part: _part_load(part, kind, name) for part in refresh}
        return _draft_update(entry=_entry_update(action=action, **reloads), capture=["action", *refresh], **done)
    entry = _entry_load(kind, name) if reload_current else None
    return _draft_update(entry=entry, nav=navigate(target["id"]) if target["type"] == "destination" else None,
                         section=target["id"] if target["type"] == "section" else None,
                         capture=DRAFT_PARTS if reload_current else (), **done)


def _choose(target, loaded, base, values, scope=None):
    dirty = unsaved_parts(base, *values, scope=scope)
    return _ask(target, loaded, dirty, values) if dirty else _perform(target, loaded, values)


@ui
def entry_choice_ui(kind, name, loaded, base, *values):
    """The user chose another entry or content type (that content type's first entry)."""
    loaded = loaded or {}
    if kind != loaded.get("kind"):
        names = _entity_names(kind)
        name = names[0] if names else None
    if (kind, name) == (loaded.get("kind"), loaded.get("entity")):
        return _draft_update()
    return _choose({"type": "entry", "kind": kind, "entity": name}, loaded, base, values)


@ui
def new_entry_choice_ui(loaded, base, *values):
    """*New entry*: a blank form for the content type."""
    return _choose({"type": "new_entry"}, loaded, base, values)


@ui
def action_choice_ui(index, loaded, base, *values):
    """Another of the entry's actions picked: only the action form's draft is at stake."""
    state = _split_draft(values)["action"][0] or {}
    if index is not None and index == state.get("index"):
        return _draft_update()
    return _choose({"type": "action", "index": index}, loaded, base, values, scope=("action",))


@ui
def new_action_choice_ui(loaded, base, *values):
    """*New action*: a new action's form (saved with *Save action*)."""
    if not (loaded or {}).get("entity"):
        raise gr.Error("Select an entry first.")
    return _choose({"type": "action", "new": True}, loaded, base, values, scope=("action",))


def section_choice_ui(section, loaded, base, *values):
    """Another section of 🌍 World opened: with unsaved changes the editor stays open and asks."""
    dirty = unsaved_parts(base, *values)
    return _ask({"type": "section", "id": section}, loaded, dirty, values) if dirty else _draft_update()


def nav_choice_ui(destination, now, loaded, base, *values):
    """The banner or the menu: leaving 🌍 World (``now``) with unsaved changes asks first; anything
    else goes at once (navigate)."""
    destination = destination if destination in DESTINATION_IDS else "world"
    dirty = unsaved_parts(base, *values) if now == "world" and destination != "world" else []
    if dirty:
        return _ask({"type": "destination", "id": destination}, loaded, dirty, values)
    return _draft_update(nav=navigate(destination))


class DraftNotSaved(Exception):
    """A draft that could not be saved (whole): what was saved before, and why the rest was not."""

    def __init__(self, message, saved=(), loaded=None):
        super().__init__(message)
        self.message, self.saved, self.loaded = message, list(saved), loaded


def _save_part(part, kind, name, split):
    """Save one part of the draft with its own save (the API checks it). Returns (the entry's name
    afterwards, the message); a failure raises gr.Error."""
    if part == "fields":
        dropdown, message = save_entity_ui(kind, name, *split["fields"])
        return _prop_value(dropdown), message
    if part == "action":
        return name, save_action_form_ui(split["action"][0], *split["action"][1:])[-2]
    if part == "scheduling":
        return name, save_policy_form_ui(kind, name, *split["scheduling"])[0]
    if part == "details":
        return name, save_details_ui(kind, name, *split["details"])[0]
    saver = {"entry_yaml": save_entity_yaml_ui, "actions_yaml": save_entity_actions_ui,
             "policy_yaml": save_entity_policy_ui}[part]
    return name, saver(kind, name, split[part][0])[1]


def _prop_value(component):
    return component["value"] if isinstance(component, dict) else getattr(component, "value", None)


def _error_text(exc):
    """An error as the prompt shows it: escaped (a YAML error names '<stream end>', not a tag)."""
    return html.escape(str(getattr(exc, "message", None) or exc))


def _save_draft(dirty, loaded, values):
    """Save every part with unsaved changes, each with its own save; nothing when two of them edit the
    same data. Returns (the entry as it is now, the parts saved); a failure raises DraftNotSaved."""
    loaded = dict(loaded or {})
    conflict = next(((a, b) for a, b in DRAFT_CONFLICTS if a in dirty and b in dirty), None)
    if conflict:
        raise DraftNotSaved(f"⚠ **Nothing was saved:** {DRAFT_PARTS[conflict[0]]} and {DRAFT_PARTS[conflict[1]]} both "
                            "change the same data. Choose **Stay** and save the one to keep with its own button, or "
                            "**Discard** both.", (), loaded)
    kind, name = loaded.get("kind"), loaded.get("entity")
    order = sorted(dirty, key=DRAFT_SAVE_ORDER.index)
    if name is None and "fields" in order:  # a new entry exists once its fields are saved
        order.remove("fields")
        order.insert(0, "fields")
    split, saved = _split_draft(values), []
    for part in order:
        try:
            name, _ = _save_part(part, kind, name, split)
        except gr.Error as exc:
            done = f" ({', '.join(DRAFT_PARTS[p] for p in saved)} saved before it)" if saved else ""
            raise DraftNotSaved(f"⚠ **{DRAFT_PARTS[part]} could not be saved**{done}: {_error_text(exc)}  \n"
                                "Nothing was discarded: correct it and **Save** again, **Stay**, or **Discard**.",
                                saved, {"kind": kind, "entity": name}) from exc
        saved.append(part)
    return {"kind": kind, "entity": name}, saved


@ui
def resolve_draft_ui(choice, pending, loaded, base, *values):
    """The prompt's answer: **save** the unsaved changes and go on (a failed save changes nothing:
    the draft, the entry and the prompt stay, with why), **discard** them and go on, or **stay**."""
    if not pending or choice == "stay":
        return _draft_update(pending=None, prompt=False, message="")
    if choice == "discard":
        return _perform(pending, loaded, values, reload_current=True)
    scope = ("action",) if pending["type"] == "action" else None
    dirty = unsaved_parts(base, *values, scope=scope)
    try:
        loaded_now, saved = _save_draft(dirty, loaded, values)
    except DraftNotSaved as failure:
        renamed = failure.loaded and failure.loaded.get("entity") != (loaded or {}).get("entity")
        entry = None
        if renamed:  # a new entry was created (its fields saved) before another part failed: it is chosen
            kind, created = failure.loaded.get("kind"), failure.loaded.get("entity")
            entry = _entry_update(selection=(gr.skip(), gr.Dropdown(choices=_entity_names(kind), value=created)),
                                  loaded=(failure.loaded,))
        return _draft_update(entry=entry, pending=pending, prompt=True, message=failure.message, capture=failure.saved)
    if pending["type"] == "action":
        others = set(unsaved_parts(base, *values)) - {"action"}
        refresh = [part for part in STALE_AFTER["action"] if part not in others]
        return _perform(pending, loaded_now, values, refresh=refresh)
    return _perform(pending, loaded_now, values, reload_current=True)


@ui
def save_part_ui(part, loaded, base, *values):
    """A form's own save button: that part is saved (a failure changes nothing), then reloaded as
    stored, with what the save made out of date unless it has unsaved changes of its own. A new or
    renamed entry is reloaded under its name. Returns (the entry editor, the message, the parts to capture)."""
    loaded = dict(loaded or {})
    kind, name = loaded.get("kind"), loaded.get("entity")
    split = _split_draft(values)
    new_name, message = _save_part(part, kind, name, split)
    others = set(unsaved_parts(base, *values)) - {part}
    segments = {}
    if part == "fields" and new_name != name:  # created or renamed: what is not being edited follows it
        reload = [p for p in DRAFT_PARTS if p not in others]
        loaded = {"kind": kind, "entity": new_name}
        segments["selection"] = (gr.skip(), gr.Dropdown(choices=_entity_names(kind), value=new_name))
        segments["loaded"] = (loaded,)
        if "action" in others:  # its action form saves under the new name
            state = {**(split["action"][0] or {}), "name": new_name}
            segments["action"] = (gr.skip(), state, *[gr.skip()] * (ENTRY_SEGMENTS["action"] - 2))
        kept = sorted(others, key=list(DRAFT_PARTS).index)
    else:
        reload = [part, *(p for p in STALE_AFTER[part] if p not in others)]
        kept = [p for p in STALE_AFTER[part] if p in others]
    index = (split["action"][0] or {}).get("index") if part == "action" else None
    for p in reload:
        segments[p] = _part_load(p, kind, new_name, index=index if p == "action" else None)
    if kept:
        message += ("\n\n⚠ Not reloaded, to keep their unsaved changes (they may no longer match what is stored): "
                    + ", ".join(DRAFT_PARTS[p] for p in kept) + ".")
    return (*_entry_update(**segments), message, reload)


@ui
def reload_part_ui(part, loaded, base, *values):
    """*Reload from disk* / *Discard changes*: that part as stored (the action being edited stays chosen)."""
    loaded = loaded or {}
    state = _split_draft(values)["action"][0] or {}
    index = state.get("index") if part == "action" and state.get("index") is not None and \
        state.get("index") < len(_action_choices(loaded.get("kind"), loaded.get("entity"))) else None
    return (*_entry_update(**{part: _part_load(part, loaded.get("kind"), loaded.get("entity"), index=index)}), [part])


@ui
def refresh_entry_names_ui(kind, name):
    """The entries' names after the world grew: the entry being edited stays chosen (nothing reloaded)."""
    return gr.Dropdown(choices=_entity_names(kind), value=name)


# ---- deleting, with a question first and Undo after (entries, actions, missions) ---------------------
#
# *Delete* asks first, naming exactly what goes (content type, name, place in its list, what goes with
# it); the confirmed deletion removes that one, even if the form moved on meanwhile. The API keeps the
# deleted record whole with its place (DELETE ... -> undo.token), and this session's **Undo** puts it
# back (POST /v1/deletions/{token}/restore): only it, where it was, never over anything newer.

ENTRY_WORDS = {"Locations": "place", "NPCs": "person", "Items": "item", "Events": "event"}


def deletion_panels(prefix):
    """The question asked before a deletion (its text, Delete, Cancel) and the Undo bar shown after one,
    both hidden until needed (built in the Blocks being defined)."""
    with gr.Group(visible=False, elem_id=f"{prefix}-confirm", elem_classes="delete-confirm") as confirm:
        question = gr.Markdown()
        with gr.Row():
            delete = gr.Button("🗑 Delete", variant="stop", elem_id=f"{prefix}-confirm-delete")
            cancel = gr.Button("Cancel", elem_id=f"{prefix}-confirm-cancel")
    with gr.Group(visible=False, elem_id=f"{prefix}-undo", elem_classes="undo-bar") as bar:
        with gr.Row(equal_height=True):
            said = gr.Markdown()
            undo = gr.Button("↶ Undo", elem_id=f"{prefix}-undo-button", scale=0, min_width=120)
    return confirm, question, delete, cancel, bar, said, undo


def _ordinal(number):
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def _question(text, deletable=True):
    """(the question's panel shown, its text, its Delete button)."""
    return gr.update(visible=True), text, gr.update(visible=deletable)


def _closed():
    return gr.update(visible=False), "", gr.update(visible=True)


def _history_bar(history, note=""):
    """(the deletions Undo can bring back, newest last; its bar; its text): the newest is named, with how
    many earlier ones can be undone after it, after ``note`` (what the last Undo did). Hidden when there is
    nothing to undo and nothing to say."""
    history = list(history or [])
    lines = [note] if note else []
    if history:
        earlier = len(history) - 1
        lines.append(history[-1]["said"] + (f"  \n*{earlier} earlier deletion(s) can be undone after it.*"
                                            if earlier else ""))
    return history, gr.update(visible=bool(lines)), "\n\n".join(lines)


def restore_history_ui(history):
    """A panel's Undo history as this browser kept it (a reload shows it again)."""
    return _history_bar([item for item in (history or []) if isinstance(item, dict) and item.get("undo")])


def _pushed(history, undo, said):
    return _history_bar([*(history or []), {"undo": undo, "said": said}])


@ui
def ask_delete_entry_ui(loaded, base, *values):
    """*Delete entry*: the question, naming the entry exactly. Returns (what is to be deleted, the
    question's panel, its text, its Delete button)."""
    loaded = loaded or {}
    kind, name = loaded.get("kind"), loaded.get("entity")
    if not name:
        raise gr.Error("Choose an entry to delete (a new entry is not saved: there is nothing to delete).")
    names = _entity_names(kind)
    if name not in names:
        raise gr.Error(f"`{name}` is no longer in {kind}: choose it again from the list.")
    actions = len(API.get(_path(f"/v1/entities/{kind}", name, "actions"))["actions"])
    references = API.get(_path(f"/v1/entities/{kind}", name, "references"))["references"]
    unsaved = unsaved_parts(base, *values)
    text = (f"### 🗑 Delete the {ENTRY_WORDS.get(kind, 'entry')} **{name}**?\n"
            f"{kind}, {_ordinal(names.index(name) + 1)} of {len(names)}: every field of the entry, its "
            f"{actions} action(s) and its details."
            + (f" Its unsaved changes ({', '.join(DRAFT_PARTS[p] for p in unsaved)}) go with it." if unsaved else "")
            + " **Undo** brings it back, where it was.")
    if references:  # what refers to it would no longer work (the deletion is still allowed)
        shown = "\n".join(f"- {html.escape(problem)}" for problem in references[:8])
        more = f"\n- … and {len(references) - 8} more" if len(references) > 8 else ""
        text += (f"\n\n⚠ **{len(references)} thing(s) refer to it** and would no longer work (fix them, or undo "
                 f"the deletion):\n{shown}{more}")
    return {"type": "entity", "kind": kind, "name": name}, *_question(text)


@ui
def ask_delete_action_ui(loaded, base, *values):
    """*Delete action*: the question, naming the stored action exactly (its number and name)."""
    state = _split_draft(values)["action"][0] or {}
    kind, entry, index = state.get("kind"), state.get("name"), state.get("index")
    actions = API.get(_path(f"/v1/entities/{kind}", entry, "actions"))["actions"] if entry else []
    if index is None or not entry or index >= len(actions):
        raise gr.Error("Pick a saved action first (a new action is not saved: *Discard changes* drops it).")
    action = actions[index]
    edited = "action" in unsaved_parts(base, *values)
    text = (f"### 🗑 Delete the action **{index + 1}. {action.get('name')}** of **{entry}** ({kind})?\n"
            f"It goes with its {len(action.get('outcomes') or [])} outcome(s)"
            + (", and the changes not saved in the form" if edited else "")
            + ". **Undo** brings it back, where it was, in this session.")
    return {"type": "action", "kind": kind, "entry": entry, "index": index, "name": action.get("name")}, \
        *_question(text)


@ui
def ask_delete_mission_ui(state):
    """*Delete mission*: the question, naming the mission exactly (title, key, place)."""
    key = (state or {}).get("key")
    if not key:
        raise gr.Error("Pick a mission first.")
    missions = API.get("/v1/missions")["missions"]
    if key not in missions:
        raise gr.Error(f"The mission `{key}` is no longer there: pick it again from the list.")
    mission = missions[key] or {}
    stages = len(mission.get("stages") or [])
    parts = f"its {stages} step(s)" if stages else "its goal"
    text = (f"### 🗑 Delete the mission **{mission.get('title') or key}** (`{key}`)?\n"
            f"{_ordinal(list(missions).index(key) + 1)} of {len(missions)}: {parts}, rewards and conditions. "
            "**Undo** brings it back, where it was, in this session.")
    return {"type": "mission", "key": key, "title": mission.get("title") or key}, *_question(text)


def cancel_delete_ui():
    """*Cancel*: nothing is deleted. Returns (nothing pending, the question closed)."""
    return None, *_closed()


def _refused(exc, outputs_before):
    """A deletion the API refused (the last place, person or item; an action that changed): nothing
    was deleted; the question stays open with why, and without its Delete button."""
    return (*outputs_before, gr.skip(), gr.update(visible=True), f"⚠ **Not deleted:** {html.escape(str(exc))}",
            gr.update(visible=False), gr.skip(), gr.skip(), gr.skip())


@ui
def confirm_delete_entry_ui(pending, history, loaded, base, *values):
    """The confirmed deletion of exactly the entry asked about. When it is the one being edited, the
    content type's first entry opens. Returns (the entry editor, the message, the parts to capture,
    nothing pending, the question closed, Undo's state, bar and text)."""
    if not pending or pending.get("type") != "entity":
        raise gr.Error("Nothing to delete: press *Delete entry* first.")
    kind, name = pending["kind"], pending["name"]
    try:
        result = API.delete(_path(f"/v1/entities/{kind}", name))
    except APIError as exc:
        return _refused(exc, (*_entry_update(), gr.skip(), []))
    loaded = loaded or {}
    names = result["entities"]
    if (loaded.get("kind"), loaded.get("entity")) == (kind, name):
        entry, capture = _entry_load(kind, names[0] if names else None, names=names), list(DRAFT_PARTS)
    else:  # another entry is being edited: it stays; the list follows
        entry = _entry_update(selection=(gr.skip(), gr.Dropdown(choices=names, value=loaded.get("entity")))) \
            if loaded.get("kind") == kind else _entry_update()
        capture = []
    word = ENTRY_WORDS.get(kind, "entry")
    return (*entry, f"Deleted the {word} **{name}** ({kind}).", capture, None, *_closed(),
            *_pushed(history, result["undo"], f"🗑 Deleted the {word} **{name}** ({kind}, "
                                              f"{_ordinal(result['undo']['position'])} of {result['undo']['of']})."))


@ui
def confirm_delete_action_ui(pending, history, loaded, base, *values):
    """The confirmed deletion of exactly the action asked about (refused if the action at its number
    is not that one any more)."""
    if not pending or pending.get("type") != "action":
        raise gr.Error("Nothing to delete: press *Delete action* first.")
    kind, entry, index, name = pending["kind"], pending["entry"], pending["index"], pending["name"]
    try:
        result = API.delete(_path(f"/v1/entities/{kind}", entry, "actions", index), expected_name=name)
    except APIError as exc:
        return _refused(exc, (*_entry_update(), gr.skip(), []))
    loaded = loaded or {}
    capture = []
    entry_update = _entry_update()
    if (loaded.get("kind"), loaded.get("entity")) == (kind, entry):
        others = set(unsaved_parts(base, *values)) - {"action"}
        capture = ["action", *(p for p in STALE_AFTER["action"] if p not in others)]
        entry_update = _entry_update(**{p: _part_load(p, kind, entry) for p in capture})
    return (*entry_update, result["message"], capture, None, *_closed(),
            *_pushed(history, result["undo"], f"🗑 Deleted the action **{index + 1}. {name}** of **{entry}** ({kind})."))


@ui
def confirm_delete_mission_ui(pending, history=None):
    """The confirmed deletion of exactly the mission asked about. Returns (the mission picker, the
    message, missions.yaml, nothing pending, the question closed, Undo's state, bar and text)."""
    if not pending or pending.get("type") != "mission":
        raise gr.Error("Nothing to delete: press *Delete mission* first.")
    key = pending["key"]
    try:
        result = API.delete(_path("/v1/missions", key))
    except APIError as exc:
        return _refused(exc, (gr.skip(), gr.skip(), gr.skip()))
    return (gr.Dropdown(choices=list(result["missions"]), value=None), result["message"], load_missions_yaml(), None,
            *_closed(), *_pushed(history, result["undo"], f"🗑 Deleted the mission **{pending['title']}** (`{key}`)."))


def _restore(history):
    """Undo the newest deletion of ``history`` (POST /v1/deletions/{token}/restore). Returns (the
    result or None, the history left, why it was not restored): a deletion no longer kept leaves the
    history; one in conflict stays, to be tried again."""
    history = list(history or [])
    if not history:
        raise gr.Error("Nothing to undo.")
    try:
        return API.post(_path("/v1/deletions", history[-1]["undo"]["token"], "restore")), history[:-1], None
    except APIError as exc:
        return None, history[:-1] if exc.status == 404 else history, f"⚠ **Not restored:** {html.escape(str(exc))}"


@ui
def undo_delete_entry_ui(history, loaded, base, *values):
    """**Undo** a deleted entry: it goes back where it was and opens, unless the entry being edited has
    unsaved changes (they are kept: the list shows it). Returns (the entry editor, the message, the parts
    to capture, Undo's state, bar and text)."""
    result, history, why = _restore(history)
    if result is None:
        return (*_entry_update(), gr.skip(), [], *_history_bar(history, why))
    loaded = loaded or {}
    kind, name = result["kind"], result["name"]
    if unsaved_parts(base, *values):
        entry = _entry_update(selection=(gr.skip(), gr.Dropdown(choices=_entity_names(kind), value=loaded.get("entity")))) \
            if loaded.get("kind") == kind else _entry_update()
        message, capture = result["message"] + " Your unsaved changes were kept: open it from the list.", []
    else:
        entry, message, capture = _entry_load(kind, name), result["message"], list(DRAFT_PARTS)
    return (*entry, message, capture, *_history_bar(history))


@ui
def undo_delete_action_ui(history, loaded, base, *values):
    """**Undo** a deleted action: it goes back where it was; when its entry is the one being edited, it
    opens (unless the action form has unsaved changes: then only the list shows it)."""
    result, history, why = _restore(history)
    if result is None:
        return (*_entry_update(), gr.skip(), [], *_history_bar(history, why))
    loaded = loaded or {}
    kind, entry = result["kind"], result["entry"]
    capture, segments = [], {}
    if (loaded.get("kind"), loaded.get("entity")) == (kind, entry):
        dirty = set(unsaved_parts(base, *values))
        capture = [p for p in STALE_AFTER["action"] if p not in dirty]
        segments = {p: _part_load(p, kind, entry) for p in capture}
        if "action" in dirty:
            segments["action"] = (gr.Dropdown(choices=_action_choices(kind, entry)),
                                  *[gr.skip()] * (ENTRY_SEGMENTS["action"] - 1))
        else:
            segments["action"] = _part_load("action", kind, entry, index=result["position"] - 1)
            capture.append("action")
    return (*_entry_update(**segments), result["message"], capture, *_history_bar(history))


@ui
def undo_delete_mission_ui(history):
    """**Undo** a deleted mission: it goes back where it was, and opens in the form. Returns (the picker,
    the mission form and its state, the message, missions.yaml, Undo's state, bar and text)."""
    result, history, why = _restore(history)
    if result is None:
        return (gr.skip(), *[gr.skip()] * 8, gr.skip(), gr.skip(), *_history_bar(history, why))
    key = result["name"]
    return (gr.Dropdown(choices=_mission_keys(), value=key), *mission_form_ui(key), result["message"],
            load_missions_yaml(), *_history_bar(history))


# Before the page is closed or reloaded with unsaved changes, the browser asks (launch(head=...)).
DRAFT_HEAD = """<script>
window.addEventListener("beforeunload", (event) => {
  if (document.querySelector('#draft-status [data-dirty="1"]')) { event.preventDefault(); event.returnValue = ""; }
});
</script>"""


def world_size_ui():
    """How big the world is against the augmentation targets."""
    size = API.get("/v1/world/size")
    rows = [{"what": key, "the world has": size["have"][key], "target": size["want"][key],
             "": "✓" if size["have"][key] >= size["want"][key] else "below"} for key in size["want"]]
    return pd.DataFrame(rows, columns=["what", "the world has", "target", ""])


def _proposal_choices(proposal):
    return ([f"Place: {name}" for name in proposal.get("locations") or {}]
            + [f"Person: {entry.get('name')}" for entry in proposal.get("characters") or []]
            + [f"Item: {entry.get('name')}" for entry in proposal.get("objects") or []])


@ui
def propose_world_ui(progress=gr.Progress()):
    """Gemma proposes new places, people and items (a background job); nothing is written."""
    job = API.post("/v1/jobs/world-proposal")
    progress(0.1, desc="The language model is writing new places, people and items…")
    proposal = API.wait(job)
    choices = _proposal_choices(proposal)
    return (gr.update(choices=choices, value=choices), to_yaml(proposal),
            f"Proposed {len(choices)} new entries. Read and edit them below, untick those you do not want, then "
            "press *Add the ticked ones*.")


@ui
def add_proposal_ui(proposal_text, ticked):
    """Add the ticked entries of the (edited) proposal to the world; the world is checked first."""
    proposal = from_yaml(proposal_text, "The proposal") or {}
    if not isinstance(proposal, dict):
        raise gr.Error("The proposal must be a mapping with locations, characters and objects.")
    ticked = set(ticked or [])
    body = {"locations": {name: entry for name, entry in (proposal.get("locations") or {}).items()
                          if f"Place: {name}" in ticked},
            "characters": [entry for entry in proposal.get("characters") or [] if f"Person: {entry.get('name')}" in ticked],
            "objects": [entry for entry in proposal.get("objects") or [] if f"Item: {entry.get('name')}" in ticked]}
    if not any(body.values()):
        raise gr.Error("Tick at least one entry to add.")
    return API.post("/v1/world/additions", body)["message"], world_size_ui()


# Missions (/v1/missions/{key}): a form, and its steps as a table.

STAGE_COLUMNS = ["Step", "What the player is told", "Goal", "Rewards"]


def _pairs(mapping):
    """A goal (or one effect) as `key: value; key: value`."""
    return "; ".join(f"{key}: {_flow(value) if isinstance(value, (dict, list)) else value}"
                     for key, value in (mapping or {}).items())


def _parse_pairs(text, what):
    """`key: value; key: value` back into a mapping (values read as YAML)."""
    found = {}
    for part in [part.strip() for part in str(text or "").split(";") if part.strip()]:
        if ":" not in part:
            raise gr.Error(f"{what}: write `key: value` (e.g. `item: Heavy Shield`), not `{part}`.")
        key, value = part.split(":", 1)
        try:
            found[key.strip()] = yaml.safe_load(value.strip()) if value.strip() else None
        except yaml.YAMLError as exc:
            raise gr.Error(f"{what}: could not read `{value.strip()}` ({exc}).")
    return found


def _rewards_text(rewards):
    return "; ".join(_pairs(effect) for effect in rewards or [] if isinstance(effect, dict))


def _parse_rewards(text):
    return [{key: value} for key, value in _parse_pairs(text, "Rewards").items()]


def _mission_keys():
    return list(API.get("/v1/missions")["missions"])


@ui
def mission_form_ui(key):
    mission = API.get(_path("/v1/missions", key))["mission"] if key else {}
    stages = mission.get("stages") or ([{"title": mission.get("title", ""), "description": "",
                                         "goal": mission.get("goal") or {}, "rewards": []}] if mission else [])
    table = pd.DataFrame([[stage.get("title", ""), stage.get("description", ""), _pairs(stage.get("goal")),
                           _rewards_text(stage.get("rewards"))] for stage in stages], columns=STAGE_COLUMNS)
    return (key or "", mission.get("title", ""), mission.get("description", ""), float(mission.get("weight") or 1.0),
            _flow(mission.get("enabled_if")), _rewards_text(mission.get("rewards")), table,
            {"key": key, "staged": bool(mission.get("stages")), "mission": mission})


def new_mission_ui():
    table = pd.DataFrame([["First step", "", "location: Market", ""]], columns=STAGE_COLUMNS)
    return (gr.Dropdown(value=None), "new_mission", "A new quest", "", 1.0, "", "", table,
            {"key": None, "staged": True, "mission": {}})


@ui
def save_mission_form_ui(state, key, title, description, weight, enabled_if, rewards, table):
    state = dict(state or {})
    mission = dict(state.get("mission") or {})
    mission.update(title=str(title or "").strip(), description=str(description or "").strip())
    _set(mission, "weight", None if float(weight or 1.0) == 1.0 else float(weight))
    _set(mission, "enabled_if", _parse_flow(enabled_if, "Offered only if"))
    _set(mission, "rewards", _parse_rewards(rewards))
    rows = [row for _, row in (table if table is not None else pd.DataFrame()).iterrows()
            if str(row.iloc[2] or "").strip()]
    if not rows:
        raise gr.Error("A mission needs a goal: at least one step with a Goal.")
    stages = [{"title": str(row.iloc[0] or "").strip() or f"Step {number}", "description": str(row.iloc[1] or "").strip(),
               "goal": _parse_pairs(row.iloc[2], f"Step {number}: Goal"),
               **({"rewards": _parse_rewards(row.iloc[3])} if str(row.iloc[3] or "").strip() else {})}
              for number, (row) in enumerate(rows, start=1)]
    if len(stages) > 1 or state.get("staged"):
        mission["stages"] = stages
        mission.pop("goal", None)
    else:
        mission["goal"] = stages[0]["goal"]
        mission.pop("stages", None)
    old, new = state.get("key"), str(key or "").strip()
    target = old or new
    result = API.put(_path("/v1/missions", target), {"mission": mission, "rename": new if old and new != old else None})
    return (gr.Dropdown(choices=list(result["missions"]), value=new), result["message"], load_missions_yaml())


# The map (/v1/map): a place's borders, and the whole map drawn.

def _map_svg(routes, places, selected=None):
    """The map drawn: every place on a ring, a line for each route; the selected place and its
    borders stand out."""
    import math

    places = list(places)
    if not places:
        return "<p>No places yet.</p>"
    size, radius = 640, 230
    spots = {place: (size / 2 + radius * math.cos(2 * math.pi * i / len(places) - math.pi / 2),
                     size / 2 + radius * math.sin(2 * math.pi * i / len(places) - math.pi / 2))
             for i, place in enumerate(places)}
    pairs = {tuple(sorted((a, b))) for a, borders in (routes or {}).items() for b in borders or [] if a in spots and b in spots}
    near = set((routes or {}).get(selected) or []) | {a for a, borders in (routes or {}).items() if selected in (borders or [])}
    lines = []
    for a, b in sorted(pairs):
        lit = selected in (a, b)
        (x1, y1), (x2, y2) = spots[a], spots[b]
        lines.append(f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" stroke="{"#8b5cf6" if lit else "#94a3b8"}" '
                     f'stroke-width="{3 if lit else 1.5}" opacity="{1 if lit or not selected else .35}"/>')
    for place, (x, y) in spots.items():
        fill = "#8b5cf6" if place == selected else "#c4b5fd" if place in near else "#e2e8f0"
        anchor = "start" if x > size / 2 + 5 else "end" if x < size / 2 - 5 else "middle"
        dx = 12 if anchor == "start" else -12 if anchor == "end" else 0
        dy = 18 if y > size / 2 + radius * .9 else -12 if y < size / 2 - radius * .9 else 4
        weight = ' font-weight="bold"' if place == selected else ""
        label = place.replace("&", "&amp;").replace("<", "&lt;")
        lines.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="9" fill="{fill}" stroke="#475569" stroke-width="1"/>'
                     f'<text x="{x + dx:.0f}" y="{y + dy:.0f}" text-anchor="{anchor}" font-size="13" '
                     f'fill="currentColor"{weight}>{label}</text>')
    if not routes:
        lines.append(f'<text x="{size / 2}" y="{size / 2}" text-anchor="middle" font-size="14" fill="currentColor">'
                     'No authored map: every story draws its own.</text>')
    return (f'<svg viewBox="-110 0 {size + 220} {size}" style="width:100%;max-width:820px;height:auto" '
            f'xmlns="http://www.w3.org/2000/svg">{"".join(lines)}</svg>')


@ui
def map_form_ui(place):
    found = API.get("/v1/map")
    routes, places = found.get("routes") or {}, found.get("places") or []
    place = place if place in places else (places[0] if places else None)
    borders = sorted(set(routes.get(place) or []) | {a for a, b in routes.items() if place in (b or [])})
    return (_map_svg(routes, places, place), gr.Dropdown(choices=places, value=place),
            gr.CheckboxGroup(choices=[p for p in places if p != place], value=borders,
                             label=f"The places {place} borders" if place else "Borders"))


def map_place_ui(place):
    """Another place picked: the map, with its borders ticked."""
    view, _, borders = map_form_ui(place)
    return view, borders


@ui
def save_borders_ui(place, borders):
    if not place:
        raise gr.Error("Pick a place.")
    result = API.put(_path("/v1/map", place), {"borders": list(borders or [])})
    return (_map_svg(result.get("routes") or {}, result.get("places") or [], place), result["message"],
            load_document("map"))


# Behaviour by tags as tables (/v1/behaviours/rules/{npcs|locations}).

BEHAVIOUR_TABLES = {"npcs": ("starts_conversation", "appearances"), "locations": ("draws_in", "visits")}
BEHAVIOUR_COLUMNS = {"npcs": ["Tags (comma separated)", "Comes up to the player", "How often seen"],
                     "locations": ["Tags (comma separated)", "Draws the player in", "How many visits"]}


def _rules_table(group, value):
    first, second = BEHAVIOUR_TABLES[group]
    rows = [[", ".join(rule.get("tags") or []), str(rule.get(first, "")), str(rule.get(second, ""))]
            for rule in value.get("rules") or []]
    return pd.DataFrame(rows, columns=BEHAVIOUR_COLUMNS[group])


def _cell(value):
    value = str(value if value is not None else "").strip()
    return int(value) if value.isdigit() else value


@ui
def behaviour_tables_ui():
    found = API.get("/v1/behaviours/rules")
    npcs, places = found["npcs"], found["locations"]
    return (_rules_table("npcs", npcs), str(npcs["default"].get("starts_conversation", "never")),
            str(npcs["default"].get("appearances", "always")),
            _rules_table("locations", places), str(places["default"].get("draws_in", "never")),
            str(places["default"].get("visits", "always")))


@ui
def save_behaviour_table_ui(group, table, first_default, second_default):
    first, second = BEHAVIOUR_TABLES[group]
    rules = []
    for _, row in (table if table is not None else pd.DataFrame()).iterrows():
        tags = [tag.strip() for tag in str(row.iloc[0] or "").split(",") if tag.strip()]
        rule = {"tags": tags}
        for key, cell in ((first, row.iloc[1]), (second, row.iloc[2])):
            if str(cell if cell is not None else "").strip():
                rule[key] = _cell(cell)
        if tags or len(rule) > 1:
            rules.append(rule)
    default = {first: _cell(first_default) or "never", second: _cell(second_default) or "always"}
    result = API.put(_path("/v1/behaviours/rules", group), {"default": default, "rules": rules})
    return result["message"]


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
        # The workflow banner is the navigation: a button per destination, the current one highlighted.
        with gr.Row(elem_id="ane-banner", elem_classes="nav-banner", equal_height=True):
            nav_buttons = [gr.Button(label, variant="primary" if key == "world" else "secondary", size="sm", scale=0,
                                     min_width=0, elem_id=f"nav-{key}",
                                     elem_classes=["nav-step" if key in WORKFLOW_IDS else "nav-advanced"]
                                     + (["nav-last"] if key == WORKFLOW_IDS[-1] else []))
                           for key, label in DESTINATIONS]
        # On a narrow screen the banner gives way to this menu (every destination, by name).
        nav_menu = gr.Radio([(label, key) for key, label in DESTINATIONS], value="world",
                            label="☰ Menu: go to a section", elem_id="ane-menu", elem_classes="nav-menu")
        dest_now = gr.State("world")  # the destination open (leaving 🌍 World with unsaved changes asks first)
        # 🧠 Train's completed engine, kept in this browser so that a reload shows it again (restore_result_ui).
        # Defined here, outside the destinations: the browser restores it before the page's load events.
        saved_result = gr.BrowserState(None, storage_key="ane-train-result")
        # Each delete panel's Undo history, kept in this browser too (the API keeps the deletions on disk).
        saved_histories = {name: gr.BrowserState([], storage_key=f"ane-undo-{name}")
                           for name in ("entry", "action", "mission")}
        studio_status = gr.Markdown(elem_classes="status")
        demo.load(studio_status_ui, None, studio_status)

        # 📦 Export's choice of a run's generation, which a training in 🧠 Train fills: created here, and
        # rendered in Export.
        export_generation = gr.Dropdown([], label="Generation (for a generation's champion)", visible=False,
                                        render=False)

        with gr.Tabs(elem_id="ane-nav", elem_classes="main-tabs") as main_tabs:
            nav_outputs = [main_tabs, *nav_buttons, nav_menu, dest_now]  # what navigate() returns
            with gr.Tab("🌍 World", id="world", elem_id="dest-world") as world_tab:
                with gr.Tabs(selected="entries") as world_sections:
                    with gr.Tab(WORLD_SECTIONS["entries"], id="entries"):
                        with gr.Row():
                            kind = gr.Radio(REFERENCE["entity_kinds"], value="Locations", label="Content type")
                            entity = gr.Dropdown(label="Entry", choices=names, value=selected_name)
                            add_entry = gr.Button("New entry")
                        # The draft: compared with what was last loaded or saved (draft_base, part by part).
                        draft_status = gr.HTML(draft_status_markup([], None), elem_id="draft-status")
                        draft_base = gr.State({})
                        draft_loaded = gr.State({"kind": "Locations", "entity": selected_name})  # what the forms show
                        draft_pending = gr.State(None)  # where the user was going when asked
                        draft_capture = gr.State([])    # the parts just loaded or saved, to become the baseline
                        with gr.Group(visible=False, elem_id="draft-prompt", elem_classes="draft-prompt") as draft_prompt:
                            draft_message = gr.Markdown()
                            with gr.Row():
                                draft_save = gr.Button("💾 Save", variant="primary", elem_id="draft-save")
                                draft_discard = gr.Button("Discard", variant="stop", elem_id="draft-discard")
                                draft_stay = gr.Button("Stay", elem_id="draft-stay")
                        places_now, people_now, items_now = _names_of("Locations"), _names_of("NPCs"), _names_of("Items")
                        events_now, stats_now = _names_of("Events"), _stat_names()
                        with gr.Tabs():
                            with gr.Tab("Fields"):
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
                                (entry_confirm, entry_question, entry_delete, entry_cancel, entry_undo_bar,
                                 entry_undo_said, entry_undo) = deletion_panels("entry")
                                entry_pending, entry_undo_state = gr.State(None), gr.State(None)

                            with gr.Tab("Actions & outcomes"):
                                gr.Markdown("Pick one of the entry's actions to edit it, or add one. An action's **outcomes** are "
                                            "what can happen when it is taken; pick one to edit it. **Save action** checks the "
                                            "whole entry (fields, events, places, people, items, variables) before writing.")
                                action_state = gr.State({})
                                with gr.Row(equal_height=True):
                                    act_pick = gr.Dropdown(label="Action", choices=_action_choices("Locations", selected_name),
                                                           value=None, scale=4)
                                    act_new = gr.Button("➕ New action", scale=1)
                                    act_delete = gr.Button("🗑 Delete action", variant="stop", scale=1)
                                (action_confirm, action_question, action_delete, action_cancel, action_undo_bar,
                                 action_undo_said, action_undo) = deletion_panels("action")
                                action_pending, action_undo_state = gr.State(None), gr.State(None)
                                with gr.Group():
                                    with gr.Row():
                                        act_name = gr.Textbox(label="Name", scale=3)
                                        act_repeat = gr.Radio([REFERENCE["repeat_default"], *REFERENCE["repeat_rules"]],
                                                              value=REFERENCE["repeat_default"], label="Offered", scale=3,
                                                              info="once per game · again on each visit · every turn")
                                        act_retry = gr.Checkbox(label="Retry after failure", scale=1,
                                                                info="A failure uses nothing up.")
                                    with gr.Row():
                                        act_stat = gr.Dropdown([NONE, *stats_now], value=NONE, label="Skill check")
                                        act_modifier = gr.Number(value=0, precision=0, label="Check modifier",
                                                                 info="+ easier, - harder")
                                        act_group = gr.Textbox(label="Exclusive group", info="Actions sharing it are one choice.")
                                        act_event = gr.Dropdown([NONE, *events_now], value=NONE, label="Starts the event")
                                    with gr.Row():
                                        act_coins = gr.Number(value=0, precision=0, minimum=0, label="Costs coins")
                                        act_item = gr.Dropdown([NONE, *items_now], value=NONE, label="Needs the item")
                                        act_tag = gr.Textbox(label="Needs an item tagged")
                                        act_consumes = gr.Checkbox(label="Uses the item up")
                                        act_collects = gr.Dropdown([NONE, *items_now], value=NONE, label="Takes the item")
                                    with gr.Row():
                                        act_enabled = gr.Textbox(label="Offered only if", placeholder="e.g. reputation: {gte: 1}")
                                        act_until = gr.Textbox(label="No longer offered once",
                                                               placeholder="e.g. action_done: Crack the main safe")
                                gr.Markdown("**Outcomes**")
                                with gr.Row(equal_height=True):
                                    out_pick = gr.Dropdown(label="Outcome", choices=[], value=None, scale=4)
                                    out_new = gr.Button("➕ New outcome", scale=1)
                                    out_delete = gr.Button("🗑 Delete outcome", variant="stop", scale=1)
                                with gr.Group():
                                    out_text = gr.Textbox(label="What is told", lines=2)
                                    with gr.Row():
                                        out_when = gr.Radio(["any", "pass", "fail"], value="any", label="When (with a skill check)")
                                        out_tags = gr.Dropdown(REFERENCE["common_tags"], value=[], multiselect=True,
                                                               allow_custom_value=True, label="Tags")
                                    with gr.Row():
                                        out_health = gr.Number(value=0, precision=0, label="Health ±")
                                        out_coins = gr.Number(value=0, precision=0, label="Coins ±")
                                        out_move = gr.Dropdown([NONE, *places_now], value=NONE, label="Moves to")
                                        out_send = gr.Dropdown([NONE, *places_now], value=NONE, label="Sends to (one way)")
                                    with gr.Row():
                                        out_place = gr.Dropdown([NONE, *places_now], value=NONE, label="Reveals a place")
                                        out_person = gr.Dropdown([NONE, *people_now], value=NONE, label="Reveals a person")
                                        out_item = gr.Dropdown([NONE, *items_now], value=NONE, label="Reveals an item")
                                        out_remove = gr.Dropdown([NONE, ACTING, *people_now], value=NONE, label="Removes a person")
                                    with gr.Row():
                                        out_spawn = gr.Checkbox(label="Opens a new path")
                                        out_lead = gr.Checkbox(label="Someone shows the way")
                                        out_sets = gr.Textbox(label="Sets variables", placeholder="thief_caught: true")
                                        out_changes = gr.Textbox(label="Changes variables", placeholder="reputation: 1")
                                        out_stats = gr.Textbox(label="Changes stats", placeholder="persuasion: 5")
                                    out_effects = gr.Code(language="yaml", label="Other effects (YAML list, optional)", lines=2)
                                with gr.Row():
                                    act_save = gr.Button("Save action", variant="primary")
                                    act_discard = gr.Button("Discard changes")
                                actions_status = gr.Markdown()
                                with gr.Accordion("All of this entry's actions as YAML (advanced)", open=False):
                                    gr.Markdown(REFERENCE["action_reference"])
                                    actions_editor = gr.Code(
                                        value=load_entity_actions_yaml("Locations", selected_name), language="yaml", label="actions",
                                    )
                                    with gr.Row():
                                        save_actions_button = gr.Button("Validate & save actions")
                                        reload_actions_button = gr.Button("Reload from disk")

                            with gr.Tab("Scheduling"):
                                policy_note = gr.Markdown()
                                with gr.Group(visible=False) as event_policy:
                                    with gr.Row():
                                        pol_trigger = gr.Radio(["action", "random"], value="action", label="Starts",
                                                               info="action: something in the story starts it · random: also by itself")
                                        pol_chance = gr.Slider(0, 1, value=0, step=0.01, label="Chance each turn (random)")
                                        pol_weight = gr.Number(value=1.0, minimum=0, label="Weight among random events")
                                    with gr.Row():
                                        pol_cooldown = gr.Number(value=0, precision=0, minimum=0, label="Cooldown (turns)")
                                        pol_most = gr.Number(value=0, precision=0, minimum=0, label="At most (times; 0: no limit)")
                                        pol_repeat = gr.Radio(["once", "per_location", "per_visit", "always"], value="always",
                                                              label="How often")
                                with gr.Group(visible=False) as item_policy:
                                    with gr.Row():
                                        pol_unique = gr.Checkbox(label="Unique: one in each story")
                                        pol_location = gr.Dropdown([NONE, *places_now], value=NONE,
                                                                   label="Lies at (none: where it fits best)")
                                with gr.Row():
                                    save_policy_form = gr.Button("Save", variant="primary")
                                policy_status = gr.Markdown()
                                with gr.Accordion("As YAML (advanced)", open=False):
                                    policy_editor = gr.Code(value=load_entity_policy_yaml("Locations", selected_name),
                                                            language="yaml", label="rules")
                                    with gr.Row():
                                        save_policy_button = gr.Button("Validate & save rules")
                                        reload_policy_button = gr.Button("Reload from disk")

                            with gr.Tab("More details"):
                                details_note = gr.Markdown()
                                with gr.Group(visible=False) as npc_details:
                                    with gr.Row():
                                        det_places = gr.Dropdown(places_now, value=[], multiselect=True, label="Knows these places",
                                                                 info="Where they can lead the player; unique ones live in one of them.")
                                        det_items = gr.Dropdown(items_now, value=[], multiselect=True, label="Knows these items")
                                        det_article = gr.Dropdown(ARTICLES, value="", label="The story calls them")
                                    det_dialogue = gr.Dataframe(headers=["Said when (condition, empty: always)", "Says"],
                                                                datatype=["str", "str"], column_count=(2, "fixed"), interactive=True,
                                                                wrap=True, label="What they say when talked with (the first line "
                                                                "whose condition holds)")
                                with gr.Group(visible=False) as place_details:
                                    with gr.Row():
                                        det_distant = gr.Textbox(label="Seen from afar", lines=3, info="One per line.")
                                        det_entered = gr.Textbox(label="On arrival", lines=3, info="One per line.")
                                        det_prompts = gr.Textbox(label="Ways to go there", lines=3, info="Travel option names.")
                                    det_locked = gr.Checkbox(label="Its exits are locked until an outcome opens them")
                                    det_points = gr.Dataframe(
                                        headers=POINT_COLUMNS, datatype=["str"] * len(POINT_COLUMNS),
                                        column_count=(len(POINT_COLUMNS), "fixed"), interactive=True, wrap=True,
                                        label="Points of interest: what the player can look at here, and the place, person, item, "
                                              "action or outcome each leads to (one row each)")
                                save_details = gr.Button("Save the details", variant="primary")
                                details_status = gr.Markdown()

                            with gr.Tab("✨ Suggest"):
                                gr.Markdown("Ask the language model (Gemma, as the judge) for text in the world's voice for this entry. "
                                            "Tick what you want and add it to the form; nothing is saved until you save the entry "
                                            "(*Fields*) or its details (*More details*).")
                                with gr.Row(equal_height=True):
                                    suggest_field = gr.Dropdown([(label, key) for key, label in SUGGESTION_TARGETS.items()],
                                                                value="descriptions", label="Suggest", scale=3)
                                    suggest_button = gr.Button("✨ Suggest", variant="primary", scale=1)
                                suggestions = gr.CheckboxGroup([], label="Suggestions")
                                add_suggestions = gr.Button("Add the ticked ones to the form")
                                suggest_status = gr.Markdown()

                            with gr.Tab("Every field (YAML)"):
                                gr.Markdown(
                                    "The whole entry as stored: what the forms show and everything else. The whole world is "
                                    "checked with the entry in place before it is saved; rename an entry with the form above."
                                )
                                entity_yaml_editor = gr.Code(value=load_entity_yaml_ui("Locations", selected_name), language="yaml",
                                                             label="entry")
                                with gr.Row():
                                    save_entity_yaml_button = gr.Button("Validate & save the entry", variant="primary")
                                    reload_entity_yaml_button = gr.Button("Reload from disk")
                                entity_yaml_status = gr.Markdown()

                    with gr.Tab(WORLD_SECTIONS["missions"], id="missions") as section_missions:
                        gr.Markdown("Each story draws one mission (by weight) among those it may have. A mission is a "
                                    "quest in **steps**, done in order; the last completes it. A step's **Goal** is one "
                                    "or more of `location: Palace`, `item: Heavy Shield`, `action: Crack the main safe`, "
                                    "`npc_removed: Thief`, `event: confront_thief`, `outcome_tag: brawl_won`, "
                                    "`coins: 20`, `condition: {any: [...]}`, separated by `;` (all must hold). "
                                    "**Rewards**: effects such as `change_coins: 10; change_variable: {reputation: 1}`.")
                        mission_state = gr.State({})
                        with gr.Row(equal_height=True):
                            mis_pick = gr.Dropdown(label="Mission", choices=_mission_keys(), value=None, scale=4)
                            mis_new = gr.Button("➕ New mission", scale=1)
                            mis_delete = gr.Button("🗑 Delete mission", variant="stop", scale=1)
                        (mission_confirm, mission_question, mission_delete, mission_cancel, mission_undo_bar,
                         mission_undo_said, mission_undo) = deletion_panels("mission")
                        mission_pending, mission_undo_state = gr.State(None), gr.State(None)
                        with gr.Group():
                            with gr.Row():
                                mis_key = gr.Textbox(label="Key", info="letters, digits and _")
                                mis_title = gr.Textbox(label="Title", scale=2)
                                mis_weight = gr.Number(value=1.0, minimum=0, label="Weight")
                            mis_description = gr.Textbox(label="Description", lines=2)
                            with gr.Row():
                                mis_enabled = gr.Textbox(label="Offered only if", placeholder="e.g. thief_caught: false")
                                mis_rewards = gr.Textbox(label="Rewards when complete",
                                                         placeholder="change_coins: 10; change_variable: {reputation: 1}")
                            mis_stages = gr.Dataframe(headers=STAGE_COLUMNS, datatype=["str", "str", "str", "str"],
                                                      column_count=(4, "fixed"), interactive=True, wrap=True,
                                                      label="Steps (one row each; one row: a mission with one goal)")
                        save_mission_form = gr.Button("Save mission", variant="primary")
                        missions_status = gr.Markdown()
                        with gr.Accordion("Every mission as YAML (advanced)", open=False):
                            missions_editor = gr.Code(value=load_missions_yaml(), language="yaml", label="missions.yaml")
                            with gr.Row():
                                save_missions_button = gr.Button("Validate & save missions")
                                reload_missions_button = gr.Button("Reload from disk")
                        save_missions_button.click(save_missions_yaml, missions_editor, [missions_editor, missions_status])
                        reload_missions_button.click(load_missions_yaml, None, missions_editor)

                    with gr.Tab(WORLD_SECTIONS["story-rules"], id="story-rules") as section_story_rules:
                        with gr.Accordion("Story rules", open=True):
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
                            story_rules_status = gr.Markdown()
                            save_rules_button.click(save_rules_ui, rule_inputs, story_rules_status)
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
                    with gr.Tab(WORLD_SECTIONS["map"], id="map") as section_map:
                        gr.Markdown("Pick a place and tick the places it borders: a route holds both ways, and a far "
                                    "place is reached through the places between. The map below shows every route.")
                        with gr.Row():
                            with gr.Column(scale=2):
                                map_place = gr.Dropdown(places_now, value=places_now[0] if places_now else None,
                                                        label="Place")
                                map_borders = gr.CheckboxGroup([], label="Borders")
                                save_borders = gr.Button("Save the borders", variant="primary")
                                map_status = gr.Markdown()
                            with gr.Column(scale=3):
                                map_view = gr.HTML()
                        with gr.Accordion("The map as YAML (advanced)", open=False):
                            map_editor = gr.Code(value=load_document("map"), language="yaml", label="map")
                            with gr.Row():
                                save_map_button = gr.Button("Validate & save the map")
                                reload_map_button = gr.Button("Reload from disk")
                            save_map_button.click(lambda text: save_document("map", text), map_editor,
                                                  [map_editor, map_status])
                            reload_map_button.click(lambda: load_document("map"), None, map_editor)

                    with gr.Tab(WORLD_SECTIONS["behaviour"], id="behaviour") as section_behaviour:
                        gr.Markdown(
                            "What people and places do comes from their **tags**: each row gives everyone (or "
                            "everywhere) with one of its tags a behaviour, and the choices above set those tags. Leave a "
                            "cell empty to leave that behaviour to other rules. **People** come up to the player "
                            "`never`, `first_meeting` or `every_visit`, and are seen `always`, `once` or in a number of "
                            "visits. **Places** draw the player in `never`, `first_visit` or `every_visit`, and may be "
                            "visited `always`, `once` or a number of times. The first matching row decides; someone or "
                            "somewhere the quest needs now is always there."
                        )
                        with gr.Row():
                            with gr.Column():
                                gr.Markdown("**People**")
                                with gr.Row():
                                    npc_default_a = gr.Dropdown(["never", "first_meeting", "every_visit"], value="never",
                                                                label="Without a rule: comes up")
                                    npc_default_b = gr.Dropdown(["always", "once", "2", "3", "5"], value="always",
                                                                allow_custom_value=True, label="Without a rule: seen")
                                npc_table = gr.Dataframe(headers=BEHAVIOUR_COLUMNS["npcs"], datatype=["str", "str", "str"],
                                                         column_count=(3, "fixed"), interactive=True, wrap=True)
                                save_npc_rules = gr.Button("Save the people's rules", variant="primary")
                            with gr.Column():
                                gr.Markdown("**Places**")
                                with gr.Row():
                                    place_default_a = gr.Dropdown(["never", "first_visit", "every_visit"], value="never",
                                                                  label="Without a rule: draws in")
                                    place_default_b = gr.Dropdown(["always", "once", "2", "3", "5"], value="always",
                                                                  allow_custom_value=True, label="Without a rule: visits")
                                place_table = gr.Dataframe(headers=BEHAVIOUR_COLUMNS["locations"],
                                                           datatype=["str", "str", "str"], column_count=(3, "fixed"),
                                                           interactive=True, wrap=True)
                                save_place_rules = gr.Button("Save the places' rules", variant="primary")
                        rules_status = gr.Markdown()

                    with gr.Tab(WORLD_SECTIONS["grow"], id="grow") as section_grow:
                        gr.Markdown("The language model proposes new **places, people and items** that fit this world (its places "
                                    "and tags). Nothing is written until you add them: each must be new, and the whole world is "
                                    "checked with them in place. The targets are `augmentation.min_*` (⚙ Advanced → Engine logic).")
                        with gr.Row():
                            world_size = gr.Dataframe(label="The world's size", interactive=False, scale=2)
                            with gr.Column(scale=1):
                                grow_llm_status = gr.Markdown()
                                propose_button = gr.Button("✨ Propose new entries", variant="primary")
                        proposal_pick = gr.CheckboxGroup([], label="Add these")
                        proposal_text = gr.Code(language="yaml", label="The proposal (edit it before adding)", lines=12)
                        add_proposal = gr.Button("Add the ticked ones to the world", variant="primary")
                        grow_status = gr.Markdown()

                    with gr.Tab(WORLD_SECTIONS["checks"], id="checks") as section_checks:
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
                action_fields = [act_name, act_repeat, act_retry, act_stat, act_modifier, act_group, act_event,
                                 act_coins, act_item, act_tag, act_consumes, act_collects, act_enabled, act_until]
                outcome_fields = [out_text, out_when, out_tags, out_health, out_coins, out_move, out_send, out_place,
                                  out_person, out_item, out_remove, out_spawn, out_lead, out_sets, out_changes,
                                  out_stats, out_effects]
                action_form = [action_state, *action_fields, out_pick, *outcome_fields]
                policy_fields = [pol_trigger, pol_chance, pol_weight, pol_cooldown, pol_most, pol_repeat, pol_unique,
                                 pol_location]
                detail_fields = [det_places, det_items, det_article, det_dialogue, det_distant, det_entered, det_prompts,
                                 det_locked, det_points]
                mission_fields = [mis_key, mis_title, mis_description, mis_weight, mis_enabled, mis_rewards, mis_stages]

                # ---- 📝 Entries: one draft, its loads and its saves. No load follows a dropdown's change: the
                # user's choices are guarded (with unsaved changes: undone, then Save, Discard or Stay), and a
                # load or a save is followed by the capture of what the browser then holds as the baseline.
                draft_inputs = [*entry_fields, action_state, *action_fields, *outcome_fields, *policy_fields,
                                *detail_fields, entity_yaml_editor, actions_editor, policy_editor]
                entry_outputs = [kind, entity, *entry_fields, entity_yaml_editor, actions_editor, policy_editor,
                                 act_pick, *action_form, event_policy, item_policy, policy_note, *policy_fields,
                                 npc_details, place_details, details_note, *detail_fields, draft_loaded]
                if len(draft_inputs) != sum(DRAFT_SIZES.values()) or len(entry_outputs) != ENTRY_OUTPUTS:
                    raise RuntimeError("The entry editor's components do not match DRAFT_SIZES / ENTRY_SEGMENTS.")
                draft_outputs = [*entry_outputs, *nav_outputs, world_sections, draft_pending, draft_prompt,
                                 draft_message, draft_capture]
                guard_inputs = [draft_loaded, draft_base, *draft_inputs]
                capture = {"fn": capture_draft_ui, "inputs": [draft_capture, draft_base, draft_loaded, *draft_inputs],
                           "outputs": [draft_base, draft_status, draft_capture], "show_progress": "hidden"}
                status = {"fn": draft_status_ui, "inputs": [draft_base, draft_loaded, *draft_inputs],
                          "outputs": draft_status, "show_progress": "hidden"}
                demo.load(load_selection_ui, [kind, entity], [*entry_outputs, draft_capture]).success(**capture)
                # The user's choices: carried out at once, or, with unsaved changes, undone and asked about.
                kind.input(entry_choice_ui, [kind, entity, *guard_inputs], draft_outputs).success(**capture)
                entity.input(entry_choice_ui, [kind, entity, *guard_inputs], draft_outputs).success(**capture)
                add_entry.click(new_entry_choice_ui, guard_inputs, draft_outputs).success(**capture)
                act_pick.input(action_choice_ui, [act_pick, *guard_inputs], draft_outputs).success(**capture)
                act_new.click(new_action_choice_ui, guard_inputs, draft_outputs).success(**capture)
                for button, choice in ((draft_save, "save"), (draft_discard, "discard"), (draft_stay, "stay")):
                    button.click(lambda pending, *rest, choice=choice: resolve_draft_ui(choice, pending, *rest),
                                 [draft_pending, *guard_inputs], draft_outputs).success(**capture)
                # Each form's own save: that part (a failure changes nothing), then reloaded as stored.
                for button, part, said in ((save_entry, "fields", editor_status), (act_save, "action", actions_status),
                                           (save_policy_form, "scheduling", policy_status),
                                           (save_details, "details", details_status),
                                           (save_entity_yaml_button, "entry_yaml", entity_yaml_status),
                                           (save_actions_button, "actions_yaml", actions_status),
                                           (save_policy_button, "policy_yaml", policy_status)):
                    button.click(lambda *rest, part=part: save_part_ui(part, *rest), guard_inputs,
                                 [*entry_outputs, said, draft_capture]).success(**capture)
                for button, part in ((act_discard, "action"), (reload_entity_yaml_button, "entry_yaml"),
                                     (reload_actions_button, "actions_yaml"), (reload_policy_button, "policy_yaml")):
                    button.click(lambda *rest, part=part: reload_part_ui(part, *rest), guard_inputs,
                                 [*entry_outputs, draft_capture]).success(**capture)
                # Deleting: a question naming exactly what goes, then Undo (this session) once it is gone.
                history_events = {"entry": [], "action": [], "mission": []}
                for ask, asked, pending, (confirm, question, delete, cancel, bar, said, undo), state, confirmed, \
                        undone, said_where in (
                        (delete_entry, ask_delete_entry_ui, entry_pending,
                         (entry_confirm, entry_question, entry_delete, entry_cancel, entry_undo_bar, entry_undo_said,
                          entry_undo), entry_undo_state, confirm_delete_entry_ui, undo_delete_entry_ui, editor_status),
                        (act_delete, ask_delete_action_ui, action_pending,
                         (action_confirm, action_question, action_delete, action_cancel, action_undo_bar,
                          action_undo_said, action_undo), action_undo_state, confirm_delete_action_ui,
                         undo_delete_action_ui, actions_status)):
                    question_outputs = [pending, confirm, question, delete]
                    ask.click(asked, guard_inputs, question_outputs, show_progress="hidden")
                    cancel.click(cancel_delete_ui, None, question_outputs, show_progress="hidden")
                    deleted = delete.click(confirmed, [pending, state, *guard_inputs],
                                           [*entry_outputs, said_where, draft_capture, *question_outputs, state, bar,
                                            said])
                    undone_event = undo.click(undone, [state, *guard_inputs],
                                              [*entry_outputs, said_where, draft_capture, state, bar, said])
                    for event in (deleted, undone_event):
                        event.success(**capture)
                    history_events["entry" if state is entry_undo_state else "action"] += [deleted, undone_event]
                # An action's outcomes are switched, added and removed inside its draft (saved with Save action).
                out_pick.input(pick_outcome_ui, [action_state, out_pick, *outcome_fields],
                               [action_state, *outcome_fields]).then(**status)
                out_new.click(new_outcome_ui, [action_state, *outcome_fields],
                              [action_state, out_pick, *outcome_fields]).then(**status)
                out_delete.click(delete_outcome_ui, action_state, [action_state, out_pick, *outcome_fields]).then(**status)
                # Every edit: the indicator compares the draft with what was last loaded or saved.
                gr.on([component.input for component in draft_inputs if not isinstance(component, gr.State)],
                      trigger_mode="always_last", **status)
                # Leaving the editor for another section of the World asks first, with unsaved changes.
                for section, tab in (("missions", section_missions), ("story-rules", section_story_rules),
                                     ("map", section_map), ("behaviour", section_behaviour), ("grow", section_grow),
                                     ("checks", section_checks)):
                    tab.select(lambda *rest, section=section: section_choice_ui(section, *rest), guard_inputs,
                               draft_outputs, show_progress="hidden")
                # Missions.
                mis_pick.input(mission_form_ui, mis_pick, [*mission_fields, mission_state])
                mis_new.click(new_mission_ui, None, [mis_pick, *mission_fields, mission_state])
                save_mission_form.click(save_mission_form_ui, [mission_state, *mission_fields],
                                        [mis_pick, missions_status, missions_editor]).then(
                    mission_form_ui, mis_pick, [*mission_fields, mission_state])
                mission_question_outputs = [mission_pending, mission_confirm, mission_question, mission_delete]
                mis_delete.click(ask_delete_mission_ui, mission_state, mission_question_outputs, show_progress="hidden")
                mission_cancel.click(cancel_delete_ui, None, mission_question_outputs, show_progress="hidden")
                history_events["mission"] += [
                    mission_delete.click(confirm_delete_mission_ui, [mission_pending, mission_undo_state],
                                         [mis_pick, missions_status, missions_editor, *mission_question_outputs,
                                          mission_undo_state, mission_undo_bar, mission_undo_said]),
                    mission_undo.click(undo_delete_mission_ui, mission_undo_state,
                                       [mis_pick, *mission_fields, mission_state, missions_status, missions_editor,
                                        mission_undo_state, mission_undo_bar, mission_undo_said])]
                # The map.
                map_place.input(map_place_ui, map_place, [map_view, map_borders])
                demo.load(map_form_ui, map_place, [map_view, map_place, map_borders])
                save_borders.click(save_borders_ui, [map_place, map_borders], [map_view, map_status, map_editor])
                save_map_button.click(map_form_ui, map_place, [map_view, map_place, map_borders])

                # What an NPC or a place does follows its tags: the choices show the tag in effect, and
                # choosing one sets it (an entry loaded, a tag typed: the choices follow).
                behaviours = [behaviour_a, behaviour_b]
                kind.change(behaviour_fields_ui, [kind, tags], behaviours)
                tags.change(behaviour_fields_ui, [kind, tags], behaviours)
                demo.load(behaviour_fields_ui, [kind, tags], behaviours)
                behaviour_a.input(lambda k, c, t: apply_behaviour_ui(k, 0, c, t), [kind, behaviour_a, tags], tags).then(
                    **status)
                behaviour_b.input(lambda k, c, t: apply_behaviour_ui(k, 1, c, t), [kind, behaviour_b, tags], tags).then(
                    **status)
                behaviour_tables = [npc_table, npc_default_a, npc_default_b, place_table, place_default_a,
                                    place_default_b]
                demo.load(behaviour_tables_ui, None, behaviour_tables)
                save_npc_rules.click(lambda t, a, b: save_behaviour_table_ui("npcs", t, a, b),
                                     [npc_table, npc_default_a, npc_default_b], rules_status).then(
                    behaviour_fields_ui, [kind, tags], behaviours).then(behaviour_tables_ui, None, behaviour_tables)
                save_place_rules.click(lambda t, a, b: save_behaviour_table_ui("locations", t, a, b),
                                       [place_table, place_default_a, place_default_b], rules_status).then(
                    behaviour_fields_ui, [kind, tags], behaviours).then(behaviour_tables_ui, None, behaviour_tables)

                # The language model: suggestions for this entry, and new entries for the world.
                suggest_button.click(suggest_ui, [kind, entity, suggest_field], [suggestions, suggest_status],
                                     concurrency_limit=1, concurrency_id="language_model")
                add_suggestions.click(add_suggestions_ui,
                                      [suggest_field, suggestions, descriptions, goals, tags, det_distant, det_entered,
                                       det_prompts, det_dialogue],
                                      [descriptions, goals, tags, det_distant, det_entered, det_prompts, det_dialogue,
                                       suggest_status]).then(**status)
                demo.load(world_size_ui, None, world_size)
                demo.load(judge_status_ui, None, grow_llm_status)
                propose_button.click(propose_world_ui, None, [proposal_pick, proposal_text, grow_status],
                                     concurrency_limit=1, concurrency_id="language_model")
                add_proposal.click(add_proposal_ui, [proposal_text, proposal_pick], [grow_status, world_size]).success(
                    refresh_entry_names_ui, [kind, entity], entity).then(studio_status_ui, None, studio_status)

            with gr.Tab("🧠 Train", id="train", elem_id="dest-train") as train_tab:
                with gr.Tabs(selected="train-run") as train_sections:
                    with gr.Tab("Run", id="train-run"):
                        gr.Markdown("Evolve AI storytellers on this world: a tournament of storytellers, each "
                                    "scored on the stories it tells. The best becomes the **completed engine**. "
                                    "Training only trains (no game is started): play, evaluate or export the "
                                    "engine with the buttons under it.  \n**A run uses the saved engine defaults "
                                    "(⚙ Advanced → Simulation) except what you set here for this run.** The summary "
                                    "below is what it will run with, as the API resolves it.", elem_classes="lead")
                        gr.Markdown("#### 1 · Configure the run")
                        first_plan = API.post("/v1/simulations/resolve", {})
                        saved, first = first_plan["defaults"], first_plan["effective"]
                        steps_look = _steps_look(first_plan["work"])
                        with gr.Row(equal_height=True):
                            with gr.Column(min_width=200):
                                use_population = gr.Checkbox(False, label="Set the population for this run")
                                population = gr.Slider(1, limits["candidate_limit"], value=first["population"],
                                                       step=1, label="Population (storytellers)", interactive=False,
                                                       info=f"Saved default: {saved['candidate_count']} · used "
                                                            "unless set here")
                            with gr.Column(min_width=200):
                                use_generations = gr.Checkbox(False, label="Set the generations for this run")
                                generations = gr.Slider(1, limits["generation_limit"], value=first["generations"],
                                                        step=1, label="Generations", interactive=False,
                                                        info=f"Saved default: {saved['generations']} · used "
                                                             "unless set here")
                            with gr.Column(min_width=200):
                                use_episodes = gr.Checkbox(False, label="Set the stories for this run")
                                episodes = gr.Slider(1, 10, value=first["stories_per_storyteller"], step=1,
                                                     label="Stories per storyteller", interactive=False,
                                                     info=f"Saved default: {saved['episodes']} · used unless set "
                                                          "here")
                            with gr.Column(min_width=200):
                                use_steps = gr.Checkbox(False, label="Set the steps for this run")
                                sim_steps = gr.Slider(1, limits["simulation_step_limit"], value=first["requested_steps"],
                                                      step=1, interactive=False,
                                                      **{**steps_look, "info": f"{steps_look['info']} Saved default: "
                                                                               f"{saved['simulation_steps']}."})
                        with gr.Row(equal_height=True):
                            run_reproducibility = gr.Radio(_reproducibility_choices(saved), value="default",
                                                           label="Reproducibility")
                            with gr.Column(min_width=200):
                                use_seed = gr.Checkbox(False, label="Set the seed for this run")
                                run_seed = gr.Number(saved["seed"], precision=0, label="Seed", interactive=False,
                                                     info=f"Saved default: {saved['seed']}")
                            run_warm = gr.Radio(_warm_start_choices(saved), value="default", label="Warm start",
                                                info="Seed the population with the best storytellers the AI remembers.")
                        # What the run will execute, resolved by the API (a run is refused on the same numbers).
                        run_summary = gr.Markdown(resolved_markdown(first_plan), elem_classes="run-summary")
                        run_plan = gr.State(first_plan)
                        run_controls = [use_population, population, use_generations, generations, use_episodes,
                                        episodes, use_steps, sim_steps, run_reproducibility, use_seed, run_seed, run_warm]
                        run_form = [population, generations, episodes, sim_steps, run_seed, run_reproducibility,
                                    run_warm, run_summary, run_plan]
                        for control in run_controls:  # what the user sets (the form's own look is an output)
                            control.input(run_form_ui, run_controls, run_form, show_progress="hidden")
                        run_button = gr.Button("🧠 Evolve storytellers", variant="primary", elem_id="train-run-button")
                        # Its progress (generation by generation) shows here while it runs, then the engine it made.
                        gr.Markdown("#### 2 · Progress and the completed engine")
                        result_card = gr.Markdown(NO_RESULT, elem_classes="result-card", elem_id="train-result")
                        train_result = gr.State(None)  # the completed engine: its exact id, for the actions below
                        with gr.Row(elem_classes="result-actions"):
                            play_result = gr.Button("▶ Play this result", interactive=False, elem_id="train-play-result")
                            evaluate_result = gr.Button("⚖ Evaluate this result", interactive=False,
                                                        elem_id="train-evaluate-result")
                            export_result = gr.Button("📦 Export this result", interactive=False,
                                                      elem_id="train-export-result")
                        result_buttons = [play_result, evaluate_result, export_result]
                        gr.Markdown("#### 3 · Learning results")
                        with gr.Row():
                            learning_plot = gr.LinePlot(
                                x="generation", y="score", color="series", label="Learning curve",
                                x_title="Generation", y_title="Fitness", height=260,
                            )
                            learned_traits = gr.Dataframe(
                                headers=["trait", "meaning", "evolved", "population", "tendency"],
                                label="What the evolved storyteller learned", interactive=False, wrap=True,
                            )
                        simulation_report = gr.Markdown(elem_classes="status")
                        with gr.Accordion("The run's files: its story configuration and engine model", open=False):
                            with gr.Row():
                                generated_config = gr.File(label="Story configuration", interactive=False)
                                generated_engine = gr.File(
                                    label="Engine model (world, storyteller, training state, memory, tag vectors)",
                                    interactive=False,
                                )
                        with gr.Accordion("Learning memory", open=False):
                            memory_status = gr.Markdown(learning_memory_text())
                            forget_button = gr.Button("Forget what the AI learned", variant="stop")
                            forget_button.click(forget_learning_ui, None, memory_status)
                        run_state = gr.State(None)

                    with gr.Tab("From a recorded engine", id="train-records"):
                        with gr.Row(equal_height=True):
                            record_engine = gr.Dropdown(recorded_engines(), label="Recorded engine", scale=4,
                                                        info="Every run's best, every generation's champion, loaded, "
                                                        "continued and retrained engines. Its files: 📦 Export; its "
                                                        "stories: ⚖ Evaluate.")
                            refresh_records = gr.Button("↻ Refresh", scale=1)
                        with gr.Row(equal_height=True):
                            more_generations = gr.Slider(1, limits["generation_limit"], value=3, step=1,
                                                         label="Further generations", scale=2)
                            continue_record = gr.Button("Continue its training", scale=1)
                            retrain_record = gr.Button("Retrain from it", scale=1)
                        gr.Markdown("*Continue* trains it further exactly where it stopped (its own settings); "
                                    "*Retrain* starts a new evolution seeded with its storyteller, with its saved "
                                    "defaults and what *Run* sets for a run. Either opens *Run* with its completed "
                                    "engine when it finishes.")
                        retrain_summary = gr.Markdown("Choose a recorded engine to see what retraining it would run "
                                                      "with.", elem_classes="run-summary")
                        retrain_plan = gr.State(None)

                train_outputs = [simulation_report, generated_config, generated_engine, learning_plot, learned_traits]
                # Every training ends with its completed engine (the state the follow-up actions carry), shown
                # on the card, whose progress bar follows the generations while it runs.
                result_outputs = [train_result, result_card, *result_buttons]
                engine_events = [run_button.click(
                    run_simulation_ui, [*run_controls, run_plan],
                    [*train_outputs, memory_status, run_state, export_generation, *result_outputs],
                    concurrency_limit=1,
                    concurrency_id="narrative_evolution",
                )]
                record_outputs = [*train_outputs, run_state, export_generation, *result_outputs]
                engine_events.append(continue_record.click(
                    continue_recorded_ui, [record_engine, more_generations],
                    record_outputs, concurrency_limit=1, concurrency_id="narrative_evolution"))
                engine_events.append(retrain_record.click(
                    retrain_recorded_ui, [record_engine, more_generations, *run_controls, retrain_plan],
                    record_outputs, concurrency_limit=1, concurrency_id="narrative_evolution"))
                for event in engine_events[1:]:  # continued or retrained: Run shows its completed engine
                    event.success(lambda: gr.Tabs(selected="train-run"), None, train_sections, show_progress="hidden")
                for event in engine_events:  # remembered in this browser, across reloads
                    event.success(lambda done: done, train_result, saved_result, show_progress="hidden")
                demo.load(restore_result_ui, saved_result, [train_result, result_card, *result_buttons],
                          show_progress="hidden")
                retrain_inputs = [record_engine, more_generations, *run_controls]
                record_engine.change(retrain_form_ui, retrain_inputs, [retrain_summary, retrain_plan],
                                     show_progress="hidden")
                more_generations.input(retrain_form_ui, retrain_inputs, [retrain_summary, retrain_plan],
                                       show_progress="hidden")
                for control in run_controls:  # the Train form's settings apply to a retraining too
                    control.input(retrain_form_ui, retrain_inputs, [retrain_summary, retrain_plan],
                                  show_progress="hidden")

            with gr.Tab("▶ Play", id="play", elem_id="dest-play") as play_tab:
                play_token = session_token  # the game played here (Export's "this game's storyteller" too)
                play_actions = gr.State([])
                play_finished = finished_run_choices()
                play_first_quests, quest_overview_text = quest_overview(profiles["default"])
                allocation = character_stats.get("allocation") or {}
                with gr.Accordion("🎮 New game", open=True) as play_setup:
                    with gr.Row():
                        play_run = gr.Dropdown(play_finished, value=play_finished[0][1] if play_finished else None,
                                               label="Trained AI", scale=3,
                                               info="Finished runs and loaded engines, newest first.")
                        play_who = gr.Dropdown(profiles["profiles"], value=profiles["default"], allow_custom_value=True,
                                               label="Player profile", scale=2,
                                               info="Its progression and persistent variables are kept.")
                        play_quest = gr.Dropdown(play_first_quests, value="", label="Quest", scale=2,
                                                 info="Or let the story draw one.")
                        play_stats = gr.Dropdown(PLAY_STATS, value="story default", label="Character", scale=2,
                                                 info="My own points: set them below.")
                    with gr.Row():
                        play_new = gr.Button("▶ New game", variant="primary", scale=3)
                        play_upload = gr.UploadButton("📂 Continue a save game", file_types=[".json"],
                                                      type="filepath", scale=2)
                        play_refresh = gr.Button("↻ Refresh", scale=1)
                    with gr.Accordion("🎲 My own stat points", open=False):
                        gr.Markdown(
                            f"Spread **{allocation.get('total_points', 50)} points** over "
                            f"{', '.join(f'`{stat}`' for stat in (character_stats.get('stats') or {})) or 'no stats'}; "
                            f"each stat takes {allocation.get('min_points', 5)}-{allocation.get('max_points', 20)} "
                            "points. *Character: My own points* plays with them."
                        )
                        with gr.Row():
                            randomize_stats = gr.Button("Randomize", size="sm")
                            balance_stats = gr.Button("Balance", size="sm")
                        stats_points = gr.Code(value=to_yaml(character_stats.get("defined_points") or {}),
                                               language="yaml", label="Stat points", lines=4)
                        stats_check = gr.Markdown()
                    with gr.Accordion("🗂 Another engine: a recorded one, or a file", open=False):
                        with gr.Row(equal_height=True):
                            play_record_engine = gr.Dropdown(recorded_engines(), label="Recorded engine", scale=4,
                                                             info="Any run's best, a generation's champion, ...")
                            play_record = gr.Button("▶ Play with it", scale=1)
                        gr.Markdown("Or an engine file: an engine model (`.ane.zip`), an older engine ZIP, or a complete "
                                    "configuration YAML (with its model-parameters YAML when it has no "
                                    "`generated_run.genome`). It is kept: play it again, or export it.")
                        with gr.Row(equal_height=True):
                            engine_upload = gr.File(label="Engine model or configuration",
                                                    file_types=[".zip", ".yaml", ".yml"], type="filepath", scale=2)
                            model_parameters_upload = gr.File(label="Model parameters (optional)",
                                                              file_types=[".yaml", ".yml"], type="filepath", scale=2)
                            load_engine_button = gr.Button("▶ Load & play", scale=1)
                    with gr.Accordion("🎯 The world's quests, and this profile's save game", open=False):
                        quest_overview_md = gr.Markdown(quest_overview_text)
                        gr.Markdown("Every session saves the player's progression: its report (goal achieved or "
                                    "mission failed, stats, inventory) and, when achieved, its goal, which then leaves "
                                    "the goal pool (each goal once per save game). The report goes to the UNP "
                                    "(`session_report` in the engine logic; to send it again: ⚙ Advanced → 🔌 "
                                    "Integration). *Reset profile* (🌍 World → 📜 Story rules → Persistent variables) "
                                    "starts a new save game.")
                        with gr.Row():
                            refresh_quests = gr.Button("Refresh the quests", size="sm")
                            save_game_refresh = gr.Button("Show the save game", size="sm")
                        save_game_text = gr.Markdown()
                        save_game_table = gr.Dataframe(label="Sessions", interactive=False, wrap=True)
                play_status = gr.Markdown()
                with gr.Row():
                    with gr.Column(scale=3):
                        play_ending = gr.Markdown(elem_classes="ending", visible=False)
                        play_hud = gr.Markdown(elem_classes="play-hud", visible=False)
                        # Where you are, then what your last choice did, then what you can do now.
                        play_scene = gr.Markdown("Choose a trained AI and press **▶ New game**.",
                                                 elem_classes="scene-card")
                        play_turn = gr.Markdown(elem_classes="turn-card", visible=False)
                        play_buttons = [gr.Button(visible=False, elem_classes="choice", size="md")
                                        for _ in range(CHOICE_BUTTONS)]
                        with gr.Row():
                            play_save = gr.Button("💾 Save game", size="sm")
                            play_stop = gr.Button("End the game", variant="stop", size="sm")
                        play_save_file = gr.File(label="Save game (JSON)", interactive=False, visible=False)
                    with gr.Column(scale=2):
                        play_quest_log = gr.Markdown("### 🎯 Quest\nStart a game to see its quest.",
                                                     elem_classes="quest-card")
                        play_you = gr.Markdown("### 🧍 You\nStart a game to see your character.", elem_classes="you-card")
                        with gr.Accordion("👥 People met & the map", open=False):
                            play_people = gr.Markdown("### People met\nNobody yet.", elem_classes="people-card")
                with gr.Accordion("🧭 Your journey: every choice and what came of it", open=False):
                    play_journey = gr.Markdown("*Your journey starts with your first choice.*", elem_classes="journey")
                with gr.Accordion("📖 The story so far", open=False):
                    play_story = gr.Markdown(elem_classes="story-book")
                with gr.Accordion("⚖ Judge this playthrough (Gemma 4 as a game critic)", open=False):
                    game_judge_status = gr.Markdown()
                    judge_game_button = gr.Button("Judge the game played so far")
                    game_judgement = gr.Markdown()

                play_screen = [play_ending, play_hud, play_scene, play_turn, *play_buttons, play_quest_log, play_you,
                               play_people, play_actions, play_journey]
                new_game_outputs = [play_token, *play_screen, play_story, play_status]
                player_inputs = [play_who, play_quest, play_stats, stats_points]
                started = [
                    play_new.click(play_new_game_ui, [play_run, *player_inputs], new_game_outputs, concurrency_limit=1),
                    play_record.click(play_recorded_ui, [play_record_engine, *player_inputs], new_game_outputs,
                                      concurrency_limit=1),
                    play_upload.upload(play_continue_ui, [play_upload, play_token], new_game_outputs,
                                       concurrency_limit=1),
                ]
                loaded = load_engine_button.click(play_engine_file_ui,
                                                  [engine_upload, model_parameters_upload, *player_inputs],
                                                  new_game_outputs, concurrency_limit=1)
                for event in [*started, loaded]:  # a game is on: the setup folds away
                    event.success(lambda: gr.Accordion(open=False), None, play_setup)
                loaded.success(lambda: play_runs_ui(), None, play_run).then(studio_status_ui, None, studio_status)
                play_refresh.click(play_runs_ui, play_run, play_run).then(
                    lambda: gr.Dropdown(choices=recorded_engines()), None, play_record_engine)
                demo.load(play_runs_ui, play_run, play_run)
                for event in engine_events:  # a run has finished: it is the newest to play
                    event.success(lambda: play_runs_ui(), None, play_run).then(studio_status_ui, None, studio_status)
                play_who.change(playable_quests_ui, play_who, [play_quest, quest_overview_md])
                refresh_quests.click(playable_quests_ui, play_who, [play_quest, quest_overview_md])
                randomize_stats.click(lambda: stats_points_text("random"), None, stats_points).then(
                    lambda: "defined", None, play_stats)
                balance_stats.click(lambda: stats_points_text("balanced"), None, stats_points).then(
                    lambda: "defined", None, play_stats)
                stats_points.change(check_points_text, stats_points, stats_check)
                for index, button in enumerate(play_buttons):
                    button.click(lambda token, actions, index=index: play_choose_ui(token, actions, index),
                                 [play_token, play_actions], [*play_screen, play_story], concurrency_limit=1)
                play_stop.click(play_end_ui, play_token, [*play_screen, play_story], concurrency_limit=1)
                play_save.click(download_savegame_ui, play_token, [play_save_file, play_status]).success(
                    lambda: gr.File(visible=True), None, play_save_file)
                save_game_refresh.click(progression_ui, play_who, [save_game_text, save_game_table])
                demo.load(judge_status_ui, None, game_judge_status)
                judge_game_button.click(judge_session_ui, play_token, game_judgement)

            with gr.Tab("⚖ Evaluate", id="evaluate", elem_id="dest-evaluate") as evaluate_tab:
                gr.Markdown(
                    "Gemma 4 as a game critic (`vllm` in the engine logic: an embedded model or a vLLM server). "
                    "Stories are played in a separate process, so the games in progress keep their settings. "
                    "The judge also runs in the loop of evolution when `evolution.judge_weight` > 0 (Engine logic)."
                )
                evaluate_note = gr.Markdown()  # the storyteller chosen by 🧠 Train's "Evaluate this result"
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
                    evaluation_engines = evaluation_engine_choices()
                    bench_engine = gr.Dropdown(evaluation_engines, value="", label="Storyteller",
                                               info="Finished runs (newest first), then other recorded engines.")
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
                        calib_engine = gr.Dropdown(evaluation_engines, value="", label="Storyteller")
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
                with gr.Accordion("Recorded stories: judge one, or compare two", open=False):
                    record_story = gr.Dropdown(recorded_stories(), label="Recorded story",
                                               info="Every story recorded: training previews, games played, imports.")
                    with gr.Row():
                        judge_story_button = gr.Button("Judge this story (Gemma 4)")
                        compare_story = gr.Dropdown(recorded_stories(), label="Compare with another recorded story")
                        compare_button = gr.Button("Which is the better game?")
                    story_judgement = gr.Markdown()
                    story_view = gr.Markdown()

            with gr.Tab("📦 Export", id="export", elem_id="dest-export") as export_tab:
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
                export_generation.render()  # created above the destinations: a training (🧠 Train) fills it
                export_engine_note = gr.Markdown()
                with gr.Row():
                    export_memory = gr.Checkbox(True, label="Include the learning memory (keep training offline)")
                    export_name = gr.Textbox(label="Name", placeholder="narrative-engine")
                export_button = gr.Button("Export", variant="primary")
                export_file = gr.File(label="Downloads (ZIP, or .unitypackage for Unity)", interactive=False,
                                      file_count="multiple")
                export_status = gr.Markdown()
                gr.Markdown("Which systems this API has runtimes for, and the world as a UNP receives it: "
                            "⚙ Advanced → 🔌 Integration.", elem_classes="lead")
                with gr.Accordion("Recorded engines, runs and worlds: their files", open=False):
                    with gr.Row(equal_height=True):
                        files_engine = gr.Dropdown(recorded_engines(), label="Recorded engine", scale=4,
                                                   info="Any run's best, a generation's champion, a loaded, continued "
                                                   "or retrained engine.")
                        download_record = gr.Button("Download its model file and world", scale=2)
                        refresh_record_files = gr.Button("↻ Refresh", scale=1)
                    with gr.Row():
                        record_model_file = gr.File(label="Engine model (.ane.zip)", interactive=False)
                        record_world_file = gr.File(label="World and tuning (YAML)", interactive=False)
                    runs_table = gr.Dataframe(recorded_runs(), label="Recorded runs", interactive=False)
                    record_run = gr.Dropdown(recorded_run_choices(), label="A run: its settings and generations")
                    run_detail = gr.Markdown()
                    run_generations = gr.Dataframe(label="Generations", interactive=False)
                    with gr.Row():
                        record_world = gr.Dropdown(recorded_world_choices(), label="Recorded world")
                        download_world = gr.Button("Download it as a configuration to upload")
                    record_world_download = gr.File(label="World and tuning (YAML)", interactive=False)
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
            with gr.Tab("⚙ Advanced", id="advanced", elem_id="dest-advanced") as advanced_tab:
                gr.Markdown("Technical settings: the simulation, every weight, the engine's logic, the app and its background jobs. The story's own rules are in 🌍 World → 📜 Story rules.", elem_classes="lead")
                with gr.Tabs():
                    with gr.Tab("Simulation"):
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
                        gr.Markdown("#### Training defaults\nWhat a run uses for whatever it does not set itself: "
                                    "**🧠 Train** sets a run's own values and shows what it will run with before it "
                                    "starts. The terminal (`python main.py`) and API clients use them too. Warm start "
                                    "and full-length stories are `evolution.warm_start` and "
                                    "`evolution.full_length_stories` (Engine logic).", elem_classes="lead")
                        with gr.Row():
                            pop_size = gr.Number(settings["pop_size"], label="Default population (storytellers)",
                                                 precision=0, minimum=1, info="evolution.pop_size")
                            generations = gr.Number(settings["generations"], label="Default generations", precision=0,
                                                    minimum=1, info="evolution.generations")
                            episodes = gr.Number(settings["episodes_per_genome"], label="Default stories per storyteller",
                                                 precision=0, minimum=1, info="evolution.episodes_per_genome")
                            max_steps = gr.Number(settings["max_steps"], label="Default steps per simulation",
                                                  precision=0, minimum=1, info="evolution.max_steps")
                        with gr.Row():
                            deterministic = gr.Checkbox(
                                value=settings["deterministic"], label="Deterministic runs by default",
                                info="A deterministic run repeats the same evolution with the same configuration and "
                                     "seed (reproducibility.deterministic).",
                            )
                            seed = gr.Number(settings["seed"], label="Default seed", precision=0,
                                             info="reproducibility.seed")
                        gr.Markdown("#### Game", elem_classes="lead")
                        with gr.Row():
                            health = gr.Number(settings["initial_health"], label="Starting health", precision=0, minimum=1)
                            max_health = gr.Number(
                                settings["max_health"], label="Maximum health", precision=0, minimum=1,
                                info="Healing never raises health above this value.",
                            )
                            coins = gr.Number(settings["initial_coins"], label="Starting coins", precision=0, minimum=0)
                            max_actions = gr.Number(
                                settings["max_actions"], label="Story length (game action limit)", precision=0,
                                minimum=100, info="Turns before a story ends (narrative_end.max_actions, at least 100); "
                                "full-length training stories are this long.",
                            )
                        save_config = gr.Button("Save settings", variant="primary")
                        config_status = gr.Markdown()
                        setting_components = [pop_size, generations, episodes, max_steps, health, coins, max_actions,
                                              deterministic, seed, max_health]
                        activate_config.click(upload_config, config_upload, [*setting_components, config_status])
                        restore_config.click(use_builtin_config, None, [*setting_components, config_status])
                        save_config.click(save_settings, setting_components, config_status)

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

                    with gr.Tab("Weights"):
                        gr.Markdown(
                            "Every numeric weight, bonus, penalty, threshold, chance, rate, mitigation, floor and "
                            "power used by scoring and the narrative engine. Each says what the engine does with it, "
                            "its unit (chances have different scales: a percentage of the 1–100 roll is not a "
                            "probability), the engine's default and the bounds the engine sets, then its parameter "
                            "path. Whole-number parameters take whole numbers only; values you do not change are "
                            "saved exactly as they were."
                        )
                        weight_infos = API.get("/v1/weights")["parameters"]
                        weight_paths = [info["path"] for info in weight_infos]
                        weight_components = []
                        grouped_weights = {}
                        for info in weight_infos:
                            grouped_weights.setdefault(info["section"], []).append(info)
                        for section, section_entries in grouped_weights.items():
                            described = sum(info["described"] for info in section_entries)
                            with gr.Accordion(f"{section} ({described} of {len(section_entries)} described)",
                                              open=False):
                                for index in range(0, len(section_entries), 3):
                                    with gr.Row():
                                        for info in section_entries[index:index + 3]:
                                            weight_components.append(weight_field(info))
                        save_weights_button = gr.Button("Save all weights", variant="primary")
                        weights_status = gr.Markdown()
                        save_weights_button.click(save_weights, [gr.State(weight_paths), *weight_components], weights_status)

                    with gr.Tab("Engine logic"):
                        gr.Markdown(
                            "The engine's logic runs in the backend scripts; here you change **how it behaves** "
                            "through its parameters, section by section. Every section is checked (same settings, "
                            "same value types) before it is saved, and new simulations and games use it at once."
                        )
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

                    with gr.Tab("App & jobs"):
                        with gr.Accordion("App parameters", open=True):
                            gr.Markdown("Saved in config/ui_settings.yml. Upload limits are in bytes. A training's crossover is "
                                        "evolution.crossover_rate (⚙ Advanced → Weights → Training).")
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
                    with gr.Tab("🗂 Records", id="advanced-records"):
                        records_status = gr.Markdown(records_status_text())
                        with gr.Accordion("Import earlier engines and games (the server's import folder)", open=True):
                            gr.Markdown("Engine models, offline exports, older engine ZIPs and story configurations "
                                        "in the server's import folder are read, completed (tag vectors, a score, "
                                        "their run, a preview story) and recorded; files that are not engines or "
                                        "games, or whose world is invalid, stay where they are.")
                            import_note = gr.Markdown()
                            with gr.Row():
                                import_move = gr.Checkbox(False, label="Move the files into the records (else copy)")
                                import_preview = gr.Checkbox(True, label="Play a preview story of each new engine")
                                import_button = gr.Button("Import", variant="primary")
                            import_status = gr.Markdown()
                    with gr.Tab("🔌 Integration", id="advanced-integration"):
                        gr.Markdown("Diagnostics for a UNP (the game platform that launches and drives the exported "
                                    "ANE AI): its protocol tried on the game in ▶ Play, session reports sent again, "
                                    "the runtimes this API exports and the world it sends. How a UNP uses the ANE AI: "
                                    "`docs/UNP.md`.", elem_classes="lead")
                        with gr.Accordion("The game in ▶ Play as a UNP sees it: the player, events, parameters",
                                          open=True):
                            integration_session = gr.Markdown(integration_session_text(None))
                            gr.Markdown(
                                "What the exported ANE AI does for a UNP, tried on that game: **set the player**, tell "
                                "it **what happened** in the UNP's game, read its **parameters** "
                                "(`ane.session_state/1`). ▶ Play's *Save game* downloads its `ane.savegame/1`; "
                                "*Continue a save game* goes on from one (the ANE AI's too), exactly or rebuilt from "
                                "its parameters."
                            )
                            with gr.Row():
                                player_json = gr.Code(value='{"coins": 20}', language="json", lines=4,
                                                      label="Set the player (stat_points, stat_changes, inventory, "
                                                            "add_items, remove_items, coins, health, max_health, "
                                                            "variables)")
                                with gr.Column():
                                    sync_kind = gr.Dropdown(list(SYNC_EVENTS), value="item_gained",
                                                            label="What happened in the UNP's game")
                                    sync_value = gr.Textbox(label="Its action, item, NPC, location or amount")
                                    with gr.Row():
                                        player_button = gr.Button("Set the player")
                                        sync_button = gr.Button("Tell the session")
                            parameters_button = gr.Button("Show the session's parameters (ane.session_state/1)")
                            parameters_view = gr.Code(language="json", label="Session parameters", interactive=False)
                        with gr.Accordion("Session reports sent to the UNP", open=False):
                            gr.Markdown("A session's report goes to the UNP when it ends (`session_report` in the "
                                        "engine logic); ▶ Play's save game shows whether it was sent.")
                            with gr.Row(equal_height=True):
                                report_profile = gr.Dropdown(profiles["profiles"], value=profiles["default"],
                                                             allow_custom_value=True, label="Player profile (save game)",
                                                             scale=3)
                                resend_report_button = gr.Button("Send its latest report to the UNP again", scale=2)
                            save_game_status = gr.Markdown()
                        with gr.Accordion("Where the ANE AI runs, and the world it sends", open=False):
                            gr.Markdown("The standalone runtimes this API has (built by CI for each system, from the "
                                        "same engine code), and the world as the ANE AI sends it to a UNP "
                                        "(`GET /world`).")
                            export_platforms = gr.Dataframe(export_platforms_table(), interactive=False, wrap=True)
                            with gr.Row():
                                refresh_platforms = gr.Button("Refresh")
                                catalogue_button = gr.Button("Download the world as a UNP receives it (JSON)")
                            catalogue_file = gr.File(label="World catalogue (ane.world/1)", interactive=False)
                            catalogue_status = gr.Markdown()
                        # The diagnostics act on the game in ▶ Play; what they change is shown there and here.
                        player_button.click(update_player_ui, [play_token, player_json], [*play_screen, play_story],
                                            concurrency_limit=1).success(session_parameters_ui, play_token,
                                                                         parameters_view)
                        sync_button.click(sync_session_ui, [play_token, sync_kind, sync_value],
                                          [*play_screen, play_story], concurrency_limit=1).success(
                            session_parameters_ui, play_token, parameters_view)
                        parameters_button.click(session_parameters_ui, play_token, parameters_view)
                        resend_report_button.click(resend_report_ui, report_profile, save_game_status)
                        refresh_platforms.click(export_platforms_table, None, export_platforms)
                        catalogue_button.click(world_catalogue_ui, None, [catalogue_file, catalogue_status])


        # Across destinations: a finished run or a loaded engine is the newest to export.
        for event in [*engine_events, loaded]:
            event.success(lambda platform, source: export_form_ui(platform, source), [export_platform, export_source],
                          export_form)

        # Navigation: the banner, the menu, the address (?section=<id>) and the links inside a destination all
        # select the destination's tab; nothing is re-rendered, so every form, game and run keeps its state.
        # Leaving 🌍 World with unsaved changes in the entry editor asks first (Save, Discard or Stay).
        leave_inputs = [dest_now, *guard_inputs]
        nav_events = {key: button.click(lambda *rest, key=key: nav_choice_ui(key, *rest), leave_inputs, draft_outputs,
                                        show_progress="hidden")
                      for key, button in zip(DESTINATION_IDS, nav_buttons)}
        train_button_event = nav_events["train"]
        menu_event = nav_menu.input(nav_choice_ui, [nav_menu, *leave_inputs], draft_outputs, show_progress="hidden")
        destination_tabs = {"world": world_tab, "train": train_tab, "play": play_tab, "evaluate": evaluate_tab,
                            "export": export_tab, "advanced": advanced_tab}
        for key, tab in destination_tabs.items():  # a tab chosen another way: the banner and the menu follow
            tab.select(lambda key=key: mark_destination(key), None, [*nav_buttons, nav_menu, dest_now],
                       show_progress="hidden")
        # Each panel's Undo history follows into this browser (after each deletion or undo), and comes back
        # after a reload.
        for name, state, bar, said in (("entry", entry_undo_state, entry_undo_bar, entry_undo_said),
                                       ("action", action_undo_state, action_undo_bar, action_undo_said),
                                       ("mission", mission_undo_state, mission_undo_bar, mission_undo_said)):
            for event in history_events[name]:
                event.then(lambda kept: kept, state, saved_histories[name], show_progress="hidden")
            demo.load(restore_history_ui, saved_histories[name], [state, bar, said], show_progress="hidden")

        # The records, used across destinations: continue or retrain in 🧠 Train, judge their stories in
        # ⚖ Evaluate, download their files in 📦 Export, import and see whether recording is on in ⚙ Advanced.
        record_lists = [record_engine, files_engine, record_story, runs_table, records_status]
        for event in engine_events:  # a finished run is recorded
            event.success(refresh_records_ui, None, record_lists)
        for refresh in (refresh_records, refresh_record_files):
            refresh.click(refresh_records_ui, None, record_lists).then(
                lambda: (gr.Dropdown(choices=recorded_stories()), gr.Dropdown(choices=recorded_run_choices()),
                         gr.Dropdown(choices=recorded_world_choices())), None, [compare_story, record_run, record_world])
        record_run.change(run_detail_ui, record_run, [run_detail, run_generations])
        download_world.click(download_world_ui, record_world, record_world_download)
        judge_story_button.click(judge_story_ui, record_story, story_judgement)
        compare_button.click(compare_stories_ui, [record_story, compare_story], story_judgement)
        download_record.click(download_recorded_ui, files_engine, [record_model_file, record_world_file])
        record_story.change(story_view_ui, record_story, story_view)
        demo.load(import_folder_ui, None, import_note)
        import_button.click(import_records_ui, [import_move, import_preview], import_status,
                            concurrency_limit=1).then(refresh_records_ui, None, record_lists).then(
            import_folder_ui, None, import_note)

        # 🧠 Train's completed engine, carried by its exact id to ▶ Play, ⚖ Evaluate and 📦 Export: each opens
        # with that engine chosen, and nothing starts (no game, no judgement, no export) until the user asks.
        play_result.click(play_result_ui, [train_result, *player_inputs],
                          [*nav_outputs, play_run, play_setup, *new_game_outputs], concurrency_limit=1).success(
            integration_session_text, play_token, integration_session, show_progress="hidden")
        evaluate_result.click(evaluate_result_ui, train_result, [*nav_outputs, bench_engine, calib_engine, evaluate_note],
                              show_progress="hidden")
        export_result.click(export_result_ui, [train_result, export_platform], [*nav_outputs, *export_form],
                            show_progress="hidden")
        # ⚙ Advanced → 🔌 Integration names the game it acts on: the one open in ▶ Play.
        for event in [*started, loaded, nav_events["advanced"], menu_event]:
            event.then(integration_session_text, play_token, integration_session, show_progress="hidden")
        # Train's summary follows the saved defaults (e.g. changed in ⚙ Advanced): on load, and on the way to Train.
        demo.load(run_form_ui, run_controls, run_form, show_progress="hidden")
        for event in (train_button_event, menu_event):
            event.then(run_form_ui, run_controls, run_form, show_progress="hidden")
        demo.load(open_requested, None, nav_outputs, show_progress="hidden")

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
        head=DRAFT_HEAD,  # with unsaved changes in the entry editor, the browser asks before leaving the page
    )
