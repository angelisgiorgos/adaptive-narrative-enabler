"""Checks of world content, as pure functions of a world (``world_definition``): the same checks
the studio and the REST API make (backend/content.py wraps them), available to the offline
runtime, which validates the content the UNP sends (narrative_runtime/world.py).

``world_errors(world, roles)`` checks a whole world: every entry's fields and actions (unknown
fields, unknown places, NPCs, items and events they name, conditions, effects, stats), NPC roles
and secrets, missions and their goals, the map, and the engine's own logic checks
(core_engine/builder.py ``logic_errors``).
"""
from __future__ import annotations

from numbers import Number

from . import conditions, effects
from .actions import repetition_errors
from .builder import _stat_usage_errors, logic_errors, route_errors
from .variables import validate_changes
from .world import Mission

ACTION_FIELDS = {
    "id", "name", "outcomes", "required_object", "required_tag", "required_coins",
    "collects_object", "consumes_object", "triggers_event", "repeat", "exclusive_group", "skill_check",
    "enabled_if", "retry_on_failure", "until",
}

OUTCOME_FIELDS = {
    "id", "desc", "tags", "success_prob", "use_genome_bias", "health_change", "coin_change",
    "spawn", "move_to", "move_to_tags", "reveal_npc", "reveal_object", "reveal_location",
    "linked_location", "linked_location_mode", "linked_npc", "linked_item", "lead_to_known",
    "remove_npc", "send_to", "set_variables", "change_variables", "on_check", "change_stats", "effects", "until",
    "target_tags", "unlock_exit", "return_to_previous", "setup_id", "requires_setup", "payoff_id",
    "variants",
}

KINDS = {  # the entity kinds of a world: (world_definition key, shape)
    "locations": ("locations", "mapping"),
    "npcs": ("characters", "list"),
    "items": ("objects", "list"),
    "events": ("events", "mapping"),
    "missions": ("missions", "mapping"),
}
TAG_FIELDS = {"locations": "tags", "npcs": "associated_tags", "items": "associated_tags", "events": "tags"}


def references(world):
    """The names conditions and effects may refer to in ``world``."""
    return conditions.References.from_world(world or {})


