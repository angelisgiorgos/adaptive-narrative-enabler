"""Builds the runtime world (locations, NPCs, items, events, missions) from the
configuration, and validates what the writer wrote."""
import copy

from utils.config_loader import config
from utils.tag_similarity import semantic_tag_similarity

from . import conditions, effects, rules, starts, stats, variables
from .actions import Action, Outcome, repetition_errors
from .narration import message
from .world import Character, DialogueLine, Event, Location, Mission, Object, WorldGraph


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

DIALOGUE_FIELDS = {"text", "enabled_if", "effects"}
EVENT_POLICY_FIELDS = ("trigger", "chance", "weight", "cooldown", "max_occurrences", "repeat")
ITEM_POLICY_FIELDS = ("unique", "location")


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_whole(value):
    return isinstance(value, int) and not isinstance(value, bool)


def event_policy_errors(data, where):
    """Problems with when an event may start: trigger, chance, weight, cooldown,
    max_occurrences and repeat."""
    errors = []
    trigger = data.get("trigger", "action")
    if trigger not in Event.TRIGGERS:
        errors.append(f"{where}: `trigger` must be one of {', '.join(Event.TRIGGERS)}.")
    if trigger == "random":
        chance = data.get("chance")
        if not _is_number(chance) or not 0 < chance <= 1:
            errors.append(f"{where}: a random event needs a `chance` above 0 and at most 1 (per turn).")
        if "weight" in data and (not _is_number(data["weight"]) or data["weight"] <= 0):
            errors.append(f"{where}: `weight` must be a number above 0.")
    elif {"chance", "weight"} & set(data):
        errors.append(f"{where}: `chance` and `weight` only apply with `trigger: random`.")
    if "cooldown" in data and (not _is_whole(data["cooldown"]) or data["cooldown"] < 0):
        errors.append(f"{where}: `cooldown` must be a whole number of turns, 0 or more.")
    if data.get("max_occurrences") is not None and (not _is_whole(data["max_occurrences"]) or data["max_occurrences"] < 1):
        errors.append(f"{where}: `max_occurrences` must be a whole number of 1 or more.")
    if data.get("repeat", "always") not in Event.REPEAT:
        errors.append(f"{where}: `repeat` must be one of {', '.join(Event.REPEAT)}.")
    return errors


def item_policy_errors(data, where, locations=None):
    """Problems with an item's uniqueness: ``unique`` and the ``location`` of its instance."""
    errors = []
    if not isinstance(data.get("unique", False), bool):
        errors.append(f"{where}: `unique` must be true or false.")
    location = data.get("location")
    if location is not None:
        if data.get("unique") is not True:
            errors.append(f"{where}: only a unique item has a `location` (generic items appear wherever they fit).")
        elif locations is not None and location not in locations:
            errors.append(f"{where}: unknown location `{location}`.")
    return errors


def logic_errors(world):
    """Problems with every condition (``enabled_if``, mission goals) and effect list
    (outcome ``effects`` and variable changes, dialogue lines, mission ``rewards``)
    of a ``world_definition`` mapping, checked against its own variables, stats,
    items, locations, NPCs, events and missions. Empty when valid."""
    world = world or {}
    refs = conditions.References.from_world(world)
    errors = []

    def condition(raw, where):
        errors.extend(conditions.validate(raw, refs, where=where))

    def effect_list(raw, where):
        errors.extend(effects.validate(raw, refs, where=where))

    def actions(entry, where):
        for a_data in entry.get("actions") or []:
            if not isinstance(a_data, dict):
                continue
            label = f"{where} / {a_data.get('name', 'Action')}"
            errors.extend(repetition_errors(a_data.get("repeat"), a_data.get("retry_on_failure"), label))
            condition(a_data.get("enabled_if"), f"{label}.enabled_if")
            for number, o_data in enumerate(a_data.get("outcomes") or [], start=1):
                if not isinstance(o_data, dict):
                    continue
                errors.extend(variables.validate_changes(
                    o_data.get("set_variables"), o_data.get("change_variables"), refs.variables, where=label,
                ))
                effect_list(o_data.get("effects"), f"{label} / outcome {number}.effects")

    for name, data in (world.get("locations") or {}).items():
        condition((data or {}).get("enabled_if"), f"Location '{name}'.enabled_if")
        actions(data or {}, f"Location '{name}'")
    for entry in world.get("characters") or []:
        where = f"NPC '{entry.get('name')}'"
        condition(entry.get("enabled_if"), f"{where}.enabled_if")
        actions(entry, where)
        errors.extend(_dialogue_errors(entry.get("dialogue"), where, condition, effect_list))
    for entry in world.get("objects") or []:
        condition(entry.get("enabled_if"), f"Item '{entry.get('name')}'.enabled_if")
        actions(entry, f"Item '{entry.get('name')}'")
        errors.extend(item_policy_errors(entry, f"Item '{entry.get('name')}'", refs.locations))
    for name, data in (world.get("events") or {}).items():
        condition((data or {}).get("enabled_if"), f"Event '{name}'.enabled_if")
        actions(data or {}, f"Event '{name}'")
        errors.extend(event_policy_errors(data or {}, f"Event '{name}'"))
    for name, data in (world.get("missions") or {}).items():
        data = data or {}
        condition(data.get("enabled_if"), f"Mission '{name}'.enabled_if")
        for key, value in (data.get("goal") or {}).items():
            if key in Mission.GOAL_KEYS:
                condition(conditions.goal_condition(key, value), f"Mission '{name}'.goal.{key}")
        effect_list(data.get("rewards"), f"Mission '{name}'.rewards")
    return errors


