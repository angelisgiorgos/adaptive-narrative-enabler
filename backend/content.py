"""World authoring: entries, actions, missions, messages, persistent variables, character stats."""
from __future__ import annotations

import collections
import copy
import hashlib
import json
import random
import re
import uuid
from numbers import Number
import yaml
import main as engine
from core_engine import Mission, conditions, content_rules, effects, starts
from core_engine.builder import (EVENT_POLICY_FIELDS, ITEM_POLICY_FIELDS, event_policy_errors, item_policy_errors,
                                 logic_errors, route_errors)
from core_engine.actions import REPEAT_RULES, repetition_errors
from core_engine.stats import MODES as STAT_MODES, CharacterStats, stats_config, validate_settings as validate_stat_settings
from core_engine.narration import DEFAULT_MESSAGES, validate_messages
from core_engine.variables import PROFILE_PATTERN, VariableStore, clamp, validate_changes, validate_definitions
from utils.config_loader import config

from .errors import AuthoringError, ConflictError
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
        "\n".join(_goal_text(goal) for goal in record.get("goals") or []),
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

def _goal_text(goal) -> str:
    """A goal as one line of the editor: its text, or a structured goal ({goal, tags, needed, reward})
    as one line of YAML, so that it survives being edited and saved."""
    if isinstance(goal, str):
        return goal
    return yaml.safe_dump(goal, default_flow_style=True, sort_keys=False, allow_unicode=True, width=10**9).strip()


def _goal_lines(goals) -> list:
    """The goals typed in the editor, a line each: a line written as a YAML mapping ({goal: ..., tags:
    [...]}) is a structured goal again; any other line is a goal's text."""
    found = []
    for line in _lines(goals):
        if line.startswith("{"):
            try:
                value = yaml.safe_load(line)
            except yaml.YAMLError:
                value = None
            if isinstance(value, dict):
                found.append(value)
                continue
        found.append(line)
    return found


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
    return content_rules.secret_errors(config.get("world_definition") or {}, secret, where)


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
            record["goals"] = _goal_lines(goals)
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
                goals=_goal_lines(goals),
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
    """Delete an entry (at least one location, NPC and item remains; events may all go). Returns (the
    entries left, the first of them, a message); remove_entity also keeps it to be restored."""
    result = remove_entity(kind, selected_name)
    choices = result["entities"]
    return choices, (choices[0] if choices else None), result["message"]


def remove_entity(kind: str, selected_name: str) -> dict:
    """delete_entity, keeping the entry whole, with its place, to be restored (restore_deletion).
    Returns {entities, message, undo}."""
    if not selected_name:
        raise AuthoringError("Select an entry to delete first.")

    with CONFIG_LOCK:
        path, data, records, shape = _entity_records(kind)
        if kind != "Events" and len(records) <= 1:
            raise AuthoringError(f"At least one {kind.lower()[:-1]} must remain in the configuration.")
        names = _names_in(records, shape)
        if selected_name not in names:
            raise AuthoringError(f"`{selected_name}` no longer exists. Refresh the entry list.")
        index = names.index(selected_name)
        known = _entity_known_errors(kind, records[selected_name] if shape == "mapping" else records[index])
        record = records.pop(selected_name) if shape == "mapping" else records.pop(index)
        _write_yaml(path, data)
        config.clear_override()
        config.reload()
        choices = _names_in(records, shape)
        undo = _keep_deletion({"type": "entity", "kind": kind, "name": selected_name, "record": record, "file": path.name,
                               "known_errors": known}, names, index, choices)
    return {"entities": choices, "message": f"Deleted **{selected_name}** from `{path.name}`.", "undo": undo}

ACTION_FIELDS = content_rules.ACTION_FIELDS

OUTCOME_FIELDS = content_rules.OUTCOME_FIELDS

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