def action_errors(world, kind, actions):
    """Structural and reference checks for an entry's actions (empty when valid)."""
    world = world or {}
    if not isinstance(actions, list):
        return ["Actions must be a list."]
    locations = set(world.get("locations") or {})
    npcs = {entry.get("name") for entry in world.get("characters") or []}
    items = {entry.get("name") for entry in world.get("objects") or []}
    events = set(world.get("events") or {})
    specs = world.get("variables") or {}
    refs = references(world)
    errors = []
    for index, action in enumerate(actions, start=1):
        label = f"Action {index}"
        if not isinstance(action, dict):
            errors.append(f"{label} must be a mapping.")
            continue
        label = f"Action {index} ({action.get('name', 'unnamed')})"
        if not str(action.get("name", "")).strip():
            errors.append(f"{label}: `name` is required.")
        unknown = sorted(set(action) - ACTION_FIELDS)
        if unknown:
            errors.append(f"{label}: unknown field(s) {', '.join(unknown)}.")
        errors.extend(repetition_errors(action.get("repeat"), action.get("retry_on_failure"), label))
        if action.get("triggers_event") and action["triggers_event"] not in events:
            errors.append(f"{label}: unknown event `{action['triggers_event']}`.")
        if not isinstance(action.get("required_coins", 0), int) or action.get("required_coins", 0) < 0:
            errors.append(f"{label}: `required_coins` must be a whole number of 0 or more.")
        for field in ("required_object", "collects_object"):
            if action.get(field) and action[field] not in items:
                errors.append(f"{label}: unknown item `{action[field]}` in `{field}`.")
        errors.extend(conditions.validate(action.get("enabled_if"), refs, where=f"{label}.enabled_if"))
        outcomes = action.get("outcomes") or []
        if not isinstance(outcomes, list) or (not outcomes and not action.get("triggers_event")):
            errors.append(f"{label}: needs a list of `outcomes` (or `triggers_event`).")
            continue
        for number, outcome in enumerate(outcomes, start=1):
            where = f"{label}, outcome {number}"
            if not isinstance(outcome, dict):
                errors.append(f"{where} must be a mapping.")
                continue
            unknown = sorted(set(outcome) - OUTCOME_FIELDS)
            if unknown:
                errors.append(f"{where}: unknown field(s) {', '.join(unknown)}.")
            if not isinstance(outcome.get("desc", ""), str) or not outcome.get("desc", "").strip():
                errors.append(f"{where}: `desc` (the story text) is required.")
            for field in ("health_change", "coin_change"):
                if not isinstance(outcome.get(field, 0), int) or isinstance(outcome.get(field, 0), bool):
                    errors.append(f"{where}: `{field}` must be a whole number.")
            if not isinstance(outcome.get("tags", []), list):
                errors.append(f"{where}: `tags` must be a list.")
            variants = outcome.get("variants", [])
            if not isinstance(variants, list) or not all(isinstance(text, str) and text.strip() for text in variants):
                errors.append(f"{where}: `variants` must be a list of texts (other ways to tell it).")
            for field in ("move_to", "send_to", "reveal_location", "linked_location"):
                if outcome.get(field) and outcome[field] not in locations:
                    errors.append(f"{where}: unknown location `{outcome[field]}` in `{field}`.")
            for field in ("reveal_npc", "linked_npc"):
                if outcome.get(field) and outcome[field] not in npcs:
                    errors.append(f"{where}: unknown NPC `{outcome[field]}` in `{field}`.")
            for field in ("reveal_object", "linked_item"):
                if outcome.get(field) and outcome[field] not in items:
                    errors.append(f"{where}: unknown item `{outcome[field]}` in `{field}`.")
            remove = outcome.get("remove_npc")
            if isinstance(remove, str) and remove not in npcs:
                errors.append(f"{where}: unknown NPC `{remove}` in `remove_npc`.")
            errors.extend(validate_changes(outcome.get("set_variables"), outcome.get("change_variables"), specs,
                                           where=where))
            errors.extend(effects.validate(outcome.get("effects"), refs, where=f"{where}.effects"))
    stat_names = list((world.get("character_stats") or {}).get("stats") or {})
    errors.extend(_stat_usage_errors({"actions": [a for a in actions if isinstance(a, dict)]}, stat_names, kind))
    return errors


