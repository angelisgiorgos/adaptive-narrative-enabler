"""The fitness function: how engaging a finished story is. Evolution maximises it."""
from utils.config_loader import config
from utils.tag_similarity import semantic_tag_similarity

from . import rules


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

def _thirds(values):
    """The first and the last third of a sequence (at least one element each)."""
    size = max(1, len(values) // 3)
    return values[:size], values[-size:]


def _share(values):
    return sum(values) / len(values) if values else 0.0


def _engagement(state, cfg):
    """What a player feels turn by turn, from the story's turn log.

    A turn is *new* when it brings a place, an event, a person, an outcome not told
    before, a skill check, or a change of health or coins; otherwise it is filler.
    A *loop* is an action repeated with the same outcome within ``loop_window``
    turns. *Tension* is damage, an event under way or a failed check. A story is
    better when few turns are filler, it does not loop, the new content does not
    fade toward the end, and tension rises (escalation)."""
    log = getattr(state, "turn_log", None) or []
    if not cfg or not log:
        return {}
    places, events, people, outcomes = {log[0]["location_before"]}, set(), set(), set()
    new_turns, tension, loops = [], [], 0
    window = max(1, int(cfg.get("loop_window", 6)))
    previous = None
    for index, turn in enumerate(log):
        new = False
        if turn["location_after"] not in places:
            places.add(turn["location_after"])
            new = True
        for event in (turn.get("event"), turn.get("event_after")):
            if event and event not in events:
                events.add(event)
                new = True
        if turn.get("source") in ("npc", "conversation") and turn.get("owner") and turn["owner"] not in people:
            people.add(turn["owner"])
            new = True
        if turn.get("outcome") and turn["outcome"] not in outcomes:
            outcomes.add(turn["outcome"])
            new = True
        hurt = previous is not None and turn["health"] < previous["health"]
        if turn.get("check") or (previous is not None and (hurt or turn["coins"] != previous["coins"])):
            new = True
        new_turns.append(new)
        tension.append(bool(hurt or turn.get("event_after") or (turn.get("check") and not turn.get("succeeded"))))
        if any(earlier["action"] == turn["action"] and earlier.get("outcome") == turn.get("outcome")
               for earlier in log[max(0, index - window):index]):
            loops += 1
        previous = turn
    terms = {
        "filler": -(1.0 - _share(new_turns)) * float(cfg.get("filler_penalty", 0)),
        "loops": -loops * float(cfg.get("loop_penalty", 0)),
    }
    if len(log) >= 6:
        early_new, late_new = _thirds(new_turns)
        early_tension, late_tension = _thirds(tension)
        terms["novelty_fade"] = -max(0.0, _share(early_new) - _share(late_new)) * float(cfg.get("novelty_fade_penalty", 0))
        terms["escalation"] = max(0.0, _share(late_tension) - _share(early_tension)) * float(cfg.get("escalation_bonus", 0))
    return terms


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
    # Clues and payoffs are matched as the storyteller matches them (story_rules.clue_matching).
    pairs, open_setups, unmatched_payoffs = rules.clue_pairs(
        state.setup_indices, state.payoff_indices, rules.settings()["clue_matching"],
    )
    for _, s_idx, _, p_idx in pairs:
        gap = p_idx - s_idx
        min_gap = total_steps * setup_cfg.get("min_gap_fraction", 1 / 3)
        if gap > min_gap:
            pair_bonus += setup_cfg.get("pair_bonus", 20)
            pair_bonus += gap * setup_cfg.get("gap_step_bonus", 2)
            if p_idx >= total_steps * 0.66:
                pair_bonus += setup_cfg.get("late_payoff_bonus", 0)
        else:
            pair_bonus += setup_cfg.get("short_gap_bonus", 5)

    pair_bonus += len(pairs) * setup_cfg.get("target_count_bonus", 0)
    pair_bonus -= len(open_setups) * setup_cfg.get("unmatched_setup_penalty", 0)
    pair_bonus -= len(unmatched_payoffs) * setup_cfg.get("unmatched_payoff_penalty", 0)
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
        # Diminishing returns: talking to the same NPC again and again is not a
        # better story, so interactions beyond the cap earn nothing.
        count = min(count, int(fitness_cfg.get("npc_interaction_cap", 3)))
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
    # Variety: many distinct choices and no repeated outcomes keep a story engaging.
    variety_cfg = fitness_cfg.get("variety", {})
    choice_counts = getattr(state, "choice_counts", {})
    outcome_counts = getattr(state, "outcome_counts", {})
    chosen = sum(choice_counts.values())
    breakdown["variety"] = (
        (len(choice_counts) / chosen if chosen else 0.0) * variety_cfg.get("distinct_choice_weight", 30)
        - sum(count - 1 for count in outcome_counts.values() if count > 1)
        * variety_cfg.get("repeated_outcome_penalty", 2)
    )

    event_cfg = fitness_cfg.get("unexpected_events", {})
    event_count = len(getattr(state, "unexpected_events", []))
    event_target = int(event_cfg.get("target", 3))
    breakdown["unexpected_events"] = (
        min(event_count, event_target) * event_cfg.get("variety_bonus", 5)
        - max(0, event_count - event_target) * event_cfg.get("excess_penalty", 3)
    )

    end_score = 0.0
    # Progress through a staged mission counts, won or not.
    end_score += getattr(state, "mission_stage", 0) * base_cfg.get("stage_bonus", 0)
    if getattr(state, "won", False):
        # A rushed win is worth less than a win after a full story.
        min_steps = max(1, base_cfg.get("mission_win_min_steps", 20))
        end_score += base_cfg.get("mission_win_bonus", 60) * min(1.0, total_steps / min_steps)
    if state.ended:
        if state.health <= 0:
            end_score -= base_cfg.get("death_penalty", 0)
        else:
            end_score += base_cfg.get("completion_bonus", 0)
        # end_kind, not the (customisable) end_reason text, identifies the ending.
        end_kind = getattr(state, "end_kind", None)
        if end_kind == "stalled":
            end_score -= base_cfg.get("stall_penalty", 0)
        if end_kind == "dungeon":
            end_score -= base_cfg.get("dungeon_penalty", 0)
        if end_kind == "action_limit":  # cut off without an ending (the director ends with an epilogue)
            end_score += base_cfg.get("max_action_completion_bonus", 0)
            end_score -= base_cfg.get("action_limit_penalty", 0)
    if not state.ended:
        # Cut by the evaluation length before it ended: its ending was never judged.
        end_score -= base_cfg.get("unfinished_penalty", 0)
    breakdown["ending"] = end_score

    # Repetition: the same action again and again, and A -> B -> A travel loops.
    repetition_cfg = fitness_cfg.get("repetition", {})
    free_uses = int(repetition_cfg.get("free_uses", 2))
    repeats = sum(max(0, count - free_uses) for key, count in choice_counts.items() if key[0] == "action")
    loops = sum(1 for i in range(len(state.path) - 2) if state.path[i] == state.path[i + 2])
    breakdown["repetition"] = -(
        repeats * repetition_cfg.get("action_repeat_penalty", 0) + loops * repetition_cfg.get("backtrack_penalty", 0)
    )

    # Pacing: a mission won before the story had room to unfold.
    pacing_cfg = fitness_cfg.get("pacing", {})
    breakdown["pacing"] = 0.0
    if getattr(state, "won", False):
        min_steps = base_cfg.get("mission_win_min_steps", 20)
        breakdown["pacing"] = -max(0, min_steps - total_steps) * pacing_cfg.get("early_win_penalty_per_step", 0)

    breakdown.update(_engagement(state, fitness_cfg.get("engagement", {})))

    # Terms that grow with every step: a longer story is not better merely for being
    # longer (story_length judges length). mode "rate" counts them per turn (x the
    # target length; a story shorter than floor_steps counts as that long), mode "cap"
    # only scales longer stories down to the target length.
    normalization = fitness_cfg.get("length_normalization", {})
    if normalization.get("enabled", False):
        length_cfg = fitness_cfg.get("story_length", {})
        target = length_cfg.get("target", 35)
        if normalization.get("mode", "cap") == "rate":
            floor = max(1, int(normalization.get("floor_steps", length_cfg.get("min", 20))))
            scale = target / max(total_steps, floor)
        else:
            scale = min(1.0, target / total_steps)
        for term in normalization.get("terms", []):
            if term in breakdown:
                breakdown[term] *= scale

    # The story's rhythm: surprises, events lived through, threads closed, places explored.
    breakdown.update(_rhythm(state, fitness_cfg.get("rhythm", {})))
    # People helped: each NPC whose goal the story fulfilled (npc_goals).
    breakdown["npc_goals"] = len(getattr(state, "allies", ())) * base_cfg.get("npc_goal_bonus", 8)
    # What the story lacks (objective_losses), subtracted.
    for name, value in objective_losses(state).items():
        breakdown[f"loss_{name}"] = -value
    return breakdown

def fitness(state):
    return sum(_fitness_breakdown(state).values())


fitness.rewards_alternatives = True  # evolution adds evolution.alternatives_weight x alternatives(stories)


# ---- rhythm: what makes a story unpredictable and eventful ---------------------------------

def _developments(state):
    """[(turn, kinds)] of what each turn brought: place (a new one), event, person, outcome
    (not told before), check, fight (a threat or combat), hurt, gain (coins or an item),
    discovery (a path opened), payoff, ally. A turn with no kind is calm."""
    log = getattr(state, "turn_log", None) or []
    if not log:
        return []
    places, events, people, outcomes = {log[0]["location_before"]}, set(), set(), set()
    discoveries = sorted(r["turn"] for r in getattr(state, "transition_records", []) if r["kind"] == "discovery")
    result, previous = [], None
    for turn in log:
        kinds = set()
        if turn["location_after"] not in places:
            places.add(turn["location_after"])
            kinds.add("place")
        for event in (turn.get("event"), turn.get("event_after")):
            if event and event not in events:
                events.add(event)
                kinds.add("event")
        if turn.get("source") in ("npc", "conversation") and turn.get("owner") and turn["owner"] not in people:
            people.add(turn["owner"])
            kinds.add("person")
        if turn.get("outcome") and turn["outcome"] not in outcomes:
            outcomes.add(turn["outcome"])
            kinds.add("outcome")
        if turn.get("check"):
            kinds.add("check")
        tags = {str(t).lower() for t in turn.get("tags", [])}
        if tags & {"threat", "combat"}:
            kinds.add("fight")
        if any(t.startswith("payoff_") for t in tags):
            kinds.add("payoff")
        if previous is not None and turn["health"] < previous["health"]:
            kinds.add("hurt")
        if previous is not None and turn["coins"] > previous["coins"]:
            kinds.add("gain")
        if turn["turn"] in discoveries:
            kinds.add("discovery")
        result.append((turn["turn"], kinds))
        previous = turn
    return result


def _rhythm(state, cfg):
    """What makes a story worth playing turn after turn (fitness.rhythm):

    - predictability: the longest run of calm turns (nothing new) beyond calm_allowed
      costs predictability_penalty per turn;
    - variety: each kind of development the story offers (a new place, an event, a fight, a
      check, a discovery, a payoff, ...) is worth variety_bonus (up to variety_cap kinds);
    - events: each distinct event lived through is worth event_bonus (up to event_cap);
    - open_threads: a story that ends with clues still open (planted, never paid off) costs
      open_thread_penalty for each;
    - exploration: each distinct place visited beyond the first is worth exploration_bonus
      (up to exploration_cap)."""
    if not cfg or not cfg.get("enabled", True):
        return {}
    developments = _developments(state)
    if not developments:
        return {}
    longest = run = 0
    for _, kinds in developments:
        run = 0 if kinds else run + 1
        longest = max(longest, run)
    kinds_seen = set().union(*(kinds for _, kinds in developments))
    events = {event for turn in state.turn_log for event in (turn.get("event"), turn.get("event_after")) if event}
    places = {name for name, count in state.location_counts.items() if count}
    terms = {
        "predictability": -max(0, longest - int(cfg.get("calm_allowed", 3))) * float(cfg.get("predictability_penalty", 6)),
        "variety_of_play": min(len(kinds_seen), int(cfg.get("variety_cap", 9))) * float(cfg.get("variety_bonus", 3)),
        "events_lived": min(len(events), int(cfg.get("event_cap", 4))) * float(cfg.get("event_bonus", 5)),
        "exploration": min(max(0, len(places) - 1), int(cfg.get("exploration_cap", 8))) * float(cfg.get("exploration_bonus", 2)),
    }
    if state.ended and state.end_kind not in (None, "death"):
        terms["open_threads"] = -len(state._open_clues()) * float(cfg.get("open_thread_penalty", 8))
    # Trying the same thing again right after it failed reads as padding.
    retries, failed_at = 0, {}
    for turn in state.turn_log:
        name = turn.get("action")
        if name in failed_at and turn["turn"] - failed_at[name] <= 3:
            retries += 1
        if not turn.get("succeeded", True) and turn.get("source") not in ("navigation", "conversation"):
            failed_at[name] = turn["turn"]
    terms["repeated_failures"] = -retries * float(cfg.get("repeated_failure_penalty", 6))
    return terms


def story_signature(state):
    """What a story was made of, for telling stories apart: the places it visited, the events
    it lived through, the people it met and the outcomes it told."""
    signature = {("place", name) for name, count in state.location_counts.items() if count}
    for turn in getattr(state, "turn_log", None) or []:
        for event in (turn.get("event"), turn.get("event_after")):
            if event:
                signature.add(("event", event))
        if turn.get("source") in ("npc", "conversation") and turn.get("owner"):
            signature.add(("person", turn["owner"]))
        if turn.get("outcome"):
            signature.add(("outcome", turn["outcome"]))
    return signature


def alternatives(states):
    """0..1: how different a storyteller's stories are from one another (the mean Jaccard
    distance of their signatures): a storyteller that tells the same story whatever the
    start is predictable; evolution.alternatives_weight rewards telling alternatives."""
    signatures = [story_signature(state) for state in states if getattr(state, "actions_taken", 0)]
    if len(signatures) < 2:
        return 0.0
    distances = []
    for index, first in enumerate(signatures):
        for second in signatures[index + 1:]:
            union = first | second
            distances.append(1.0 - len(first & second) / len(union) if union else 0.0)
    return sum(distances) / len(distances)


# ---- objective losses: what a story lacks ------------------------------------------------

_SETTLED = ("travel", "sent", "auto", "back")  # moves the player chose, or the story's structure made


def _world_tags(world):
    """Every tag the world can show: its places', people's, items' and outcomes' tags."""
    cached = world.__dict__.get("_tag_universe")
    if cached is None:
        tags = set()
        entities = list(world.locations.values()) + list(world.characters) + list(world.objects) + list(world.events.values())
        for entity in entities:
            tags |= {str(t).lower() for t in getattr(entity, "tags", None) or getattr(entity, "associated_tags", ())}
            for action in getattr(entity, "actions", ()):
                for outcome in action.outcomes:
                    tags |= {str(t).lower() for t in outcome.tags if not str(t).lower().startswith(("setup_", "payoff_"))}
        cached = world._tag_universe = tags
    return cached


def objective_losses(state, cfg=None):
    """What a story lacks, as non-negative losses subtracted from its score
    (fitness.objective_losses; each term weight x shortfall):

    - transition_quality: the places of each move should fit each other (tag similarity
      below target_similarity); a story that never moves costs missing_penalty.
    - tag_coverage: the share of the world's tags the story shows, below target_ratio.
    - named_transition_dependence: the share of discoveries and engine-made moves that
      only follow an authored name (not tags, a lead or the map), above allowed_ratio.
    - scenario_duplication: the same place, action and outcome told again (share of the
      turns, to the power `power`).
    - world_extension: too little of the world discovered (below target_discovery_ratio),
      or a known map without branches (paths per place below target_branching).
    """
    cfg = (config.get("fitness.objective_losses") or {}) if cfg is None else cfg
    if not cfg.get("enabled", True) or not state.actions_taken:
        return {}
    world, losses = state.world, {}

    quality = cfg.get("transition_quality", {})
    path = [name for index, name in enumerate(state.path) if index == 0 or name != state.path[index - 1]]
    moves = [(a, b) for a, b in zip(path, path[1:]) if a in world.locations and b in world.locations]
    if moves:
        mean = sum(semantic_tag_similarity(world.locations[a].tags, world.locations[b].tags) for a, b in moves) / len(moves)
        losses["transition_quality"] = max(0.0, float(quality.get("target_similarity", 0.65)) - mean) * float(quality.get("weight", 60.0))
    else:
        losses["transition_quality"] = float(quality.get("missing_penalty", 5.0))

    coverage = cfg.get("tag_coverage", {})
    universe = _world_tags(world)
    if universe:
        seen = {str(t).lower() for t in _flatten_seen_tags(state.tags_seen)} & universe
        losses["tag_coverage"] = max(0.0, float(coverage.get("target_ratio", 0.6)) - len(seen) / len(universe)) * float(coverage.get("weight", 40.0))

    named = cfg.get("named_transition_dependence", {})
    resolved = [r for r in getattr(state, "transition_records", []) if r["resolution"] not in _SETTLED]
    if resolved:
        share = sum(1 for r in resolved if r["resolution"] == "named") / len(resolved)
        losses["named_transition_dependence"] = max(0.0, share - float(named.get("allowed_ratio", 0.5))) * float(named.get("weight", 30.0))

    duplication = cfg.get("scenario_duplication", {})
    repeats = sum(count - 1 for count in getattr(state, "scenario_counts", {}).values() if count > 1)
    losses["scenario_duplication"] = (repeats / state.actions_taken) ** float(duplication.get("power", 2.0)) * float(duplication.get("weight", 80.0))

    extension = cfg.get("world_extension", {})
    known = state.map_view() if hasattr(state, "map_view") else None
    if known and world.locations:
        ratio = len(known["places"]) / len(world.locations)
        branching = 2 * len(known["paths"]) / max(1, len(known["places"]))
        losses["world_extension"] = (
            max(0.0, float(extension.get("target_discovery_ratio", 0.45)) - ratio) * float(extension.get("discovery_weight", 45.0))
            + max(0.0, float(extension.get("target_branching", 2.4)) - branching) * float(extension.get("branching_weight", 8.0)))
    return losses
