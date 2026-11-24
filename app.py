"""Gradio UI of the Adaptive Narrative Studio: a client of the REST API.

Every operation goes through an endpoint of the API (api.py). This file only
builds the interface and shows what the API returns; validation, storage,
authoring, evolution, play and records all live behind the API.

The connection (``StudioAPI``, below):

* always HTTP to the API at ``ANE_API_URL`` (Compose: ``http://hamlet-ane-api:8005``; the
  public deployment: ``http://ane.hamlet.iti.gr``), or ``http://127.0.0.1:8005`` when it is
  unset. The studio never serves the API itself, so it and every other client share one engine
  (sessions, runs, records); start the API first.

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

import copy
import functools
import html
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

import gradio as gr
from starlette.middleware import Middleware
import pandas as pd
import yaml

# The connection to the REST API: HTTP only (studio_client.py), to ANE_API_URL or, when it is
# unset, http://127.0.0.1:8005. Re-exported here (app.APIError, app.StudioAPI, ...).
from studio_client import (
    CLIENT_HEADER, DEFAULT_API_URL, PUBLIC_API_URL, APIError, JobCancelledError, StudioAPI,
)

LOG = logging.getLogger("ane.studio")


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
#world-sections > .tab-wrapper {display: none !important}
#world-nav, #world-content-nav {gap: .35rem; flex-wrap: wrap; align-items: center}
#world-nav > button, #world-content-nav > button {flex: 0 0 auto !important; width: auto !important;
                                                  min-width: max-content !important}
#world-nav::before {content: "World"; font-size: .8rem; font-weight: 600; opacity: .7; margin-right: .15rem}
#world-content-nav {padding-left: 1rem; border-left: 3px solid var(--border-color-primary)}
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
.weights-draft ul {margin: .15rem 0 .3rem 1.2rem; font-weight: 400}
.weights-advanced-changes {margin-top: .25rem; padding: .3rem .5rem; border-left: 3px solid #b45309;
                           background: color-mix(in srgb, #b45309 10%, transparent)}
.weights-basic-area {padding: .4rem .6rem}
#library-results .wrap {flex-direction: column; flex-wrap: nowrap; max-height: 22rem; overflow-y: auto; gap: .15rem}
.draft-saved {font-weight: 400; color: var(--body-text-color-subdued)}
#library-results label {width: 100%; justify-content: flex-start; text-align: left; font-size: .88rem}
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
# A destination chosen and not yet answered (window.aneNavShow, DRAFT_HEAD): shown as the highlight, the
# one open dimmed, until the server's answer sets the buttons' own variants.
NAV_PENDING_CSS = ("#ane-banner[data-pending] > button.primary {background: var(--button-secondary-background-fill) "
                   "!important; color: var(--button-secondary-text-color) !important; border-color: "
                   "var(--button-secondary-border-color) !important}\n"
                   + ",\n".join(f'#ane-banner[data-pending="{key}"] > #nav-{key}' for key in DESTINATION_IDS)
                   + " {background: var(--button-primary-background-fill) !important; color: "
                     "var(--button-primary-text-color) !important; border-color: var(--button-primary-border-color) "
                     "!important}\n")
CSS += NAV_PENDING_CSS
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
    body = _fields_body(kind, selected_name, name, tags, descriptions, goals, unique, encounter_event,
                        encounter_repeat, enabled_if, title, stat_bonus, start_eligible, start_weight, role, secret)
    saved = API.put(_path(f"/v1/entities/{kind}", selected_name or body["name"]), body)
    return gr.Dropdown(choices=_entity_names(kind), value=saved["name"]), f"Saved **{saved['name']}**."


def _fields_body(kind, selected_name, name, tags, descriptions, goals, unique, encounter_event, encounter_repeat,
                 enabled_if, title, stat_bonus, start_eligible, start_weight, role=None, secret=None):
    """The entry form as PUT /v1/entities/{kind}/{name} (and the entry draft's fields) take it."""
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
    if not selected_name and name in _entity_names(kind):  # PUT to a taken name would replace that entry
        raise gr.Error(f"An entry named `{name}` already exists. Choose a unique name, or pick it from the list "
                       "to edit it.")
    return body


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


def policy_controls_ui(kind):
    """Scheduling's Save and YAML editor: for events and items only (the API keeps no rules for others)."""
    shown = kind in ("Events", "Items")
    return gr.update(visible=shown), gr.update(visible=shown)


@ui
def save_entity_policy_ui(kind, name, yaml_text):
    if not name:
        raise gr.Error("Select an entry first.")
    if kind not in ("Events", "Items"):
        raise gr.Error("Only events and items have scheduling rules; a place's or a person's behaviour comes from "
                       "its tags (Fields, and *Behaviour by tags*).")
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


def load_side_quests_yaml():
    return load_document("side_quests")


def save_side_quests_yaml(text):
    return save_document("side_quests", text)


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


def reset_question_ui(profile):
    """*Reset profile*: what it does, asked before it is done (it cannot be undone)."""
    name = (profile or "").strip() or "default"
    return gr.update(visible=True), (
        f"### ⚠ Reset the profile **{html.escape(name)}**?\n"
        "It starts over as a new player: its stored values, playthroughs, goals achieved and session reports are "
        "lost, and **this cannot be undone**. Unsaved changes in *Stored values* are lost too.")


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


RULE_NUMBERS = ("pursuit_start_step", "pursuit_full_step", "route_weight", "auto_travel_on_spawn_threshold")


@ui
def save_rules_ui(*values):
    rules = dict(zip(REFERENCE["rule_fields"], values))
    # PUT /v1/rules keeps the stored value of a field sent as null: a cleared number would be kept silently.
    empty = [field for field in RULE_NUMBERS if field in rules and rules[field] in (None, "")]
    if empty:
        raise gr.Error("Give a number for: " + ", ".join(f"`{field}`" for field in empty)
                       + " (an empty field would keep its saved value).")
    API.put("/v1/rules", rules)
    return "Saved the story rules."


def _number(value):
    return f"{value:g}" if isinstance(value, float) else str(value)


def _weight_facts(info):
    """Unit, engine default, the engine's bounds and whole/decimal number, joined."""
    facts = [f"Unit: {info['unit']}"] if info.get("unit") else []
    if info.get("shipped") is not None:
        facts.append(f"shipped {_number(info['shipped'])}")
    if info.get("default") is not None:
        facts.append(f"engine default {_number(info['default'])}")
    low, high = info.get("minimum"), info.get("maximum")
    if low is not None and high is not None:
        facts.append(f"from {_number(low)} to {_number(high)}")
    elif low is not None:
        facts.append(f"at least {_number(low)}")
    elif high is not None:
        facts.append(f"at most {_number(high)}")
    facts.append("whole number" if info["type"] == "integer" else "decimal number")
    return " · ".join(facts)


def _inactive_note(info):
    return (f"Inactive now: {info['requires']} is off (it takes effect when that is on). "
            if info.get("inactive") else "")


def weight_help(info, basic=None):
    """A tuning parameter's help, under its label (Advanced view): what the engine does with it, its
    unit, the engine's default, the bounds the engine defines and whether it is a whole number, then
    its path (a parameter without a description says so, with its path). ``basic``: the Basic view's
    area that also shows it."""
    kind = "whole number" if info["type"] == "integer" else "decimal number"
    if not info["described"]:
        return f"No description yet ({kind}, as written). Parameter: {info['path']}"
    text = f"{info.get('explanation') or ''} {_weight_facts(info)}. Parameter: {info['path']}".strip()
    if basic:
        text = f"Also in Basic → {basic}. {text}"
    return _inactive_note(info) + text  # inactive: no effect while the setting it needs is off


def _weight_number(info, label, help_text, elem_id, classes):
    integer = info["type"] == "integer"
    return gr.Number(value=info["value"], label=label, info=help_text, precision=0 if integer else None,
                     minimum=info.get("minimum"), maximum=info.get("maximum"), elem_id=elem_id,
                     elem_classes=["weight", *classes, *(["weight-inactive"] if info.get("inactive") else [])])


def weight_elem_id(view, path):
    """The element id of a parameter's field in a view (basic | advanced)."""
    return f"weight-{view}-" + re.sub(r"[^A-Za-z0-9_-]", "-", path)


def weight_field(info, basic=None):
    """A tuning parameter's input (Weights → Advanced): its readable label and help; an integer
    parameter takes whole numbers only; the engine's bounds, where it defines them."""
    return _weight_number(info, info["label"], weight_help(info, basic), weight_elem_id("advanced", info["path"]),
                          ["weight-described" if info["described"] else "weight-plain"])


def basic_weight_field(info, control):
    """A Basic control (Weights → Basic): the same parameter as its Advanced field (same label, type and
    bounds), explained by what raising or lowering it does in play (and how far each storyteller's
    learned traits move it), with its unit and path."""
    scaling = f" {control['scaling']}" if control.get("scaling") else ""
    help_text = (f"{_inactive_note(info)}{control['effect']}{scaling} {_weight_facts(info)}. "
                 f"Parameter: {info['path']}")
    return _weight_number(info, info["label"], help_text, weight_elem_id("basic", info["path"]), ["weight-basic"])


def weights_meta(parameters, basic_areas, shipped=None):
    """What the weights draft needs to know, fixed when the tab is built: the parameters' paths (the
    Advanced fields' order), their labels, the Basic controls' paths (their fields' order) and area, and
    the values this release ships."""
    basic = {control["path"]: area["title"] for area in basic_areas for control in area["controls"]}
    return {"paths": [info["path"] for info in parameters], "labels": {i["path"]: i["label"] for i in parameters},
            "basic_paths": list(basic), "basic": basic, "shipped": dict(shipped or {})}


def shipped_changes(meta, saved):
    """The saved values that differ from what this release ships: [(path, shipped, saved)]."""
    shipped = meta.get("shipped") or {}
    return [(path, shipped[path], (saved or {}).get(path)) for path in meta["paths"]
            if path in shipped and not _same_number((saved or {}).get(path), shipped[path])]


def weights_shipped_markup(meta, saved):
    """Which saved values were changed from the shipped ones (this release's configuration), those
    Basic shows and those only Advanced shows apart; shown above both views."""
    if not meta.get("shipped"):
        return ('<div class="weights-shipped" data-changed="">The shipped values are not known here (an older '
                '<code>default_history.json</code>): run <code>python packaging/default_history.py</code>.</div>')
    changes = shipped_changes(meta, saved)
    if not changes:
        return '<div class="weights-shipped" data-changed="0">Every saved value is the shipped one.</div>'

    def listed(items):
        return "".join(f"<li>{html.escape(meta['labels'].get(path, path))} <code>{html.escape(path)}</code>: shipped "
                       f"{html.escape(_shown(old))}, saved {html.escape(_shown(new))}</li>" for path, old, new in items)

    basic = [c for c in changes if c[0] in meta["basic"]]
    advanced = [c for c in changes if c[0] not in meta["basic"]]
    parts = [f"<summary>Changed from the shipped values ({len(changes)}): {len(basic)} in Basic, "
             f"{len(advanced)} only in Advanced</summary>"]
    if basic:
        parts.append(f"<div>In Basic and Advanced ({len(basic)}):<ul>{listed(basic)}</ul></div>")
    if advanced:
        parts.append(f'<div class="weights-advanced-changes">⚠ Only in Advanced, not shown in Basic '
                     f"({len(advanced)}):<ul>{listed(advanced)}</ul></div>")
    return f'<details class="weights-shipped" data-changed="{len(changes)}">{"".join(parts)}</details>'


def shipped_into_draft_ui(meta, saved, *values):
    """↺ Shipped values: every parameter whose saved value differs from the shipped one gets its shipped
    value in the draft (both views; each parameter its own value, nothing else changes). Nothing is
    saved until *Save changes*. Returns (every Advanced field, every Basic field, the draft's status, a
    message)."""
    changes = {path: shipped for path, shipped, _ in shipped_changes(meta, saved)}
    if not changes:
        return (*[gr.skip()] * (len(meta["paths"]) + len(meta["basic_paths"])),
                weights_draft_markup(meta, saved, *values), "Every saved value is already the shipped one.")
    draft = [changes.get(path, value) for path, value in zip(meta["paths"], values)]
    advanced = [changes[path] if path in changes else gr.skip() for path in meta["paths"]]
    basic = [changes[path] if path in changes else gr.skip() for path in meta["basic_paths"]]
    return (*advanced, *basic, weights_draft_markup(meta, saved, *draft),
            f"The shipped values of {len(changes)} parameter(s) are in the draft: **Save changes** to keep them.")


def _same_number(a, b):
    if a is None and b is None:
        return True
    try:
        return a is not None and b is not None and float(a) == float(b)
    except (TypeError, ValueError):
        return False


def weight_changes(meta, saved, values):
    """The draft's changes: [(path, saved value, draft value)] in the Advanced fields' order. ``values``:
    the Advanced fields (every parameter; Basic edits are copied into them)."""
    return [(path, (saved or {}).get(path), value) for path, value in zip(meta["paths"], values)
            if not _same_number(value, (saved or {}).get(path))]


def weights_draft_markup(meta, saved, *values):
    """The weights draft's status, shown above both views: no unsaved changes, or every change, those
    made in Advanced to parameters Basic does not show listed apart (so a Basic user sees them)."""
    changes = weight_changes(meta, saved, values)
    if not changes:
        return ('<div class="draft-status weights-draft" data-dirty="0" data-advanced="0">✓ No unsaved changes: '
                'Basic and Advanced show the saved values.</div>')

    def listed(items):
        return "".join(f"<li>{html.escape(meta['labels'].get(path, path))} <code>{html.escape(path)}</code>: "
                       f"{html.escape(_shown(old))} → {html.escape(_shown(new))}</li>" for path, old, new in items)

    basic = [c for c in changes if c[0] in meta["basic"]]
    advanced = [c for c in changes if c[0] not in meta["basic"]]
    parts = [f"● Unsaved changes ({len(changes)}), one draft for both views; Save saves them all."]
    if basic:
        parts.append(f"<div>In Basic and Advanced ({len(basic)}):<ul>{listed(basic)}</ul></div>")
    if advanced:
        parts.append(f'<div class="weights-advanced-changes">⚠ Advanced only, not shown in Basic ({len(advanced)}):'
                     f"<ul>{listed(advanced)}</ul></div>")
    return (f'<div class="draft-status dirty weights-draft" data-dirty="1" data-advanced="{len(advanced)}">'
            + "".join(parts) + "</div>")


def _shown(value):
    return "(empty)" if value is None else _number(value)


@ui
def save_weights(meta, saved, *values):
    """Save the weights draft: only the parameters it changed, each with the value it had when loaded
    (a parameter changed since by someone else is refused, not overwritten). Returns (message, the
    saved values, the draft's status). A refused save keeps the draft."""
    changes = weight_changes(meta, saved, values)
    if not changes:
        return "Nothing to save: every value is the saved one.", saved, weights_draft_markup(meta, saved, *values)
    empty = [meta["labels"].get(path, path) for path, _, new in changes if new is None]
    if empty:
        raise gr.Error("Nothing was saved: give a number to " + ", ".join(empty) + ".")
    result = API.put("/v1/weights", {"values": {path: new for path, _, new in changes},
                                     "expected": {path: old for path, old, _ in changes if old is not None}})
    saved = dict(result["values"])
    return result["message"], saved, weights_draft_markup(meta, saved, *values)


@ui
def reload_weights_ui(meta):
    """The saved values into both views (on loading the page, and Discard). Returns (saved values, every
    Advanced field, every Basic field, the draft's status, a note)."""
    values = API.get("/v1/weights")["values"]
    advanced = [values[path] if path in values else gr.skip() for path in meta["paths"]]
    basic = [values[path] if path in values else gr.skip() for path in meta["basic_paths"]]
    shown = [values[path] if path in values else None for path in meta["paths"]]
    extra = sorted(set(values) - set(meta["paths"]))
    note = (f"The configuration now has parameters this page does not show ({', '.join(extra)}): reload the page."
            if extra else "")
    return (values, *advanced, *basic, weights_draft_markup(meta, values, *shown), note)


def weights_dirty(meta, saved, dirty, values):
    """drafts_dirty with the weights' unsaved parts: "Basic" and/or "Advanced only" (what the leave
    guard asks about); no entry when nothing is unsaved."""
    changes = weight_changes(meta, saved, values)
    parts = (["Basic"] if any(path in meta["basic"] for path, _, _ in changes) else []) + (
        ["Advanced only"] if any(path not in meta["basic"] for path, _, _ in changes) else [])
    return {**{key: value for key, value in (dirty or {}).items() if key != "weights"},
            **({"weights": parts} if parts else {})}


def weights_draft_ui(meta, saved, dirty, *values):
    """An edit in either view: the draft's status, and drafts_dirty (for the leave guard)."""
    return weights_draft_markup(meta, saved, *values), weights_dirty(meta, saved, dirty, values)


def weights_after_ui(meta, saved, dirty, *values):
    """After a save, a reload or the shipped values: the draft's status, drafts_dirty and which saved
    values differ from the shipped ones."""
    return (*weights_draft_ui(meta, saved, dirty, *values), weights_shipped_markup(meta, saved))


def weights_resolve_ui(choice, pending, meta, saved, dirty, *values):
    """The weights' leave question answered: **save** the draft (a refusal keeps it, and asks again with
    why), or **discard** it (the saved values come back), then go where the user was going; or **stay**.
    Returns (the question's box, its text, the pending target, the navigation, the saved values, every
    Advanced field, every Basic field, the draft's status, the message, the shipped panel, drafts_dirty)."""
    fields = len(meta["paths"]) + len(meta["basic_paths"])
    skip_nav = [gr.skip()] * len(navigate("world"))
    if not pending or choice == "stay":
        return (gr.update(visible=False), "", None, *skip_nav, gr.skip(), *[gr.skip()] * fields, gr.skip(), gr.skip(),
                gr.skip(), gr.skip())
    if choice == "save":
        try:
            message, saved_now, markup = save_weights(meta, saved, *values)
        except gr.Error as exc:
            return (gr.update(visible=True), f"⚠ **The weights could not be saved:** {_error_text(exc)}  \nNothing was "
                    "discarded: correct it and **Save** again, **Stay**, or **Discard**.", pending, *skip_nav,
                    gr.skip(), *[gr.skip()] * fields, gr.skip(), gr.skip(), gr.skip(), gr.skip())
        fields_out = [gr.skip()] * fields
    else:
        saved_now, *reloaded = reload_weights_ui(meta)
        fields_out, markup, message = reloaded[:fields], reloaded[fields], "The unsaved weights were discarded."
    dirty_now = {key: value for key, value in (dirty or {}).items() if key != "weights"}
    return (gr.update(visible=False), "", None, *navigate(pending["id"]), saved_now, *fields_out, markup, message,
            weights_shipped_markup(meta, saved_now), dirty_now)


def weights_view_ui(view):
    """Basic or Advanced: only which fields are shown changes, never their values (one draft)."""
    return gr.update(visible=view == "Basic"), gr.update(visible=view != "Basic")


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


SIDE_QUEST_MARKS = {"offered": "❗", "active": "▶️", "completed": "✅", "failed": "✗", "declined": "▫️"}


def side_quests_markdown(side_quests):
    """The side quests of the story (PlayState ``side_quests``), under the quest: each with its state,
    who gave it, the current step and what it still needs. Empty when there are none."""
    if not side_quests:
        return ""
    lines = ["#### 🧭 Side quests"]
    for quest in side_quests:
        line = f"{SIDE_QUEST_MARKS.get(quest['status'], '▫️')} **{quest['title']}**"
        if quest.get("giver"):
            line += f" · for {quest['giver']}"
        current = next((stage for stage in quest.get("stages") or [] if stage["state"] == "current"), None)
        if quest["status"] == "offered":
            line += "  \nOn offer: take it on or turn it down (the first choices)."
            if quest.get("rewards"):
                line += f"  \nIn it for you: {quest['rewards']}."
        elif quest["status"] == "active" and quest.get("ready"):
            line += f"  \n📦 Done: take it back to {quest.get('giver') or 'who asked'}."
        elif quest["status"] == "active":
            if current:
                line += f"  \nNow: {current['title']}"
            if quest.get("todo"):
                line += f"  \n**To do:** {'; '.join(quest['todo'])}"
            if quest.get("turns_left") is not None:
                line += f"  \n⏳ {quest['turns_left']} turn(s) left."
        else:
            line += f" · {quest['status']}" + (" · it helped your quest" if quest.get("aided_mission") else "")
        lines.append(line)
    return "\n\n".join(lines)


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
            if here[person["name"]].get("side_quests"):
                notes.append("❗ offers a side quest: " + ", ".join(here[person["name"]]["side_quests"]))
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
        delivered = _report_delivery(report)
        lines += ["\n\n### Session report", f"**{(goal.get('title') or 'Free play')}: {report['outcome']}** "
                  f"({report['end_kind']}, {report['turns']} turns) · health {player['health']}/{player['max_health']} · "
                  f"coins {player['coins']} · inventory: {', '.join(player['inventory']) or 'empty'} · stats: "
                  + ", ".join(f"{k} {v}" for k, v in player["stats"].items())
                  + f" · goals achieved in this save game: {', '.join(report['progression']['goals_achieved']) or 'none'}"
                  + delivered]
    return "\n".join(lines)


def _report_delivery(report):
    """Whether a session's report reached the UNP, as a suffix for its summary. The report is sent in the
    background once the session ends, and its delivery is noted with the save game (GET /v1/progression),
    never in the play state; nothing when no UNP is configured (nothing is sent)."""
    try:
        sessions = API.get("/v1/progression", profile=report.get("profile") or None)["sessions"]
    except APIError as exc:
        LOG.warning("Could not read the delivery of report %s: %s", report.get("report_id"), exc)
        return ""
    kept = next((r for r in sessions if r.get("report_id") == report.get("report_id")), None)
    delivery = (kept or {}).get("delivery")
    if not delivery:
        return ""
    if delivery.get("sent"):
        return " · sent to the UNP"
    return f" · not sent to the UNP ({delivery.get('error') or 'HTTP ' + str(delivery.get('status'))})"


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
    lines = ["### 🧭 Discovered map (this game)",
             "*Only the places this player has found, and the ways to them; the world's complete map is the "
             "authoring map (🌍 World → 🗺 Map).*"]
    for place in known.get("places", []):
        name = place["name"]
        mark = "📍 " if name == known.get("current") else ""
        state = "" if place["visited"] else " *(not visited)*"
        locked = " 🔒" if place["locked"] else ""
        lines.append(f"- {mark}**{name}**{locked}{state} → {', '.join(sorted(paths.get(name, []))) or 'no known way on'}")
    return "\n".join(lines)


def discovered_map_svg(known):
    """The discovered map drawn (▶ Play): only the places this player knows, on a ring; where they are
    stands out, places visited are filled, places only seen are hollow, a locked one is marked; a line for
    each path both ways, an arrow for a one-way path. Not the authoring map (🌍 World → 🗺 Map)."""
    import math

    known = known or {}
    places = [place["name"] for place in known.get("places") or []]
    if not places:
        return '<p class="discovered-map-empty">The discovered map starts with your first step.</p>'
    info = {place["name"]: place for place in known.get("places") or []}
    current, previous = known.get("current"), known.get("previous")
    size = min(520, 200 + 50 * len(places))  # a few places: a small drawing, its labels readable
    radius = 0 if len(places) == 1 else size * 0.36
    spots = {place: (size / 2 + radius * math.cos(2 * math.pi * i / len(places) - math.pi / 2),
                     size / 2 + radius * math.sin(2 * math.pi * i / len(places) - math.pi / 2))
             for i, place in enumerate(places)}
    parts = ['<defs><marker id="dm-arrow" viewBox="0 0 10 10" refX="16" refY="5" markerWidth="7" markerHeight="7" '
             'orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#64748b"/></marker></defs>']
    for a, b in known.get("paths") or []:
        if a in spots and b in spots:
            (x1, y1), (x2, y2) = spots[a], spots[b]
            lit = current in (a, b)
            parts.append(f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" stroke="{"#0d9488" if lit else "#94a3b8"}" '
                         f'stroke-width="{3 if lit else 1.5}"/>')
    for a, b in known.get("one_way") or []:
        if a in spots and b in spots:
            (x1, y1), (x2, y2) = spots[a], spots[b]
            parts.append(f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" stroke="#64748b" '
                         'stroke-width="1.5" stroke-dasharray="5 4" marker-end="url(#dm-arrow)"/>')
    for place, (x, y) in spots.items():
        visited = info[place].get("visited")
        here = place == current
        fill = "#0d9488" if here else "#99f6e4" if visited else "none"
        anchor = "start" if x > size / 2 + 5 else "end" if x < size / 2 - 5 else "middle"
        dx = 12 if anchor == "start" else -12 if anchor == "end" else 0
        dy = 20 if y > size / 2 + radius * .9 else -12 if y < size / 2 - radius * .9 else 4
        label = place.replace("&", "&amp;").replace("<", "&lt;")
        marks = (" 📍" if here else "") + (" 🔒" if info[place].get("locked") else "") + (" ↩" if place == previous else "")
        weight = ' font-weight="bold"' if here else ""
        parts.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="{10 if here else 8}" fill="{fill}" stroke="#0f766e" '
                     f'stroke-width="1.5"{" stroke-dasharray=" + chr(34) + "3 2" + chr(34) if not visited else ""}/>'
                     f'<text x="{x + dx:.0f}" y="{y + dy:.0f}" text-anchor="{anchor}" font-size="13" fill="currentColor"'
                     f'{weight}>{label}{marks}</text>')
    legend = ('<p class="discovered-map-legend">🧭 <b>Discovered map (this game)</b>: filled, visited · hollow, seen '
              'but not visited · 📍 here · ↩ where you came from · 🔒 locked · dashed arrow, one way. Not the whole '
              'world: that is the authoring map (🌍 World → 🗺 Map).</p>')
    return (f'{legend}<svg viewBox="-100 0 {size + 200} {size}" style="width:100%;max-width:{size + 200}px;height:auto" '
            f'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Discovered map">{"".join(parts)}</svg>')


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


SETTING_NAMES = {"pop_size": "population", "generations": "generations", "episodes_per_genome": "stories per storyteller",
                 "max_steps": "turns per story", "validation_episodes": "benchmark stories",
                 "warm_start": "from the learning memory", "crossover_rate": "crossover", "mutation_rate": "mutation",
                 "elitism_rate": "elitism", "player": "simulated player"}


def _setting_text(key, value):
    shown = ("yes" if value else "no") if isinstance(value, bool) else _number(value) if isinstance(value, (int, float)) \
        else str(value)
    return f"{SETTING_NAMES.get(key, key.replace('_', ' '))} {shown}"


def _date(value):
    return str(value or "")[:16].replace("T", " ") or "unknown date"


def engine_card_markdown(card, heading=True):
    """An engine's result card (GET /v1/engines/{id}/card) as Markdown: its name or label, ids, when it
    was made, its world, its training and settings, then its training fitness and the judge's
    engagement, kept apart (different measures on different scales)."""
    title = card.get("name") or card.get("default_label") or card["engine_id"]
    lines = [f"### 🏷 {title}" + ("" if card.get("name") else " *(unnamed)*")] if heading else []
    ids = f"engine `{card['engine_id']}`" + (f" · run `{card['run_id']}`" if card.get("run_id") else "")
    lines.append(f"{card.get('kind') or 'Engine'} made **{_date(card.get('created_at'))}** · {ids}")
    world = card.get("world") or {}
    summary = world.get("summary") or {}
    counts = " · ".join(f"{summary[key]} {label}" for key, label in (
        ("locations", "places"), ("npcs", "people"), ("items", "items"), ("events", "events"), ("missions", "quests"))
        if key in summary)
    where = {True: "✓ the active world", False: "⚠ not the active world (playing it makes its world active)"}.get(
        world.get("current"), "")
    world_id = f"world `{world['id'][:12]}`" if world.get("id") else ""
    lines.append("- **World:** " + (" · ".join(part for part in (counts, world_id, where) if part) or "not recorded"))
    generations = card.get("generations") if card.get("generations") is not None else card.get("generation")
    training = []
    if card.get("generation") is not None and card.get("generations") is None:
        training.append(f"champion of generation {card['generation']}")
    elif generations:
        training.append(f"{generations} generation{'s' if generations != 1 else ''}")
    if card.get("resumable"):
        training.append("its training can be continued")
    main = (card.get("settings") or {}).get("main") or {}
    if main:
        training.append(", ".join(_setting_text(key, value) for key, value in main.items()))
    lines.append("- **Training:** " + (" · ".join(training) or "no training state (a loaded or recorded storyteller)"))
    fitness = card.get("fitness") or {}
    score = fitness.get("training")
    if isinstance(score, (int, float)):
        extra = []
        if isinstance(fitness.get("improvement"), (int, float)):
            extra.append(f"{fitness['improvement']:+.1f} over the run")
        if fitness.get("early_stopped"):
            extra.append("stopped early")
        lines.append(f"- **Training fitness:** **{score:.2f}** (the evolution's objective: the story score on the "
                     f"run's benchmark, on its own scale){' · ' + ', '.join(extra) if extra else ''}")
    else:
        lines.append("- **Training fitness:** not benchmarked")
    judged = [e for e in card.get("evaluations") or [] if isinstance(e.get("engagement"), (int, float))]
    if judged:
        latest = judged[0]
        what = "judge benchmark" if latest.get("kind") == "judge" else "stories judged to fit the fitness"
        detail = f" (mean story fitness {latest['story_fitness']:.1f})" if isinstance(latest.get("story_fitness"),
                                                                                  (int, float)) else ""
        more = f"; {len(judged) - 1} earlier evaluation(s)" if len(judged) > 1 else ""
        lines.append(f"- **Judge engagement:** **{latest['engagement']:.2f} / 10** (an LLM judge's rating of played "
                     f"stories, not the fitness) over {latest.get('judged') or latest.get('stories')} stories, "
                     f"{what} of {_date(latest.get('at'))}{detail}{more}")
    else:
        lines.append("- **Judge engagement:** not evaluated yet (⚖ Evaluate → *Play and judge*; it rates stories "
                     "1–10, a different measure from the training fitness)")
    if isinstance(card.get("judge_in_training"), (int, float)):
        lines.append(f"- **Judge in training:** {card['judge_in_training']:.2f} / 10 (the judge's engagement in the "
                     "loop of evolution, last generation)")
    traits = card.get("scaling_traits") or {}
    if traits:
        lines.append("- **Storyteller traits that scale the Basic weights** (pacing, repetition, events): "
                     + " · ".join(f"`{trait}` {value:g}" for trait, value in traits.items()))
    other = (card.get("settings") or {}).get("other") or {}
    if other:
        lines.append("<details><summary>Every training setting</summary>\n\n"
                     + ", ".join(_setting_text(key, value) for key, value in other.items()) + "\n</details>")
    return "\n".join(lines)


def engine_card(engine_id):
    """An engine's card (GET /v1/engines/{id}/card), or None when there is none."""
    try:
        return API.get(_path("/v1/engines", engine_id, "card"))
    except APIError as exc:
        LOG.warning("The card of engine %s could not be read: %s", engine_id, exc)
        return None


def engine_card_ui(engine_id):
    """The card of the engine chosen in a picker (Play, Evaluate, Train's finished engines)."""
    if engine_id is None:
        return "*Choose an engine to see its card.*"
    if engine_id == "":
        return "*The current settings with random storytellers: no trained engine, no card.*"
    card = engine_card(engine_id)
    return engine_card_markdown(card) if card else f"*No card for engine `{engine_id}`.*"


def engine_cards_ui(engine_ids):
    """The cards of the engines chosen in Export (several may be)."""
    engine_ids = [engine_ids] if isinstance(engine_ids, str) else list(engine_ids or [])
    if not engine_ids:
        return "*Choose a finished run to see its card.*"
    return "\n\n---\n\n".join(engine_card_ui(engine_id) for engine_id in engine_ids)


def engine_name_ui(engine_id):
    """The name box of an engine: its name (empty: unnamed)."""
    card = engine_card(engine_id) if engine_id else None
    return gr.Textbox(value=(card or {}).get("name") or "", interactive=bool(card),
                      placeholder=(card or {}).get("default_label") or "Name this engine")


def finished_engines_ui(chosen=None):
    """Train's finished engines picker: every finished run and loaded engine, then the other recorded
    engines (newest first), the chosen one kept (else the newest)."""
    choices = [choice for choice in evaluation_engine_choices() if choice[1]]
    ids = [value for _, value in choices]
    return gr.Dropdown(choices=choices, value=chosen if chosen in ids else (ids[0] if ids else None))


def engine_picker_updates():
    """New choices (labels) for every engine picker after a name changed: Train's finished engines,
    Play's trained AI, Evaluate's trained engine, Export's finished runs, the recorded-engine pickers
    of Train, Play and Export. Their values (engine ids) do not change."""
    finished, recorded = finished_run_choices(), recorded_engines()
    named = evaluation_engines()
    return (gr.Dropdown(choices=named), gr.Dropdown(choices=finished), gr.Dropdown(choices=named),
            gr.Dropdown(choices=finished), gr.Dropdown(choices=recorded), gr.Dropdown(choices=recorded),
            gr.Dropdown(choices=recorded))


@ui
def rename_engine_ui(engine_id, name, done=None):
    """Name an engine (empty: no name): only what is shown changes, its id stays. Returns (a message,
    its card, its name box, Train's result card (the completed engine's, if it is that one), then every
    engine picker's new labels)."""
    if not engine_id:
        raise gr.Error("Choose an engine first.")
    result = API.put(_path("/v1/engines", engine_id, "name"), {"name": name or None})
    message = (f"Named **{result['name']}**; its id stays `{engine_id}`." if result["name"]
               else f"Name removed: it is shown as *{result['label']}*.")
    result_card = completed_view_ui(done)[0] if done and done.get("engine_id") == engine_id else gr.skip()
    return (message, engine_card_ui(engine_id), engine_name_ui(engine_id), result_card, *engine_picker_updates())


