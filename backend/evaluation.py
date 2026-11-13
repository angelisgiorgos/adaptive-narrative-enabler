"""Evaluation with Gemma 4 as the judge, for the API (and so for the studio):

- compare two stories (a game in progress or a recorded story each): which is the better
  game, judged in both orders (``evaluator.game_judge.GameJudge.compare_digests``);
- the judge benchmark of the engine's stories: each story judged (absolute), or the same
  seeds played with the current settings (A) and with changed settings (B) and compared pair
  by pair, with B's win rate and its 95% Wilson interval (``evaluator.judge_benchmark``);
- fitting the fitness to the judge: stories played and judged, the judged engagement fitted on
  the fitness terms, and the suggested ``fitness.term_weights`` (``evaluator.calibrate_fitness``),
  applied to the configuration only when asked.

Stories are played in a separate (spawned) process that is given the configuration it needs,
so an A/B never changes the configuration the games of this process are using.
"""
from __future__ import annotations

import copy
import multiprocessing
import statistics
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

import yaml

from utils.config_loader import config

MAX_STORIES = 60


class EvaluationError(ValueError):
    """A request that cannot be evaluated (unknown story or session, bad settings)."""


# ---- playing stories in a process of their own -----------------------------------------------

def _play(config_data, tag_vectors, count, first_seed, traits, budget):
    """Run in a spawned process: ``count`` finished stories with this configuration, each as
    {seed, digest, breakdown, fitness, turns, won, end_kind}."""
    import random

    import main as engine
    from core_engine.fitness import _fitness_breakdown
    from core_engine.game_state import GameState
    from core_engine.genome import Genome
    from evaluator.game_judge import digest_state

    engine._init_spawned_worker(config_data, tag_vectors)
    stories = []
    for seed in range(first_seed, first_seed + count):
        teller = Genome(rng=random.Random(seed))
        if traits:
            for name, value in traits.items():
                setattr(teller, name, value)
        state = GameState(engine.build_world(teller), teller, rng=random.Random(seed))
        while not state.ended and state.step():
            pass
        breakdown = _fitness_breakdown(state)
        stories.append({
            "seed": seed, "digest": digest_state(state, max_chars=budget), "breakdown": breakdown,
            "fitness": sum(breakdown.values()), "turns": state.actions_taken, "won": bool(state.won),
            "end_kind": state.end_kind,
        })
    return stories


def _with_variants(config_data, variants):
    """A copy of ``config_data`` with ``variants`` ({dotted.path: value}) set."""
    changed = copy.deepcopy(config_data)
    for path, value in (variants or {}).items():
        keys = [key for key in str(path).strip().split(".") if key]
        if not keys:
            raise EvaluationError(f"`{path}` is not a settings path (use dotted.path).")
        node = changed
        for key in keys[:-1]:
            child = node.setdefault(key, {})
            if not isinstance(child, dict):
                raise EvaluationError(f"`{path}`: `{key}` is not a section.")
            node = child
        node[keys[-1]] = yaml.safe_load(value) if isinstance(value, str) else value
    return changed


def _source(engine_id):
    """(configuration, tag vectors, storyteller traits) of the current settings or a recorded engine."""
    from core_engine.model import world_tags
    from utils.tag_similarity import export_tag_vectors

    if engine_id:
        from backend import records

        try:
            model = records.load_engine(engine_id)
        except Exception as exc:  # noqa: BLE001 - an unknown engine
            raise EvaluationError(f"Unknown engine `{engine_id}`.") from exc
        return copy.deepcopy(model.config), dict(model.tag_vectors or {}), dict(model.genome_traits)
    active = config.as_dict()
    return active, export_tag_vectors(world_tags(active)), None


def play_stories(count, first_seed=0, engine_id=None, variants=None, budget=None):
    """Play ``count`` stories (seeds first_seed..) in a spawned process, with the current
    settings or a recorded engine's, and ``variants`` applied."""
    count = int(count)
    if not 1 <= count <= MAX_STORIES:
        raise EvaluationError(f"Between 1 and {MAX_STORIES} stories.")
    config_data, vectors, traits = _source(engine_id)
    config_data = _with_variants(config_data, variants)
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
        return pool.submit(_play, config_data, vectors, count, int(first_seed), traits, budget).result()


# ---- the judge -----------------------------------------------------------------------------

def _judge():
    from evaluator.game_judge import GameJudge

    return GameJudge()


def _pair_budget():
    from evaluator.game_judge import _settings

    return int(_settings()["pair_max_chars"])


def story_digest(spec, budget=None):
    """The digest of {"session_id": ...} (a game in progress) or {"story_id": ...} (a recorded story)."""
    from backend import records, sessions
    from evaluator.game_judge import digest_state, digest_turns

    spec = spec or {}
    if spec.get("session_id"):
        with sessions.SESSION_LOCK:
            state = sessions.SESSIONS.get(spec["session_id"])
            if state is None:
                raise EvaluationError(f"Play session `{spec['session_id']}` not found.")
            return digest_state(state, max_chars=budget)
    if spec.get("story_id"):
        try:
            story = records.get_story(spec["story_id"])
        except Exception as exc:  # noqa: BLE001 - an unknown story
            raise EvaluationError(f"Recorded story `{spec['story_id']}` not found.") from exc
        meta = {key: story.get(key) for key in ("mission", "start_location", "end_kind", "end_reason", "won")}
        return digest_turns(story["turn_records"], meta, max_chars=budget)
    raise EvaluationError("Name a story: {\"session_id\": ...} or {\"story_id\": ...}.")


