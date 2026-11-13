"""World authoring: entries, actions, missions, messages, persistent variables, character stats."""
from __future__ import annotations

import copy
import random
import re
from numbers import Number
import yaml
import main as engine
from core_engine import Mission, conditions, effects, starts
from core_engine.builder import (EVENT_POLICY_FIELDS, ITEM_POLICY_FIELDS, event_policy_errors, item_policy_errors,
                                 logic_errors, route_errors)
from core_engine.actions import REPEAT_RULES, repetition_errors
from core_engine.stats import MODES as STAT_MODES, CharacterStats, stats_config, validate_settings as validate_stat_settings
from core_engine.narration import DEFAULT_MESSAGES, validate_messages
from core_engine.variables import PROFILE_PATTERN, VariableStore, clamp, validate_changes, validate_definitions
from utils.config_loader import config

from .errors import AuthoringError
from .store import CONFIG_DIR, CONFIG_LOCK, _lines, _read_yaml, _write_yaml


ENTITY_FILES = {
    "Locations": (CONFIG_DIR / "locations.yaml", "locations", "mapping"),
    "NPCs": (CONFIG_DIR / "npcs.yaml", "characters", "list"),
    "Items": (CONFIG_DIR / "items.yaml", "objects", "list"),
    "Events": (CONFIG_DIR / "events.yaml", "events", "mapping"),
}

COMMON_TAGS = sorted(
    set(engine.ConfigEditor.COMMON_OUTCOME_TAGS)
    | {
        "urban", "maritime", "nature", "trade", "palace", "luxury",
        "discovery", "dark", "dangerous", "weapon", "defense", "social",
    }
)

def _entity_records(kind: str):
    path, key, shape = ENTITY_FILES[kind]
    # events.yaml is absent from config volumes created before Events existed.
    data = _read_yaml(path) if path.exists() or kind != "Events" else {}
    records = data.setdefault("world_definition", {}).setdefault(key, {} if shape == "mapping" else [])
    return path, data, records, shape

def _entity_name_values(kind: str):
    _, _, records, shape = _entity_records(kind)
    names = list(records) if shape == "mapping" else [record.get("name", "Unnamed") for record in records]
    return names, names[0] if names else None

def _event_choices():
    return [("None", "")] + [(name, name) for name in (config.get("world_definition.events") or {})]

def _conditions_text(conditions) -> str:
    return yaml.safe_dump(conditions, default_flow_style=True, sort_keys=False).strip() if conditions else ""

def load_entity(kind: str, name: str):
    """Return name, tags, descriptions, goals, the NPC options unique,
    encounter_event and encounter_repeat (None for other kinds), enabled_if,
    the event title, the item stat bonus, the location's start_eligible
    and start_weight, and the NPC's role and secret (None for other kinds)."""
    _, _, records, shape = _entity_records(kind)
    if shape == "mapping":
        record = records.get(name, {})
        descriptions = record.get("entered_descriptions") or record.get("descriptions", [])
        tags = record.get("tags", [])
    else:
        record = next((entry for entry in records if entry.get("name") == name), {})
        descriptions = record.get("descriptions", [])
        tags = record.get("associated_tags", [])
    npc = kind == "NPCs"
    return (
        record.get("name", name or ""),
        tags,
        "\n".join(descriptions),
        "\n".join(record.get("goals", [])),
        bool(record.get("unique", False)) if npc else None,
        (record.get("encounter_event") or None) if npc else None,
        bool(record.get("encounter_repeat", False)) if npc else None,
        record.get("enabled_if") or None,
        (record.get("title") or None) if kind == "Events" else None,
        (record.get("stat_bonus") or None) if kind == "Items" else None,
        bool(record.get("start_eligible", False)) if kind == "Locations" else None,
        record.get("start_weight", 1.0) if kind == "Locations" else None,
        (record.get("role") or None) if npc else None,
        (copy.deepcopy(record.get("secret")) or None) if npc else None,
    )

def _apply_start(record: dict, start_eligible, start_weight) -> None:
    """Set a location's start_eligible / start_weight (None keeps the stored value)."""
    if start_eligible is not None:
        record["start_eligible"] = bool(start_eligible)
    if start_weight is not None:
        record["start_weight"] = start_weight
    errors = starts.location_errors(record, "Location")
    if errors:
        raise AuthoringError("\n".join(errors))

def _apply_stat_bonus(record: dict, stat_bonus) -> None:
    """Set or clear an item's stat_bonus from a mapping or YAML text (None keeps it)."""
    if stat_bonus is None:
        return
    if isinstance(stat_bonus, str):
        try:
            stat_bonus = yaml.safe_load(stat_bonus) if stat_bonus.strip() else None
        except yaml.YAMLError as exc:
            raise AuthoringError(f"`Stat bonus` is not valid YAML: {exc}") from exc
    errors = engine._stat_usage_errors({"stat_bonus": stat_bonus}, list(stats_config().get("stats") or {}), "Item")
    if errors:
        raise AuthoringError("\n".join(errors))
    if stat_bonus:
        record["stat_bonus"] = stat_bonus
    else:
        record.pop("stat_bonus", None)

