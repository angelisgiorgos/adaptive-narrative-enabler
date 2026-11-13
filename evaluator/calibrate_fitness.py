"""Fit the fitness to the judge: which fitness terms make a story engaging to the LLM judge.

Reads judged stories, fits a ridge regression (numpy only) of the judged engagement on the
standardized fitness terms, and suggests a multiplier for every term, to apply as
``fitness.term_weights``: a term that runs with the judge is grown (> 1), one that runs
against it is shrunk (< 1), within [0.25, 3.0].

    python -m evaluator.calibrate_fitness judged.jsonl [more.jsonl ...]
    python -m evaluator.calibrate_fitness --stories stories.jsonl --judged judged.jsonl --write weights.yaml

Inputs (JSON lines):
- combined: each line {"breakdown": {term: value}, "engagement": x} (also "judgement":
  {"engagement": x});
- separate: --stories lines with "breakdown" and "world", "kind", "seed", and --judged
  lines with the same keys and "judgement": {"engagement": x} (the scripts that judged the
  engine's stories write these); several judgements of one story are averaged.

It prints each term's standardized coefficient, the Spearman correlation between the fitted
and the judged engagement (in sample, and on stories left out by 5-fold cross-validation,
the honest one: with few stories and many terms the in-sample fit is optimistic; by default
the ridge strength --alpha is the one of ALPHAS that predicts left-out stories best), and a
YAML block ``fitness: term_weights: ...``; --write saves that block to a file
(it never changes the configuration itself).
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics

import numpy as np

LOW, HIGH = 0.25, 3.0
ALPHAS = (1.0, 3.0, 10.0, 30.0, 100.0, 300.0)  # ridge strengths tried when none is given


def _engagement(row):
    if isinstance(row.get("engagement"), (int, float)):
        return float(row["engagement"])
    judgement = row.get("judgement") or {}
    value = judgement.get("engagement") if isinstance(judgement, dict) else None
    return float(value) if isinstance(value, (int, float)) else None


def load_combined(paths):
    """[(breakdown, engagement)] of files whose lines hold both."""
    samples = []
    for path in paths:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                row = json.loads(line)
                engagement = _engagement(row)
                if isinstance(row.get("breakdown"), dict) and engagement is not None:
                    samples.append((row["breakdown"], engagement))
    return samples


def load_separate(stories_paths, judged_paths):
    """[(breakdown, mean engagement)] joining stories and judgements by (world, kind, seed)."""
    def key(row):
        return row.get("world"), row.get("kind"), row.get("seed"), row.get("version")

    breakdowns = {}
    for path in stories_paths:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    row = json.loads(line)
                    if isinstance(row.get("breakdown"), dict):
                        breakdowns[key(row)[:3]] = row["breakdown"]
    judged = collections.defaultdict(list)
    for path in judged_paths:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    row = json.loads(line)
                    if row.get("group", "story") != "story":
                        continue
                    engagement = _engagement(row)
                    if engagement is not None:
                        judged[key(row)[:3]].append(engagement)
    return [(breakdowns[k], statistics.mean(v)) for k, v in judged.items() if k in breakdowns]


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
    ra, rb = np.array(_rank(list(a))), np.array(_rank(list(b)))
    ra, rb = ra - ra.mean(), rb - rb.mean()
    norm = float(np.sqrt((ra ** 2).sum() * (rb ** 2).sum()))
    return float((ra * rb).sum() / norm) if norm else 0.0


def _ridge(x, y, alpha):
    """(names kept, coef, mean, std) of a ridge fit on standardized columns of x."""
    mean, std = x.mean(axis=0), x.std(axis=0)
    keep = std > 1e-12
    z = (x[:, keep] - mean[keep]) / std[keep]
    coef = np.linalg.solve(z.T @ z + alpha * np.eye(z.shape[1]), z.T @ (y - y.mean()))
    return keep, coef, mean, std


def _cross_validated(x, y, alpha, folds=5):
    """Spearman of out-of-fold predictions with the judged engagement (k-fold, in order):
    how well the fit predicts stories it was not fitted on."""
    n = len(y)
    if n < 2 * folds:
        return None
    predicted = np.zeros(n)
    for fold in range(folds):
        test = np.arange(n) % folds == fold
        keep, coef, mean, std = _ridge(x[~test], y[~test], alpha)
        z = (x[test][:, keep] - mean[keep]) / std[keep]
        predicted[test] = z @ coef + y[~test].mean()
    return spearman(predicted, y)


def fit(samples, alpha=None, gain=1.0):
    """Ridge regression of engagement on the standardized terms. Returns {terms: {term:
    {coef, mean, std, multiplier}}, spearman (in sample), cv_spearman (5-fold, out of
    sample; None for fewer than 10 stories), alpha, n, intercept}. ``alpha`` None: the one
    of ALPHAS with the best cv_spearman (10 when too few stories to cross-validate). Terms
    that never vary are left out (their multiplier would mean nothing)."""
    if len(samples) < 3:
        raise ValueError("At least 3 judged stories are needed to fit the fitness to the judge.")
    names = sorted({term for breakdown, _ in samples for term in breakdown})
    x = np.array([[float(breakdown.get(term, 0.0)) for term in names] for breakdown, _ in samples])
    y = np.array([engagement for _, engagement in samples], dtype=float)
    if alpha is None:
        scored = [(_cross_validated(x, y, a), a) for a in ALPHAS]
        alpha = max(scored, key=lambda pair: pair[0])[1] if scored[0][0] is not None else 10.0
    keep, coef, mean, std = _ridge(x, y, alpha)
    names = [name for name, k in zip(names, keep) if k]
    z = (x[:, keep] - mean[keep]) / std[keep]
    fitted = z @ coef + y.mean()
    scale = float(np.abs(coef).max()) or 1.0
    terms = {}
    for name, c, m, s in zip(names, coef, mean[keep], std[keep]):
        multiplier = float(np.clip(1.0 + gain * float(c) / scale, LOW, HIGH))
        terms[name] = {"coef": float(c), "mean": float(m), "std": float(s), "multiplier": round(multiplier, 3)}
    return {"terms": terms, "spearman": spearman(fitted, y), "cv_spearman": _cross_validated(x, y, alpha),
            "alpha": float(alpha), "n": len(samples), "intercept": float(y.mean())}


def to_yaml(result):
    lines = ["fitness:", "  term_weights:            # judge-calibrated multipliers (evaluator/calibrate_fitness.py)"]
    for name, term in sorted(result["terms"].items(), key=lambda item: -abs(item[1]["coef"])):
        lines.append(f"    {name}: {term['multiplier']}")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m evaluator.calibrate_fitness", description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="*", help="combined JSON lines: breakdown and engagement on each line")
    parser.add_argument("--stories", action="append", default=[], help="stories with breakdowns (separate input)")
    parser.add_argument("--judged", action="append", default=[], help="judgements of those stories (separate input)")
    parser.add_argument("--alpha", type=float, default=None,
                        help="ridge strength (default: the one predicting left-out stories best)")
    parser.add_argument("--gain", type=float, default=1.0, help="how far the strongest term moves from 1")
    parser.add_argument("--write", help="also write the YAML block to this file")
    args = parser.parse_args(argv)
    samples = load_combined(args.files) + (load_separate(args.stories, args.judged) if args.stories else [])
    result = fit(samples, alpha=args.alpha, gain=args.gain)
    cv = result["cv_spearman"]
    print(f"{result['n']} judged stories, alpha {result['alpha']:g}; fitted vs judged engagement (Spearman): "
          f"{result['spearman']:+.2f} in sample, "
          + (f"{cv:+.2f} on stories left out (5-fold)" if cv is not None else "too few stories to cross-validate"))
    for name, term in sorted(result["terms"].items(), key=lambda item: -abs(item[1]["coef"])):
        print(f"  {term['coef']:+.3f}  {name:34} -> x{term['multiplier']}")
    block = to_yaml(result)
    print(block)
    if args.write:
        with open(args.write, "w", encoding="utf-8") as stream:
            stream.write(block)


if __name__ == "__main__":
    main()
