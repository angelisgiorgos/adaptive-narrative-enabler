import random
import copy
import os
import datetime
import sys
import threading
import yaml
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from utils.config_loader import config
from utils.config_editor import ConfigEditor
from utils.tag_similarity import semantic_tag_similarity
from core_engine import Genome, Location, WorldGraph, Action, Outcome, GameState, Character, Object

# ============================
# CONFIG (loaded from config.yaml)
# ============================

POP_SIZE = config.get("evolution.pop_size", 20)
GENERATIONS = config.get("evolution.generations", 20)
EPISODES_PER_GENOME = config.get("evolution.episodes_per_genome", 3)
MAX_STEPS = config.get("evolution.max_steps", 60)
EVALUATION_THREADS = config.get("evolution.evaluation_threads", 8)
PARALLEL_BACKEND = config.get("evolution.parallel_backend", "processes")
_SPAWN_RULES_CACHE = {}


class GenerationStopper:
    def __init__(self):
        self.stop_event = threading.Event()
        self._thread = None
        self._active = threading.Event()
        self._old_termios = None

    def __enter__(self):
        if not sys.stdin.isatty():
            return self

        self._active.set()
        if os.name == "nt":
            self._thread = threading.Thread(target=self._watch_windows, daemon=True)
        else:
            self._enable_unix_raw_input()
            self._thread = threading.Thread(target=self._watch_unix, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._active.clear()
        if self._old_termios is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_termios)

    def _enable_unix_raw_input(self):
        import termios
        import tty
        fd = sys.stdin.fileno()
        self._old_termios = termios.tcgetattr(fd)
        tty.setcbreak(fd)

    def _watch_unix(self):
        import select
        while self._active.is_set() and not self.stop_event.is_set():
            readable, _, _ = select.select([sys.stdin], [], [], 0.1)
            if readable and sys.stdin.read(1).lower() == "q":
                self.stop_event.set()
                print("\n[GENERATION] Stop requested. Finishing current step and moving to story execution...")
                return

    def _watch_windows(self):
        import msvcrt
        while self._active.is_set() and not self.stop_event.is_set():
            if msvcrt.kbhit() and msvcrt.getwch().lower() == "q":
                self.stop_event.set()
                print("\n[GENERATION] Stop requested. Finishing current step and moving to story execution...")
                return
            self.stop_event.wait(0.1)


def refresh_runtime_settings():
    global POP_SIZE, GENERATIONS, EPISODES_PER_GENOME, MAX_STEPS, EVALUATION_THREADS, PARALLEL_BACKEND
    POP_SIZE = config.get("evolution.pop_size", 20)
    GENERATIONS = config.get("evolution.generations", 20)
    EPISODES_PER_GENOME = config.get("evolution.episodes_per_genome", 3)
    MAX_STEPS = config.get("evolution.max_steps", 60)
    EVALUATION_THREADS = config.get("evolution.evaluation_threads", 8)
    PARALLEL_BACKEND = config.get("evolution.parallel_backend", "processes")


def apply_random_seed():
    seed = config.get("reproducibility.seed", config.get("simulation.seed"))
    if seed is None:
        return None

    seed = int(seed)
    random.seed(seed)

    try:
        import numpy as np
        np.random.seed(seed)
    except ImportError:
        pass

    try:
        import torch
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
    except ImportError:
        pass

    print(f"[REPRODUCIBILITY] Random seed set to: {seed}")
    return seed


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


def _flatten_seen_tags(tags_seen):
    flattened = []
    for entry in tags_seen:
        if isinstance(entry, list):
            flattened.extend(entry)
        else:
            flattened.append(entry)
    return flattened


def _matches_tag_pattern(tag, pattern):
    if pattern is None:
        return False
    if pattern.endswith("*"):
        return tag.startswith(pattern[:-1])
    if pattern.endswith("_"):
        return tag.startswith(pattern)
    return tag == pattern


def _count_matching_tags(tags, expected_tags):
    if not expected_tags:
        return 0
    return sum(
        1
        for tag in tags
        for pattern in expected_tags
        if _matches_tag_pattern(tag, pattern)
    )


def _count_pattern_occurrences(tags, patterns):
    if not patterns:
        return 0
    count = 0
    for tag in tags:
        if any(_matches_tag_pattern(tag, pattern) for pattern in patterns):
            count += 1
    return count


def _story_progress(index, total_steps):
    if total_steps <= 1:
        return 1.0
    return index / max(1, total_steps - 1)


