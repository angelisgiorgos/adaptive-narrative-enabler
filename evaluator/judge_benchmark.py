"""Benchmark how engaging the engine's stories are, with Gemma 4 as the judge.

Plays stories with the current configuration (random storytellers, or a recorded
engine's storyteller), has evaluator/game_judge.py judge each one, and prints the mean
engagement, the eight criteria, the agreement of the fitness with the judge (Spearman)
and the judge's own reliability (every story judged twice).

    python -m evaluator.judge_benchmark --stories 15 --server http://127.0.0.1:8000/v1
    python -m evaluator.judge_benchmark --engine ENGINE_ID --stories 10 --out judged.jsonl

With --server the judge calls a vLLM server (vllm serve google/gemma-4-E4B-it); without
it, the vllm settings decide (embedded or server).

Pairwise mode compares two versions of the engine on the same seeds, which detects much
smaller gains than absolute scores (whose run-to-run noise is about 0.15 points):

    python -m evaluator.judge_benchmark --pairwise --variant director.surprise=0 --stories 20

A is the current configuration, B the same with each --variant (a dotted settings path and
a YAML value) applied. Every pair (same seed) is judged in both orders and a side wins only
where both orders agree. It prints B's win rate (B wins over decided pairs) with its 95%
Wilson interval, and B's win rate per criterion.
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


def _wilson(wins, n, z=1.96):
    """The 95% Wilson score interval of a win rate (wins out of n decided pairs)."""
    if n == 0:
        return [0.0, 1.0]
    p = wins / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / (1 + z * z / n)
    return [max(0.0, centre - half), min(1.0, centre + half)]


def pairwise(states_a, states_b, judge=None, workers=4):
    """Compare two sets of stories pair by pair (same index, i.e. the same seed): ([{pair,
    winner, criteria, reason}], summary). summary: pairs, a_wins, b_wins, ties, b_win_rate
    (B wins over decided pairs), wilson_95 and B's win rate per criterion."""
    judge = judge or GameJudge()
    pairs = list(zip(states_a, states_b))
    with ThreadPoolExecutor(max(1, workers)) as pool:
        results = list(pool.map(lambda pair: judge.compare_states(*pair), pairs))
    rows = [{"pair": index, "winner": r["winner"], "criteria": r["criteria"], "reason": r["reason"],
             "agreed": r.get("agreed", False)} for index, r in enumerate(results)]
    return rows, pairwise_summary(rows)


def pairwise_summary(rows):
    a_wins = sum(1 for r in rows if r["winner"] == "A")
    b_wins = sum(1 for r in rows if r["winner"] == "B")
    decided = a_wins + b_wins
    criteria = {}
    for name in CRITERIA:
        votes = [r["criteria"].get(name) for r in rows]
        a, b = votes.count("A"), votes.count("B")
        criteria[name] = b / (a + b) if a + b else None
    return {"pairs": len(rows), "a_wins": a_wins, "b_wins": b_wins, "ties": len(rows) - decided,
            "b_win_rate": b_wins / decided if decided else None, "wilson_95": _wilson(b_wins, decided),
            "criteria": criteria}


def _apply_variants(variants):
    """Apply "dotted.path=value" settings to the active configuration; returns how to undo them."""
    import copy

    import yaml

    from utils.config_loader import config

    saved = copy.deepcopy(config._config)
    for item in variants or []:
        path, _, raw = item.partition("=")
        if not path or not _:
            raise SystemExit(f"--variant needs key.path=value, not {item!r}")
        node = config._config
        keys = path.strip().split(".")
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = yaml.safe_load(raw)

    def undo():
        config._config.clear()
        config._config.update(saved)
    return undo


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
    parser.add_argument("--pairwise", action="store_true",
                        help="compare the current configuration (A) with --variant settings (B) on the same seeds")
    parser.add_argument("--variant", action="append", default=[],
                        help="a setting of version B: dotted.path=value (YAML), repeatable")
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
    if args.pairwise:
        stories_a = play_stories(args.stories, args.seed, genome)
        undo = _apply_variants(args.variant)
        try:
            stories_b = play_stories(args.stories, args.seed, genome)
        finally:
            undo()
        rows, result = pairwise(stories_a, stories_b, workers=args.workers)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as stream:
                stream.writelines(json.dumps(row) + "\n" for row in rows)
        print(json.dumps({"variant": args.variant, **result}, indent=2))
        return
    rows = benchmark(play_stories(args.stories, args.seed, genome), workers=args.workers)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as stream:
            stream.writelines(json.dumps(row) + "\n" for row in rows)
    print(json.dumps(summary(rows), indent=2))


if __name__ == "__main__":
    main()