def _entry_errors(kind: str, record: dict) -> list:
    """An entry's own problems: its actions, an NPC's role and secret, its condition."""
    errors = validate_entity_actions(kind, record.get("actions") or [])
    if kind == "NPCs":
        if record.get("role") is not None and record["role"] not in role_names():
            errors.append(f"Unknown role `{record['role']}`; use one of {', '.join(role_names())}.")
        errors.extend(secret_errors(record.get("secret")))
    errors.extend(conditions.validate(record.get("enabled_if"), _references(), where="enabled_if"))
    return errors


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
    errors = _entry_errors(kind, record)
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
    # Only the problems this edit makes block it: the world's earlier ones (e.g. a config folder
    # from an older release) must not stop every unrelated edit; they are reported.
    earlier = list(dict.fromkeys(logic_errors(config.get("world_definition") or {})))
    errors.extend(e for e in logic_errors(world) if e not in earlier)
    if errors:
        raise AuthoringError("The entry is invalid:\n" + "\n".join(f"- {e}" for e in dict.fromkeys(errors)))
    with CONFIG_LOCK:
        path, data, stored = _record(kind, name)
        stored.clear()
        stored.update(record)
        _write_yaml(path, data)
        config.clear_override()
        config.reload()
    still = [e for e in dict.fromkeys(logic_errors(config.get("world_definition") or {})) if e in earlier]
    note = (f" The world still has {len(still)} earlier problem(s): " + "; ".join(still[:3])) if still else ""
    return f"Saved every field of **{name}** to `{path.name}`.{note}"


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
    return content_rules.action_errors(config.get("world_definition") or {}, kind, actions)

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
    return content_rules.mission_errors(config.get("world_definition") or {}, missions)

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


# ---- the studio's forms: one action, one quest, an entry's details, the map (JSON) ------------------

def load_entity_action(kind: str, name: str, index: int) -> dict:
    """One action of an entry (by its place in the list), as stored."""
    actions = load_entity_actions(kind, name)
    if not 0 <= index < len(actions):
        raise AuthoringError(f"`{name}` has no action {index + 1} (it has {len(actions)}).")
    return copy.deepcopy(actions[index])


def save_entity_action(kind: str, name: str, index: int, action: dict) -> list:
    """Replace one action of an entry (``index`` = the number of actions: a new one, at the end);
    the whole list is checked as when it is saved at once. Returns the actions."""
    if not isinstance(action, dict) or not str(action.get("name", "")).strip():
        raise AuthoringError("An action needs a name.")
    actions = load_entity_actions(kind, name)
    if index == len(actions):
        actions.append(action)
    elif 0 <= index < len(actions):
        actions[index] = action
    else:
        raise AuthoringError(f"`{name}` has no action {index + 1} (it has {len(actions)}).")
    save_entity_actions(kind, name, actions)
    return load_entity_actions(kind, name)


def delete_entity_action(kind: str, name: str, index: int) -> list:
    """Delete one action of an entry (the others are checked as a save checks them). Returns the actions."""
    return remove_entity_action(kind, name, index)["actions"]


def remove_entity_action(kind: str, name: str, index: int, expected: str | None = None) -> dict:
    """delete_entity_action, keeping the action whole, with its place, to be restored. ``expected``:
    the name the action must have (else ConflictError: the actions changed, and nothing is deleted).
    Returns {actions, message, undo}."""
    with CONFIG_LOCK:
        actions = load_entity_actions(kind, name)
        if not 0 <= index < len(actions):
            raise AuthoringError(f"`{name}` has no action {index + 1} (it has {len(actions)}).")
        found = str(actions[index].get("name", ""))
        if expected is not None and found != expected:
            raise ConflictError(f"Action {index + 1} of `{name}` is now `{found}`, not `{expected}` (its actions changed "
                                "since): nothing was deleted. Reload them and try again.")
        before = copy.deepcopy(actions)
        known = validate_entity_actions(kind, before)
        removed = actions.pop(index)
        save_entity_actions(kind, name, actions)
        stored = load_entity_actions(kind, name)
        undo = _keep_deletion({"type": "action", "kind": kind, "entry": name, "name": found, "record": removed,
                               "known_errors": known}, before, index, stored)
    return {"actions": stored, "message": f"Deleted the action `{found}` of `{name}`.", "undo": undo}