def role_names() -> list[str]:
    """The roles an NPC may play (npc_roles.roles)."""
    return list(((config.get("npc_roles") or {}).get("roles") or {}))


def secret_errors(secret, where="secret") -> list[str]:
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
    errors.extend(effects.validate(secret.get("effects"), _references(), where=f"{where}.effects"))
    return errors


def _apply_role_and_secret(record: dict, role, secret) -> None:
    """Set an NPC's role ("" removes it) and secret (a mapping or YAML text; {} or "" removes
    it); None keeps the stored value."""
    if role is not None:
        role = str(role).strip()
        if role and role not in role_names():
            raise AuthoringError(f"Unknown role `{role}`; use one of {', '.join(role_names())} (npc_roles.roles).")
        if role:
            record["role"] = role
        else:
            record.pop("role", None)
    if secret is not None:
        if isinstance(secret, str):
            try:
                secret = yaml.safe_load(secret) if secret.strip() else None
            except yaml.YAMLError as exc:
                raise AuthoringError(f"`Secret` is not valid YAML: {exc}") from exc
        if secret:
            errors = secret_errors(secret)
            if errors:
                raise AuthoringError("\n".join(errors))
            record["secret"] = secret
        else:
            record.pop("secret", None)


def _references() -> conditions.References:
    """The names conditions and effects may refer to in the current world."""
    return conditions.References.from_world(config.get("world_definition") or {})

def _apply_enabled_if(record: dict, enabled_if) -> None:
    """Set or clear an entry's enabled_if (a condition, see core_engine/conditions.py)
    from a mapping, a list or YAML text (None keeps it)."""
    if enabled_if is None:
        return
    if isinstance(enabled_if, str):
        try:
            enabled_if = yaml.safe_load(enabled_if) if enabled_if.strip() else None
        except yaml.YAMLError as exc:
            raise AuthoringError(f"`Enabled if` is not valid YAML: {exc}") from exc
    errors = conditions.validate(enabled_if, _references())
    if errors:
        raise AuthoringError("\n".join(errors))
    if enabled_if:
        record["enabled_if"] = enabled_if
    else:
        record.pop("enabled_if", None)

def save_entity(kind: str, selected_name: str, name: str, tags, descriptions: str, goals: str,
                unique=None, encounter_event=None, encounter_repeat=None, enabled_if=None, title=None,
                stat_bonus=None, start_eligible=None, start_weight=None, role=None, secret=None):
    """Save an entry. An option passed as None keeps its stored value (an NPC's
    unique defaults to generic); ``encounter_event=""`` removes the encounter, ``role=""``
    the NPC's own role (it is then inferred) and ``secret={}`` its secret."""
    name = (name or "").strip()
    if not name:
        raise AuthoringError("Name is required.")

    with CONFIG_LOCK:
        path, data, records, shape = _entity_records(kind)
        if shape == "mapping":
            if name != selected_name and name in records:
                raise AuthoringError(f"An entry named `{name}` already exists. Choose a unique name.")
            record = copy.deepcopy(records.get(selected_name, {}))
            record["tags"] = list(tags or [])
            record["entered_descriptions"] = _lines(descriptions)
            record["goals"] = _lines(goals)
            if kind == "Locations":
                _apply_start(record, start_eligible, start_weight)
            _apply_enabled_if(record, enabled_if)
            if kind == "Events" and title is not None:
                if title.strip():
                    record["title"] = title.strip()
                else:
                    record.pop("title", None)
            if selected_name != name:
                records.pop(selected_name, None)
            records[name] = record
        else:
            index = next((i for i, entry in enumerate(records) if entry.get("name") == selected_name), None)
            if name != selected_name and any(entry.get("name") == name for entry in records):
                raise AuthoringError(f"An entry named `{name}` already exists. Choose a unique name.")
            record = copy.deepcopy(records[index]) if index is not None else {}
            record.update(
                name=name,
                associated_tags=list(tags or []),
                descriptions=_lines(descriptions),
                goals=_lines(goals),
            )
            if kind == "NPCs":
                record["unique"] = bool(record.get("unique", False) if unique is None else unique)
                if encounter_event is not None:
                    events = config.get("world_definition.events") or {}
                    if encounter_event and encounter_event not in events:
                        raise AuthoringError(f"Unknown encounter event `{encounter_event}`. Define it in events.yaml first.")
                    record["encounter_event"] = encounter_event or None
                if encounter_repeat is not None:
                    record["encounter_repeat"] = bool(encounter_repeat)
                if not record.get("encounter_event"):
                    record.pop("encounter_event", None)
                    record.pop("encounter_repeat", None)
                _apply_role_and_secret(record, role, secret)
            _apply_enabled_if(record, enabled_if)
            if kind == "Items":
                _apply_stat_bonus(record, stat_bonus)
            if index is None:
                records.append(record)
            else:
                records[index] = record
        _write_yaml(path, data)
        config.clear_override()
        config.reload()
        choices = list(records) if shape == "mapping" else [entry["name"] for entry in records]
    return choices, name, f"Saved **{name}** to `{path.name}`."