def _normalize_step_tags(step_tags):
    if isinstance(step_tags, list):
        return step_tags
    return [step_tags]


def _band_score(value, cfg, default_bonus=10):
    min_v = cfg.get("min")
    max_v = cfg.get("max")
    target = cfg.get("target", value)
    bonus = cfg.get("bonus", default_bonus)

    if min_v is not None and max_v is not None and min_v <= value <= max_v:
        return bonus
    return max(0, bonus - abs(value - target) * cfg.get("penalty_per_unit", 1))


def _fitness_breakdown(state):
    fitness_cfg = config.get("fitness", {})
    base_cfg = fitness_cfg.get("base", {})
    total_steps = state.actions_taken
    if total_steps == 0:
        return {"zero_step_penalty": base_cfg.get("zero_step_penalty", -100)}

    breakdown = {}

    arc_cfg = fitness_cfg.get("dramatic_arc", {})
    intro_tags = arc_cfg.get("intro_tags", ["exploration", "urban", "nature", "spawn", "maritime"])
    middle_tags = arc_cfg.get("middle_tags", ["threat", "guards", "social", "sleazy", "prison"])
    climax_tags = arc_cfg.get("climax_tags", ["payoff_", "palace", "vault", "grand", "success"])
    intro_end = arc_cfg.get("intro_end", 0.25)
    middle_end = arc_cfg.get("middle_end", 0.75)
    intro_bonus = arc_cfg.get("intro_bonus", 2)
    middle_bonus = arc_cfg.get("middle_bonus", 3)
    climax_bonus = arc_cfg.get("climax_bonus", 5)

    arc_score = 0.0
    for i, step_tags in enumerate(state.tags_seen):
        progress = _story_progress(i, len(state.tags_seen))
        current_tags = _normalize_step_tags(step_tags)
        if progress < intro_end:
            arc_score += _count_matching_tags(current_tags, intro_tags) * intro_bonus
        elif progress < middle_end:
            arc_score += _count_matching_tags(current_tags, middle_tags) * middle_bonus
        else:
            arc_score += _count_matching_tags(current_tags, climax_tags) * climax_bonus
    breakdown["dramatic_arc"] = arc_score

    contrast_weight = fitness_cfg.get("contrast_weight", 10)
    contrast_score = 0.0
    for i in range(len(state.path) - 1):
        loc1 = state.world.locations.get(state.path[i])
        loc2 = state.world.locations.get(state.path[i + 1])
        if loc1 and loc2:
            similarity = semantic_tag_similarity(loc1.tags, loc2.tags)
            contrast_score += (1.0 - similarity) * contrast_weight
    breakdown["scene_contrast"] = contrast_score

    setup_cfg = fitness_cfg.get("setup_payoff", {})
    pair_bonus = 0.0
    for s_tag, s_idx in state.setup_indices.items():
        p_tag = s_tag.replace("setup_", "payoff_")
        if p_tag in state.payoff_indices:
            p_idx = state.payoff_indices[p_tag]
            gap = p_idx - s_idx
            min_gap = total_steps * setup_cfg.get("min_gap_fraction", 1 / 3)
            if gap > min_gap:
                pair_bonus += setup_cfg.get("pair_bonus", 20)
                pair_bonus += gap * setup_cfg.get("gap_step_bonus", 2)
            else:
                pair_bonus += setup_cfg.get("short_gap_bonus", 5)

    matched_pairs = min(len(state.setup_indices), len(state.payoff_indices))
    pair_bonus += matched_pairs * setup_cfg.get("target_count_bonus", 0)
    unmatched_setups = max(0, len(state.setup_indices) - matched_pairs)
    unmatched_payoffs = max(0, len(state.payoff_indices) - matched_pairs)
    pair_bonus -= unmatched_setups * setup_cfg.get("unmatched_setup_penalty", 0)
    pair_bonus -= unmatched_payoffs * setup_cfg.get("unmatched_payoff_penalty", 0)
    breakdown["setup_payoff"] = pair_bonus

    all_tags = _flatten_seen_tags(state.tags_seen)
    volatility_cfg = fitness_cfg.get("volatility", {})
    threat_patterns = volatility_cfg.get("threat_patterns", ["threat", "combat"])
    recovery_patterns = volatility_cfg.get("recovery_patterns", ["neutral", "success"])
    threats = _count_pattern_occurrences(all_tags, threat_patterns)
    recoveries = _count_pattern_occurrences(all_tags, recovery_patterns)
    breakdown["volatility"] = min(threats, recoveries) * volatility_cfg.get("pair_weight", fitness_cfg.get("volatility_pair_weight", 10))
    breakdown["threat_balance"] = _band_score(threats, fitness_cfg.get("threat_balance", {}), default_bonus=20)
    breakdown["health_balance"] = _band_score(state.health, fitness_cfg.get("health_balance", {}), default_bonus=20)

    tone_cfg = fitness_cfg.get("tone_variety", {})
    ignore_patterns = tone_cfg.get("ignore_patterns", ["spawn", "failure", "success", "neutral", "setup_", "payoff_"])
    narrative_tags = []
    for step_tags in state.tags_seen:
        for tag in _normalize_step_tags(step_tags):
            if not any(_matches_tag_pattern(tag, pattern) for pattern in ignore_patterns):
                narrative_tags.append(tag)

    tone_score = 0.0
    if narrative_tags:
        unique_narrative = len(set(narrative_tags))
        variety_ratio = unique_narrative / len(narrative_tags)
        tone_score += variety_ratio * tone_cfg.get("density_weight", fitness_cfg.get("tone_variety_weight", 30))
        tone_score += unique_narrative * tone_cfg.get("unique_tag_bonus", fitness_cfg.get("tone_variety_bonus", 15))
    breakdown["tone_variety"] = tone_score

    breakdown["story_length"] = _band_score(total_steps, fitness_cfg.get("story_length", {}), default_bonus=25)

    path_cfg = fitness_cfg.get("path_variety", {})
    consecutive_repeats = sum(
        1 for i in range(1, len(state.path)) if state.path[i] == state.path[i - 1]
    )
    repeat_power = path_cfg.get("repeat_penalty_power", fitness_cfg.get("path_repeat_penalty_power", 2))
    repeat_weight = path_cfg.get("repeat_penalty_weight", fitness_cfg.get("path_repeat_penalty_weight", 10))
    path_score = -(consecutive_repeats ** repeat_power) * repeat_weight

    max_visits = fitness_cfg.get("max_location_visits", 3)
    visit_penalty = fitness_cfg.get("visit_penalty", 5)
    for visits in state.location_counts.values():
        if visits > max_visits:
            path_score -= (visits - max_visits) * visit_penalty

    unique_locations = len(set(state.path))
    revisits = max(0, len(state.path) - unique_locations)
    path_score += unique_locations * path_cfg.get("unique_location_bonus", 0)
    path_score -= revisits * path_cfg.get("revisit_penalty", 0)
    breakdown["path_variety"] = path_score

    char_bonus = fitness_cfg.get("character_interaction_bonus", 5)
    char_spawn_bonus = fitness_cfg.get("character_spawn_bonus", 15)
    npc_similarity_weight = fitness_cfg.get("npc_similarity_weight", 20)
    npc_repeat_weight = fitness_cfg.get("npc_repeat_weight", 10)
    npc_context_score = 0.0
    for char_name, count in state.character_interactions.items():
        char_obj = next((c for c in state.world.characters if c.name == char_name), None)
        if not char_obj:
            continue
        npc_context_score += count * char_bonus
        path_similarity = semantic_tag_similarity(char_obj.associated_tags, state.tags_seen)
        npc_context_score += path_similarity * npc_similarity_weight
        npc_context_score += count * npc_repeat_weight
        if count >= 2:
            npc_context_score += char_spawn_bonus
    breakdown["character_interactions"] = npc_context_score

    combat_bonus = 0.0
    for step_tags in state.tags_seen:
        current_tags = set(_normalize_step_tags(step_tags))
        if {"combat", "success"} <= current_tags or {"threat", "success"} <= current_tags:
            combat_bonus += fitness_cfg.get("combat_success_bonus", 25)
    breakdown["combat_success"] = combat_bonus

    discovery_cfg = fitness_cfg.get("discovery", {})
    breakdown["discovery"] = (
        len(set(state.path)) * discovery_cfg.get("location_bonus", 0)
        + len(state.inventory) * discovery_cfg.get("inventory_bonus", 0)
        + len(state.character_interactions) * discovery_cfg.get("character_bonus", 0)
    )

    end_score = 0.0
    if state.ended:
        if state.health <= 0:
            end_score -= base_cfg.get("death_penalty", 0)
        else:
            end_score += base_cfg.get("completion_bonus", 0)
        if state.end_reason:
            reason_text = state.end_reason.lower()
            if "stalled" in reason_text:
                end_score -= base_cfg.get("stall_penalty", 0)
            if "dungeon" in reason_text:
                end_score -= base_cfg.get("dungeon_penalty", 0)
            if "action limit" in reason_text:
                end_score += base_cfg.get("max_action_completion_bonus", 0)
    breakdown["ending"] = end_score

    return breakdown