def rename_result_ui(done, name, browsed=None):
    """Name the completed engine from Train's result card: as rename_engine_ui, with the finished
    engines' card refreshed only when it shows this engine (``browsed``)."""
    if not done or not done.get("engine_id"):
        raise gr.Error("No training has finished in this session yet: run one in 🧠 Train first.")
    message, card, box, result_card, *pickers = rename_engine_ui(done["engine_id"], name, done)
    return (message, card if browsed == done["engine_id"] else gr.skip(), box, result_card, *pickers)


def result_name_ui(done):
    """Train's name box for the completed engine (empty and locked before one exists)."""
    return engine_name_ui(done.get("engine_id") if done else None)


def _label_of(engine_id, choices):
    """An engine as a follow-up action names it: its label (its name, if named) and its exact id."""
    label = next((label for label, value in choices if value == engine_id), None)
    return f"{label} (engine `{engine_id}`)" if label else f"`{engine_id}`"


def completed_view_ui(done):
    """Train's result card and its three actions (enabled once a training has completed)."""
    ready = bool(done and done.get("engine_id"))
    if not ready:
        text = NO_RESULT
    else:
        card = engine_card(done["engine_id"])
        if card:
            body = engine_card_markdown(card, heading=False)
            title = card.get("name") or card.get("default_label")
            text = f"### ✅ Completed engine: {title}\n{TRAINING_KINDS.get(done['kind'], done['kind'])}. {body}"
        else:  # the card cannot be read: what the training answered
            score = f"{done['score']:.2f}" if isinstance(done.get("score"), (int, float)) else "not benchmarked"
            text = (f"### ✅ Completed engine `{done['engine_id']}`\n"
                    f"{TRAINING_KINDS.get(done['kind'], done['kind'])} · run `{done['run_id']}` · training fitness "
                    f"**{score}** (the evolution's objective) · **{done.get('generations') or 0}** generation(s)")
        text += ((f"\n\n{done['check']}" if done.get("check") else "")
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
def play_result_ui(done, profile, mission, stats_choice, stats_points=None, titles=None):
    """▶ Play this result: a new game with exactly the completed engine starts in ▶ Play, with the
    player, quest and character chosen there (the user asked to play it: this is the game's start).
    Returns the navigation, the trained-AI picker (that engine), the setup panel (folded) and the new
    game (play_new_game_ui's outputs: the session, the screen, the story, the status)."""
    engine_id = _result_engine(done)
    choices = finished_run_choices()
    _offered(engine_id, choices, "▶ Play")
    cleared = ""
    if mission:  # the quest chosen in ▶ Play may belong to another engine's world, or not be open to the profile
        offered = [value for _, value in quest_overview(profile, engine_id)[0]]
        if mission not in offered:
            cleared = (f"  \nThe quest chosen before, {quest_name(mission, titles)}, is not offered in this engine's world for profile "
                       f"`{profile or 'default'}`: the story drew one.")
            mission = ""
    token, *game, status = play_new_game_ui(engine_id, profile, mission, stats_choice, stats_points)
    return (*navigate("play"), gr.Dropdown(choices=choices, value=engine_id), gr.Accordion(open=False), token,
            *game, f"**Playing the engine just trained, {_label_of(engine_id, choices)}.**{cleared}  \n{status}")


def evaluation_engine_choices():
    """(label, engine id) of the storytellers the judge can play: the current settings with random
    storytellers, the finished runs (newest first, kept with or without the records), then the
    other recorded engines (a generation's champion, ...)."""
    finished = finished_run_choices()
    listed = {value for _, value in finished}
    return ([("Current settings, random storytellers", "")] + finished
            + [(label, value) for label, value in recorded_engines() if value not in listed])


# What ⚖ Evaluate plays: a selected trained engine, or the explicit baseline.
TARGET_ENGINE = "Selected trained engine"
TARGET_BASELINE = "Baseline: current configuration with random storytellers"
EVALUATION_TARGETS = (TARGET_ENGINE, TARGET_BASELINE)


def evaluation_engines():
    """(label, engine id) of the trained engines Evaluate can select: finished runs (newest first), then
    the other recorded engines; the baseline is a choice of its own, not one of them."""
    return [choice for choice in evaluation_engine_choices() if choice[1]]


def evaluation_engines_ui(target, chosen=None, newest=False):
    """Evaluate's engine picker after a training, a loaded engine or ↻ Refresh: the engines offered now,
    the chosen one kept while it is offered (``newest``: the newest chosen, e.g. the one just trained).
    Returns (the target, the engine picker)."""
    choices = evaluation_engines()
    ids = [value for _, value in choices]
    value = ids[0] if ids and (newest or chosen not in ids) else (chosen if chosen in ids else None)
    if target not in EVALUATION_TARGETS:
        target = TARGET_ENGINE if ids else TARGET_BASELINE
    return gr.Radio(value=target), gr.Dropdown(choices=choices, value=value, visible=target == TARGET_ENGINE)


def evaluation_engine_id(target, engine_id):
    """The engine an evaluation plays: None for the baseline; the selected one for a trained engine
    (refused when none is selected: it is never replaced by the baseline in silence)."""
    if target == TARGET_BASELINE:
        return None
    if target == TARGET_ENGINE and not engine_id:
        raise gr.Error("Choose a trained engine to evaluate, or choose the baseline (the current configuration "
                       "with random storytellers).")
    return engine_id or None


def evaluation_target_markdown(target):
    """An evaluation's target (GET /v1/evaluation/target, or a result's ``target``) in words."""
    world = target.get("world") or {}
    summary = world.get("summary") or {}
    counts = " · ".join(f"{summary[key]} {name}" for key, name in (
        ("locations", "places"), ("npcs", "people"), ("items", "items"), ("events", "events"), ("missions", "quests"))
        if key in summary)
    identity = f"world `{(world.get('digest') or '')[:12]}`"
    if target.get("kind") == "engine":
        where = "the active world" if world.get("current") else "not the active world"
        return (f"**Selected trained engine:** {target['label']} (engine `{target['engine_id']}`): its saved world "
                f"({counts} · {identity}, {where}) and {target['storyteller']}.")
    return (f"**Baseline:** the current configuration ({counts} · {identity}) with {target['storyteller']}: no trained "
            "engine.")


def _variants_note(variants_text):
    try:
        variants = _variants(variants_text)
    except gr.Error as exc:
        return None, f"⚠ {exc}"
    return variants, ", ".join(f"`{key}: {value}`" for key, value in variants.items()) or "none"


def evaluation_plan_ui(target, engine_id, pairwise, variants_text, stories, seed, calib_stories, calib_seed):
    """Before anything is submitted: what will be evaluated (the target, its world and storyteller), the
    judge benchmark's plan (its seeds; in A/B: A and B are both the target, B with version B's settings,
    on the same seeds, each pair judged in both orders) and the calibration's. Returns (the engine
    picker's visibility, the target, its card, the benchmark's plan, the calibration's plan)."""
    shown = gr.Dropdown(visible=target == TARGET_ENGINE)
    if target == TARGET_ENGINE and not engine_id:
        missing = ("⛔ **No trained engine selected.** Choose one (↻ Refresh after a training), or choose the "
                   "baseline.")
        return shown, missing, "", missing, missing
    try:
        described = API.get("/v1/evaluation/target",
                            engine_id=(engine_id or None) if target == TARGET_ENGINE else None)
    except APIError as exc:
        problem = f"⛔ The target cannot be read: {exc}"
        return shown, problem, "", problem, problem
    label = described["label"]
    card = engine_card_ui(described["engine_id"]) if described["engine_id"] else (
        "*The baseline has no result card: the current configuration, a random storyteller per seed.*")
    first = int(seed or 0)
    seeds = f"seeds {first}–{first + int(stories) - 1}"
    if pairwise:
        variants, said = _variants_note(variants_text)
        bench = (f"**A/B on the same {int(stories)} seeds** ({seeds}), each pair judged in both orders:  \n"
                 f"**A** = {label}, as it is · **B** = {label}, with version B's settings ({said}).")
        if variants is not None and not variants:
            bench += "  \n⚠ Version B has no settings: give at least one, or untick A/B."
    else:
        bench = f"Will play and judge **{int(stories)}** stories ({seeds}) of **{label}**."
    calib_first = int(calib_seed or 0)
    calib = (f"Will play and judge **{int(calib_stories)}** stories (seeds {calib_first}–"
             f"{calib_first + int(calib_stories) - 1}) of **{label}**, then fit the fitness to the judge.")
    return shown, evaluation_target_markdown(described), card, bench, calib


@ui
def evaluate_result_ui(done):
    """⚖ Evaluate this result: Evaluate opens with the completed engine as the selected trained engine,
    the target of both the judge benchmark and the fitness calibration (nothing is judged until the user
    starts it). Returns the navigation, the target, the engine picker and the note."""
    engine_id = _result_engine(done)
    choices = evaluation_engines()
    _offered(engine_id, choices, "⚖ Evaluate")
    return (*navigate("evaluate"), gr.Radio(value=TARGET_ENGINE),
            gr.Dropdown(choices=choices, value=engine_id, visible=True),
            f"**Selected trained engine:** the engine just trained, {_label_of(engine_id, choices)}. Press **Play and "
            "judge** (or *Play, judge and fit*) to evaluate it.")


@ui
def export_result_ui(done, platform, name=""):
    """📦 Export this result: Export opens with the completed engine as the one finished run chosen
    (the platform kept; Export enabled only if that platform is available here). Returns the navigation
    and the export form (export_form_ui's outputs)."""
    engine_id = _result_engine(done)
    choices = finished_run_choices()
    _offered(engine_id, choices, "📦 Export")
    form = list(export_form_ui(platform or "python", FINISHED_RUNS, [engine_id], name=name))
    form[3] = gr.Markdown(f"**Chosen:** the engine just trained, {_label_of(engine_id, choices)}, with its own world, "
                          "storyteller, training state and tag vectors.")
    return (*navigate("export"), *form)


def integration_session_text(token):
    """Which game the UNP diagnostics (⚙ Advanced → 🔌 Integration) act on: the one open in ▶ Play."""
    if not token:
        return "**No game is open in ▶ Play.** Start one there; these diagnostics act on it."
    return f"Acting on the game open in **▶ Play**: session `{token}`."


def _job_progress(progress, label="Generation"):
    """A job's progress on the studio's progress bar, counted as the job counts it (its ``label``: a
    generation, stories judged, ...; ``label`` here: when the job does not say)."""
    def report(state):
        benchmark = state.get("validation")
        counted = str(state.get("label") or label)
        progress((state["generation"], state["total"]),
                 desc=f"{counted[:1].upper()}{counted[1:]} {state['generation']}/{state['total']}"
                 + (f": benchmark {benchmark:.0f}" if isinstance(benchmark, (int, float)) else ""))
    return report


# ---- the active-job panel (🧠 Train, ⚖ Evaluate): GET /v1/jobs, GET /v1/jobs/{id} -----------------------

TRAIN_JOBS = {"simulation": "🧠 Run", "continue": "🧠 Training continued", "retrain": "🧠 Retrained"}
EVALUATE_JOBS = {"judge-benchmark": "⚖ Judge benchmark", "calibrate-fitness": "⚖ Fitness calibration"}
JOB_STATES = {"queued": "⏳ queued", "running": "▶ running", "cancel_requested": "⏸ cancellation requested (it stops "
              "at its next safe point)", "cancelled": "🚫 cancelled", "finished": "✅ finished", "failed": "❌ failed"}
ACTIVE_STATES = ("queued", "running", "cancel_requested")
JOB_HINTS = (  # (what the error says, what to do), first match
    (r"judge|vllm|language model|gemma", "The judge (the language model) is not available or failed: check it with "
                                         "*Check the judge again* in ⚖ Evaluate; its settings are `vllm` in ⚙ Advanced → "
                                         "Engine logic."),
    (r"max_simulation_work|work limit|too much work", "Lower the population, generations, stories or steps of the "
                                                       "run, or raise `max_simulation_work` (⚙ Advanced → App & jobs)."),
    (r"seed", "Give a seed (*Set the seed for this run*), or turn reproducibility off."),
    (r"unknown engine|not found|no longer", "The engine is gone (finished runs are kept up to a limit): ↻ Refresh "
                                            "the engine list and choose another."),
    (r"invalid|cannot be played|not valid", "The world or the settings are invalid: correct what the message names "
                                            "(🌍 World → 🔍 Checks lists the world's problems), then submit again."),
)


def _parse_time(text):
    import datetime

    try:
        return datetime.datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


def _duration(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}h {seconds // 60 % 60:02d}m" if seconds >= 3600 else \
        f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def job_failure_hint(error):
    """What to do about a job's error (the error itself is shown as it is)."""
    for pattern, hint in JOB_HINTS:
        if re.search(pattern, str(error or ""), re.I):
            return hint
    return "Correct what the error names, then submit again: the form keeps its settings."


def job_line(job, now=None, mine=None):
    """One job as the panel shows it: its kind, state, description; where it is (queued: how many jobs run
    first; running: its progress, counted as the job counts it, and the scores so far); how long it has
    queued, run or took; a failure's error with what to do."""
    import datetime

    now = now or datetime.datetime.now(datetime.timezone.utc)
    name = {**TRAIN_JOBS, **EVALUATE_JOBS}.get(job.get("kind"), job.get("kind"))
    state = JOB_STATES.get(job.get("status"), job.get("status"))
    created, started, finished = (_parse_time(job.get(k)) for k in ("created_at", "started_at", "finished_at"))
    parts = [f"**{name}** · {state} · {job.get('description') or ''} · `{str(job.get('id'))[:8]}`"
             + (" · **yours (this browser)**" if mine and job.get("id") == mine else "")]
    progress = job.get("progress") or {}
    if job.get("status") == "queued":
        ahead = job.get("queue_ahead")
        parts.append(("first in line: it starts when the job running now ends" if ahead == 1 else
                      f"{ahead} job(s) run before it (one at a time, in order)" if ahead else "about to start")
                     + (f" · waiting {_duration((now - created).total_seconds())}" if created else ""))
    if progress:
        counted = str(progress.get("label") or "step")
        line = f"{counted[:1].upper()}{counted[1:]} {progress.get('generation')}/{progress.get('total')}"
        scores = [f"{label} {progress[key]:.1f}" for key, label in (("best", "best"), ("mean", "mean"),
                                                                    ("validation", "benchmark"))
                  if isinstance(progress.get(key), (int, float))]
        parts.append(line + (" · " + " · ".join(scores) if scores else "")
                     + (" · improved" if progress.get("improved") else ""))
    if job.get("status") in ("running", "cancel_requested") and started:
        parts.append(f"running for {_duration((now - started).total_seconds())}")
    elif job.get("status") in ("finished", "failed", "cancelled") and started and finished:
        parts.append(f"took {_duration((finished - started).total_seconds())}")
    if job.get("status") == "cancelled":
        parts.append(f"**Nothing kept:** {(job.get('partial') or {}).get('note') or 'it was cancelled.'}")
    if job.get("cancel_note"):
        parts.append(job["cancel_note"])
    if job.get("status") == "failed":
        parts.append(f"**Error:** {job.get('error') or 'no message'}  \n**What to do:** {job_failure_hint(job.get('error'))}")
    return "  \n".join(parts)


def jobs_panel(jobs, kinds, now=None, recent=5, yours=None):
    """(the panel's text, whether a job of these kinds is still queued, running or stopping, the finished
    jobs with a result to open: (label, id), the jobs that can be cancelled: (label, id)). ``yours``: the
    id of the job this browser submitted (marked; no other job is followed by this browser)."""
    mine = [job for job in jobs if job.get("kind") in kinds]
    active = [job for job in mine if job.get("status") in ACTIVE_STATES]
    done = [job for job in mine if job.get("status") in ("finished", "failed", "cancelled")][:recent]
    others = [job for job in jobs if job.get("kind") not in kinds and job.get("status") in ACTIVE_STATES]
    lines = []
    if active:
        lines += ["#### Now"] + [f"- {job_line(job, now, yours)}" for job in active]
    if others:
        lines.append(f"*{len(others)} other job(s) of the studio queued or running (one runs at a time): "
                     + ", ".join(sorted({str(job.get('kind')) for job in others})) + ".*")
    if done:
        lines += ["#### Recent"] + [f"- {job_line(job, now, yours)}" for job in done]
    if not lines:
        lines = ["*No job yet: a run (or an evaluation) shows here while it is queued and runs, then its result.*"]
    names = {**TRAIN_JOBS, **EVALUATE_JOBS}
    results = [(f"{names.get(job['kind'], job['kind'])} · {job.get('description') or ''} · "
                f"{str(job.get('finished_at') or '')[11:16]} UTC · {job['id'][:8]}", job["id"])
               for job in done if job.get("status") == "finished" and job.get("has_result", True)]
    cancellable = [(f"{names.get(job['kind'], job['kind'])} · {JOB_STATES[job['status']].split(' (')[0]} · "
                    f"{job.get('description') or ''} · {job['id'][:8]}", job["id"])
                   for job in active if job.get("cancellable", job.get("status") in ("queued", "running"))]
    return "\n".join(lines), bool(active), results, cancellable


JOBS_POLL_ACTIVE, JOBS_POLL_IDLE = 2.0, 10.0  # seconds between the job panel's refreshes: a job of its own active, or not


def jobs_poll(active):
    """The panel's timer: never off, so a refresh computed before a job existed (the page still loading
    when the job was submitted) slows the next one down instead of stopping the polling; quick while one
    of its jobs is queued, running or stopping."""
    return gr.Timer(JOBS_POLL_ACTIVE if active else JOBS_POLL_IDLE, active=True)


@ui
def jobs_panel_ui(kinds_key, chosen=None, yours=None, to_cancel=None):
    """The panel's refresh (on its timer): its text, the timer (quick while one of its jobs is queued,
    running or stopping, slow otherwise: jobs_poll), the finished jobs to open and the jobs that can be
    cancelled (the chosen ones kept; this browser's job first to cancel). ``yours``: what this browser
    keeps of its job."""
    kinds = TRAIN_JOBS if kinds_key == "train" else EVALUATE_JOBS
    mine = (yours or {}).get("id") if isinstance(yours, dict) else yours
    try:
        jobs = API.get("/v1/jobs")["jobs"]
    except APIError as exc:
        return f"⚠ The jobs could not be read: {exc}", jobs_poll(False), gr.skip(), gr.skip()
    text, active, results, cancellable = jobs_panel(jobs, kinds, yours=mine)
    ids = [value for _, value in results]
    cancel_ids = [value for _, value in cancellable]
    pick = to_cancel if to_cancel in cancel_ids else (mine if mine in cancel_ids else (cancel_ids[0] if cancel_ids else None))
    return (text, jobs_poll(active),
            gr.Dropdown(choices=results, value=chosen if chosen in ids else (ids[0] if ids else None)),
            gr.Dropdown(choices=cancellable, value=pick))


@ui
def open_train_job_ui(job_id):
    """A finished training job's result on Train's result card, with its Play/Evaluate/Export actions (its
    exact engine). Returns (the completed engine, the card, its three actions)."""
    if not job_id:
        raise gr.Error("Choose a finished job.")
    job = API.get(_path("/v1/jobs", job_id))
    if job.get("status") != "finished" or not job.get("result"):
        raise gr.Error(f"That job has no result ({job.get('status')}): {job.get('error') or 'it has not finished'}.")
    kind = {"simulation": "run", "continue": "continue", "retrain": "retrain"}.get(job["kind"], "run")
    done = completed_engine(job["result"], kind)
    return (done, *completed_view_ui(done))


@ui
def open_evaluate_job_ui(job_id):
    """A finished evaluation job's result where it belongs: a judge benchmark's summary and table, or a
    calibration's summary and suggested weights. Returns (benchmark summary, its table, calibration
    summary, the term weights)."""
    if not job_id:
        raise gr.Error("Choose a finished job.")
    job = API.get(_path("/v1/jobs", job_id))
    if job.get("status") != "finished" or not job.get("result"):
        raise gr.Error(f"That job has no result ({job.get('status')}): {job.get('error') or 'it has not finished'}.")
    if job["kind"] == "judge-benchmark":
        return (*benchmark_view(job["result"]), gr.skip(), gr.skip())
    return (gr.skip(), gr.skip(), *calibration_view(job["result"]))


# ---- a job kept in this browser (BrowserState): submitted, then followed; after a reload, followed again ----

JOB_GONE = ("Your {what} job `{id}` is no longer known to the API: the API restarted (it keeps its jobs in memory "
            "only: a restart loses them, queued, running or finished) or the job was dropped (the latest "
            "{keep} are kept). {after}")


def _job_ref(job, kind, **extra):
    """What this browser keeps of a job it submitted: its id and what its result needs to be shown."""
    return {"id": job["id"], "kind": kind, "reused": bool(job.get("reused")), **extra}


def _job_outcome(ref, what, after_gone, exc):
    """The message of a job that ended without a result: cancelled, failed, or no longer known."""
    if isinstance(exc, JobCancelledError):
        partial = exc.job.get("partial") or {}
        return f"### 🚫 Cancelled\nYour {what} job `{ref['id'][:8]}` was cancelled. {partial.get('note') or ''}"
    if exc.status == 404:
        return "### ⚠ Job lost\n" + JOB_GONE.format(what=what, id=ref["id"][:8], keep=100, after=after_gone)
    return (f"### ❌ Failed\nYour {what} job `{ref['id'][:8]}` failed: {exc}  \n**What to do:** "
            f"{job_failure_hint(str(exc))}")


@ui
def submit_run_ui(use_population, population, use_generations, generations, use_episodes, episodes, use_steps, steps,
                  reproducibility, use_seed, seed, warm_start, shown=None):
    """🧠 Evolve storytellers: the job is submitted and kept in this browser (its result follows:
    follow_training_ui). An identical run still queued or running is followed instead of a second one."""
    controls = (use_population, population, use_generations, generations, use_episodes, episodes, use_steps, steps,
                reproducibility, use_seed, seed, warm_start)
    return _job_ref(API.post("/v1/jobs/simulations", {**_run_overrides(*controls), **TRAIN_ONLY}), "run", shown=shown)


@ui
def submit_continue_ui(engine_id, more_generations):
    if not engine_id:
        raise gr.Error("Choose a recorded engine first.")
    job = API.post(_path("/v1/jobs/engines", engine_id, "continue"), {"generations": int(more_generations), **TRAIN_ONLY})
    return _job_ref(job, "continue")


@ui
def submit_retrain_ui(engine_id, more_generations, *controls_and_shown):
    controls, shown = controls_and_shown[:len(RUN_CONTROLS)], (controls_and_shown[len(RUN_CONTROLS):] or [None])[0]
    if not engine_id:
        raise gr.Error("Choose a recorded engine first.")
    job = API.post(_path("/v1/jobs/engines", engine_id, "retrain"),
                   {**_retrain_overrides(more_generations, *controls), **TRAIN_ONLY})
    return _job_ref(job, "retrain", shown=shown)


TRAINING_FOLLOW_OUTPUTS = 13  # the report, 2 files, the curve, the traits, the memory, the run, its generations,
                              # the completed engine, its card and its 3 actions


@ui
def follow_training_ui(ref, progress=gr.Progress()):
    """The training this browser submitted (kept across a reload or a lost connection), followed through
    GET /v1/jobs/{id} until it ends (never a new job): its result shown as a run, a continued or a retrained
    engine; or why there is none (cancelled, failed, no longer known to the API). Returns the training's
    outputs, then what this browser keeps (nothing once it has ended)."""
    skip = gr.skip()
    if not ref or not ref.get("id"):
        return (*[skip] * TRAINING_FOLLOW_OUTPUTS, skip)
    try:
        result = API.wait({"id": ref["id"]}, _job_progress(progress))
    except APIError as exc:
        message = _job_outcome(ref, "training", "If it had finished, its engine is in 🧠 Train → 🏷 Finished engines; "
                               "otherwise submit it again.", exc)
        return (message, *[skip] * (TRAINING_FOLLOW_OUTPUTS - 1), None)
    report, *outputs = _training_outputs(result)
    kind = ref.get("kind") or "run"
    check = executed_markdown(result.get("training"), ref.get("shown")) if kind in ("run", "retrain") else ""
    return (report, *outputs[:4], learning_memory_text(), *outputs[4:], *_training_done(result, kind, check), None)


@ui
def submit_benchmark_ui(stories, seed, engine_id, pairwise, variants_text, workers, target=None):
    """Play and judge: the job is submitted and kept in this browser (follow_evaluation_ui)."""
    engine_id = evaluation_engine_id(target, engine_id) if target else (engine_id or None)
    variants = _variants(variants_text) if pairwise else {}
    if pairwise and not variants:
        raise gr.Error("A/B needs version B's settings (at least one dotted.path: value), or untick A/B.")
    job = API.post("/v1/jobs/judge-benchmark", {
        "stories": int(stories), "seed": int(seed or 0), "engine_id": engine_id, "pairwise": bool(pairwise),
        "variants": variants, "workers": int(workers)})
    return _job_ref(job, "judge-benchmark", engine_id=engine_id)


@ui
def submit_calibration_ui(stories, seed, engine_id, apply, target=None):
    engine_id = evaluation_engine_id(target, engine_id) if target else (engine_id or None)
    job = API.post("/v1/jobs/calibrate-fitness", {"stories": int(stories), "seed": int(seed or 0),
                                                  "engine_id": engine_id, "apply": bool(apply)})
    return _job_ref(job, "calibrate-fitness", engine_id=engine_id)


@ui
def follow_evaluation_ui(ref, progress=gr.Progress()):
    """The evaluation this browser submitted, followed until it ends (never a new job): a benchmark's
    summary and table, or a calibration's summary and weights; or why there is none. Returns (benchmark
    summary, its table, calibration summary, the weights, what this browser keeps)."""
    skip = gr.skip()
    if not ref or not ref.get("id"):
        return skip, skip, skip, skip, skip
    benchmark = ref.get("kind") == "judge-benchmark"
    try:
        result = API.wait({"id": ref["id"]}, _job_progress(progress, "Judged"))
    except APIError as exc:
        message = _job_outcome(ref, "evaluation", "Submit it again.", exc)
        return (message, skip, skip, skip, None) if benchmark else (skip, skip, message, skip, None)
    if benchmark:
        return (*benchmark_view(result, ref.get("engine_id")), skip, skip, None)
    return (skip, skip, *calibration_view(result, ref.get("engine_id")), None)


@ui
def cancel_job_ui(job_id):
    """🚫 Cancel: a queued job never starts; a running one stops at its next safe point."""
    if not job_id:
        raise gr.Error("Choose a queued or running job to cancel.")
    result = API.post(_path("/v1/jobs", job_id, "cancel"))
    return f"{result['message']} (`{job_id[:8]}`)"


def jobs_panel_block(kinds_key, title):
    """The active-job panel (its components), polled by a timer while a job is queued or running."""
    with gr.Accordion(title, open=True, elem_id=f"{kinds_key}-jobs"):
        text = gr.Markdown(elem_id=f"{kinds_key}-jobs-text")
        with gr.Row(equal_height=True):
            # allow_custom_value: a job chosen before the list changed (it ended, or left the latest kept) is
            # not refused by the dropdown before the panel's refresh replaces it.
            finished = gr.Dropdown([], label="A finished job", scale=4, allow_custom_value=True,
                                   elem_id=f"{kinds_key}-jobs-finished")
            open_button = gr.Button("Open its result", scale=1, elem_id=f"{kinds_key}-jobs-open")
            refresh = gr.Button("↻", scale=0, min_width=48, elem_id=f"{kinds_key}-jobs-refresh")
        with gr.Row(equal_height=True):
            cancellable = gr.Dropdown([], label="A queued or running job", scale=4, allow_custom_value=True,
                                      elem_id=f"{kinds_key}-jobs-cancellable")
            cancel = gr.Button("🚫 Cancel it", variant="stop", scale=1, elem_id=f"{kinds_key}-jobs-cancel")
        cancel_note = gr.Markdown(elem_id=f"{kinds_key}-jobs-cancel-note")
        timer = gr.Timer(JOBS_POLL_IDLE)
    return {"text": text, "finished": finished, "open": open_button, "refresh": refresh, "timer": timer,
            "cancellable": cancellable, "cancel": cancel, "cancel_note": cancel_note}


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
            "info": f"Stories end by themselves after {work['story_length']} turns"
                    + (f" (plus up to {work['fast_travel_turns']} of fast travel)" if work.get("fast_travel_turns") else "")
                    + "; steps above it are not played.",
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


def _quests_text(quests, profile):
    """(choices of the quest picker, Markdown overview): the quests open to the profile now can be
    chosen; the others are listed, struck through."""
    choices = [(ANY_QUEST, "")] + [(q["title"], q["key"]) for q in quests if q["available"]]
    lines = [f"**Quests in this world** (for profile `{profile or 'default'}`):"]
    for quest in quests:
        stages = " → ".join(quest["stages"])
        closed = "achieved in this save game" if quest.get("achieved") else "not open now"
        lines.append(f"- {'**' + quest['title'] + '**' if quest['available'] else '~~' + quest['title'] + f'~~ ({closed})'}"
                     + (f" — {quest['description']}" if quest["description"] else "")
                     + (f" *Stages:* {stages}" if stages else ""))
    return choices, "\n".join(lines)


def quest_overview(profile, engine_id=None):
    """(choices of the quest picker, Markdown overview of the world's quests) for this profile, in the
    world of ``engine_id`` (its saved configuration) or, without one, the active world."""
    data = API.get("/v1/missions/playable", profile=profile or None, engine_id=engine_id or None)
    return _quests_text(data["missions"], profile)


def quest_world_markdown(world, label=None):
    """Beside the quest picker: which engine's saved world the quests come from, and its identity."""
    summary = world.get("summary") or {}
    counts = " · ".join(f"{summary[key]} {name}" for key, name in (
        ("locations", "places"), ("npcs", "people"), ("items", "items"), ("events", "events"), ("missions", "quests"))
        if key in summary)
    same = ("✓ the same world as the active configuration" if world.get("current") else
            "⚠ not the active world: a new game with this engine makes it active")
    if world.get("source") == "engine":
        return (f"**Quests from the saved world of** {label or ''} (engine `{world['engine_id'][:8]}`): {counts} · world "
                f"`{world['digest'][:12]}` · {same}.")
    return (f"**Quests from the active world** (no trained AI chosen): {counts} · world `{world['digest'][:12]}`.")


def quest_name(key, titles=None):
    """A quest's title as the page last listed it (``titles``: {key: title}, play_quests_titled_ui), with
    its key; just the key when it was never listed."""
    title = (titles or {}).get(key)
    return f"**{title}** (`{key}`)" if title and title != key else f"**{key}**"


@ui
def play_quests_titled_ui(engine_id, profile, chosen="", titles=None):
    """play_quests_ui, remembering the titles of the quests listed ({key: title}, merged with those listed
    before): a quest of another engine's world, cleared later, is then named by its title, which the new
    world does not know. Returns play_quests_ui's outputs and the titles."""
    picker, overview, note, listed = _play_quests(engine_id, profile, chosen, titles)
    return picker, overview, note, {**(titles or {}), **listed}


@ui
def play_quests_ui(engine_id, profile, chosen="", titles=None):
    """The quests a new game may have, for the trained AI and the profile chosen: read from that engine's
    saved world (the active configuration is not changed) with the profile's save game. A quest chosen
    before that is not offered now (not in that world, or not open to the profile) is cleared, saying
    why (by its title: from this world, else from ``titles``, else its key). Returns (the quest picker,
    the quests' overview, the note beside the picker)."""
    return _play_quests(engine_id, profile, chosen, titles)[:3]


def _play_quests(engine_id, profile, chosen, titles):
    try:
        data = API.get("/v1/missions/playable", profile=profile or None, engine_id=engine_id or None)
    except APIError as exc:
        return (gr.Dropdown(choices=[(ANY_QUEST, "")], value=""), "",
                f"⚠ The quests of engine `{(engine_id or '')[:8]}` could not be read ({exc}): the story will draw one.",
                {})
    choices, text = _quests_text(data["missions"], profile)
    listed = {q["key"]: q.get("title") or q["key"] for q in data["missions"]}
    label = None
    if engine_id:
        label = next((name for name, value in finished_run_choices() if value == engine_id), None)
        label = label.rsplit(" · ", 1)[0] if label else None
    note = quest_world_markdown(data["world"], f"**{label}**" if label else None)
    offered = [value for _, value in choices]
    if chosen and chosen not in offered:
        quest = next((q for q in data["missions"] if q["key"] == chosen), None)
        why = ("is not in this engine's world" if quest is None else
               f"was already achieved in profile `{profile or 'default'}`'s save game" if quest.get("achieved") else
               f"is not open to profile `{profile or 'default'}` now")
        name = quest_name(chosen, {**(titles or {}), **listed})
        note = (f"⚠ The quest chosen before, {name}, {why}: it was cleared (the story draws one, or choose "
                "another).  \n" + note)
        chosen = ""
    return gr.Dropdown(choices=choices, value=chosen or ""), text, note, listed


@ui
def playable_quests_ui(profile, engine_id=None):
    choices, text = quest_overview(profile, engine_id)
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
GENERATION_SOURCE = "A generation of this run"
EXPORT_PLATFORM_NAMES = {"python": "Python", "windows-x64": "Windows (x64)", "linux-x64": "Linux (x64)",
                         "macos-arm64": "macOS (Apple silicon)", "macos-x64": "macOS (Intel)", "unity": "Unity package"}
PYTHON_PLATFORM = {"platform": "python", "available": True, "description": "Python 3.10+ with pyyaml and numpy"}
# What an ANE AI (a standalone program or the Unity package) is made from: a finished run (its engine).
ANE_AI_PLATFORMS = ("windows-x64", "linux-x64", "macos-arm64", "macos-x64", "unity")
NO_COMPLETE_ENGINE = ("**No finished run yet.** An export is made from a finished run (its engine: world, "
                      "storyteller, training state): run a training in 🧠 Train, or load an engine in ▶ Play, then "
                      "export it here.")


def finished_run_choices():
    """(label, engine id) of the finished runs (complete engines) to export, newest first
    (GET /v1/exports/engines)."""
    try:
        engines = API.get("/v1/exports/engines")["engines"]
    except APIError as exc:
        LOG.warning("The finished runs could not be read: %s", exc)
        return []

    def label(e):  # the API's label (its name, or what it is, when and how it scored, with its short id)
        if e.get("label"):
            return e["label"]
        parts = [(e.get("created_at") or "")[:16].replace("T", " "), "loaded engine" if e.get("loaded") else "run"]
        if e.get("generations"):
            parts.append(f"{e['generations']} generation(s)")
        if isinstance(e.get("score"), (int, float)):
            parts.append(f"fitness {e['score']:.1f}")
        return " · ".join(parts + [e["engine_id"][:8]])

    return [(label(e), e["engine_id"]) for e in engines]


def export_platform_list():
    """What an export can run on here (GET /v1/exports/platforms): [{platform, available, reason?,
    description?, code_version?, built_at?, includes?}]; Python alone when the API cannot be asked."""
    try:
        return API.get("/v1/exports/platforms")["platforms"]
    except APIError as exc:
        LOG.warning("The export platforms could not be read: %s", exc)
        return [PYTHON_PLATFORM]


def _platform_name(platform):
    return EXPORT_PLATFORM_NAMES.get(platform, f"Standalone {platform}")


def export_platform_choices(listed=None):
    """(label, platform) of what an export can run on, each saying whether it is available here."""
    listed = listed if listed is not None else export_platform_list()
    return [(_platform_name(p["platform"]) + ("" if p["available"] else " (not available here)"), p["platform"])
            for p in listed]


def _platform_entry(platform, listed):
    return next((p for p in listed if p["platform"] == platform), None)


def export_platform_note(platform, listed=None):
    """Step 2's note: what the chosen platform is and, when it is not available here, why (the API's
    own reason) and what to do."""
    listed = listed if listed is not None else export_platform_list()
    entry = _platform_entry(platform, listed)
    name = _platform_name(platform)
    if entry is None:
        return f"⛔ **{name}** is not offered by this API. Choose another system."
    if not entry["available"]:
        others = [_platform_name(p["platform"]) for p in listed if p["available"]]
        return (f"⛔ **{name} is not available here:** {entry.get('reason') or 'no reason given'}. "
                f"Available now: {', '.join(others) or 'none'}.")
    if platform == "python":
        return ("**Python**: the engine as Python code (3.10+ with pyyaml and numpy), for developers: play it in a "
                "terminal, serve it over HTTP, embed it, keep training it.")
    if platform == "unity":
        return (f"**Unity package**: {entry.get('description') or 'the C# library and the standalone runtimes'}. "
                "Import it into a Unity project; the library starts and stops the ANE AI for you.")
    built = (entry.get("built_at") or "")[:10]
    return (f"**{name}**: the ANE AI as a standalone program, no Python needed (runtime "
            f"`{entry.get('code_version')}`{', built ' + built if built else ''}). A UNP launches it, drives it "
            "over HTTP and stops it.")


def _chosen_ids(chosen):
    return [chosen] if isinstance(chosen, str) and chosen else list(chosen or [])


def export_problems(platform, source_label, chosen=None, generation=None, run_id=None, token=None, listed=None,
                    choices=None):
    """Why this export cannot be made as chosen, each with how to resolve it: [(problem, fix)]. Empty:
    it can. Checks the platform (offered and available here), the combination (the ANE AI is made from
    finished runs only) and that the chosen source exists (a finished run chosen, a run and one of its
    generations, a remembered storyteller, a game open in ▶ Play)."""
    listed = listed if listed is not None else export_platform_list()
    found = []
    entry = _platform_entry(platform, listed)
    if entry is None or not entry["available"]:
        reason = (entry or {}).get("reason") or "this API does not offer it"
        found.append((f"{_platform_name(platform)} is not available here ({reason}).",
                      "Choose Python, or another system marked available in step 2."))
    if platform in ANE_AI_PLATFORMS and source_label != FINISHED_RUNS:
        found.append((f"The ANE AI ({_platform_name(platform)}) is made from a complete engine, and *{source_label}* "
                      "is a storyteller without one (no world, training state or tag vectors of its own).",
                      "Choose *finished runs* in step 1, or *Python* in step 2."))
    if source_label == FINISHED_RUNS:
        choices = choices if choices is not None else finished_run_choices()
        offered = {value for _, value in choices}
        ids = _chosen_ids(chosen)
        if not choices:
            found.append(("There is no finished run yet.", "Run a training in 🧠 Train, or load an engine in ▶ Play."))
        elif not ids:
            found.append(("No finished run is chosen.", "Choose one or more finished runs in step 1."))
        elif any(engine not in offered for engine in ids):
            found.append(("A chosen finished run is no longer offered (finished runs are kept up to a limit).",
                          "Press ↻ Refresh in step 1 and choose again."))
    elif source_label == GENERATION_SOURCE:
        if not run_id:
            found.append(("No run was trained in this session: a generation's champion comes from the run 🧠 Train "
                          "has just made.", "Run a training in 🧠 Train, or export *finished runs*."))
        elif generation in (None, ""):
            found.append(("No generation is chosen.", "Choose a generation in step 1."))
    elif source_label == "This game's storyteller" and not token:
        found.append(("No game is open in ▶ Play.", "Start or continue a game in ▶ Play, then come back."))
    elif source_label == "Best remembered storyteller":
        try:
            remembered = API.get("/v1/learning").get("size", 0)
        except APIError:
            remembered = None
        if remembered == 0:
            found.append(("The AI remembers no storyteller yet.",
                          "Run a training with warm start in 🧠 Train, or export *finished runs*."))
    return found


def export_options_note(platform, source_label, chosen=None, name=""):
    """Step 3's note: what the options do for this export."""
    lines = ["- **Learning memory**: the storytellers remembered from training, so the export can keep training "
             "offline" + (" (`python run.py evolve`)." if platform == "python" else ".")]
    ids = _chosen_ids(chosen)
    if source_label == FINISHED_RUNS and len(ids) > 1:
        lines.append(f"- **Name**: {len(ids)} downloads, one per finished run: "
                     + (f"`{name}-<engine id>`." if name else "each named after its engine (its name, if it has one)."))
    else:
        lines.append("- **Name**: the download's folder name"
                     + (" (empty: the engine's name, if it has one, else a dated name)." if not name else "."))
    return "\n".join(lines)


def export_check_markdown(problems, platform, source_label, chosen=None):
    """Step 4's summary: what Export will make, or why it cannot, with how to resolve it."""
    if problems:
        return "⛔ **Export is not possible as chosen:**\n" + "\n".join(
            f"- {problem} **To resolve:** {fix}" for problem, fix in problems)
    count = len(_chosen_ids(chosen)) if source_label == FINISHED_RUNS else 1
    what = {FINISHED_RUNS: "finished run", GENERATION_SOURCE: "generation's champion",
            "Best remembered storyteller": "remembered storyteller",
            "This game's storyteller": "game's storyteller"}.get(source_label, "storyteller")
    return (f"✅ **Ready:** {count} download{'s' if count != 1 else ''} for {_platform_name(platform)}, from the "
            f"{what}{'s' if count != 1 else ''} chosen.")


def export_check_ui(platform, source_label, chosen=None, generation=None, run_id=None, token=None, name=""):
    """After a choice in steps 1 or 3: Export enabled only for a valid combination, the options' note and
    the summary. Returns (the Export button, the options' note, the summary)."""
    problems = export_problems(platform, source_label, chosen, generation, run_id, token)
    return (gr.Button(interactive=not problems), export_options_note(platform, source_label, chosen, name),
            export_check_markdown(problems, platform, source_label, chosen))


def export_form_ui(platform, source_label=FINISHED_RUNS, chosen=None, generation=None, run_id=None, token=None,
                   name=""):
    """The whole Export form for a platform and a source: what to export (finished runs, the newest
    chosen unless others are; several can be; a generation, the remembered storyteller or this game's
    storyteller), what the platform is or why it is unavailable, the options, and Export enabled only
    for a valid combination, with why not and how to resolve it. Returns (the source, the generation,
    the finished runs, step 1's note, the Export button, step 2's note, step 3's note, the summary)."""
    listed = export_platform_list()
    source_label = source_label if source_label in EXPORT_SOURCES else FINISHED_RUNS
    choices = finished_run_choices()
    ids = [value for _, value in choices]
    kept = [engine for engine in _chosen_ids(chosen) if engine in ids] or ids[:1]
    from_runs = source_label == FINISHED_RUNS
    if from_runs:
        note = (NO_COMPLETE_ENGINE if not choices else
                f"{len(kept)} finished run(s) chosen: each is exported with its own world, storyteller, training "
                "state and tag vectors.")
    else:
        note = {GENERATION_SOURCE: "The champion of one generation of the run 🧠 Train made in this session.",
                "Best remembered storyteller": "The best storyteller the AI remembers, with the active world.",
                "This game's storyteller": "The storyteller of the game open in ▶ Play, with its world."}[source_label] \
            + " (Python only.)"
    problems = export_problems(platform, source_label, kept, generation, run_id, token, listed, choices)
    return (gr.Radio(value=source_label), gr.Dropdown(visible=source_label == GENERATION_SOURCE),
            gr.Dropdown(choices=choices, value=kept, visible=from_runs), gr.Markdown(note),
            gr.Button(interactive=not problems), export_platform_note(platform, listed),
            export_options_note(platform, source_label, kept, name),
            export_check_markdown(problems, platform, source_label, kept))


def export_refresh_ui(platform, source_label, chosen=None, generation=None, run_id=None, token=None, name=""):
    """↻ Refresh: the platforms (availability may have changed) and the whole form again."""
    listed = export_platform_list()
    return (gr.Radio(choices=export_platform_choices(listed), value=platform),
            *export_form_ui(platform, source_label, chosen, generation, run_id, token, name))


def _export_status(result):
    score = f"{result['score']:.1f}" if isinstance(result["score"], (int, float)) else "not benchmarked"
    if result.get("engine_id"):
        score += f", engine `{result['engine_id'][:8]}`"
    return (f"Exported **{result['name']}**: {result['source']} (fitness {score}, the training objective). "
            f"World: {result['world']['locations']} locations, {result['world']['npcs']} NPCs, "
            f"{result['world']['missions']} missions.")


def export_usage_markdown(platform):
    """Step 5: how to use what was downloaded, for its platform (each download's README says more)."""
    if platform == "python":
        return ("#### How to use it (Python)\n"
                "1. Unzip it, then `pip install -r requirements.txt` (Python 3.10+).\n"
                "2. `python run.py info` says what is inside; `python run.py play` plays in the terminal.\n"
                "3. `python run.py serve` is a JSON server on `http://127.0.0.1:8765` for any game engine (the token "
                "is in its ready line); `python run.py evolve` keeps training offline.\n"
                "4. In Python: `from narrative_runtime import NarrativeEngine`, then "
                "`NarrativeEngine(\"path/to/folder\").new_game(...)`.\n\nIts README.md has the details.")
    if platform == "unity":
        return ("#### How to use it (Unity)\n"
                "1. In Unity: *Assets → Import Package → Custom Package…*, choose the `.unitypackage`.\n"
                "2. *Tools → ANE AI → Add the ANE AI to the scene*: the library starts the runtime for the player's "
                "system, drives it and stops it with the game.\n\n`Assets/ANE-AI/README.md` has the details.")
    executable = "ane-ai.exe" if platform.startswith("windows") else "./ane-ai"
    notes = {"macos-arm64": "macOS quarantines an unsigned program downloaded with a browser: run "
                            "`xattr -dr com.apple.quarantine <folder>` once, or let the UNP download it.",
             "macos-x64": "macOS quarantines an unsigned program downloaded with a browser: run "
                          "`xattr -dr com.apple.quarantine <folder>` once, or let the UNP download it.",
             "windows-x64": "Windows may warn about an unsigned program (SmartScreen); start it with "
                            "`CREATE_NO_WINDOW` in a Job Object with `KILL_ON_JOB_CLOSE`."}
    return (f"#### How to use it ({_platform_name(platform)}, no Python needed)\n"
            f"1. Unzip it on {_platform_name(platform)}.\n"
            f"2. The UNP starts `{executable} serve --port 0 --ready-file ready.json --parent-pid <UNP pid> "
            "--exit-on-stdin-eof` (token in `ANE_RUNTIME_TOKEN`) and reads the ready line for its URL.\n"
            "3. It sends requests with `Authorization: Bearer <token>`: `POST /games`, `POST /games/<id>/choose`, "
            "`POST /games/<id>/save`, …\n"
            "4. `POST /shutdown {\"save\": true}` stops it (sessions saved).\n"
            + (f"\n{notes[platform]}\n" if platform in notes else "")
            + "\nIts README.md and docs/UNP.md have the whole protocol.")


@ui
def export_engine_ui(source_label, run_id, generation, token, include_memory, name, platform="python", engine_ids=None):
    """Export: from the chosen finished runs (one download each), or, for the Python version, from a
    generation's champion, the remembered storyteller or this game's storyteller. An invalid combination
    is refused before anything is asked (with how to resolve it). Returns ([downloaded files], status)."""
    platform = platform or "python"
    source_label = FINISHED_RUNS if platform in ANE_AI_PLATFORMS and source_label not in EXPORT_SOURCES else source_label
    problems = export_problems(platform, source_label, engine_ids, generation, run_id, token)
    if problems:
        raise gr.Error("Nothing was exported: " + " ".join(f"{problem} {fix}" for problem, fix in problems))
    if source_label == FINISHED_RUNS:
        engine_ids = _chosen_ids(engine_ids)
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
        "generation": int(generation) if source_label == GENERATION_SOURCE and generation not in (None, "") else None,
        "session_id": token or None, "include_memory": bool(include_memory), "name": name or None,
        "platform": platform,
    })
    return [API.download(result["download_url"])], _export_status(result)