def delete_entity(kind: str, selected_name: str):
    if not selected_name:
        raise AuthoringError("Select an entry to delete first.")

    with CONFIG_LOCK:
        path, data, records, shape = _entity_records(kind)
        if kind != "Events" and len(records) <= 1:
            raise AuthoringError(f"At least one {kind.lower()[:-1]} must remain in the configuration.")
        if shape == "mapping":
            if selected_name not in records:
                raise AuthoringError(f"`{selected_name}` no longer exists. Refresh the entry list.")
            del records[selected_name]
            choices = list(records)
        else:
            index = next((i for i, entry in enumerate(records) if entry.get("name") == selected_name), None)
            if index is None:
                raise AuthoringError(f"`{selected_name}` no longer exists. Refresh the entry list.")
            records.pop(index)
            choices = [entry["name"] for entry in records]
        _write_yaml(path, data)
        config.clear_override()
        config.reload()

    return choices, (choices[0] if choices else None), f"Deleted **{selected_name}** from `{path.name}`."

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

ACTION_REFERENCE = """
**Action fields:** `name` · `outcomes` · `repeat` (once / per_visit / always) ·
`retry_on_failure` (true: a failed attempt does not use the action up) · `exclusive_group` ·
`triggers_event` · `skill_check` ({stat, modifier}) · `required_tag` · `required_object` · `required_coins` ·
`collects_object` · `consumes_object` · `enabled_if` (a condition: the action is offered only while it holds)

**Outcome fields:** `desc` · `tags` · `success_prob` · `health_change` · `coin_change` · `spawn` ·
`move_to` · `send_to` (one way) · `move_to_tags` · `target_tags` (a discovery led by tags) · `reveal_location` ·
`reveal_npc` · `reveal_object` · `unlock_exit` · `return_to_previous` · `setup_id` / `requires_setup` / `payoff_id` ·
`lead_to_known` · `remove_npc` (true or a name) · `set_variables` · `change_variables` ·
`on_check` (pass / fail) · `change_stats` · `effects` (a list, e.g. `[{give_item: Royal Pass}, {change_variable: {reputation: 1}}]`) ·
`variants` (other ways to tell it, used in turn when it is told again)

**Conditions** (`enabled_if`): `{all: [...]}` · `{any: [...]}` · `{not: ...}` · `{variable: x, op: gte, value: 2}` ·
`{stat: s, op: gte, value: 60}` · `{resource: coins, op: gte, value: 20}` · `{has_item: X}` · `{visited: L}` ·
`{at_location: L}` · `{npc_removed: N}` · `{event_done: E}` · `{event_succeeded: E}` · `{event_failed: E}` ·
`{item_collected: X}` · `{action_done: A}` · `{outcome_tag: t}` · `{mission: m}` · `{secret_known: N}` (the NPC's
secret came out), or the shorthand `{variable: value}`.

**Effects:** `set_variable` · `change_variable` · `set_stat` · `change_stat` · `give_item` · `take_item` ·
`change_coins` · `change_health` · `remove_npc` · `reveal_npc` · `reveal_item` · `reveal_location` ·
`unlock_location` · `move_to` · `send_to` · `start_event` · `complete_mission` · `end_story`
"""

def _record(kind: str, name: str):
    path, data, records, shape = _entity_records(kind)
    if shape == "mapping":
        record = records.get(name)
    else:
        record = next((entry for entry in records if entry.get("name") == name), None)
    if record is None:
        raise AuthoringError(f"`{name}` does not exist in {kind}.")
    return path, data, record

def load_entity_yaml(kind: str, name: str) -> str:
    """Every field of an entry as YAML, as stored (its actions included)."""
    if not name:
        return "{}\n"
    return yaml.safe_dump(copy.deepcopy(_record(kind, name)[2]), sort_keys=False, allow_unicode=True)


