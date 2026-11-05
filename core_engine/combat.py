"""Skill checks and combat: every roll of a story is resolved here.

A skill check rolls 1..``roll_sides`` (default 100). It passes when the roll is
at or below the target: the stat's value (with item bonuses), plus the
action's modifier, minus the story's difficulty. Difficulty settings live in
``skill_check_resolution`` (hyperparameters.yaml, "Skill check resolution"
on the Weights tab):

    difficulty_penalty          added to every check (positive = harder)
    difficulty_rate_per_step    extra difficulty per story step (negative = easier over time)
    difficulty_rate_start_step  step from which the change over time applies
    min_chance / max_chance     the target never goes below / above these
    damage_mitigation_per_point value points of the damage-reduction stat per 1 damage prevented
    failure_mitigation_threshold  that stat's value at which a failed dangerous action costs no health

Which checks apply: an action's ``skill_check: {stat, modifier}``; otherwise
the first outcome tag mapped in ``character_stats.tag_checks`` (e.g.
``combat: attack``). Rolls use the story's seeded random generator, so a
seeded simulation is reproducible.
"""
import random
from dataclasses import dataclass

from utils.config_loader import config


@dataclass
class CheckResult:
    stat: str
    roll: int
    target: int
    passed: bool
    step: int


class CombatSystem:
    """Resolves the skill checks and fights of one playthrough."""

    def __init__(self, stats, world=None, rng=None):
        self.stats = stats
        self.world = world
        self.rng = rng or random
        self.history = []  # every CheckResult of the story

    @staticmethod
    def _cfg():
        return config.get("skill_check_resolution") or {}

    # ---- stat values ---------------------------------------------------------

    def item_bonuses(self, inventory):
        """Stat bonuses of the items the player is holding."""
        bonuses = {}
        for obj in getattr(self.world, "objects", []) or []:
            if obj.name in inventory:
                for stat, bonus in (getattr(obj, "stat_bonus", None) or {}).items():
                    bonuses[stat] = bonuses.get(stat, 0) + bonus
        return bonuses

    def value(self, stat, inventory=()):
        return self.stats.value(stat, self.item_bonuses(inventory).get(stat, 0))

    # ---- checks ----------------------------------------------------------------

    def check_for(self, action, outcome=None):
        """(stat, modifier) of the check an action/outcome requires, or None."""
        check = getattr(action, "skill_check", None)
        if check and check.get("stat") in self.stats.names:
            return check["stat"], int(check.get("modifier", 0))
        if outcome is not None:
            tag_checks = self.stats.settings.get("tag_checks") or {}
            for tag in outcome.tags:
                stat = tag_checks.get(str(tag).lower())
                if stat in self.stats.names:
                    return stat, 0
        return None

    def difficulty(self, step):
        cfg = self._cfg()
        difficulty = float(cfg.get("difficulty_penalty", 0))
        start = int(cfg.get("difficulty_rate_start_step", 0))
        difficulty += max(0, step - start) * float(cfg.get("difficulty_rate_per_step", 0.0))
        return difficulty

    def target(self, stat, modifier=0, inventory=(), step=0):
        """The highest roll that still passes."""
        cfg = self._cfg()
        sides = int(cfg.get("roll_sides", 100))
        raw = self.value(stat, inventory) + modifier - self.difficulty(step)
        low = float(cfg.get("min_chance", 5)) / 100 * sides
        high = float(cfg.get("max_chance", 95)) / 100 * sides
        return int(round(min(high, max(low, raw))))

    def pass_chance(self, stat, modifier=0, inventory=(), step=0):
        return self.target(stat, modifier, inventory, step) / int(self._cfg().get("roll_sides", 100))

    def roll(self, stat, modifier=0, inventory=(), step=0):
        sides = int(self._cfg().get("roll_sides", 100))
        target = self.target(stat, modifier, inventory, step)
        rolled = self.rng.randint(1, sides)
        result = CheckResult(stat=stat, roll=rolled, target=target, passed=rolled <= target, step=step)
        self.history.append(result)
        return result

    # ---- combat consequences -----------------------------------------------------

    def _reduction_value(self, inventory):
        stat = self.stats.settings.get("damage_reduction_stat")
        return self.value(stat, inventory) if stat in self.stats.names else 0

    def mitigate(self, delta, inventory=()):
        """Soften combat damage with the damage-reduction stat (never into healing)."""
        if delta >= 0:
            return delta
        per_point = float(self._cfg().get("damage_mitigation_per_point", 25))
        prevented = int(self._reduction_value(inventory) // per_point) if per_point > 0 else 0
        return min(0, delta + prevented)

    def failure_penalty(self, dangerous, inventory=()):
        """Health lost when an action fails; a strong defence shrugs off dangerous failures."""
        if dangerous and self._reduction_value(inventory) >= float(self._cfg().get("failure_mitigation_threshold", 80)):
            return 0
        return 1