# ============================
# FITNESS FUNCTION
# ============================

def fitness(state):
    return sum(_fitness_breakdown(state).values())


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


# ============================
# WORLD CREATION
# ============================

def build_world(genome):
    world = WorldGraph()
    config_wd = config.get("world_definition", {})
    
    # 1. Locations
    locations_cfg = config_wd.get("locations", {})
    for name, data in locations_cfg.items():
        loc = Location(
            name, 
            data.get("tags", []), 
            interact_prompts=_interaction_prompts(data),
            goals=data.get("goals", []),
            distant_descriptions=_location_distant_descriptions(data),
            entered_descriptions=_location_entered_descriptions(data)
        )
        
        # 2. Actions/Outcomes
        actions_list = []
        for action_index, a_data in enumerate(data.get("actions", [])):
            outcomes_list = []
            for outcome_index, o_data in enumerate(a_data.get("outcomes", [])):
                move_to, reveal_location, reveal_npc, reveal_object = _resolve_outcome_links(o_data)
                success_prob = 1.0
                if o_data.get("use_genome_bias"):
                    success_prob = genome.success_bias
                elif "success_prob" in o_data:
                    success_prob = o_data["success_prob"]
                
                # Clue Handling
                tags = copy.copy(o_data.get("tags", []))
                if "setup_clue" in tags:
                    tags.remove("setup_clue")
                    tags.append(_next_setup_tag(action_index, outcome_index))

                outcomes_list.append(Outcome(
                    desc=o_data.get("desc", ""),
                    tags=tags,
                    success_prob=success_prob,
                    health_change=o_data.get("health_change", 0),
                    coin_change=o_data.get("coin_change", 0),
                    spawn=o_data.get("spawn", False),
                    move_to=move_to,
                    reveal_location=reveal_location,
                    reveal_npc=reveal_npc,
                    reveal_object=reveal_object,
                    lead_to_known=o_data.get("lead_to_known", False)
                ))
            actions_list.append(Action(
                name=a_data.get("name", "Action"), 
                outcomes=outcomes_list,
                required_object=a_data.get("required_object"),
                required_tag=a_data.get("required_tag"),
                required_coins=a_data.get("required_coins", 0),
                collects_object=a_data.get("collects_object"),
                consumes_object=a_data.get("consumes_object", False)
            ))
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
            char_outcomes_list = []
            for o_data in a_data.get("outcomes", []):
                move_to, reveal_location, reveal_npc, reveal_object = _resolve_outcome_links(o_data)
                success_prob = 1.0
                if o_data.get("use_genome_bias"):
                    success_prob = genome.success_bias
                elif "success_prob" in o_data:
                    success_prob = o_data["success_prob"]
                
                char_outcomes_list.append(Outcome(
                    desc=o_data.get("desc", ""),
                    tags=o_data.get("tags", []),
                    success_prob=success_prob,
                    health_change=o_data.get("health_change", 0),
                    coin_change=o_data.get("coin_change", 0),
                    spawn=o_data.get("spawn", False),
                    move_to=move_to,
                    reveal_location=reveal_location,
                    reveal_npc=reveal_npc,
                    reveal_object=reveal_object,
                    lead_to_known=o_data.get("lead_to_known", False)
                ))
            char_actions_list.append(Action(
                name=f"({char_data['name']}) {a_data.get('name', 'Action')}", 
                outcomes=char_outcomes_list,
                character_name=char_data['name'],
                required_object=a_data.get("required_object"),
                required_tag=a_data.get("required_tag"),
                required_coins=a_data.get("required_coins", 0),
                collects_object=a_data.get("collects_object"),
                consumes_object=a_data.get("consumes_object", False)
            ))
        
        char = Character(
            char_data["name"], 
            char_data.get("associated_tags", []), 
            goals=char_data.get("goals", []),
            descriptions=char_data.get("descriptions", []),
            actions=char_actions_list,
            known_locations=char_data.get("known_locations", []),
            known_objects=char_data.get("known_objects", [])
        )
        world.add_character(char)

    # 5. Objects
    objects_cfg = config_wd.get("objects", [])
    for obj_data in objects_cfg:
        obj_actions_list = []
        for a_data in obj_data.get("actions", []):
            obj_outcomes_list = []
            for o_data in a_data.get("outcomes", []):
                move_to, reveal_location, reveal_npc, reveal_object = _resolve_outcome_links(o_data)
                success_prob = 1.0
                if o_data.get("use_genome_bias"):
                    success_prob = genome.success_bias
                elif "success_prob" in o_data:
                    success_prob = o_data["success_prob"]
                
                obj_outcomes_list.append(Outcome(
                    desc=o_data.get("desc", ""),
                    tags=o_data.get("tags", []),
                    success_prob=success_prob,
                    health_change=o_data.get("health_change", 0),
                    coin_change=o_data.get("coin_change", 0),
                    spawn=o_data.get("spawn", False),
                    move_to=move_to,
                    reveal_location=reveal_location,
                    reveal_npc=reveal_npc,
                    reveal_object=reveal_object
                ))
            obj_actions_list.append(Action(
                name=f"[{obj_data['name']}] {a_data.get('name', 'Action')}", 
                outcomes=obj_outcomes_list,
                required_object=a_data.get("required_object"),
                required_tag=a_data.get("required_tag"),
                required_coins=a_data.get("required_coins", 0),
                collects_object=a_data.get("collects_object"),
                consumes_object=a_data.get("consumes_object", False)
            ))
        
        obj = Object(
            obj_data["name"], 
            obj_data.get("associated_tags", []), 
            interact_prompts=obj_data.get("interact_prompts", []),
            goals=obj_data.get("goals", []),
            descriptions=obj_data.get("descriptions", []),
            actions=obj_actions_list,
            collectible=obj_data.get("collectible", False)
        )
        world.add_object(obj)

    return world