# ---- ▶ Play: the world of a finished run, as a game ---------------------------------------------

CHOICE_BUTTONS = 12  # buttons for the offered choices (player_options.limit, 6 by default; beyond: said in the HUD)
PLAY_STATS = [("Story default", "story default"), ("Random", "random"), ("Balanced", "balanced"),
              ("My own points", "defined")]
# What the game screen shows, in the order _play_screen returns it (play_part picks one by name).
PLAY_PARTS = ("ending", "hud", "place", "turn", *(f"choice {i + 1}" for i in range(CHOICE_BUTTONS)), "quest", "you",
              "people", "map", "actions", "journey")


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
        marks = "".join(mark for flag, mark in ((person.get("quest"), " ★"), (person.get("side_quests"), " ❗"),
                                                (person.get("hostile"), " ⚠"), (person.get("ally"), " ♥")) if flag)
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
    words = {"offered": "❗ side quest offered: {title}", "declined": "▫️ turned down: {title}",
             "lapsed": "▫️ let go: {title}", "started": "🧭 new side quest: {title}", "stage": "🧭 {title}: {stage} done",
             "ready": "📦 {title}: done, take it back", "hurry": "⏳ hurry: {title}",
             "completed": "✅ side quest done: {title}", "failed": "✗ side quest failed: {title}"}
    chips += [words[event["what"]].format(**event) for event in entry.get("side_quests") or [] if event["what"] in words]
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
    log, the player, the people and the map, what each button sends (the choice's id and the state it was
    offered in; its name for an API without ids), the journey."""
    actions = list(state.get("actions") or [])
    checks = {item["name"]: item.get("check") for item in state.get("choices") or []}
    # What each button sends: the choice's stable id with the state it was offered in (a choice made
    # from older options, e.g. in another tab, is refused instead of taking another), else its name.
    unclaimed = list(state.get("choices") or [])
    requests = []
    for name in actions:
        choice = next((item for item in unclaimed if item.get("name") == name and item.get("id")), None)
        if choice is not None:
            unclaimed.remove(choice)
            requests.append({"action_id": choice["id"], "state_version": state.get("state_version"), "action": name})
        else:
            requests.append({"action": name})
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
    hud = _hud(state)
    if len(actions) > CHOICE_BUTTONS:  # never hidden silently: a raised player_options.limit
        hud += (f"\n\n⚠ {len(actions) - CHOICE_BUTTONS} more choice(s) than this screen shows ({CHOICE_BUTTONS}): "
                "lower `player_options.limit` (⚙ Advanced → Engine logic).")
    return (gr.Markdown(value=ending, visible=bool(ending)), gr.Markdown(value=hud, visible=True),
            place_markdown(state), gr.Markdown(value=card, visible=bool(card)),
            *buttons, "\n\n".join(part for part in (quest_markdown(state.get("quest"), journal),
                                                   side_quests_markdown(state.get("side_quests"))) if part),
            you_markdown(state), people_markdown(state),
            discovered_map_svg(state.get("map")), requests, journey_markdown(journal))


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


def restore_game_ui(token, size):
    """After a reload: the game this browser was playing (GET /v1/sessions/{id}): the API restores it from
    its last save after a restart; when it is gone (removed, or never saved) the kept id is dropped. Returns (the session, the ``size`` outputs of the
    game screen and the story, the id kept)."""
    if not token:
        return ("", *[gr.skip()] * size, "")
    try:
        state = API.get(_path("/v1/sessions", token))
    except APIError as exc:
        if exc.status == 404:  # the API no longer has it
            return ("", *[gr.skip()] * size, "")
        LOG.warning("Could not reopen the game %s: %s", token, exc)  # kept, to try again on the next load
        return ("", *[gr.skip()] * size, gr.skip())
    return (token, *_play_update(state, token), token)


@ui
def play_choose_ui(token, actions, index):
    """The player takes the choice of button ``index``."""
    if not token:
        raise gr.Error("Start a new game first (choose a trained AI and press New game).")
    if index >= len(actions or []):
        raise gr.Error("That choice is no longer there.")
    choice = actions[index] if isinstance(actions[index], dict) else {"action": actions[index]}
    try:
        state = API.post(_path("/v1/sessions", token, "actions"), choice)
    except APIError as exc:
        if exc.status != 409:
            raise
        # The game moved on since these choices were shown (another tab): nothing was done; the
        # current choices are shown instead.
        gr.Warning(f"{exc} The current choices are shown now.")
        state = API.get(_path("/v1/sessions", token))
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


# ---- saved games: every game is saved as it is played (GET /v1/sessions, /v1/saves) ---------------

RESTORED_TEXT = {"live": "Continued where you left it.", "exact": "Continued exactly where it was saved."}


def _when(stamp):
    return (stamp or "")[:16].replace("T", " ")


def saved_games_choices(profile=None):
    """[(label, value)] of the games to open: games in progress (``session:<id>``, each saved after every
    change) and save slots (``save:<id>``), of ``profile`` (all profiles when none). None when saved
    games cannot be listed (off on the server, or it cannot be reached)."""
    try:
        games = API.get("/v1/sessions", profile=profile or None, status="active")["sessions"]
        saves = API.get("/v1/saves", profile=profile or None)["saves"]
    except APIError as exc:
        LOG.info("Saved games are not available: %s", exc)
        return None
    choices = [(f"▶ In progress · {g['profile']} · {g['location']} · turn {g['turn']} · {_when(g['updated_at'])}",
                f"session:{g['session_id']}") for g in games]
    choices += [(f"💾 {s['slot']} · {s['profile']} · {s['location']} · turn {s['turn']} · {_when(s['updated_at'])}",
                 f"save:{s['save_id']}") for s in saves]
    return choices


def saved_games_ui(profile=None, selected=None):
    """The saved-games list of the chosen profile, keeping the selection when it is still there."""
    choices = saved_games_choices(profile)
    if choices is None:
        return gr.Dropdown(choices=[], value=None, info="Saved games are off on this server.")
    values = [value for _, value in choices]
    return gr.Dropdown(choices=choices, value=selected if selected in values else (values[0] if values else None),
                       info=f"{len(choices)} saved game(s)" if choices else "No saved games yet: every game you play "
                       "is saved as you play it.")


def _saved(choice):
    if not choice or ":" not in choice:
        raise gr.Error("Choose a saved game first (↻ to list them).")
    kind, _, ident = choice.partition(":")
    return kind, ident


@ui
def open_saved_game_ui(choice):
    """Open a saved game, as a normal action: a game in progress goes on (restored from its last save
    when the server restarted); a save slot goes on as a new game."""
    kind, ident = _saved(choice)
    if kind == "session":
        state = API.post(_path("/v1/sessions", ident, "continue"))
    else:
        state = API.post(_path("/v1/saves", ident, "restore"), {})
    token = state["session_id"]
    status = RESTORED_TEXT.get(state["restored"]) or (
        f"Rebuilt from its parameters ({state.get('note') or 'its snapshot no longer fits this engine'}).")
    return (token, *_play_screen(state, _journal(token)), _story_so_far(token), status)


@ui
def restart_saved_game_ui(choice, token):
    """Start the adventure again (same AI, world, profile, quest and character settings): the game in
    progress chosen in the list, else the game being played. The old one is set aside."""
    kind, _, ident = (choice or "").partition(":")
    target = ident if kind == "session" else token
    if not target:
        raise gr.Error("Choose a game in progress (▶) to restart, or start a game first.")
    state = API.post(_path("/v1/sessions", target, "restart"))
    new = state["session_id"]
    return (new, *_play_screen(state, _journal(new)), _story_so_far(new), "Started again from the beginning.")


@ui
def save_to_slot_ui(token, slot):
    """Save the game being played into a save slot (its name; saving there again replaces it), or a new
    manual save. The game goes on."""
    _need_game(token)
    saved = API.post(_path("/v1/sessions", token, "saves"), {"slot": (slot or "").strip() or None})
    return f"💾 Saved to **{saved['slot']}** ({saved['location']}, turn {saved['turn']}). The game goes on."


@ui
def delete_saved_game_ui(choice):
    kind, ident = _saved(choice)
    if kind != "save":
        raise gr.Error("Only save slots (💾) are deleted here; a game in progress ends with *End the game*.")
    deleted = API.delete(_path("/v1/saves", ident))["deleted"]
    return f"Deleted the save **{deleted['slot']}**."


@ui
def cleanup_games_ui():
    report = API.post("/v1/sessions/cleanup", {})
    policy = report["policy"]
    return (f"🧹 Removed {len(report['removed'])} finished game(s): finished games are kept "
            f"{policy['retention_days']} days, at most {policy['keep_finished']} per user. Save slots and games in "
            "progress are kept.")


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
    def label(e):  # the API's label: the same as in the finished runs' pickers
        if e.get("label"):
            return e["label"] + (" · resumable" if e["resumable"] else "")
        return (f"{e['created_at'][:16]} · {e['source']}" + (f" · gen {e['generation']}" if e["generation"] is not None
                else "") + (f" · fitness {e['score']:.1f}" if e["score"] is not None else "")
                + (" · resumable" if e["resumable"] else "") + f" · {e['id'][:8]}")

    return [(label(e), e["id"]) for e in _recorded("/v1/records/engines", "engines")]


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


@ui
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


@ui
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
        return f"✅ **The judge is ready:** {status.get('model')}{where}.{model_sleep_note(status)}"
    return (f"⚠ **The judge is not available here.** {status.get('reason') or ''}\n\n"
            f"{status.get('how') or ''}")


def model_sleep_note(model):
    """A model that takes turns on the GPU (llm_sharing): asleep in CPU memory, or awake on the GPU."""
    if model.get("sleeping") is True:
        return " It is asleep in CPU memory (taking turns on the GPU): it wakes in a few seconds when asked."
    if model.get("sleeping") is False:
        return " It is awake on the GPU."
    return ""


def augmentation_markdown(state):
    """Beside the tick: whether augmentation is on, and its own language model (augmentation.llm:
    Qwen3.5-4B on vLLM) ready, or why not and how to start it."""
    model = state.get("model") or {}
    if not state.get("enabled"):
        head = "☐ **World augmentation is off.** Tick it to let the language model propose new entries."
    else:
        head = "☑ **World augmentation is on.**"
    if model.get("available"):
        where = f" at `{model['server']}`" if model.get("server") else " (in the API's process)"
        return f"{head}  \n✅ **Its model is ready:** {model.get('model')}{where}.{model_sleep_note(model)}"
    return (f"{head}  \n⚠ **Its model ({model.get('model')}) is not available here.** {model.get('reason') or ''}"
            f"\n\n{model.get('how') or ''}")


@ui
def augmentation_ui():
    """🌍 World → ✨ Grow the world, on loading: the tick (augmentation.enabled), its model's status and
    whether *Propose* can be pressed (GET /v1/augmentation)."""
    state = API.get("/v1/augmentation")
    return (gr.Checkbox(value=bool(state["enabled"])), augmentation_markdown(state),
            gr.Button(interactive=bool(state["enabled"])))


@ui
def set_augmentation_ui(enabled):
    """The tick: world augmentation switched on or off, saved (PUT /v1/augmentation); *Propose* follows,
    and the confirmation says when it takes effect."""
    state = API.put("/v1/augmentation", {"enabled": bool(enabled)})
    said = augmentation_markdown(state) + (f"  \n_{state['message']}_" if state.get("message") else "")
    return said, gr.Button(interactive=bool(state["enabled"]))


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


DECISION_ICONS = {"shown": "✅", "hidden": "👁‍🗨", "blocked": "⛔"}


def decisions_markdown(trace):
    """The author's view of a game's choices now (GET /v1/sessions/{id}/decisions): what is shown,
    hidden and blocked, and why, with the reason codes. Never part of the player's screen."""
    if trace.get("ended"):
        return "The game has ended: nothing is offered."
    limits = trace.get("limits") or {}
    lines = [f"**Now:** {trace.get('location')} · mode **{trace.get('mode')}**"
             + (f" · event *{trace['event']['title'] or trace['event']['name']}*" if trace.get("event") else "")
             + (f" · talking with **{trace['talking_to']}**" if trace.get("talking_to") else "")
             + f" · state {trace.get('state_version')}",
             f"**Limits:** up to {limits.get('cap', limits.get('limit'))} of {limits.get('limit')} choices shown now"
             + (f" (tension {trace['tension']})" if trace.get("tension") is not None else "")
             + " · " + " · ".join(f"{count} {status}" for status, count in (trace.get("counts") or {}).items())]
    for note in trace.get("notes") or []:
        lines.append(f"> ℹ️ {note['message']}")
    lines += ["", "| | Choice | Id | Why |", "| --- | --- | --- | --- |"]
    for entry in trace.get("actions") or []:
        why = "; ".join(f"`{r['code']}` {r['message']}" for r in entry["reasons"])
        rank = f" (rank {entry['rank']})" if entry.get("rank") else ""
        lines.append(f"| {DECISION_ICONS.get(entry['status'], '')} {entry['status']} | {_cell_text(entry['label'], 60)}"
                     f"{rank} | `{entry['id']}` | {_cell_text(why, 220)} |")
    return "\n".join(lines)


@ui
def decisions_ui(token):
    if not token:
        raise gr.Error("Start a game first.")
    return decisions_markdown(API.get(_path("/v1/sessions", token, "decisions")))


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


def _evaluated(result, engine_id):
    """The result's own account of what it played; a result that played another target than the one
    submitted says so (it never should)."""
    target = result.get("target") or {}
    line = evaluation_target_markdown(target) if target else ""
    if target and (target.get("engine_id") or None) != (engine_id or None):
        line = (f"⚠ **This result played `{target.get('engine_id') or 'the baseline'}`, not the target submitted "
                f"(`{engine_id or 'the baseline'}`).** " + line)
    return line


@ui
def judge_benchmark_ui(stories, seed, engine_id, pairwise, variants_text, workers, target=None,
                       progress=gr.Progress()):
    """Play and judge: the target is the selected trained engine (``engine_id``) or the baseline (``target``
    = TARGET_BASELINE). In A/B, A and B are both that target (B with version B's settings), on the same
    seeds, each pair judged in both orders. Returns (summary naming what was evaluated, the table)."""
    engine_id = evaluation_engine_id(target, engine_id) if target else (engine_id or None)
    variants = _variants(variants_text) if pairwise else {}
    if pairwise and not variants:
        raise gr.Error("A/B needs version B's settings (at least one dotted.path: value), or untick A/B.")
    job = API.post("/v1/jobs/judge-benchmark", {
        "stories": int(stories), "seed": int(seed or 0), "engine_id": engine_id, "pairwise": bool(pairwise),
        "variants": variants, "workers": int(workers),
    })
    result = API.wait(job, _job_progress(progress, "Judged"))
    return benchmark_view(result, engine_id)


def benchmark_view(result, engine_id=None):
    """A judge benchmark's result (absolute, or pairwise A/B): (its summary naming what was evaluated, the
    table of stories or pairs)."""
    target = result.get("target") or {}
    engine_id = engine_id if engine_id is not None else target.get("engine_id")
    evaluated = _evaluated(result, engine_id)
    if "a_wins" in result:
        rate = result.get("b_win_rate")
        low, high = result["wilson_95"]
        a_rate = None if rate is None else 1 - rate
        criteria = " · ".join(f"{name} {1 - value:.0%}" for name, value in result["criteria"].items() if value is not None)
        a_label = (result.get("a") or {}).get("label") or "A"
        b_label = (result.get("b") or {}).get("label") or "B"
        summary = (f"### A vs B: A won {result['a_wins']}, B won {result['b_wins']}, {result['ties']} tie(s)\n"
                   f"**A** = {a_label} · **B** = {b_label} (same seeds, each pair judged in both orders).  \n"
                   f"{evaluated}\n\n"
                   + (f"A's win rate **{a_rate:.0%}** of decided pairs (95% interval {1 - high:.0%}-{1 - low:.0%})."
                      if a_rate is not None else "No pair was decided.")
                   + (f"\n\nA's win rate per criterion: {criteria}" if criteria else ""))
        table = pd.DataFrame([{"seed": r["seed"], "winner": r["winner"], "agreed": r["agreed"],
                               "fitness A": round(r["fitness_a"], 1), "fitness B": round(r["fitness_b"], 1),
                               "reason": r["reason"]} for r in result["rows"]])
    else:
        engagement = result.get("engagement")
        criteria = " · ".join(f"{name} {value:.1f}" for name, value in result["criteria"].items())
        summary = ((f"### Engagement {engagement:.2f}/10 over {result['judged']} judged stories\n{criteria}"
                    if engagement is not None else "### No judgement could be read.") + f"\n\n{evaluated}")
        table = pd.DataFrame([{"seed": r["seed"], "engagement": r["engagement"], "fitness": round(r["fitness"], 1),
                               "turns": r["turns"], "won": r["won"], "verdict": r["verdict"]} for r in result["rows"]])
    return summary, table


@ui
def calibrate_fitness_ui(stories, seed, engine_id, apply, target=None, progress=gr.Progress()):
    """Play, judge and fit, on the selected trained engine or the baseline (``target``)."""
    engine_id = evaluation_engine_id(target, engine_id) if target else (engine_id or None)
    job = API.post("/v1/jobs/calibrate-fitness", {"stories": int(stories), "seed": int(seed or 0),
                                                  "engine_id": engine_id, "apply": bool(apply)})
    result = API.wait(job, _job_progress(progress, "Judged"))
    return calibration_view(result, engine_id)


def calibration_view(result, engine_id=None):
    """A calibration's result: (its summary naming what was evaluated, the suggested term weights as YAML)."""
    engine_id = engine_id if engine_id is not None else (result.get("target") or {}).get("engine_id")
    cv = result.get("cv_spearman")
    summary = (f"### Fitted on {result['n']} judged stories\nAgreement with the judge (Spearman): "
               f"{result['spearman']:+.2f} on the stories fitted, "
               + (f"**{cv:+.2f} on stories left out**" if cv is not None else "not cross-validated (too few stories)")
               + f" (ridge strength {result['alpha']:g}). "
               + ("The weights were saved as `fitness.term_weights`." if result["applied"]
                  else "Apply them below, or edit them first.")
               + f"\n\n{_evaluated(result, engine_id)}")
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


def fresh_choices_ui(source, extra, value):
    """A dropdown's choices as the world has them now (asked when it is opened: entries made, renamed or deleted
    since the page was built). Its value stays a choice, so a form that refers to an entry deleted meanwhile
    still sends it (the API then says what is wrong)."""
    if source == "stats":
        names = _stat_names()
    elif source == "profiles":
        try:
            names = list(API.get("/v1/variables/profiles")["profiles"])
        except APIError:
            names = []
    else:
        names = _names_of(source)
    choices = list(dict.fromkeys([*extra, *names]))
    for kept in (value if isinstance(value, list) else [value]):
        if kept not in (None, "") and kept not in choices:
            choices.append(kept)
    return gr.update(choices=choices)


def _stat_names():
    try:
        return list((API.get("/v1/stats")["character_stats"].get("stats") or {}))
    except APIError:
        return []


