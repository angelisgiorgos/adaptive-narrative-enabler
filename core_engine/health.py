from utils.config_loader import config

# ============================
# PLAYER HEALTH & POWER
# ============================


class HealthPlayer:
    """Health and power of the player, always kept within their bounds.

    Health is an integer between 0 and ``max_health``: healing never raises it
    above the maximum and damage never takes it below 0. Power is derived from
    the health ratio and the combat bonuses of the inventory, and is clamped to
    ``power_model.min_power`` .. ``power_model.max_power``.

    Settings come from ``game_state`` (initial_health, max_health,
    low_health_threshold), ``combat_resolution`` (damage mitigation) and
    ``power_model``; explicit arguments override them.
    """

    def __init__(self, initial=None, maximum=None, low_threshold=None):
        self.max_health = int(config.get("game_state.max_health", 10) if maximum is None else maximum)
        if self.max_health < 1:
            raise ValueError(f"Maximum health must be at least 1, not {self.max_health}.")
        start = config.get("game_state.initial_health", 8) if initial is None else initial
        self.initial_health = min(self.max_health, max(0, int(start)))
        self.low_threshold = int(
            config.get("game_state.low_health_threshold", 4) if low_threshold is None else low_threshold
        )
        self.health = self.initial_health
        self.power = 0.0

        # Statistics over the whole game.
        self.damage_taken = 0
        self.healing_received = 0
        self.healing_wasted = 0  # healing that would have gone above max_health
        self.lowest_health = self.health
        self.highest_health = self.health

    # ---- state -------------------------------------------------------------

    @property
    def is_alive(self):
        return self.health > 0

    @property
    def is_dead(self):
        return self.health <= 0

    @property
    def is_low(self):
        return self.health <= self.low_threshold

    @property
    def is_full(self):
        return self.health >= self.max_health

    @property
    def missing(self):
        """Health that healing can still restore."""
        return self.max_health - self.health

    @property
    def ratio(self):
        """0.0 (dead) .. 1.0 (full health)."""
        return self.health / self.max_health

    # ---- changes -----------------------------------------------------------

    def clamp(self, value):
        return min(self.max_health, max(0, int(value)))

    def effective_change(self, delta):
        """The change a delta would really make, after clamping (no side effects)."""
        return self.clamp(self.health + delta) - self.health

    def change(self, delta):
        """Apply a health change (positive heals, negative damages); return the applied change."""
        delta = int(delta)
        applied = self.effective_change(delta)
        if delta > 0:
            self.healing_received += applied
            self.healing_wasted += delta - applied
        elif delta < 0:
            self.damage_taken += -applied
        self.health += applied
        self.lowest_health = min(self.lowest_health, self.health)
        self.highest_health = max(self.highest_health, self.health)
        return applied

    def heal(self, amount):
        """Restore health, never above max_health; return the health actually restored."""
        return self.change(max(0, int(amount)))

    def damage(self, amount):
        """Lose health, never below 0; return the health actually lost (positive)."""
        return -self.change(-max(0, int(amount)))

    def set(self, value):
        """Set health directly (clamped); return the applied change."""
        return self.change(self.clamp(value) - self.health)

    def restore_full(self):
        return self.heal(self.missing)

    # ---- combat ------------------------------------------------------------

    @staticmethod
    def mitigated_combat_change(delta, attack_bonus, defense_bonus):
        """Reduce combat damage by the inventory's attack and defense bonuses.

        Only negative changes are mitigated, and mitigation never turns damage
        into healing.
        """
        if delta >= 0:
            return delta
        combat_cfg = config.get("combat_resolution", {})
        mitigation = int(round(
            attack_bonus * combat_cfg.get("attack_damage_mitigation", 0.15)
            + defense_bonus * combat_cfg.get("defense_damage_mitigation", 0.5)
        ))
        return min(0, delta + mitigation)

    @staticmethod
    def failure_penalty(defense_bonus, dangerous):
        """Health lost when an action fails; armour softens failures in fights or against guards."""
        penalty = 1
        if dangerous:
            mitigation = config.get("combat_resolution.failure_defense_mitigation", 0.5)
            penalty = max(0, penalty - int(defense_bonus * mitigation))
        return penalty

    # ---- power -------------------------------------------------------------

    def calculate_power(self, attack_bonus=0.0, defense_bonus=0.0):
        """Recompute power from health and the inventory's combat bonuses, clamped to its bounds."""
        power_cfg = config.get("power_model", {})
        power = power_cfg.get("base_power", 0.0)
        power += self.ratio * power_cfg.get("health_weight", 1.5)
        power += attack_bonus * power_cfg.get("attack_weight", 1.0)
        power += defense_bonus * power_cfg.get("defense_weight", 0.8)
        if self.is_low:
            power -= power_cfg.get("low_health_penalty", 0.5)
        self.power = min(power_cfg.get("max_power", 5.0), max(power_cfg.get("min_power", 0.0), power))
        return self.power

    # ---- reporting ---------------------------------------------------------

    def status(self):
        return f"{self.health}/{self.max_health}"

    def summary(self):
        return {
            "health": self.health,
            "max_health": self.max_health,
            "power": round(self.power, 3),
            "damage_taken": self.damage_taken,
            "healing_received": self.healing_received,
            "healing_wasted": self.healing_wasted,
            "lowest_health": self.lowest_health,
            "highest_health": self.highest_health,
        }

    def __repr__(self):
        return f"HealthPlayer(health={self.status()}, power={self.power:.2f})"