def _dialogue_errors(lines, where, condition, effect_list):
    if lines is None:
        return []
    if not isinstance(lines, list):
        return [f"{where}: `dialogue` must be a list of lines."]
    errors = []
    for number, line in enumerate(lines, start=1):
        label = f"{where} dialogue line {number}"
        if not isinstance(line, dict):
            errors.append(f"{label} must be a mapping with `text`.")
            continue
        unknown = sorted(set(line) - DIALOGUE_FIELDS)
        if unknown:
            errors.append(f"{label}: unknown field(s) {', '.join(unknown)} (use {', '.join(sorted(DIALOGUE_FIELDS))}).")
        text = line.get("text")
        texts = text if isinstance(text, list) else [text]
        if not texts or not all(isinstance(item, str) and item.strip() for item in texts):
            errors.append(f"{label} needs `text` (a sentence, or a list of variants).")
        condition(line.get("enabled_if"), f"{label}.enabled_if")
        effect_list(line.get("effects"), f"{label}.effects")
    return errors


def _checked(effect_list):
    """Mark an effect list validated (logic_errors ran before the world was built)."""
    effect_list.checked = True
    return effect_list


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

    outcome = Outcome(
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
        effects=o_data.get("effects"),
    )
    _checked(outcome.effects)
    return outcome

def _build_action(a_data, outcomes, events_cfg, name=None, character_name=None, source=None, owner=None):
    repeat = a_data.get("repeat")
    errors = repetition_errors(repeat, a_data.get("retry_on_failure"), f"Action '{a_data.get('name', 'Action')}' of {owner}")
    if errors:
        raise ValueError("; ".join(errors))
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
        enabled_if=a_data.get("enabled_if"),
        retry_on_failure=a_data.get("retry_on_failure", False),
    )

def build_world(genome):
    world = WorldGraph()
    config_wd = config.get("world_definition", {})
    variable_specs = variables.definitions()
    spec_errors = variables.validate_definitions(variable_specs)
    if spec_errors:
        raise ValueError("; ".join(spec_errors))
    errors = logic_errors(config_wd)  # every condition and effect list, before anything is built
    if errors:
        raise ValueError("; ".join(errors))
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
    start_errors = starts.validate_settings(starts.settings()) + rules.validate_settings(rules.settings())
    for name, data in (config_wd.get("locations") or {}).items():
        start_errors += starts.location_errors(data, f"Location '{name}'")
    if start_errors:
        raise ValueError("; ".join(start_errors))
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
            enabled_if=data.get("enabled_if"),
            start_eligible=data.get("start_eligible", False),
            start_weight=data.get("start_weight", 1.0),
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
            enabled_if=char_data.get("enabled_if"),
            dialogue=[
                DialogueLine(line.get("text"), line.get("enabled_if"), _checked(effects.EffectList(line.get("effects"))))
                for line in char_data.get("dialogue") or []
            ],
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
            enabled_if=obj_data.get("enabled_if"),
            stat_bonus=obj_data.get("stat_bonus"),
            unique=obj_data.get("unique", False),
            location=obj_data.get("location"),
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
            enabled_if=event_data.get("enabled_if"),
            **{field: event_data[field] for field in EVENT_POLICY_FIELDS if field in event_data},
        ))

    # 7. Missions (one is chosen at random as the story's win condition)
    for mission_name, mission_data in (config_wd.get("missions") or {}).items():
        world.add_mission(Mission(
            mission_name,
            mission_data.get("goal"),
            title=mission_data.get("title"),
            description=mission_data.get("description", ""),
            weight=mission_data.get("weight", 1.0),
            enabled_if=mission_data.get("enabled_if"),
            rewards=_checked(effects.EffectList(mission_data.get("rewards"))),
        ))

    return world
