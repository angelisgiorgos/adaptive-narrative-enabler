"""The save game of a session, as the UNP keeps it (``ane.savegame/1``).

It carries the session's parameters (core_engine/resume.py: what the UNP can read and change)
and an exact snapshot of the story (core_engine/snapshot.py). Loading it continues the session
exactly when the snapshot fits this engine (same code, world and storyteller) and is intact;
the parameters are then applied on top, so what the UNP changed (stats, inventory, coins,
health, variables) counts. Otherwise the session is rebuilt from the parameters alone.
"""
from __future__ import annotations

import datetime

from . import resume, snapshot, version

FORMAT = "ane.savegame/1"
OVERRIDABLE = ("stat_points", "stat_changes", "inventory", "coins", "health", "max_health", "variables")


class SaveGameError(ValueError):
    """A document that is not a usable save game."""


def make(state, engine=None):
    """The save game of a session in progress (or ended)."""
    data = snapshot.dump(state)
    return {
        "format": FORMAT,
        "engine": {**(engine or {}), "code_version": version.code_version(), "config_hash": data["config_hash"]},
        "session_id": state.__dict__.get("session_id"),
        "profile": state.profile,
        "saved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "parameters": resume.parameters(state),
        "snapshot": data,
        "checksum": snapshot.checksum(data),
    }


def _overrides(state, params):
    """What the UNP changed in the parameters, compared with the restored session."""
    current = resume.parameters(state)
    changed = {}
    for field in OVERRIDABLE:
        if field in params and params[field] != current.get(field):
            changed[field] = params[field]
    if "inventory" in changed:
        changed["inventory"] = list(changed["inventory"])
    return changed


def load(doc, genome, store=None, profile=None, seed=None):
    """(state, how, note): how is "exact" (the snapshot) or "parameters" (rebuilt); note says why
    the snapshot was not used, or which parameters the UNP changed."""
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise SaveGameError("Not an ane.savegame/1 save game.")
    params = doc.get("parameters") or {}
    data = doc.get("snapshot")
    note = None
    if data:
        if doc.get("checksum") != snapshot.checksum(data):
            note = "the snapshot does not match its checksum (changed or damaged)"
        else:
            try:
                state = snapshot.load(data, store=store, genome=genome)
            except snapshot.SnapshotError as exc:
                note = f"the snapshot does not fit this engine: {exc}"
            else:
                if profile:
                    state.profile = profile
                changed = _overrides(state, params)
                if changed:
                    resume.apply_player(state, changed)
                return state, "exact", ("parameters changed by the UNP: " + ", ".join(sorted(changed))) if changed else None
    else:
        note = "the save game has no snapshot"
    if not params:
        raise SaveGameError("The save game has neither a usable snapshot nor parameters.")
    return resume.rebuild(genome, params, store=store, profile=profile, seed=seed), "parameters", note
