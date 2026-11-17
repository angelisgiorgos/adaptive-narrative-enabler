"""World content from the UNP: a patch of the engine's configuration.

    {"upsert":      {"items": [{"name": "Lantern", "associated_tags": ["light"], ...}],
                     "locations": {"Lighthouse": {"tags": ["sea"], ...}}},
     "update":      {"npcs": {"Merchant": {"goals": ["Sell the lantern"], "tags": ["trade"]}}},
     "delete":      {"items": ["Rusty Sword"]},
     "add_actions": {"locations": {"Harbor": [{"name": "Light the beacon", "outcomes": [...]}]}},
     "tags":        {"npcs": {"Merchant": {"add": ["smuggler"], "remove": ["guidance"]}}},
     "stats":       {"stats": {...}, "allocation": {...}, "tag_checks": {...}},
     "persist":     false}

Kinds: locations, npcs, items, events, missions. ``upsert`` inserts or replaces whole entries;
``update`` changes fields of existing ones (``tags`` stands for the kind's tag field; ``null``
removes a field); ``delete`` removes entries; ``add_actions`` appends actions; ``tags`` adds or
removes tags (or sets them with a list); ``stats`` changes the character stats. The result is
checked like the studio checks a world (core_engine/content_rules.py) before it is used.
"""
from __future__ import annotations

import copy

from core_engine import content_rules

OPERATIONS = ("upsert", "update", "delete", "add_actions", "tags", "stats", "persist", "note")
KINDS = content_rules.KINDS
TAG_FIELDS = content_rules.TAG_FIELDS


class PatchFormatError(ValueError):
    pass


def _collection(world, kind):
    if kind not in KINDS:
        raise PatchFormatError(f"Unknown kind `{kind}`; use {', '.join(KINDS)}.")
    key, shape = KINDS[kind]
    return world.setdefault(key, {} if shape == "mapping" else []), shape


def _find(collection, shape, name):
    if shape == "mapping":
        return collection.get(name)
    return next((entry for entry in collection if isinstance(entry, dict) and entry.get("name") == name), None)


def _records(kind, value):
    """(name, record) pairs of an upsert: a list of records with `name`, or a mapping name -> record."""
    if isinstance(value, dict):
        return [(name, dict(record or {})) for name, record in value.items()]
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
    for kind, value in (patch.get("upsert") or {}).items():
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
    for kind, entries in (patch.get("update") or {}).items():
        collection, shape = _collection(world, kind)
        for name, fields in (entries or {}).items():
            record = _find(collection, shape, name)
            if record is None:
                raise PatchFormatError(f"No {kind[:-1]} `{name}` to update.")
            for field, value in (fields or {}).items():
                field = TAG_FIELDS.get(kind, field) if field == "tags" else field
                if field == "name":
                    raise PatchFormatError("Rename an entry with delete + upsert.")
                if value is None:
                    record.pop(field, None)
                else:
                    record[field] = copy.deepcopy(value)
    for kind, names in (patch.get("delete") or {}).items():
        collection, shape = _collection(world, kind)
        for name in names or []:
            record = _find(collection, shape, name)
            if record is None:
                raise PatchFormatError(f"No {kind[:-1]} `{name}` to delete.")
            if shape == "mapping":
                collection.pop(name)
            else:
                collection.remove(record)
    for kind, entries in (patch.get("add_actions") or {}).items():
        collection, shape = _collection(world, kind)
        for name, actions in (entries or {}).items():
            record = _find(collection, shape, name)
            if record is None:
                raise PatchFormatError(f"No {kind[:-1]} `{name}` to add actions to.")
            if not isinstance(actions, list):
                raise PatchFormatError(f"`add_actions.{kind}.{name}` must be a list of actions.")
            record.setdefault("actions", []).extend(copy.deepcopy(actions))
    for kind, entries in (patch.get("tags") or {}).items():
        collection, shape = _collection(world, kind)
        if kind not in TAG_FIELDS:
            raise PatchFormatError(f"{kind} have no tags.")
        for name, change in (entries or {}).items():
            record = _find(collection, shape, name)
            if record is None:
                raise PatchFormatError(f"No {kind[:-1]} `{name}` to tag.")
            field = TAG_FIELDS[kind]
            if isinstance(change, list):
                record[field] = list(dict.fromkeys(str(t) for t in change))
            else:
                tags = list(record.get(field) or [])
                tags += [str(t) for t in change.get("add") or [] if str(t) not in tags]
                removed = {str(t) for t in change.get("remove") or []}
                record[field] = [t for t in tags if t not in removed]
    if patch.get("stats"):
        stats = world.setdefault("character_stats", {})
        for field, value in dict(patch["stats"]).items():
            if field not in ("stats", "allocation", "tag_checks", "mode", "defined_points"):
                raise PatchFormatError(f"Unknown stats field `{field}`.")
            stats[field] = copy.deepcopy(value)
    return data


def patch_errors(config_data):
    """Every problem of the patched world (empty: it can be played)."""
    from core_engine import stats as stats_module

    world = (config_data or {}).get("world_definition") or {}
    roles = list(((config_data or {}).get("npc_roles") or {}).get("roles") or {})
    errors = content_rules.world_errors(world, roles)
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
