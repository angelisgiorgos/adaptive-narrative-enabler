"""A language model as the judge of a story, from the view of a video game.

Gemma 4 (the shared runtime of utils/llm_model_loader.py: vLLM embedded or a vLLM
server) reads a compact digest of a finished story - every turn's place, choice,
outcome, check, event, change of health or coins, the story beats the player saw
(a quest step, a twist, a new path, an ally, an arrest, ...) and the choices passed
up - and scores it, as a game critic would, on eight criteria (1-10 each):

    agency, pacing, surprise, payoff, exploration, stakes, coherence, replayability

with an overall ``engagement`` (1-10), the best and the weakest moment and a one-line
verdict. Several samples (``llm_judge.samples``) are averaged, and their spread is kept
as a measure of how sure the judge is.

    judge = GameJudge()
    result = judge.judge_state(state)          # a GameState that was played
    result = judge.judge_turns(turns, meta)     # a recorded story (records.get_story)
    result["engagement"], result["scores"], result["verdict"]
    duel = judge.compare_states(state_a, state_b)   # which playthrough is the better game
    duel["winner"], duel["criteria"]                # "A", "B" or "tie" (both orders must agree)

Command line (a recorded story, by id):

    python -m evaluator.game_judge STORY_ID
"""
from __future__ import annotations

import json
import re
import statistics

from utils.config_loader import config

CRITERIA = {
    "agency": "the player's choices matter: real options with consequences, not one obvious path",
    "pacing": "tension and calm alternate; no stretches of filler; it builds toward a climax",
    "surprise": "unexpected but fair turns: events, twists and reversals a player would not predict",
    "payoff": "what is set up (clues, rumours, promises, threats) pays off later; events resolve",
    "exploration": "moving through a world of distinct, connected places worth discovering",
    "stakes": "risk and reward: danger, fights, costs, things to win or lose",
    "coherence": "places, people and events fit together without contradictions",
    "replayability": "a player would want to play it again and see it unfold differently",
}


def _settings():
    return {"samples": 1, "temperature": 0.3, "max_new_tokens": 700, "max_turns": 80, "outcome_chars": 160,
            "max_chars": 20000, "pair_max_chars": 11000, **(config.get("llm_judge") or {})}


# The lines of a turn's text a player notices as story beats (a quest step, a twist, a new
# path, an ally, an arrest, an encounter, an item taken, ...); the rest is already in the turn.
BEATS = ("[QUEST]", "[MISSION COMPLETE]", "[UNEXPECTED EVENT]", "[TWIST]", "[CALLBACK]", "[ALLY]",
         "[SPAWN] New path", "[LEAD]", "[SENT]", "[ESCAPE]", "[FINALE]", "[ENCOUNTER]", "[EVENT] ",
         "[NPC REMOVED]", "[RELEASED]", "[UNLOCKED]", "[BACK]", "[ROUTE]", "[EVENT DISCOVERY]", "You collected",
         # The engine's own beats (core_engine/story_beats.py).
         "[REVEAL]", "[TRIUMPH]", "[CLIMAX]", "[SETBACK]", "[REVELATION]", "[ANOTHER WAY]", "[REFUND]",
         "[ON THE WAY]", "[ALERT]", "[MANHUNT]", "[SECRET]", "[CONSEQUENCE]", "[LAST TIME]")


def _beats(text, limit=4, chars=150):
    found = []
    for line in (text or "").splitlines():
        line = line.strip().lstrip(">").strip()
        if line.startswith(BEATS):
            found.append(line if len(line) <= chars else line[:chars - 3].rstrip() + "...")
    return found[:limit]


# ---- the story, as the judge reads it --------------------------------------------------

def _check_text(check):
    if isinstance(check, str):
        try:
            check = json.loads(check)
        except ValueError:
            return ""
    if not isinstance(check, dict) or not check.get("stat"):
        return ""
    return f"{check['stat']} check {'passed' if check.get('passed') else 'failed'}"


