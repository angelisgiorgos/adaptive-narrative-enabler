import random
import copy
import os
import datetime
import sys
import threading
import yaml
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from utils.config_loader import config
from utils.config_editor import ConfigEditor
from utils.tag_similarity import semantic_tag_similarity
from core_engine import Genome, Location, WorldGraph, Action, Outcome, GameState, Character, Object, Event, Mission
from core_engine.actions import REPEAT_RULES
from core_engine.narration import message
from core_engine import stats, variables
from core_engine.evolution import LearningMemory, NarrativeEvolution
# World building and fitness live in core_engine (also shipped in offline engine exports).
from core_engine.builder import (  # noqa: F401  (re-exported for callers of main)
    _SPAWN_RULES_CACHE, _build_action, _build_outcome, _build_spawn_rules, _coerce_list, _interaction_prompts,
    _location_distant_descriptions, _location_entered_descriptions, _resolve_outcome_links, _stat_usage_errors,
    build_world,
)
from core_engine.fitness import _fitness_breakdown, fitness  # noqa: F401

# ============================
# CONFIG (loaded from config.yaml)
# ============================

POP_SIZE = config.get("evolution.pop_size", 20)
GENERATIONS = config.get("evolution.generations", 20)
EPISODES_PER_GENOME = config.get("evolution.episodes_per_genome", 3)
MAX_STEPS = config.get("evolution.max_steps", 60)
EVALUATION_THREADS = config.get("evolution.evaluation_threads", 8)
PARALLEL_BACKEND = config.get("evolution.parallel_backend", "processes")
LAST_LEARNING = None  # learning summary of the latest evolve() run
LAST_EVOLUTION = None  # the NarrativeEvolution and EvolutionResult of the latest evolve() run
LAST_RESULT = None


class GenerationStopper:
    def __init__(self):
        self.stop_event = threading.Event()
        self._thread = None
        self._active = threading.Event()
        self._old_termios = None

    def __enter__(self):
        if not sys.stdin.isatty():
            return self

        self._active.set()
        if os.name == "nt":
            self._thread = threading.Thread(target=self._watch_windows, daemon=True)
        else:
            self._enable_unix_raw_input()
            self._thread = threading.Thread(target=self._watch_unix, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._active.clear()
        if self._old_termios is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_termios)

    def _enable_unix_raw_input(self):
        import termios
        import tty
        fd = sys.stdin.fileno()
        self._old_termios = termios.tcgetattr(fd)
        tty.setcbreak(fd)

    def _watch_unix(self):
        import select
        while self._active.is_set() and not self.stop_event.is_set():
            readable, _, _ = select.select([sys.stdin], [], [], 0.1)
            if readable and sys.stdin.read(1).lower() == "q":
                self.stop_event.set()
                print("\n[GENERATION] Stop requested. Finishing current step and moving to story execution...")
                return

    def _watch_windows(self):
        import msvcrt
        while self._active.is_set() and not self.stop_event.is_set():
            if msvcrt.kbhit() and msvcrt.getwch().lower() == "q":
                self.stop_event.set()
                print("\n[GENERATION] Stop requested. Finishing current step and moving to story execution...")
                return
            self.stop_event.wait(0.1)


def refresh_runtime_settings():
    global POP_SIZE, GENERATIONS, EPISODES_PER_GENOME, MAX_STEPS, EVALUATION_THREADS, PARALLEL_BACKEND
    POP_SIZE = config.get("evolution.pop_size", 20)
    GENERATIONS = config.get("evolution.generations", 20)
    EPISODES_PER_GENOME = config.get("evolution.episodes_per_genome", 3)
    MAX_STEPS = config.get("evolution.max_steps", 60)
    EVALUATION_THREADS = config.get("evolution.evaluation_threads", 8)
    PARALLEL_BACKEND = config.get("evolution.parallel_backend", "processes")


def apply_random_seed(force=False):
    if not force and not config.get("reproducibility.deterministic", True):
        return None
    seed = config.get("reproducibility.seed", config.get("simulation.seed"))
    if seed is None:
        return None

    seed = int(seed)
    random.seed(seed)

    try:
        import numpy as np
        np.random.seed(seed)
    except ImportError:
        pass

    try:
        import torch
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
        torch.use_deterministic_algorithms(True, warn_only=True)
        if hasattr(torch.backends, "cudnn"):
            torch.backends.cudnn.benchmark = False
            torch.backends.cudnn.deterministic = True
    except ImportError:
        pass

    print(f"[REPRODUCIBILITY] Random seed set to: {seed}")
    return seed