def save_entity_yaml(kind: str, name: str, yaml_text: str) -> str:
    """Validate and save every field of an entry from YAML (the entry keeps its name: rename it
    with the entry form). The whole world is checked with the entry in place (fields, actions,
    conditions, effects, references, the NPC's role and secret); nothing is written when it is
    invalid."""
    try:
        record = yaml.safe_load(yaml_text) if (yaml_text or "").strip() else None
    except yaml.YAMLError as exc:
        raise AuthoringError(f"Could not read the YAML: {exc}") from exc
    if not isinstance(record, dict):
        raise AuthoringError("The entry must be a YAML mapping of its fields.")
    path, _, records, shape = _entity_records(kind)
    if shape == "list":
        if record.setdefault("name", name) != name:
            raise AuthoringError("Rename an entry with the entry form (Name), not here.")
    else:
        record.pop("name", None)
    errors = validate_entity_actions(kind, record.get("actions") or [])
    if kind == "NPCs":
        if record.get("role") is not None and record["role"] not in role_names():
            errors.append(f"Unknown role `{record['role']}`; use one of {', '.join(role_names())}.")
        errors.extend(secret_errors(record.get("secret")))
    errors.extend(conditions.validate(record.get("enabled_if"), _references(), where="enabled_if"))
    key = ENTITY_FILES[kind][1]
    world = copy.deepcopy(config.get("world_definition") or {})
    entries = world.setdefault(key, {} if shape == "mapping" else [])
    if shape == "mapping":
        if name not in entries:
            raise AuthoringError(f"`{name}` does not exist in {kind}.")
        entries[name] = record
    else:
        index = next((i for i, entry in enumerate(entries) if entry.get("name") == name), None)
        if index is None:
            raise AuthoringError(f"`{name}` does not exist in {kind}.")
        entries[index] = record
    from .store import _validate_complete_config

    errors.extend(_validate_complete_config(entries, None, f"world_definition.{key}"))
    errors.extend(logic_errors(world))
    if errors:
        raise AuthoringError("The entry is invalid:\n" + "\n".join(f"- {e}" for e in dict.fromkeys(errors)))
    with CONFIG_LOCK:
        path, data, stored = _record(kind, name)
        stored.clear()
        stored.update(record)
        _write_yaml(path, data)
        config.clear_override()
        config.reload()
    return f"Saved every field of **{name}** to `{path.name}`."


MAP_PATH = CONFIG_DIR / "locations.yaml"   # the map lives with the locations (world_definition.routes)
MAP_HELP = """\
# The world's map: which places border which (world_definition.routes in locations.yaml).
# A route holds both ways. A move only goes to a neighbour; a far place is reached through
# the places between, one turn each; being sent (an arrest) is the only jump.
# `routes: null` (or no routes): every story draws its own map.
"""


def load_map_yaml() -> str:
    """The world's map (world_definition.routes) as a YAML document."""
    routes = ((_read_yaml(MAP_PATH).get("world_definition") or {}).get("routes"))
    return MAP_HELP + yaml.safe_dump({"routes": routes}, sort_keys=False, allow_unicode=True, width=1 << 16,
                                     default_flow_style=None)


def save_map_yaml(yaml_text: str):
    """Validate and save the world's map: every place and neighbour must be a location."""
    try:
        data = yaml.safe_load(yaml_text) if (yaml_text or "").strip() else {}
    except yaml.YAMLError as exc:
        raise AuthoringError(f"Could not read the YAML: {exc}") from exc
    if not isinstance(data, dict) or set(data) - {"routes"}:
        raise AuthoringError("The document must contain only `routes:` (a mapping of place: [neighbours], or null).")
    routes = data.get("routes")
    world = dict(config.get("world_definition") or {})
    errors = route_errors({**world, "routes": routes})
    if errors:
        raise AuthoringError("The map is invalid:\n" + "\n".join(f"- {e}" for e in errors))
    path = MAP_PATH
    with CONFIG_LOCK:
        stored = _read_yaml(path)
        definition = stored.setdefault("world_definition", {})
        if routes:
            definition["routes"] = routes
        else:
            definition.pop("routes", None)
        # Keep routes first, as written by hand.
        stored["world_definition"] = {**({"routes": routes} if routes else {}),
                                      **{k: v for k, v in definition.items() if k != "routes"}}
        _write_yaml(path, stored)
        config.clear_override()
        config.reload()
    count = len(routes or {})
    return load_map_yaml(), (f"Saved the map: {count} place(s) with routes." if routes
                             else "Saved: no authored map; every story draws its own.")


def load_entity_actions(kind: str, name: str) -> list:
    """The actions of an entry exactly as stored in its YAML file."""
    return copy.deepcopy(_record(kind, name)[2].get("actions") or [])

def load_entity_actions_yaml(kind: str, name: str) -> str:
    if not name:
        return "[]\n"
    actions = load_entity_actions(kind, name)
    return yaml.safe_dump(actions, sort_keys=False, allow_unicode=True) if actions else "[]\n"

def validate_entity_actions(kind: str, actions) -> list[str]:
    """Structural and reference checks for an entry's actions (empty when valid)."""
    if not isinstance(actions, list):
        return ["Actions must be a list."]
    world = config.get("world_definition") or {}
    locations = set(world.get("locations") or {})
    npcs = {entry.get("name") for entry in world.get("characters") or []}
    items = {entry.get("name") for entry in world.get("objects") or []}
    events = set(world.get("events") or {})
    specs = world.get("variables") or {}
    refs = _references()
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
            errors.extend(validate_changes(outcome.get("set_variables"), outcome.get("change_variables"), specs, where=where))
            errors.extend(effects.validate(outcome.get("effects"), refs, where=f"{where}.effects"))
    stat_names = list((world.get("character_stats") or {}).get("stats") or {})
    errors.extend(engine._stat_usage_errors({"actions": [a for a in actions if isinstance(a, dict)]}, stat_names, kind))
    return errors