def digest_turns(turns, meta=None, max_turns=None, outcome_chars=None, max_chars=None):
    """The story as numbered lines, one per turn: place, choice, outcome, and what changed.
    ``turns``: a GameState's turn_log or a recorded story's turn_records; ``meta``: mission,
    start_location, end_kind, end_reason, won. Long stories keep their beginning and end."""
    cfg = _settings()
    max_turns = int(max_turns or cfg["max_turns"])
    outcome_chars = int(outcome_chars or cfg["outcome_chars"])
    meta = meta or {}
    lines = []
    if meta.get("mission"):
        lines.append(f"Mission: {meta['mission']}")
    if meta.get("start_location"):
        lines.append(f"Start: {meta['start_location']}")
    lines.append(f"Turns played: {len(turns)}")
    rows, previous = [], None
    for number, turn in enumerate(turns, start=1):
        place = turn.get("location_before") or ""
        parts = [f"{number}. [{place}] {turn.get('action') or ''}"]
        outcome = (turn.get("told") or turn.get("outcome") or "").strip()  # as the player read it (variants)
        if outcome:
            parts.append(f"-> {outcome[:outcome_chars]}")
        notes = []
        check = _check_text(turn.get("check") if "check" in turn else turn.get("check_json"))
        if check:
            notes.append(check)
        elif turn.get("succeeded") in (0, False):
            notes.append("failed")
        if turn.get("event_after") and turn.get("event_after") != turn.get("event"):
            notes.append(f"event begins: {turn['event_after']}")
        elif turn.get("event") and not turn.get("event_after"):
            notes.append(f"event over: {turn['event']}")
        if turn.get("location_after") and turn.get("location_after") != place:
            notes.append(f"moves to {turn['location_after']}")
        if previous is not None:
            for key, label in (("health", "health"), ("coins", "coins")):
                if turn.get(key) is not None and previous.get(key) is not None and turn[key] != previous[key]:
                    notes.append(f"{label} {previous[key]}->{turn[key]}")
        if notes:
            parts.append(f"({'; '.join(notes)})")
        beats = _beats(turn.get("text"))
        if beats:
            parts.append("| " + " | ".join(beats))
        options = [str(o)[:40] for o in (turn.get("options") or []) if o != turn.get("action")][:3]
        if options:
            parts.append(f"[could also: {'; '.join(options)}]")
        rows.append(" ".join(parts))
        previous = turn
    budget = int(max_chars or _settings()["max_chars"])
    keep = max_turns
    while True:  # long stories keep their beginning and end, within max_turns and max_chars
        shown = rows
        if len(rows) > keep:
            head = keep * 2 // 3
            shown = rows[:head] + [f"... ({len(rows) - keep} turns not shown) ..."] + rows[-(keep - head):]
        if sum(len(row) + 1 for row in shown) <= budget or keep <= 20:
            break
        keep = min(keep, len(rows)) - 10
    lines += shown
    ending = meta.get("end_kind") or "unfinished"
    result = "won" if meta.get("won") else "not won"
    lines.append(f"Ending: {ending} ({result}). {(meta.get('end_reason') or '').strip()[:300]}")
    return "\n".join(lines)


def digest_state(state, **kwargs):
    """The digest of a played GameState (each turn with its story text, for the beats)."""
    meta = {
        "mission": f"{state.mission.title} - {state.mission.description}" if state.mission else None,
        "start_location": state.start_location, "end_kind": state.end_kind, "end_reason": state.end_reason,
        "won": state.won,
    }
    turns = [{**turn, "text": state.turn_text(turn)} for turn in getattr(state, "turn_log", None) or []]
    return digest_turns(turns, meta, **kwargs)


def build_prompt(digest):
    criteria = "\n".join(f"- {name}: {meaning}" for name, meaning in CRITERIA.items())
    keys = ", ".join(f'"{name}": <1-10>' for name in CRITERIA)
    return f"""You are a demanding video game critic. Below is the log of one playthrough of a narrative
game: each line is a turn (the place, the player's choice, what happened, and what changed).

Judge it as a GAME a player experiences, not as prose. Score each criterion from 1 (bad) to 10
(excellent); use the whole scale and reserve 8-10 for genuinely strong play:
{criteria}

Then give "engagement": how engaging the playthrough is overall for a player, from 1 to 10.

Reply with ONE JSON object and nothing else:
{{"scores": {{{keys}}}, "engagement": <1-10>, "best_moment": "Turn <n>: <why it works>",
"weakest_moment": "Turn <n>: <why it falls flat>", "verdict": "<one sentence>"}}

PLAYTHROUGH:
{digest}
"""


# ---- what the judge says -----------------------------------------------------------------