def save_mission(key: str, mission: dict, rename: str | None = None) -> dict:
    """Add or replace one mission (``rename``: its new key, in the same place); every mission is
    checked as when they are saved at once. Returns the missions."""
    key, new_key = str(key).strip(), str(rename or key).strip()
    if not new_key or not re.fullmatch(r"[A-Za-z0-9_]+", new_key):
        raise AuthoringError("A mission's key is letters, digits and _ (e.g. rid_the_market_of_the_thief).")
    if not isinstance(mission, dict):
        raise AuthoringError("A mission is a mapping of its fields.")
    missions = _stored_missions()
    if new_key != key and new_key in missions:
        raise AuthoringError(f"A mission `{new_key}` exists already.")
    updated = {}
    for name, value in missions.items():
        updated[new_key if name == key else name] = mission if name == key else value
    if key not in missions:
        updated[new_key] = mission
    save_missions(updated)
    return updated


def delete_mission(key: str) -> dict:
    """Delete a mission (the others are checked as a save checks them). Returns the missions left."""
    return remove_mission(key)["missions"]


def remove_mission(key: str) -> dict:
    """delete_mission, keeping the mission whole, with its place, to be restored. Returns
    {missions, message, undo}."""
    with CONFIG_LOCK:
        missions = _stored_missions()
        if key not in missions:
            raise AuthoringError(f"No mission `{key}`.")
        keys = list(missions)
        known = validate_missions(missions)
        record = missions.pop(key)
        save_missions(missions)
        undo = _keep_deletion({"type": "mission", "name": key, "title": (record or {}).get("title") or key,
                               "record": record, "known_errors": known}, keys, keys.index(key), list(missions))
    return {"missions": missions, "message": f"Deleted the mission `{key}`.", "undo": undo}


def _stored_missions() -> dict:
    """The missions as missions.yaml has them (what a save writes), else the active ones."""
    if MISSIONS_PATH.exists():
        return copy.deepcopy((_read_yaml(MISSIONS_PATH).get("world_definition") or {}).get("missions") or {})
    return load_missions()


# The fields of an entry the studio's details form edits (the rest: the entry form, the YAML).
DETAIL_FIELDS = {
    "NPCs": ("known_locations", "known_objects", "article", "dialogue"),
    "Locations": ("distant_descriptions", "entered_descriptions", "interact_prompts", "exit_locked",
                  "interaction_points"),
}


# A place's points of interest (the terminal editor's interaction points): what the player can
# look at there, and the action, place, person or item each leads to.
INTERACTION_POINT_FIELDS = ("id", "label", "prompt", "linked_action", "linked_outcome", "linked_location",
                            "linked_npc", "linked_item", "notes")


def load_entity_details(kind: str, name: str) -> dict:
    record = _record(kind, name)[2]
    return {field: copy.deepcopy(record.get(field)) for field in DETAIL_FIELDS.get(kind, ())}


