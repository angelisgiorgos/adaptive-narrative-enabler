"""Builds the runtime world (locations, NPCs, items, events, missions) from the
configuration, and validates what the writer wrote."""
import copy

from utils.config_loader import config
from utils.tag_similarity import semantic_tag_similarity

from . import stats, variables
from .actions import REPEAT_RULES, Action, Outcome
from .narration import message
from .world import Character, Event, Location, Mission, Object, WorldGraph


_SPAWN_RULES_CACHE = {}

def _coerce_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]

def _location_distant_descriptions(data):
    return _coerce_list(
        data.get("distant_descriptions")
        or data.get("distant_text")
        or data.get("descriptions")
    )

def _location_entered_descriptions(data):
    return _coerce_list(
        data.get("entered_descriptions")
        or data.get("entering_text")
        or data.get("entered_text")
        or data.get("descriptions")
    )

def _interaction_prompts(data):
    prompts = list(_coerce_list(data.get("interact_prompts")))
    for point in _coerce_list(data.get("interaction_points")):
        prompt = point.get("prompt") or point.get("label")
        if prompt and prompt not in prompts:
            prompts.append(prompt)
    return prompts

def _resolve_outcome_links(o_data):
    move_to = o_data.get("move_to")
    reveal_location = o_data.get("reveal_location") or o_data.get("linked_location")
    reveal_npc = o_data.get("reveal_npc") or o_data.get("linked_npc")
    reveal_object = o_data.get("reveal_object") or o_data.get("linked_item")

    if move_to is None and o_data.get("linked_location_mode") == "move":
        move_to = o_data.get("linked_location")

    return move_to, reveal_location, reveal_npc, reveal_object

def _next_setup_tag(action_index, outcome_index):
    setup_cfg = config.get("fitness.setup_payoff", {})
    clue_variants = max(1, int(setup_cfg.get("clue_variants", 3)))
    clue_index = ((action_index + outcome_index) % clue_variants) + 1
    return f"setup_{clue_index}"

def _location_signature(locations_cfg):
    return tuple(
        sorted(
            (
                name,
                tuple(sorted(_coerce_list(data.get("tags", [])))),
            )
            for name, data in locations_cfg.items()
        )
    )

def _build_spawn_rules(locations, locations_cfg, config_wd):
    strategy = config_wd.get("spawn_strategy", "static")
    if strategy == "similarity":
        threshold = config_wd.get("similarity_threshold", 0.2)
        cache_key = ("similarity", threshold, _location_signature(locations_cfg))
        cached = _SPAWN_RULES_CACHE.get(cache_key)
        if cached is not None:
            return copy.deepcopy(cached)

        spawn_rules = {}
        loc_names = list(locations.keys())
        for i in range(len(loc_names)):
            for j in range(len(loc_names)):
                if i == j:
                    continue
                name1, name2 = loc_names[i], loc_names[j]
                l1, l2 = locations[name1], locations[name2]
                sim = semantic_tag_similarity(l1.tags, l2.tags)
                if sim >= threshold:
                    spawn_rules.setdefault(name1, []).append(name2)

        _SPAWN_RULES_CACHE[cache_key] = copy.deepcopy(spawn_rules)
        return spawn_rules

    spawn_rules_cfg = config_wd.get("spawn_rules", {})
    return {parent: list(children) for parent, children in spawn_rules_cfg.items()}

def _checked_conditions(data, specs, where):
    """``enabled_if`` of a config entry, validated against the variable definitions."""
    conditions = data.get("enabled_if")
    errors = variables.validate_conditions(conditions, specs, where=f"{where}.enabled_if")
    if errors:
        raise ValueError("; ".join(errors))
    return conditions

def _check_variable_changes(entry, specs, where):
    for a_data in entry.get("actions", []):
        for o_data in a_data.get("outcomes", []):
            errors = variables.validate_changes(
                o_data.get("set_variables"), o_data.get("change_variables"), specs,
                where=f"{where} / {a_data.get('name', 'Action')}",
            )
            if errors:
                raise ValueError("; ".join(errors))

def _stat_usage_errors(entry, stat_names, where):
    """Problems with skill checks, check branches, stat changes and item bonuses."""
    errors = []

    def numbers(mapping, field, label):
        if mapping is None:
            return
        if not isinstance(mapping, dict):
            errors.append(f"{label}: `{field}` must map stat names to numbers.")
            return
        for stat, value in mapping.items():
            if stat not in stat_names:
                errors.append(f"{label}: `{field}` uses undefined stat `{stat}`.")
            elif not isinstance(value, int) or isinstance(value, bool):
                errors.append(f"{label}: `{field}.{stat}` must be a whole number.")

    numbers(entry.get("stat_bonus"), "stat_bonus", where)
    for a_data in entry.get("actions", []) or []:
        label = f"{where} / {a_data.get('name', 'Action')}"
        check = a_data.get("skill_check")
        if check is not None:
            if not isinstance(check, dict) or check.get("stat") not in stat_names:
                errors.append(f"{label}: `skill_check.stat` must be one of: {', '.join(stat_names) or 'no stats defined'}.")
            elif not isinstance(check.get("modifier", 0), int) or set(check) - {"stat", "modifier"}:
                errors.append(f"{label}: `skill_check` takes `stat` and a whole-number `modifier`.")
        for o_data in a_data.get("outcomes", []) or []:
            if o_data.get("on_check") not in (None, "pass", "fail"):
                errors.append(f"{label}: `on_check` must be pass or fail.")
            numbers(o_data.get("change_stats"), "change_stats", label)
    return errors