def _clamp(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(1.0, min(10.0, number))


def parse_judgement(text):
    """The judgement in a model's reply: {scores, engagement, best_moment, weakest_moment,
    verdict}, or None. Tolerates code fences, prose around the JSON, trailing commas and
    scores given as "criterion: 7" lines."""
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text)
    data = None
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        candidate = cleaned[start:end + 1]
        for attempt in (candidate, re.sub(r",\s*([\]}])", r"\1", candidate)):
            try:
                data = json.loads(attempt)
                break
            except ValueError:
                continue
    scores = {}
    if isinstance(data, dict):
        raw = data.get("scores") if isinstance(data.get("scores"), dict) else data
        for name in CRITERIA:
            value = _clamp(raw.get(name))
            if value is not None:
                scores[name] = value
    if len(scores) < len(CRITERIA):  # salvage "pacing: 7" / "\"pacing\": 7" lines
        for name in CRITERIA:
            if name not in scores:
                match = re.search(rf'"?{name}"?\s*[:=]\s*(\d+(?:\.\d+)?)', cleaned, flags=re.I)
                if match:
                    scores[name] = _clamp(match.group(1))
    if not scores:
        return None
    engagement = _clamp((data or {}).get("engagement")) if isinstance(data, dict) else None
    if engagement is None:
        match = re.search(r'"?engagement"?\s*[:=]\s*(\d+(?:\.\d+)?)', cleaned, flags=re.I)
        engagement = _clamp(match.group(1)) if match else statistics.mean(scores.values())
    return {
        "scores": scores, "engagement": engagement,
        "best_moment": str((data or {}).get("best_moment", "")) if isinstance(data, dict) else "",
        "weakest_moment": str((data or {}).get("weakest_moment", "")) if isinstance(data, dict) else "",
        "verdict": str((data or {}).get("verdict", "")) if isinstance(data, dict) else "",
    }


# ---- pairwise: which of two playthroughs is the better game --------------------------------

def build_pairwise_prompt(digest_a, digest_b):
    """A side-by-side comparison of two playthroughs (A and B) of the same game."""
    criteria = "\n".join(f"- {name}: {meaning}" for name, meaning in CRITERIA.items())
    keys = ", ".join(f'"{name}": "A"|"B"|"tie"' for name in CRITERIA)
    return f"""You are a demanding video game critic. Below are the logs of two playthroughs, A and B,
of the same narrative game (each line is a turn: the place, the player's choice, what happened,
and what changed). Judge them as GAMES a player experiences, not as prose.

For each criterion say which playthrough is better, or "tie" when neither clearly is:
{criteria}

Then say which playthrough is more engaging overall for a player ("winner").

Reply with ONE JSON object and nothing else:
{{"criteria": {{{keys}}}, "winner": "A"|"B"|"tie", "reason": "<one sentence>"}}

PLAYTHROUGH A:
{digest_a}

PLAYTHROUGH B:
{digest_b}
"""


def _choice(value):
    text = str(value or "").strip().strip('"').strip().upper()
    if text in ("A", "B"):
        return text
    if text in ("TIE", "EQUAL", "NEITHER", "BOTH", "DRAW"):
        return "tie"
    return None


def parse_pairwise(text):
    """{winner, criteria, reason} in a model's reply, or None. Tolerates code fences, prose
    around the JSON, trailing commas and "criterion: A" lines."""
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text)
    data = None
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        candidate = cleaned[start:end + 1]
        for attempt in (candidate, re.sub(r",\s*([\]}])", r"\1", candidate)):
            try:
                data = json.loads(attempt)
                break
            except ValueError:
                continue
    criteria = {}
    if isinstance(data, dict):
        raw = data.get("criteria") if isinstance(data.get("criteria"), dict) else data
        for name in CRITERIA:
            choice = _choice(raw.get(name))
            if choice is not None:
                criteria[name] = choice
    for name in CRITERIA:  # salvage "pacing: A" lines
        if name not in criteria:
            match = re.search(rf'"?{name}"?\s*[:=]\s*"?(A|B|tie)\b', cleaned, flags=re.I)
            if match:
                criteria[name] = _choice(match.group(1))
    winner = _choice(data.get("winner")) if isinstance(data, dict) else None
    if winner is None:
        match = re.search(r'"?winner"?\s*[:=]\s*"?(A|B|tie)\b', cleaned, flags=re.I)
        winner = _choice(match.group(1)) if match else None
    if winner is None and not criteria:
        return None
    return {"winner": winner or "tie", "criteria": criteria,
            "reason": str(data.get("reason", "")) if isinstance(data, dict) else ""}


def _swap(choice):
    return {"A": "B", "B": "A"}.get(choice, choice)