def save_entity_details(kind: str, name: str, details: dict) -> dict:
    """Change an entry's details (DETAIL_FIELDS; an empty value removes the field). The entry is
    checked with the whole world in place, as every save is. Returns the details."""
    fields = DETAIL_FIELDS.get(kind, ())
    unknown = sorted(set(details or {}) - set(fields))
    if unknown:
        raise AuthoringError(f"{kind} have no detail {', '.join(unknown)}; use {', '.join(fields) or 'none'}.")
    errors = []
    world = config.get("world_definition") or {}
    places = set(world.get("locations") or {})
    items = {entry.get("name") for entry in world.get("objects") or [] if isinstance(entry, dict)}
    for field, known in (("known_locations", places), ("known_objects", items)):
        for value in (details or {}).get(field) or []:
            if value not in known:
                errors.append(f"{field}: `{value}` is not a {'location' if field == 'known_locations' else 'item'}.")
    if (details or {}).get("article") not in (None, "", "the", "a", "an", "none"):
        errors.append("article must be the, a, an, none (a proper name) or empty (automatic).")
    for number, line in enumerate((details or {}).get("dialogue") or [], start=1):
        if not isinstance(line, dict) or not str(line.get("text", "")).strip():
            errors.append(f"dialogue line {number} needs a text.")
    people = {entry.get("name") for entry in world.get("characters") or [] if isinstance(entry, dict)}
    for number, point in enumerate((details or {}).get("interaction_points") or [], start=1):
        if not isinstance(point, dict) or not str(point.get("label") or point.get("prompt") or "").strip():
            errors.append(f"interaction point {number} needs a label or a prompt.")
            continue
        unknown = sorted(set(point) - set(INTERACTION_POINT_FIELDS))
        if unknown:
            errors.append(f"interaction point {number}: no field {', '.join(unknown)}; use {', '.join(INTERACTION_POINT_FIELDS)}.")
        for field, known, what in (("linked_location", places, "location"), ("linked_npc", people, "NPC"),
                                   ("linked_item", items, "item")):
            if point.get(field) and point[field] not in known:
                errors.append(f"interaction point {number}: {field} `{point[field]}` is not a {what}.")
    if errors:
        raise AuthoringError("The details are invalid:\n" + "\n".join(f"- {e}" for e in errors))
    record = copy.deepcopy(_record(kind, name)[2])
    for field, value in (details or {}).items():
        if field == "article" and value == "none":
            value = ""  # a proper name: no article
        if value in (None, [], {}) or (value == "" and field != "article") or (field == "exit_locked" and not value):
            record.pop(field, None)
        else:
            record[field] = value
    save_entity_yaml(kind, name, yaml.safe_dump(record, sort_keys=False, allow_unicode=True))
    return load_entity_details(kind, name)


def load_map() -> dict:
    """The world's map as data: {routes: {place: [the places it borders]} (null: every story draws
    its own map), places: [every location]}."""
    routes = (_read_yaml(MAP_PATH).get("world_definition") or {}).get("routes")
    places = sorted((config.get("world_definition") or {}).get("locations") or {})
    return {"routes": routes, "places": places}


def save_map(routes) -> tuple:
    """Save the whole map (null: none); checked like the map document. Returns (map, message)."""
    _, message = save_map_yaml(yaml.safe_dump({"routes": routes}, sort_keys=False, allow_unicode=True))
    return load_map(), message


def set_borders(place: str, borders) -> tuple:
    """The places ``place`` borders, both ways: a route added or removed here is added to or
    removed from the other place too. Returns (map, message)."""
    places = set(load_map()["places"])
    if place not in places:
        raise AuthoringError(f"`{place}` is not a location.")
    borders = [str(other) for other in borders or [] if str(other) != place]
    unknown = sorted(set(borders) - places)
    if unknown:
        raise AuthoringError(f"Not locations: {', '.join(unknown)}.")
    routes = load_map()["routes"] or {}
    adjacency = {name: set(neighbours or []) for name, neighbours in routes.items()}
    for name, neighbours in list(adjacency.items()):
        for other in neighbours:
            adjacency.setdefault(other, set()).add(name)  # a route holds both ways
    for other in adjacency.get(place, set()) - set(borders):
        adjacency[other].discard(place)
    for other in borders:
        adjacency.setdefault(other, set()).add(place)
    adjacency[place] = set(borders)
    order = list(routes) + sorted(set(adjacency) - set(routes))
    updated = {name: sorted(adjacency[name]) for name in order if adjacency.get(name)}
    return save_map(updated or None)