# ============================
# FITNESS FUNCTION
# ============================


# ============================
# WORLD CREATION
# ============================


def print_action_graph(world):
    print("\n" + "=" * 40)
    print("      FULL WORLD ACTION GRAPH")
    print("=" * 40)

    for name, loc in world.locations.items():
        print(f"\n[Location: {name}]")
        if loc.description:
            print(f"  Description: {loc.description}")
        print(f"  Tags: {', '.join(loc.tags)}")
        
        if not loc.actions:
            print("  (No actions defined)")
            continue

        for action in loc.actions:
            print(f"  > Action: {action.name}")
            for outcome in action.outcomes:
                status = f"Success Prob: {outcome.success_prob:.2f}"
                effects = []
                if outcome.health_change != 0:
                    effects.append(f"Health {outcome.health_change:+}")
                if outcome.spawn:
                    effects.append("New Location Spawn")
                if outcome.move_to:
                    effects.append(f"Move to {outcome.move_to}")
                if outcome.tags:
                    effects.append(f"Tags: {', '.join(outcome.tags)}")
                
                effect_str = f" | {', '.join(effects)}" if effects else ""
                print(f"    - Outcome: {outcome.desc:<30} ({status}{effect_str})")
    print("\n" + "=" * 40 + "\n")


# ============================
# EVALUATION
# ============================

def evaluate_genome(genome, rng_seed=None, stop_event=None):
    total = 0
    best_episode = None
    best_episode_score = float("-inf")
    episodes_completed = 0
    rng = random.Random(rng_seed) if rng_seed is not None else random.Random()

    for _ in range(EPISODES_PER_GENOME):
        if stop_event and stop_event.is_set() and best_episode is not None:
            break

        world = build_world(genome)
        state = GameState(world, genome, rng=rng)

        for _ in range(MAX_STEPS):
            if stop_event and stop_event.is_set():
                break
            if state.health <= 0:
                break
            if not state.step():
                break

        episode_score = fitness(state)
        total += episode_score
        episodes_completed += 1
        if episode_score > best_episode_score:
            best_episode_score = episode_score
            best_episode = state

    if episodes_completed == 0:
        return float("-inf"), best_episode_score, best_episode

    return total / episodes_completed, best_episode_score, best_episode


# ============================
# EVOLUTION LOOP
# ============================

def _base_evaluation_seed():
    seed = config.get("reproducibility.seed", config.get("simulation.seed"))
    return int(seed) if seed is not None else None


def _evaluation_seed(base_seed, generation, genome_index):
    if base_seed is None:
        return None
    return base_seed + generation * max(1, POP_SIZE) + genome_index


def _init_spawned_worker(config_data, tag_vectors):
    """A spawned evaluation process starts from this process's configuration (an uploaded or
    augmented world) and tag vectors, as a forked one would inherit them."""
    from utils.tag_similarity import load_tag_vectors

    config.use_override(config_data)
    load_tag_vectors(tag_vectors)
    refresh_runtime_settings()


def _new_process_pool(max_workers):
    """A pool of evaluation processes. They are forked, so they inherit this process's
    configuration, unless this process holds an embedded vLLM engine (Gemma 4 on the GPU,
    e.g. after world augmentation): a CUDA context must not be forked, so they are spawned
    and given the configuration and tag vectors explicitly."""
    from utils.llm_model_loader import embedded_engine_active

    if not embedded_engine_active():
        return ProcessPoolExecutor(max_workers=max_workers)
    import multiprocessing

    from core_engine.model import world_tags
    from utils.tag_similarity import export_tag_vectors

    active = config.as_dict()
    return ProcessPoolExecutor(
        max_workers=max_workers, mp_context=multiprocessing.get_context("spawn"),
        initializer=_init_spawned_worker, initargs=(active, export_tag_vectors(world_tags(active))),
    )


def _worker_evaluate_genome(args):
    index, genome, rng_seed = args
    score, episode_score, _ = evaluate_genome(genome, rng_seed=rng_seed)
    return index, score, episode_score