def save_entity_actions(kind: str, name: str, actions) -> str:
    """Validate and save an entry's actions (a list, or YAML text of a list)."""
    if isinstance(actions, str):
        try:
            actions = yaml.safe_load(actions) if actions.strip() else []
        except yaml.YAMLError as exc:
            raise AuthoringError(f"The actions are not valid YAML: {exc}") from exc
    actions = actions or []
    errors = validate_entity_actions(kind, actions)
    if errors:
        raise AuthoringError("The actions are invalid:\n" + "\n".join(f"- {e}" for e in errors))
    with CONFIG_LOCK:
        path, data, record = _record(kind, name)
        record["actions"] = actions
        _write_yaml(path, data)
        config.clear_override()
        config.reload()
    return f"Saved {len(actions)} action(s) of **{name}** to `{path.name}`."

POLICY_FIELDS = {"Events": EVENT_POLICY_FIELDS, "Items": ITEM_POLICY_FIELDS}
POLICY_HELP = {
    "Events": "# When the event may start (every way in follows these rules):\n"
              "#   trigger: action | random   chance: 0.2 (random, per turn)   weight: 1 (random)\n"
              "#   cooldown: 3 (turns)   max_occurrences: 2   repeat: once | per_location | per_visit | always\n",
    "Items": "# unique: true makes the item exist once per story, at `location` (or where it fits best).\n",
}

def load_entity_policy(kind: str, name: str) -> dict:
    """An event's start rules or an item's uniqueness, as stored (fields left out use defaults)."""
    if kind not in POLICY_FIELDS:
        raise AuthoringError("Only events and items have uniqueness and scheduling rules here "
                             "(NPC uniqueness is the Unique NPC option).")
    record = _record(kind, name)[2]
    return {field: copy.deepcopy(record[field]) for field in POLICY_FIELDS[kind] if field in record}

def load_entity_policy_yaml(kind: str, name: str) -> str:
    if kind not in POLICY_FIELDS or not name:
        return "# Events and items only.\n"
    policy = load_entity_policy(kind, name)
    return POLICY_HELP[kind] + (yaml.safe_dump(policy, sort_keys=False, allow_unicode=True) if policy else "{}\n")

def save_entity_policy(kind: str, name: str, policy) -> dict:
    """Validate and store an event's start rules or an item's uniqueness (a mapping
    or YAML text). The whole policy replaces the stored one; nothing is written
    when it is invalid."""
    if kind not in POLICY_FIELDS:
        raise AuthoringError("Only events and items have uniqueness and scheduling rules here.")
    if isinstance(policy, str):
        try:
            policy = yaml.safe_load(policy) if policy.strip() else {}
        except yaml.YAMLError as exc:
            raise AuthoringError(f"The rules are not valid YAML: {exc}") from exc
    policy = policy or {}
    if not isinstance(policy, dict):
        raise AuthoringError("The rules must be a mapping.")
    unknown = sorted(set(policy) - set(POLICY_FIELDS[kind]))
    errors = [f"Unknown field(s) {', '.join(unknown)}; use {', '.join(POLICY_FIELDS[kind])}."] if unknown else []
    if kind == "Events":
        errors += event_policy_errors(policy, f"Event '{name}'")
    else:
        errors += item_policy_errors(policy, f"Item '{name}'", set(config.get("world_definition.locations") or {}))
    if errors:
        raise AuthoringError("The rules are invalid:\n" + "\n".join(f"- {e}" for e in errors))
    with CONFIG_LOCK:
        path, data, record = _record(kind, name)
        for field in POLICY_FIELDS[kind]:
            record.pop(field, None)
        record.update(policy)
        _write_yaml(path, data)
        config.clear_override()
        config.reload()
    return load_entity_policy(kind, name)

def save_entity_policy_ui(kind: str, name: str, yaml_text: str):
    if not name:
        raise AuthoringError("Select an entry first.")
    save_entity_policy(kind, name, yaml_text)
    return load_entity_policy_yaml(kind, name), f"Saved the rules of **{name}**."

REPEAT_DEFAULT = "default"  # the action leaves `repeat` out: action_rules.default_repeat decides

def entity_action_choices(kind: str, name: str) -> list[tuple[str, int]]:
    """(label, index) of an entry's actions, for picking one in the editor."""
    if not name:
        return []
    return [(f"{index + 1}. {action.get('name', 'Action')}", index)
            for index, action in enumerate(load_entity_actions(kind, name))]

def load_action_repetition(kind: str, name: str, index) -> tuple[str, bool]:
    """(repeat or "default", retry_on_failure) of one action of an entry."""
    actions = load_entity_actions(kind, name) if name else []
    if index is None or not 0 <= int(index) < len(actions):
        return REPEAT_DEFAULT, False
    action = actions[int(index)]
    return action.get("repeat") or REPEAT_DEFAULT, bool(action.get("retry_on_failure", False))