# ---- with the language model: suggestions for a field, and the world expanded (proposals) ----------

SUGGESTION_FIELDS = {"descriptions": "Descriptions", "goals": "Goals", "tags": "Tags",
                     "distant_descriptions": "Distant descriptions", "entered_descriptions": "Entered descriptions",
                     "interact_prompts": "Interact prompts", "dialogue": "Dialogue lines"}


def _need_language_model():
    """RuntimeError (with how to enable it) when the language model cannot be used from here."""
    from utils import llm_model_loader

    found = llm_model_loader.status()
    if not found["available"]:
        raise RuntimeError(f"The language model is not available: {found['reason']} {found['how'] or ''}".strip())


def _world_names():
    world = config.get("world_definition") or {}
    return {"locations": sorted(world.get("locations") or {}),
            "npcs": [c.get("name") for c in world.get("characters") or [] if isinstance(c, dict)],
            "items": [o.get("name") for o in world.get("objects") or [] if isinstance(o, dict)],
            "events": sorted(world.get("events") or {}),
            "missions": {key: (m or {}).get("title") for key, m in (world.get("missions") or {}).items()}}


def suggest(kind: str, name: str, field: str) -> list:
    """Gemma's suggestions for a text field of an entry (the terminal editor's suggestions):
    a few values in the world's voice. RuntimeError when the language model is not available."""
    if field not in SUGGESTION_FIELDS:
        raise AuthoringError(f"No suggestions for `{field}`; use {', '.join(SUGGESTION_FIELDS)}.")
    from utils.llm_suggester import LLMSuggester

    entry = copy.deepcopy(_record(kind, name)[2]) if name else {}
    context = {
        "selection": {"kind": kind, "field_name": field, "entry_name": name},
        "current_entry": {"name": name, "tags": entry.get("tags") or entry.get("associated_tags") or [],
                          "goals": entry.get("goals") or [], "descriptions": entry.get("descriptions") or [],
                          "existing_values": entry.get(field) or [],
                          "action_names": [a.get("name") for a in entry.get("actions") or [] if isinstance(a, dict)]},
        "whole_case": _world_names(),
    }
    _need_language_model()
    suggester = LLMSuggester()
    if not suggester.available():
        raise RuntimeError(suggester.last_error() or "The language model is not available.")
    found = suggester.suggest_list(SUGGESTION_FIELDS[field], context)
    if not found:
        raise RuntimeError(suggester.last_error() or "The language model suggested nothing.")
    return [str(value) for value in found]


def world_size() -> dict:
    """How big the world is against augmentation's targets: {have: {...}, want: {...}, enough}."""
    world = config.get("world_definition") or {}
    have = {"locations": len(world.get("locations") or {}), "items": len(world.get("objects") or []),
            "npcs": len(world.get("characters") or []),
            "outcomes": sum(len(action.get("outcomes") or []) for place in (world.get("locations") or {}).values()
                            for action in place.get("actions") or [] if isinstance(action, dict))}
    want = {"locations": int(config.get("augmentation.min_locations", 10)), "items": int(config.get("augmentation.min_items", 5)),
            "npcs": int(config.get("augmentation.min_npcs", 5)), "outcomes": int(config.get("augmentation.min_outcomes", 30))}
    return {"have": have, "want": want, "enough": all(have[key] >= want[key] for key in want)}


def propose_additions() -> dict:
    """Gemma's proposal of new places, people and items for this world; nothing is written (add
    the ones wanted with add_world_entries). RuntimeError without a language model."""
    from core_engine.augmenter import WorldAugmenter

    _need_language_model()
    proposal = WorldAugmenter().propose()
    if proposal is None:
        raise RuntimeError("The language model proposed nothing usable; try again.")
    return proposal


