"""The evolutionary AI behind every story the tool creates.

A population of genomes (core_engine.genome: how a storyteller weighs threat,
discovery, goals, novelty, ...) plays stories; the fitness function scores how
engaging each story is; the best storytellers are kept, crossed and mutated,
and the loop repeats. The same engine drives the terminal (main.py), the
studio and the REST API.

Learning features:
- Every genome plays ``episodes_per_genome`` stories per generation, and all
  genomes of a generation play the *same* story seeds, so they are compared
  fairly (a better score means a better storyteller, not a luckier story).
- Tournament selection, elitism, crossover, mutation and random immigrants.
- A fixed validation benchmark (``validation_episodes`` story seeds, the same
  in every generation) scores each generation's champion. The overall best
  storyteller, adaptive mutation and early stopping follow this comparable
  score, so a lucky generation cannot fool the search.
- Adaptive mutation: when the benchmark score stops improving for
  ``stagnation_generations``, mutation widens (up to ``max_mutation_step``)
  to escape the plateau; it narrows again after an improvement.
- Early stopping after ``early_stop_generations`` without improvement.
- A learning memory (hall of fame) of the best genomes of earlier runs, each
  with its benchmark score, seeds the next run, so the tool keeps learning
  across runs (``warm_start``).
"""
import datetime
import json
import os
import random
import statistics
import threading
from dataclasses import dataclass, field

from utils.config_loader import config

from .game_state import GameState
from .genome import TRAIT_BOUNDS, Genome, trait_bounds

TRAIT_MEANINGS = {
    "threat_prob": "appetite for danger and combat",
    "spawn_prob": "eagerness to open new places",
    "setup_prob": "planting clues that pay off later",
    "success_bias": "leaning toward outcomes that succeed",
    "guard_bias": "willingness to deal with guards",
    "discovery_bias": "curiosity: revealing people, items and paths",
    "recovery_bias": "resting and healing when hurt",
    "lead_bias": "following NPC leads",
    "event_prob": "welcoming unexpected events and triggered situations",
    "chaos_bias": "tolerance for surprising twists",
    "bias_urban": "preference for city scenes",
    "bias_maritime": "preference for harbour and sea scenes",
    "bias_social": "preference for social characters",
    "bias_threat": "preference for threatening characters",
    "bias_stealth": "preference for stealthy approaches",
    "bias_luxury": "preference for palaces and riches",
    "voyage_bias": "wanderlust: moving on instead of staying",
    "goal_bias": "drive toward the mission in the climax",
    "novelty_bias": "avoiding repetition",
}

DEFAULTS = {
    "pop_size": 20,
    "generations": 10,
    "episodes_per_genome": 3,
    "max_steps": 60,
    "elitism_rate": 0.08,
    "crossover_rate": 0.75,
    "random_immigrant_rate": 0.08,
    "tournament_size": 3,
    "mutation_rate": 0.22,
    "mutation_step": 0.14,
    "max_mutation_step": 0.4,
    "stagnation_generations": 3,
    "early_stop_generations": 0,
    "warm_start": True,
    "hall_of_fame_size": 12,
    "validation_episodes": 10,
}


def evolution_settings(overrides=None):
    """Evolution settings: DEFAULTS < config ``evolution`` < ``overrides``."""
    settings = dict(DEFAULTS)
    settings.update({k: v for k, v in (config.get("evolution") or {}).items() if k in DEFAULTS})
    settings.update({k: v for k, v in (overrides or {}).items() if v is not None})
    return settings


# ---- evaluation -------------------------------------------------------------------

def play_episode(build_world, genome, seed, max_steps):
    """One story played by ``genome`` with the story seed ``seed``."""
    state = GameState(build_world(genome), genome, rng=random.Random(seed))
    for _ in range(max_steps):
        if state.health <= 0 or not state.step():
            break
    return state


def evaluate_task(task):
    """(build_world, fitness, traits, seeds, max_steps) -> mean fitness.

    A module-level function so a process pool can run it."""
    build_world, fitness, traits, seeds, max_steps = task
    genome = Genome.from_traits(traits)
    scores = [fitness(play_episode(build_world, genome, seed, max_steps)) for seed in seeds]
    return statistics.mean(scores) if scores else float("-inf")


# ---- results -------------------------------------------------------------------------

@dataclass
class GenerationStats:
    generation: int
    best: float
    mean: float
    worst: float
    spread: float
    diversity: float
    mutation_step: float
    improved: bool
    best_traits: dict
    validation: float = 0.0  # the champion's score on the fixed benchmark