def save_action_repetition(kind: str, name: str, index, repeat: str, retry_on_failure: bool) -> str:
    """Set how often one action is offered and whether a failure uses it up; the
    whole action list is validated before anything is written."""
    actions = load_entity_actions(kind, name) if name else []
    if index is None or not 0 <= int(index) < len(actions):
        raise AuthoringError("Select an action first.")
    action = actions[int(index)]
    if repeat in (None, "", REPEAT_DEFAULT):
        action.pop("repeat", None)
    else:
        action["repeat"] = repeat
    if retry_on_failure:
        action["retry_on_failure"] = True
    else:
        action.pop("retry_on_failure", None)
    save_entity_actions(kind, name, actions)
    retry = "retried after a failure" if retry_on_failure else "used up by any attempt"
    return f"Saved **{action.get('name', 'Action')}**: repeat `{action.get('repeat', REPEAT_DEFAULT)}`, {retry}."

def save_entity_actions_ui(kind: str, name: str, yaml_text: str):
    if not name:
        raise AuthoringError("Select an entry first.")
    message = save_entity_actions(kind, name, yaml_text)
    return load_entity_actions_yaml(kind, name), message

MESSAGES_PATH = CONFIG_DIR / "messages.yaml"

VARIABLES_PATH = CONFIG_DIR / "variables.yaml"

def load_variables_yaml() -> str:
    if VARIABLES_PATH.exists():
        return VARIABLES_PATH.read_text(encoding="utf-8")
    return "world_definition:\n  variables: {}\n"

def _parse_variable_definitions(yaml_text: str) -> dict:
    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as exc:
        raise AuthoringError(f"Could not read the YAML: {exc}") from exc
    if not isinstance(data, dict) or set(data) - {"world_definition"} or set(data.get("world_definition") or {}) - {"variables"}:
        raise AuthoringError("The document must contain only `world_definition: {variables: ...}`.")
    specs = (data.get("world_definition") or {}).get("variables") or {}
    errors = validate_definitions(specs)
    if errors:
        raise AuthoringError("The variable definitions are invalid:\n" + "\n".join(f"- {e}" for e in errors))
    return specs

def save_variable_definitions(specs: dict) -> str:
    """Validate and save variable definitions given as a mapping (used by the API)."""
    errors = validate_definitions(specs)
    if errors:
        raise AuthoringError("The variable definitions are invalid:\n" + "\n".join(f"- {e}" for e in errors))
    text = yaml.safe_dump({"world_definition": {"variables": specs}}, sort_keys=False, allow_unicode=True)
    with CONFIG_LOCK:
        VARIABLES_PATH.write_text(text, encoding="utf-8")
        config.clear_override()
        config.reload()
    return f"Saved {len(specs)} persistent variable definition(s)."

def save_variables_yaml(yaml_text: str):
    """Validate and save the variable definitions editor, keeping its comments."""
    specs = _parse_variable_definitions(yaml_text)
    with CONFIG_LOCK:
        VARIABLES_PATH.write_text(yaml_text, encoding="utf-8")
        config.clear_override()
        config.reload()
    return yaml_text, f"Saved {len(specs)} persistent variable definition(s)."

def _profile(profile: str | None = None) -> str:
    profile = (profile or "").strip() or config.get("persistence.profile", "default")
    if not re.match(PROFILE_PATTERN, profile):
        raise AuthoringError("A player profile name may use letters, digits, `_`, `-` and `.` (up to 64).")
    return profile

def player_profiles() -> list[str]:
    return sorted(set(VariableStore().profiles()) | {config.get("persistence.profile", "default")})

def stored_values_yaml(profile: str | None = None) -> str:
    data = stored_variables(profile)
    values = {k: v for k, v in data["values"].items() if k != "playthroughs"}
    return yaml.safe_dump({"playthroughs": data["playthroughs"], "values": values}, sort_keys=False, allow_unicode=True)

def save_stored_values(profile: str | None, values: dict, playthroughs=None) -> dict:
    """Overwrite a profile's stored values (e.g. to test later playthroughs)."""
    profile = _profile(profile)
    specs = config.get("world_definition.variables") or {}
    errors = validate_changes(values, None, specs, where="values") if isinstance(values, dict) else ["`values` must be a mapping."]
    if playthroughs is not None and (not isinstance(playthroughs, int) or isinstance(playthroughs, bool) or playthroughs < 0):
        errors.append("`playthroughs` must be a whole number of 0 or more.")
    if errors:
        raise AuthoringError("The stored values are invalid:\n" + "\n".join(f"- {e}" for e in errors))
    store = VariableStore()
    merged = {**store.load(profile, specs), **values}
    merged = {name: clamp(name, value, specs) for name, value in merged.items()}
    store.save(profile, merged, playthroughs=playthroughs)
    return stored_variables(profile)