def add_world_entries(additions: dict) -> dict:
    """Add new places, people and items (e.g. a proposal): each must be new, and the whole world is
    checked with them in place; nothing is written when it is invalid. Returns what was added."""
    from .store import _validate_complete_config

    additions = additions or {}
    places, items, people = (dict(additions.get("locations") or {}), list(additions.get("objects") or []),
                             list(additions.get("characters") or []))
    if not (places or items or people):
        raise AuthoringError("Nothing to add: give new locations, objects (items) or characters (NPCs).")
    world = copy.deepcopy(config.get("world_definition") or {})
    errors = [f"`{name}` is already a location." for name in places if name in (world.get("locations") or {})]
    known = {e.get("name") for e in (world.get("objects") or []) + (world.get("characters") or []) if isinstance(e, dict)}
    errors += [f"`{entry.get('name') if isinstance(entry, dict) else entry}` already exists or has no name."
               for entry in items + people
               if not isinstance(entry, dict) or not entry.get("name") or entry["name"] in known]
    named = [entry.get("name") for entry in items + people if isinstance(entry, dict) and entry.get("name")]
    errors += [f"`{name}` is added twice." for name in sorted({n for n in named if named.count(n) > 1})]
    for kind, entries in (("Locations", [{**entry, "name": name} for name, entry in places.items()
                                         if isinstance(entry, dict)]),
                          ("Items", items), ("NPCs", people)):
        for entry in entries:
            if isinstance(entry, dict):
                errors.extend(f"{entry.get('name')}: {e}" for e in _entry_errors(kind, entry))
    world.setdefault("locations", {}).update(places)
    world["objects"] = list(world.get("objects") or []) + items
    world["characters"] = list(world.get("characters") or []) + people
    for key, value in (("locations", world["locations"]), ("objects", world["objects"]), ("characters", world["characters"])):
        errors.extend(_validate_complete_config(value, None, f"world_definition.{key}"))
    earlier = list(dict.fromkeys(logic_errors(config.get("world_definition") or {})))
    errors.extend(e for e in logic_errors(world) if e not in earlier)
    if errors:
        raise AuthoringError("The additions are invalid:\n" + "\n".join(f"- {e}" for e in dict.fromkeys(errors)))
    with CONFIG_LOCK:
        for kind, new in (("Locations", places), ("Items", items), ("NPCs", people)):
            if not new:
                continue
            path, data, records, shape = _entity_records(kind)
            if shape == "mapping":
                records.update(new)
            else:
                records.extend(new)
            _write_yaml(path, data)
        config.clear_override()
        config.reload()
    return {"locations": sorted(places), "objects": [e["name"] for e in items], "characters": [e["name"] for e in people]}


# ---- deletions and their undo ---------------------------------------------------------------------
#
# A deleted entry, action or mission is kept whole (every field, as stored) with its place: its index,
# its neighbours, and the list it left. Undo (restore_deletion) puts back that record only, never a
# whole file or configuration: where it was when the list is as the deletion left it, else next to its
# old neighbour. A name taken again, or an entry no longer there for its action, is a conflict:
# nothing newer is overwritten. Kept in this process, for its most recent deletions.

DELETIONS: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
MAX_DELETIONS = 200


def _names_in(records, shape) -> list:
    return list(records) if shape == "mapping" else [entry.get("name") for entry in records]


def _fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def _entity_known_errors(kind: str, record: dict) -> list:
    """The problems the world (and the entry) had while the entry was there: a restore is blocked only
    by problems it would bring that were not there then (an older configuration's are not)."""
    from .store import _validate_complete_config

    key, shape = ENTITY_FILES[kind][1], ENTITY_FILES[kind][2]
    world = config.get("world_definition") or {}
    entries = world.get(key) or ({} if shape == "mapping" else [])
    return list(dict.fromkeys([*_entry_errors(kind, record), *logic_errors(world),
                               *_validate_complete_config(entries, None, f"world_definition.{key}")]))


