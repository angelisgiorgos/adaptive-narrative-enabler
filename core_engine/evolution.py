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
  in every generation; with ``cover_situations`` at least one per start x
  mission situation) scores the ``benchmark_top_k`` best of each generation;
  the best of them on the benchmark is the generation's champion and is kept.
  The overall best storyteller, adaptive mutation and early stopping follow
  this comparable score, so a lucky generation cannot fool the search.
- Diversity: when the population's diversity falls below ``min_diversity``,
  more random immigrants (up to ``max_immigrant_rate``) and a wider mutation
  keep it from collapsing, and children identical to a parent are mutated again.
- Adaptive mutation: when the benchmark score stops improving for
  ``stagnation_generations``, mutation widens (up to ``max_mutation_step``)
  to escape the plateau; it narrows again after an improvement.
- Early stopping after ``early_stop_generations`` without improvement.
- A learning memory (hall of fame) of the best genomes of earlier runs, each
  with its benchmark score, seeds the next run, so the tool keeps learning
  across runs (``warm_start``).
- Judge in the loop (``judge_weight`` > 0, off by default): one story of each of
  the ``judge_top_k`` best of a generation is read by the LLM judge
  (evaluator/game_judge.py) and ``judge_weight`` x (engagement - 5) is added to
  their scores before breeding, so what a reader finds engaging steers the
  search. Without a model the run goes on with the fitness alone.
"""
import datetime
import json
import logging
import os
import random
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Optional

from utils.config_loader import config

from .game_state import GameState
from .genome import TRAIT_BOUNDS, Genome, trait_bounds

LOGGER = logging.getLogger(__name__)

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
    "cover_situations": False,      # the benchmark has at least one story per start x mission situation
    "benchmark_top_k": 1,           # this many of each generation's best are benchmarked
    "min_diversity": 0.0,           # below this diversity: more immigrants, wider mutation
    "max_immigrant_rate": 0.3,
    # Methodology (README, "How the evolutionary AI learns"):
    "full_length_stories": True,    # play every story to its end (narrative_end.max_actions), not max_steps
    "stratify_episodes": True,      # every storyteller plays the same spread of start x mission situations
    "score_aggregate": "mean_minus_std",  # mean | mean_minus_std: penalise storytellers whose stories vary in quality
    "consistency_weight": 0.25,     # mean_minus_std: score = mean - this x standard deviation
    "alternatives_weight": 0.0,     # + this x how different a storyteller's stories are (fitness.alternatives)
    # Judge in the loop: the LLM judge reads one story of each of the judge_top_k best of a
    # generation; judge_weight x (engagement - 5) is added to their scores before breeding.
    "judge_weight": 0.0,            # 0: off (no model needed, runs stay reproducible)
    "judge_top_k": 2,
    "judge_workers": 4,             # judgements sent to the model at once
}
AGGREGATES = ("mean", "mean_minus_std")


def evolution_settings(overrides=None):
    """Evolution settings: DEFAULTS < config ``evolution`` < ``overrides``."""
    settings = dict(DEFAULTS)
    settings.update({k: v for k, v in (config.get("evolution") or {}).items() if k in DEFAULTS})
    settings.update({k: v for k, v in (overrides or {}).items() if v is not None})
    return settings


# ---- evaluation -------------------------------------------------------------------

def play_episode(build_world, genome, seed, max_steps, situation=None):
    """One story played by ``genome`` with the story seed ``seed``; ``situation``
    (start, mission) fixes where it starts and what it is about."""
    start, mission = situation or (None, None)
    state = GameState(build_world(genome), genome, rng=random.Random(seed), start=start, mission=mission)
    for _ in range(max_steps):
        if state.health <= 0 or not state.step():
            break
    return state


def evaluate_task(task):
    """(build_world, fitness, traits, seeds, max_steps[, options]) -> score.

    ``options``: ``situations`` (one (start, mission) per seed), ``aggregate``
    (mean or mean_minus_std) and ``consistency_weight``. A module-level
    function so a process pool can run it."""
    build_world, fitness, traits, seeds, max_steps = task[:5]
    options = task[5] if len(task) > 5 else {}
    situations = options.get("situations") or [None] * len(seeds)
    genome = Genome.from_traits(traits)
    states = [play_episode(build_world, genome, seed, max_steps, situation)
              for seed, situation in zip(seeds, situations)]
    scores = [fitness(state) for state in states]
    if not scores:
        return float("-inf")
    score = statistics.mean(scores)
    if options.get("aggregate") == "mean_minus_std" and len(scores) > 1:
        score -= float(options.get("consistency_weight", 0.25)) * statistics.pstdev(scores)
    # Telling different stories is part of the engine's own fitness (fitness.rewards_alternatives),
    # not of any fitness a caller passes in.
    weight = float(options.get("alternatives_weight", 0.0) or 0.0)
    if weight and len(states) > 1 and getattr(fitness, "rewards_alternatives", False):
        from .fitness import alternatives

        score += weight * alternatives(states)  # telling different stories, not one story again
    return score


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
    judge_engagement: Optional[float] = None  # judge in the loop: mean engagement of the judged stories


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

def merge_hall_of_fame(entries, scored, size):
    """Hall-of-fame entries plus (score, genome) pairs: the ``size`` best, one per genome."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    entries = list(entries) + [
        {"score": float(score), "traits": genome.traits(), "remembered": now} for score, genome in scored
    ]
    unique = {}
    for entry in entries:
        key = tuple(round(entry["traits"].get(name, 0), 4) for name in sorted(entry["traits"]))
        if key not in unique or entry["score"] > unique[key]["score"]:
            unique[key] = entry
    return sorted(unique.values(), key=lambda e: e["score"], reverse=True)[:size]