def save_stored_values_yaml(profile: str, yaml_text: str):
    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as exc:
        raise AuthoringError(f"Could not read the YAML: {exc}") from exc
    if not isinstance(data, dict) or set(data) - {"playthroughs", "values"}:
        raise AuthoringError("Use `playthroughs: <number>` and `values: {<variable>: <value>}`.")
    save_stored_values(profile, data.get("values") or {}, data.get("playthroughs"))
    return stored_values_yaml(profile), f"Saved the stored values of profile `{_profile(profile)}`."

def stored_variables(profile: str | None = None) -> dict:
    """The stored profile: current values (defaults filled in) and playthroughs."""
    profile = _profile(profile)
    store = VariableStore()
    record = store.profile(profile)
    return {
        "profile": profile,
        "values": store.load(profile),
        "playthroughs": record["playthroughs"],
        "updated": record["updated"],
        "path": store.path,
    }

def stored_variables_text(profile: str | None = None) -> str:
    data = stored_variables(profile)
    lines = [f"**Profile:** `{data['profile']}` · **Playthroughs finished:** {data['playthroughs']}"]
    for name, value in data["values"].items():
        if name != "playthroughs":
            lines.append(f"- `{name}` = `{value}`")
    return "\n".join(lines)

def reset_stored_variables(profile: str | None = None) -> str:
    profile = _profile(profile)
    VariableStore().reset(profile)
    return stored_variables_text(profile)

def reset_stored_values_ui(profile: str):
    reset_stored_variables(profile)
    return stored_values_yaml(profile), f"Profile `{_profile(profile)}` starts over as a new player."

STATS_PATH = CONFIG_DIR / "stats.yaml"

PLAYER_STAT_CHOICES = ["story default", *STAT_MODES]

def load_stats_yaml() -> str:
    if STATS_PATH.exists():
        return STATS_PATH.read_text(encoding="utf-8")
    return "world_definition:\n  character_stats:\n    stats: {}\n"

def _store_stats(text: str) -> None:
    with CONFIG_LOCK:
        STATS_PATH.write_text(text, encoding="utf-8")
        config.clear_override()
        config.reload()

def save_stats_settings(settings: dict) -> str:
    """Validate and save the character stat definitions (used by the API)."""
    errors = validate_stat_settings(settings)
    if errors:
        raise AuthoringError("The character stats are invalid:\n" + "\n".join(f"- {e}" for e in errors))
    _store_stats(yaml.safe_dump({"world_definition": {"character_stats": settings}}, sort_keys=False, allow_unicode=True))
    return f"Saved {len(settings.get('stats') or {})} character stat(s)."

def save_stats_yaml(yaml_text: str):
    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as exc:
        raise AuthoringError(f"Could not read the YAML: {exc}") from exc
    if not isinstance(data, dict) or set(data) - {"world_definition"} or set(data.get("world_definition") or {}) - {"character_stats"}:
        raise AuthoringError("The document must contain only `world_definition: {character_stats: ...}`.")
    settings = (data.get("world_definition") or {}).get("character_stats") or {}
    errors = validate_stat_settings(settings)
    if errors:
        raise AuthoringError("The character stats are invalid:\n" + "\n".join(f"- {e}" for e in errors))
    _store_stats(yaml_text)
    return yaml_text, f"Saved {len(settings.get('stats') or {})} character stat(s). New games use them."

def allocate_stats(mode: str | None = None, points: dict | None = None) -> dict:
    """Preview or check a player's stats: points, 1-100 values, and problems."""
    settings = stats_config()
    names = list(settings.get("stats") or {})
    try:
        stats = CharacterStats(points=points, mode=None if points is not None else (mode or None), rng=random.Random())
    except ValueError as exc:
        return {"mode": "defined" if points is not None else mode, "points": points or {}, "values": {},
                "errors": str(exc).split("; "), "stats": names}
    return {**stats.summary(), "errors": [], "stats": names, "budget": stats.budget}

def _player_stats(choice: str | None, points_text: str | None):
    """(points, mode) for GameState from the play tab's stat choice."""
    if choice in (None, "", "story default"):
        return None, None
    if choice != "defined":
        return None, choice
    try:
        points = yaml.safe_load(points_text or "") or {}
    except yaml.YAMLError as exc:
        raise AuthoringError(f"The stat points are not valid YAML: {exc}") from exc
    result = allocate_stats(points=points)
    if result["errors"]:
        raise AuthoringError("Your stat points are invalid:\n" + "\n".join(f"- {e}" for e in result["errors"]))
    return points, None

def defined_points_text() -> str:
    """YAML of the story's default ``defined_points``, for the play tab."""
    return yaml.safe_dump(stats_config().get("defined_points") or {}, sort_keys=False)


def stats_points_text(mode: str) -> str:
    """YAML of a random or balanced split, for the play tab's helper buttons."""
    result = allocate_stats(mode=mode)
    return yaml.safe_dump(result["points"], default_flow_style=False, sort_keys=False)