def _keep_deletion(deletion: dict, items: list, index: int, remaining: list) -> dict:
    """Keep a deletion to undo: the record whole, its index among ``items`` (the list before it was
    deleted), its neighbours there, and the list it left (``remaining``). Returns what the client
    keeps to undo it: {token, type, name, kind, entry, position, of}."""
    token = uuid.uuid4().hex
    kept = {**deletion, "record": copy.deepcopy(deletion["record"]), "index": index, "of": len(items),
            "before": copy.deepcopy(items[index - 1]) if index > 0 else None,
            "after": copy.deepcopy(items[index + 1]) if index + 1 < len(items) else None,
            "context": _fingerprint(remaining)}
    with CONFIG_LOCK:
        DELETIONS[token] = kept
        while len(DELETIONS) > MAX_DELETIONS:
            DELETIONS.popitem(last=False)
    return {"token": token, "type": kept["type"], "name": kept["name"], "kind": kept.get("kind"),
            "entry": kept.get("entry"), "position": index + 1, "of": len(items)}


def _place(current: list, deletion: dict) -> tuple[int, tuple | None]:
    """Where a restored record goes among ``current``: its old index when the list is as the deletion
    left it; when it changed since, after its old neighbour before it, else before the one after it,
    else its old index. Returns (index, the neighbour it was placed by: ("after" | "before", it), or None)."""
    index, anchor = deletion["index"], None
    if _fingerprint(current) != deletion["context"]:
        before, after = deletion.get("before"), deletion.get("after")
        if before is not None and before in current:
            index, anchor = current.index(before) + 1, ("after", before)
        elif after is not None and after in current:
            index, anchor = current.index(after), ("before", after)
    return min(index, len(current)), anchor


def _insert(entries, shape, name, record, index):
    if shape == "mapping":
        items = list(entries.items())
        items.insert(index, (name, record))
        entries.clear()
        entries.update(items)
    else:
        entries.insert(index, record)


def _not_restorable(what: str, errors) -> AuthoringError:
    return AuthoringError(f"{what} cannot go back as it was: the world changed since it was deleted, and it would "
                          "now be invalid:\n" + "\n".join(f"- {e}" for e in dict.fromkeys(errors)))


def _restore_entity(deletion: dict) -> int:
    from .store import _validate_complete_config

    kind, name, record = deletion["kind"], deletion["name"], copy.deepcopy(deletion["record"])
    path, data, records, shape = _entity_records(kind)
    names = _names_in(records, shape)
    if name in names:
        raise ConflictError(f"An entry named `{name}` is in {kind} again (added or renamed since it was deleted): it "
                            "is not overwritten. Rename or delete that one, then undo again.")
    index, anchor = _place(names, deletion)
    key = ENTITY_FILES[kind][1]
    current = copy.deepcopy(config.get("world_definition") or {})
    world = copy.deepcopy(current)
    entries = world.setdefault(key, {} if shape == "mapping" else [])
    _insert(entries, shape, name, copy.deepcopy(record), index)
    known = set(deletion["known_errors"]) | set(logic_errors(current)) | set(
        _validate_complete_config(current.get(key) or ({} if shape == "mapping" else []), None, f"world_definition.{key}"))
    errors = [*_entry_errors(kind, record), *_validate_complete_config(entries, None, f"world_definition.{key}"),
              *logic_errors(world)]
    new = [error for error in errors if error not in known]
    if new:
        raise _not_restorable(f"`{name}`", new)
    _insert(records, shape, name, record, index)
    _write_yaml(path, data)
    config.clear_override()
    config.reload()
    return index, anchor