def _evaluation_worker_count(population_size):
    configured = EVALUATION_THREADS
    if isinstance(configured, str) and configured.lower() == "auto":
        configured = os.cpu_count() or 1
    maximum = int(config.get("evolution.max_evaluation_processes", 16))
    return max(1, min(int(configured), maximum, population_size))


def evaluate_population(population, generation, base_seed, stop_event=None, executor=None):
    max_workers = _evaluation_worker_count(len(population))
    if max_workers == 1:
        results = []
        for index, genome in enumerate(population):
            if stop_event and stop_event.is_set() and results:
                break
            score, episode_score, state = evaluate_genome(
                genome,
                rng_seed=_evaluation_seed(base_seed, generation, index),
                stop_event=stop_event,
            )
            results.append((score, genome, episode_score, state))
        return results

    if PARALLEL_BACKEND == "processes":
        results = [None] * len(population)
        tasks = [
            (index, genome, _evaluation_seed(base_seed, generation, index))
            for index, genome in enumerate(population)
        ]
        local_executor = executor is None
        process_executor = executor or _new_process_pool(max_workers)
        futures = {
            process_executor.submit(_worker_evaluate_genome, task): task[0]
            for task in tasks
        }
        try:
            for future in as_completed(futures):
                index, score, episode_score = future.result()
                results[index] = (score, population[index], episode_score, None)
                if stop_event and stop_event.is_set():
                    for pending in futures:
                        pending.cancel()
                    break
        finally:
            if local_executor:
                process_executor.shutdown(
                    wait=not (stop_event and stop_event.is_set()),
                    cancel_futures=True,
                )
        return [result for result in results if result is not None]

    results = [None] * len(population)
    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = {
            executor.submit(
                evaluate_genome,
                genome,
                _evaluation_seed(base_seed, generation, index),
                stop_event,
            ): index
            for index, genome in enumerate(population)
    }
    try:
        for future in as_completed(futures):
            index = futures[future]
            score, episode_score, state = future.result()
            results[index] = (score, population[index], episode_score, state)
            if stop_event and stop_event.is_set():
                for pending in futures:
                    pending.cancel()
                break
    finally:
        executor.shutdown(wait=not (stop_event and stop_event.is_set()), cancel_futures=True)

    results = [result for result in results if result is not None]
    return results


def evolve(stop_event=None, progress=None):
    """Evolve storytellers with the shared evolutionary engine (core_engine.evolution).

    Returns (best mean score, best genome, best episode score, best episode state).
    """
    max_workers = _evaluation_worker_count(POP_SIZE)
    executor = None
    if max_workers > 1:
        executor = (_new_process_pool(max_workers) if PARALLEL_BACKEND == "processes"
                    else ThreadPoolExecutor(max_workers=max_workers))
    map_fn = (lambda function, tasks: list(executor.map(function, tasks))) if executor else None

    def report(generation, total, stats):
        print(
            f"Gen {generation} | Best Score: {stats.best:.2f} | Mean: {stats.mean:.2f} | "
            f"Diversity: {stats.diversity:.3f} | Mutation step: {stats.mutation_step:.3f}"
            + ("  (improved)" if stats.improved else "")
        )
        if progress is not None:
            progress(generation, total, stats)

    memory = LearningMemory() if config.get("evolution.warm_start", True) else None
    evolution = NarrativeEvolution(build_world, fitness, seed=_base_evaluation_seed(), memory=memory, map_fn=map_fn)
    try:
        result = evolution.run(progress=report, stop_event=stop_event)
    finally:
        if executor is not None:
            executor.shutdown(wait=not (stop_event and stop_event.is_set()), cancel_futures=True)
    global LAST_LEARNING, LAST_EVOLUTION, LAST_RESULT
    LAST_LEARNING = result.summary()
    LAST_EVOLUTION, LAST_RESULT = evolution, result
    if result.warm_started:
        print(f"[LEARNING] Started from {result.warm_started} genome(s) remembered from earlier runs.")
    return result.best_score, result.best_genome, fitness(result.best_state), result.best_state


# ============================
# RUN
# ============================

def _runs_dir():
    path = os.path.join(os.path.dirname(__file__), "runs")
    if not os.path.exists(path):
        os.makedirs(path)
    return path


