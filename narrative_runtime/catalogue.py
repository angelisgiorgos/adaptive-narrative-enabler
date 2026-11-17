"""The world as parameters for the UNP (``ane.world/1``): every location, NPC, item, event and
mission with all its authored fields (names, tags, goals, descriptions, actions and their
outcomes, encounters, roles, secrets, event titles, ...), the stats and variables, the map and
the starting locations. Each entity carries its ``kind`` and ``name``."""
from __future__ import annotations

import copy

FORMAT = "ane.world/1"
KINDS = {"locations": ("locations", "mapping"), "npcs": ("characters", "list"), "items": ("objects", "list"),
         "events": ("events", "mapping"), "missions": ("missions", "mapping")}


def _entities(world, kind):
    key, shape = KINDS[kind]
    raw = world.get(key) or ({} if shape == "mapping" else [])
    if shape == "mapping":
        return [{"kind": kind, "name": name, **copy.deepcopy(entry or {})} for name, entry in raw.items()]
    return [{"kind": kind, **copy.deepcopy(entry)} for entry in raw if isinstance(entry, dict)]


def world_catalogue(config_data, starting_locations=(), revision=0, engine=None):
    world = (config_data or {}).get("world_definition") or {}
    stats = world.get("character_stats") or {}
    return {
        "format": FORMAT,
        "engine": engine,
        "revision": revision,
        **{kind: _entities(world, kind) for kind in KINDS},
        "stats": {"names": list(stats.get("stats") or {}), "definitions": copy.deepcopy(stats.get("stats") or {}),
                  "allocation": copy.deepcopy(stats.get("allocation") or {}),
                  "tag_checks": copy.deepcopy(stats.get("tag_checks") or {})},
        "variables": copy.deepcopy(world.get("variables") or {}),
        "routes": copy.deepcopy(world.get("routes")),
        "starting_locations": list(starting_locations),
        "npc_roles": list(((config_data or {}).get("npc_roles") or {}).get("roles") or {}),
    }


def world_entity(config_data, kind, name):
    if kind not in KINDS:
        raise KeyError(f"Unknown kind `{kind}`; use {', '.join(KINDS)}.")
    world = (config_data or {}).get("world_definition") or {}
    for entity in _entities(world, kind):
        if entity.get("name") == name:
            return entity
    raise KeyError(f"No {kind[:-1]} `{name}`.")