def _build_outcome(o_data, genome, tags=None):
    move_to, reveal_location, reveal_npc, reveal_object = _resolve_outcome_links(o_data)
    success_prob = 1.0
    if o_data.get("use_genome_bias"):
        success_prob = genome.success_bias
    elif "success_prob" in o_data:
        success_prob = o_data["success_prob"]

    return Outcome(
        desc=o_data.get("desc", ""),
        tags=o_data.get("tags", []) if tags is None else tags,
        success_prob=success_prob,
        health_change=o_data.get("health_change", 0),
        coin_change=o_data.get("coin_change", 0),
        spawn=o_data.get("spawn", False),
        move_to=move_to,
        reveal_location=reveal_location,
        reveal_npc=reveal_npc,
        reveal_object=reveal_object,
        lead_to_known=o_data.get("lead_to_known", False),
        remove_npc=o_data.get("remove_npc"),
        send_to=o_data.get("send_to"),
        set_variables=o_data.get("set_variables"),
        change_variables=o_data.get("change_variables"),
        on_check=o_data.get("on_check"),
        change_stats=o_data.get("change_stats"),
    )

def _build_action(a_data, outcomes, events_cfg, name=None, character_name=None, source=None, owner=None):
    repeat = a_data.get("repeat")
    if repeat is not None and repeat not in REPEAT_RULES:
        raise ValueError(
            f"Action '{a_data.get('name', 'Action')}' of {owner}: repeat must be one of "
            f"{', '.join(REPEAT_RULES)}, not {repeat!r}."
        )
    triggers_event = a_data.get("triggers_event")
    if triggers_event and not outcomes:
        # A trigger action needs no outcomes of its own; the event carries the story.
        title = events_cfg.get(triggers_event, {}).get("title", triggers_event)
        outcomes = [Outcome(desc=message("event_trigger_result", title=title), tags=["event"])]

    return Action(
        name=name or a_data.get("name", "Action"),
        outcomes=outcomes,
        character_name=character_name,
        required_object=a_data.get("required_object"),
        required_tag=a_data.get("required_tag"),
        required_coins=a_data.get("required_coins", 0),
        collects_object=a_data.get("collects_object"),
        consumes_object=a_data.get("consumes_object", False),
        triggers_event=triggers_event,
        repeat=repeat,
        exclusive_group=a_data.get("exclusive_group"),
        source=source,
        owner=owner,
        skill_check=a_data.get("skill_check"),
    )