def _flow(value):
    """A value on one line, as YAML (a condition, a mapping of variables)."""
    return to_yaml(value, flow=True) if value not in (None, "", {}, []) else ""


# ---- the condition builder: a condition (core_engine/conditions.py) as a tree of groups and predicates -----
# The tree keeps how the condition was written: a list or {all: [...]}, the variables' shorthand, an `op`
# left out, so that a condition the builder only reads is written back exactly as it was; what the builder
# cannot show is kept as written (an "advanced" node) and never dropped or simplified.

CONDITION_GROUPS = {"all": "ALL of", "any": "ANY of", "not": "NOT"}
CONDITION_FACTS = {"has_item": "Player has item", "item_collected": "Item collected (this story)",
                   "visited": "Visited location", "at_location": "Player is at location", "npc_removed": "NPC removed",
                   "event_done": "Event done", "event_succeeded": "Event succeeded", "event_failed": "Event failed",
                   "action_done": "Action done", "outcome_tag": "Outcome tag happened", "mission": "Mission is",
                   "secret_known": "NPC's secret known"}
CONDITION_COMPARISONS = {"variable": "Variable", "stat": "Stat", "resource": "Coins, health or turn"}
CONDITION_OPERATORS = {"eq": "=", "ne": "≠", "gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "in": "in", "not_in": "not in"}
CONDITION_KINDS = ([(f"Group: {label} …", key) for key, label in (("all", "all of"), ("any", "any of"), ("not", "not"))]
                   + [(label, key) for key, label in CONDITION_FACTS.items()]
                   + [(f"{label} comparison", key) for key, label in CONDITION_COMPARISONS.items()])
CONDITION_TOP = "top"  # the condition itself (a path); its children: "top.0", "top.1", ...


def _advanced(raw, why):
    return {"t": "raw", "raw": raw, "why": why}


def condition_tree(raw):
    """The builder's tree of a condition (None: no condition): groups {t: group, g, form, c}, predicates
    {t: leaf, k, name, op, value, short}, and what the builder does not show, kept as written {t: raw}."""
    if raw is None or raw == {} or raw == []:
        return None
    return _condition_node(raw)


def _condition_node(raw):
    if isinstance(raw, list):
        if not raw:
            return _advanced(raw, "an empty list")
        return {"t": "group", "g": "all", "form": "list", "c": [_condition_node(child) for child in raw]}
    if not isinstance(raw, dict) or not raw:
        return _advanced(raw, "not a mapping or a list")
    keys = set(raw)
    groups = keys & set(CONDITION_GROUPS)
    if groups:
        group = groups.pop()
        body = raw[group]
        if len(keys) != 1:
            return _advanced(raw, f"`{group}` with other keys")
        if group == "not":
            if body in (None, {}, []):  # an empty NOT (being built): kept as written, still a group
                return {"t": "group", "g": "not", "form": "map", "c": [], "empty": body}
            return {"t": "group", "g": "not", "form": "map", "c": [_condition_node(body)]}
        if not isinstance(body, list):
            return _advanced(raw, f"`{group}` without a list of conditions")
        return {"t": "group", "g": group, "form": "map", "c": [_condition_node(child) for child in body]}
    subjects = keys & (set(CONDITION_FACTS) | set(CONDITION_COMPARISONS))
    if subjects:
        if len(subjects) != 1:
            return _advanced(raw, "more than one predicate in one mapping")
        key = subjects.pop()
        if key in CONDITION_FACTS:
            if keys != {key} or not isinstance(raw[key], str):
                return _advanced(raw, f"`{key}` written another way")
            return {"t": "leaf", "k": key, "name": raw[key], "op": None, "value": None, "short": None}
        if keys - {key, "op", "value"} or "value" not in raw or not isinstance(raw[key], str):
            return _advanced(raw, f"`{key}` written another way")
        return {"t": "leaf", "k": key, "name": raw[key], "op": raw.get("op"), "value": raw["value"], "short": None}
    # The shorthand: a mapping of variables, each a value or {op: value, ...}; all of them hold.
    children = []
    for name, expected in raw.items():
        if not isinstance(name, str):
            return _advanced(raw, "a shorthand with a name that is not text")
        if isinstance(expected, dict):
            if not expected or any(op not in CONDITION_OPERATORS for op in expected):
                return _advanced(raw, f"`{name}` with an unknown comparison")
            children += [{"t": "leaf", "k": "variable", "name": name, "op": op, "value": value, "short": "ops"}
                         for op, value in expected.items()]
        else:
            children.append({"t": "leaf", "k": "variable", "name": name, "op": None, "value": expected, "short": "bare"})
    return {"t": "group", "g": "all", "form": "shorthand", "c": children}


def condition_value(node):
    """The condition a tree stands for, written as it was read (what changed, written plainly)."""
    if node is None:
        return None
    if node["t"] == "raw":
        return copy.deepcopy(node["raw"])
    if node["t"] == "leaf":
        if node["k"] in CONDITION_FACTS:
            return {node["k"]: node["name"]}
        written = {node["k"]: node["name"]}
        if node.get("op") is not None:
            written["op"] = node["op"]
        written["value"] = copy.deepcopy(node["value"])
        return written
    children = node["c"]
    if node["g"] == "not":
        return {"not": condition_value(children[0]) if children else copy.deepcopy(node.get("empty"))}
    if node["g"] == "all" and node.get("form") == "shorthand":
        mapping = _as_shorthand(children)
        if mapping is not None:
            return mapping
    if node["g"] == "all" and node.get("form") == "list":
        return [condition_value(child) for child in children]
    return {node["g"]: [condition_value(child) for child in children]}


def _as_shorthand(children):
    """The variables' shorthand mapping of these predicates, or None when they cannot be written so."""
    mapping = {}
    for child in children:
        if child["t"] != "leaf" or child["k"] != "variable" or child["name"] in CONDITION_GROUPS \
                or child["name"] in CONDITION_FACTS or child["name"] in CONDITION_COMPARISONS:
            return None
        op = child.get("op")
        if child.get("short") == "bare" and op in (None, "eq") and child["name"] not in mapping:
            mapping[child["name"]] = copy.deepcopy(child["value"])
        elif isinstance(mapping.get(child["name"], {}), dict) and (op or "eq") not in mapping.get(child["name"], {}):
            mapping.setdefault(child["name"], {})[op or "eq"] = copy.deepcopy(child["value"])
        else:
            return None
    return mapping or None


def _condition_at(node, path):
    """The node at ``path`` ("top", "top.0.2", ...) and its parent (None for the top)."""
    parent = None
    for index in [int(part) for part in path.split(".")[1:]]:
        if node is None or node["t"] != "group" or not 0 <= index < len(node["c"]):
            raise gr.Error("That part of the condition no longer exists: choose it again.")
        parent, node = node, node["c"][index]
    return node, parent


def condition_add(tree, target, new):
    """``new`` added into the group at ``target`` (the top: into the top group; a top that is no group, or
    no condition: every one must hold, an ALL group around the two, or ``new`` alone)."""
    tree, new = copy.deepcopy(tree), copy.deepcopy(new)
    if tree is None:
        return new
    target = target or CONDITION_TOP
    node, _ = _condition_at(tree, target)
    if node["t"] == "group":
        if node["g"] == "not" and node["c"]:
            raise gr.Error("NOT holds one condition: put an ALL or ANY group in it to hold more.")
        if node.get("form") == "shorthand" and not (new["t"] == "leaf" and new["k"] == "variable"):
            node["form"] = "map"  # no longer only variables: written as {all: [...]}
        if node.get("form") == "shorthand":
            new["short"] = "bare" if new.get("op") in (None, "eq") else "ops"
        node["c"].append(new)
        return tree
    if target != CONDITION_TOP:
        raise gr.Error("Add into a group (ALL, ANY or NOT), or at the top.")
    return {"t": "group", "g": "all", "form": "map", "c": [tree, new]}


def condition_replace(tree, path, new):
    """The node at ``path`` replaced by ``new``; a group changed into another keeps its conditions."""
    tree = copy.deepcopy(tree)
    node, parent = _condition_at(tree, path)
    if node["t"] == "group" and new["t"] == "group":
        if new["g"] == "not" and len(node["c"]) > 1:
            raise gr.Error("NOT holds one condition: remove the others first, or put them in an ALL group.")
        node.update(g=new["g"], form="map" if node.get("form") == "shorthand" or new["g"] != "all" else node["form"])
        return tree
    new = copy.deepcopy(new)
    if node["t"] == "leaf" and new["t"] == "leaf" and node.get("short") and new["k"] == "variable":
        new["short"] = node["short"] if (new.get("op") in (None, "eq")) == (node["short"] == "bare") else "ops"
    if parent is None:
        return new
    if parent.get("form") == "shorthand" and not (new["t"] == "leaf" and new["k"] == "variable"):
        parent["form"] = "map"
    parent["c"][parent["c"].index(node)] = new
    return tree


def condition_remove(tree, path):
    """The node at ``path`` removed (the top: no condition)."""
    if (path or CONDITION_TOP) == CONDITION_TOP:
        return None
    tree = copy.deepcopy(tree)
    node, parent = _condition_at(tree, path)
    parent["c"].remove(node)
    return tree


def condition_node(kind, subject=None, op=None, value=""):
    """A node from the builder's form: a group, or a predicate (a comparison's value is YAML: 2, true,
    "text", [a, b])."""
    if kind in CONDITION_GROUPS:
        return {"t": "group", "g": kind, "form": "map", "c": []}
    if kind not in CONDITION_FACTS and kind not in CONDITION_COMPARISONS:
        raise gr.Error("Choose what the condition checks.")
    subject = str(subject or "").strip()
    if not subject:
        raise gr.Error("Choose what it refers to (an item, a place, a variable, ...).")
    if kind in CONDITION_FACTS:
        return {"t": "leaf", "k": kind, "name": subject, "op": None, "value": None, "short": None}
    if str(value if value is not None else "").strip() == "":
        raise gr.Error("Give the value to compare with (e.g. 2, true, \"text\", [a, b]).")
    try:
        parsed = yaml.safe_load(str(value))
    except yaml.YAMLError as exc:
        raise gr.Error(f"The value is not valid YAML: {exc}") from exc
    return {"t": "leaf", "k": kind, "name": subject, "op": None if op in (None, "", "eq") else op, "value": parsed,
            "short": None}


def _condition_label(node):
    if node is None:
        return "*(no condition: always)*"
    if node["t"] == "raw":
        return f"⚙ kept as written (advanced: {node['why']}): `{to_yaml(node['raw'], flow=True) or node['raw']}`"
    if node["t"] == "group":
        return f"**{CONDITION_GROUPS[node['g']]}**" + (" *(shorthand)*" if node.get("form") == "shorthand" else "")
    if node["k"] in CONDITION_FACTS:
        return f"{CONDITION_FACTS[node['k']]}: **{node['name']}**"
    op = node.get("op") or "eq"
    return (f"{CONDITION_COMPARISONS[node['k']]} **{node['name']}** {CONDITION_OPERATORS.get(op, op)} "
            f"`{to_yaml(node['value'], flow=True) if isinstance(node['value'], (list, dict)) else node['value']}`")


def condition_rows(tree):
    """(path, display number, depth, label) of every node, in order (the top first)."""
    rows = []

    def walk(node, path, number, depth):
        rows.append((path, number, depth, _condition_label(node)))
        if node is not None and node["t"] == "group":
            for index, child in enumerate(node["c"]):
                walk(child, f"{path}.{index}", f"{number}.{index + 1}" if number else str(index + 1), depth + 1)

    walk(tree, CONDITION_TOP, "", 0)
    return rows


def condition_tree_markdown(tree):
    lines = []
    for _, number, depth, label in condition_rows(tree):
        lines.append(("&nbsp;" * 6 * depth) + (f"`{number}` " if number else "") + label)
    return "  \n".join(lines)


def condition_text(text):
    """A condition field's text as YAML (one line; the braces of a mapping may be left out)."""
    text = str(text or "").strip()
    if not text:
        return None
    try:
        return yaml.safe_load(text if text.startswith(("{", "[")) else "{" + text + "}")
    except yaml.YAMLError as exc:
        raise gr.Error(f"The condition is not valid YAML, so the builder cannot read it: {exc}") from exc


def condition_yaml(value):
    """A condition as the one-line YAML its field holds (empty: no condition)."""
    if value in (None, {}, []):
        return ""
    return yaml.safe_dump(value, default_flow_style=True, sort_keys=False, allow_unicode=True, width=10**9).strip()


def _plain_label(label):
    return re.sub(r"[*`]", "", label).replace("&nbsp;", " ")


def condition_targets(tree):
    """The builder's *Part of the condition* choices: every node, numbered as the tree shows them."""
    return [(f"{number or 'The top'} — {_plain_label(label)}"[:90], path) for path, number, _, label in condition_rows(tree)]


def condition_status(tree, where="enabled_if"):
    """The condition checked as a save checks it (POST /v1/conditions/validate), and what is kept as written."""
    value = condition_value(tree)
    try:
        checked = API.post("/v1/conditions/validate", {"condition": value, "where": where})
    except APIError as exc:
        return f"⚠ The condition could not be checked: {exc}"
    kept = sum(1 for _, _, _, label in condition_rows(tree) if label.startswith("⚙"))
    note = (f"  \n{kept} part(s) kept as written (advanced): edit them in *Advanced YAML*; the builder leaves them "
            "as they are." if kept else "")
    if checked["empty"]:
        return "✓ No condition: always." + note
    if checked["valid"]:
        return "✓ Valid: checked as a save checks it." + note
    return "⚠ **Not valid:**\n" + "\n".join(f"- {error}" for error in checked["errors"]) + note


def _subject_choices(kind, ref):
    """What a predicate of this kind may name (from GET /v1/conditions/reference)."""
    ref = ref or {}
    if kind in CONDITION_FACTS:
        return list(((ref.get("facts") or {}).get(kind) or {}).get("names") or [])
    if kind == "variable":
        return [(f"{name} ({kind_ or 'any'})", name) for name, kind_ in ((ref.get("comparisons") or {}).get("variable")
                                                                        or {}).items()]
    if kind in ("stat", "resource"):
        return list((ref.get("comparisons") or {}).get(kind) or [])
    return []


@ui
def condition_kind_ui(kind, ref, subject=None):
    """The builder's form for a kind of condition: what it may name, and an operator and a value for a
    comparison (a group names nothing)."""
    comparison = kind in CONDITION_COMPARISONS
    group = kind in CONDITION_GROUPS
    choices = _subject_choices(kind, ref)
    values = [value if isinstance(value, str) else value[1] for value in choices]
    free = kind in ("action_done", "outcome_tag")
    return (gr.Dropdown(choices=choices, value=subject if subject in values or (free and subject) else None,
                        visible=not group, allow_custom_value=free,
                        info="Type it: any text." if free else None),
            gr.Dropdown(visible=comparison), gr.Textbox(visible=comparison))


@ui
def condition_mode_ui(mode, text, where="enabled_if"):
    """*Edit the condition as*: the builder reads the field (what it cannot show is kept as written); the
    YAML field is then written by the builder only. Advanced YAML: the field is edited as text. Returns
    (the mode, the builder, the field, the tree, the reference, the tree shown, the parts, the status)."""
    if mode != "Builder":
        return (gr.skip(), gr.update(visible=False), gr.Textbox(interactive=True), gr.skip(), gr.skip(), gr.skip(),
                gr.skip(), "")
    try:
        tree = condition_tree(condition_text(text))
    except gr.Error as exc:
        return (gr.Radio(value="Advanced YAML"), gr.update(visible=False), gr.Textbox(interactive=True), gr.skip(),
                gr.skip(), gr.skip(), gr.skip(), f"⚠ {_error_text(exc)} Correct it here first.")
    ref = API.get("/v1/conditions/reference")
    return (gr.skip(), gr.update(visible=True), gr.Textbox(interactive=False), tree, ref,
            condition_tree_markdown(tree), gr.Dropdown(choices=condition_targets(tree), value=CONDITION_TOP),
            condition_status(tree, where))


@ui
def condition_sync_ui(mode, text, where="enabled_if", tree=None):
    """The field changed by a load (another entry or action): the builder follows it (not when the field
    holds what the builder itself just wrote: its tree stays as it is)."""
    if mode != "Builder":
        return gr.skip(), gr.skip(), gr.skip(), gr.skip()
    try:
        parsed = condition_text(text)
    except gr.Error as exc:
        return gr.skip(), gr.skip(), gr.skip(), f"⚠ {_error_text(exc)}"
    if tree is not None and condition_value(tree) == parsed:
        return gr.skip(), gr.skip(), gr.skip(), gr.skip()
    tree = condition_tree(parsed)
    return (tree, condition_tree_markdown(tree), gr.Dropdown(choices=condition_targets(tree), value=CONDITION_TOP),
            condition_status(tree, where))


@ui
def condition_pick_ui(tree, target, ref):
    """A part of the condition chosen: the form shows it (to replace it), or stays as it is for a group."""
    node, _ = _condition_at(tree, target or CONDITION_TOP) if tree is not None else (None, None)
    if node is None or node["t"] == "raw":
        return gr.skip(), gr.skip(), gr.skip(), gr.skip()
    if node["t"] == "group":
        return node["g"], gr.Dropdown(visible=False), gr.Dropdown(visible=False), gr.Textbox(visible=False)
    comparison = node["k"] in CONDITION_COMPARISONS
    return (node["k"], condition_kind_ui(node["k"], ref, node["name"])[0],
            gr.Dropdown(visible=comparison, value=node.get("op") or "eq"),
            gr.Textbox(visible=comparison, value=condition_yaml(node["value"]) if isinstance(node["value"], (list, dict))
                       else ("" if node["value"] is None else yaml.safe_dump(node["value"]).strip().removesuffix("..."
                                                                                                         ).strip())))


@ui
def condition_edit_ui(action, tree, target, kind, subject, op, value, where="enabled_if"):
    """➕ Add, ✏ Replace or 🗑 Remove a part of the condition: the tree, then the field written from it (as it
    was written, but for what changed). Returns (the tree, the field, the tree shown, the parts, the status)."""
    if action == "remove":
        tree = condition_remove(tree, target)
    else:
        node = condition_node(kind, subject, op, value)
        tree = condition_add(tree, target, node) if action == "add" else condition_replace(tree, target or CONDITION_TOP, node)
    targets = condition_targets(tree)
    keep = target if action != "remove" and target in [path for _, path in targets] else CONDITION_TOP
    return (tree, condition_yaml(condition_value(tree)), condition_tree_markdown(tree),
            gr.Dropdown(choices=targets, value=keep), condition_status(tree, where))


def condition_builder_panel(prefix, label):
    """The builder beside a condition field (its components; wired by wire_condition_builder)."""
    with gr.Accordion(f"🧩 {label}: condition builder", open=False, elem_id=f"{prefix}-builder"):
        mode = gr.Radio(["Advanced YAML", "Builder"], value="Advanced YAML", label="Edit the condition as",
                        elem_id=f"{prefix}-mode",
                        info="Builder: groups (ALL, ANY, NOT) and predicates chosen from the world; what it cannot "
                        "show is kept as written. Advanced YAML: the field above, as text.")
        with gr.Group(visible=False) as box:
            tree_view = gr.Markdown(elem_id=f"{prefix}-tree")
            with gr.Row():
                target = gr.Dropdown([("The top", CONDITION_TOP)], value=CONDITION_TOP, label="Part of the condition",
                                     scale=3, elem_id=f"{prefix}-target")
                add = gr.Button("➕ Add into it", scale=1, elem_id=f"{prefix}-add")
                replace = gr.Button("✏ Replace it", scale=1, elem_id=f"{prefix}-replace")
                remove = gr.Button("🗑 Remove it", scale=1, elem_id=f"{prefix}-remove")
            with gr.Row():
                kind = gr.Dropdown(CONDITION_KINDS, value="has_item", label="Checks", scale=2, elem_id=f"{prefix}-kind")
                subject = gr.Dropdown([], label="Which", scale=2, elem_id=f"{prefix}-subject")
                op = gr.Dropdown([(symbol, key) for key, symbol in CONDITION_OPERATORS.items()], value="gte",
                                 label="Compare", scale=1, visible=False, elem_id=f"{prefix}-op")
                value = gr.Textbox(label="Value (YAML: 2, true, text, [a, b])", scale=2, visible=False,
                                   elem_id=f"{prefix}-value")
        status = gr.Markdown(elem_id=f"{prefix}-status")
        tree, ref = gr.State(None), gr.State({})
    return {"mode": mode, "box": box, "tree_view": tree_view, "target": target, "add": add, "replace": replace,
            "remove": remove, "kind": kind, "subject": subject, "op": op, "value": value, "status": status,
            "tree": tree, "ref": ref}


def wire_condition_builder(panel, field, where, after):
    """Wire a builder to its field; ``after``: what follows a write into the field (its draft's status)."""
    b = panel
    b["mode"].input(lambda mode, text: condition_mode_ui(mode, text, where), [b["mode"], field],
                    [b["mode"], b["box"], field, b["tree"], b["ref"], b["tree_view"], b["target"], b["status"]]).then(
        condition_kind_ui, [b["kind"], b["ref"]], [b["subject"], b["op"], b["value"]], show_progress="hidden")
    field.change(lambda mode, text, tree: condition_sync_ui(mode, text, where, tree), [b["mode"], field, b["tree"]],
                 [b["tree"], b["tree_view"], b["target"], b["status"]], show_progress="hidden")
    b["kind"].input(condition_kind_ui, [b["kind"], b["ref"]], [b["subject"], b["op"], b["value"]], show_progress="hidden")
    b["target"].input(condition_pick_ui, [b["tree"], b["target"], b["ref"]],
                      [b["kind"], b["subject"], b["op"], b["value"]], show_progress="hidden")
    for button, action in ((b["add"], "add"), (b["replace"], "replace"), (b["remove"], "remove")):
        button.click(lambda tree, target, kind, subject, op, value, action=action: condition_edit_ui(
            action, tree, target, kind, subject, op, value, where),
            [b["tree"], b["target"], b["kind"], b["subject"], b["op"], b["value"]],
            [b["tree"], field, b["tree_view"], b["target"], b["status"]], show_progress="hidden").success(**after)


def _dialogue_frame(dialogue):
    """The dialogue table as a DataFrame of (condition, line) text (empty when there is none)."""
    columns = ["Said when (condition, empty: always)", "Says"]
    if dialogue is None:
        return pd.DataFrame(columns=columns)
    frame = dialogue if isinstance(dialogue, pd.DataFrame) else pd.DataFrame(dialogue)
    return frame if len(frame.columns) == 2 else pd.DataFrame(columns=columns)


def _dialogue_cell(value):
    return "" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value)


def dialogue_lines_ui(dialogue, line=None, condition=""):
    """The dialogue table changed (a load, or an edit): the lines its builder can take (the chosen one
    kept while it is there) and that line's condition, when it differs from the field (so the field and
    the table never echo each other). Returns (the line picker, the condition field)."""
    frame = _dialogue_frame(dialogue)
    choices = [(f"{index + 1}. {_dialogue_cell(row.iloc[1])[:50] or '(no line yet)'}", index)
               for index, (_, row) in enumerate(frame.iterrows())]
    line = line if isinstance(line, int) and 0 <= line < len(frame) else (0 if len(frame) else None)
    held = _dialogue_cell(frame.iat[line, 0]).strip() if line is not None else ""
    # Only the value: whether the field can be typed in is the builder's (it writes the field itself).
    field = gr.skip() if held == str(condition or "").strip() else gr.Textbox(value=held)
    return gr.Dropdown(choices=choices, value=line), field


def dialogue_line_condition_ui(dialogue, line):
    """A line chosen for the builder: its condition, into the field."""
    frame = _dialogue_frame(dialogue)
    if not isinstance(line, int) or not 0 <= line < len(frame):
        return gr.Textbox(value="")
    return gr.Textbox(value=_dialogue_cell(frame.iat[line, 0]).strip())


def dialogue_write_condition_ui(dialogue, line, condition):
    """The field changed (typed, or written by the builder): into its line of the table, only when it
    differs (else nothing, so the table and the field never echo each other)."""
    frame = _dialogue_frame(dialogue)
    if not isinstance(line, int) or not 0 <= line < len(frame) or _dialogue_cell(frame.iat[line, 0]).strip() == str(
            condition or "").strip():
        return gr.skip()
    frame = frame.copy()
    frame.iat[line, 0] = str(condition or "").strip()
    return frame


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
    kind, name, index, action = _action_body(state, *values)
    API.put(_path(f"/v1/entities/{kind}", name, "actions", index), {"action": action})
    form = load_action_form_ui(kind, name, index)
    return (gr.Dropdown(choices=_action_choices(kind, name), value=index), *form,
            f"Saved the action **{action['name']}** of {name}.", load_entity_actions_yaml(kind, name))


def _action_body(state, *values):
    """The action in the form (with the outcome being edited) as PUT .../actions/{index} (and the entry
    draft's action) take it: (kind, entry, index, action)."""
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
    return state["kind"], state["name"], state["index"], action


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
    policy = _policy_body(kind, trigger, chance, weight, cooldown, most, repeat, unique, location)
    API.put(_path(f"/v1/entities/{kind}", name, "policy"), {"policy": policy})
    return f"Saved when **{name}** happens." if kind == "Events" else f"Saved **{name}**'s uniqueness.", \
        load_entity_policy_yaml(kind, name)


def _policy_body(kind, trigger, chance, weight, cooldown, most, repeat, unique, location):
    """The scheduling form as PUT .../policy (and the entry draft's policy) take it."""
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
    return policy


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
    details = _details_body(kind, name, known_places, known_items, article, dialogue, distant, entered, prompts,
                            locked, points)
    API.put(_path(f"/v1/entities/{kind}", name, "details"), {"details": details})
    return f"Saved the details of **{name}**.", load_entity_yaml_ui(kind, name)


def _details_body(kind, name, known_places, known_items, article, dialogue, distant, entered, prompts, locked,
                  points=None):
    """The details form as PUT .../details (and the entry draft's details) take it."""
    if kind == "NPCs":
        # A line with several variants (a list) is shown joined with " / ": it is saved as the list again, as it
        # was (unchanged), or split at " / " (edited in its row), never as one string holding the separators.
        stored = API.get(_path(f"/v1/entities/{kind}", name, "details"))["details"].get("dialogue") or []
        variants = {" / ".join(line["text"]): list(line["text"]) for line in stored if isinstance(line.get("text"), list)}
        rows = list((dialogue if dialogue is not None else pd.DataFrame()).iterrows())
        lines = []
        for number, (_, row) in enumerate(rows):
            condition, text = str(row.iloc[0] or "").strip(), str(row.iloc[1] or "").strip()
            if not text:
                continue
            said = variants.get(text)
            if said is None and " / " in text and len(rows) == len(stored) \
                    and isinstance(stored[number].get("text"), list):
                said = [part.strip() for part in text.split(" / ") if part.strip()]
            lines.append({"text": said or text,
                          **({"enabled_if": _parse_flow(condition, "Said when")} if condition else {})})
        details = {"known_locations": list(known_places or []), "known_objects": list(known_items or []),
                   "article": article or None, "dialogue": lines}
    else:
        details = {"distant_descriptions": _lines(distant), "entered_descriptions": _lines(entered),
                   "interact_prompts": _lines(prompts), "exit_locked": bool(locked),
                   "interaction_points": _parse_points(points)}
    return details


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


def entry_sections(kind):
    """The editor's sections an entry of this kind has (scheduling: events and items; details: people and places)."""
    return [part for part in DRAFT_PARTS
            if not (part in ("scheduling", "policy_yaml") and kind not in ("Events", "Items"))
            and not (part == "details" and kind not in ("NPCs", "Locations"))]


def draft_status_markup(dirty, loaded):
    """The editor's draft indicator: the sections with unsaved changes, and those saved (data-dirty: the
    page asks before it is closed or reloaded)."""
    saved = [part for part in entry_sections((loaded or {}).get("kind")) if part not in (dirty or [])]
    if dirty:
        what = ", ".join(DRAFT_PARTS[part] for part in dirty)
        rest = (f' <span class="draft-saved">· ✓ Saved: {html.escape(", ".join(DRAFT_PARTS[p] for p in saved))}</span>'
                if saved else "")
        return (f'<div class="draft-status dirty" data-dirty="1">● Unsaved changes in '
                f'{html.escape((loaded or {}).get("entity") or "the new entry")}: {html.escape(what)}{rest}</div>')
    return '<div class="draft-status" data-dirty="0">✓ No unsaved changes: every section is saved</div>'


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
            gr.skip() if message is None else message,
            gr.skip() if section is None else section,  # the World section open now
            list(capture))


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


# ---- the content library (🌍 World → Entries): every location, NPC, item and event, searchable ----------

LIBRARY_ALL = "All"
LIBRARY_KINDS = ("Locations", "NPCs", "Items", "Events")
LIBRARY_ICONS = {"Locations": "📍", "NPCs": "🧑", "Items": "🎒", "Events": "⚡"}
LIBRARY_LIMIT = 200  # results listed at once (the rest: narrow the search)


def library_key(kind, name):
    """A library entry's value: its content type and its name (JSON: any name is safe in it)."""
    return json.dumps([kind, name], ensure_ascii=False)


def library_entries():
    """{kind: [summary, ...]} of the world's entries (GET /v1/entities/{kind}?fields=summary); a kind
    that cannot be read is empty (and logged)."""
    found = {}
    for kind in LIBRARY_KINDS:
        try:
            found[kind] = API.get(_path("/v1/entities", kind), fields="summary")["entries"]
        except APIError as exc:
            LOG.warning("The %s could not be listed: %s", kind, exc)
            found[kind] = []
    return found


def library_matches(entry, query):
    """Whether every word of the search is in the entry's name, tags, summary or facts (any case)."""
    words = str(query or "").lower().split()
    if not words:
        return True
    text = " ".join([entry["name"], *entry.get("tags", []), entry.get("summary", ""), *entry.get("facts", [])]).lower()
    return all(word in text for word in words)


def library_label(kind, entry):
    """A result's line: the type's icon, the name, its facts and tags, its summary."""
    parts = [f"{LIBRARY_ICONS.get(kind, '')} {entry['name']}"]
    if entry.get("facts"):
        parts.append(f"({', '.join(entry['facts'])})")
    if entry.get("tags"):
        shown = entry["tags"][:6]
        parts.append("· tags: " + ", ".join(shown) + (f" +{len(entry['tags']) - 6}" if len(entry["tags"]) > 6 else ""))
    if entry.get("summary"):
        parts.append(f"— {entry['summary']}")
    return " ".join(parts)


def library_view(query, kind_filter, loaded, entries=None, limit=None):
    """The library for a search and a type filter: (the type filter's choices with per-type counts, the
    results (label, key) shown: the first ``limit`` (LIBRARY_LIMIT a page; more with *Show more*, and
    always as far as the entry open in the editor), the key of the entry the editor has open (None when
    the search or filter hides it: it stays open, nothing else is chosen), the status line)."""
    return _library_view(query, kind_filter, loaded, entries, limit)[:4]


