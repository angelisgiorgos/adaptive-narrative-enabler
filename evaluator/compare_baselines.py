"""Compare storytellers with the judge, against baselines.

    python -m evaluator.compare_baselines --server http://127.0.0.1:18090/v1 \\
        --worlds city examples/frozen_monastery.yaml examples/orbital_station.yaml \\
        --stories 16 --train 12 8 3 --out comparison.json

For each world it trains two storytellers with the same settings and seed: the **old** way (the
storyteller plays its own stories: ``evolution.player: storyteller``, the earlier protocol) and the
**new** way (simulated players choose, core_engine/players.py). Then it plays the same story seeds
with each of them and with **random** storytellers, and measures:

- fitness (the training objective) and how stories end (turns, quests won);
- the judge (Gemma 4 reads each story twice as a player would: engagement and eight criteria, 1-10),
  the agreement of fitness with the judge (Spearman) and the judge's test-retest difference;
- head to head (the judge compares two stories of the same seed, in both orders; a story wins
  only when both readings agree): new against old, and new against random.

Stories are played by simulated players (how people would play). The old storyteller is also
measured playing its own stories, the way the earlier results in docs/CHANGELOG.md were. No
learning memory is used (no warm start) and nothing is recorded, so baselines are clean. Writes
every story's judgement and the summary to ``--out`` (JSON) and prints the tables (markdown).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CONDITIONS = ("random", "old", "new")


def _activate(world):
    """Make ``world`` ("city": the repository's configuration, or a world-only YAML file) active."""
    import yaml

    import main as engine
    from backend.store import complete_config
    from utils.config_loader import config

    config.clear_override()
    if world != "city":
        completed, errors, _ = complete_config(yaml.safe_load(Path(world).read_text(encoding="utf-8")))
        if errors:
            raise SystemExit(f"{world}: " + "; ".join(errors[:5]))
        config.use_override(completed)
    engine.refresh_runtime_settings()


def _use_server(url):
    from utils.config_loader import config

    config._config.setdefault("vllm", {}).update({"mode": "server", "base_url": url})


def train(player, pop, generations, episodes, seed, workers):
    """The best storyteller of a run evaluated with ``player`` (simulated | storyteller)."""
    import main as engine
    from core_engine.evolution import NarrativeEvolution

    settings = {"pop_size": pop, "generations": generations, "episodes_per_genome": episodes,
                "warm_start": False, "judge_weight": 0.0, "player": player, "early_stop_generations": 0}
    started = time.time()
    with ProcessPoolExecutor(max_workers=workers) as pool:  # forked: they inherit the active world
        evolution = NarrativeEvolution(engine.build_world, engine.fitness, settings=settings, seed=seed,
                                       memory=None, map_fn=lambda f, tasks: list(pool.map(f, tasks)))
        result = evolution.run()
    return result.best_genome, {"player": player, "best_fitness": result.best_score,
                                "generations": result.generations_run, "seconds": round(time.time() - started)}


def play(genome, seeds, player_mode):
    """One whole story per seed: ``genome`` (None: a random storyteller per seed) tells it, and a
    simulated player (``simulated``) or the storyteller itself (``self``) plays it."""
    import main as engine
    from core_engine import Genome, players
    from core_engine.evolution import evolution_settings, play_episode

    settings = evolution_settings()
    states = []
    for seed in seeds:
        teller = genome or Genome(rng=random.Random(seed))
        player = players.player_for(seed, settings["player_mix"]) if player_mode == "simulated" else None
        states.append(play_episode(engine.build_world, teller, seed, 10 ** 6, None, player))
    return states


def _ci95(values):
    if len(values) < 2:
        return None
    return 1.96 * statistics.stdev(values) / math.sqrt(len(values))


def describe(states, rows):
    """The measures of a set of stories and their judgements."""
    from evaluator.judge_benchmark import summary

    judged = summary(rows)
    engagements = [r["engagement"] for r in rows if r["engagement"] is not None]
    fitness = [r["fitness"] for r in rows]
    return {
        "stories": len(states),
        "fitness": statistics.mean(fitness), "fitness_ci95": _ci95(fitness),
        "turns": statistics.mean(s.actions_taken for s in states),
        "quests_won": sum(1 for s in states if s.won) / len(states),
        "engagement": judged.get("engagement"), "engagement_ci95": _ci95(engagements),
        "criteria": judged.get("criteria"), "fitness_vs_judge": judged.get("fitness_vs_judge"),
        "retest_difference": judged.get("retest_difference"), "judged": judged.get("judged"),
        "fitness_version": judged.get("fitness_version"), "optional_content": judged.get("optional_content"),
        "filler_engine_vs_judge": judged.get("filler_engine_vs_judge"),
    }


def compare_world(world, args):
    from evaluator.judge_benchmark import benchmark, pairwise

    _activate(world)
    print(f"\n## {world}", flush=True)
    trained = {}
    for condition, player in (("old", "storyteller"), ("new", "simulated")):
        genome, info = train(player, *args.train, args.seed, args.workers)
        trained[condition] = genome
        print(f"trained {condition}: {info}", flush=True)
        trained[f"{condition}_training"] = info
    _use_server(args.server)
    seeds = list(range(args.first_seed, args.first_seed + args.stories))
    stories = {
        "random": play(None, seeds, "simulated"),
        "old": play(trained["old"], seeds, "simulated"),
        "new": play(trained["new"], seeds, "simulated"),
        "old, playing itself": play(trained["old"], seeds, "self"),
    }
    result = {"world": world, "seeds": seeds, "training": {k: trained[f"{k}_training"] for k in ("old", "new")},
              "conditions": {}, "pairwise": {}, "rows": {}}
    for name, states in stories.items():
        started = time.time()
        rows = benchmark(states, workers=args.workers_judge, repeats=2)
        for row, seed in zip(rows, seeds):
            row["seed"] = seed
        result["rows"][name] = rows
        result["conditions"][name] = describe(states, rows)
        print(f"judged {name}: engagement {result['conditions'][name]['engagement']} "
              f"({round(time.time() - started)}s)", flush=True)
    for label, a, b in (("new vs old", "old", "new"), ("new vs random", "random", "new")):
        _, outcome = pairwise(stories[a], stories[b], workers=args.workers_judge)
        result["pairwise"][label] = outcome
        print(f"{label}: new won {outcome['b_wins']}, lost {outcome['a_wins']}, tied {outcome['ties']}", flush=True)
    return result


def _fmt(value, digits=2):
    return "-" if value is None else f"{value:.{digits}f}"


def report(results):
    """The comparison as markdown tables."""
    from evaluator.game_judge import CRITERIA

    lines = []
    for world in results:
        lines += [f"### {world['world']}", "",
                  "| Storyteller (stories played by) | Fitness | Engagement (judge) | " + " | ".join(CRITERIA)
                  + " | Fitness vs judge (Spearman) | Turns | Quests won |",
                  "|" + " --- |" * (6 + len(CRITERIA))]
        for name, c in world["conditions"].items():
            crit = c.get("criteria") or {}
            lines.append(
                f"| {name} | {_fmt(c['fitness'], 0)} ± {_fmt(c['fitness_ci95'], 0)} | "
                f"{_fmt(c['engagement'])} ± {_fmt(c['engagement_ci95'])} | "
                + " | ".join(_fmt(crit.get(k)) for k in CRITERIA)
                + f" | {_fmt(c['fitness_vs_judge'])} | {_fmt(c['turns'], 1)} | {c['quests_won']:.0%} |")
        lines.append("")
        for label, p in world["pairwise"].items():
            low, high = p["wilson_95"]
            rate = "-" if p["b_win_rate"] is None else f"{p['b_win_rate']:.0%}"
            lines.append(f"- **{label}**: new won {p['b_wins']}, lost {p['a_wins']}, tied {p['ties']} of "
                         f"{p['pairs']}: win rate {rate} of decided pairs (95% Wilson {low:.0%}-{high:.0%})")
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--server", required=True, help="the judge's vLLM server (OpenAI-compatible URL)")
    parser.add_argument("--worlds", nargs="+", default=["city"],
                        help="city (the repository's world) or world-only YAML files")
    parser.add_argument("--stories", type=int, default=16, help="stories per storyteller and world")
    parser.add_argument("--first-seed", type=int, default=5000)
    parser.add_argument("--train", type=int, nargs=3, default=[12, 8, 3], metavar=("POP", "GENERATIONS", "EPISODES"))
    parser.add_argument("--seed", type=int, default=11, help="the training runs' seed (the same for old and new)")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 4, help="training processes")
    parser.add_argument("--workers-judge", type=int, default=8, help="judgements sent to the model at once")
    parser.add_argument("--out", default="comparison.json")
    args = parser.parse_args(argv)
    os.environ.setdefault("ANE_RECORDS", "off")  # baselines are not recorded
    results = []
    for world in args.worlds:
        results.append(compare_world(world, args))
        Path(args.out).write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    print("\n" + report(results))


if __name__ == "__main__":
    main()