def compare(a, b, judge=None):
    """Which of two stories is the better game: {winner (A, B, tie or None when unreadable),
    criteria {criterion: A|B|tie}, reason, agreed (both orders agreed)}."""
    budget = _pair_budget()
    digest_a, digest_b = story_digest(a, budget), story_digest(b, budget)
    result = (judge or _judge()).compare_digests(digest_a, digest_b)
    return {key: result.get(key) for key in ("winner", "criteria", "reason", "agreed")}


def _progress_step(progress, total):
    done = {"count": 0}

    def step(label):
        done["count"] += 1
        if progress is not None:
            progress(done["count"], total, label)
    return step


def benchmark(stories=10, seed=0, engine_id=None, variants=None, pairwise=False, workers=4, judge=None, progress=None):
    """The judge on the engine's stories. Absolute: each story judged ({engagement, criteria,
    stories: [{seed, engagement, fitness, turns, won, verdict}]}). Pairwise (``variants``
    {dotted.path: value} are version B): the same seeds with and without them, each pair judged
    in both orders ({pairs, a_wins, b_wins, ties, b_win_rate, wilson_95, criteria, rows})."""
    from evaluator.game_judge import CRITERIA
    from evaluator.judge_benchmark import pairwise_summary

    judge = judge or _judge()
    workers = max(1, min(8, int(workers)))
    if pairwise:
        if not variants:
            raise EvaluationError("A pairwise benchmark needs the settings of version B (`variants`).")
        budget = _pair_budget()
        played_a = play_stories(stories, seed, engine_id, None, budget)
        played_b = play_stories(stories, seed, engine_id, variants, budget)
        step = _progress_step(progress, len(played_a))

        def one(pair):
            result = judge.compare_digests(pair[0]["digest"], pair[1]["digest"])
            step("pairs judged")
            return result

        with ThreadPoolExecutor(workers) as pool:
            results = list(pool.map(one, zip(played_a, played_b)))
        rows = [{"seed": a["seed"], "winner": r["winner"], "criteria": r["criteria"], "reason": r["reason"],
                 "agreed": r.get("agreed", False), "fitness_a": a["fitness"], "fitness_b": b["fitness"]}
                for a, b, r in zip(played_a, played_b, results)]
        return {"variants": dict(variants), **pairwise_summary(rows), "rows": rows}

    played = play_stories(stories, seed, engine_id, variants)
    step = _progress_step(progress, len(played))

    def judge_one(story):
        result = judge.judge_digest(story["digest"])
        step("stories judged")
        return result

    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(judge_one, played))
    rows = [{"seed": s["seed"], "engagement": r["engagement"], "scores": r["scores"], "fitness": s["fitness"],
             "turns": s["turns"], "won": s["won"], "verdict": r.get("verdict", "")} for s, r in zip(played, results)]
    judged = [row for row in rows if row["engagement"] is not None]
    criteria = {name: round(statistics.mean(row["scores"][name] for row in judged if name in row["scores"]), 2)
                for name in CRITERIA if any(name in row["scores"] for row in judged)}
    return {
        "variants": dict(variants or {}), "stories": len(rows), "judged": len(judged),
        "engagement": round(statistics.mean(row["engagement"] for row in judged), 2) if judged else None,
        "criteria": criteria, "rows": rows,
    }


def calibrate(stories=30, seed=0, engine_id=None, alpha=None, apply=False, workers=4, judge=None, progress=None):
    """Fit the fitness to the judge: play and judge ``stories`` stories, fit the judged
    engagement on the fitness terms (evaluator/calibrate_fitness.py) and suggest
    ``fitness.term_weights``. {term_weights, terms, spearman, cv_spearman (how well the fit
    predicts stories it was not fitted on), alpha, n, yaml, applied}. ``apply``: the
    suggested weights are saved as fitness.term_weights."""
    from evaluator.calibrate_fitness import fit, to_yaml

    if int(stories) < 10:
        raise EvaluationError("Calibrating needs at least 10 judged stories.")
    judge = judge or _judge()
    played = play_stories(stories, seed, engine_id)
    step = _progress_step(progress, len(played))

    def judge_one(story):
        result = judge.judge_digest(story["digest"])
        step("stories judged")
        return result

    with ThreadPoolExecutor(max(1, min(8, int(workers)))) as pool:
        results = list(pool.map(judge_one, played))
    samples = [(story["breakdown"], result["engagement"]) for story, result in zip(played, results)
               if result.get("engagement") is not None]
    if len(samples) < 10:
        raise EvaluationError(f"Only {len(samples)} judgements could be read; at least 10 are needed.")
    result = fit(samples, alpha=alpha)
    weights = {name: term["multiplier"] for name, term in result["terms"].items()}
    applied = False
    if apply:
        from backend import store

        store.set_term_weights(weights)
        applied = True
    return {"term_weights": weights, "terms": result["terms"], "spearman": result["spearman"],
            "cv_spearman": result["cv_spearman"], "alpha": result["alpha"], "n": result["n"],
            "yaml": to_yaml(result), "applied": applied}