def _library_view(query, kind_filter, loaded, entries=None, limit=None):
    """library_view, with how many results are shown and how many there are."""
    limit = max(LIBRARY_LIMIT, int(limit or LIBRARY_LIMIT))
    entries = entries if entries is not None else library_entries()
    kind_filter = kind_filter if kind_filter in (LIBRARY_ALL, *LIBRARY_KINDS) else LIBRARY_ALL
    matching = {kind: [e for e in entries.get(kind, []) if library_matches(e, query)] for kind in LIBRARY_KINDS}
    total = sum(len(entries.get(kind, [])) for kind in LIBRARY_KINDS)
    searching = bool(str(query or "").strip())

    def count(kind=None):
        have = total if kind is None else len(entries.get(kind, []))
        found = sum(map(len, matching.values())) if kind is None else len(matching[kind])
        return f"{found} of {have}" if searching else str(have)

    filters = [(f"All ({count()})", LIBRARY_ALL)] + [(f"{LIBRARY_ICONS[k]} {k} ({count(k)})", k) for k in LIBRARY_KINDS]
    shown_kinds = LIBRARY_KINDS if kind_filter == LIBRARY_ALL else (kind_filter,)
    results = [(library_label(kind, e), library_key(kind, e["name"])) for kind in shown_kinds for e in matching[kind]]
    loaded = loaded or {}
    current = library_key(loaded.get("kind"), loaded.get("entity")) if loaded.get("entity") else None
    keys = [value for _, value in results]
    if current in keys and keys.index(current) >= limit:  # the open entry is never cut off: a page more
        limit = (keys.index(current) // LIBRARY_LIMIT + 1) * LIBRARY_LIMIT
    shown = results[:limit]
    selected = current if current in keys else None
    what = "entries" if kind_filter == LIBRARY_ALL else kind_filter
    if total == 0:
        status = "**The world has no entries yet.** Choose a content type below and press **New entry** to add one."
    elif kind_filter != LIBRARY_ALL and not entries.get(kind_filter):
        status = (f"**No {kind_filter.lower() if kind_filter != 'NPCs' else 'NPCs'} in this world yet.** Choose "
                  f"*{kind_filter}* as the content type below and press **New entry** to add one.")
    elif not results:
        status = (f"**No {what} match “{str(query).strip()}”.** Clear the search, or choose *All* to look in "
                  "every type.")
    else:
        have = total if kind_filter == LIBRARY_ALL else len(entries.get(kind_filter, []))
        status = (f"**{len(results)}** of {have} {what} match “{str(query).strip()}”" if searching
                  else f"Showing all **{have}** {what}")
        if len(results) > len(shown):
            status += (f" — showing the first **{len(shown)}**: *Show {min(LIBRARY_LIMIT, len(results) - len(shown))} "
                       "more*, or narrow the search")
        status += "."
    if current:
        name = f"**{loaded['entity']}** ({loaded['kind']})"
        exists = any(e["name"] == loaded["entity"] for e in entries.get(loaded["kind"], []))
        status += (f"  \nOpen in the editor: {name}." if selected else
                   f"  \n⚠ Open in the editor: {name}, no longer in the world (renamed or deleted elsewhere): saving "
                   "it would add it again; choose another entry, or discard it." if not exists else
                   f"  \nOpen in the editor: {name}, hidden by the search or filter: it stays open, nothing else "
                   "was chosen.")
    return filters, shown, selected, status, len(shown), len(results)


@ui
def library_ui(query, kind_filter, loaded, limit=None):
    """The world changed (or ↻ Refresh): the library's filter, results and status, as many shown as before
    (``limit``). The entry open in the editor never changes here. Returns (the type filter, the results,
    the status, *Show more*, how many are shown)."""
    filters, shown, selected, status, count, total = _library_view(query, kind_filter, loaded, limit=limit)
    kind_filter = kind_filter if kind_filter in (LIBRARY_ALL, *LIBRARY_KINDS) else LIBRARY_ALL
    left = total - count
    more = gr.Button(value=f"Show {min(LIBRARY_LIMIT, left)} more ({left} not shown)", visible=left > 0)
    return (gr.Radio(choices=filters, value=kind_filter), gr.Radio(choices=shown, value=selected), status, more,
            max(count, LIBRARY_LIMIT))


LIBRARY_WATCH = 10.0  # seconds between two looks at the world's revision (changes made elsewhere)


@ui
def library_watch_ui(known, query, kind_filter, loaded, limit=None):
    """Now and then: the world's revision (GET /v1/world/revision). Changed since it was last seen (an
    entry added, changed, renamed or deleted by another browser, a tool, an import or by hand): the
    library is listed again (library_ui; the editor is not touched). Unchanged: nothing. Returns the
    library's outputs and the revision seen."""
    revision = API.get("/v1/world/revision")["revision"]
    if revision == known:
        return (*[gr.skip()] * 5, known)
    if known is None:  # the first look (the page has just listed it): only remembered
        return (*[gr.skip()] * 5, revision)
    return (*library_ui(query, kind_filter, loaded, limit), revision)


@ui
def library_search_ui(query, kind_filter, loaded):
    """A search or a type filter: the first page of what matches."""
    return library_ui(query, kind_filter, loaded, LIBRARY_LIMIT)


@ui
def library_more_ui(query, kind_filter, loaded, limit):
    """*Show more*: the next page of the results (those shown stay, the open entry stays marked)."""
    return library_ui(query, kind_filter, loaded, int(limit or LIBRARY_LIMIT) + LIBRARY_LIMIT)


@ui
def library_choice_ui(key, loaded, base, *values):
    """An entry chosen in the library: opened in the editor as when chosen in its pickers; with unsaved
    changes, nothing is opened yet and the editor asks first (Save, Discard or Stay)."""
    try:
        kind, name = json.loads(key) if key else (None, None)
    except (TypeError, ValueError):
        kind, name = None, None
    loaded = loaded or {}
    if kind not in LIBRARY_KINDS or not name or (kind, name) == (loaded.get("kind"), loaded.get("entity")):
        return _draft_update()
    if name not in _entity_names(kind):
        raise gr.Error(f"{name} is no longer among the {kind}: it was renamed or deleted. ↻ Refresh the library.")
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


# 🌍 World's sub-navigation: three places, Content (its own sections), Missions and Map. They select the
# World's sections (world_sections; their ids are what the leave guards, the address and the drafts use).
WORLD_GROUPS = {"content": ("📚 Content", ("entries", "story-rules", "behaviour", "grow", "checks")),
                "missions": ("🎯 Missions", ("missions",)), "map": ("🗺 Map", ("map",))}
CONTENT_SECTIONS = WORLD_GROUPS["content"][1]


def world_group_of(section):
    return next((group for group, (_, sections) in WORLD_GROUPS.items() if section in sections), "content")


def world_group_target(group, now_section, content_last=None):
    """The section a sub-navigation button opens: Content its last section open (else Entries), Missions
    and Map their own."""
    if group == "content":
        if now_section in CONTENT_SECTIONS:
            return now_section
        return content_last if content_last in CONTENT_SECTIONS else "entries"
    return WORLD_GROUPS[group][1][0]


def world_nav_ui(section, content_last=None):
    """The sub-navigation for the section open now: the place highlighted, Content's own row shown (its
    section highlighted) while in Content, and Content's last section remembered. Returns (Content,
    Missions, Map, Content's row, its sections' buttons, the last Content section)."""
    group = world_group_of(section)
    tops = [gr.Button(variant="primary" if key == group else "secondary") for key in WORLD_GROUPS]
    inner = [gr.Button(variant="primary" if key == section else "secondary") for key in CONTENT_SECTIONS]
    last = section if section in CONTENT_SECTIONS else (content_last or "entries")
    return (*tops, gr.update(visible=group == "content"), *inner, last)


def world_section_choice_ui(section, now_section, loaded, dirty):
    """A sub-navigation button: the World's section guard (section_choice_ui: leaving an editor with
    unsaved changes asks first), and the section's tab selected when it may go."""
    result = section_choice_ui(section, now_section, loaded, dirty)
    if "section" not in result and result.get("section_now"):
        result = {**result, "section": result["section_now"]}
    return result


# Which World section each editor with a draft is in (drafts_dirty: {editor: [parts unsaved]}).
EDITOR_SECTIONS = {"entries": "entries", "missions": "missions", "rules": "story-rules", "map": "map"}
SECTION_EDITORS = {section: editor for editor, section in EDITOR_SECTIONS.items()}


def _dirty_of(dirty, editor):
    return list((dirty or {}).get(editor) or [])


def _leave_question(editor, loaded, parts, target):
    if editor == "weights":
        return (f"### ● Unsaved changes\nThe weights have unsaved changes ({', '.join(parts)}). Save them before "
                f"{_going(target)}? **Discard** drops them (the saved values come back); **Stay** keeps editing.")
    if editor == "entries":
        what, who = ", ".join(DRAFT_PARTS[p] for p in parts), _entry_label(loaded)
    else:
        what = ", ".join(_form_parts(editor)[p][0] for p in parts)
        who = FORM_EDITORS[editor]["title"]
    return (f"### ● Unsaved changes\n{who} has unsaved changes in **{what}**. Save them before {_going(target)}? "
            "**Discard** drops them; **Stay** keeps editing.")


def section_choice_ui(section, now_section, loaded, dirty):
    """Another section of 🌍 World opened: leaving an editor with unsaved changes keeps it open and asks
    (in that editor); otherwise the section is the one open now. Returns {section, section_now, prompts}."""
    editor = SECTION_EDITORS.get(now_section)
    parts = _dirty_of(dirty, editor) if editor and section != now_section else []
    if parts:
        target = {"type": "section", "id": section}
        return {"section": now_section, "prompts": {editor: (True, _leave_question(editor, loaded, parts, target), target)}}
    return {"section_now": section}


def nav_choice_ui(destination, now, now_section, loaded, dirty):
    """The banner or the menu: leaving 🌍 World (``now``) with unsaved changes in any of its editors asks
    first, in that editor (opened); anything else goes at once. Reads only which parts are unsaved
    (drafts_dirty), not the drafts. Returns {nav, section, section_now, prompts}."""
    destination = destination if destination in DESTINATION_IDS else "world"
    if now == "world" and destination != "world":
        editor = next((name for name in EDITOR_SECTIONS if _dirty_of(dirty, name)), None)
        if editor:
            target = {"type": "destination", "id": destination}
            return {"nav": navigate("world"), "section": EDITOR_SECTIONS[editor], "section_now": EDITOR_SECTIONS[editor],
                    "prompts": {editor: (True, _leave_question(editor, loaded, _dirty_of(dirty, editor), target), target)}}
    if now == "advanced" and destination != "advanced" and _dirty_of(dirty, "weights"):
        # Leaving ⚙ Advanced with an unsaved weights draft: asked in Weights (opened).
        target = {"type": "destination", "id": destination}
        return {"nav": navigate("advanced"), "advanced_section": "weights",
                "prompts": {"weights": (True, _leave_question("weights", None, _dirty_of(dirty, "weights"), target),
                                        target)}}
    return {"nav": navigate(destination)}


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
    return _output_value(component)[1]


def _error_text(exc):
    """An error as the prompt shows it: escaped (a YAML error names '<stream end>', not a tag)."""
    return html.escape(str(getattr(exc, "message", None) or exc))


def _draft_body(dirty, kind, name, split):
    """The entry draft (PUT /v1/entities/{kind}/{name}/draft) of the parts with unsaved changes, each as its
    own save sends it: fields, action, policy (scheduling, or its YAML), details, entry_yaml, actions."""
    body = {}
    for part in dirty:
        if part == "fields":
            body["fields"] = _fields_body(kind, name, *split["fields"])
        elif part == "action":
            _, _, index, action = _action_body(split["action"][0], *split["action"][1:])
            body["action"] = {"index": index, "action": action}
        elif part == "scheduling":
            body["policy"] = _policy_body(kind, *split["scheduling"])
        elif part == "details":
            body["details"] = _details_body(kind, name, *split["details"])
        elif part == "entry_yaml":
            body["entry_yaml"] = split["entry_yaml"][0]
        elif part == "actions_yaml":
            body["actions"] = split["actions_yaml"][0]
        elif part == "policy_yaml":
            body["policy"] = split["policy_yaml"][0]
    return body


def _save_draft(dirty, loaded, values):
    """Save every part with unsaved changes at once (PUT .../draft): the entry is checked whole and written
    once, or not at all. Nothing is saved when two parts edit the same data. Returns (the entry as it is
    now, the parts saved: all of them); a failure raises DraftNotSaved, nothing having been saved."""
    loaded = dict(loaded or {})
    conflict = next(((a, b) for a, b in DRAFT_CONFLICTS if a in dirty and b in dirty), None)
    if conflict:
        raise DraftNotSaved(f"⚠ **Nothing was saved:** {DRAFT_PARTS[conflict[0]]} and {DRAFT_PARTS[conflict[1]]} both "
                            "change the same data. Choose **Stay** and save the one to keep with its own button, or "
                            "**Discard** both.", (), loaded)
    kind, name = loaded.get("kind"), loaded.get("entity")
    order = [part for part in DRAFT_PARTS if part in dirty]
    try:
        body = _draft_body(order, kind, name, _split_draft(values))
        if name is None and "fields" not in body:
            raise gr.Error("A new entry is saved from its fields: give it a name first.")
        target = name or body["fields"]["name"]
        result = API.put(_path(f"/v1/entities/{kind}", target, "draft"), body, create=True if name is None else None)
    except (gr.Error, APIError) as exc:
        raise DraftNotSaved(f"⚠ **Nothing was saved** ({', '.join(DRAFT_PARTS[p] for p in order)}): "
                            f"{_error_text(exc)}  \nNothing was discarded: correct it and **Save** again, **Stay**, "
                            "or **Discard**.", (), loaded) from exc
    return {"kind": kind, "entity": result["name"]}, order


# ---- the preview of an entry's draft (POST /v1/entities/{kind}/{name}/preview: read only) ---------------

PREVIEW_NOTE = ("*Preview of the unsaved draft: nothing is saved, no player's progression changes, nothing is "
                "recorded, no game advances.*")


def _preview_request(loaded, base, values):
    """(kind, entry, body, params, unsaved parts) of the preview of the editor's draft: the parts with
    unsaved changes, as *Save changes* would send them (none: the stored entry)."""
    loaded = dict(loaded or {})
    kind, name = loaded.get("kind"), loaded.get("entity")
    dirty = unsaved_parts(base, *values)
    body = _draft_body([part for part in DRAFT_PARTS if part in dirty], kind, name, _split_draft(values))
    if name is None:
        if "fields" not in body:
            raise gr.Error("A new entry is previewed from its fields: give it a name first.")
        return kind, body["fields"]["name"], body, {"create": True}, dirty
    return kind, name, body, {}, dirty


def entry_preview_markdown(result, dirty=()):
    """The static preview: the content as the draft writes it (no engine)."""
    static = result["static"]
    lines = [f"### 👁 {static['name']}" + (f" — *{static['title']}*" if static.get("title") else "")
             + f" ({static['kind']})", PREVIEW_NOTE,
             ("Unsaved sections shown: " + ", ".join(DRAFT_PARTS[p] for p in dirty) if dirty
              else "No unsaved changes: the entry as stored.")]
    if result.get("problems"):
        lines.append("**⚠ A save would refuse this draft:**\n" + "\n".join(f"- {p}" for p in result["problems"]))
    if static.get("tags"):
        lines.append("**Tags:** " + ", ".join(static["tags"]))
    if static.get("facts"):
        lines.append("**About it:** " + " · ".join(static["facts"]))
    for text in static.get("texts") or []:
        lines.append(f"**{text['label']}:**\n" + "\n".join(f"> {line}  " for line in text["lines"]))  # a line each
    if static.get("dialogue"):
        lines.append("**What they say:**\n" + "\n".join(
            f"- “{d['text']}”" + (f" *(when {d['when']})*" if d.get("when") else "") for d in static["dialogue"]))
    if static.get("actions"):
        lines.append("**Actions:**")
        for action in static["actions"]:
            needs = f" — {'; '.join(action['requirements'])}" if action["requirements"] else ""
            lines.append(f"- **{action['name']}**{needs}")
            for number, outcome in enumerate(action["outcomes"], start=1):
                effects = f" *({'; '.join(outcome['effects'])})*" if outcome["effects"] else ""
                variants = f" (+{outcome['variants']} other wording(s))" if outcome.get("variants") else ""
                lines.append(f"    {number}. {outcome['text'] or '*(no text)*'}{effects}{variants}")
    else:
        lines.append("*No actions.*")
    return "\n\n".join(lines)


def simulated_preview_markdown(result):
    """The simulated preview: a sample scene the engine plays, with its assumptions said first."""
    if result.get("simulated_error"):
        return f"⚠ {result['simulated_error']}"
    sim = result.get("simulated")
    if not sim:
        return "*Press **Simulate a sample scene** to see what the engine makes of the draft.*"
    lines = ["### 🎲 Simulated sample scene", PREVIEW_NOTE,
             "**Sample state (assumptions):**\n" + "\n".join(f"- {a}" for a in sim["assumptions"])]
    if not sim.get("present"):
        lines.append(f"⚠ **{result['name']}** is not in this sample scene: its actions below can be unavailable.")
    lines.append(f"**At {sim['start']}:**\n> " + "  \n> ".join((sim.get("description") or "").splitlines() or ["…"]))
    if sim.get("choices"):
        lines.append("**Choices the player sees:** " + " · ".join(
            (f"**{c['name']}**" if c.get("entry") else c["name"]) + (f" ({c['check']})" if c.get("check") else "")
            for c in sim["choices"]))
    for action in sim.get("actions") or []:
        if not action["offered"]:
            lines.append(f"- **{action['name']}**: not available in this sample state (its requirements, or the "
                         "entry is not here).")
            continue
        check = action.get("check") or {}
        rolled = (f" Check: {check.get('stat')} rolled {check.get('roll')} (needed {check.get('needed')}): "
                  f"{'passed' if check.get('passed') else 'failed'}." if check.get("stat") else "")
        shown = "" if action.get("shown") else " *(not among the choices shown: ranked lower)*"
        changes = f"  \n  Changes: {'; '.join(action['changes'])}" if action.get("changes") else "  \n  Changes: none"
        lines.append(f"- **{action['name']}**{shown}:{rolled}  \n  " + (action.get("text") or "*(nothing told)*")
                     .replace("\n", "  \n  ") + changes)
    return "\n\n".join(lines)


@ui
def preview_entry_ui(loaded, base, *values):
    """👁 Preview the draft: the content as the unsaved draft writes it (read only)."""
    try:
        kind, entry, body, params, dirty = _preview_request(loaded, base, values)
    except gr.Error as exc:
        return f"⚠ The draft cannot be previewed yet: {_error_text(exc)}"
    return entry_preview_markdown(API.post(_path(f"/v1/entities/{kind}", entry, "preview"), body, **params), dirty)


@ui
def simulate_entry_ui(loaded, base, seed, *values):
    """🎲 Simulate a sample scene with the unsaved draft (read only; it takes a few seconds)."""
    try:
        kind, entry, body, params, _ = _preview_request(loaded, base, values)
    except gr.Error as exc:
        return f"⚠ The draft cannot be previewed yet: {_error_text(exc)}"
    return simulated_preview_markdown(API.post(_path(f"/v1/entities/{kind}", entry, "preview"), body, simulate=True,
                                               seed=int(seed or 0), **params))


@ui
def save_changes_ui(loaded, base, *values):
    """💾 Save changes: every section with unsaved changes saved at once, checked whole (an invalid action,
    condition, field, ...: nothing is saved, the draft stays, every problem is named), then the whole entry
    reloaded as stored, so the forms and the YAML show the same entry. Returns (the entry editor, the
    message, the parts to capture)."""
    loaded = dict(loaded or {})
    dirty = unsaved_parts(base, *values)
    if not dirty:
        return (*_entry_update(), "✓ No unsaved changes: every section is saved.", [])
    try:
        now, saved = _save_draft(dirty, loaded, values)
    except DraftNotSaved as failure:
        return (*_entry_update(), failure.message, [])
    kind, name = now["kind"], now["entity"]
    index = (_split_draft(values)["action"][0] or {}).get("index")
    actions = _action_choices(kind, name)
    index = index if index in [value for _, value in actions] else None
    entry = _entry_update(selection=(gr.update(value=kind), gr.Dropdown(choices=_entity_names(kind), value=name)),
                          loaded=(now,), **{part: _part_load(part, kind, name, index if part == "action" else None)
                                            for part in DRAFT_PARTS})
    return (*entry, f"✓ Saved **{name}** at once: {', '.join(DRAFT_PARTS[p] for p in saved)}. Every section now "
                    "shows the entry as stored.", list(DRAFT_PARTS))


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


def form_panels(prefix):
    """A form editor's draft indicator and its question (Save, Discard, Stay), hidden until asked, with
    what it was asked for (built in the Blocks being defined)."""
    status = gr.HTML(form_status_markup(prefix, []), elem_id=f"{prefix}-draft-status")
    with gr.Group(visible=False, elem_id=f"{prefix}-draft-prompt", elem_classes="draft-prompt") as prompt:
        text = gr.Markdown()
        with gr.Row():
            save = gr.Button("💾 Save", variant="primary", elem_id=f"{prefix}-draft-save")
            discard = gr.Button("Discard", variant="stop", elem_id=f"{prefix}-draft-discard")
            stay = gr.Button("Stay", elem_id=f"{prefix}-draft-stay")
    return status, prompt, text, save, discard, stay, gr.State(None)


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


MISSION_FORM_SIZE = 7  # the mission form's components (mission_form_ui's values, without its state)


@ui
def confirm_delete_mission_ui(pending, history=None, state=None, base=None, dirty=None):
    """The confirmed deletion of exactly the mission asked about. When it is the mission in the form, the
    form no longer stands for it (Save would bring it back): it is cleared, or, with unsaved changes, kept
    as a new mission. missions.yaml is reloaded unless it has unsaved changes. Returns (the mission picker,
    the message, missions.yaml, nothing pending, the question closed, Undo's state, bar and text, the
    mission form and its state, forms_base, drafts_dirty, the Missions indicator)."""
    if not pending or pending.get("type") != "mission":
        raise gr.Error("Nothing to delete: press *Delete mission* first.")
    key = pending["key"]
    skip = gr.skip()
    try:
        result = API.delete(_path("/v1/missions", key))
    except APIError as exc:
        return (*_refused(exc, (skip, skip, skip)), *[skip] * (MISSION_FORM_SIZE + 4))
    unsaved = _dirty_of(dirty, "missions")
    reload, context, message = {"values": {}}, skip, result["message"]
    if (state or {}).get("key") == key:
        if "mission" in unsaved:  # the edits stay, as a new mission: Save adds it under the key it then has
            context = {**state, "key": None}
            message += " Your unsaved changes to it stay in the form, as a new mission."
        else:
            *values, context = _mission_form(None)
            reload["values"]["mission"] = tuple(values)
    if "missions_yaml" not in unsaved:
        reload["values"]["missions_yaml"] = (load_missions_yaml(),)
    done = _form_captured("missions", reload, base, dirty, list(reload["values"]))
    form = reload["values"].get("mission") or [skip] * MISSION_FORM_SIZE
    return (gr.Dropdown(choices=list(result["missions"]), value=None), message,
            reload["values"].get("missions_yaml", (skip,))[0], None, *_closed(),
            *_pushed(history, result["undo"], f"🗑 Deleted the mission **{pending['title']}** (`{key}`)."),
            *form, context, done["base"], done["dirty"], done["status"])


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
def undo_delete_mission_ui(history, base=None, dirty=None):
    """**Undo** a deleted mission: it goes back where it was, and opens in the form, unless the mission
    form has unsaved changes (they are kept; the list shows it); missions.yaml is reloaded unless it has
    unsaved changes. Returns (the picker, the mission form and its state, the message, missions.yaml,
    Undo's state, bar and text, forms_base, drafts_dirty, the Missions indicator)."""
    result, history, why = _restore(history)
    if result is None:
        return (gr.skip(), *[gr.skip()] * 8, gr.skip(), gr.skip(), *_history_bar(history, why), gr.skip(), gr.skip(),
                gr.skip())
    key, unsaved = result["name"], _dirty_of(dirty, "missions")
    reload = {"values": {}}
    if "mission" not in unsaved:
        *values, state = mission_form_ui(key)
        reload["values"]["mission"], reload["context"] = tuple(values), state
    if "missions_yaml" not in unsaved:
        reload["values"]["missions_yaml"] = (load_missions_yaml(),)
    done = _form_captured("missions", reload, base, dirty, list(reload["values"]))
    form = reload["values"].get("mission")
    message = result["message"] + ("" if form else " Your unsaved mission changes were kept: pick it to edit.")
    picker = gr.Dropdown(choices=_mission_keys(), value=key) if form else gr.Dropdown(choices=_mission_keys())
    return (picker,
            *(form if form else [gr.skip()] * 7), reload.get("context", gr.skip()), message,
            reload["values"].get("missions_yaml", (gr.skip(),))[0], *_history_bar(history),
            done["base"], done["dirty"], done["status"])


# ---- the entry editor's unsaved parts, kept as they change (each edit sends only its part) ----------

def part_canonical(part, values):
    """One part of the entry draft as it compares (draft_parts, part by part)."""
    values = tuple(values)
    if part == "action":
        return _action_draft(values[0], values[1:])
    if part in ("entry_yaml", "actions_yaml", "policy_yaml"):
        return _plain(values[0])
    return _plain(values)


def _output_value(update):
    """(whether an output sets a value, the value): a skip or an update without a value sets none."""
    if isinstance(update, dict) and update.get("__type__") == "update":
        return ("value" in update), update.get("value")
    if isinstance(update, gr.blocks.Block):  # a component returned as an update: the arguments it was given
        given = update.constructor_args  # (as Gradio applies it: a value only if one was passed)
        return ("value" in given), given.get("value")
    return True, update


def _parts_from_entry(entry):
    """The draft parts an update of the entry editor sets in full ({part: values}); a part it skips is
    not there (its baseline stays)."""
    segments, at = {}, 0
    for name, size in ENTRY_SEGMENTS.items():
        segments[name], at = list(entry[at:at + size]), at + size
    picked = {"fields": segments["fields"], "entry_yaml": segments["entry_yaml"],
              "actions_yaml": segments["actions_yaml"], "policy_yaml": segments["policy_yaml"],
              "action": [segments["action"][1], *segments["action"][2:16], *segments["action"][17:]],
              "scheduling": segments["scheduling"][3:], "details": segments["details"][3:]}
    found = {}
    for part, updates in picked.items():
        values = [_output_value(update) for update in updates]
        if all(has for has, _ in values):
            found[part] = tuple(value for _, value in values)
    return found


def _serial(value):
    if isinstance(value, pd.DataFrame):
        return {"__table__": {"headers": [str(c) for c in value.columns],
                              "data": value.astype(object).where(value.notna(), "").values.tolist()}}
    return value


def _unserial(value):
    if isinstance(value, dict) and "__table__" in value:
        return pd.DataFrame(value["__table__"]["data"], columns=value["__table__"]["headers"])
    return value


def _kept_draft(saved, loaded, part, values):
    """The draft kept in this browser (it survives the tab being closed): {kind, entity, parts: {part:
    values}}; ``values`` None drops the part."""
    loaded = loaded or {}
    saved = dict(saved or {})
    if (saved.get("kind"), saved.get("entity")) != (loaded.get("kind"), loaded.get("entity")):
        saved = {"kind": loaded.get("kind"), "entity": loaded.get("entity"), "parts": {}}
    parts = dict(saved.get("parts") or {})
    if values is None:
        parts.pop(part, None)
    else:
        parts[part] = [_serial(value) for value in values]
    return {**saved, "parts": parts} if parts else {}


def entry_part_status_ui(part, base, dirty, loaded, saved, *values):
    """An edit of one part of the entry draft (only its values travel): whether it now differs from its
    baseline, the indicator, and the draft kept in this browser. Returns (drafts_dirty, indicator, kept)."""
    changed = part in (base or {}) and part_canonical(part, values) != base[part]
    parts = set(_dirty_of(dirty, "entries"))
    (parts.add if changed else parts.discard)(part)
    ordered = [p for p in DRAFT_PARTS if p in parts]
    return ({**(dirty or {}), "entries": ordered}, draft_status_markup(ordered, loaded),
            _kept_draft(saved, loaded, part, values if changed else None))


def entry_baselined(entry, capture, base, dirty, saved, loaded):
    """After a load or a save of the entry editor (``entry``: its update; ``capture``: the parts it
    loaded or saved): those parts' baselines from the very values it sets, in the same step (no round
    trip in which a keystroke could be taken for loaded), and they are no longer unsaved. Returns
    (baseline, drafts_dirty, indicator, kept draft)."""
    parts = _parts_from_entry(entry)
    captured = [part for part in (capture or ()) if part in parts]
    base = dict(base or {})
    base.update({part: part_canonical(part, parts[part]) for part in captured})
    has, now = _output_value(entry[-1])
    loaded = now if has and now else loaded
    left = [p for p in _dirty_of(dirty, "entries") if p not in captured]
    kept = saved or {}
    for part in captured:
        kept = _kept_draft(kept, loaded, part, None) if kept else kept
    if not left:
        kept = {}
    return base, {**(dirty or {}), "entries": left}, draft_status_markup(left, loaded), kept


# ---- a draft kept in the browser: offered back when the studio opens again ---------------------------

def offer_kept_draft_ui(saved):
    """When the studio opens: an entry draft this browser kept (the tab was closed or reloaded with it
    unsaved) is offered back. Returns (what is offered, its panel, its text)."""
    parts = [p for p in (saved or {}).get("parts") or {} if p in DRAFT_PARTS]
    if not parts:
        return None, gr.update(visible=False), ""
    what = ", ".join(DRAFT_PARTS[p] for p in parts)
    return saved, gr.update(visible=True), (
        f"### ↺ Unsaved changes from your last visit\n**{html.escape(str(saved.get('entity') or 'A new entry'))}** "
        f"({html.escape(str(saved.get('kind')))}) had unsaved changes in **{what}** when the page was closed. "
        "**Restore** opens it with them (still unsaved); **Discard** drops them.")


@ui
def restore_kept_draft_ui(offer, base, dirty, loaded, *values):
    """**Restore**: the entry opens as stored, with the kept changes on top (unsaved, so they can be saved
    or discarded). Not over unsaved changes made since. Returns (the entry editor, baseline, drafts_dirty,
    indicator, kept draft, offered nothing, the panel hidden, its text)."""
    if unsaved_parts(base, *values):
        raise gr.Error("Save or discard the changes you made since first.")
    kind, name, kept = (offer or {}).get("kind"), (offer or {}).get("entity"), (offer or {}).get("parts") or {}
    names = _entity_names(kind) if kind else []
    if name and name not in names:
        return (*_entry_update(), base, dirty, gr.skip(), {}, None, gr.update(visible=True),
                f"⚠ **{html.escape(str(name))}** is no longer in {kind}: its kept changes cannot be restored.")
    entry = list(_entry_load(kind, name, names=names))
    loaded_now = {"kind": kind, "entity": name}
    new_base, _, _, _ = entry_baselined(entry, list(DRAFT_PARTS), base, {}, {}, loaded_now)
    at, places = 0, {}
    for segment, size in ENTRY_SEGMENTS.items():
        places[segment] = list(range(at, at + size))
        at += size
    slots = {"fields": places["fields"], "entry_yaml": places["entry_yaml"], "actions_yaml": places["actions_yaml"],
             "policy_yaml": places["policy_yaml"], "scheduling": places["scheduling"][3:], "details": places["details"][3:],
             "action": [places["action"][1], *places["action"][2:16], *places["action"][17:]]}
    restored = []
    for part, values in kept.items():
        if part not in slots or len(values) != len(slots[part]):
            continue
        values = [_unserial(value) for value in values]
        for slot, value in zip(slots[part], values):
            entry[slot] = value
        if part == "action":  # its pickers follow the kept action
            state = values[0] or {}
            entry[places["action"][0]] = gr.Dropdown(choices=_action_choices(kind, name) if name else [],
                                                     value=state.get("index"))
            entry[places["action"][16]] = gr.Dropdown(choices=_outcome_choices(state.get("action") or {}),
                                                      value=state.get("outcome"))
        if part_canonical(part, values) != new_base.get(part):
            restored.append(part)
    ordered = [p for p in DRAFT_PARTS if p in restored]
    return (*entry, new_base, {**(dirty or {}), "entries": ordered}, draft_status_markup(ordered, loaded_now),
            offer if ordered else {}, None, gr.update(visible=False), "")


def discard_kept_draft_ui():
    """**Discard**: the kept changes are dropped. Returns (kept draft, offered nothing, panel, text)."""
    return {}, None, gr.update(visible=False), ""


# ---- the other World editors with a draft: 🎯 Missions, 📜 Story rules, 🗺 Map --------------------------
#
# Each has parts (a form, a YAML editor) with a baseline (forms_base: {editor: {part: canonical}}) set
# in the same step as every load and save, the parts unsaved in drafts_dirty, an indicator, and a
# question (Save, Discard, Stay) when its picker would replace a draft or the user leaves it.

FORM_EDITORS = {
    "missions": {"title": "🎯 Missions", "scope": "mission"},
    "rules": {"title": "📜 Story rules", "scope": "values"},
    "map": {"title": "🗺 Map", "scope": "borders"},
}
NEW_MISSION = "__new_mission__"
# What a part's save makes out of date (reloaded unless it has unsaved changes of its own).
FORM_STALE = {("missions", "mission"): ("missions_yaml",), ("missions", "missions_yaml"): ("mission",),
              ("rules", "variables"): ("values",), ("map", "borders"): ("map_yaml",), ("map", "map_yaml"): ("borders",)}
FORM_CONFLICTS = {"missions": ("mission", "missions_yaml"), "map": ("borders", "map_yaml")}


def _form_parts(editor):
    """{part: (label, number of components)}, in the order of the editor's components."""
    return {"missions": {"mission": ("Mission form", 7), "missions_yaml": ("Missions as YAML", 1),
                         "side_quests_yaml": ("Side quests as YAML", 1)},
            "rules": {"rules": ("Story rules", len(REFERENCE["rule_fields"])), "messages": ("Story messages", 1),
                      "variables": ("Persistent variables", 1), "values": ("Stored values of a profile", 1),
                      "stats": ("Character stats", 1)},
            "map": {"borders": ("Borders", 1), "map_yaml": ("Map as YAML", 1)}}[editor]


def _form_split(editor, values):
    parts, at = {}, 0
    for part, (_, size) in _form_parts(editor).items():
        parts[part], at = tuple(values[at:at + size]), at + size
    return parts


def _form_canonical(values):
    return _plain([_output_value(value)[1] for value in values])


def form_status_markup(editor, parts):
    if parts:
        what = ", ".join(_form_parts(editor)[p][0] for p in parts)
        return f'<div class="draft-status dirty" data-dirty="1">● Unsaved changes: {html.escape(what)}</div>'
    return '<div class="draft-status" data-dirty="0">✓ No unsaved changes</div>'


def form_part_status_ui(editor, part, base, dirty, *values):
    """An edit in one part of a form editor (only its values travel). Returns (drafts_dirty, indicator)."""
    known = ((base or {}).get(editor) or {}).get(part)
    changed = known is not None and _form_canonical(values) != known
    parts = set(_dirty_of(dirty, editor))
    (parts.add if changed else parts.discard)(part)
    ordered = [p for p in _form_parts(editor) if p in parts]
    return {**(dirty or {}), editor: ordered}, form_status_markup(editor, ordered)


def _form_selected(editor, context):
    return (context or {}).get("key") if editor == "missions" else context


def _form_load(editor, part, context):
    """A part as stored: (its values, other outputs: context, view)."""
    if editor == "missions":
        if part == "mission":
            key = _form_selected(editor, context)
            if key and key not in _mission_keys():  # gone with the save that made it stale (missions.yaml)
                key = None
            *values, state = mission_form_ui(key)
            return tuple(values), {"context": state}
        if part == "side_quests_yaml":
            return (load_side_quests_yaml(),), {}
        return (load_missions_yaml(),), {}
    if editor == "rules":
        if part == "rules":
            return tuple(load_rules_ui()), {}
        loader = {"messages": load_messages_yaml, "variables": load_variables_yaml, "stats": load_stats_yaml,
                  "values": lambda: stored_values_yaml(context)}[part]
        return (loader(),), {}
    if part == "borders":  # the places as they are now: a place gone falls back to the first (context too)
        view, picker, borders = map_form_ui(context)
        return (borders,), {"view": view, "picker": picker, "context": _output_value(picker)[1]}
    return (load_document("map"),), {}


def _form_save(editor, part, context, values):
    """Save a part with its own save (a failure raises gr.Error). Returns (message, other outputs)."""
    if editor == "missions":
        if part == "mission":
            picker, message, _ = save_mission_form_ui(context, *values)
            key = _output_value(picker)[1]
            return message, {"context": {**(context or {}), "key": key},
                             "picker": gr.Dropdown(choices=_mission_keys(), value=key)}
        if part == "side_quests_yaml":
            return save_side_quests_yaml(values[0])[1], {}
        return save_missions_yaml(values[0])[1], {"picker": gr.Dropdown(choices=_mission_keys())}
    if editor == "rules":
        if part == "rules":
            return save_rules_ui(*values), {}
        if part == "values":
            return save_stored_values_yaml(context, values[0])[1], {}
        saver = {"messages": save_messages_yaml, "variables": save_variables_yaml, "stats": save_stats_yaml}[part]
        return saver(values[0])[1], {}
    if part == "borders":
        view, message, _ = save_borders_ui(context, values[0])
        return message, {"view": view}
    return save_document("map", values[0])[1], {}


def _form_go(editor, target, context):
    """Carry out a pick: the scope part loaded for ``target`` (a mission, a profile, a place, or a new
    mission). Returns a result with that part's values, the context and the picker."""
    if editor == "missions":
        if target == NEW_MISSION:
            picker, *values, state = new_mission_ui()
            return {"values": {"mission": tuple(values)}, "context": state, "picker": picker}
        *values, state = mission_form_ui(target)
        return {"values": {"mission": tuple(values)}, "context": state, "picker": gr.update(value=target)}
    if editor == "rules":
        return {"values": {"values": (stored_values_yaml(target),)}, "context": target, "picker": gr.update(value=target)}
    view, borders = map_place_ui(target)
    return {"values": {"borders": (borders,)}, "context": target, "view": view, "picker": gr.update(value=target)}


def _form_captured(editor, result, base, dirty, parts):
    """A result whose ``parts`` were just loaded or saved: their baselines from the values it sets."""
    base = {**(base or {})}
    known = dict(base.get(editor) or {})
    for part in parts:
        known[part] = _form_canonical(result["values"][part])
    base[editor] = known
    left = [p for p in _dirty_of(dirty, editor) if p not in parts]
    return {**result, "base": base, "dirty": {**(dirty or {}), editor: left}, "status": form_status_markup(editor, left)}


def _with_stale(editor, saved, context, dirty, result):
    """Reload what the saves made out of date, unless it has unsaved changes. Returns the parts reloaded."""
    reloaded = []
    for part in saved:
        for stale in FORM_STALE.get((editor, part), ()):
            if stale not in saved and stale not in _dirty_of(dirty, editor) and stale not in reloaded:
                values, extras = _form_load(editor, stale, context)
                result["values"][stale] = values
                result.update({k: v for k, v in extras.items() if k != "context" or stale == FORM_EDITORS[editor]["scope"]})
                reloaded.append(stale)
    return reloaded


def form_load_all_ui(editor, context):
    """The editor as stored (the studio opening), with every baseline."""
    result = {"values": {}}
    for part in _form_parts(editor):
        values, extras = _form_load(editor, part, context)
        result["values"][part] = values
        result.update(extras)
    return _form_captured(editor, result, {}, {}, list(_form_parts(editor)))


def form_pick_ui(editor, target, context, base, dirty):
    """The editor's picker changed (a mission, *New mission*, a profile, a place): carried out, unless its
    form has unsaved changes; then the picker goes back and Save, Discard or Stay is asked."""
    scope = FORM_EDITORS[editor]["scope"]
    if scope in _dirty_of(dirty, editor):
        pending = {"type": "pick", "value": target}
        what = "starting a new mission" if target == NEW_MISSION else f"opening {target}"
        text = (f"### ● Unsaved changes\n{FORM_EDITORS[editor]['title']} has unsaved changes in "
                f"**{_form_parts(editor)[scope][0]}**. Save them before {what}? **Discard** drops them; "
                "**Stay** keeps editing.")
        revert = {} if target == NEW_MISSION else {"picker": gr.update(value=_form_selected(editor, context))}
        return {**revert, "prompt": True, "prompt_text": text, "pending": pending}
    return _form_captured(editor, {**_form_go(editor, target, context), "prompt": False, "pending": None},
                          base, dirty, [scope])


def form_save_ui(editor, part, context, base, dirty, *values):
    """A part's own save button: that part (a failure changes nothing), then reloaded as stored, with what
    the save made out of date unless it has unsaved changes of its own."""
    split = _form_split(editor, values)
    message, extras = _form_save(editor, part, context, split[part])
    context = extras.get("context", context)
    result = {"values": {}, **extras}
    loaded, more = _form_load(editor, part, context)
    result["values"][part] = loaded
    result.update(more)
    reloaded = _with_stale(editor, [part], context, dirty, result)
    return {**_form_captured(editor, result, base, dirty, [part, *reloaded]), "message": message}


def form_reload_ui(editor, part, context, base, dirty):
    """*Reload* / *Reload from disk*: that part as stored (its changes dropped)."""
    values, extras = _form_load(editor, part, context)
    return _form_captured(editor, {"values": {part: values}, **extras}, base, dirty, [part])


def form_resolve_ui(editor, choice, pending, context, base, dirty, *values):
    """The editor's question answered: **save** the unsaved parts concerned (each with its own save; a
    failure keeps the drafts and asks again, with why), **discard** them, then carry on (the pick, the
    section, the destination); or **stay**."""
    if not pending or choice == "stay":
        return {"prompt": False, "prompt_text": "", "pending": None}
    scope = [FORM_EDITORS[editor]["scope"]] if pending["type"] == "pick" else list(_form_parts(editor))
    parts = [p for p in _dirty_of(dirty, editor) if p in scope]
    result, saved = {"values": {}}, []
    if choice == "save":
        conflict = FORM_CONFLICTS.get(editor)
        if conflict and all(p in parts for p in conflict):
            labels = " and ".join(_form_parts(editor)[p][0] for p in conflict)
            return {"prompt": True, "prompt_text": f"⚠ **Nothing was saved:** {labels} both change the same data. "
                    "Choose **Stay** and save the one to keep with its own button, or **Discard** both."}
        split = _form_split(editor, values)
        for part in parts:
            try:
                message, extras = _form_save(editor, part, context, split[part])
            except gr.Error as exc:
                done = {p: split[p] for p in saved}
                partial = _form_captured(editor, {"values": done}, base, dirty, saved) if saved else {}
                return {**partial, "values": {}, "prompt": True, "prompt_text":
                        f"⚠ **{_form_parts(editor)[part][0]} could not be saved:** {_error_text(exc)}  \nNothing was "
                        "discarded: correct it and **Save** again, **Stay**, or **Discard**."}
            context = extras.get("context", context)
            result.update({k: v for k, v in extras.items() if k != "context"})
            saved.append(part)
            result["message"] = message
    for part in parts:  # saved or discarded: as stored now
        loaded, extras = _form_load(editor, part, context)
        result["values"][part] = loaded
        result.update(extras)
    captured = list(parts) + _with_stale(editor, saved, context, dirty, result)
    if pending["type"] == "pick":
        result.update({k: v for k, v in _form_go(editor, pending["value"], result.get("context", context)).items()
                       if k != "values"})
        result["values"].update(_form_go(editor, pending["value"], result.get("context", context))["values"])
        captured.append(FORM_EDITORS[editor]["scope"])
    elif pending["type"] == "destination":
        result["nav"] = navigate(pending["id"])
    elif pending["type"] == "section":
        result["section"], result["section_now"] = pending["id"], pending["id"]
    return _form_captured(editor, {**result, "prompt": False, "prompt_text": "", "pending": None}, base, dirty,
                          list(dict.fromkeys(captured)))


# Before the page is closed or reloaded with unsaved changes, the browser asks (launch(head=...)).
DRAFT_HEAD = """<script>
window.addEventListener("beforeunload", (event) => {
  if (document.querySelector('.draft-status[data-dirty="1"]')) { event.preventDefault(); event.returnValue = ""; }
});
// The banner's highlight, at once: a destination chosen in the banner or the menu is marked pending
// (data-pending, styled as the highlight: NAV_PENDING_CSS) before the server answers, unless an editor
// has unsaved changes (its guard may keep the page where it is: the answer then decides). The buttons'
// own classes stay Gradio's; the server's answer sets them and clears the mark (or, unanswered, 10 s).
window.aneNavShow = (key) => {
  const banner = document.getElementById("ane-banner");
  if (!banner || document.querySelector('.draft-status[data-dirty="1"]')) return;
  banner.dataset.pending = key;
  clearTimeout(window.aneNavPending);
  window.aneNavPending = setTimeout(() => { delete banner.dataset.pending; }, 10000);
};
// The address follows the destination open, however it was opened (the banner, the menu, a guard's
// answer, ▶ Play this result...): ?section=<destination>, so a reload or a copied link opens it again.
(function followTheBanner() {
  const banner = document.getElementById("ane-banner");
  if (!banner) { setTimeout(followTheBanner, 500); return; }
  const write = () => {
    delete banner.dataset.pending;  // the server has answered: its highlight is the one shown
    const on = banner.querySelector('button.primary[id^="nav-"]');
    if (!on) return;
    const key = on.id.slice(4), url = new URL(window.location.href), now = url.searchParams.get("section");
    if (now === key || (!now && key === "world")) return;
    url.searchParams.set("section", key);
    history.replaceState(history.state, "", url);
  };
  new MutationObserver(write).observe(banner, {subtree: true, childList: true, attributes: true,
                                               attributeFilter: ["class"]});
  write();
})();
</script>"""


def nav_show_js(key=None):
    """The client-side half of a banner click (key) or a menu choice (key None: its first input): the
    highlight at once (window.aneNavShow, DRAFT_HEAD); the inputs go on to the server unchanged."""
    shown = json.dumps(key) if key else "args[0]"
    return f"(...args) => {{ window.aneNavShow && window.aneNavShow({shown}); return args; }}"


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


def sync_proposal_ui(proposal_text, ticked, offered):
    """The proposal edited: the ticks follow its entries. Entries still there keep their tick; an entry new
    to it (added, or renamed in the YAML) is ticked, as the proposal's entries are; YAML that does not read
    leaves the ticks as they are. Returns (the ticks, the entries offered)."""
    try:
        proposal = yaml.safe_load(proposal_text or "") or {}
    except yaml.YAMLError:
        return gr.skip(), gr.skip()
    if not isinstance(proposal, dict):
        return gr.skip(), gr.skip()
    choices = _proposal_choices(proposal)
    ticked, offered = set(ticked or []), set(offered or [])
    return gr.update(choices=choices, value=[c for c in choices if c in ticked or c not in offered]), choices


@ui
def add_proposal_ui(proposal_text, ticked):
    """Add the ticked entries of the (edited) proposal to the world; the world is checked first."""
    proposal = from_yaml(proposal_text, "The proposal") or {}
    if not isinstance(proposal, dict):
        raise gr.Error("The proposal must be a mapping with locations, characters and objects.")
    ticked = set(ticked or [])
    missing = sorted(ticked - set(_proposal_choices(proposal)))
    if missing:  # a tick for an entry renamed or removed in the YAML: never left out silently
        raise gr.Error("Ticked but no longer in the proposal (renamed or removed in the YAML?): " + ", ".join(missing)
                       + ". Tick the entries as they are named now.")
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


def _mission_form(key):
    """The mission form for ``key`` (None or "": empty) and its state; APIError as the API answers."""
    mission = API.get(_path("/v1/missions", key))["mission"] if key else {}
    stages = mission.get("stages") or ([{"title": mission.get("title", ""), "description": "",
                                         "goal": mission.get("goal") or {}, "rewards": []}] if mission else [])
    table = pd.DataFrame([[stage.get("title", ""), stage.get("description", ""), _pairs(stage.get("goal")),
                           _rewards_text(stage.get("rewards"))] for stage in stages], columns=STAGE_COLUMNS)
    weight = mission.get("weight")
    return (key or "", mission.get("title", ""), mission.get("description", ""), 1.0 if weight is None else float(weight),
            _flow(mission.get("enabled_if")), _rewards_text(mission.get("rewards")), table,
            {"key": key or None, "staged": bool(mission.get("stages")), "mission": mission})


mission_form_ui = ui(_mission_form)


def new_mission_ui():
    table = pd.DataFrame([["First step", "", "location: Market", ""]], columns=STAGE_COLUMNS)
    return (gr.Dropdown(value=None), "new_mission", "A new quest", "", 1.0, "", "", table,
            {"key": None, "staged": True, "mission": {}})


@ui
def save_mission_form_ui(state, key, title, description, weight, enabled_if, rewards, table):
    state = dict(state or {})
    old, new = state.get("key"), str(key or "").strip()
    if not new:
        raise gr.Error("Give the mission a key: letters, digits and _ (e.g. rid_the_market_of_the_thief).")
    if new != old and new in _mission_keys():  # a new mission, or a rename, never replaces another mission
        raise gr.Error(f"A mission `{new}` exists already: choose another key, or pick it from the list to edit it.")
    mission = dict(state.get("mission") or {})
    stored_title = mission.get("title")
    mission.update(title=str(title or "").strip(), description=str(description or "").strip())
    weight = 1.0 if weight is None or weight == "" else float(weight)  # 0 is a weight (never drawn), not "unset"
    _set(mission, "weight", None if weight == 1.0 else weight)
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
    # One step that carries nothing but its goal is a one-goal mission; a step with its own title (other than the
    # mission's), a description or rewards is kept as a step, so nothing written in the table is lost.
    first = stages[0]
    goal_only = (len(stages) == 1 and not first["description"] and "rewards" not in first
                 and first["title"] in ("Step 1", mission["title"], stored_title))
    if state.get("staged") or not goal_only:
        mission["stages"] = stages
        mission.pop("goal", None)
    else:
        mission["goal"] = first["goal"]
        mission.pop("stages", None)
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
    if place not in (API.get("/v1/map").get("places") or []):
        raise gr.Error(f"The place `{place}` is no longer in the world: pick a place from the list.")
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


BEHAVIOUR_GROUPS = {"npcs": "the people's rules", "locations": "the places' rules"}


def behaviour_status_markup(dirty):
    """The behaviour tables' unsaved changes (a draft-status: the browser asks before the page is left)."""
    dirty = [group for group in BEHAVIOUR_GROUPS if group in (dirty or [])]
    if dirty:
        what = " and ".join(BEHAVIOUR_GROUPS[group] for group in dirty)
        return f'<div class="draft-status dirty" data-dirty="1">● Unsaved changes: {html.escape(what)}</div>'
    return '<div class="draft-status" data-dirty="0">✓ No unsaved changes</div>'


def behaviour_load_ui():
    """Both tables as stored, as the baseline of their drafts. Returns (the six table values, draft, status)."""
    values = behaviour_tables_ui()
    draft = {"base": {"npcs": _plain(list(values[:3])), "locations": _plain(list(values[3:]))}, "dirty": []}
    return (*values, draft, behaviour_status_markup([]))


def behaviour_edit_ui(group, draft, *values):
    """An edit in one table: unsaved when it differs from the table as loaded or saved."""
    draft = {"base": dict((draft or {}).get("base") or {}), "dirty": list((draft or {}).get("dirty") or [])}
    known = draft["base"].get(group)
    changed = known is not None and _plain(list(values)) != known
    dirty = set(draft["dirty"])
    (dirty.add if changed else dirty.discard)(group)
    draft["dirty"] = [g for g in BEHAVIOUR_GROUPS if g in dirty]
    return draft, behaviour_status_markup(draft["dirty"])


def behaviour_reload_ui(group, draft):
    """After ``group``'s save: only its table reloads (the other keeps its edits). Returns (its three values,
    the draft, its status)."""
    values = behaviour_tables_ui()
    mine = values[:3] if group == "npcs" else values[3:]
    draft = {"base": dict((draft or {}).get("base") or {}), "dirty": list((draft or {}).get("dirty") or [])}
    draft["base"][group] = _plain(list(mine))
    draft["dirty"] = [g for g in draft["dirty"] if g != group]
    return (*mine, draft, behaviour_status_markup(draft["dirty"]))


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


# ---- ⚙ Advanced → App & jobs: when the app parameters take effect -----------------------------------------
#
# The API reads its parameters when it starts, the studio its own when it starts: a save changes nothing
# that is running. The studio's launch settings come from its own environment first (ANE_UI_HOST,
# ANE_UI_PORT, ANE_UI_SHARE); only the studio can see that environment, so it says so itself.

UI_ENVIRONMENT = {"server_host": "ANE_UI_HOST", "server_port": "ANE_UI_PORT", "share": "ANE_UI_SHARE"}
STUDIO_PARAMETERS = ("candidate_limit", "generation_limit", "simulation_step_limit", "queue_concurrency",
                     "queue_max_size", "server_host", "server_port", "share")
STUDIO_RUNNING = {}  # {parameter: (value, source)} this studio runs with: built (limits) and launched (__main__)
STUDIO_LAUNCHED = False


def studio_environment(key, environ=None):
    """(variable, value as the studio uses it) when the studio's environment sets ``key``, else None."""
    name = UI_ENVIRONMENT.get(key)
    environ = os.environ if environ is None else environ
    raw = (environ.get(name) or "").strip() if name else ""
    if not raw:
        return None
    if key == "server_port":
        return (name, int(raw)) if raw.isdigit() else (name, raw)
    if key == "share":
        return name, raw.lower() in {"1", "true", "yes"}
    return name, raw


def studio_launch_values(saved, environ=None):
    """What the studio launches with: {parameter: (value, source)}, the source the environment variable
    that set it, or "saved" (config/ui_settings.yml as the API had it when the studio started)."""
    values = {}
    for key in STUDIO_PARAMETERS:
        env = studio_environment(key, environ)
        values[key] = (env[1], env[0]) if env else (saved[key], "saved")
    return values


def app_activation_markdown(state, running=None, launched=None, environ=None):
    """App & jobs: each parameter saved and running (in the API, in this studio), when a saved value
    takes effect, and a pending-restart notice where they differ (GET /v1/app-settings for the API's
    side; the studio's own values and environment for the studio's)."""
    running = STUDIO_RUNNING if running is None else running
    launched = STUDIO_LAUNCHED if launched is None else launched
    rows, api_pending, studio_pending, env_notes = [], [], [], []
    for row in state["parameters"]:
        key, saved, readers = row["key"], row["saved"], row["readers"]
        api_now = f"`{row['running_api']!r}`" if "API" in readers else "—"
        studio_now, status = "—", []
        if row.get("pending"):
            api_pending.append(key)
            status.append("⏳ restart the API")
        if "studio" in readers:
            value, source = running.get(key, (None, None))
            studio_now = f"`{value!r}`" + (f" (from {source})" if source not in (None, "saved") else "")
            if source not in (None, "saved"):
                if value != saved:
                    env_notes.append(f"**{source}** is set in the studio's environment ({value!r}): the studio uses it "
                                     f"instead of the saved {key} ({saved!r}), now and after a restart, until it is "
                                     "unset.")
                status.append(f"set by {source}")
            elif value != saved:
                studio_pending.append(key)
                status.append("⏳ restart the studio")
        rows.append(f"| {key} | `{saved!r}` | {api_now} | {studio_now} | {row['takes_effect']} | "
                    f"{' · '.join(status) or '✓ in use'} |")
    for key, name in UI_ENVIRONMENT.items():  # set, but equal to the saved value: still worth knowing
        env = studio_environment(key, environ)
        if env and not any(name in note for note in env_notes):
            env_notes.append(f"**{name}** is set in the studio's environment ({env[1]!r}): it decides {key}, "
                             "whatever is saved here, until it is unset.")
    if api_pending or studio_pending:
        notice = "⏳ **Restart pending.** " + " ".join(part for part in (
            f"The API runs other values than the saved ones for {', '.join(api_pending)}: restart the API." if api_pending
            else "",
            f"This studio runs other values than the saved ones for {', '.join(studio_pending)}: restart the studio."
            if studio_pending else "") if part)
    else:
        notice = "✓ **Every saved value is in use** (or set by the studio's environment, as said below)."
    if not launched:
        notice += " (This studio was not started as a server here: its launch values are those it would use.)"
    table = ["| Parameter | Saved | Running in the API | Running in this studio | Takes effect after | Now |",
             "|---|---|---|---|---|---|", *rows]
    return "\n\n".join([notice, "\n".join(table), *env_notes])


def app_parameter_info(key, readers):
    """The note under an app parameter's field: who reads it (when it starts), and its environment variable."""
    who = " and the studio" if len(readers) > 1 else ""
    text = f"Read by the {readers[0]}{who} when it starts: saving changes nothing running until a restart."
    env = studio_environment(key)
    if env:
        text += f" {env[0]} is set ({env[1]!r}) and is used instead."
    elif key in UI_ENVIRONMENT:
        text += f" {UI_ENVIRONMENT[key]}, if set in the studio's environment, is used instead."
    return text


@ui
def app_activation_ui():
    """App & jobs, on loading: when each parameter takes effect (GET /v1/app-settings)."""
    return app_activation_markdown(API.get("/v1/app-settings"))


@ui
def save_app_parameters(*values):
    """Save the app parameters (PUT /v1/app-settings): the API's confirmation (nothing running changes;
    what a restart of the API would change), what a restart of this studio would change, and what its
    environment keeps as it is. Returns (the confirmation, the activation table)."""
    state = API.put("/v1/app-settings", {"settings": dict(zip(APP_PARAMETER_KEYS, values))})
    saved = state["settings"]
    studio_changes, env_kept = [], []
    for key in STUDIO_PARAMETERS:
        value, source = STUDIO_RUNNING.get(key, (None, None))
        if source not in (None, "saved"):
            if value != saved[key]:
                env_kept.append(f"{source} is set ({value!r}): this studio keeps using it instead of the saved {key} "
                                f"({saved[key]!r}), after a restart too, until it is unset.")
        elif value != saved[key]:
            studio_changes.append(f"{key} {value!r} → {saved[key]!r}")
    message = state["message"]
    message += (" A restart of the studio will use: " + ", ".join(studio_changes) + "." if studio_changes
                else " This studio already runs every value it reads.")
    return " ".join([message, *env_kept]), app_activation_markdown(state)


APP_PARAMETER_KEYS = []
REFERENCE = {}

class CompressPages:
    """gzip for the studio's own pages and data, which have no file extension (the page, /config: about
    2 MB each, sent uncompressed: Gradio's Brotli compresses only paths with a file extension, .js,
    .css, ...; those it keeps compressing). Starlette's gzip leaves event streams (text/event-stream)
    as they are."""

    def __init__(self, app, minimum_size=1024):
        from starlette.middleware.gzip import GZipMiddleware

        self.app, self.gzip = app, GZipMiddleware(app, minimum_size=minimum_size)

    async def __call__(self, scope, receive, send):
        last = str(scope.get("path", "")).rsplit("/", 1)[-1]
        if scope.get("type") == "http" and "." not in last:
            return await self.gzip(scope, receive, send)
        return await self.app(scope, receive, send)


# Components that never show an event's progress (no status overlay of their own).
NO_PROGRESS_SHOWN = ("State", "BrowserState", "Timer", "Tabs", "Tab", "Group", "Row", "Column", "Accordion", "Form")


def lighten_loading_status(demo):
    """Keep the page's loading-status bookkeeping small. Gradio's page keeps a loading status for every
    component an event has ever shown progress on (its outputs, or show_progress_on), and on every status
    message of any event it updates all of them: with this page's 1,500 components that took nearly all
    of the browser's time. Nothing an event does changes: an event whose progress is hidden shows it on
    nothing (it had no overlay to show); any other shows it on its outputs that can show it (not its
    states, timers or layout blocks)."""
    for fn in demo.fns.values():
        if fn.show_progress_on is not None:
            continue
        if fn.show_progress == "hidden":
            fn.show_progress_on = []
            continue
        shown = [o for o in fn.outputs if type(o).__name__ not in NO_PROGRESS_SHOWN]
        if len(shown) != len(fn.outputs):
            fn.show_progress_on = shown


def build_app():
    global REFERENCE, APP_PARAMETER_KEYS
    REFERENCE = API.get("/v1/reference")
    app_settings = API.get("/v1/app-settings")
    limits, app_defaults = app_settings["settings"], app_settings["defaults"]
    APP_PARAMETER_KEYS = list(limits)
    if not STUDIO_LAUNCHED:  # what this studio runs with (its sliders now; its launch settings when it starts)
        STUDIO_RUNNING.update(studio_launch_values(limits))
    parameter_readers = {row["key"]: row["readers"] for row in app_settings.get("parameters") or []}
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
        drafts_dirty = gr.State({})  # every World editor's unsaved parts, {editor: [parts]}: what the guards read
        forms_base = gr.State({})  # 🎯 Missions, 📜 Story rules, 🗺 Map: their baselines, {editor: {part: ...}}
        section_now = gr.State("entries")  # the World section open
        saved_draft = gr.BrowserState({}, storage_key="ane-entry-draft")  # the entry draft, kept in this browser
        # 🧠 Train's completed engine, kept in this browser so that a reload shows it again (restore_result_ui).
        # Defined here, outside the destinations: the browser restores it before the page's load events.
        saved_result = gr.BrowserState(None, storage_key="ane-train-result")
        # The training and the evaluation this browser submitted and has not seen end: followed again after a
        # reload or a lost connection (never another job, never one this browser did not submit).
        train_job = gr.BrowserState(None, storage_key="ane-train-job")
        evaluate_job = gr.BrowserState(None, storage_key="ane-evaluate-job")
        # ▶ Play's game (its session id), kept in this browser so that a reload goes on with it (restore_game_ui).
        saved_game = gr.BrowserState("", storage_key="ane-play-session")
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
                # The World's sub-navigation (the sections' own tab bar is not shown, as the destinations').
                with gr.Row(elem_id="world-nav", elem_classes="world-nav"):
                    world_nav_buttons = [gr.Button(label, variant="primary" if key == "content" else "secondary",
                                                   size="sm", elem_id=f"world-nav-{key}")
                                         for key, (label, _) in WORLD_GROUPS.items()]
                with gr.Row(elem_id="world-content-nav", elem_classes="world-content-nav") as world_content_nav:
                    content_nav_buttons = [gr.Button(WORLD_SECTIONS[key], size="sm",
                                                     variant="primary" if key == "entries" else "secondary",
                                                     elem_id=f"world-content-{key}") for key in CONTENT_SECTIONS]
                content_last = gr.State("entries")  # Content's last section open (📚 Content goes back to it)
                with gr.Tabs(selected="entries", elem_id="world-sections") as world_sections:
                    with gr.Tab(WORLD_SECTIONS["entries"], id="entries") as section_entries:
                        # The content library: every location, NPC, item and event, searchable; choosing one opens
                        # it below (guarded like the pickers); searching or filtering never changes what is open.
                        library_filters, library_found, library_selected, library_said = library_view(
                            "", LIBRARY_ALL, {"kind": "Locations", "entity": selected_name})
                        with gr.Accordion("📚 Content library: search every location, NPC, item and event", open=True,
                                          elem_id="library"):
                            with gr.Row(equal_height=True):
                                library_search = gr.Textbox(label="Search", scale=5, elem_id="library-search",
                                                            placeholder="Names, tags, descriptions… (every word must match)")
                                library_refresh = gr.Button("↻ Refresh", scale=1, elem_id="library-refresh")
                            library_filter = gr.Radio(library_filters, value=LIBRARY_ALL, label="Type",
                                                      elem_id="library-filter")
                            library_status = gr.Markdown(library_said, elem_id="library-status")
                            library_results = gr.Radio(library_found, value=library_selected, label="Entries",
                                                       elem_id="library-results", elem_classes="library-results")
                            library_total = sum(len(e) for e in library_entries().values())
                            library_more = gr.Button(f"Show {min(LIBRARY_LIMIT, library_total - len(library_found))} more",
                                                     visible=library_total > len(library_found), size="sm",
                                                     elem_id="library-more")
                            library_shown = gr.State(LIBRARY_LIMIT)  # how many results are listed (a page more each time)
                            library_revision = gr.State(None)  # the world's revision the list was made from
                            library_timer = gr.Timer(LIBRARY_WATCH)  # a look for changes made elsewhere
                        with gr.Row():
                            kind = gr.Radio(REFERENCE["entity_kinds"], value="Locations", label="Content type")
                            entity = gr.Dropdown(label="Entry", choices=names, value=selected_name)
                            add_entry = gr.Button("New entry")
                        # The draft: compared with what was last loaded or saved (draft_base, part by part).
                        draft_status = gr.HTML(draft_status_markup([], None), elem_id="draft-status")
                        with gr.Row(equal_height=True, elem_classes="entry-save-row"):
                            save_changes = gr.Button("💾 Save changes", variant="primary", scale=0, min_width=180,
                                                     elem_id="entry-save-changes")
                            save_changes_status = gr.Markdown(elem_id="entry-save-changes-status")
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
                        # A draft kept in this browser when the page was closed: offered back.
                        with gr.Group(visible=False, elem_id="draft-offer", elem_classes="draft-prompt") as draft_offer_group:
                            draft_offer_text = gr.Markdown()
                            with gr.Row():
                                draft_offer_restore = gr.Button("↺ Restore", variant="primary", elem_id="draft-offer-restore")
                                draft_offer_discard = gr.Button("Discard", elem_id="draft-offer-discard")
                        draft_offer = gr.State(None)
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
                                entry_condition = condition_builder_panel("entry-condition", "Enabled if")
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
                                action_condition = condition_builder_panel("action-condition", "Offered only if")
                                until_condition = condition_builder_panel("action-until", "No longer offered once")
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
                                # Places and people have no scheduling rules (their behaviour comes from tags):
                                # Save and the YAML editor are shown for events and items only (policy_controls_ui).
                                with gr.Row(visible=False) as policy_actions:
                                    save_policy_form = gr.Button("Save", variant="primary")
                                policy_status = gr.Markdown()
                                with gr.Accordion("As YAML (advanced)", open=False, visible=False) as policy_yaml_box:
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
                                    with gr.Row():
                                        dialogue_line = gr.Dropdown([], value=None, label="A line's condition", scale=1,
                                                                    elem_id="dialogue-line",
                                                                    info="Choose a line to build its condition.")
                                        dialogue_condition_text = gr.Textbox(
                                            label="Said when (this line's condition, empty: always)", scale=3,
                                            elem_id="dialogue-condition-text",
                                            placeholder="e.g. has_item: Ancient Map")
                                    dialogue_condition = condition_builder_panel("dialogue-condition", "Said when")
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
                        # The preview of the draft (read only): the content as written, and a simulated scene.
                        with gr.Accordion("👁 Preview the draft (nothing is saved)", open=False, elem_id="entry-preview"):
                            with gr.Tabs():
                                with gr.Tab("Content (static)"):
                                    preview_button = gr.Button("👁 Preview the draft", elem_id="entry-preview-button")
                                    preview_static = gr.Markdown(elem_id="entry-preview-static")
                                with gr.Tab("Simulated scene"):
                                    gr.Markdown("The engine plays a sample scene with the draft, in a separate process: "
                                                "its sample state (start, storyteller, character, variables) is listed "
                                                "with the result. It takes a few seconds.", elem_classes="lead")
                                    with gr.Row(equal_height=True):
                                        preview_seed = gr.Number(value=0, precision=0, minimum=0, label="Seed", scale=1)
                                        simulate_button = gr.Button("🎲 Simulate a sample scene", scale=3,
                                                                    elem_id="entry-simulate-button")
                                    preview_simulated = gr.Markdown(elem_id="entry-preview-simulated")

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
                        (missions_draft_status, missions_prompt, missions_prompt_text, missions_save, missions_discard,
                         missions_stay, missions_pending_state) = form_panels("missions")
                        with gr.Group():
                            with gr.Row():
                                mis_key = gr.Textbox(label="Key", info="letters, digits and _")
                                mis_title = gr.Textbox(label="Title", scale=2)
                                mis_weight = gr.Number(value=1.0, minimum=0, label="Weight")
                            mis_description = gr.Textbox(label="Description", lines=2)
                            with gr.Row():
                                mis_enabled = gr.Textbox(label="Offered only if", placeholder="e.g. thief_caught: false")
                                mission_condition = condition_builder_panel("mission-condition", "Offered only if")
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
                        with gr.Accordion("🧭 Side quests: optional quests besides the mission", open=False,
                                          elem_id="world-side-quests"):
                            gr.Markdown("Written like a mission (title, description, goal or stages, rewards, "
                                        "enabled_if), plus `giver` (the NPC who offers it: it starts once the player "
                                        "has dealt with them), `start` (a condition), `fail_if`, `on_fail` and "
                                        "`repeatable`. Several run at once; none ends the story. See docs/WORLD.md.")
                            side_quests_editor = gr.Code(value=load_side_quests_yaml(), language="yaml",
                                                         label="side_quests.yaml")
                            with gr.Row():
                                save_side_quests_button = gr.Button("Validate & save side quests")
                                reload_side_quests_button = gr.Button("Reload from disk")

                    with gr.Tab(WORLD_SECTIONS["story-rules"], id="story-rules") as section_story_rules:
                        gr.Markdown(REFERENCE["activation"]["configuration"], elem_classes="activation-note")  # rules, messages, variables, stats
                        rules_profile = gr.State(profiles["default"])  # the profile whose stored values are shown
                        (rules_draft_status, rules_prompt, rules_prompt_text, rules_save, rules_discard, rules_stay,
                         rules_pending_state) = form_panels("rules")
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
                            # Resetting cannot be undone: it is asked first (reset_question_ui).
                            with gr.Group(visible=False) as reset_question:
                                reset_question_text = gr.Markdown()
                                with gr.Row():
                                    reset_values_confirm = gr.Button("Reset this profile", variant="stop")
                                    reset_values_cancel = gr.Button("Cancel")
                            values_status = gr.Markdown()
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
                    with gr.Tab(WORLD_SECTIONS["map"], id="map") as section_map:
                        map_selected = gr.State(places_now[0] if places_now else None)  # the place whose borders show
                        (map_draft_status, map_prompt, map_prompt_text, map_save, map_discard, map_stay,
                         map_pending_state) = form_panels("map")
                        gr.Markdown("### 🗺 The authoring map: the whole world\nEvery place and every route of this "
                                    "world, as authored: what every story's map is made from. A player never sees it "
                                    "whole: ▶ Play shows each game's **discovered map** (the places that player has "
                                    "found, and the ways to them), which grows as the story goes.", elem_classes="lead")
                        gr.Markdown("Pick a place and tick the places it borders: a route holds both ways (saving "
                                    "a border updates both places), and a far place is reached through the places "
                                    "between. The drawing shows every route.")
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
                        behaviour_status = gr.HTML(behaviour_status_markup([]))
                        behaviour_draft = gr.State({})

                    with gr.Tab(WORLD_SECTIONS["grow"], id="grow") as section_grow:
                        gr.Markdown("The augmentation model (**Qwen3.5-4B** on vLLM, its own: the judge stays Gemma 4) proposes "
                                    "new **places, people and items** that fit this world (its places and tags). Nothing is "
                                    "written until you add them: each must be new, and the whole world is checked with them in "
                                    "place. The targets are `augmentation.min_*` (⚙ Advanced → Engine logic).")
                        augment_tick = gr.Checkbox(False, label="Augment the world with the language model",
                                                   elem_id="grow-augment",
                                                   info="Saved (augmentation.enabled). Off: nothing is proposed, here or by "
                                                        "python main.py.")
                        with gr.Row():
                            world_size = gr.Dataframe(label="The world's size", interactive=False, scale=2)
                            with gr.Column(scale=1):
                                grow_llm_status = gr.Markdown()
                                propose_button = gr.Button("✨ Propose new entries", variant="primary")
                        proposal_pick = gr.CheckboxGroup([], label="Add these")
                        proposal_offered = gr.State([])  # the entries the ticks were offered for (sync_proposal_ui)
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
                                 draft_message, section_now, draft_capture]
                guard_inputs = [draft_loaded, draft_base, *draft_inputs]

                def tracked(fn, inputs, outputs, keep_saved=False):
                    """A handler of the entry editor whose output at draft_capture names the parts it loaded or
                    saved: their baselines are set from the values it sets, in the same step (entry_baselined),
                    with the unsaved parts, the indicator and the draft kept in this browser."""
                    at = outputs.index(draft_capture)

                    def run(dirty, saved, base, loaded, *args):
                        out = list(fn(*args))
                        new_base, new_dirty, indicator, kept = entry_baselined(out[:ENTRY_OUTPUTS], out[at], base, dirty,
                                                                               saved, loaded)
                        out[at] = new_base
                        return (*out, new_dirty, indicator, *(() if keep_saved else (kept,)))

                    run.__name__ = getattr(fn, "__name__", "run")  # the handler it runs (not functools.wraps: its signature)
                    return (run, [drafts_dirty, saved_draft, draft_base, draft_loaded, *inputs],
                            [*outputs[:at], draft_base, *outputs[at + 1:], drafts_dirty, draft_status,
                             *(() if keep_saved else (saved_draft,))])

                # On loading the page (before anything can be typed): its progress on the editor's status line,
                # not on its 80 fields (lighten_loading_status).
                demo.load(*tracked(load_selection_ui, [kind, entity], [*entry_outputs, draft_capture], keep_saved=True),
                          show_progress_on=[draft_status])
                demo.load(offer_kept_draft_ui, saved_draft, [draft_offer, draft_offer_group, draft_offer_text])
                draft_offer_restore.click(restore_kept_draft_ui, [draft_offer, draft_base, drafts_dirty, draft_loaded,
                                                                  *draft_inputs],
                                          [*entry_outputs, draft_base, drafts_dirty, draft_status, saved_draft, draft_offer,
                                           draft_offer_group, draft_offer_text])
                draft_offer_discard.click(discard_kept_draft_ui, None,
                                          [saved_draft, draft_offer, draft_offer_group, draft_offer_text])
                # The user's choices: carried out at once, or, with unsaved changes, undone and asked about.
                kind.input(*tracked(entry_choice_ui, [kind, entity, *guard_inputs], draft_outputs))
                # The library: a search, a filter or a change in the world (a load, save, rename, delete, undo:
                # every one sets the draft's baseline) lists it again; choosing an entry opens it, guarded.
                library_inputs = [library_search, library_filter, draft_loaded]
                library_outputs = [library_filter, library_results, library_status, library_more, library_shown]
                gr.on([library_search.input, library_filter.input], library_search_ui, library_inputs, library_outputs,
                      trigger_mode="always_last", show_progress="hidden")
                gr.on([library_refresh.click, draft_base.change, draft_loaded.change, section_entries.select],
                      library_ui, [*library_inputs, library_shown], library_outputs, trigger_mode="always_last",
                      show_progress="hidden")
                library_more.click(library_more_ui, [*library_inputs, library_shown], library_outputs,
                                   show_progress="hidden")
                # Changes made elsewhere (another browser, a tool, an import, by hand): seen by the world's
                # revision, now and then; the list follows without a click.
                gr.on([library_timer.tick, demo.load], library_watch_ui,
                      [library_revision, *library_inputs, library_shown], [*library_outputs, library_revision],
                      trigger_mode="always_last", show_progress="hidden")
                library_results.input(*tracked(library_choice_ui, [library_results, *guard_inputs], draft_outputs)).then(
                    library_ui, [*library_inputs, library_shown], library_outputs, show_progress="hidden")
                entity.input(*tracked(entry_choice_ui, [kind, entity, *guard_inputs], draft_outputs))
                add_entry.click(*tracked(new_entry_choice_ui, guard_inputs, draft_outputs))
                act_pick.input(*tracked(action_choice_ui, [act_pick, *guard_inputs], draft_outputs))
                act_new.click(*tracked(new_action_choice_ui, guard_inputs, draft_outputs))
                for button, choice in ((draft_save, "save"), (draft_discard, "discard"), (draft_stay, "stay")):
                    button.click(*tracked(lambda pending, *rest, choice=choice: resolve_draft_ui(choice, pending, *rest),
                                          [draft_pending, *guard_inputs], draft_outputs))
                # The preview: the editor's draft as it is now (read only).
                preview_button.click(preview_entry_ui, guard_inputs, preview_static)
                simulate_button.click(simulate_entry_ui, [draft_loaded, draft_base, preview_seed, *draft_inputs],
                                      preview_simulated, concurrency_limit=2)
                # 💾 Save changes: every section with unsaved changes, checked whole and written at once (or
                # not at all), then the whole entry reloaded as stored.
                save_changes.click(*tracked(save_changes_ui, guard_inputs,
                                            [*entry_outputs, save_changes_status, draft_capture]))
                # Each form's own save: that part (a failure changes nothing), then reloaded as stored.
                for button, part, said in ((save_entry, "fields", editor_status), (act_save, "action", actions_status),
                                           (save_policy_form, "scheduling", policy_status),
                                           (save_details, "details", details_status),
                                           (save_entity_yaml_button, "entry_yaml", entity_yaml_status),
                                           (save_actions_button, "actions_yaml", actions_status),
                                           (save_policy_button, "policy_yaml", policy_status)):
                    button.click(*tracked(lambda *rest, part=part: save_part_ui(part, *rest), guard_inputs,
                                          [*entry_outputs, said, draft_capture]))
                for button, part in ((act_discard, "action"), (reload_entity_yaml_button, "entry_yaml"),
                                     (reload_actions_button, "actions_yaml"), (reload_policy_button, "policy_yaml")):
                    button.click(*tracked(lambda *rest, part=part: reload_part_ui(part, *rest), guard_inputs,
                                          [*entry_outputs, draft_capture]))
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
                    deleted = delete.click(*tracked(confirmed, [pending, state, *guard_inputs],
                                                    [*entry_outputs, said_where, draft_capture, *question_outputs, state,
                                                     bar, said]))
                    undone_event = undo.click(*tracked(undone, [state, *guard_inputs],
                                                       [*entry_outputs, said_where, draft_capture, state, bar, said]))
                    history_events["entry" if state is entry_undo_state else "action"] += [deleted, undone_event]
                # Every edit: only its part travels, and is compared with that part's baseline.
                part_components, at = {}, 0
                for part, size in DRAFT_SIZES.items():
                    part_components[part], at = draft_inputs[at:at + size], at + size
                part_status = {part: {"fn": (lambda base, dirty, loaded, saved, *values, part=part:
                                             entry_part_status_ui(part, base, dirty, loaded, saved, *values)),
                                      "inputs": [draft_base, drafts_dirty, draft_loaded, saved_draft, *components],
                                      "outputs": [drafts_dirty, draft_status, saved_draft], "show_progress": "hidden"}
                               for part, components in part_components.items()}
                for part, components in part_components.items():
                    gr.on([c.input for c in components if not isinstance(c, gr.State)], trigger_mode="always_last",
                          **part_status[part])
                # The condition builders write their field; its part's draft status follows.
                wire_condition_builder(entry_condition, enabled_if, "Enabled if", part_status["fields"])
                wire_condition_builder(action_condition, act_enabled, "Offered only if", part_status["action"])
                wire_condition_builder(until_condition, act_until, "until", part_status["action"])
                # A dialogue line's condition: the line chosen, its condition in a field kept in step with the
                # table both ways (each writes only what differs), and the builder on that field.
                det_dialogue.change(dialogue_lines_ui, [det_dialogue, dialogue_line, dialogue_condition_text],
                                    [dialogue_line, dialogue_condition_text], show_progress="hidden")
                dialogue_line.input(dialogue_line_condition_ui, [det_dialogue, dialogue_line], dialogue_condition_text,
                                    show_progress="hidden")
                dialogue_condition_text.change(dialogue_write_condition_ui,
                                               [det_dialogue, dialogue_line, dialogue_condition_text], det_dialogue,
                                               show_progress="hidden").then(**part_status["details"])
                wire_condition_builder(dialogue_condition, dialogue_condition_text, "dialogue", part_status["details"])
                # An action's outcomes are switched, added and removed inside its draft (saved with Save action).
                out_pick.input(pick_outcome_ui, [action_state, out_pick, *outcome_fields],
                               [action_state, *outcome_fields]).then(**part_status["action"])
                out_new.click(new_outcome_ui, [action_state, *outcome_fields],
                              [action_state, out_pick, *outcome_fields]).then(**part_status["action"])
                out_delete.click(delete_outcome_ui, action_state, [action_state, out_pick, *outcome_fields]).then(
                    **part_status["action"])
                # Missions.
                mission_question_outputs = [mission_pending, mission_confirm, mission_question, mission_delete]
                mis_delete.click(ask_delete_mission_ui, mission_state, mission_question_outputs, show_progress="hidden")
                mission_cancel.click(cancel_delete_ui, None, mission_question_outputs, show_progress="hidden")
                history_events["mission"] += [
                    mission_delete.click(confirm_delete_mission_ui,
                                         [mission_pending, mission_undo_state, mission_state, forms_base, drafts_dirty],
                                         [mis_pick, missions_status, missions_editor, *mission_question_outputs,
                                          mission_undo_state, mission_undo_bar, mission_undo_said, *mission_fields,
                                          mission_state, forms_base, drafts_dirty, missions_draft_status]),
                    mission_undo.click(undo_delete_mission_ui, [mission_undo_state, forms_base, drafts_dirty],
                                       [mis_pick, *mission_fields, mission_state, missions_status, missions_editor,
                                        mission_undo_state, mission_undo_bar, mission_undo_said, forms_base,
                                        drafts_dirty, missions_draft_status])]
                # 🎯 Missions, 📜 Story rules, 🗺 Map: each form's draft, its picker guarded, its saves and reloads
                # setting its baselines in the same step, and a question when its draft would be replaced.
                form_layouts = {
                    "missions": {"parts": {"mission": mission_fields, "missions_yaml": [missions_editor],
                                           "side_quests_yaml": [side_quests_editor]},
                                 "context": mission_state, "picker": mis_pick, "view": None, "message": missions_status,
                                 "panels": (missions_draft_status, missions_prompt, missions_prompt_text, missions_save,
                                            missions_discard, missions_stay, missions_pending_state)},
                    "rules": {"parts": {"rules": rule_inputs, "messages": [messages_editor],
                                        "variables": [variables_editor], "values": [stored_values],
                                        "stats": [stats_editor]},
                              "context": rules_profile, "picker": values_profile, "view": None,
                              "message": story_rules_status,
                              "panels": (rules_draft_status, rules_prompt, rules_prompt_text, rules_save, rules_discard,
                                         rules_stay, rules_pending_state)},
                    "map": {"parts": {"borders": [map_borders], "map_yaml": [map_editor]}, "context": map_selected,
                            "picker": map_place, "view": map_view, "message": map_status,
                            "panels": (map_draft_status, map_prompt, map_prompt_text, map_save, map_discard, map_stay,
                                       map_pending_state)}}

                def form_outputs(editor, message=None):
                    layout = form_layouts[editor]
                    status_box, prompt_box, prompt_text, *_, pending_state = layout["panels"]
                    return [*[c for components in layout["parts"].values() for c in components], layout["context"],
                            layout["picker"], *([layout["view"]] if layout["view"] else []),
                            message or layout["message"], status_box, prompt_box, prompt_text, pending_state,
                            forms_base, drafts_dirty, *nav_outputs, world_sections, section_now]

                def form_emit(editor, result):
                    """A form editor's result dict as its outputs (form_outputs; what it leaves out is skipped)."""
                    layout, skip = form_layouts[editor], gr.skip()
                    out = []
                    for part, components in layout["parts"].items():
                        values = (result.get("values") or {}).get(part)
                        out += list(values) if values is not None else [skip] * len(components)
                    out += [result.get("context", skip), result.get("picker", skip)]
                    if layout["view"]:
                        out.append(result.get("view", skip))
                    out += [result.get("message", skip), result.get("status", skip),
                            gr.update(visible=result["prompt"]) if "prompt" in result else skip,
                            result.get("prompt_text", skip), result.get("pending", skip),
                            result.get("base", skip), result.get("dirty", skip),
                            *(result.get("nav") or [skip] * len(nav_outputs)),
                            gr.Tabs(selected=result["section"]) if result.get("section") else skip,
                            result.get("section_now", skip)]
                    return tuple(out)

                for editor, layout in form_layouts.items():
                    values = [c for components in layout["parts"].values() for c in components]
                    status_box, prompt_box, prompt_text, save_button, discard_button, stay_button, pending_state = \
                        layout["panels"]
                    demo.load(lambda context, editor=editor: form_emit(editor, form_load_all_ui(editor, context)),
                              layout["context"], form_outputs(editor), show_progress="hidden")
                    for part, components in layout["parts"].items():  # every edit: its part against its baseline
                        gr.on([c.input for c in components],
                              lambda base, dirty, *vals, editor=editor, part=part: form_part_status_ui(
                                  editor, part, base, dirty, *vals),
                              [forms_base, drafts_dirty, *components], [drafts_dirty, status_box],
                              trigger_mode="always_last", show_progress="hidden")
                    for button, choice in ((save_button, "save"), (discard_button, "discard"), (stay_button, "stay")):
                        button.click(lambda pending, context, base, dirty, *vals, editor=editor, choice=choice: form_emit(
                            editor, form_resolve_ui(editor, choice, pending, context, base, dirty, *vals)),
                            [pending_state, layout["context"], forms_base, drafts_dirty, *values], form_outputs(editor))
                pick_inputs = {editor: [form_layouts[editor]["context"], forms_base, drafts_dirty] for editor in form_layouts}
                # The mission's condition builder writes its field; the mission's draft status follows.
                wire_condition_builder(mission_condition, mis_enabled, "Offered only if", {
                    "fn": lambda base, dirty, *vals: form_part_status_ui("missions", "mission", base, dirty, *vals),
                    "inputs": [forms_base, drafts_dirty, *mission_fields], "outputs": [drafts_dirty, missions_draft_status],
                    "show_progress": "hidden"})
                mis_pick.input(lambda target, context, base, dirty: form_emit("missions", form_pick_ui(
                    "missions", target, context, base, dirty)), [mis_pick, *pick_inputs["missions"]], form_outputs("missions"))
                mis_new.click(lambda context, base, dirty: form_emit("missions", form_pick_ui(
                    "missions", NEW_MISSION, context, base, dirty)), pick_inputs["missions"], form_outputs("missions"))
                values_profile.input(lambda target, context, base, dirty: form_emit("rules", form_pick_ui(
                    "rules", target, context, base, dirty)), [values_profile, *pick_inputs["rules"]], form_outputs("rules"))
                map_place.input(lambda target, context, base, dirty: form_emit("map", form_pick_ui(
                    "map", target, context, base, dirty)), [map_place, *pick_inputs["map"]], form_outputs("map"))
                # Each part's own save and reload buttons (a failure changes nothing).
                for editor, part, button, said, action in (
                        ("missions", "mission", save_mission_form, missions_status, "save"),
                        ("missions", "missions_yaml", save_missions_button, missions_status, "save"),
                        ("missions", "missions_yaml", reload_missions_button, missions_status, "reload"),
                        ("missions", "side_quests_yaml", save_side_quests_button, missions_status, "save"),
                        ("missions", "side_quests_yaml", reload_side_quests_button, missions_status, "reload"),
                        ("rules", "rules", save_rules_button, story_rules_status, "save"),
                        ("rules", "rules", reload_rules_button, story_rules_status, "reload"),
                        ("rules", "messages", save_messages_button, messages_status, "save"),
                        ("rules", "messages", reload_messages_button, messages_status, "reload"),
                        ("rules", "variables", save_variables_button, variables_status, "save"),
                        ("rules", "values", save_values_button, values_status, "save"),
                        ("rules", "values", refresh_values_button, values_status, "reload"),
                        ("rules", "stats", save_stats_button, stats_status, "save"),
                        ("rules", "stats", reload_stats_button, stats_status, "reload"),
                        ("map", "borders", save_borders, map_status, "save"),
                        ("map", "map_yaml", save_map_button, map_status, "save"),
                        ("map", "map_yaml", reload_map_button, map_status, "reload")):
                    layout = form_layouts[editor]
                    values = [c for components in layout["parts"].values() for c in components]
                    if action == "save":
                        button.click(lambda context, base, dirty, *vals, editor=editor, part=part: form_emit(
                            editor, form_save_ui(editor, part, context, base, dirty, *vals)),
                            [layout["context"], forms_base, drafts_dirty, *values], form_outputs(editor, said))
                    else:
                        button.click(lambda context, base, dirty, editor=editor, part=part: form_emit(
                            editor, form_reload_ui(editor, part, context, base, dirty)),
                            [layout["context"], forms_base, drafts_dirty], form_outputs(editor, said))
                reset_values_button.click(reset_question_ui, values_profile, [reset_question, reset_question_text],
                                          show_progress="hidden")
                reset_values_cancel.click(lambda: (gr.update(visible=False), ""), None,
                                          [reset_question, reset_question_text], show_progress="hidden")
                reset_values_confirm.click(lambda: (gr.update(visible=False), ""), None,
                                           [reset_question, reset_question_text], show_progress="hidden").then(
                    reset_stored_values_ui, values_profile, [stored_values, values_status]).success(
                    lambda context, base, dirty: form_emit("rules", form_reload_ui("rules", "values", context, base, dirty)),
                    [rules_profile, forms_base, drafts_dirty], form_outputs("rules"))
                # The map.

                # What an NPC or a place does follows its tags: the choices show the tag in effect, and
                # choosing one sets it (an entry loaded, a tag typed: the choices follow).
                behaviours = [behaviour_a, behaviour_b]
                kind.change(behaviour_fields_ui, [kind, tags], behaviours)
                kind.change(policy_controls_ui, kind, [policy_actions, policy_yaml_box], show_progress="hidden")
                demo.load(policy_controls_ui, kind, [policy_actions, policy_yaml_box], show_progress="hidden")
                tags.change(behaviour_fields_ui, [kind, tags], behaviours)
                demo.load(behaviour_fields_ui, [kind, tags], behaviours)
                behaviour_a.input(lambda k, c, t: apply_behaviour_ui(k, 0, c, t), [kind, behaviour_a, tags], tags).then(
                    **part_status["fields"])
                behaviour_b.input(lambda k, c, t: apply_behaviour_ui(k, 1, c, t), [kind, behaviour_b, tags], tags).then(
                    **part_status["fields"])
                behaviour_tables = [npc_table, npc_default_a, npc_default_b, place_table, place_default_a,
                                    place_default_b]
                demo.load(behaviour_load_ui, None, [*behaviour_tables, behaviour_draft, behaviour_status])
                # Dropdowns of the world's names: filled when the page was built, fresh whenever they are opened.
                for dropdown, source, extra in (
                        (act_stat, "stats", [NONE]), (act_event, "Events", [NONE]), (act_item, "Items", [NONE]),
                        (act_collects, "Items", [NONE]), (out_move, "Locations", [NONE]),
                        (out_send, "Locations", [NONE]), (out_place, "Locations", [NONE]),
                        (out_person, "NPCs", [NONE]), (out_item, "Items", [NONE]),
                        (out_remove, "NPCs", [NONE, ACTING]), (pol_location, "Locations", [NONE]),
                        (det_places, "Locations", []), (det_items, "Items", []), (map_place, "Locations", []),
                        (rule_arrest, "Locations", []), (values_profile, "profiles", [])):
                    dropdown.focus(lambda value, source=source, extra=extra: fresh_choices_ui(source, extra, value),
                                   dropdown, dropdown, show_progress="hidden")
                # Each table is a draft of its own: an edit marks it unsaved; its save reloads it alone.
                for group, parts, save_button in (("npcs", behaviour_tables[:3], save_npc_rules),
                                                  ("locations", behaviour_tables[3:], save_place_rules)):
                    gr.on([c.input for c in parts], lambda draft, *values, group=group: behaviour_edit_ui(
                        group, draft, *values), [behaviour_draft, *parts], [behaviour_draft, behaviour_status],
                          show_progress="hidden", trigger_mode="always_last")
                    save_button.click(lambda t, a, b, group=group: save_behaviour_table_ui(group, t, a, b),
                                      list(parts), rules_status).success(
                        behaviour_fields_ui, [kind, tags], behaviours).then(
                        lambda draft, group=group: behaviour_reload_ui(group, draft), behaviour_draft,
                        [*parts, behaviour_draft, behaviour_status])

                # The language model: suggestions for this entry, and new entries for the world.
                suggest_button.click(suggest_ui, [kind, entity, suggest_field], [suggestions, suggest_status],
                                     concurrency_limit=1, concurrency_id="language_model")
                add_suggestions.click(add_suggestions_ui,
                                      [suggest_field, suggestions, descriptions, goals, tags, det_distant, det_entered,
                                       det_prompts, det_dialogue],
                                      [descriptions, goals, tags, det_distant, det_entered, det_prompts, det_dialogue,
                                       suggest_status]).then(**part_status["fields"]).then(**part_status["details"])
                demo.load(world_size_ui, None, world_size)
                demo.load(augmentation_ui, None, [augment_tick, grow_llm_status, propose_button])
                augment_tick.input(set_augmentation_ui, augment_tick, [grow_llm_status, propose_button])
                propose_button.click(propose_world_ui, None, [proposal_pick, proposal_text, grow_status],
                                     concurrency_limit=1, concurrency_id="language_model")
                proposal_text.change(sync_proposal_ui, [proposal_text, proposal_pick, proposal_offered],
                                     [proposal_pick, proposal_offered], show_progress="hidden")
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
                        train_jobs = jobs_panel_block("train", "⏱ Jobs: queued, running and recent trainings")
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
                        with gr.Row(equal_height=True, elem_classes="result-name"):
                            result_name = gr.Textbox(label="Name (optional)", scale=4, interactive=False,
                                                     elem_id="train-result-name", max_length=80,
                                                     info="Shown instead of its date in Train, Play, Evaluate and "
                                                     "Export; its id stays the same. Empty: no name.")
                            result_name_save = gr.Button("🏷 Save name", scale=1, elem_id="train-result-name-save")
                        result_name_status = gr.Markdown()
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

                    with gr.Tab("🏷 Finished engines", id="train-engines"):
                        gr.Markdown("Every finished run and loaded engine (and, with the records, the other recorded "
                                    "engines): its result card, and an optional name, shown instead of its date in "
                                    "every picker. The engine and run ids stay what identifies it. Names are kept "
                                    "with the saves, so they work with recording off too.")
                        named_engines = [choice for choice in evaluation_engine_choices() if choice[1]]
                        with gr.Row(equal_height=True):
                            card_engine = gr.Dropdown(named_engines, value=named_engines[0][1] if named_engines else None,
                                                      label="Engine", scale=4, elem_id="engines-picker")
                            card_refresh = gr.Button("↻ Refresh", scale=1)
                        with gr.Row(equal_height=True):
                            card_name = gr.Textbox(label="Name (optional)", scale=4, max_length=80,
                                                   elem_id="engines-name", interactive=False)
                            card_name_save = gr.Button("🏷 Save name", scale=1, elem_id="engines-name-save")
                        card_name_status = gr.Markdown()
                        engine_card_view = gr.Markdown(elem_classes="result-card", elem_id="engines-card")
                        card_engine.change(engine_card_ui, card_engine, engine_card_view, show_progress="hidden")
                        card_engine.change(engine_name_ui, card_engine, card_name, show_progress="hidden")
                        for listed in (card_refresh.click(finished_engines_ui, card_engine, card_engine),
                                       demo.load(finished_engines_ui, card_engine, card_engine)):
                            listed.then(engine_card_ui, card_engine, engine_card_view, show_progress="hidden").then(
                                engine_name_ui, card_engine, card_name, show_progress="hidden")

                train_outputs = [simulation_report, generated_config, generated_engine, learning_plot, learned_traits]
                # Every training ends with its completed engine (the state the follow-up actions carry), shown
                # on the card, whose progress bar follows the generations while it runs.
                result_outputs = [train_result, result_card, *result_buttons]
                # A training: submitted (the job kept in this browser), then followed until it ends; its result,
                # or why there is none, is shown. After a reload the same job is followed again (on load).
                run_outputs = [*train_outputs, memory_status, run_state, export_generation, *result_outputs, train_job]
                submitted = [
                    run_button.click(submit_run_ui, [*run_controls, run_plan], train_job, trigger_mode="once"),
                    continue_record.click(submit_continue_ui, [record_engine, more_generations], train_job,
                                          trigger_mode="once"),
                    retrain_record.click(submit_retrain_ui, [record_engine, more_generations, *run_controls,
                                                             retrain_plan], train_job, trigger_mode="once")]
                engine_events = [event.success(follow_training_ui, train_job, run_outputs, concurrency_limit=8,
                                               concurrency_id="training_follow") for event in submitted]
                reattached = demo.load(follow_training_ui, train_job, run_outputs, concurrency_limit=8,
                                       concurrency_id="training_follow")
                reattached.success(lambda done: done, train_result, saved_result, show_progress="hidden")
                # The jobs panel: polled while a training is queued, running or stopping (a submission starts the
                # polling; it stops by itself); a finished training's result opens on the card, with its actions;
                # a queued or running one can be cancelled.
                train_job_outputs = [train_jobs["text"], train_jobs["timer"], train_jobs["finished"],
                                     train_jobs["cancellable"]]
                train_panel_inputs = [train_jobs["finished"], train_job, train_jobs["cancellable"]]
                train_panel = (lambda chosen, yours, to_cancel: jobs_panel_ui("train", chosen, yours, to_cancel))
                for event in (train_jobs["timer"].tick, train_jobs["refresh"].click, demo.load):
                    event(train_panel, train_panel_inputs, train_job_outputs, show_progress="hidden")
                for event in submitted:
                    event.success(lambda: jobs_poll(True), None, train_jobs["timer"], show_progress="hidden")
                for event in [*engine_events, reattached]:
                    event.then(train_panel, train_panel_inputs, train_job_outputs, show_progress="hidden")
                train_jobs["cancel"].click(cancel_job_ui, train_jobs["cancellable"], train_jobs["cancel_note"],
                                           show_progress="hidden").then(train_panel, train_panel_inputs,
                                                                        train_job_outputs, show_progress="hidden")
                train_jobs["open"].click(open_train_job_ui, train_jobs["finished"], result_outputs,
                                         show_progress="hidden").success(lambda done: done, train_result, saved_result,
                                                                         show_progress="hidden")
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
                play_first_quests, quest_overview_text = quest_overview(
                    profiles["default"], play_finished[0][1] if play_finished else None)
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
                                                 elem_id="play-quest",
                                                 info="From the chosen AI's own world, open to this profile; or let "
                                                 "the story draw one.")
                        play_stats = gr.Dropdown(PLAY_STATS, value="story default", label="Character", scale=2,
                                                 info="My own points: set them below.")
                    play_quest_world = gr.Markdown(elem_id="play-quest-world")
                    play_quest_titles = gr.State({})  # {key: title} of the quests listed: a cleared one is named
                    with gr.Row():
                        play_new = gr.Button("▶ New game", variant="primary", scale=3)
                        play_refresh = gr.Button("↻ Refresh", scale=1)
                    with gr.Accordion("🏷 About this engine: its result card", open=False):
                        play_card = gr.Markdown(elem_id="play-engine-card")
                    play_run.change(engine_card_ui, play_run, play_card, show_progress="hidden")
                    demo.load(engine_card_ui, play_run, play_card, show_progress="hidden")
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
                with gr.Accordion("📂 Saved games: continue, restart or open a save", open=True,
                                  elem_id="play-saved-games"):
                    gr.Markdown("Every game is saved as you play it, so it survives closing the page or a "
                                "restart of the server. ▶ games in progress; 💾 your save slots (of the player "
                                "profile chosen above).")
                    with gr.Row(equal_height=True):
                        saved_list = gr.Dropdown([], label="Saved games", scale=5, elem_id="play-saved-list")
                        saved_refresh = gr.Button("↻", scale=0, min_width=40)
                    with gr.Row():
                        saved_open = gr.Button("▶ Continue", variant="primary", scale=2)
                        saved_restart = gr.Button("↺ Restart from the beginning", scale=2)
                        saved_delete = gr.Button("🗑 Delete save", variant="stop", scale=1)
                        saved_cleanup = gr.Button("🧹 Clean up finished games", size="sm", scale=1)
                    with gr.Accordion("A save game file (ane.savegame/1, e.g. from the ANE AI)", open=False):
                        play_upload = gr.UploadButton("📂 Open a save game file", file_types=[".json"],
                                                      type="filepath")
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
                        with gr.Row(equal_height=True):
                            play_slot = gr.Textbox(placeholder="Save slot name (empty: a new manual save)",
                                                   show_label=False, scale=3, max_length=64)
                            play_save = gr.Button("💾 Save", size="sm", scale=1)
                            play_download = gr.Button("⬇ Save game file", size="sm", scale=1)
                            play_stop = gr.Button("End the game", variant="stop", size="sm", scale=1)
                        play_save_file = gr.File(label="Save game (JSON)", interactive=False, visible=False)
                    with gr.Column(scale=2):
                        play_quest_log = gr.Markdown("### 🎯 Quest\nStart a game to see its quest.",
                                                     elem_classes="quest-card")
                        play_you = gr.Markdown("### 🧍 You\nStart a game to see your character.", elem_classes="you-card")
                        with gr.Accordion("👥 People met & the discovered map", open=False):
                            play_people = gr.Markdown("### People met\nNobody yet.", elem_classes="people-card")
                            play_map = gr.HTML(discovered_map_svg(None), elem_id="play-discovered-map")
                with gr.Accordion("🧭 Your journey: every choice and what came of it", open=False):
                    play_journey = gr.Markdown("*Your journey starts with your first choice.*", elem_classes="journey")
                with gr.Accordion("📖 The story so far", open=False):
                    play_story = gr.Markdown(elem_classes="story-book")
                with gr.Accordion("🔍 Author: why these choices (shown, hidden, blocked)", open=False):
                    gr.Markdown("For authors: why each action is offered to the player now or not (its conditions, "
                                "items and coins, one-time use, conversations and encounters, the storyteller's "
                                "ranking and the choice limits). Asking changes nothing in the game.")
                    decisions_button = gr.Button("Explain the current choices")
                    decisions_view = gr.Markdown()
                with gr.Accordion("⚖ Judge this playthrough (Gemma 4 as a game critic)", open=False):
                    game_judge_status = gr.Markdown()
                    judge_game_button = gr.Button("Judge the game played so far")
                    game_judgement = gr.Markdown()

                play_screen = [play_ending, play_hud, play_scene, play_turn, *play_buttons, play_quest_log, play_you,
                               play_people, play_map, play_actions, play_journey]
                new_game_outputs = [play_token, *play_screen, play_story, play_status]
                player_inputs = [play_who, play_quest, play_stats, stats_points]
                started = [
                    play_new.click(play_new_game_ui, [play_run, *player_inputs], new_game_outputs, concurrency_limit=1),
                    play_record.click(play_recorded_ui, [play_record_engine, *player_inputs], new_game_outputs,
                                      concurrency_limit=1),
                    play_upload.upload(play_continue_ui, [play_upload, play_token], new_game_outputs,
                                       concurrency_limit=1),
                    saved_open.click(open_saved_game_ui, saved_list, new_game_outputs, concurrency_limit=1),
                    saved_restart.click(restart_saved_game_ui, [saved_list, play_token], new_game_outputs,
                                        concurrency_limit=1),
                ]
                for event in (saved_refresh.click, play_who.change, demo.load, *(e.success for e in started)):
                    event(saved_games_ui, [play_who, saved_list], saved_list, show_progress="hidden")
                saved_delete.click(delete_saved_game_ui, saved_list, play_status).success(
                    saved_games_ui, [play_who, saved_list], saved_list, show_progress="hidden")
                saved_cleanup.click(cleanup_games_ui, None, play_status)
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
                # The quests follow the chosen AI (its own saved world) and the profile (its save game): on
                # either change, and on loading the page; a quest no longer offered is cleared, saying why.
                quest_outputs = [play_quest, quest_overview_md, play_quest_world, play_quest_titles]
                for event in (play_run.change, play_who.change, refresh_quests.click, demo.load):
                    event(play_quests_titled_ui, [play_run, play_who, play_quest, play_quest_titles], quest_outputs,
                          show_progress="hidden")
                randomize_stats.click(lambda: stats_points_text("random"), None, stats_points).then(
                    lambda: "defined", None, play_stats)
                balance_stats.click(lambda: stats_points_text("balanced"), None, stats_points).then(
                    lambda: "defined", None, play_stats)
                stats_points.change(check_points_text, stats_points, stats_check)
                for index, button in enumerate(play_buttons):
                    button.click(lambda token, actions, index=index: play_choose_ui(token, actions, index),
                                 [play_token, play_actions], [*play_screen, play_story], concurrency_limit=1)
                play_stop.click(play_end_ui, play_token, [*play_screen, play_story], concurrency_limit=1)
                play_save.click(save_to_slot_ui, [play_token, play_slot], play_status).success(
                    saved_games_ui, [play_who, saved_list], saved_list, show_progress="hidden")
                play_download.click(download_savegame_ui, play_token, [play_save_file, play_status]).success(
                    lambda: gr.File(visible=True), None, play_save_file)
                save_game_refresh.click(progression_ui, play_who, [save_game_text, save_game_table])
                demo.load(judge_status_ui, None, game_judge_status)
                judge_game_button.click(judge_session_ui, play_token, game_judgement)
                decisions_button.click(decisions_ui, play_token, decisions_view)
                # The game survives a reload: its session id is kept in this browser, and the game opens again
                # while the API still has it.
                play_token.change(lambda token: token or "", play_token, saved_game, show_progress="hidden")
                demo.load(lambda token, size=len(play_screen) + 1: restore_game_ui(token, size), saved_game,
                          [play_token, *play_screen, play_story, saved_game], show_progress="hidden").success(
                    lambda token: gr.Accordion(open=not token), play_token, play_setup)

            with gr.Tab("⚖ Evaluate", id="evaluate", elem_id="dest-evaluate") as evaluate_tab:
                gr.Markdown(
                    "Gemma 4 as a game critic (`vllm` in the engine logic: an embedded model or a vLLM server). "
                    "Stories are played in a separate process, so the games in progress keep their settings. "
                    "The judge also runs in the loop of evolution when `evolution.judge_weight` > 0 (Engine logic)."
                )
                evaluate_note = gr.Markdown()  # the engine chosen by 🧠 Train's "Evaluate this result"
                with gr.Row():
                    judge_status = gr.Markdown()
                    judge_status_button = gr.Button("Check the judge again", size="sm", scale=0)
                demo.load(judge_status_ui, None, judge_status)
                judge_status_button.click(judge_status_ui, None, judge_status)
                evaluate_jobs = jobs_panel_block("evaluate", "⏱ Jobs: queued, running and recent evaluations")
                gr.Markdown("#### What is evaluated")
                evaluation_choices = evaluation_engines()
                eval_target = gr.Radio(list(EVALUATION_TARGETS),
                                       value=TARGET_ENGINE if evaluation_choices else TARGET_BASELINE,
                                       label="Evaluate", elem_id="evaluate-target",
                                       info="A selected trained engine (its saved world and trained storyteller), or "
                                       "the baseline to compare it with (the current configuration, a random "
                                       "storyteller per seed). Both the judge benchmark and the calibration use it.")
                with gr.Row(equal_height=True):
                    eval_engine = gr.Dropdown(evaluation_choices,
                                              value=evaluation_choices[0][1] if evaluation_choices else None,
                                              label="Trained engine", scale=5, elem_id="evaluate-engine",
                                              visible=bool(evaluation_choices),
                                              info="Finished runs (newest first), then other recorded engines.")
                    eval_refresh = gr.Button("↻ Refresh", scale=1, elem_id="evaluate-refresh")
                eval_target_note = gr.Markdown(elem_id="evaluate-target-note")
                with gr.Accordion("🏷 About this engine: its result card", open=False):
                    bench_card = gr.Markdown(elem_id="evaluate-engine-card")
                with gr.Accordion("Judge the engine's stories", open=True):
                    with gr.Row():
                        bench_stories = gr.Slider(1, 60, value=10, step=1, label="Stories")
                        bench_seed = gr.Number(value=0, precision=0, label="First seed")
                        bench_workers = gr.Slider(1, 8, value=4, step=1, label="Judgements at once")
                    bench_pairwise = gr.Checkbox(
                        label="Compare with version B (pairwise A/B: the target as it is (A) and with version B's "
                        "settings (B), on the same seeds, each pair judged in both orders)", elem_id="evaluate-pairwise")
                    bench_variants = gr.Code(value="# version B: dotted.path: value\nclimax.enabled: false\n",
                                             language="yaml", label="Version B's settings")
                    bench_plan = gr.Markdown(elem_id="evaluate-plan")
                    bench_button = gr.Button("Play and judge", variant="primary", elem_id="evaluate-run")
                    bench_summary = gr.Markdown(elem_id="evaluate-summary")
                    bench_table = gr.Dataframe(label="Stories", interactive=False, wrap=True)
                    bench_submitted = bench_button.click(
                        submit_benchmark_ui, [bench_stories, bench_seed, eval_engine, bench_pairwise, bench_variants,
                                              bench_workers, eval_target], evaluate_job, trigger_mode="once")
                with gr.Accordion("Fit the fitness to the judge", open=False):
                    gr.Markdown(
                        "Plays and judges stories of the target above, fits the judged engagement on the fitness "
                        "terms and suggests a multiplier per term (`fitness.term_weights`). Trust it as far as its "
                        "agreement on stories left out goes."
                    )
                    with gr.Row():
                        calib_stories = gr.Slider(10, 60, value=30, step=1, label="Stories")
                        calib_seed = gr.Number(value=0, precision=0, label="First seed")
                        calib_apply = gr.Checkbox(label="Save the suggested weights at once")
                    calib_plan = gr.Markdown(elem_id="evaluate-calibration-plan")
                    calib_button = gr.Button("Play, judge and fit", variant="primary")
                    calib_summary = gr.Markdown()
                    term_weights_editor = gr.Code(value=load_term_weights_ui(), language="yaml",
                                                  label="fitness.term_weights (term: multiplier)")
                    with gr.Row():
                        save_weights_terms = gr.Button("Save term weights", variant="primary")
                        reload_weights_terms = gr.Button("Reload")
                    term_weights_status = gr.Markdown()
                    calib_submitted = calib_button.click(
                        submit_calibration_ui, [calib_stories, calib_seed, eval_engine, calib_apply, eval_target],
                        evaluate_job, trigger_mode="once")
                    # An evaluation: submitted (kept in this browser), followed until it ends; after a reload,
                    # followed again. Its result goes where it belongs (the benchmark's, or the calibration's).
                    evaluation_outputs = [bench_summary, bench_table, calib_summary, term_weights_editor, evaluate_job]
                    evaluation_followed = [event.success(follow_evaluation_ui, evaluate_job, evaluation_outputs,
                                                         concurrency_limit=8, concurrency_id="evaluation_follow")
                                           for event in (bench_submitted, calib_submitted)]
                    evaluation_followed.append(demo.load(follow_evaluation_ui, evaluate_job, evaluation_outputs,
                                                         concurrency_limit=8, concurrency_id="evaluation_follow"))
                    # The jobs panel: polled while an evaluation is queued, running or stopping; a finished one
                    # opens its result; a queued or running one can be cancelled.
                    evaluate_job_outputs = [evaluate_jobs["text"], evaluate_jobs["timer"], evaluate_jobs["finished"],
                                            evaluate_jobs["cancellable"]]
                    evaluate_panel_inputs = [evaluate_jobs["finished"], evaluate_job, evaluate_jobs["cancellable"]]
                    evaluate_panel = (lambda chosen, yours, to_cancel: jobs_panel_ui("evaluate", chosen, yours,
                                                                                     to_cancel))
                    for event in (evaluate_jobs["timer"].tick, evaluate_jobs["refresh"].click, demo.load):
                        event(evaluate_panel, evaluate_panel_inputs, evaluate_job_outputs, show_progress="hidden")
                    for event in (bench_submitted, calib_submitted):
                        event.success(lambda: jobs_poll(True), None, evaluate_jobs["timer"],
                                      show_progress="hidden")
                    for event in evaluation_followed:
                        event.then(evaluate_panel, evaluate_panel_inputs, evaluate_job_outputs, show_progress="hidden")
                    evaluate_jobs["cancel"].click(cancel_job_ui, evaluate_jobs["cancellable"],
                                                  evaluate_jobs["cancel_note"], show_progress="hidden").then(
                        evaluate_panel, evaluate_panel_inputs, evaluate_job_outputs, show_progress="hidden")
                    evaluate_jobs["open"].click(open_evaluate_job_ui, evaluate_jobs["finished"],
                                                [bench_summary, bench_table, calib_summary, term_weights_editor],
                                                show_progress="hidden")
                    save_weights_terms.click(save_term_weights_ui, term_weights_editor,
                                             [term_weights_editor, term_weights_status])
                    reload_weights_terms.click(load_term_weights_ui, None, term_weights_editor)
                # The plan follows every choice it depends on, before anything is submitted.
                plan_inputs = [eval_target, eval_engine, bench_pairwise, bench_variants, bench_stories, bench_seed,
                               calib_stories, calib_seed]
                plan_outputs = [eval_engine, eval_target_note, bench_card, bench_plan, calib_plan]
                gr.on([eval_target.change, eval_engine.change, bench_pairwise.change, bench_variants.change,
                       bench_stories.change, bench_seed.change, calib_stories.change, calib_seed.change],
                      evaluation_plan_ui, plan_inputs, plan_outputs, show_progress="hidden", trigger_mode="always_last")
                for listed in (eval_refresh.click(evaluation_engines_ui, [eval_target, eval_engine],
                                                  [eval_target, eval_engine]),
                               demo.load(evaluation_engines_ui, [eval_target, eval_engine], [eval_target, eval_engine])):
                    listed.then(evaluation_plan_ui, plan_inputs, plan_outputs, show_progress="hidden")
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
                    "Download a trained result to run offline in a game, in five steps. **The ANE AI** (Windows, "
                    "macOS, Linux or Unity) is a standalone program that a UNP launches, drives over HTTP and stops, "
                    "no Python needed; it is made from **finished runs**. **The Python version** (for developers) "
                    "can also be made from a generation's champion, the remembered storyteller or this game's "
                    "storyteller.", elem_classes="lead")
                export_listed = export_platform_list()
                gr.Markdown("#### 1 · Choose what to export")
                export_source = gr.Radio(list(EXPORT_SOURCES), value=FINISHED_RUNS, label="What to export",
                                         elem_id="export-source",
                                         info="The ANE AI is made from finished runs only; the other three export the "
                                         "Python version.")
                finished = finished_run_choices()
                with gr.Row(equal_height=True):
                    export_engine = gr.Dropdown(finished, value=[v for _, v in finished[:1]], multiselect=True,
                                                label="Finished runs", scale=5, elem_id="export-engines",
                                                info="Newest first: the latest is chosen. Choose several to export "
                                                "each (one download per run).")
                    export_refresh = gr.Button("↻ Refresh", scale=1, elem_id="export-refresh")
                export_generation.render()  # created above the destinations: a training (🧠 Train) fills it
                export_engine_note = gr.Markdown(elem_id="export-source-note")
                with gr.Accordion("🏷 The chosen engines' result cards", open=False):
                    export_cards = gr.Markdown(elem_id="export-engine-cards")
                gr.Markdown("#### 2 · Choose where it runs")
                export_platform = gr.Radio(export_platform_choices(export_listed), value="python", label="Runs on",
                                           elem_id="export-platform",
                                           info="Systems this API has no runtime for are marked; their reason shows "
                                           "below.")
                export_platform_info = gr.Markdown(export_platform_note("python", export_listed),
                                                   elem_id="export-platform-note")
                gr.Markdown("#### 3 · Options")
                with gr.Row():
                    export_memory = gr.Checkbox(True, label="Include the learning memory (keep training offline)")
                    export_name = gr.Textbox(label="Name", placeholder="the engine's name, or narrative-engine-<date>",
                                             elem_id="export-name")
                export_options = gr.Markdown(elem_id="export-options-note")
                gr.Markdown("#### 4 · Export")
                export_check = gr.Markdown(elem_id="export-check")
                export_button = gr.Button("Export", variant="primary", elem_id="export-button")
                gr.Markdown("#### 5 · Download and use it")
                export_file = gr.File(label="Downloads (ZIP, or .unitypackage for Unity)", interactive=False,
                                      file_count="multiple", elem_id="export-files")
                export_status = gr.Markdown(elem_id="export-status")
                export_usage = gr.Markdown(elem_id="export-usage")
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
                # One form: a choice of source or platform lays it out again; a choice of runs, generation or
                # name re-checks it. Export is enabled only for a valid combination (why not, and how to resolve
                # it, in step 4).
                export_form = [export_source, export_generation, export_engine, export_engine_note, export_button,
                               export_platform_info, export_options, export_check]
                export_inputs = [export_platform, export_source, export_engine, export_generation, run_state,
                                 session_token, export_name]
                export_checked = [export_button, export_options, export_check]
                for control in (export_platform, export_source):
                    control.input(export_form_ui, export_inputs, export_form, show_progress="hidden")
                export_engine.change(export_check_ui, export_inputs, export_checked, show_progress="hidden")
                export_generation.change(export_check_ui, export_inputs, export_checked, show_progress="hidden")
                export_name.input(export_check_ui, export_inputs, export_checked, show_progress="hidden")
                export_engine.change(engine_cards_ui, export_engine, export_cards, show_progress="hidden")
                export_refresh.click(export_refresh_ui, export_inputs, [export_platform, *export_form],
                                     show_progress="hidden")
                demo.load(export_refresh_ui, export_inputs, [export_platform, *export_form], show_progress="hidden")
                demo.load(engine_cards_ui, export_engine, export_cards, show_progress="hidden")
                export_button.click(
                    lambda: (None, "", ""), None, [export_file, export_status, export_usage], show_progress="hidden"
                ).then(
                    export_engine_ui,
                    [export_source, run_state, export_generation, session_token, export_memory, export_name,
                     export_platform, export_engine],
                    [export_file, export_status],
                ).success(export_usage_markdown, export_platform, export_usage, show_progress="hidden")
            with gr.Tab("⚙ Advanced", id="advanced", elem_id="dest-advanced") as advanced_tab:
                gr.Markdown("Technical settings: the simulation, every weight, the engine's logic, the app and its background jobs. The story's own rules are in 🌍 World → 📜 Story rules.", elem_classes="lead")
                with gr.Tabs() as advanced_sections:
                    with gr.Tab("Simulation", id="simulation"):
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
                        gr.Markdown(REFERENCE["activation"]["configuration"], elem_classes="activation-note")
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

                    with gr.Tab("Weights", id="weights"):
                        gr.Markdown(
                            "The numeric weights, bonuses, penalties, thresholds, chances and rates of scoring and "
                            "the narrative engine. **Basic** shows a chosen set of common gameplay controls "
                            "(difficulty, pacing, repetition, event frequency), each one parameter, explained by "
                            "what it does in play; **Advanced** shows every parameter. Both edit one draft: a "
                            "value changed in one view is the same value in the other, and switching views "
                            "changes nothing. Each field gives its unit (chances have different scales: a "
                            "percentage of the 1–100 roll is not a probability), the engine's default and bounds "
                            "and its parameter path. Whole-number parameters take whole numbers only; only the "
                            "values you change are saved, and the others stay exactly as they are written."
                        )
                        weights_data = API.get("/v1/weights")
                        weight_infos = weights_data["parameters"]
                        weight_by_path = {info["path"]: info for info in weight_infos}
                        basic_areas = weights_data.get("basic") or []
                        weights_meta_value = weights_meta(weight_infos, basic_areas, weights_data.get("shipped"))
                        weights_meta_state = gr.State(weights_meta_value)
                        weights_saved = gr.State(dict(weights_data["values"]))
                        weights_view = gr.Radio(["Basic", "Advanced"], value="Basic", label="View",
                                                elem_id="weights-view",
                                                info="Basic: common gameplay controls. Advanced: every parameter.")
                        weights_draft = gr.HTML(weights_draft_markup(
                            weights_meta_value, weights_data["values"],
                            *(weights_data["values"][path] for path in weights_meta_value["paths"])),
                            elem_id="weights-draft-status")
                        weights_shipped = gr.HTML(weights_shipped_markup(weights_meta_value, weights_data["values"]),
                                                  elem_id="weights-shipped")
                        with gr.Group(visible=False, elem_id="weights-draft-prompt") as weights_prompt_group:
                            weights_prompt_text = gr.Markdown()
                            with gr.Row():
                                weights_prompt_save = gr.Button("💾 Save", variant="primary",
                                                                elem_id="weights-draft-save")
                                weights_prompt_discard = gr.Button("Discard", elem_id="weights-draft-discard")
                                weights_prompt_stay = gr.Button("Stay", elem_id="weights-draft-stay")
                        weights_pending = gr.State(None)
                        with gr.Column(visible=True, elem_id="weights-basic") as weights_basic_box:
                            basic_components = []
                            for area in basic_areas:
                                with gr.Group(elem_classes="weights-basic-area"):
                                    gr.Markdown(f"#### {area['title']}\n{area['summary']}")
                                    controls = area["controls"]
                                    for index in range(0, len(controls), 3):
                                        with gr.Row():
                                            for control in controls[index:index + 3]:
                                                basic_components.append(
                                                    basic_weight_field(weight_by_path[control["path"]], control))
                        with gr.Column(visible=False, elem_id="weights-advanced") as weights_advanced_box:
                            weight_components = []
                            grouped_weights = {}
                            for info in weight_infos:
                                grouped_weights.setdefault(info["section"], []).append(info)
                            for section, weight_group in grouped_weights.items():
                                described = sum(info["described"] for info in weight_group)
                                with gr.Accordion(f"{section} ({described} of {len(weight_group)} described)",
                                                  open=False):
                                    for index in range(0, len(weight_group), 3):
                                        with gr.Row():
                                            for info in weight_group[index:index + 3]:
                                                weight_components.append(weight_field(
                                                    info, weights_meta_value["basic"].get(info["path"])))
                        gr.Markdown(REFERENCE["activation"]["configuration"], elem_classes="activation-note")
                        with gr.Row():
                            save_weights_button = gr.Button("Save changes", variant="primary",
                                                            elem_id="weights-save")
                            discard_weights_button = gr.Button("Discard changes (reload the saved values)",
                                                               elem_id="weights-discard")
                            shipped_weights_button = gr.Button("↺ Shipped values into the draft",
                                                               elem_id="weights-shipped-draft")
                        weights_status = gr.Markdown(elem_id="weights-message")
                        weights_view.change(weights_view_ui, weights_view, [weights_basic_box, weights_advanced_box])
                        # One draft: a Basic field and its parameter's Advanced field hold the same value; an
                        # edit in either is copied to the other in the browser (user edits only: .input), then
                        # the draft's status is worked out from the Advanced fields (every parameter).
                        draft_inputs = [weights_meta_state, weights_saved, *weight_components]
                        dirty_inputs = [weights_meta_state, weights_saved, drafts_dirty, *weight_components]
                        advanced_of = dict(zip(weights_meta_value["paths"], weight_components))
                        for path, basic_field in zip(weights_meta_value["basic_paths"], basic_components):
                            twin = advanced_of[path]
                            for source, target in ((basic_field, twin), (twin, basic_field)):
                                source.input(None, source, target, js="(value) => value", queue=False).then(
                                    weights_draft_ui, dirty_inputs, [weights_draft, drafts_dirty],
                                    trigger_mode="always_last")
                        advanced_only = [field for path, field in advanced_of.items()
                                         if path not in weights_meta_value["basic"]]
                        if advanced_only:
                            gr.on([field.input for field in advanced_only], weights_draft_ui, dirty_inputs,
                                  [weights_draft, drafts_dirty], trigger_mode="always_last")
                        after_outputs = [weights_draft, drafts_dirty, weights_shipped]
                        save_weights_button.click(save_weights, draft_inputs,
                                                  [weights_status, weights_saved, weights_draft]).success(
                            weights_after_ui, dirty_inputs, after_outputs, show_progress="hidden")
                        weights_reload_outputs = [weights_saved, *weight_components, *basic_components,
                                                  weights_draft, weights_status]
                        for reloaded in (discard_weights_button.click(reload_weights_ui, weights_meta_state,
                                                                      weights_reload_outputs),
                                         # On loading the page (⚙ Advanced not shown yet; the fields hold the saved
                                         # values already): its progress on the status line, not on 251 fields.
                                         demo.load(reload_weights_ui, weights_meta_state, weights_reload_outputs,
                                                   show_progress_on=[weights_draft])):
                            reloaded.success(weights_after_ui, dirty_inputs, after_outputs, show_progress="hidden")
                        shipped_weights_button.click(
                            shipped_into_draft_ui, draft_inputs,
                            [*weight_components, *basic_components, weights_draft, weights_status]).success(
                            weights_after_ui, dirty_inputs, after_outputs, show_progress="hidden")
                        # Leaving ⚙ Advanced with unsaved weights asks here (the navigation's guard, below).
                        weights_resolve_outputs = [weights_prompt_group, weights_prompt_text, weights_pending,
                                                   *nav_outputs, weights_saved, *weight_components, *basic_components,
                                                   weights_draft, weights_status, weights_shipped, drafts_dirty]
                        for button, choice in ((weights_prompt_save, "save"), (weights_prompt_discard, "discard"),
                                               (weights_prompt_stay, "stay")):
                            button.click(lambda pending, meta, saved, dirty, *values, choice=choice: weights_resolve_ui(
                                choice, pending, meta, saved, dirty, *values),
                                [weights_pending, weights_meta_state, weights_saved, drafts_dirty, *weight_components],
                                weights_resolve_outputs, show_progress="hidden")

                    with gr.Tab("Engine logic"):
                        gr.Markdown(
                            "The engine's logic runs in the backend scripts; here you change **how it behaves** "
                            "through its parameters, section by section. Every section is checked (same settings, "
                            "same value types) before it is saved."
                        )
                        gr.Markdown(REFERENCE["activation"]["configuration"], elem_classes="activation-note")
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
                                        "evolution.crossover_rate (⚙ Advanced → Weights → Training).  \n"
                                        "**When they take effect:** the API reads its parameters when it starts, and the "
                                        "studio its own when it starts. Saving changes nothing that is running: games in "
                                        "progress, submitted jobs and this studio go on with the values they started with, "
                                        "until the API or the studio is restarted (the table below says which).")
                            parameter_inputs = []
                            for key, value in limits.items():
                                label = key.replace("_", " ").title()
                                info = app_parameter_info(key, parameter_readers.get(key, ["API"]))
                                if isinstance(value, bool):
                                    component = gr.Checkbox(value=value, label=label, info=info)
                                elif isinstance(value, str):
                                    component = gr.Textbox(value=value, label=label, info=info)
                                else:
                                    component = gr.Number(value=value, label=label, info=info,
                                                          precision=0 if isinstance(app_defaults[key], int) else None)
                                parameter_inputs.append(component)
                            save_parameters = gr.Button("Save app parameters", variant="primary")
                            parameters_status = gr.Markdown(elem_id="app-parameters-status")
                            app_activation = gr.Markdown(elem_id="app-activation")
                            save_parameters.click(save_app_parameters, parameter_inputs,
                                                  [parameters_status, app_activation])
                            demo.load(app_activation_ui, None, app_activation)
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
            event.success(lambda platform, source, generation, run_id, token, name: export_form_ui(
                platform, source, None, generation, run_id, token, name),
                [export_platform, export_source, export_generation, run_state, session_token, export_name],
                export_form)

        # Navigation: the banner, the menu, the address (?section=<id>) and the links inside a destination all
        # select the destination's tab; nothing is re-rendered, so every form, game and run keeps its state.
        # Leaving 🌍 World with unsaved changes in the entry editor asks first (Save, Discard or Stay).
        # Leaving 🌍 World, or one of its sections, with unsaved changes in any of its editors asks first (in
        # that editor); the guards read only which parts are unsaved (drafts_dirty), not the drafts.
        form_prompts = {editor: form_layouts[editor]["panels"] for editor in form_layouts}
        leave_outputs = [*nav_outputs, world_sections, section_now, draft_pending, draft_prompt, draft_message,
                         *[c for editor in form_prompts for c in (form_prompts[editor][6], form_prompts[editor][1],
                                                                  form_prompts[editor][2])],
                         advanced_sections, weights_pending, weights_prompt_group, weights_prompt_text]

        def leave_emit(result):
            skip = gr.skip()
            out = [*(result.get("nav") or [skip] * len(nav_outputs)),
                   gr.Tabs(selected=result["section"]) if result.get("section") else skip, result.get("section_now", skip)]
            for editor in ("entries", *form_prompts):
                asked = (result.get("prompts") or {}).get(editor)
                out += [asked[2], gr.update(visible=True), asked[1]] if asked else [skip, skip, skip]
            out.append(gr.Tabs(selected=result["advanced_section"]) if result.get("advanced_section") else skip)
            asked = (result.get("prompts") or {}).get("weights")
            out += [asked[2], gr.update(visible=True), asked[1]] if asked else [skip, skip, skip]
            return tuple(out)

        leave_inputs = [dest_now, section_now, draft_loaded, drafts_dirty]
        for section, tab in (("entries", section_entries), ("missions", section_missions),
                             ("story-rules", section_story_rules), ("map", section_map), ("behaviour", section_behaviour),
                             ("grow", section_grow), ("checks", section_checks)):
            tab.select(lambda now_section, loaded, dirty, section=section: leave_emit(
                section_choice_ui(section, now_section, loaded, dirty)), [section_now, draft_loaded, drafts_dirty],
                leave_outputs, show_progress="hidden")
        # The World's sub-navigation: each button goes through the same guard as the sections' tabs (unsaved
        # changes: asked in the editor first); the bar follows the section open, however it was opened.
        world_nav_outputs = [*world_nav_buttons, world_content_nav, *content_nav_buttons, content_last]
        for group, button in zip(WORLD_GROUPS, world_nav_buttons):
            button.click(lambda now_section, loaded, dirty, last, group=group: leave_emit(world_section_choice_ui(
                world_group_target(group, now_section, last), now_section, loaded, dirty)),
                [section_now, draft_loaded, drafts_dirty, content_last], leave_outputs, show_progress="hidden")
        for section, button in zip(CONTENT_SECTIONS, content_nav_buttons):
            button.click(lambda now_section, loaded, dirty, section=section: leave_emit(world_section_choice_ui(
                section, now_section, loaded, dirty)), [section_now, draft_loaded, drafts_dirty], leave_outputs,
                show_progress="hidden")
        section_now.change(world_nav_ui, [section_now, content_last], world_nav_outputs, show_progress="hidden")
        nav_events = {key: button.click(lambda *rest, key=key: leave_emit(nav_choice_ui(key, *rest)), leave_inputs,
                                        leave_outputs, js=nav_show_js(key),
                                        show_progress="hidden")
                      for key, button in zip(DESTINATION_IDS, nav_buttons)}
        train_button_event = nav_events["train"]
        menu_event = nav_menu.input(lambda key, *rest: leave_emit(nav_choice_ui(key, *rest)), [nav_menu, *leave_inputs],
                                    leave_outputs, js=nav_show_js(), show_progress="hidden")
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
        play_result.click(play_result_ui, [train_result, *player_inputs, play_quest_titles],
                          [*nav_outputs, play_run, play_setup, *new_game_outputs], concurrency_limit=1).success(
            integration_session_text, play_token, integration_session, show_progress="hidden")
        evaluate_result.click(evaluate_result_ui, train_result, [*nav_outputs, eval_target, eval_engine, evaluate_note],
                              show_progress="hidden")
        # After a training or a loaded engine, Evaluate offers it (the newest is selected) and its plan follows.
        for event in [*engine_events, loaded]:
            event.success(lambda target, chosen: evaluation_engines_ui(target, chosen, newest=True),
                          [eval_target, eval_engine], [eval_target, eval_engine], show_progress="hidden").then(
                evaluation_plan_ui, plan_inputs, plan_outputs, show_progress="hidden")
        export_result.click(export_result_ui, [train_result, export_platform, export_name],
                            [*nav_outputs, *export_form], show_progress="hidden")
        # Names: from Train's result card or its finished engines; every picker's labels follow (ids unchanged).
        engine_pickers = [card_engine, play_run, eval_engine, export_engine, record_engine, play_record_engine,
                          files_engine]
        renamed = [result_name_save.click(rename_result_ui, [train_result, result_name, card_engine],
                                          [result_name_status, engine_card_view, result_name, result_card,
                                           *engine_pickers]),
                   card_name_save.click(rename_engine_ui, [card_engine, card_name, train_result],
                                        [card_name_status, engine_card_view, card_name, result_card, *engine_pickers])]
        for event in renamed:  # the cards beside Play's, Evaluate's and Export's pickers show the new name too
            event.success(engine_card_ui, play_run, play_card, show_progress="hidden").then(
                evaluation_plan_ui, plan_inputs, plan_outputs, show_progress="hidden").then(
                engine_cards_ui, export_engine, export_cards, show_progress="hidden")
        train_result.change(result_name_ui, train_result, result_name, show_progress="hidden")
        # ⚙ Advanced → 🔌 Integration names the game it acts on: the one open in ▶ Play.
        for event in [*started, loaded, nav_events["advanced"], menu_event]:
            event.then(integration_session_text, play_token, integration_session, show_progress="hidden")
        # Train's summary follows the saved defaults (e.g. changed in ⚙ Advanced): on load, and on the way to Train.
        demo.load(run_form_ui, run_controls, run_form, show_progress="hidden")
        for event in (train_button_event, menu_event):
            event.then(run_form_ui, run_controls, run_form, show_progress="hidden")
        demo.load(open_requested, None, nav_outputs, show_progress="hidden")

    demo.launch_settings = limits
    lighten_loading_status(demo)
    # The studio's events are its own page's (it is a client of the API, which is the API to call):
    # undocumented, they stay callable by the page but are left out of the API description Gradio
    # embeds in every page (gradio_api_info: 11 MB for the 571 events, parsed on every load).
    for fn in demo.fns.values():
        if fn.api_visibility == "public":
            fn.api_visibility = "undocumented"
    return demo


