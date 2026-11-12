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

    return breakdown

def fitness(state):
    return sum(_fitness_breakdown(state).values())
