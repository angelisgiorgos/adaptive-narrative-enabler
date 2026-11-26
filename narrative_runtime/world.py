"""The world and the UNP: what the UNP reads (the catalogue) and what it changes (patches).

The catalogue (``ane.world/1``, ``world_catalogue``): every location, NPC, item, event, mission, side
quest and faction with all its authored fields (an NPC's faction, relationship, haunts, goals and
reactions too) (names, tags, goals, descriptions, actions and their outcomes,
encounters, roles, secrets, event titles, ...), the stats and variables, the map and the starting
locations. Each entity carries its ``kind`` and ``name``.

A patch (``apply_patch``, ``patch_errors``): world content from the UNP, changing the engine's
configuration.

    {"upsert":      {"items": [{"name": "Lantern", "associated_tags": ["light"], ...}],
                     "locations": {"Lighthouse": {"tags": ["sea"], ...}}},
     "update":      {"npcs": {"Merchant": {"goals": ["Sell the lantern"], "tags": ["trade"]}}},
     "delete":      {"items": ["Rusty Sword"]},
     "add_actions": {"locations": {"Harbor": [{"name": "Light the beacon", "outcomes": [...]}]}},
     "tags":        {"npcs": {"Merchant": {"add": ["smuggler"], "remove": ["guidance"]}}},
     "stats":       {"stats": {...}, "allocation": {...}, "tag_checks": {...}},
     "persist":     false}

Kinds: locations, npcs, items, events, missions, side_quests, factions. ``upsert`` inserts or replaces whole entries;
``update`` changes fields of existing ones (``tags`` stands for the kind's tag field; ``null``
removes a field); ``delete`` removes entries; ``add_actions`` appends actions; ``tags`` adds or
removes tags (or sets them with a list); ``stats`` changes the character stats. The result is
checked like the studio checks a world (core_engine/content_rules.py) before it is used.
"""
from __future__ import annotations

import copy

from core_engine import content_rules

OPERATIONS = ("upsert", "update", "delete", "add_actions", "tags", "stats", "persist", "note")
# The world's entity kinds, and its side quests (core_engine/side_quests.py), which a UNP reads and
# patches like missions.
KINDS = {**content_rules.KINDS, "side_quests": ("side_quests", "mapping"),
         # who stands with whom (core_engine/relationships.py), read and patched like side quests
         "factions": ("factions", "mapping")}
TAG_FIELDS = content_rules.TAG_FIELDS


class PatchFormatError(ValueError):
    pass


def _collection(world, kind):
    if kind not in KINDS:
        raise PatchFormatError(f"Unknown kind `{kind}`; use {', '.join(KINDS)}.")
    key, shape = KINDS[kind]
    return world.setdefault(key, {} if shape == "mapping" else []), shape


