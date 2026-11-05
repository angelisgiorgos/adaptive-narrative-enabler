import random
from utils.config_loader import config

# ============================
# GENOME (evolvable parameters)
# ============================

# Every evolvable trait with its default bounds (overridden by genome_bounds.<trait>).
# The order is the order in which a random genome draws its values.
TRAIT_BOUNDS = {
    "threat_prob": [0.1, 0.5],
    "spawn_prob": [0.1, 0.5],
    "setup_prob": [0.05, 0.3],
    "success_bias": [0.5, 0.9],
    "guard_bias": [0.1, 0.4],
    "discovery_bias": [0.2, 0.8],
    "recovery_bias": [0.2, 0.8],
    "lead_bias": [0.2, 0.8],
    "event_prob": [0.08, 0.35],
    "chaos_bias": [0.1, 0.7],
    # NPC tag biases (evolvable preferences for character types)
    "bias_urban": [0.1, 0.9],
    "bias_maritime": [0.1, 0.9],
    "bias_social": [0.1, 0.9],
    "bias_threat": [0.1, 0.9],
    "bias_stealth": [0.1, 0.9],
    "bias_luxury": [0.1, 0.9],
    # Preference for moving to new locations vs staying
    "voyage_bias": [0.1, 0.5],
    # How strongly the story pursues its mission goal
    "goal_bias": [0.3, 1.0],
    # How strongly the story avoids repeating choices and outcomes
    "novelty_bias": [0.2, 0.9],
}


def trait_bounds(name):
    return config.get(f"genome_bounds.{name}", TRAIT_BOUNDS[name])


class Genome:
    def __init__(self, rng=None):
        """A random genome within the configured bounds; ``rng`` makes it reproducible."""
        rng = rng or random
        for name in TRAIT_BOUNDS:
            setattr(self, name, rng.uniform(*trait_bounds(name)))

    @staticmethod
    def added_field_defaults():
        """Parameters added after bundles were first exported.

        Bundles saved before a parameter existed load with these values: the
        midpoint of the parameter's configured bounds.
        """
        return {name: sum(trait_bounds(name)) / 2 for name in ("goal_bias", "novelty_bias")}

    def traits(self):
        return {name: float(value) for name, value in vars(self).items() if isinstance(value, (int, float))}

    @classmethod
    def from_traits(cls, traits, rng=None):
        genome = cls(rng)
        for name, value in traits.items():
            if name in TRAIT_BOUNDS:
                setattr(genome, name, float(value))
        return genome

    @classmethod
    def crossover(cls, first, second, rng=None):
        """Blend two genomes while retaining occasional parental extremes."""
        rng = rng or random
        child = cls(rng)
        blend_chance = config.get("evolution.blend_crossover_chance", 0.55)
        for attr, first_value in vars(first).items():
            second_value = getattr(second, attr, first_value)
            if not isinstance(first_value, (int, float)):
                setattr(child, attr, first_value)
            elif rng.random() < blend_chance:
                alpha = rng.random()
                setattr(child, attr, first_value * alpha + second_value * (1.0 - alpha))
            else:
                setattr(child, attr, rng.choice([first_value, second_value]))
        return child

    def mutate(self, rng=None, rate=None, step=None):
        rng = rng or random
        if rate is None:
            rate = config.get("evolution.mutation_rate", config.get("simulation.mutation_rate", 0.2))
        if step is None:
            step = config.get("evolution.mutation_step", 0.1)
        for attr in vars(self):
            if rng.random() < rate:
                val = getattr(self, attr)
                # Ensure we only mutate numerical types
                if isinstance(val, (int, float)):
                    val += rng.uniform(-step, step)
                    val = max(0, min(1, val))
                    setattr(self, attr, val)
