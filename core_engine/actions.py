import random
from utils.config_loader import config


def _inventory_combat_profile(state):
    if state is None or not getattr(state, "world", None):
        return 0.0, 0.0

    attack = 0.0
    defense = 0.0
    combat_cfg = config.get("combat_resolution", {})
    attack_values = combat_cfg.get("attack_tags", {})
    defense_values = combat_cfg.get("defense_tags", {})

    for obj in state.world.objects:
        if obj.name not in state.inventory:
            continue
        tags = {tag.lower() for tag in obj.associated_tags}
        for tag, value in attack_values.items():
            if tag in tags:
                attack += value
        for tag, value in defense_values.items():
            if tag in tags:
                defense += value

    return attack, defense


def _state_power(state):
    if state is None:
        return 0.0
    return getattr(state, "power", 0.0)

# ============================
# ACTIONS / OUTCOMES
# ============================

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
                 lead_to_known=False):
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

    def _clamp(self, value, low=0.05, high=0.95):
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

        attack_bonus, defense_bonus = _inventory_combat_profile(state)
        power = _state_power(state)
        outcome_tags = {tag.lower() for tag in self.tags}
        is_combat = "combat" in outcome_tags or "threat" in outcome_tags
        is_positive = (
            "success" in outcome_tags
            or self.health_change > 0
            or self.coin_change > 0
            or self.spawn
            or self.move_to is not None
            or self.reveal_location is not None
            or self.reveal_npc is not None
            or self.reveal_object is not None
        )
        is_negative = (
            "failure" in outcome_tags
            or "threat" in outcome_tags
            or self.health_change < 0
            or self.coin_change < 0
        )

        if is_combat:
            combat_cfg = config.get("combat_resolution", {})
            if is_positive:
                probability += attack_bonus * combat_cfg.get("attack_success_weight", 0.08)
                probability += defense_bonus * combat_cfg.get("defense_success_weight", 0.04)
                probability += power * combat_cfg.get("power_success_weight", 0.05)
            if is_negative:
                probability -= attack_bonus * combat_cfg.get("attack_failure_weight", 0.06)
                probability -= defense_bonus * combat_cfg.get("defense_failure_weight", 0.08)
                probability -= power * combat_cfg.get("power_failure_weight", 0.04)

        # Certain mandatory travel/navigation outcomes should remain reliable.
        if action and action.name.startswith("Move to"):
            probability = max(probability, config.get("outcome_resolution.navigation_floor", 0.98))

        return self._clamp(probability)

    def success(self, genome=None, state=None, action=None):
        probability = self.success_probability(genome=genome, state=state, action=action)
        rng = getattr(state, "rng", random)
        return rng.random() < probability


class Action:
    def __init__(self, name, outcomes, character_name=None, 
                 required_object=None, required_tag=None, required_coins=0,
                 collects_object=None, consumes_object=False):
        self.name = name
        self.outcomes = outcomes
        self.character_name = character_name
        self.required_object = required_object
        self.required_tag = required_tag
        self.required_coins = required_coins
        self.collects_object = collects_object
        self.consumes_object = consumes_object
        self.active_item = None # Track the specific item used during a step

    def choose_outcome(self, genome=None, state=None):
        if not self.outcomes: return None
        rng = getattr(state, "rng", random)
        if not genome: return rng.choice(self.outcomes)

        # AI Decision Logic: Calculate 'Desirability' of each outcome for this specific genome
        weights = []
        scoring_cfg = config.get("outcome_scoring", {})
        attack_bonus, defense_bonus = _inventory_combat_profile(state)
        power = _state_power(state)
        for o in self.outcomes:
            # 1. Mechanical Utility
            # Positive health/coins are weighted by success_bias
            # Negative health/coins are penalized by success_bias but tolerated by threat_prob
            mechanical_utility = (o.health_change * 2.0) + (o.coin_change * 0.5)
            # High success_bias genome wants positive utility
            w = 1.0 + (mechanical_utility * (genome.success_bias * 2.0))
            
            # 2. Thematic Alignment
            # Match outcome tags against genome biases
            thematic_utility = 0
            tag_bias_map = {
                "urban": genome.bias_urban,
                "maritime": genome.bias_maritime,
                "social": genome.bias_social,
                "threat": genome.bias_threat,
                "combat": genome.bias_threat,
                "stealth": genome.bias_stealth,
                "luxury": genome.bias_luxury,
                "palace": genome.bias_luxury
            }
            
            for tag in o.tags:
                for match_tag, bias_val in tag_bias_map.items():
                    if match_tag in tag.lower():
                        thematic_utility += bias_val
            
            # Add thematic weighting
            w += thematic_utility * 1.5
            
            # 3. Risk Tolerance
            # High threat_prob genomes actually "prefer" threat outcomes (it fits their narrative)
            if "threat" in o.tags or "combat" in o.tags:
                w += genome.threat_prob * 2.0

            # 4. Discovery, Movement, and Story Progression
            if o.spawn:
                w += scoring_cfg.get("spawn_bonus", 1.5) * (0.5 + genome.spawn_prob)
            if o.reveal_location or o.move_to or o.move_to_tags:
                w += scoring_cfg.get("movement_bonus", 1.5) * (0.5 + genome.voyage_bias)
            if o.reveal_npc or o.reveal_object:
                w += scoring_cfg.get("discovery_bonus", 1.25) * (0.5 + genome.discovery_bias)
            if o.lead_to_known:
                w += scoring_cfg.get("lead_bonus", 1.25) * (0.5 + genome.lead_bias)

            # 5. Recovery and payoff weighting
            if o.health_change > 0 or o.coin_change > 0:
                w += scoring_cfg.get("recovery_bonus", 1.0) * (0.5 + genome.recovery_bias)
            if any(tag.startswith("setup_") for tag in o.tags):
                w += scoring_cfg.get("setup_bonus", 1.0) * (0.5 + genome.setup_prob)
            if any(tag.startswith("payoff_") for tag in o.tags) or "success" in o.tags:
                w += scoring_cfg.get("payoff_bonus", 1.0) * (0.5 + genome.success_bias)

            # 6. Inventory-informed combat weighting
            lower_tags = {tag.lower() for tag in o.tags}
            is_combat = "combat" in lower_tags or "threat" in lower_tags
            if is_combat:
                combat_cfg = config.get("combat_resolution", {})
                if "success" in lower_tags:
                    w += attack_bonus * combat_cfg.get("attack_outcome_weight", 0.6)
                    w += defense_bonus * combat_cfg.get("defense_outcome_weight", 0.25)
                    w += power * combat_cfg.get("power_outcome_weight", 0.4)
                if "failure" in lower_tags or "threat" in lower_tags:
                    w -= attack_bonus * combat_cfg.get("attack_lose_weight", 0.35)
                    w -= defense_bonus * combat_cfg.get("defense_lose_weight", 0.5)
                    w -= power * combat_cfg.get("power_lose_weight", 0.25)

            weights.append(max(0.1, w))

        return rng.choices(self.outcomes, weights=weights, k=1)[0]