def print_action_graph(world):
    print("\n" + "=" * 40)
    print("      FULL WORLD ACTION GRAPH")
    print("=" * 40)

    for name, loc in world.locations.items():
        print(f"\n[Location: {name}]")
        if loc.description:
            print(f"  Description: {loc.description}")
        print(f"  Tags: {', '.join(loc.tags)}")
        
        if not loc.actions:
            print("  (No actions defined)")
            continue

        for action in loc.actions:
            print(f"  > Action: {action.name}")
            for outcome in action.outcomes:
                status = f"Success Prob: {outcome.success_prob:.2f}"
                effects = []
                if outcome.health_change != 0:
                    effects.append(f"Health {outcome.health_change:+}")
                if outcome.spawn:
                    effects.append("New Location Spawn")
                if outcome.move_to:
                    effects.append(f"Move to {outcome.move_to}")
                if outcome.tags:
                    effects.append(f"Tags: {', '.join(outcome.tags)}")
                
                effect_str = f" | {', '.join(effects)}" if effects else ""
                print(f"    - Outcome: {outcome.desc:<30} ({status}{effect_str})")
    print("\n" + "=" * 40 + "\n")


# ============================
# EVALUATION
# ============================

def evaluate_genome(genome, rng_seed=None, stop_event=None):
    total = 0
    best_episode = None
    best_episode_score = float("-inf")
    episodes_completed = 0
    rng = random.Random(rng_seed) if rng_seed is not None else random.Random()

    for _ in range(EPISODES_PER_GENOME):
        if stop_event and stop_event.is_set() and best_episode is not None:
            break

        world = build_world(genome)
        state = GameState(world, genome, rng=rng)

        for _ in range(MAX_STEPS):
            if stop_event and stop_event.is_set():
                break
            if state.health <= 0:
                break
            if not state.step():
                break

        episode_score = fitness(state)
        total += episode_score
        episodes_completed += 1
        if episode_score > best_episode_score:
            best_episode_score = episode_score
            best_episode = state

    if episodes_completed == 0:
        return float("-inf"), best_episode_score, best_episode

    return total / episodes_completed, best_episode_score, best_episode