def _run_archive_dir(timestamp):
    base_path = os.path.join(_runs_dir(), timestamp)
    path = base_path
    suffix = 1
    while os.path.exists(path):
        path = f"{base_path}_{suffix:03d}"
        suffix += 1
    if not os.path.exists(path):
        os.makedirs(path)
    return path


def _world_definition_with_runtime_connections(state):
    world_definition = copy.deepcopy(config.get("world_definition", {}))
    spawn_rules = copy.deepcopy(world_definition.get("spawn_rules", {}))

    for name, loc in state.world.locations.items():
        connected = sorted(loc.connected)
        if connected:
            existing = set(spawn_rules.get(name, []))
            spawn_rules[name] = sorted(existing | set(connected))

    world_definition["spawn_rules"] = spawn_rules
    return world_definition


def _resolve_project_path(path):
    path = os.path.expanduser(path.strip())
    if os.path.isabs(path):
        return path
    return os.path.abspath(os.path.join(os.path.dirname(__file__), path))


def load_world_from_path(world_path):
    resolved_path = _resolve_project_path(world_path)
    if not os.path.exists(resolved_path):
        print(f"[ARCHIVE] World file not found: {resolved_path}")
        return None

    with open(resolved_path, "r") as f:
        data = yaml.safe_load(f) or {}

    world_definition = data.get("world_definition", data)
    if not isinstance(world_definition, dict) or "locations" not in world_definition:
        print(f"[ARCHIVE] No world_definition.locations found in: {resolved_path}")
        return None

    config.set_world_definition(world_definition)
    print(f"[ARCHIVE] Loaded world from: {resolved_path}")
    return resolved_path


def load_best_world_if_available():
    if not config.get("experiment_archive.load_best_world_on_startup", True):
        return None

    best_world_file = os.path.join(_runs_dir(), "best_world.yaml")
    if not os.path.exists(best_world_file):
        return None

    with open(best_world_file, "r") as f:
        data = yaml.safe_load(f) or {}

    world_definition = data.get("world_definition")
    if not world_definition:
        return None

    config.set_world_definition(world_definition)
    print(f"[ARCHIVE] Loaded best world from: {best_world_file}")
    return best_world_file


def export_offline_engine(genome, score, run_dir, timestamp):
    """Write the trained engine as an offline, game-ready ZIP next to the run archive."""
    try:
        from backend.export import export_engine  # imported here: the backend imports this module

        result = export_engine(genome=genome, score=score, learning=LAST_LEARNING,
                               description="best of a terminal run", name=f"narrative-engine-{timestamp}")
        target = os.path.join(run_dir, "offline_engine.zip")
        os.replace(result["path"], target)
        print(f"[EXPORT] Offline engine for games saved to: {target} (unzip, then `python run.py play|serve`)")
        return target
    except Exception as exc:  # the story and archive are saved either way
        print(f"[EXPORT] The offline engine could not be exported: {exc}")
        return None


def record_terminal_run(evolution, result, run_id, run_dir=None, origin="terminal", parent_id=None, source=None):
    """Record a finished terminal run in the records database and save its engine
    model (with the complete training state) into the run archive.
    Returns the EngineModel."""
    from backend import records  # imported here: the backend imports this module
    from core_engine.model import EngineModel

    model = EngineModel.from_training(evolution, result, source=source or f"{origin} run {run_id}", parent_id=parent_id)
    records.finish_run(run_id, result, model=model, origin=origin)
    records.record_story(result.best_state, origin, "preview", run_id=run_id, engine_id=model.id,
                         parameters={"benchmark": True})
    if run_dir:
        path = model.save(os.path.join(run_dir, "engine_model.ane.zip"))
        print(f"[ARCHIVE] Engine model (resumable training) saved to: {path}")
    return model