def combine_orders(first, second):
    """The verdict of two judgements of one pair, A shown first and B shown first (``second``
    already speaks of the swapped labels): a winner only where both orders agree once mapped
    back, otherwise a tie. This cancels the judge's preference for a position."""
    if first is None and second is None:
        return None
    if first is None or second is None:
        single = first if first is not None else {**second, "winner": _swap(second["winner"]),
                                                   "criteria": {k: _swap(v) for k, v in second["criteria"].items()}}
        return {"winner": "tie", "criteria": {k: "tie" for k in single["criteria"]},
                "reason": single["reason"], "agreed": False}
    back = {"winner": _swap(second["winner"]), "criteria": {k: _swap(v) for k, v in second["criteria"].items()}}
    winner = first["winner"] if first["winner"] == back["winner"] else "tie"
    criteria = {name: (first["criteria"][name] if first["criteria"].get(name) == back["criteria"].get(name) else "tie")
                for name in CRITERIA if name in first["criteria"] or name in back["criteria"]}
    return {"winner": winner, "criteria": criteria, "reason": first["reason"] or second["reason"],
            "agreed": first["winner"] == back["winner"]}


class GameJudge:
    """Gemma 4 as the judge of a playthrough (llm_judge settings; the vllm runtime)."""

    def __init__(self, runtime=None, samples=None, temperature=None, max_new_tokens=None):
        cfg = _settings()
        self.samples = max(1, int(samples or cfg["samples"]))
        self.temperature = float(cfg["temperature"] if temperature is None else temperature)
        self.max_new_tokens = int(max_new_tokens or cfg["max_new_tokens"])
        if runtime is None:
            from utils.llm_model_loader import get_llm_runtime

            runtime = get_llm_runtime()
        self.runtime = runtime

    def judge_digest(self, digest):
        """{engagement, scores, spread, samples, verdict, best_moment, weakest_moment, parsed}:
        the mean of the samples that could be read (parsed: how many)."""
        prompt = build_prompt(digest)
        judgements, raw = [], []
        for _ in range(self.samples):
            text = self.runtime.generate(prompt, max_new_tokens=self.max_new_tokens, temperature=self.temperature)
            raw.append(text)
            parsed = parse_judgement(text)
            if parsed is not None:
                judgements.append(parsed)
        if not judgements:
            return {"engagement": None, "scores": {}, "spread": None, "samples": self.samples, "parsed": 0,
                    "verdict": "The judge's reply could not be read.", "raw": raw}
        scores = {name: statistics.mean(j["scores"][name] for j in judgements if name in j["scores"])
                  for name in CRITERIA if any(name in j["scores"] for j in judgements)}
        engagements = [j["engagement"] for j in judgements]
        return {
            "engagement": statistics.mean(engagements),
            "scores": scores,
            "spread": statistics.pstdev(engagements) if len(engagements) > 1 else 0.0,
            "samples": self.samples, "parsed": len(judgements),
            "verdict": judgements[0]["verdict"], "best_moment": judgements[0]["best_moment"],
            "weakest_moment": judgements[0]["weakest_moment"], "raw": raw,
        }

    def compare_digests(self, digest_a, digest_b):
        """Which of two playthroughs is the better game: {winner, criteria, reason, orders,
        agreed}, winner "A", "B" or "tie". Both orders are judged (A first, then B first) and
        a side wins only where they agree, so the judge's preference for a position cancels."""
        orders = []
        for first, second, swapped in ((digest_a, digest_b, False), (digest_b, digest_a, True)):
            text = self.runtime.generate(build_pairwise_prompt(first, second),
                                         max_new_tokens=self.max_new_tokens, temperature=self.temperature)
            orders.append({"swapped": swapped, "raw": text, "parsed": parse_pairwise(text)})
        combined = combine_orders(orders[0]["parsed"], orders[1]["parsed"])
        if combined is None:
            return {"winner": None, "criteria": {}, "reason": "The judge's replies could not be read.",
                    "orders": orders, "agreed": False}
        return {**combined, "orders": orders}

    def compare_states(self, state_a, state_b):
        # Two digests share one prompt: each gets llm_judge.pair_max_chars (the model's context holds both).
        budget = int(_settings()["pair_max_chars"])
        return self.compare_digests(digest_state(state_a, max_chars=budget), digest_state(state_b, max_chars=budget))

    def judge_state(self, state):
        return self.judge_digest(digest_state(state))

    def judge_turns(self, turns, meta=None):
        return self.judge_digest(digest_turns(turns, meta))


def judge_recorded_story(story_id, judge=None):
    """Judge a story of the records database (backend/records.py) by its id."""
    from backend import records

    story = records.get_story(story_id)
    meta = {key: story.get(key) for key in ("mission", "start_location", "end_kind", "end_reason", "won")}
    return (judge or GameJudge()).judge_turns(story["turn_records"], meta)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        sys.exit("python -m evaluator.game_judge STORY_ID")
    outcome = judge_recorded_story(sys.argv[1])
    outcome.pop("raw", None)
    print(json.dumps(outcome, indent=2))
