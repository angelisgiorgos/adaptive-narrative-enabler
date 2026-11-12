"""Benchmark how engaging the engine's stories are, with Gemma 4 as the judge.

Plays stories with the current configuration (random storytellers, or a recorded
engine's storyteller), has evaluator/game_judge.py judge each one, and prints the mean
engagement, the eight criteria, the agreement of the fitness with the judge (Spearman)
and the judge's own reliability (every story judged twice).

    python -m evaluator.judge_benchmark --stories 15 --server http://127.0.0.1:8000/v1
    python -m evaluator.judge_benchmark --engine ENGINE_ID --stories 10 --out judged.jsonl

With --server the judge calls a vLLM server (vllm serve google/gemma-4-E4B-it); without
it, the vllm settings decide (embedded or server).
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
from concurrent.futures import ThreadPoolExecutor

from evaluator.game_judge import CRITERIA, GameJudge, digest_state


def _rank(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks, i = [0.0] * len(values), 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(a, b):
    """Spearman's rank correlation of two equally long lists (0 when either is constant)."""
    ra, rb = _rank(list(a)), _rank(list(b))
    ma, mb = statistics.mean(ra), statistics.mean(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    norm = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return cov / norm if norm else 0.0


def play_stories(count, first_seed=0, genome=None):
    """``count`` finished stories (seeds first_seed..), by ``genome`` or random storytellers."""
    import main as engine
    from core_engine.game_state import GameState
    from core_engine.genome import Genome

    stories = []
    for seed in range(first_seed, first_seed + count):
        teller = genome or Genome(rng=random.Random(seed))
        state = GameState(engine.build_world(teller), teller, rng=random.Random(seed))
        while not state.ended and state.step():
            pass
        stories.append(state)
    return stories


def benchmark(states, judge=None, workers=4, repeats=2):
    """[{seed, fitness, engagement, scores, judgements}] for each story; the judge reads
    each story ``repeats`` times (their agreement measures its reliability)."""
    from core_engine.fitness import fitness

    judge = judge or GameJudge()
    digests = [digest_state(state) for state in states]
    with ThreadPoolExecutor(max(1, workers)) as pool:
        results = list(pool.map(judge.judge_digest, [d for d in digests for _ in range(repeats)]))
    rows = []
    for index, state in enumerate(states):
        mine = [r for r in results[index * repeats:(index + 1) * repeats] if r["engagement"] is not None]
        rows.append({
            "seed": index, "fitness": fitness(state), "turns": state.actions_taken,
            "engagement": statistics.mean(r["engagement"] for r in mine) if mine else None,
            "scores": {c: statistics.mean(r["scores"][c] for r in mine if c in r["scores"])
                       for c in CRITERIA if any(c in r["scores"] for r in mine)},
            "judgements": [r["engagement"] for r in mine],
            "verdict": mine[0]["verdict"] if mine else "",
        })
    return rows


def summary(rows):
    judged = [r for r in rows if r["engagement"] is not None]
    if not judged:
        return {"stories": len(rows), "judged": 0}
    twice = [r["judgements"] for r in judged if len(r["judgements"]) >= 2]
    return {
        "stories": len(rows), "judged": len(judged),
        "engagement": statistics.mean(r["engagement"] for r in judged),
        "criteria": {c: statistics.mean(r["scores"][c] for r in judged if c in r["scores"]) for c in CRITERIA},
        "fitness_vs_judge": spearman([r["fitness"] for r in judged], [r["engagement"] for r in judged]),
        "retest_difference": statistics.mean(abs(j[0] - j[1]) for j in twice) if twice else None,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m evaluator.judge_benchmark", description=__doc__.split("\n\n")[0])
    parser.add_argument("--stories", type=int, default=10)
    parser.add_argument("--seed", type=int, default=0, help="first story seed")
    parser.add_argument("--engine", help="a recorded engine's id (its world and storyteller)")
    parser.add_argument("--server", help="a vLLM server's OpenAI-compatible URL, e.g. http://127.0.0.1:8000/v1")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", help="write every story's judgement here (JSON lines)")
    args = parser.parse_args(argv)

    from utils.config_loader import config

    genome = None
    if args.engine:
        from backend import records

        model = records.load_engine(args.engine)
        model.activate()
        genome = model.genome
    if args.server:
        config._config.setdefault("vllm", {}).update({"mode": "server", "base_url": args.server})
    rows = benchmark(play_stories(args.stories, args.seed, genome), workers=args.workers)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as stream:
            stream.writelines(json.dumps(row) + "\n" for row in rows)
    print(json.dumps(summary(rows), indent=2))


if __name__ == "__main__":
    main()