# ============================
# EVOLUTION LOOP
# ============================

def _base_evaluation_seed():
    seed = config.get("reproducibility.seed", config.get("simulation.seed"))
    return int(seed) if seed is not None else None


def _evaluation_seed(base_seed, generation, genome_index):
    if base_seed is None:
        return None
    return base_seed + generation * max(1, POP_SIZE) + genome_index


def _worker_evaluate_genome(args):
    index, genome, rng_seed = args
    score, episode_score, _ = evaluate_genome(genome, rng_seed=rng_seed)
    return index, score, episode_score


def evaluate_population(population, generation, base_seed, stop_event=None, executor=None):
    max_workers = max(1, min(int(EVALUATION_THREADS), len(population)))
    if max_workers == 1:
        results = []
        for index, genome in enumerate(population):
            if stop_event and stop_event.is_set() and results:
                break
            score, episode_score, state = evaluate_genome(
                genome,
                rng_seed=_evaluation_seed(base_seed, generation, index),
                stop_event=stop_event,
            )
            results.append((score, genome, episode_score, state))
        return results

    if PARALLEL_BACKEND == "processes":
        results = [None] * len(population)
        tasks = [
            (index, genome, _evaluation_seed(base_seed, generation, index))
            for index, genome in enumerate(population)
        ]
        local_executor = executor is None
        process_executor = executor or ProcessPoolExecutor(max_workers=max_workers)
        futures = {
            process_executor.submit(_worker_evaluate_genome, task): task[0]
            for task in tasks
        }
        try:
            for future in as_completed(futures):
                index, score, episode_score = future.result()
                results[index] = (score, population[index], episode_score, None)
                if stop_event and stop_event.is_set():
                    for pending in futures:
                        pending.cancel()
                    break
        finally:
            if local_executor:
                process_executor.shutdown(
                    wait=not (stop_event and stop_event.is_set()),
                    cancel_futures=True,
                )
        return [result for result in results if result is not None]

    results = [None] * len(population)
    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = {
            executor.submit(
                evaluate_genome,
                genome,
                _evaluation_seed(base_seed, generation, index),
                stop_event,
            ): index
            for index, genome in enumerate(population)
    }
    try:
        for future in as_completed(futures):
            index = futures[future]
            score, episode_score, state = future.result()
            results[index] = (score, population[index], episode_score, state)
            if stop_event and stop_event.is_set():
                for pending in futures:
                    pending.cancel()
                break
    finally:
        executor.shutdown(wait=not (stop_event and stop_event.is_set()), cancel_futures=True)

    results = [result for result in results if result is not None]
    return results