@dataclass
class EvolutionResult:
    best_genome: Genome
    best_score: float
    best_state: object
    history: list
    final_population: list = field(default_factory=list)
    generations_run: int = 0
    early_stopped: bool = False
    warm_started: int = 0

    def learned_traits(self):
        """What the evolved storyteller learned, trait by trait, in plain words."""
        rows = []
        population = [g.traits() for g in self.final_population] or [self.best_genome.traits()]
        for name, value in self.best_genome.traits().items():
            low, high = trait_bounds(name) if name in TRAIT_BOUNDS else (0.0, 1.0)
            mean = statistics.mean(t.get(name, value) for t in population)
            position = (value - low) / (high - low) if high > low else 0.5
            rows.append({
                "trait": name,
                "meaning": TRAIT_MEANINGS.get(name, ""),
                "value": round(value, 3),
                "population_mean": round(mean, 3),
                "bounds": [low, high],
                "tendency": "high" if position >= 0.66 else "low" if position <= 0.33 else "moderate",
            })
        return rows

    def summary(self):
        first = self.history[0] if self.history else None
        return {
            "best_score": round(self.best_score, 3),
            "generations_run": self.generations_run,
            "early_stopped": self.early_stopped,
            "warm_started_from_memory": self.warm_started,
            "improvement": round(self.best_score - first.validation, 3) if first else 0.0,
            "history": [vars(stats) for stats in self.history],
            "best_genome": self.best_genome.traits(),
            "learned_traits": self.learned_traits(),
        }


# ---- learning memory -------------------------------------------------------------------

class LearningMemory:
    """Hall of fame of the best genomes across runs (``evolution_memory.json``)."""

    _lock = threading.Lock()

    def __init__(self, path=None):
        base = os.environ.get("ANE_SAVE_DIR") or os.path.join(os.path.dirname(os.path.dirname(__file__)), "saves")
        self.path = path or os.path.join(base, "evolution_memory.json")

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                entries = json.load(stream).get("hall_of_fame", [])
            return [e for e in entries if isinstance(e, dict) and isinstance(e.get("traits"), dict)]
        except (OSError, ValueError, AttributeError):
            return []

    def genomes(self, limit=None):
        entries = sorted(self.load(), key=lambda e: e.get("score", float("-inf")), reverse=True)
        return [Genome.from_traits(e["traits"]) for e in entries[:limit]]

    def remember(self, scored, size):
        """Merge (score, genome) pairs into the hall of fame, keeping the ``size`` best."""
        with self._lock:
            now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            entries = self.load() + [
                {"score": float(score), "traits": genome.traits(), "remembered": now} for score, genome in scored
            ]
            unique = {}
            for entry in entries:
                key = tuple(round(entry["traits"].get(name, 0), 4) for name in sorted(entry["traits"]))
                if key not in unique or entry["score"] > unique[key]["score"]:
                    unique[key] = entry
            best = sorted(unique.values(), key=lambda e: e["score"], reverse=True)[:size]
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            temporary = f"{self.path}.tmp"
            with open(temporary, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "hall_of_fame": best}, stream, indent=2)
            os.replace(temporary, self.path)
            return best

    def forget(self):
        with self._lock:
            if os.path.exists(self.path):
                os.remove(self.path)


# ---- the evolutionary loop -------------------------------------------------------------

def _diversity(population):
    traits = [g.traits() for g in population]
    if len(traits) < 2:
        return 0.0
    return statistics.mean(statistics.pstdev(t[name] for t in traits) for name in traits[0])