def _restore_action(deletion: dict) -> int:
    kind, entry, name, record = deletion["kind"], deletion["entry"], deletion["name"], copy.deepcopy(deletion["record"])
    try:
        path, data, stored = _record(kind, entry)
    except AuthoringError as exc:
        raise ConflictError(f"`{entry}` is no longer in {kind} (deleted or renamed since): its action `{name}` cannot "
                            f"go back. Restore or recreate `{entry}` first.") from exc
    actions = copy.deepcopy(stored.get("actions") or [])
    if any(str(action.get("name", "")) == name for action in actions):
        raise ConflictError(f"`{entry}` has an action named `{name}` again (added or renamed since it was deleted): it "
                            "is not overwritten. Rename or delete that one, then undo again.")
    index, anchor = _place(actions, deletion)
    known = set(deletion["known_errors"]) | set(validate_entity_actions(kind, actions))
    actions.insert(index, record)
    new = [error for error in validate_entity_actions(kind, actions) if error not in known]
    if new:
        raise _not_restorable(f"The action `{name}` of `{entry}`", new)
    stored["actions"] = actions
    _write_yaml(path, data)
    config.clear_override()
    config.reload()
    return index, anchor


def _restore_mission(deletion: dict) -> int:
    key, record = deletion["name"], copy.deepcopy(deletion["record"])
    missions = _stored_missions()
    if key in missions:
        raise ConflictError(f"A mission `{key}` exists again (added or renamed since it was deleted): it is not "
                            "overwritten. Rename or delete that one, then undo again.")
    index, anchor = _place(list(missions), deletion)
    items = list(missions.items())
    items.insert(index, (key, record))
    restored = dict(items)
    known = set(deletion["known_errors"]) | set(validate_missions(missions))
    new = [error for error in validate_missions(restored) if error not in known]
    if new:
        raise _not_restorable(f"The mission `{key}`", new)
    _store_missions(yaml.safe_dump({"world_definition": {"missions": restored}}, sort_keys=False, allow_unicode=True))
    return index, anchor


def _ordinal(number: int) -> str:
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def restore_deletion(token: str) -> dict:
    """Undo a deletion (``token``: from its response): the record goes back whole, only it. LookupError:
    no such deletion here (undone already, or this process restarted since); ConflictError: its name is
    taken again, or its entry is gone (nothing is overwritten); AuthoringError: it would now be invalid.
    Returns {type, name, kind, entry, position, was, moved, changed_since, message}."""
    with CONFIG_LOCK:
        deletion = DELETIONS.get(token)
        if deletion is None:
            raise LookupError("This deletion can no longer be undone here: it was undone already, or the server "
                              "restarted since (undo is kept for its recent deletions).")
        restore = {"entity": _restore_entity, "action": _restore_action, "mission": _restore_mission}[deletion["type"]]
        context = {"entity": lambda: _names_in(*_entity_records(deletion["kind"])[2:]),
                   "action": lambda: load_entity_actions(deletion["kind"], deletion["entry"]),
                   "mission": lambda: list(_stored_missions())}[deletion["type"]]
        try:
            changed = _fingerprint(context()) != deletion["context"]
        except AuthoringError:
            changed = True
        index, anchor = restore(deletion)
        DELETIONS.pop(token, None)
    what = {"entity": f"**{deletion['name']}** to {deletion.get('kind')}",
            "action": f"the action **{deletion['name']}** to `{deletion.get('entry')}`",
            "mission": f"the mission **{deletion['name']}**"}[deletion["type"]]
    moved = index != deletion["index"]
    if not changed:
        where = f"where it was ({_ordinal(index + 1)})"
    else:
        side, neighbour = anchor or (None, None)
        label = neighbour.get("name") if isinstance(neighbour, dict) else neighbour
        where = (_ordinal(index + 1) + (f", {side} **{label}** as before" if side else "")
                 + (f" (it was {_ordinal(deletion['index'] + 1)})" if moved else "")
                 + "; the list changed since it was deleted")
    return {"type": deletion["type"], "name": deletion["name"], "kind": deletion.get("kind"),
            "entry": deletion.get("entry"), "position": index + 1, "was": deletion["index"] + 1, "moved": moved,
            "changed_since": changed, "message": f"Restored {what}, {where}."}