def save_best_artifacts(score, genome, state, run_dir=None, timestamp=None, reason="Best structural evolution score"):
    if state is None:
        return None, None

    runs_dir = _runs_dir()
    timestamp = timestamp or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = run_dir or _run_archive_dir(timestamp)
    best_world_file = os.path.join(run_dir, "best_world.yaml")
    best_story_file = os.path.join(run_dir, "best_story.txt")
    latest_best_world_file = os.path.join(runs_dir, "best_world.yaml")
    latest_best_story_file = os.path.join(runs_dir, "best_story.txt")

    payload = {
        "saved_at": timestamp,
        "score": float(score),
        "reason": reason,
        "best_genome": {k: float(v) for k, v in vars(genome).items()},
        "path": state.path,
        "world_definition": _world_definition_with_runtime_connections(state),
    }

    for path in (best_world_file, latest_best_world_file):
        with open(path, "w") as f:
            yaml.safe_dump(payload, f, sort_keys=False, allow_unicode=False)

    for path in (best_story_file, latest_best_story_file):
        with open(path, "w") as f:
            f.write("=" * 60 + "\n")
            f.write(f" BEST SCORE STORY: {timestamp}\n")
            f.write("=" * 60 + "\n\n")
            f.write(f"Best Score: {score:.2f}\n")
            f.write(f"Traveler's Path: {' -> '.join(state.path)}\n\n")
            f.write("--- FULL STORY TRANSCRIPT ---\n")
            f.write(state.get_full_story())
            f.write("\n")

    return best_world_file, best_story_file

def prompt_story_setup_editor():
    editor = ConfigEditor(os.path.dirname(__file__))

    print("\nStartup options:")
    print("  1. Run the story generator")
    print("  2. Show the existing story setup")
    print("  3. Edit story setup, then run")
    print("  4. Edit story setup only")
    print("  5. Load a world YAML path, then run")

    while True:
        choice = input("Selection [1/2/3/4/5]: ").strip() or "1"
        if choice == "1":
            return True, None
        if choice == "2":
            editor.browse_story()
            continue
        if choice == "3":
            should_run = editor.run()
            config.reload()
            return should_run, None
        if choice == "4":
            editor.run()
            config.reload()
            return False, None
        if choice == "5":
            world_path = input("World YAML path: ").strip()
            if world_path:
                return True, world_path
            print("Please enter a path, or choose another option.")
            continue
        print("Please choose 1, 2, 3, 4, or 5.")