class NarrativeEvolution:
    """Evolves storytellers for the current world configuration."""

    def __init__(self, build_world, fitness, settings=None, seed=None, memory=None, map_fn=None):
        """``build_world(genome)`` and ``fitness(state)`` come from main.py;
        ``map_fn(function, tasks)`` can evaluate in parallel (default: in turn);
        ``memory`` (a LearningMemory) enables learning across runs."""
        self.build_world = build_world
        self.fitness = fitness
        self.settings = evolution_settings(settings)
        self.seed = seed
        self.rng = random.Random(seed)
        self.memory = memory
        self.map_fn = map_fn or (lambda function, tasks: list(map(function, tasks)))

    def _story_seeds(self, generation):
        """The shared story seeds of one generation."""
        episodes = max(1, int(self.settings["episodes_per_genome"]))
        if self.seed is not None:
            return [self.seed + generation * 1000 + episode for episode in range(episodes)]
        return [self.rng.randrange(2 ** 31) for _ in range(episodes)]

    def _validation_seeds(self):
        """Story seeds of the benchmark, the same in every generation."""
        episodes = max(1, int(self.settings["validation_episodes"]))
        base = self.seed if self.seed is not None else self.rng.randrange(2 ** 31)
        return [base + 10 ** 6 + episode for episode in range(episodes)]

    def _initial_population(self):
        size = max(1, int(self.settings["pop_size"]))
        remembered = []
        if self.memory is not None and self.settings["warm_start"]:
            remembered = self.memory.genomes(limit=max(1, size // 4))
        return remembered + [Genome(self.rng) for _ in range(size - len(remembered))], len(remembered)

    def _tournament(self, scored):
        size = max(1, min(int(self.settings["tournament_size"]), len(scored)))
        return max(self.rng.sample(scored, size), key=lambda pair: pair[0])[1]

    def _next_population(self, scored, mutation_step):
        size = len(scored)
        elites = max(1, int(round(size * float(self.settings["elitism_rate"]))))
        immigrants = int(size * float(self.settings["random_immigrant_rate"]))
        population = [Genome.from_traits(g.traits()) for _, g in scored[:elites]]
        while len(population) < size - immigrants:
            parent = self._tournament(scored)
            if size > 1 and self.rng.random() < float(self.settings["crossover_rate"]):
                child = Genome.crossover(parent, self._tournament(scored), rng=self.rng)
            else:
                child = Genome.from_traits(parent.traits())
            child.mutate(rng=self.rng, rate=float(self.settings["mutation_rate"]), step=mutation_step)
            population.append(child)
        population += [Genome(self.rng) for _ in range(size - len(population))]
        return population

    def run(self, progress=None, stop_event=None):
        """Evolve; ``progress(generation, total, GenerationStats)`` reports each generation."""
        generations = max(1, int(self.settings["generations"]))
        max_steps = max(1, int(self.settings["max_steps"]))
        base_step = float(self.settings["mutation_step"])
        mutation_step = base_step
        population, warm_started = self._initial_population()
        validation_seeds = self._validation_seeds()
        history, best = [], None
        champions = []  # (benchmark score, genome) of every generation's champion
        stale = 0
        early_stopped = False
        scored = []

        for generation in range(generations):
            seeds = self._story_seeds(generation)
            tasks = [(self.build_world, self.fitness, genome.traits(), seeds, max_steps) for genome in population]
            scores = self.map_fn(evaluate_task, tasks)
            scored = sorted(zip(scores, population), key=lambda pair: pair[0], reverse=True)

            # The champion of this generation on the fixed benchmark.
            champion = Genome.from_traits(scored[0][1].traits())
            validation = self.map_fn(
                evaluate_task, [(self.build_world, self.fitness, champion.traits(), validation_seeds, max_steps)]
            )[0]
            champions.append((validation, champion))
            improved = best is None or validation > best[0]
            if improved:
                best = (validation, champion, validation_seeds)
                stale, mutation_step = 0, base_step
            else:
                stale += 1
                if stale >= int(self.settings["stagnation_generations"]):
                    mutation_step = min(float(self.settings["max_mutation_step"]), mutation_step * 1.5)

            values = [score for score, _ in scored]
            stats = GenerationStats(
                generation=generation,
                best=round(values[0], 3),
                mean=round(statistics.mean(values), 3),
                worst=round(values[-1], 3),
                spread=round(statistics.pstdev(values), 3),
                diversity=round(_diversity(population), 4),
                mutation_step=round(mutation_step, 4),
                improved=improved,
                best_traits=scored[0][1].traits(),
                validation=round(validation, 3),
            )
            history.append(stats)
            if progress is not None:
                progress(generation, generations, stats)

            early = int(self.settings["early_stop_generations"])
            if (stop_event is not None and stop_event.is_set()) or (early and stale >= early):
                early_stopped = early and stale >= early
                break
            if generation < generations - 1:
                population = self._next_population(scored, mutation_step)

        best_score, best_genome, best_seeds = best
        best_state = max(
            (play_episode(self.build_world, best_genome, seed, max_steps) for seed in best_seeds),
            key=self.fitness,
        )
        if self.memory is not None:
            # Only benchmark scores are remembered, so remembered storytellers are comparable.
            self.memory.remember(champions, int(self.settings["hall_of_fame_size"]))
        return EvolutionResult(
            best_genome=best_genome,
            best_score=best_score,
            best_state=best_state,
            history=history,
            final_population=[genome for _, genome in scored],
            generations_run=len(history),
            early_stopped=bool(early_stopped),
            warm_started=warm_started,
        )