def evolve(stop_event=None):
    population = [Genome() for _ in range(POP_SIZE)]
    base_seed = _base_evaluation_seed()
    best_run = None
    max_workers = max(1, min(int(EVALUATION_THREADS), len(population)))
    process_executor = None

    if max_workers > 1 and PARALLEL_BACKEND == "processes":
        process_executor = ProcessPoolExecutor(max_workers=max_workers)

    try:
        for gen in range(GENERATIONS):
            scored = evaluate_population(
                population,
                gen,
                base_seed,
                stop_event=stop_event,
                executor=process_executor,
            )
            if not scored:
                break
            scored.sort(key=lambda x: x[0], reverse=True)
            if scored[0][3] is None:
                best_index = population.index(scored[0][1])
                _, best_episode_score, best_episode_state = evaluate_genome(
                    scored[0][1],
                    rng_seed=_evaluation_seed(base_seed, gen, best_index),
                    stop_event=stop_event,
                )
                scored[0] = (scored[0][0], scored[0][1], best_episode_score, best_episode_state)
            if best_run is None or scored[0][0] > best_run[0]:
                best_run = scored[0]

            print(f"Gen {gen} | Best Score: {scored[0][0]:.2f}")

            if stop_event and stop_event.is_set():
                break

            # Select top 25% (at least 1)
            survivors = [g for _, g, _, _ in scored[:max(1, POP_SIZE // 4)]]

            # Reproduce
            new_population = []
            while len(new_population) < POP_SIZE:
                parent = random.choice(survivors)
                child = copy.deepcopy(parent)
                child.mutate()
                new_population.append(child)

            population = new_population
    finally:
        if process_executor is not None:
            process_executor.shutdown(
                wait=not (stop_event and stop_event.is_set()),
                cancel_futures=True,
            )

    if best_run is not None:
        return best_run

    fallback_genome = population[0] if population else Genome()
    score, episode_score, state = evaluate_genome(fallback_genome, rng_seed=base_seed)
    return score, fallback_genome, episode_score, state


# ============================
# RUN
# ============================

def _runs_dir():
    path = os.path.join(os.path.dirname(__file__), "runs")
    if not os.path.exists(path):
        os.makedirs(path)
    return path


def _run_archive_dir(timestamp):
    base_path = os.path.join(_runs_dir(), timestamp)
    path = base_path
    suffix = 1
    while os.path.exists(path):
        path = f"{base_path}_{suffix:03d}"
        suffix += 1
    if not os.path.exists(path):
        os.makedirs(path)
    return path


def _world_definition_with_runtime_connections(state):
    world_definition = copy.deepcopy(config.get("world_definition", {}))
    spawn_rules = copy.deepcopy(world_definition.get("spawn_rules", {}))

    for name, loc in state.world.locations.items():
        connected = sorted(loc.connected)
        if connected:
            existing = set(spawn_rules.get(name, []))
            spawn_rules[name] = sorted(existing | set(connected))

    world_definition["spawn_rules"] = spawn_rules
    return world_definition


def _resolve_project_path(path):
    path = os.path.expanduser(path.strip())
    if os.path.isabs(path):
        return path
    return os.path.abspath(os.path.join(os.path.dirname(__file__), path))


def load_world_from_path(world_path):
    resolved_path = _resolve_project_path(world_path)
    if not os.path.exists(resolved_path):
        print(f"[ARCHIVE] World file not found: {resolved_path}")
        return None

    with open(resolved_path, "r") as f:
        data = yaml.safe_load(f) or {}

    world_definition = data.get("world_definition", data)
    if not isinstance(world_definition, dict) or "locations" not in world_definition:
        print(f"[ARCHIVE] No world_definition.locations found in: {resolved_path}")
        return None

    config.set_world_definition(world_definition)
    print(f"[ARCHIVE] Loaded world from: {resolved_path}")
    return resolved_path


def load_best_world_if_available():
    if not config.get("experiment_archive.load_best_world_on_startup", True):
        return None

    best_world_file = os.path.join(_runs_dir(), "best_world.yaml")
    if not os.path.exists(best_world_file):
        return None

    with open(best_world_file, "r") as f:
        data = yaml.safe_load(f) or {}

    world_definition = data.get("world_definition")
    if not world_definition:
        return None

    config.set_world_definition(world_definition)
    print(f"[ARCHIVE] Loaded best world from: {best_world_file}")
    return best_world_file


def save_best_artifacts(score, genome, state, run_dir=None, timestamp=None, reason="Best structural evolution score"):
    if state is None:
        return None, None

    runs_dir = _runs_dir()
    timestamp = timestamp or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = run_dir or _run_archive_dir(timestamp)
    best_world_file = os.path.join(run_dir, "best_world.yaml")
    best_story_file = os.path.join(run_dir, "best_story.txt")
    latest_best_world_file = os.path.join(runs_dir, "best_world.yaml")
    latest_best_story_file = os.path.join(runs_dir, "best_story.txt")

    payload = {
        "saved_at": timestamp,
        "score": float(score),
        "reason": reason,
        "best_genome": {k: float(v) for k, v in vars(genome).items()},
        "path": state.path,
        "world_definition": _world_definition_with_runtime_connections(state),
    }

    for path in (best_world_file, latest_best_world_file):
        with open(path, "w") as f:
            yaml.safe_dump(payload, f, sort_keys=False, allow_unicode=False)

    for path in (best_story_file, latest_best_story_file):
        with open(path, "w") as f:
            f.write("=" * 60 + "\n")
            f.write(f" BEST SCORE STORY: {timestamp}\n")
            f.write("=" * 60 + "\n\n")
            f.write(f"Best Score: {score:.2f}\n")
            f.write(f"Traveler's Path: {' -> '.join(state.path)}\n\n")
            f.write("--- FULL STORY TRANSCRIPT ---\n")
            f.write(state.get_full_story())
            f.write("\n")

    return best_world_file, best_story_file

def prompt_story_setup_editor():
    editor = ConfigEditor(os.path.dirname(__file__))

    print("\nStartup options:")
    print("  1. Run the story generator")
    print("  2. Show the existing story setup")
    print("  3. Edit story setup, then run")
    print("  4. Edit story setup only")
    print("  5. Load a world YAML path, then run")

    while True:
        choice = input("Selection [1/2/3/4/5]: ").strip() or "1"
        if choice == "1":
            return True, None
        if choice == "2":
            editor.browse_story()
            continue
        if choice == "3":
            should_run = editor.run()
            config.reload()
            return should_run, None
        if choice == "4":
            editor.run()
            config.reload()
            return False, None
        if choice == "5":
            world_path = input("World YAML path: ").strip()
            if world_path:
                return True, world_path
            print("Please enter a path, or choose another option.")
            continue
        print("Please choose 1, 2, 3, 4, or 5.")


def run_story(world_path=None):
    config.reload()
    refresh_runtime_settings()
    apply_random_seed()
    if world_path:
        load_world_from_path(world_path)
    else:
        load_best_world_if_available()

    # Check for World Augmentation
    # Ensure the output directory exists as per user request
    output_dir = config.get("augmentation.output_dir", "augmentations")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        print(f"Created directory: {output_dir}")

    if config.get("augmentation.enabled", False):
        from core_engine.augmenter import WorldAugmenter
        print("\n--- Checking for World Augmentation ---")
        augmenter = WorldAugmenter()
        res = augmenter.augment(update_config=True)
        if res and res[0]:
            augmented_data, augmented_path = res
            print(f"\n[SUCCESS] World expanded! Using augmented configuration found in: {augmented_path}")
        else:
            print("Augmentation skipped or failed.")

    # Print world locations after potential augmentation
    print("\n--- World Locations (Active Configuration) ---")
    dummy_world = build_world(Genome())
    for name, loc in dummy_world.locations.items():
        print(f"Location: {name:<15} | Tags: {', '.join(loc.tags)}")
    print("-" * 30)
    for obj in dummy_world.objects:
        print(f"Object:   {obj.name:<15} | Tags: {', '.join(obj.associated_tags)}")
    print("-" * 30 + "\n")

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = _run_archive_dir(timestamp)
    timestamp = os.path.basename(run_dir)
    print(f"[ARCHIVE] This generation run will be saved in: {run_dir}")
    print("[GENERATION] Press 'q' to stop generation early and execute the best story found so far.")
    with GenerationStopper() as stopper:
        best_score, best_genome, best_episode_score, best_episode_state = evolve(
            stop_event=stopper.stop_event,
        )
    best_world_file, best_story_file = save_best_artifacts(
        best_episode_score,
        best_genome,
        best_episode_state,
        run_dir=run_dir,
        timestamp=timestamp,
    )

    print("\nBest Genome:")
    for k, v in vars(best_genome).items():
        print(f"{k}: {v:.3f}")

    print("Score:", best_score)
    if best_world_file and best_story_file:
        print(f"[ARCHIVE] Best score world saved to: {best_world_file}")
        print(f"[ARCHIVE] Best score story saved to: {best_story_file}")

    print("\nSimulating detailed best story...")
    final_world = build_world(best_genome)
    
    # Obtain simulation parameters from config
    sim_verbose = config.get("simulation.verbose", True)
    sim_interactive = config.get("simulation.interactive", True)
    
    final_state = GameState(final_world, best_genome, 
                            verbose=True, 
                            interactive=sim_interactive)
    for _ in range(MAX_STEPS):
        if final_state.health <= 0: break
        if not final_state.step(): break
    
    # (Transcript moved to evaluation section below for visibility)

    print(f"\nTraveler's Path: {' -> '.join(final_state.path)}")

    # print_action_graph(final_world)

    # Final Narrative Report & Evaluation
    print("\n" + "="*50)
    print("      THE UNFOLDED STORY (Full Transcript)")
    print("="*50)
    print(final_state.get_full_story())
    print("="*50 + "\n")

    if config.get("llm_evaluator.enabled", False):
        print("\n--- Final Narrative Evaluation ---")
        from evaluator.llm_eval import LLMNarrativeEvaluator
        evaluator = LLMNarrativeEvaluator()
        score, reason = evaluator.evaluate_with_debug(final_state)
        print(f"\nFinal Narrative Score: {score}/100")
        print(f"Reasoning: {reason}")
    else:
        score, reason = "N/A", "Evaluation Disabled"

    # --- Save Run Archival ---
    run_file = os.path.join(run_dir, f"story_{timestamp}.txt")
    
    with open(run_file, "w") as f:
        f.write("="*60 + "\n")
        f.write(f" ADAPTIVE NARRATIVE RUN: {timestamp}\n")
        f.write("="*60 + "\n\n")
        
        f.write("--- EVOLUTION PARAMETERS (Best Genome) ---\n")
        for k, v in vars(best_genome).items():
            f.write(f"{k}: {v:.4f}\n")
        f.write("\n")
        
        f.write("--- NARRATIVE EVALUATION ---\n")
        f.write(f"Final Score: {score}/100\n")
        f.write(f"Reasoning: {reason}\n\n")
        
        f.write("--- TRAVELER'S PATH ---\n")
        f.write(" -> ".join(final_state.path) + "\n\n")
        f.write("--- FULL STORY TRANSCRIPT ---\n")
        f.write(final_state.get_full_story())
        f.write("\n\n" + "="*60 + "\n")

    print(f"\n[ARCHIVE] Story successfully saved to: {run_file}")


if __name__ == "__main__":
    should_run, world_path = prompt_story_setup_editor()
    if should_run:
        run_story(world_path=world_path)
