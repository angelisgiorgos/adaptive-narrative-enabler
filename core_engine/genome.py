import random
from utils.config_loader import config

# ============================
# GENOME (evolvable parameters)
# ============================

class Genome:
    def __init__(self, values=None):
        self.threat_prob = random.uniform(*config.get("genome_bounds.threat_prob", [0.1, 0.5]))
        self.spawn_prob = random.uniform(*config.get("genome_bounds.spawn_prob", [0.1, 0.5]))
        self.setup_prob = random.uniform(*config.get("genome_bounds.setup_prob", [0.05, 0.3]))
        self.success_bias = random.uniform(*config.get("genome_bounds.success_bias", [0.5, 0.9]))
        self.guard_bias = random.uniform(*config.get("genome_bounds.guard_bias", [0.1, 0.4]))
        self.discovery_bias = random.uniform(*config.get("genome_bounds.discovery_bias", [0.2, 0.8]))
        self.recovery_bias = random.uniform(*config.get("genome_bounds.recovery_bias", [0.2, 0.8]))
        self.lead_bias = random.uniform(*config.get("genome_bounds.lead_bias", [0.2, 0.8]))
        
        # NPC Tag Biases (Evolvable preferences for character types)
        self.bias_urban = random.uniform(0.1, 0.9)
        self.bias_maritime = random.uniform(0.1, 0.9)
        self.bias_social = random.uniform(0.1, 0.9)
        self.bias_threat = random.uniform(0.1, 0.9)
        self.bias_stealth = random.uniform(0.1, 0.9)
        self.bias_luxury = random.uniform(0.1, 0.9)
        
        # Voyage Bias (Preference for moving to new locations vs staying)
        self.voyage_bias = random.uniform(*config.get("genome_bounds.voyage_bias", [0.1, 0.5]))
        self.extension_bias = random.uniform(
            *config.get("genome_bounds.extension_bias", [0.35, 0.9])
        )
        self.tag_coherence = random.uniform(
            *config.get("genome_bounds.tag_coherence", [0.25, 0.8])
        )
        self.tag_novelty = random.uniform(
            *config.get("genome_bounds.tag_novelty", [0.25, 0.8])
        )

        # This mapping makes the genome extensible: adding a tag to configuration
        # creates an evolvable preference without adding another Python attribute.
        self.tag_biases = {
            str(tag).lower(): random.uniform(0.1, 0.9)
            for tag in config.get("world_evolution.evolvable_tags", [])
        }

        if values:
            for name, value in values.items():
                if name == "tag_biases" and isinstance(value, dict):
                    self.tag_biases.update({
                        str(tag).lower(): float(bias)
                        for tag, bias in value.items()
                        if isinstance(bias, (int, float))
                    })
                elif hasattr(self, name) and isinstance(value, (int, float)):
                    setattr(self, name, float(value))

    @classmethod
    def from_dict(cls, values):
        """Restore a genome saved in a run artifact."""
        if not isinstance(values, dict):
            raise ValueError("Saved genome data must be a mapping.")
        return cls(values=values)

    def to_dict(self):
        result = {}
        for name, value in vars(self).items():
            if isinstance(value, dict):
                result[name] = {
                    str(key): float(item)
                    for key, item in value.items()
                    if isinstance(item, (int, float))
                }
            elif isinstance(value, (int, float)):
                result[name] = float(value)
        return result

    def tag_preference(self, tag):
        """Return the evolvable preference for any narrative tag."""
        normalized = str(tag).lower()
        if normalized in self.tag_biases:
            return self.tag_biases[normalized]

        legacy = {
            "urban": self.bias_urban,
            "maritime": self.bias_maritime,
            "social": self.bias_social,
            "threat": self.bias_threat,
            "combat": self.bias_threat,
            "stealth": self.bias_stealth,
            "luxury": self.bias_luxury,
            "palace": self.bias_luxury,
        }
        return legacy.get(normalized, 0.5)

    def mutate(self):
        mutation_rate = config.get("evolution.mutation_rate", config.get("simulation.mutation_rate", 0.2))
        mutation_step = config.get("evolution.mutation_step", 0.1)
        for attr in vars(self):
            if attr == "tag_biases":
                for tag, bias in list(self.tag_biases.items()):
                    if random.random() < mutation_rate:
                        updated = bias + random.uniform(-mutation_step, mutation_step)
                        self.tag_biases[tag] = max(0, min(1, updated))
                continue
            if random.random() < mutation_rate:
                val = getattr(self, attr)
                # Ensure we only mutate numerical types
                if isinstance(val, (int, float)):
                    val += random.uniform(-mutation_step, mutation_step)
                    val = max(0, min(1, val))
                    setattr(self, attr, val)
