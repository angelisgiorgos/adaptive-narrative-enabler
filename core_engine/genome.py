import random
from utils.config_loader import config

# ============================
# GENOME (evolvable parameters)
# ============================

class Genome:
    def __init__(self):
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

    def mutate(self):
        mutation_rate = config.get("evolution.mutation_rate", config.get("simulation.mutation_rate", 0.2))
        mutation_step = config.get("evolution.mutation_step", 0.1)
        for attr in vars(self):
            if random.random() < mutation_rate:
                val = getattr(self, attr)
                # Ensure we only mutate numerical types
                if isinstance(val, (int, float)):
                    val += random.uniform(-mutation_step, mutation_step)
                    val = max(0, min(1, val))
                    setattr(self, attr, val)