def _mapping(value, where):
    """A mapping from the patch (an absent value is an empty one), or PatchFormatError."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PatchFormatError(f"`{where}` must be a mapping, not {type(value).__name__}.")
    return value


def _find(collection, shape, name):
    if shape == "mapping":
        return collection.get(name)
    return next((entry for entry in collection if isinstance(entry, dict) and entry.get("name") == name), None)


def _records(kind, value):
    """(name, record) pairs of an upsert: a list of records with `name`, or a mapping name -> record."""
    if isinstance(value, dict):
        return [(name, dict(_mapping(record, f"upsert.{kind}.{name}"))) for name, record in value.items()]
    if isinstance(value, list):
        pairs = []
        for record in value:
            if not isinstance(record, dict) or not str(record.get("name", "")).strip():
                raise PatchFormatError(f"Every {kind[:-1]} to upsert needs a `name`.")
            pairs.append((record["name"], dict(record)))
        return pairs
    raise PatchFormatError(f"`upsert.{kind}` must be a list of entries or a mapping of names to entries.")


def apply_patch(config_data, patch):
    """A copy of the configuration with the patch applied (PatchFormatError for a malformed patch)."""
    if not isinstance(patch, dict):
        raise PatchFormatError("A patch must be a mapping.")
    unknown = sorted(set(patch) - set(OPERATIONS))
    if unknown:
        raise PatchFormatError(f"Unknown operation(s) {', '.join(unknown)}; use {', '.join(OPERATIONS)}.")
    data = copy.deepcopy(config_data)
    world = data.setdefault("world_definition", {})
    for kind, value in _mapping(patch.get("upsert"), "upsert").items():
        collection, shape = _collection(world, kind)
        for name, record in _records(kind, value):
            if shape == "mapping":
                record.pop("name", None)
                collection[name] = record
            else:
                record["name"] = name
                existing = _find(collection, shape, name)
                if existing is None:
                    collection.append(record)
                else:
                    collection[collection.index(existing)] = record
    for kind, entries in _mapping(patch.get("update"), "update").items():
        collection, shape = _collection(world, kind)
        for name, fields in _mapping(entries, f"update.{kind}").items():
            record = _find(collection, shape, name)
            if record is None:
                raise PatchFormatError(f"No {kind[:-1]} `{name}` to update.")
            for field, value in _mapping(fields, f"update.{kind}.{name}").items():
                field = TAG_FIELDS.get(kind, field) if field == "tags" else field
                if field == "name":
                    raise PatchFormatError("Rename an entry with delete + upsert.")
                if value is None:
                    record.pop(field, None)
                else:
                    record[field] = copy.deepcopy(value)
    for kind, names in _mapping(patch.get("delete"), "delete").items():
        collection, shape = _collection(world, kind)
        if names is not None and not isinstance(names, list):
            raise PatchFormatError(f"`delete.{kind}` must be a list of names.")
        for name in names or []:
            record = _find(collection, shape, name)
            if record is None:
                raise PatchFormatError(f"No {kind[:-1]} `{name}` to delete.")
            if shape == "mapping":
                collection.pop(name)
            else:
                collection.remove(record)
    for kind, entries in _mapping(patch.get("add_actions"), "add_actions").items():
        collection, shape = _collection(world, kind)
        for name, actions in _mapping(entries, f"add_actions.{kind}").items():
            record = _find(collection, shape, name)
            if record is None:
                raise PatchFormatError(f"No {kind[:-1]} `{name}` to add actions to.")
            if not isinstance(actions, list):
                raise PatchFormatError(f"`add_actions.{kind}.{name}` must be a list of actions.")
            record.setdefault("actions", []).extend(copy.deepcopy(actions))
    for kind, entries in _mapping(patch.get("tags"), "tags").items():
        collection, shape = _collection(world, kind)
        if kind not in TAG_FIELDS:
            raise PatchFormatError(f"{kind} have no tags.")
        for name, change in _mapping(entries, f"tags.{kind}").items():
            record = _find(collection, shape, name)
            if record is None:
                raise PatchFormatError(f"No {kind[:-1]} `{name}` to tag.")
            field = TAG_FIELDS[kind]
            if isinstance(change, list):
                record[field] = list(dict.fromkeys(str(t) for t in change))
            else:
                change = _mapping(change, f"tags.{kind}.{name}")
                for part in ("add", "remove"):
                    if change.get(part) is not None and not isinstance(change[part], list):
                        raise PatchFormatError(f"`tags.{kind}.{name}.{part}` must be a list of tags.")
                tags = list(record.get(field) or [])
                tags += [str(t) for t in change.get("add") or [] if str(t) not in tags]
                removed = {str(t) for t in change.get("remove") or []}
                record[field] = [t for t in tags if t not in removed]
    if patch.get("stats"):
        stats = world.setdefault("character_stats", {})
        for field, value in _mapping(patch["stats"], "stats").items():
            if field not in ("stats", "allocation", "tag_checks", "mode", "defined_points"):
                raise PatchFormatError(f"Unknown stats field `{field}`.")
            stats[field] = copy.deepcopy(value)
    return data


def patch_errors(config_data):
    """Every problem of the patched world (empty: it can be played)."""
    from core_engine import stats as stats_module

    world = (config_data or {}).get("world_definition") or {}
    roles = list(((config_data or {}).get("npc_roles") or {}).get("roles") or {})
    tuning_factions = list((((config_data or {}).get("encounters") or {}).get("factions")) or {})
    errors = content_rules.world_errors(world, roles, tuning_factions=tuning_factions)
    character_stats = world.get("character_stats")
    if character_stats:
        errors += stats_module.validate_settings(character_stats)
    return errors


def new_tags(config_data, known):
    """Tags of the world without a trained vector (``known``: the vectors' tags)."""
    from utils.tag_similarity import _normalize_tag_text

    from core_engine.model import world_tags

    known = {_normalize_tag_text(tag) for tag in known}
    return {tag for tag in world_tags(config_data) if _normalize_tag_text(tag) not in known} if known else set()


# ---- the catalogue ---------------------------------------------------------------------------------

FORMAT = "ane.world/1"


def _entities(world, kind):
    key, shape = KINDS[kind]
    raw = world.get(key) or ({} if shape == "mapping" else [])
    if shape == "mapping":
        return [{"kind": kind, "name": name, **copy.deepcopy(entry or {})} for name, entry in raw.items()]
    return [{"kind": kind, **copy.deepcopy(entry)} for entry in raw if isinstance(entry, dict)]


def starting_locations(config_data):
    """Where a story may start (``start_eligible`` locations, or the single default start of a
    world that marks none)."""
    from core_engine import starts

    locations = ((config_data or {}).get("world_definition") or {}).get("locations") or {}
    eligible = [name for name, data in locations.items() if (data or {}).get("start_eligible")]
    default = starts.settings()["default_location"]
    return eligible or [default if default in locations else next(iter(locations), default)]


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