def build_world(genome):
    world = WorldGraph()
    config_wd = config.get("world_definition", {})
    variable_specs = variables.definitions()
    spec_errors = variables.validate_definitions(variable_specs)
    if spec_errors:
        raise ValueError("; ".join(spec_errors))
    for name, data in (config_wd.get("locations") or {}).items():
        _check_variable_changes(data, variable_specs, f"Location '{name}'")
    for entry in (config_wd.get("characters") or []) + (config_wd.get("objects") or []):
        _check_variable_changes(entry, variable_specs, f"'{entry.get('name')}'")
    for name, data in (config_wd.get("events") or {}).items():
        _check_variable_changes(data, variable_specs, f"Event '{name}'")
    stat_settings = stats.stats_config()
    stat_errors = stats.validate_settings(stat_settings)
    stat_names = list(stat_settings.get("stats") or {})
    for name, data in (config_wd.get("locations") or {}).items():
        stat_errors += _stat_usage_errors(data, stat_names, f"Location '{name}'")
    for entry in (config_wd.get("characters") or []) + (config_wd.get("objects") or []):
        stat_errors += _stat_usage_errors(entry, stat_names, f"'{entry.get('name')}'")
    for name, data in (config_wd.get("events") or {}).items():
        stat_errors += _stat_usage_errors(data, stat_names, f"Event '{name}'")
    if stat_errors:
        raise ValueError("; ".join(stat_errors))
    events_cfg = config_wd.get("events") or {}
    
    # 1. Locations
    locations_cfg = config_wd.get("locations", {})
    for name, data in locations_cfg.items():
        loc = Location(
            name, 
            data.get("tags", []), 
            interact_prompts=_interaction_prompts(data),
            goals=data.get("goals", []),
            distant_descriptions=_location_distant_descriptions(data),
            entered_descriptions=_location_entered_descriptions(data),
            enabled_if=_checked_conditions(data, variable_specs, f"Location '{name}'"),
        )
        
        # 2. Actions/Outcomes
        actions_list = []
        for action_index, a_data in enumerate(data.get("actions", [])):
            outcomes_list = []
            for outcome_index, o_data in enumerate(a_data.get("outcomes", [])):
                # Clue Handling
                tags = copy.copy(o_data.get("tags", []))
                if "setup_clue" in tags:
                    tags.remove("setup_clue")
                    tags.append(_next_setup_tag(action_index, outcome_index))
                outcomes_list.append(_build_outcome(o_data, genome, tags=tags))
            actions_list.append(_build_action(a_data, outcomes_list, events_cfg, source="location", owner=name))
        loc.actions = actions_list
        world.add_location(loc)

    # 3. Spawn rules (Dynamic Similarity or Static)
    for parent, children in _build_spawn_rules(world.locations, locations_cfg, config_wd).items():
        for child in children:
            world.allow_spawn(parent, child)

    # 4. Characters
    characters_cfg = config_wd.get("characters", [])
    for char_data in characters_cfg:
        char_actions_list = []
        for a_data in char_data.get("actions", []):
            char_outcomes_list = [
                _build_outcome(o_data, genome) for o_data in a_data.get("outcomes", [])
            ]
            char_actions_list.append(_build_action(
                a_data,
                char_outcomes_list,
                events_cfg,
                name=f"({char_data['name']}) {a_data.get('name', 'Action')}",
                character_name=char_data['name'],
                source="npc",
                owner=char_data['name'],
            ))
        
        char = Character(
            char_data["name"], 
            char_data.get("associated_tags", []), 
            goals=char_data.get("goals", []),
            descriptions=char_data.get("descriptions", []),
            actions=char_actions_list,
            known_locations=char_data.get("known_locations", []),
            known_objects=char_data.get("known_objects", []),
            unique=char_data.get("unique", False),
            encounter_event=char_data.get("encounter_event"),
            encounter_repeat=char_data.get("encounter_repeat", False),
            enabled_if=_checked_conditions(char_data, variable_specs, f"NPC '{char_data['name']}'"),
        )
        world.add_character(char)

    # 5. Objects
    objects_cfg = config_wd.get("objects", [])
    for obj_data in objects_cfg:
        obj_actions_list = []
        for a_data in obj_data.get("actions", []):
            obj_outcomes_list = [
                _build_outcome(o_data, genome) for o_data in a_data.get("outcomes", [])
            ]
            obj_actions_list.append(_build_action(
                a_data,
                obj_outcomes_list,
                events_cfg,
                name=f"[{obj_data['name']}] {a_data.get('name', 'Action')}",
                source="item",
                owner=obj_data['name'],
            ))
        
        obj = Object(
            obj_data["name"], 
            obj_data.get("associated_tags", []), 
            interact_prompts=obj_data.get("interact_prompts", []),
            goals=obj_data.get("goals", []),
            descriptions=obj_data.get("descriptions", []),
            actions=obj_actions_list,
            collectible=obj_data.get("collectible", False),
            enabled_if=_checked_conditions(obj_data, variable_specs, f"Item '{obj_data['name']}'"),
            stat_bonus=obj_data.get("stat_bonus"),
        )
        world.add_object(obj)

    # 6. Events (entered through actions with `triggers_event`)
    for event_name, event_data in events_cfg.items():
        event_actions = [
            _build_action(
                a_data,
                [_build_outcome(o_data, genome) for o_data in a_data.get("outcomes", [])],
                events_cfg,
                source="event",
                owner=event_name,
            )
            for a_data in event_data.get("actions", [])
        ]
        world.add_event(Event(
            event_name,
            title=event_data.get("title"),
            tags=event_data.get("tags", []),
            descriptions=_location_entered_descriptions(event_data),
            actions=event_actions,
        ))

    # 7. Missions (one is chosen at random as the story's win condition)
    for mission_name, mission_data in (config_wd.get("missions") or {}).items():
        goal_conditions = (mission_data.get("goal") or {}).get("variables")
        goal_errors = variables.validate_conditions(goal_conditions, variable_specs, where=f"Mission '{mission_name}'.goal.variables")
        if goal_errors:
            raise ValueError("; ".join(goal_errors))
        world.add_mission(Mission(
            mission_name,
            mission_data.get("goal"),
            title=mission_data.get("title"),
            description=mission_data.get("description", ""),
            weight=mission_data.get("weight", 1.0),
            enabled_if=_checked_conditions(mission_data, variable_specs, f"Mission '{mission_name}'"),
        ))

    return world
