from utils.config_loader import config


# ============================
# ACTIONS / OUTCOMES
# ============================

# Action.repeat values: once per game, again on each visit, or every turn.
REPEAT_RULES = ("once", "per_visit", "always")

class Outcome:
    def __init__(self, desc, tags=None,
                 success_prob=1.0,
                 health_change=0,
                 coin_change=0,
                 spawn=False,
                 move_to=None,
                 move_to_tags=None,
                 reveal_npc=None,
                 reveal_object=None,
                 reveal_location=None,
                 lead_to_known=False,
                 remove_npc=None,
                 send_to=None,
                 set_variables=None,
                 change_variables=None,
                 on_check=None,
                 change_stats=None):
        self.desc = desc
        self.tags = tags or []
        self.success_prob = success_prob
        self.health_change = health_change
        self.coin_change = coin_change
        self.spawn = spawn
        self.move_to = move_to
        self.move_to_tags = move_to_tags or []
        self.reveal_npc = reveal_npc
        self.reveal_object = reveal_object
        self.reveal_location = reveal_location
        self.lead_to_known = lead_to_known
        # True removes the acting/event NPC; a name removes that NPC from the story.
        self.remove_npc = remove_npc
        # One-way move: the player is taken to this location and cannot simply
        # walk back (e.g. arrested and thrown into the Dungeon).
        self.send_to = send_to
        # Persistent variables: set to a value, or change a number by a delta.
        self.set_variables = dict(set_variables or {})
        self.change_variables = dict(change_variables or {})
        # Skill checks: "pass" / "fail" limits the outcome to that check result
        # (None: either); change_stats raises or lowers stats for the playthrough.
        self.on_check = on_check
        self.change_stats = dict(change_stats or {})

    def _clamp(self, value, low=None, high=None):
        low = config.get("outcome_resolution.probability_floor", 0.05) if low is None else low
        high = config.get("outcome_resolution.probability_ceiling", 0.95) if high is None else high
        return max(low, min(high, value))

    def _fitness_constraint_adjustment(self, state):
        if state is None:
            return 0.0

        adjustment = 0.0
        resolution_cfg = config.get("outcome_resolution", {})

        # Help the run recover when health is low and the outcome is restorative.
        low_health_threshold = config.get("game_state.low_health_threshold", 4)
        if state.health <= low_health_threshold and self.health_change > 0:
            adjustment += resolution_cfg.get("low_health_recovery_bonus", 0.08)

        # Encourage movement and discovery when the story stagnates in one place.
        stagnation_threshold = resolution_cfg.get("stagnation_threshold", 2)
        if state.consecutive_steps_in_location >= stagnation_threshold:
            if self.move_to or self.move_to_tags or self.spawn or self.reveal_location:
                adjustment += resolution_cfg.get("stagnation_movement_bonus", 0.1)

        # Reward clue/setup generation early enough in the story to matter later.
        target_length = config.get("fitness.story_length.target", 35)
        if any(tag.startswith("setup_") or tag == "setup_clue" for tag in self.tags):
            if state.actions_taken < max(3, target_length * 0.4):
                adjustment += resolution_cfg.get("early_setup_bonus", 0.05)

        # Slightly dampen very punishing outcomes when the traveler is already weak.
        if state.health <= low_health_threshold and self.health_change < -2:
            adjustment -= resolution_cfg.get("severe_risk_penalty", 0.08)

        return adjustment

    def _genome_adjustment(self, genome):
        if genome is None:
            return 0.0

        adjustment = 0.0
        resolution_cfg = config.get("outcome_resolution", {})
        tag_weights = resolution_cfg.get("tag_weights", {})

        adjustment += (genome.success_bias - 0.5) * resolution_cfg.get("success_bias_weight", 0.3)

        for tag in self.tags:
            tag_lower = tag.lower()
            if "threat" in tag_lower or "combat" in tag_lower:
                adjustment += (genome.threat_prob - 0.5) * tag_weights.get("threat", -0.18)
            if "spawn" in tag_lower:
                adjustment += (genome.spawn_prob - 0.5) * tag_weights.get("spawn", 0.18)
            if "social" in tag_lower:
                adjustment += (genome.bias_social - 0.5) * tag_weights.get("social", 0.12)
            if "stealth" in tag_lower:
                adjustment += (genome.bias_stealth - 0.5) * tag_weights.get("stealth", 0.12)
            if "urban" in tag_lower:
                adjustment += (genome.bias_urban - 0.5) * tag_weights.get("urban", 0.08)
            if "maritime" in tag_lower:
                adjustment += (genome.bias_maritime - 0.5) * tag_weights.get("maritime", 0.08)
            if "luxury" in tag_lower or "palace" in tag_lower:
                adjustment += (genome.bias_luxury - 0.5) * tag_weights.get("luxury", 0.08)
            if tag_lower.startswith("setup_") or tag_lower == "setup_clue":
                adjustment += (genome.setup_prob - 0.5) * tag_weights.get("setup", 0.12)

        if self.reveal_npc or self.reveal_object or self.reveal_location:
            adjustment += (genome.discovery_bias - 0.5) * resolution_cfg.get("discovery_weight", 0.16)
        if self.lead_to_known:
            adjustment += (genome.lead_bias - 0.5) * resolution_cfg.get("lead_weight", 0.16)
        if self.move_to or self.move_to_tags:
            adjustment += (genome.voyage_bias - 0.5) * resolution_cfg.get("voyage_weight", 0.18)
        if self.health_change > 0 or self.coin_change > 0:
            adjustment += (genome.recovery_bias - 0.5) * resolution_cfg.get("recovery_weight", 0.12)

        return adjustment

    def success_probability(self, genome=None, state=None, action=None):
        probability = self.success_prob
        probability += self._genome_adjustment(genome)
        probability += self._fitness_constraint_adjustment(state)

        # Fights and other risky moves are decided by a skill check instead
        # (core_engine/combat.py); this probability covers everything else.

        # Certain mandatory travel/navigation outcomes should remain reliable.
        if action and action.source == "navigation":
            probability = max(probability, config.get("outcome_resolution.navigation_floor", 0.98))

        return self._clamp(probability)


class Action:
    def __init__(self, name, outcomes, character_name=None, 
                 required_object=None, required_tag=None, required_coins=0,
                 collects_object=None, consumes_object=False,
                 triggers_event=None, repeat=None, exclusive_group=None,
                 source=None, owner=None, skill_check=None):
        self.name = name
        self.outcomes = outcomes
        self.character_name = character_name
        self.required_object = required_object
        self.required_tag = required_tag
        self.required_coins = required_coins
        self.collects_object = collects_object
        self.consumes_object = consumes_object
        self.triggers_event = triggers_event # Name of the Event entered after this action succeeds
        # How often the action is offered: "once" (per game), "per_visit", or
        # "always"; None uses action_rules.default_repeat for its source.
        self.repeat = repeat
        # Actions sharing a group form one choice: once one succeeds, the rest disappear.
        self.exclusive_group = exclusive_group
        # What offers the action: location, npc, item, event, navigation, conversation.
        self.source = source
        self.owner = owner # Name of the location, NPC, item, or event offering it
        # {stat, modifier}: taking the action rolls a check of that character stat.
        self.skill_check = dict(skill_check) if skill_check else None
        self.active_item = None # Track the specific item used during a step