def check_points_text(points_text: str) -> str:
    try:
        points = yaml.safe_load(points_text or "") or {}
    except yaml.YAMLError as exc:
        return f"Not valid YAML: {exc}"
    result = allocate_stats(points=points if isinstance(points, dict) else {})
    if result["errors"]:
        return "⚠️ " + " ".join(result["errors"])
    values = ", ".join(f"{k} {v}" for k, v in result["values"].items())
    return f"✅ {sum(result['points'].values())} of {result['budget']['total_points']} points · values: {values}"

MISSIONS_PATH = CONFIG_DIR / "missions.yaml"

def validate_missions(missions) -> list[str]:
    if not isinstance(missions, dict):
        return ["Missions must be a mapping of mission ids to missions."]
    world = config.get("world_definition") or {}
    references = {
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
        goals = [(f"stages[{n}].goal", (s or {}).get("goal") if isinstance(s, dict) else None) for n, s in enumerate(stages or [], 1)]
        if mission.get("goal") is not None or not goals:
            goals.insert(0, ("goal", mission.get("goal")))
        refs = conditions.References.from_world({**world, "missions": missions})
        for where, goal in goals:
            if not isinstance(goal, dict) or not goal:
                errors.append(f"{label} needs a `{where}` mapping.")
                continue
            bad = sorted(set(goal) - set(Mission.GOAL_KEYS))
            if bad:
                errors.append(f"{label}: unknown goal key(s) in `{where}` {', '.join(bad)}; use {', '.join(Mission.GOAL_KEYS)}.")
            for key, known in references.items():
                if key in goal and goal[key] not in known:
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

def load_missions() -> dict:
    return copy.deepcopy(config.get("world_definition.missions") or {})

def load_missions_yaml() -> str:
    if MISSIONS_PATH.exists():
        return MISSIONS_PATH.read_text(encoding="utf-8")
    return "world_definition:\n  missions: {}\n"

def _store_missions(text: str) -> None:
    with CONFIG_LOCK:
        MISSIONS_PATH.write_text(text, encoding="utf-8")
        config.clear_override()
        config.reload()

def save_missions(missions: dict) -> str:
    errors = validate_missions(missions)
    if errors:
        raise AuthoringError("The missions are invalid:\n" + "\n".join(f"- {e}" for e in errors))
    _store_missions(yaml.safe_dump({"world_definition": {"missions": missions}}, sort_keys=False, allow_unicode=True))
    return f"Saved {len(missions)} mission(s)."

def save_missions_yaml(yaml_text: str):
    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as exc:
        raise AuthoringError(f"Could not read the YAML: {exc}") from exc
    if not isinstance(data, dict) or set(data) - {"world_definition"} or set(data.get("world_definition") or {}) - {"missions"}:
        raise AuthoringError("The document must contain only `world_definition: {missions: ...}`.")
    missions = (data.get("world_definition") or {}).get("missions") or {}
    errors = validate_missions(missions)
    if errors:
        raise AuthoringError("The missions are invalid:\n" + "\n".join(f"- {e}" for e in errors))
    _store_missions(yaml_text)
    return yaml_text, f"Saved {len(missions)} mission(s). New stories draw from them."

def effective_messages() -> dict:
    """Every message key with its current text (customised or default)."""
    configured = config.get("messages") or {}
    return {key: configured.get(key, default) for key, default in DEFAULT_MESSAGES.items()}

def load_messages_yaml() -> str:
    if MESSAGES_PATH.exists():
        return MESSAGES_PATH.read_text(encoding="utf-8")
    return yaml.safe_dump({"messages": DEFAULT_MESSAGES}, sort_keys=False, allow_unicode=True)

def _store_messages(text: str) -> None:
    with CONFIG_LOCK:
        MESSAGES_PATH.write_text(text, encoding="utf-8")
        config.clear_override()
        config.reload()

def save_messages(messages: dict) -> str:
    """Validate and save story messages given as a mapping (used by the API)."""
    errors = validate_messages(messages)
    if errors:
        raise AuthoringError("The story messages are invalid:\n" + "\n".join(f"- {e}" for e in errors))
    _store_messages(yaml.safe_dump({"messages": messages}, sort_keys=False, allow_unicode=True))
    return f"Saved {len(messages)} story message(s) to `messages.yaml`."

def save_messages_yaml(yaml_text: str):
    """Validate and save the story messages editor, keeping its comments."""
    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as exc:
        raise AuthoringError(f"Could not read the YAML: {exc}") from exc
    if not isinstance(data, dict) or set(data) - {"messages"}:
        raise AuthoringError("The document must contain a single top-level `messages:` mapping.")
    messages = data.get("messages") or {}
    errors = validate_messages(messages)
    if errors:
        raise AuthoringError("The story messages are invalid:\n" + "\n".join(f"- {e}" for e in errors))
    _store_messages(yaml_text)
    return yaml_text, f"Saved {len(messages)} story message(s). New stories use them straight away."