class ModelMemory:
    """A hall of fame kept with a saved engine model instead of a file (same interface
    as LearningMemory), so each engine continues from its own memory."""

    def __init__(self, entries=None):
        self.entries = [dict(e) for e in entries or [] if isinstance(e, dict) and isinstance(e.get("traits"), dict)]
        self.path = None

    def load(self):
        return [dict(e) for e in self.entries]

    def genomes(self, limit=None):
        entries = sorted(self.entries, key=lambda e: e.get("score", float("-inf")), reverse=True)
        return [Genome.from_traits(e["traits"]) for e in entries[:limit]]

    def remember(self, scored, size):
        self.entries = merge_hall_of_fame(self.entries, scored, size)
        return self.load()

    def forget(self):
        self.entries = []


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
            best = merge_hall_of_fame(self.load(), scored, size)
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

    def __init__(self, build_world, fitness, settings=None, seed=None, memory=None, map_fn=None, judge=None):
        """``build_world(genome)`` and ``fitness(state)`` come from main.py;
        ``map_fn(function, tasks)`` can evaluate in parallel (default: in turn);
        ``memory`` (a LearningMemory) enables learning across runs; ``judge`` (with
        ``judge_digest(digest)``, default: evaluator.game_judge.GameJudge when
        judge_weight > 0) is the judge in the loop."""
        self.build_world = build_world
        self.fitness = fitness
        self.settings = evolution_settings(settings)
        self.seed = seed
        self.rng = random.Random(seed)
        self.memory = memory
        self.map_fn = map_fn or (lambda function, tasks: list(map(function, tasks)))
        self.judge = judge
        self._judge_off = False          # the judge proved unavailable: the run goes on without it
        self._judge_unreadable = False   # an unreadable judgement was already logged

    def _max_steps(self):
        """Steps per evaluated story: the whole story with full_length_stories."""
        steps = max(1, int(self.settings["max_steps"]))
        if self.settings["full_length_stories"]:
            steps = max(steps, max(100, int(config.get("narrative_end.max_actions", 100))))
        return steps

    def _situations(self):
        """(start, mission) pairs every storyteller is evaluated on: each starting
        location with each mission that can be reached from it (None: not stratified)."""
        if not self.settings["stratify_episodes"]:
            return None
        if getattr(self, "_situation_cache", None) is None:
            genome = Genome(random.Random(0))
            probe = GameState(self.build_world(genome), genome, rng=random.Random(0))
            missions = [m for m in probe.world.missions.values() if probe.is_enabled(m)] \
                if config.get("mission_settings.enabled", True) else []
            pairs = [
                (loc.name, mission.name) for loc in probe._start_candidates() for mission in missions
                if probe._mission_viable_from(mission, loc.name)
            ] or [(loc.name, None) for loc in probe._start_candidates()]
            self._situation_cache = pairs
        return self._situation_cache

    def _options(self, situations):
        return {"situations": situations, "aggregate": self.settings["score_aggregate"],
                "consistency_weight": self.settings["consistency_weight"],
                "alternatives_weight": self.settings.get("alternatives_weight", 0.0)}

    def _episode_situations(self, generation, count):
        situations = self._situations()
        if not situations:
            return None
        return [situations[(generation * count + episode) % len(situations)] for episode in range(count)]

    def benchmark_task(self, traits):
        """The validation task of a storyteller (same stories for every one compared)."""
        seeds = self._validation_seeds()
        situations = self._situations()
        situations = [situations[i % len(situations)] for i in range(len(seeds))] if situations else None
        return (self.build_world, self.fitness, traits, seeds, self._max_steps(), self._options(situations))

    def _story_seeds(self, generation):
        """The shared story seeds of one generation."""
        episodes = max(1, int(self.settings["episodes_per_genome"]))
        if self.seed is not None:
            return [self.seed + generation * 1000 + episode for episode in range(episodes)]
        return [self.rng.randrange(2 ** 31) for _ in range(episodes)]

    def _validation_seeds(self):
        """Story seeds of the benchmark, the same in every generation (drawn once
        when no seed is set, so the benchmark stays fixed)."""
        if getattr(self, "_validation_seed_cache", None) is None:
            episodes = max(1, int(self.settings["validation_episodes"]))
            if self.settings["cover_situations"]:
                episodes = max(episodes, len(self._situations() or []))
            base = self.seed if self.seed is not None else self.rng.randrange(2 ** 31)
            self._validation_seed_cache = [base + 10 ** 6 + episode for episode in range(episodes)]
        return list(self._validation_seed_cache)

    def _initial_population(self):
        size = max(1, int(self.settings["pop_size"]))
        remembered = []
        if self.memory is not None and self.settings["warm_start"]:
            remembered = self.memory.genomes(limit=max(1, size // 4))
        return remembered + [Genome(self.rng) for _ in range(size - len(remembered))], len(remembered)

    def _tournament(self, scored):
        size = max(1, min(int(self.settings["tournament_size"]), len(scored)))
        return max(self.rng.sample(scored, size), key=lambda pair: pair[0])[1]

    def _next_population(self, scored, mutation_step, diversity=None):
        size = len(scored)
        elites = max(1, int(round(size * float(self.settings["elitism_rate"]))))
        immigrant_rate = float(self.settings["random_immigrant_rate"])
        if diversity is not None and diversity < float(self.settings["min_diversity"]):
            # The population is collapsing onto one storyteller: more newcomers, wider steps.
            immigrant_rate = max(immigrant_rate, float(self.settings["max_immigrant_rate"]))
            mutation_step = min(float(self.settings["max_mutation_step"]), mutation_step * 1.5)
        immigrants = int(size * immigrant_rate) if size > elites else 0
        population = [Genome.from_traits(g.traits()) for _, g in scored[:elites]]
        seen = {self._genome_key(g) for g in population}
        while len(population) < size - immigrants:
            parent = self._tournament(scored)
            if size > 1 and self.rng.random() < float(self.settings["crossover_rate"]):
                child = Genome.crossover(parent, self._tournament(scored), rng=self.rng)
            else:
                child = Genome.from_traits(parent.traits())
            child.mutate(rng=self.rng, rate=float(self.settings["mutation_rate"]), step=mutation_step)
            if self._genome_key(child) in seen:  # a copy adds nothing: mutate it again, harder
                child.mutate(rng=self.rng, rate=1.0, step=min(float(self.settings["max_mutation_step"]), mutation_step * 2))
            seen.add(self._genome_key(child))
            population.append(child)
        population += [Genome(self.rng) for _ in range(size - len(population))]
        return population

    @staticmethod
    def _genome_key(genome):
        return tuple(round(value, 4) for _, value in sorted(genome.traits().items()))

    # ---- judge in the loop ---------------------------------------------------------

    def _judge_unavailable(self, error):
        if not self._judge_off:
            LOGGER.warning("The judge in the loop is off for this run (%s); evolution goes on with the "
                           "fitness alone.", error)
        self._judge_off = True

    def _loop_judge(self):
        """The judge in the loop, created on first use (None once it proved unavailable)."""
        if self._judge_off:
            return None
        if self.judge is None:
            try:
                from evaluator.game_judge import GameJudge

                self.judge = GameJudge()
            except Exception as error:  # no vLLM, no server, no model: never stop the evolution
                self._judge_unavailable(error)
                return None
        return self.judge

    def _judged(self, scored, seed, situation, max_steps):
        """Judge in the loop: one story (``seed``, ``situation``) of each of the judge_top_k
        best in ``scored`` is judged; judge_weight x (engagement - 5) is added to their
        scores. Returns (scored again sorted best first, mean engagement or None)."""
        weight = float(self.settings.get("judge_weight") or 0.0)
        top_k = min(max(0, int(self.settings.get("judge_top_k") or 0)), len(scored))
        if not weight or not top_k:
            return scored, None
        judge = self._loop_judge()
        if judge is None:
            return scored, None
        from evaluator.game_judge import digest_state

        digests = [digest_state(play_episode(self.build_world, genome, seed, max_steps, situation))
                   for _, genome in scored[:top_k]]
        try:
            with ThreadPoolExecutor(max_workers=max(1, min(int(self.settings.get("judge_workers") or 1), top_k))) as pool:
                judgements = list(pool.map(judge.judge_digest, digests))
        except Exception as error:  # the model went away mid-run
            self._judge_unavailable(error)
            return scored, None
        engagements = [j.get("engagement") if isinstance(j, dict) else None for j in judgements]
        engagements = [float(e) if isinstance(e, (int, float)) else None for e in engagements]
        if None in engagements and not self._judge_unreadable:
            LOGGER.warning("The judge in the loop could not be read for some stories; their scores are left as they are.")
            self._judge_unreadable = True
        rescored = [(score + weight * (engagement - 5.0) if engagement is not None else score, genome)
                    for (score, genome), engagement in zip(scored[:top_k], engagements)] + list(scored[top_k:])
        rescored.sort(key=lambda pair: pair[0], reverse=True)
        read = [e for e in engagements if e is not None]
        return rescored, (round(statistics.mean(read), 3) if read else None)

    # ---- training state ------------------------------------------------------------

    CHECKPOINT_FORMAT = "ane-evolution-checkpoint"

    def _checkpoint(self, next_generation, population, scored, mutation_step, stale, best, champions,
                    history, warm_started, early_stopped):
        """The complete training state after a generation, as plain JSON data:
        resuming from it continues exactly as an uninterrupted run would."""
        version, internal, gauss = self.rng.getstate()
        return {
            "format": self.CHECKPOINT_FORMAT,
            "version": 1,
            "settings": dict(self.settings),
            "seed": self.seed,
            "rng_state": [version, list(internal), gauss],
            "next_generation": next_generation,
            "population": [genome.traits() for genome in population],
            "scored": [[float(score), genome.traits()] for score, genome in scored],
            "mutation_step": mutation_step,
            "stale": stale,
            "best": None if best is None else {"score": best[0], "traits": best[1].traits(), "seeds": list(best[2])},
            "champions": [[float(score), genome.traits()] for score, genome in champions],
            "history": [asdict(stats) for stats in history],
            "warm_started": warm_started,
            "early_stopped": bool(early_stopped),
            "validation_seeds": self._validation_seeds(),
            "situations": [list(pair) for pair in self._situations()] if self._situations() else None,
        }

    @classmethod
    def from_checkpoint(cls, build_world, fitness, checkpoint, generations=None, memory=None, map_fn=None, judge=None):
        """An evolution set up to continue ``checkpoint``; ``generations`` more
        generations (default: the remaining ones of the original run)."""
        if not isinstance(checkpoint, dict) or checkpoint.get("format") != cls.CHECKPOINT_FORMAT:
            raise ValueError("Not an evolution checkpoint.")
        settings = dict(checkpoint["settings"])
        if generations is not None:
            settings["generations"] = int(checkpoint["next_generation"]) + max(1, int(generations))
        return cls(build_world, fitness, settings=settings, seed=checkpoint.get("seed"), memory=memory, map_fn=map_fn,
                   judge=judge)

    def run(self, progress=None, stop_event=None, resume=None, checkpoint=None):
        """Evolve; ``progress(generation, total, GenerationStats)`` reports each generation.

        ``checkpoint(state)`` receives the complete training state after every
        generation (also kept as ``last_checkpoint``). ``resume`` continues such
        a state from its next generation, exactly as an uninterrupted run would.
        """
        if self.settings["score_aggregate"] not in AGGREGATES:
            raise ValueError(f"evolution.score_aggregate must be one of {', '.join(AGGREGATES)}.")
        generations = max(1, int(self.settings["generations"]))
        max_steps = self._max_steps()
        base_step = float(self.settings["mutation_step"])
        if resume is not None:
            if resume.get("format") != self.CHECKPOINT_FORMAT:
                raise ValueError("Not an evolution checkpoint.")
            version, internal, gauss = resume["rng_state"]
            self.rng.setstate((version, tuple(internal), gauss))
            self._validation_seed_cache = list(resume["validation_seeds"])
            if resume.get("situations") is not None:
                self._situation_cache = [tuple(pair) for pair in resume["situations"]]
            start = int(resume["next_generation"])
            population = [Genome.from_traits(traits) for traits in resume["population"]]
            scored = [(score, Genome.from_traits(traits)) for score, traits in resume["scored"]]
            mutation_step, stale = float(resume["mutation_step"]), int(resume["stale"])
            saved = resume.get("best")
            best = None if saved is None else (saved["score"], Genome.from_traits(saved["traits"]), list(saved["seeds"]))
            champions = [(score, Genome.from_traits(traits)) for score, traits in resume["champions"]]
            history = [GenerationStats(**stats) for stats in resume["history"]]
            warm_started = int(resume.get("warm_started", 0))
        else:
            start, mutation_step, stale, best, scored = 0, base_step, 0, None, []
            population, warm_started = self._initial_population()
            history, champions = [], []  # champions: (benchmark score, genome) of every generation
        validation_seeds = self._validation_seeds()
        early_stopped = False
        self.last_checkpoint = resume

        for generation in range(start, generations):
            seeds = self._story_seeds(generation)
            options = self._options(self._episode_situations(generation, len(seeds)))
            tasks = [(self.build_world, self.fitness, genome.traits(), seeds, max_steps, options) for genome in population]
            scores = self.map_fn(evaluate_task, tasks)
            scored = sorted(zip(scores, population), key=lambda pair: pair[0], reverse=True)
            values = [score for score, _ in scored]  # the generation's fitness, before the judge's say
            # Judge in the loop (judge_weight > 0): the LLM judge's engagement moves the best few.
            scored, judge_engagement = self._judged(
                scored, seeds[0], (options.get("situations") or [None])[0], max_steps)

            # The champion of this generation: the best on the fixed benchmark among the
            # benchmark_top_k best on this generation's stories (a lucky story cannot crown it).
            top_k = max(1, min(int(self.settings["benchmark_top_k"]), len(scored)))
            contenders = [Genome.from_traits(genome.traits()) for _, genome in scored[:top_k]]
            validations = self.map_fn(evaluate_task, [self.benchmark_task(g.traits()) for g in contenders])
            order = sorted(range(top_k), key=lambda i: validations[i], reverse=True)
            validation, champion = validations[order[0]], contenders[order[0]]
            # The benchmarked ones lead the breeding pool in their benchmark order (the elites).
            scored = [scored[i] for i in order] + scored[top_k:]
            champions.append((validation, champion))
            improved = best is None or validation > best[0]
            if improved:
                best = (validation, champion, validation_seeds)
                stale, mutation_step = 0, base_step
            else:
                stale += 1
                if stale >= int(self.settings["stagnation_generations"]):
                    mutation_step = min(float(self.settings["max_mutation_step"]), mutation_step * 1.5)

            stats = GenerationStats(
                generation=generation,
                best=round(max(values), 3),
                mean=round(statistics.mean(values), 3),
                worst=round(min(values), 3),
                spread=round(statistics.pstdev(values), 3),
                diversity=round(_diversity(population), 4),
                mutation_step=round(mutation_step, 4),
                improved=improved,
                best_traits=champion.traits(),
                validation=round(validation, 3),
                judge_engagement=judge_engagement,
            )
            history.append(stats)
            if progress is not None:
                progress(generation, generations, stats)

            # The next generation is bred now, so the saved state can continue from it.
            population = self._next_population(scored, mutation_step, diversity=stats.diversity)
            early = int(self.settings["early_stop_generations"])
            early_stopped = bool(early and stale >= early)
            self.last_checkpoint = self._checkpoint(
                generation + 1, population, scored, mutation_step, stale, best, champions, history,
                warm_started, early_stopped,
            )
            if checkpoint is not None:
                checkpoint(self.last_checkpoint)
            if (stop_event is not None and stop_event.is_set()) or early_stopped:
                break

        best_score, best_genome, best_seeds = best
        situations = self.benchmark_task(best_genome.traits())[5]["situations"] or [None] * len(best_seeds)
        best_state = max(
            (play_episode(self.build_world, best_genome, seed, max_steps, situation)
             for seed, situation in zip(best_seeds, situations)),
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
