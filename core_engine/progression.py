"""A player's progression across sessions (a save game is a player profile), and the report of
each session for the UNP.

After every session of real play (a GameState with a profile's store) the engine:

- saves the player's progression in the profile (core_engine/variables.py VariableStore): the
  session's report, and its goal among the goals achieved when it was achieved; every goal can
  be achieved once per save game, so an achieved goal leaves the profile's goal pool
  (``progression.goals_once``);
- communicates the report to the UNP (``session_report``): the player's stats, inventory, health
  and coins, and the story's progression (the goal achieved, or the mission failed, the goals
  achieved so far in the save game). It is sent as JSON (POST) to ``session_report.url`` (or the
  ``ANE_UNP_URL`` environment variable; ``ANE_UNP_TOKEN`` adds an ``Authorization: Bearer``
  header), in the background, and its delivery is noted in the profile. Without a URL the
  report is kept in the profile only (GET /v1/progression, GET /v1/sessions/{id}/report).
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import threading
import urllib.error
import urllib.request
import uuid

from utils.config_loader import config

FORMAT = "ane.session_report/1"
LOGGER = logging.getLogger(__name__)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def outcome(state):
    """achieved (the session's goal was reached), failed (it was not), or free (no goal)."""
    if state.mission is None:
        return "free"
    return "achieved" if state.won else "failed"


def build_report(state, completed_goals=None):
    """The report of a session: who played, where it started, its goal and how it ended, the
    player's stats, inventory, health and coins, and the save game's progression."""
    mission = state.mission
    stages = (getattr(mission, "stages", None) or []) if mission is not None else []
    bonuses = state.combat.item_bonuses(state.inventory)
    unique = state.unique_actions() if hasattr(state, "unique_actions") else []
    return {
        "format": FORMAT,
        "report_id": uuid.uuid4().hex,
        "session_id": state.__dict__.get("session_id"),
        "profile": state.profile,
        "started_at": state.__dict__.get("started_at"),
        "ended_at": _now(),
        "start_location": state.start_location,
        "goal": None if mission is None else {
            "key": mission.name, "title": mission.title, "description": mission.description,
            "stages": [stage["title"] for stage in stages],
            "stages_done": min(getattr(state, "mission_stage", 0) + (1 if state.won else 0), len(stages)) if stages else int(state.won),
        },
        "outcome": outcome(state),
        "end_kind": state.end_kind,
        "end_reason": state.end_reason,
        "turns": state.actions_taken,
        "player": {
            "health": state.health, "max_health": state.max_health, "coins": state.coins,
            "power": round(state.power, 3), "inventory": sorted(state.inventory),
            "stats": state.stats.values(bonuses), "stat_points": dict(state.stats.points),
            "stat_changes": {k: v for k, v in state.stats.changes.items() if v},
        },
        "story": {
            "places_visited": sorted(name for name, count in state.location_counts.items() if count),
            "people_met": [person["name"] for person in state.people_met()],
            "allies": sorted(state.__dict__.get("allies", ())),
            "defeated": [d["npc"] for d in state.__dict__.get("defeated", [])],
            "unique_actions": unique,
            "cast": state.scope() if hasattr(state, "scope") else None,
        },
        "progression": {
            "goals_achieved": list(completed_goals or []),
            "playthroughs": playthroughs(state),
            "variables": {k: v for k, v in state.variables.items() if k in state.variable_specs},
        },
    }


def playthroughs(state):
    store = state.__dict__.get("_store")
    if store is not None:
        return int(store.profile(state.profile)["playthroughs"])
    return int(state.variables.get("playthroughs", 0))


def settings():
    cfg = dict(config.get("session_report") or {})
    url = os.environ.get("ANE_UNP_URL", "").strip() or str(cfg.get("url") or "").strip()
    return {"enabled": bool(cfg.get("enabled", True)), "url": url, "timeout": float(cfg.get("timeout", 5))}


def send(report, url=None, timeout=None):
    """POST the report to the UNP; {sent, status, error, at}."""
    cfg = settings()
    url = url or cfg["url"]
    if not url:
        return {"sent": False, "status": None, "error": "no UNP endpoint configured (session_report.url, ANE_UNP_URL)",
                "at": _now()}
    headers = {"content-type": "application/json", "user-agent": "adaptive-narrative-enabler"}
    token = os.environ.get("ANE_UNP_TOKEN", "").strip()
    if token:
        headers["authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=json.dumps(report).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout or cfg["timeout"]) as response:
            return {"sent": 200 <= response.status < 300, "status": response.status, "error": None, "at": _now()}
    except urllib.error.HTTPError as exc:
        return {"sent": False, "status": exc.code, "error": str(exc), "at": _now()}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"sent": False, "status": None, "error": str(exc), "at": _now()}


def deliver(report, store=None, profile=None, wait=False):
    """Send the report to the UNP in the background (``wait``: here and now) and note the
    delivery in the profile. Returns the thread (or the delivery when ``wait``)."""
    cfg = settings()
    if not cfg["enabled"] or not cfg["url"]:
        return None

    def run():
        delivery = send(report)
        if not delivery["sent"]:
            LOGGER.warning("The session report %s did not reach the UNP: %s", report["report_id"], delivery["error"])
        if store is not None and profile is not None:
            store.note_delivery(profile, report["report_id"], delivery)
        return delivery

    if wait:
        return run()
    thread = threading.Thread(target=run, name=f"unp-report-{report['report_id'][:8]}", daemon=True)
    thread.start()
    return thread


def finish_session(state):
    """Save the session in the player's progression and report it (called when a session of
    real play ends). Returns the report."""
    store = state.__dict__.get("_store")
    cfg = config.get("progression") or {}
    completed = []
    if store is not None:
        if state.won and state.mission is not None and cfg.get("goals_once", True):
            store.complete_goal(state.profile, state.mission.name)
        completed = store.completed_goals(state.profile)
    report = build_report(state, completed)
    if store is not None:
        store.record_session(state.profile, report, limit=int(cfg.get("history", 50)))
        deliver(report, store, state.profile)
    return report