def secret_errors(world, secret, where="secret"):
    """Problems with an NPC's secret ({text, talks, clues, effects}); empty when valid."""
    if secret is None:
        return []
    if not isinstance(secret, dict):
        return [f"`{where}` must be a mapping {{text, talks, clues, effects}}."]
    errors = []
    unknown = sorted(set(secret) - {"text", "talks", "clues", "effects"})
    if unknown:
        errors.append(f"`{where}`: unknown field(s) {', '.join(unknown)}; use text, talks, clues, effects.")
    if not str(secret.get("text", "")).strip():
        errors.append(f"`{where}.text` (what comes out; {{npc}} names them) is required.")
    for field in ("talks", "clues"):
        value = secret.get(field, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(f"`{where}.{field}` must be a whole number of 0 or more.")
    errors.extend(effects.validate(secret.get("effects"), references(world), where=f"{where}.effects"))
    return errors


def mission_errors(world, missions):
    """Problems with missions (goals, stages, rewards, references); empty when valid."""
    world = world or {}
    if not isinstance(missions, dict):
        return ["Missions must be a mapping of mission ids to missions."]
    known = {
        "location": set(world.get("locations") or {}),
        "item": {entry.get("name") for entry in world.get("objects") or []},
        "event": set(world.get("events") or {}),
        "event_succeeded": set(world.get("events") or {}),
        "npc_removed": {entry.get("name") for entry in world.get("characters") or []},
    }
    errors = []
    for mission_id, mission in missions.items():
        label = f"Mission `{mission_id}`"
        if not isinstance(mission, dict):
            errors.append(f"{label} must be a mapping.")
            continue
        unknown = sorted(set(mission) - {"title", "description", "goal", "weight", "enabled_if", "rewards", "stages"})
        if unknown:
            errors.append(f"{label}: unknown field(s) {', '.join(unknown)}.")
        stages = mission.get("stages")
        if stages is not None and (not isinstance(stages, list) or not stages):
            errors.append(f"{label}: `stages` must be a list of stages (title, description, goal, rewards).")
            continue
        goals = [(f"stages[{n}].goal", (s or {}).get("goal") if isinstance(s, dict) else None)
                 for n, s in enumerate(stages or [], 1)]
        if mission.get("goal") is not None or not goals:
            goals.insert(0, ("goal", mission.get("goal")))
        refs = conditions.References.from_world({**world, "missions": missions})
        for where, goal in goals:
            if not isinstance(goal, dict) or not goal:
                errors.append(f"{label} needs a `{where}` mapping.")
                continue
            bad = sorted(set(goal) - set(Mission.GOAL_KEYS))
            if bad:
                errors.append(f"{label}: unknown goal key(s) in `{where}` {', '.join(bad)}; "
                              f"use {', '.join(Mission.GOAL_KEYS)}.")
            for key, names in known.items():
                if key in goal and goal[key] not in names:
                    errors.append(f"{label}: `{goal[key]}` in `{where}.{key}` does not exist.")
            if "coins" in goal and (not isinstance(goal["coins"], int) or goal["coins"] < 0):
                errors.append(f"{label}: `{where}.coins` must be a whole number of 0 or more.")
            for key in ("variables", "condition"):
                errors += conditions.validate(goal.get(key), refs, where=f"{mission_id}.{where}.{key}")
        for n, stage in enumerate(stages or [], 1):
            if isinstance(stage, dict):
                errors += effects.validate(stage.get("rewards"), refs, where=f"{mission_id}.stages[{n}].rewards")
        errors += conditions.validate(mission.get("enabled_if"), refs, where=f"{mission_id}.enabled_if")
        errors += effects.validate(mission.get("rewards"), refs, where=f"{mission_id}.rewards")
        weight = mission.get("weight", 1)
        if not isinstance(weight, Number) or isinstance(weight, bool) or weight < 0:
            errors.append(f"{label}: `weight` must be a number of 0 or more.")
    return errors


def _entries(world, kind):
    key, shape = KINDS[kind]
    raw = world.get(key) or ({} if shape == "mapping" else [])
    if shape == "mapping":
        return [(name, entry or {}) for name, entry in raw.items()]
    return [(entry.get("name"), entry) for entry in raw if isinstance(entry, dict)]


def world_errors(world, roles=None):
    """Every problem of a whole world (empty when it can be played): entries and their actions,
    NPC roles and secrets, missions, the map, and the engine's logic checks."""
    world = world or {}
    errors = []
    events = set(world.get("events") or {})
    for kind in ("locations", "npcs", "items", "events"):
        key, shape = KINDS[kind]
        raw = world.get(key)
        if raw is not None and not isinstance(raw, dict if shape == "mapping" else list):
            errors.append(f"`{key}` must be a {'mapping' if shape == 'mapping' else 'list'}.")
            continue
        seen = set()
        for name, entry in _entries(world, kind):
            label = f"{kind[:-1].capitalize()} `{name}`"
            if not isinstance(entry, dict) or not str(name or "").strip():
                errors.append(f"Every entry of {kind} needs a name and a mapping.")
                continue
            if name in seen:
                errors.append(f"{label} is defined twice.")
            seen.add(name)
            tags = entry.get(TAG_FIELDS[kind])
            if tags is not None and not isinstance(tags, (list, set)):
                errors.append(f"{label}: `{TAG_FIELDS[kind]}` must be a list.")
            errors.extend(f"{label}: {e}" for e in action_errors(world, kind, entry.get("actions") or []))
            if kind == "npcs":
                if entry.get("role") is not None and roles is not None and entry["role"] not in roles:
                    errors.append(f"{label}: unknown role `{entry['role']}`; use one of {', '.join(roles)}.")
                if entry.get("encounter_event") and entry["encounter_event"] not in events:
                    errors.append(f"{label}: unknown encounter event `{entry['encounter_event']}`.")
                errors.extend(f"{label}: {e}" for e in secret_errors(world, entry.get("secret"), "secret"))
    errors.extend(mission_errors(world, world.get("missions") or {}))
    errors.extend(route_errors(world))
    errors.extend(logic_errors(world))
    return list(dict.fromkeys(errors))