# A process spawned from this script (as __mp_main__) needs no UI; the studio itself starts none
# (training, evaluation and play run in the API).
demo = build_app() if __name__ != "__mp_main__" else None


STARTUP_WAIT = float(os.getenv("ANE_UI_STARTUP_WAIT", "90"))  # seconds the page may take to answer at start


def patient_startup_check(wait=STARTUP_WAIT):
    """Gradio checks at launch that its page answers, giving it one 3-second try: on a busy host the
    first render of this (large) page can take longer, and the studio then refuses to start ("When
    localhost is not accessible, a shareable link must be created"). This check asks the same way, but
    tries again (10 s each) for up to ``wait`` seconds before giving up."""
    import httpx
    from gradio import networking

    def url_ok(url):
        deadline = time.monotonic() + wait
        while True:
            try:
                response = httpx.head(url, timeout=10, verify=False)
                if response.status_code in (200, 401, 301, 302, 303, 307, 308):
                    return True
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                LOG.warning("The studio's page did not answer within %.0f s of starting.", wait)
                return False
            time.sleep(0.5)

    networking.url_ok = url_ok
    return url_ok


if __name__ == "__main__":
    launched = studio_launch_values(demo.launch_settings)  # the environment first, then the saved values
    STUDIO_RUNNING.update(launched)
    STUDIO_LAUNCHED = True
    patient_startup_check()
    demo.queue(default_concurrency_limit=launched["queue_concurrency"][0],
               max_size=launched["queue_max_size"][0]).launch(
        server_name=launched["server_host"][0],
        server_port=int(launched["server_port"][0]),
        share=bool(launched["share"][0]),
        css=CSS,
        theme=THEME,
        head=DRAFT_HEAD,  # with unsaved changes in the entry editor, the browser asks before leaving the page
        app_kwargs={"middleware": [Middleware(CompressPages)]},  # the page and its config gzipped
    )