def run_story(world_path=None):
    config.reload()
    refresh_runtime_settings()
    apply_random_seed()
    if world_path:
        load_world_from_path(world_path)
    else:
        load_best_world_if_available()

    # Check for World Augmentation
    # Ensure the output directory exists as per user request
    output_dir = config.get("augmentation.output_dir", "augmentations")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        print(f"Created directory: {output_dir}")

    if config.get("augmentation.enabled", False):
        from core_engine.augmenter import WorldAugmenter
        print("\n--- Checking for World Augmentation ---")
        augmenter = WorldAugmenter()
        res = augmenter.augment(update_config=True)
        if res and res[0]:
            augmented_data, augmented_path = res
            print(f"\n[SUCCESS] World expanded! Using augmented configuration found in: {augmented_path}")
        else:
            print("Augmentation skipped or failed.")

    # Print world locations after potential augmentation
    print("\n--- World Locations (Active Configuration) ---")
    dummy_world = build_world(Genome())
    for name, loc in dummy_world.locations.items():
        print(f"Location: {name:<15} | Tags: {', '.join(loc.tags)}")
    print("-" * 30)
    for obj in dummy_world.objects:
        print(f"Object:   {obj.name:<15} | Tags: {', '.join(obj.associated_tags)}")
    print("-" * 30 + "\n")

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = _run_archive_dir(timestamp)
    timestamp = os.path.basename(run_dir)
    print(f"[ARCHIVE] This generation run will be saved in: {run_dir}")
    print("[GENERATION] Press 'q' to stop generation early and execute the best story found so far.")
    from backend import records  # imported here: the backend imports this module

    run_id = records.start_run(
        "terminal", settings=dict(config.get("evolution") or {}), seed=_base_evaluation_seed(),
        parameters={"archive": run_dir, "world_path": world_path},
    )
    try:
        with GenerationStopper() as stopper:
            best_score, best_genome, best_episode_score, best_episode_state = evolve(
                stop_event=stopper.stop_event,
            )
    except Exception as exc:
        records.finish_run(run_id, None, error=exc)
        raise
    model = record_terminal_run(LAST_EVOLUTION, LAST_RESULT, run_id, run_dir=run_dir)
    best_world_file, best_story_file = save_best_artifacts(
        best_episode_score,
        best_genome,
        best_episode_state,
        run_dir=run_dir,
        timestamp=timestamp,
    )

    print("\nBest Genome:")
    for k, v in vars(best_genome).items():
        print(f"{k}: {v:.3f}")

    print("Score:", best_score)
    if best_world_file and best_story_file:
        print(f"[ARCHIVE] Best score world saved to: {best_world_file}")
        print(f"[ARCHIVE] Best score story saved to: {best_story_file}")

    print("\nSimulating detailed best story...")
    final_world = build_world(best_genome)
    
    # Obtain simulation parameters from config
    sim_verbose = config.get("simulation.verbose", True)
    sim_interactive = config.get("simulation.interactive", True)
    
    final_state = GameState(final_world, best_genome, store=variables.VariableStore(), 
                            verbose=True, 
                            interactive=sim_interactive)
    story_id = records.record_story(final_state, "terminal", "terminal", run_id=run_id, engine_id=model.id,
                                    parameters={"interactive": sim_interactive})
    for _ in range(max(MAX_STEPS, config.get("narrative_end.max_actions", 100))):  # the whole story
        if final_state.health <= 0: break
        if not final_state.step(): break
        records.update_story(story_id, final_state)
    records.update_story(story_id, final_state)
    
    # (Transcript moved to evaluation section below for visibility)

    print(f"\nTraveler's Path: {' -> '.join(final_state.path)}")
    print(f"Mission: {final_state.mission_status}")

    # print_action_graph(final_world)

    # Final Narrative Report & Evaluation
    print("\n" + "="*50)
    print("      THE UNFOLDED STORY (Full Transcript)")
    print("="*50)
    print(final_state.get_full_story())
    print("="*50 + "\n")

    if config.get("llm_evaluator.enabled", False):
        print("\n--- Final Narrative Evaluation ---")
        from evaluator.llm_eval import LLMNarrativeEvaluator
        evaluator = LLMNarrativeEvaluator()
        score, reason = evaluator.evaluate_with_debug(final_state)
        print(f"\nFinal Narrative Score: {score}/100")
        print(f"Reasoning: {reason}")
    else:
        score, reason = "N/A", "Evaluation Disabled"

    # --- Save Run Archival ---
    run_file = os.path.join(run_dir, f"story_{timestamp}.txt")
    
    with open(run_file, "w") as f:
        f.write("="*60 + "\n")
        f.write(f" ADAPTIVE NARRATIVE RUN: {timestamp}\n")
        f.write("="*60 + "\n\n")
        
        f.write("--- EVOLUTION PARAMETERS (Best Genome) ---\n")
        for k, v in vars(best_genome).items():
            f.write(f"{k}: {v:.4f}\n")
        f.write("\n")
        
        f.write("--- NARRATIVE EVALUATION ---\n")
        f.write(f"Final Score: {score}/100\n")
        f.write(f"Reasoning: {reason}\n\n")
        
        f.write("--- TRAVELER'S PATH ---\n")
        f.write(" -> ".join(final_state.path) + "\n\n")
        f.write("--- MISSION ---\n")
        f.write(f"{final_state.mission_status}\n\n")
        f.write("--- FULL STORY TRANSCRIPT ---\n")
        f.write(final_state.get_full_story())
        f.write("\n\n" + "="*60 + "\n")

    print(f"\n[ARCHIVE] Story successfully saved to: {run_file}")
    offline = export_offline_engine(best_genome, best_score, run_dir, timestamp)
    for kind, path in (("best_world", best_world_file), ("best_story", best_story_file), ("story", run_file),
                       ("offline_engine", offline)):
        if path:
            records.record_artifact(kind, path, run_id=run_id, engine_id=model.id)


def _load_model(reference):
    """An engine model from a file path or a recorded engine id."""
    from backend import records
    from core_engine.model import EngineModel

    return EngineModel.load(reference) if os.path.isfile(reference) else records.load_engine(reference)


def _parallel_map():
    workers = _evaluation_worker_count(POP_SIZE)
    if workers <= 1:
        return None, None
    executor = _new_process_pool(workers) if PARALLEL_BACKEND == "processes" else ThreadPoolExecutor(max_workers=workers)
    return executor, (lambda function, tasks: list(executor.map(function, tasks)))


def engine_command(argv=None):
    """Work with saved engines from the terminal (python main.py --help)."""
    import argparse

    from backend import records

    parser = argparse.ArgumentParser(prog="python main.py", description=(
        "Without options: the interactive editor and a new evolution run. With an engine (a model file "
        ".ane.zip or a recorded engine id from the records database):"))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--continue-training", metavar="ENGINE", help="continue its training exactly where it stopped")
    group.add_argument("--retrain", metavar="ENGINE", help="a new run that starts from its storyteller and memory")
    group.add_argument("--play", metavar="ENGINE", help="play a story with it in the terminal")
    group.add_argument("--export-world", metavar="ENGINE", help="write its world and tuning as one YAML file")
    group.add_argument("--list-engines", action="store_true", help="list the recorded engines")
    group.add_argument("--import-artifacts", metavar="FOLDER", nargs="?", const="",
                       help="record every engine and game in FOLDER (default: the artifacts folder) and move the "
                            "files into the records folder")
    parser.add_argument("--copy", action="store_true", help="--import-artifacts: copy the files instead of moving them")
    parser.add_argument("--no-preview", action="store_true", help="--import-artifacts: no preview story per new engine")
    parser.add_argument("--generations", type=int, default=5)
    parser.add_argument("--out", help="output file (--export-world)")
    parser.add_argument("--seed", type=int)
    args = parser.parse_args(argv)

    if args.import_artifacts is not None:
        import json

        from backend import sessions

        folder = args.import_artifacts or os.getenv("ANE_ARTIFACT_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")
        report = sessions.import_artifacts(folder, move=not args.copy, preview=not args.no_preview)
        print(json.dumps(report, indent=2))
        return
    if args.list_engines:
        for engine in records.list_engines(limit=100):
            print(f"{engine['id']}  {engine['source']:<20} gen={engine['generation']!s:<5} "
                  f"score={engine['score']!s:<10} model={'yes' if engine['model_path'] else 'no'}  {engine['created_at']}")
        return
    reference = args.continue_training or args.retrain or args.play or args.export_world
    model = _load_model(reference)
    if args.export_world:
        out = args.out or f"world_{model.id[:8]}.yaml"
        with open(out, "w", encoding="utf-8") as stream:
            stream.write(model.world_yaml())
        print(f"[WORLD] World and tuning of engine {model.id} written to {out}")
        return
    if args.play:
        state = model.new_game(seed=args.seed, interactive=True, store=variables.VariableStore())
        story_id = records.record_story(state, "terminal", "terminal", engine_id=model.id, seed=args.seed)
        while not state.ended and state.step():
            records.update_story(story_id, state)
        records.update_story(story_id, state)
        print(state.get_full_story())
        return

    model.activate()
    refresh_runtime_settings()
    executor, map_fn = _parallel_map()
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = _run_archive_dir(timestamp)
    kind = "continue" if args.continue_training else "retrain"
    run_id = records.start_run("terminal", kind=kind, settings=(model.checkpoint or {}).get("settings"),
                               parameters={"generations": args.generations, "archive": run_dir}, seed=args.seed,
                               parent_engine_id=model.id)

    def report(generation, total, stats):
        print(f"Gen {generation} | Best Score: {stats.best:.2f} | Benchmark: {stats.validation:.2f}"
              + ("  (improved)" if stats.improved else ""))

    try:
        if args.continue_training:
            result, newer = model.continue_training(args.generations, progress=report, map_fn=map_fn)
        else:
            result, newer = model.retrain({"generations": args.generations}, seed=args.seed, progress=report,
                                          map_fn=map_fn)
    except Exception as exc:
        records.finish_run(run_id, None, error=exc)
        raise
    finally:
        if executor is not None:
            executor.shutdown(wait=True)
    records.finish_run(run_id, result, model=newer, origin="terminal")
    records.record_story(result.best_state, "terminal", "preview", run_id=run_id, engine_id=newer.id)
    path = newer.save(os.path.join(run_dir, "engine_model.ane.zip"))
    print(f"[ENGINE] {kind}: benchmark {result.best_score:.2f}; engine {newer.id} saved to {path}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        engine_command(sys.argv[1:])
    else:
        should_run, world_path = prompt_story_setup_editor()
        if should_run:
            run_story(world_path=world_path)
