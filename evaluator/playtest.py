"""Gameplay playtests: reproducible scenarios played by simulated players, measured and checked.

A **scenario** fixes everything a playthrough depends on: the world (the repository's, a world-only
YAML file such as examples/frozen_monastery.yaml) or a saved engine (an engine model file: its own
world, tuning and storyteller), the storyteller (the engine's, explicit traits, or a random one drawn
from ``storyteller_seed``), the player strategy (core_engine/players.py: STRATEGIES), the story seed,
the starting place, the quest and the number of turns. The same scenario always plays the same story.

    from evaluator import playtest
    scenario = playtest.Scenario(name="monastery-sidequester", world="examples/frozen_monastery.yaml",
                                 player="side_quester", seed=3, max_turns=40)
    run = playtest.run_scenario(scenario)        # a Playthrough: the state, its turns and decisions
    run.metrics                                  # automated measures (never the judge's)
    run.issues                                   # softlocks, impossible objectives, contradictions, ...
    run.weak_segments                            # stretches an author should look at, with what to edit

Everything here is **automated**: counts and checks of what the engine recorded (``metrics``,
``issues``, ``fitness``). The language-model judge's view (``judge``: engagement, coherence, choice
consequences, character development, exploration, replayability) is subjective, optional (it needs
the judge's server: docs/LLM.md) and kept apart in every result and report.

Nothing here decides for the game: the engine plays, the player strategy chooses among the choices
the engine shows (as in training: core_engine/players.take_turn), and the measures only read what
happened. No training is run.

Command line (a scenario file: evaluator/scenarios/*.yaml):

    python -m evaluator.playtest evaluator/scenarios/side_content.yaml --out playtest-out
    python -m evaluator.playtest evaluator/scenarios/side_content.yaml --compare-genome modified.json
    python -m evaluator.playtest SCENARIOS --judge         # also ask the judge (its server must run)

It writes ``report.md`` (tables, failure examples, recommendations), ``playtest.json`` (every
playthrough, turn by turn) and ``replay.html`` (the visual replay: evaluator/replay.py).
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import itertools
import json
import random
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# What a playtest checks, with its default thresholds (a scenario file's ``checks`` changes them).
DEFAULT_CHECKS = {
    "stall_turns": 12,            # this many turns in a row with nothing new and the quest not done: a stall
    "unreachable_turns": 5,       # the quest out of reach this many turns in a row: a softlock
    "filler_segment": 4,          # a stretch of this many filler turns is a weak segment
    "repeat_action": 4,           # the same action at the same place this often: a loop
    "encounter_same_max": 2,      # the same attack more often than this: excessive repetition
    "encounters_per_10_turns": 3.0,   # more attacks than this per 10 turns: excessive
    "encounter_min_gap": 2,       # two attacks closer than this (turns): back to back
}

# The judge's view, as a playtest names it -> the criterion the judge scores (evaluator/game_judge.py).
JUDGE_ASPECTS = {"engagement": "engagement", "coherence": "coherence", "choice_consequences": "agency",
                 "character_development": "character_development", "exploration": "exploration",
                 "replayability": "replayability"}

ENTITY_KINDS = {"location": "Locations", "npc": "NPCs", "item": "Items", "event": "Events"}


# ---- scenarios ---------------------------------------------------------------------------------------

@dataclasses.dataclass
class Scenario:
    """One reproducible playthrough. ``world``: ``city`` (the repository's configuration) or a world-only
    YAML file; ``engine``: an engine model file (its world and storyteller; ``world`` is then ignored);
    ``genome``: storyteller traits (a mapping) instead of the engine's or a random one;
    ``storyteller_seed``: the seed of a random storyteller (default: ``seed``); ``player``: a strategy
    (core_engine/players.STRATEGIES or STRATEGY_ALIASES); ``start`` / ``quest``: the starting place and
    mission key (None: the story chooses); ``overrides``: configuration settings changed for this
    scenario ({"side_story.enabled": false}, dotted paths)."""

    name: str
    world: str = "city"
    engine: str | None = None
    genome: dict | None = None
    storyteller_seed: int | None = None
    player: str = "quester"
    seed: int = 1
    start: str | None = None
    quest: str | None = None
    max_turns: int = 40
    overrides: dict = dataclasses.field(default_factory=dict)
    variant: str = "baseline"

    def describe(self):
        return {field.name: getattr(self, field.name) for field in dataclasses.fields(self)}


def expand(spec, base_dir=None):
    """The scenarios of a scenario document: {defaults: {...}, checks: {...}, scenarios: [{...}]}. A
    scenario may give lists for ``player`` and ``seed``: one scenario per combination (named
    ``<name>-<player>-s<seed>``). Relative ``world`` / ``engine`` paths are read from ``base_dir`` first,
    then from the repository."""
    defaults = dict(spec.get("defaults") or {})
    found = []
    for raw in spec.get("scenarios") or []:
        item = {**defaults, **raw}
        players = item.pop("player", "quester")
        seeds = item.pop("seed", 1)
        players = players if isinstance(players, list) else [players]
        seeds = seeds if isinstance(seeds, list) else [seeds]
        for key in ("world", "engine"):
            if item.get(key) and item[key] != "city":
                item[key] = _resolve(item[key], base_dir)
        for player, seed in itertools.product(players, seeds):
            name = item.get("name", "scenario")
            if len(players) > 1 or len(seeds) > 1:
                name = f"{name}-{player}-s{seed}"
            found.append(Scenario(**{**item, "name": name, "player": player, "seed": int(seed)}))
    return found


def load(path):
    """(scenarios, checks) of a scenario file (YAML or JSON)."""
    import yaml

    path = Path(path)
    spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return expand(spec, path.parent), {**DEFAULT_CHECKS, **(spec.get("checks") or {})}


def _resolve(value, base_dir):
    for folder in ([Path(base_dir)] if base_dir else []) + [ROOT]:
        candidate = folder / value
        if candidate.exists():
            return str(candidate)
    return str(value)


def _set_path(data, dotted, value):
    node = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def scenario_config(scenario):
    """(complete configuration, storyteller traits or None, tag vectors) of a scenario."""
    vectors, traits = None, None
    if scenario.engine:
        from core_engine.model import EngineModel

        model = EngineModel.load(scenario.engine)
        data, traits, vectors = copy.deepcopy(model.config), dict(model.genome_traits), model.tag_vectors
    elif scenario.world in (None, "", "city"):
        data = _process_config()
    else:
        import yaml

        from backend.store import complete_config

        data, errors, _ = complete_config(yaml.safe_load(Path(scenario.world).read_text(encoding="utf-8")))
        if errors:
            raise ValueError(f"{scenario.world}: " + "; ".join(errors[:5]))
    for dotted, value in (scenario.overrides or {}).items():
        _set_path(data, dotted, value)
    if scenario.genome:
        traits = {k: float(v) for k, v in scenario.genome.items()}
    return data, traits, vectors


def _process_config():
    from utils.config_loader import config

    return copy.deepcopy(config.as_dict())


def new_game(scenario):
    """(GameState, EngineContext) of a scenario, before its first turn."""
    from core_engine import GameState, Genome
    from core_engine.builder import build_world
    from core_engine.context import EngineContext

    data, traits, vectors = scenario_config(scenario)
    if traits is not None:
        genome = Genome.from_traits(traits)
    else:
        genome = Genome(rng=random.Random(scenario.seed if scenario.storyteller_seed is None
                                          else scenario.storyteller_seed))
    context = EngineContext(data, genome=genome, tag_vectors=vectors, seed=scenario.seed,
                            source=f"playtest {scenario.name}")
    with context.bind():
        world = build_world(genome)
        state = GameState(world, genome, rng=random.Random(scenario.seed), start=scenario.start,
                          mission=scenario.quest, context=context)
    return state, context


# ---- playing ---------------------------------------------------------------------------------------

class _Recorder:
    """A player strategy whose every decision is kept: the choices shown (with their stable ids), the one
    taken, where and whether it took a turn. It decides nothing itself."""

    def __init__(self, player):
        self.player = player
        self.name = player.name
        self.decisions = []

    def choose(self, state, options):
        from core_engine import choices

        index = self.player.choose(state, options)
        ids = choices.assign_ids(options, state)
        self.decisions.append({
            "turn": state.actions_taken + 1, "where": state.current.name,
            "talking_to": state.__dict__.get("talking_to"),
            "options": [{"id": ids[i], "name": o.name, "source": o.source,
                         "owner": getattr(o, "owner", None) or getattr(o, "character_name", None)}
                        for i, o in enumerate(options)],
            "chosen": index, "took_turn": None,
        })
        return index


@dataclasses.dataclass
class Playthrough:
    scenario: Scenario
    state: object
    context: object
    decisions: list
    stopped: str
    journal: list = dataclasses.field(default_factory=list)
    texts: list = dataclasses.field(default_factory=list)
    metrics: dict = dataclasses.field(default_factory=dict)
    issues: list = dataclasses.field(default_factory=list)
    weak_segments: list = dataclasses.field(default_factory=list)
    fitness: dict = dataclasses.field(default_factory=dict)
    judge: dict | None = None
    removed_at: dict = dataclasses.field(default_factory=dict)   # unique NPC -> the turn they left the story
    ally_since: dict = dataclasses.field(default_factory=dict)   # NPC -> the turn they became an ally
    reachable: dict = dataclasses.field(default_factory=dict)    # turn -> quest_reachable after it
    world_issues: list = dataclasses.field(default_factory=list)  # world_checks: the world's, not this story's

    def to_dict(self):
        """Everything a report and the replay need, JSON-ready (the state itself is left out)."""
        state = self.state
        with self.context.bind():
            quest = state.quest_log()
            met = state.__dict__.get("met") or {}
            people = [{**person, "first_turn": (met.get(person["name"]) or {}).get("first_turn")}
                      for person in state.people_met()]  # first_turn 0: met in the opening scene
            from core_engine import side_quests

            side = side_quests.log(state)
        return {
            "scenario": self.scenario.describe(), "stopped": self.stopped,
            "start": state.start_location, "quest": quest, "side_quests": side, "people": people,
            "turns": [{**entry, "text": text} for entry, text in zip(self.journal, self.texts)],
            "decisions": self.decisions, "metrics": self.metrics, "issues": self.issues,
            "weak_segments": self.weak_segments, "world_issues": self.world_issues,
            "automated": {"fitness": self.fitness},
            "subjective": {"judge": self.judge},
        }


def play(scenario):
    """Play a scenario: the engine runs the story and the scenario's player strategy chooses among the
    choices it shows (core_engine/players.take_turn). Returns a Playthrough (not yet measured)."""
    from core_engine import players

    state, context = new_game(scenario)
    recorder = _Recorder(players.strategy(scenario.player, scenario.seed))
    stopped, free = "max_turns", 0
    removed_at, ally_since = {}, {name: 0 for name in state.__dict__.get("allies") or ()}
    unique = {c.name for c in state.world.characters if getattr(c, "unique", True)}
    reachable = {}  # turn -> whether a route leads from where the player is to where the quest advances
    with context.bind():
        world_issues = world_checks(state)
        while state.actions_taken < scenario.max_turns:
            if state.health <= 0:
                stopped = "dead"
                break
            before, decided = state.actions_taken, len(recorder.decisions)
            went_on = players.take_turn(state, recorder)
            if len(recorder.decisions) > decided:
                recorder.decisions[-1]["took_turn"] = state.actions_taken > before
            for name in (state.removed_npcs & unique) - set(removed_at):
                removed_at[name] = state.actions_taken
            for name in set(state.__dict__.get("allies") or ()) - set(ally_since):
                ally_since[name] = state.actions_taken
            if state.actions_taken > before:
                reachable[state.actions_taken] = quest_reachable(state)
            if not went_on:
                stopped = "ended" if state.ended else "no_choices"
                break
            free = free + 1 if state.actions_taken == before else 0
            if free >= 50:
                stopped = "free_choice_loop"
                break
        if state.ended and stopped == "max_turns":
            stopped = "ended"
        journal = state.journal()
        texts = [state.turn_text(entry) for entry in state.__dict__.get("turn_log") or []]
    return Playthrough(scenario, state, context, recorder.decisions, stopped, journal, texts,
                       removed_at=removed_at, ally_since=ally_since, reachable=reachable,
                       world_issues=world_issues)


def run_scenario(scenario, checks=None, judge=None):
    """Play, measure and check one scenario (``judge``: a GameJudge, or None to leave it out)."""
    run = play(scenario)
    measure(run, checks)
    if judge is not None:
        run.judge = judge_playthrough(run, judge)
    return run


# ---- measuring (automated) ---------------------------------------------------------------------------

def _new_content(journal):
    """For each turn: whether it brought something new (a place, a check, a change of health, coins or
    items, someone met, quest progress, a side-quest moment, an attack, an event or an outcome not told
    before) - the director's own sense of "new" (GameState._update_rhythm). A turn without: filler."""
    told, events, flags = set(), set(), []
    previous = None
    for entry in journal:
        new = bool(entry.get("new_places") or entry.get("check") or entry.get("items_gained")
                   or entry.get("items_lost") or entry.get("met") or entry.get("quest_done")
                   or entry.get("quest_complete") or entry.get("side_quests") or entry.get("encounter"))
        if previous is not None and (entry.get("health") != previous.get("health")
                                     or entry.get("coins") != previous.get("coins")):
            new = True
        if entry.get("event") and entry["event"] not in events:
            events.add(entry["event"])
            new = True
        if entry.get("told") and entry["told"] not in told:
            told.add(entry["told"])
            new = True
        flags.append(new)
        previous = entry
    return flags


def _streaks(flags):
    """[(first index, length)] of the runs of False in ``flags``."""
    runs, start = [], None
    for index, flag in enumerate(list(flags) + [True]):
        if not flag and start is None:
            start = index
        elif flag and start is not None:
            runs.append((start, index - start))
            start = None
    return runs


def measure(run, checks=None):
    """Fill ``run.metrics``, ``run.fitness``, ``run.issues`` and ``run.weak_segments``."""
    checks = {**DEFAULT_CHECKS, **(checks or {})}
    state, journal = run.state, run.journal
    with run.context.bind():
        run.fitness = _fitness(state)
        run.metrics = _metrics(run, checks)
        run.issues = detect_issues(run, checks)
        run.weak_segments = weak_segments(run, checks)
    return run


def _fitness(state):
    """The training objective's view (automated): the total, its terms and what the story lacks."""
    from core_engine import fitness

    try:
        breakdown = fitness._fitness_breakdown(state)
        losses = fitness.objective_losses(state)
    except Exception as exc:  # a measure must never stop a playtest
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"total": round(sum(breakdown.values()), 3),
            "terms": {k: round(v, 3) for k, v in breakdown.items()},
            "objective_losses": {k: round(v, 3) for k, v in (losses or {}).items()},
            "version": fitness.version()}


def _metrics(run, checks):
    from core_engine import side_quests

    state, journal, decisions = run.state, run.journal, run.decisions
    turns = len(journal)
    flags = _new_content(journal)
    quest = state.quest_log() or {}
    stages = quest.get("stages") or []
    complete_turn = next((e["turn"] for e in journal if e.get("quest_complete")), None)

    side = side_quests.log(state)
    status = Counter(entry["status"] for entry in side)
    side_events = [ev for entry in journal for ev in entry.get("side_quests") or ()]
    started = sum(1 for ev in side_events if ev.get("what") == "started")

    attacks = list(state.__dict__.get("dynamic_encounters") or [])
    by_event = Counter(a.get("event") for a in attacks)
    gaps = [b["turn"] - a["turn"] for a, b in zip(attacks, attacks[1:])]

    taken = [d for d in decisions if d.get("took_turn")]
    meaningful = sum(1 for entry, new in zip(journal, flags) if new and _options_at(decisions, entry["turn"]) >= 2)

    places, seen, back, ping_pong = [state.start_location], {state.start_location}, 0, 0
    for entry in journal:
        moved = entry.get("moved_to")
        if not moved:
            continue
        if moved in seen:
            back += 1
        if len(places) >= 2 and moved == places[-2]:
            ping_pong += 1
        seen.add(moved)
        places.append(moved)

    repeats = Counter((e.get("where"), e.get("action")) for e in journal)
    repeated = {f"{where} · {action}": count for (where, action), count in repeats.most_common() if count > 1}

    people = state.people_met()
    talks = Counter(e["with"] for e in journal if e.get("with"))
    factions = {k: round(v, 3) for k, v in (state.__dict__.get("faction_shift") or {}).items()}
    contradictions = [issue for issue in _contradictions(run) if issue]

    filler_runs = _streaks(flags)
    return {
        "turns": turns, "stopped": run.stopped, "end_kind": state.end_kind, "end_reason": state.end_reason,
        "main_quest": {
            "key": quest.get("key"), "title": quest.get("title"), "completed": bool(state.won),
            # A quest without stages counts as one step.
            "stages_total": len(stages) or 1,
            "stages_done": sum(1 for s in stages if s["state"] == "done") if stages else int(bool(state.won)),
            "completed_at_turn": complete_turn,
        },
        "side_quests": {
            "offered": len(side), "accepted": started,
            "completed": status.get("completed", 0), "failed": status.get("failed", 0),
            "declined": status.get("declined", 0), "active": status.get("active", 0),
            "completion_rate": round(status.get("completed", 0) / started, 3) if started else None,
        },
        "encounters": {
            "count": len(attacks), "per_10_turns": round(10 * len(attacks) / turns, 2) if turns else 0.0,
            "distinct": len(by_event), "most_repeated": by_event.most_common(1)[0] if by_event else None,
            "min_gap": min(gaps) if gaps else None,
            "categories": dict(Counter(a.get("category") for a in attacks)),
        },
        "choices": {
            "decisions": len(decisions), "turn_decisions": len(taken),
            "mean_shown": round(statistics.mean(len(d["options"]) for d in decisions), 2) if decisions else 0.0,
            "meaningful": meaningful, "meaningful_share": round(meaningful / turns, 3) if turns else 0.0,
            # Turns with only one choice shown (an event's single answer, say): no real decision.
            "forced": sum(1 for d in taken if len(d["options"]) == 1),
        },
        "movement": {"moves": len(places) - 1, "places_visited": len(seen), "backtracks": back,
                     "ping_pong": ping_pong},
        "npcs": {"met": len(people), "talked_to": len(talks),
                 "allies": sorted(p["name"] for p in people if p.get("ally")),
                 "hostile": sorted(p["name"] for p in people if p.get("hostile")),
                 "gone": sorted(p["name"] for p in people if p.get("gone")),
                 "talks": dict(talks.most_common()), "faction_shift": factions},
        "repetition": {"repeated_actions": sum(c - 1 for c in repeats.values() if c > 1),
                       "max_same_action": max(repeats.values()) if repeats else 0,
                       "top": dict(list(repeated.items())[:5])},
        "filler": {"turns": flags.count(False), "share": round(flags.count(False) / turns, 3) if turns else 0.0,
                   "longest_streak": max((length for _, length in filler_runs), default=0)},
        "coherence": {
            "contradictions": len(contradictions),
            "transition_loss": round(((run.fitness or {}).get("objective_losses") or {}).get("transition_quality", 0.0), 3)
            if run.fitness else None,
        },
    }


def _options_at(decisions, turn):
    """How many choices the turn ``turn``'s decision showed (0 when none was recorded)."""
    for decision in decisions:
        if decision["turn"] == turn and decision.get("took_turn"):
            return len(decision["options"])
    return 0


# ---- checking ------------------------------------------------------------------------------------------

def _issue(code, severity, message, turn=None, elements=(), **detail):
    return {"code": code, "severity": severity, "message": message, "turn": turn,
            "elements": [element_link(*e) for e in elements], "detail": detail}


def element_link(kind, name):
    """Where an author edits a world element: the studio's World section, and the API entry.
    ``kind``: location, npc, item, event (entries), mission or side_quest (documents)."""
    if kind in ENTITY_KINDS:
        plural = ENTITY_KINDS[kind]
        from urllib.parse import quote

        return {"kind": kind, "name": name, "studio": f"?section=world (World → {plural} → {name})",
                "api": f"/v1/entities/{plural}/{quote(str(name), safe='')}"}
    document = {"mission": "missions", "side_quest": "side_quests"}.get(kind, kind)
    return {"kind": kind, "name": name, "studio": f"?section=world (World → {document.replace('_', ' ')} → {name})",
            "api": f"/v1/documents/{document}"}


def owner_kind(world, owner):
    """The kind of element an action belongs to (location, npc, item, event), or None."""
    if not owner:
        return None
    if owner in world.locations:
        return "location"
    if any(c.name == owner for c in world.characters):
        return "npc"
    if any(o.name == owner for o in world.objects):
        return "item"
    if owner in world.events or any(getattr(e, "title", None) == owner for e in world.events.values()):
        return "event"
    return None


def _contradictions(run):
    """What the story tells against itself: someone gone from the story who speaks again; a side quest
    told again after it ended; quest steps told after the quest was complete; an ally who attacks."""
    state, journal = run.state, run.journal
    found = []
    gone_at = run.removed_at  # unique people only: a non-unique one leaves the scene, not the story
    for entry in journal:
        speaker = entry.get("with")
        if speaker and speaker in gone_at and entry["turn"] > gone_at[speaker]:
            found.append(_issue("gone_npc_speaks", "error",
                                f"{speaker} left the story at turn {gone_at[speaker]} but speaks again at turn "
                                f"{entry['turn']} (\"{entry.get('action')}\").", entry["turn"], [("npc", speaker)]))
    ended = {}
    for entry in journal:
        for event in entry.get("side_quests") or ():
            key, what = event.get("quest"), event.get("what")
            if what in ("completed", "failed", "declined", "withdrawn"):
                ended.setdefault(key, (entry["turn"], what))
            elif key in ended and what in ("offered", "started") and entry["turn"] > ended[key][0]:
                found.append(_issue("side_quest_reopened", "error",
                                    f"Side quest `{key}` was {ended[key][1]} at turn {ended[key][0]} and is "
                                    f"{what} again at turn {entry['turn']}.", entry["turn"], [("side_quest", key)]))
    won_at = next((e["turn"] for e in journal if e.get("quest_complete")), None)
    if won_at is not None:
        for entry in journal:
            if entry["turn"] > won_at and entry.get("quest_done"):
                found.append(_issue("quest_step_after_completion", "error",
                                    f"The quest was complete at turn {won_at}, but turn {entry['turn']} completes "
                                    f"its step(s) {entry['quest_done']} again.", entry["turn"],
                                    [("mission", (state.quest_log() or {}).get("key"))]))
    for attack in state.__dict__.get("dynamic_encounters") or ():
        since = run.ally_since.get(attack.get("npc"))
        if attack.get("npc") and since is not None and since < attack["turn"]:
            found.append(_issue("ally_attacks", "warning",
                                f"{attack['npc']}, an ally, leads the attack `{attack['event']}` at turn {attack['turn']}.",
                                attack["turn"], [("npc", attack["npc"]), ("event", attack["event"])]))
    return found


def quest_reachable(state):
    """Whether the story's quest can still advance from where the player is: True when it is done or
    over, or its next step needs only coins, or a known or discoverable route leads to a place where it
    can advance (the engine's own planning: GameState._goal_sites, _route_distances). Reads only."""
    if state.mission is None or state.won or state.ended:
        return True
    try:
        pending = set(state._pending_goal())
        if not pending - {"coins"}:
            return True
        sites = frozenset(state._goal_sites())
        return bool(sites) and state.current.name in state._route_distances(sites)
    except Exception:  # the engine's planning helpers: a check must never stop a playtest
        return True


def world_checks(state):
    """What the world itself makes impossible, checked on the story before its first turn (``scope:
    world``: the same for every playthrough of that world): a quest or side-quest step naming a place,
    item, person, event or action the world does not have; a quest that no known or discoverable route
    lets one work toward from the start (the engine's GameState._mission_viable_from); a side-quest step
    no route leads to from the start; a side quest given by nobody."""
    world, issues = state.world, []
    actions = {a.name for loc in world.locations.values() for a in loc.actions} | \
              {a.name for c in world.characters for a in c.actions} | {a.name for o in world.objects for a in o.actions} | \
              {a.name for e in world.events.values() for a in getattr(e, "actions", None) or ()}
    outcomes = [o for loc in world.locations.values() for a in loc.actions for o in a.outcomes] + \
               [o for c in world.characters for a in c.actions for o in a.outcomes] + \
               [o for obj in world.objects for a in obj.actions for o in a.outcomes] + \
               [o for e in world.events.values() for a in getattr(e, "actions", None) or () for o in a.outcomes]
    tags = {str(t).lower() for o in outcomes for t in o.tags}
    people, items = {c.name for c in world.characters}, {o.name for o in world.objects}
    start = state.start_location

    def flag(code, severity, message, elements):
        issues.append({**_issue(code, severity, message, None, elements), "scope": "world"})

    def check(goal, kind, owner, step):
        where = f"{kind.replace('_', ' ')} `{owner}` step {step}"
        for key, value in (goal or {}).items():
            if key == "location" and value not in world.locations:
                flag("impossible_objective", "error", f"The {where} needs the place `{value}`, which the world "
                     "does not have.", [(kind, owner)])
            elif key == "location" and kind == "side_quest" and start not in state._route_distances(frozenset({value})):
                flag("impossible_objective", "error", f"The {where} needs `{value}`, but no known or discoverable "
                     f"route leads there from the start `{start}`.", [(kind, owner), ("location", value)])
            elif key == "item" and value not in items:
                flag("impossible_objective", "error", f"The {where} needs the item `{value}`, which the world does "
                     "not have.", [(kind, owner)])
            elif key == "npc_removed" and value not in people:
                flag("impossible_objective", "error", f"The {where} needs `{value}` gone, but no such person "
                     "exists.", [(kind, owner)])
            elif key in ("event", "event_succeeded") and value not in world.events:
                flag("impossible_objective", "error", f"The {where} needs the event `{value}`, which the world "
                     "does not have.", [(kind, owner)])
            elif key == "action" and value not in actions:
                flag("missing_required_action", "error", f"The {where} needs the action \"{value}\", which no "
                     "place, person, item or event offers.", [(kind, owner)])
            elif key == "outcome_tag" and str(value).lower() not in tags:
                flag("missing_required_action", "error", f"The {where} needs an outcome tagged `{value}`, which "
                     "no action's outcome has.", [(kind, owner)])

    for key, mission in world.missions.items():
        stages = getattr(mission, "stages", None) or []
        for number, stage in enumerate(stages or [{"goal": getattr(mission, "goal", {})}], start=1):
            check(stage.get("goal"), "mission", key, number)
        try:
            viable = state._mission_viable_from(mission, start)
        except Exception:
            viable = True
        if not viable:
            flag("quest_not_viable_from_start", "warning", f"The quest `{key}` cannot be worked toward from the "
                 f"start `{start}` (no known or discoverable route to where it advances, or its goal holds "
                 "already): the story never chooses it there.", [("mission", key), ("location", start)])
    for key, quest in (getattr(world, "side_quests", None) or {}).items():
        stages = getattr(quest, "stages", None) or []
        for number, stage in enumerate(stages or [{"goal": getattr(quest, "goal", {})}], start=1):
            check(stage.get("goal"), "side_quest", key, number)
        if quest.giver and quest.giver not in people:
            flag("impossible_objective", "error", f"Side quest `{key}` is given by `{quest.giver}`, who does not "
                 "exist.", [("side_quest", key)])
    return issues


def detect_issues(run, checks):
    """The problems of a playthrough: softlocks and stalls, impossible objectives and missing actions,
    contradictions, excessive encounter repetition and action loops."""
    state, journal = run.state, run.journal
    issues = []
    if run.stopped == "no_choices":
        last = journal[-1] if journal else {}
        issues.append(_issue("softlock", "error", f"The story stopped at turn {len(journal)} without an ending: no "
                             f"choice was offered at `{state.current.name}`.", len(journal) or None,
                             [("location", state.current.name)]))
    elif run.stopped == "free_choice_loop":
        issues.append(_issue("softlock", "error", "50 choices in a row took no turn (approach/leave or answers "
                             "without end).", len(journal), [("location", state.current.name)]))
    elif state.end_kind == "stalled":
        issues.append(_issue("softlock", "error", f"The engine ended the story as stalled: {state.end_reason}",
                             len(journal), [("location", state.current.name)]))
    flags = _new_content(journal)
    for first, length in _streaks(flags):
        if length >= checks["stall_turns"] and not state.won:
            where = journal[first].get("where")
            issues.append(_issue("stall", "warning", f"{length} turns in a row (turns {first + 1}-{first + length}) "
                                 "brought nothing new while the quest was not done.", first + 1,
                                 [("location", where)] if where else ()))
    unreachable = [not run.reachable.get(entry["turn"], True) for entry in journal]
    for first, length in _streaks([not flag for flag in unreachable]):
        if length >= checks["unreachable_turns"]:
            where = journal[first].get("moved_to") or journal[first].get("where")
            issues.append(_issue("objective_unreachable", "error",
                                 f"For {length} turns in a row (turns {first + 1}-{first + length}) no known or "
                                 f"discoverable route led to where the quest advances (from `{where}` on).",
                                 first + 1, [("mission", state.mission.name if state.mission else None),
                                             ("location", where)]))
    issues += [issue for issue in _contradictions(run)]
    attacks = list(state.__dict__.get("dynamic_encounters") or [])
    for event, count in Counter(a.get("event") for a in attacks).items():
        if count > checks["encounter_same_max"]:
            issues.append(_issue("encounter_repetition", "warning",
                                 f"The attack `{event}` came {count} times (more than {checks['encounter_same_max']}).",
                                 next(a["turn"] for a in attacks if a.get("event") == event), [("event", event)]))
    turns = max(1, len(journal))
    if len(attacks) * 10 / turns > checks["encounters_per_10_turns"] and len(attacks) >= 3:
        issues.append(_issue("encounter_frequency", "warning",
                             f"{len(attacks)} attacks in {turns} turns ({10 * len(attacks) / turns:.1f} per 10 turns, "
                             f"limit {checks['encounters_per_10_turns']}).", None,
                             [("event", a.get("event")) for a in attacks[:3]]))
    for a, b in zip(attacks, attacks[1:]):
        if b["turn"] - a["turn"] < checks["encounter_min_gap"]:
            issues.append(_issue("encounters_back_to_back", "warning",
                                 f"Attacks `{a['event']}` (turn {a['turn']}) and `{b['event']}` (turn {b['turn']}) "
                                 "came back to back.", b["turn"], [("event", a["event"]), ("event", b["event"])]))
    repeats = Counter((e.get("where"), e.get("action")) for e in journal)
    for (where, action), count in repeats.items():
        if count >= checks["repeat_action"]:
            turn = next(e["turn"] for e in journal if (e.get("where"), e.get("action")) == (where, action))
            owner = next((e.get("with") for e in journal if (e.get("where"), e.get("action")) == (where, action)
                          and e.get("with")), None)
            elements = [("npc", owner)] if owner else [("location", where)]
            issues.append(_issue("action_loop", "warning", f"\"{action}\" at `{where}` was taken {count} times.",
                                 turn, elements))
    return issues


def weak_segments(run, checks):
    """Stretches of the playthrough an author should look at, with why and what to edit: filler runs,
    action loops, failed checks that changed nothing, and attack clusters."""
    from core_engine.story_beats import failure_lint

    state, journal = run.state, run.journal
    world, segments = state.world, []
    flags = _new_content(journal)
    for first, length in _streaks(flags):
        if length < checks["filler_segment"]:
            continue
        stretch = journal[first:first + length]
        places = Counter(e.get("where") for e in stretch)
        actions = Counter(e.get("action") for e in stretch)
        place = places.most_common(1)[0][0]
        elements = [("location", place)]
        owners = {e.get("with") for e in stretch if e.get("with")}
        elements += [("npc", name) for name in sorted(owners)]
        segments.append({
            "kind": "filler", "turns": [first + 1, first + length],
            "why": f"{length} turns brought nothing new: no new place, person, check, item, quest progress or "
                   f"outcome. Most of it at `{place}` ({', '.join(f'{a} ×{c}' for a, c in actions.most_common(3))}).",
            "suggestion": f"Give `{place}` more to discover (new outcomes for its actions, an event, a person with a "
                          "lead toward the quest), or connect it to a place the quest needs.",
            "elements": [element_link(*e) for e in elements],
        })
    dead_ends = {(item["owner"], item["action"]): item["why"] for item in failure_lint(world)}
    for entry in journal:
        if entry.get("succeeded") is False or (entry.get("check") and not entry["check"].get("passed")):
            owner = entry.get("with") or entry.get("where")
            why = dead_ends.get((owner, entry.get("action")))
            if why is None:
                continue
            kind = owner_kind(world, owner) or "location"
            segments.append({
                "kind": "dead_end_failure", "turns": [entry["turn"], entry["turn"]],
                "why": f"Turn {entry['turn']}: \"{entry.get('action')}\" failed and {why} (it leads nowhere).",
                "suggestion": "Give its failure a consequence: an outcome that moves, reveals or sets something up, or "
                              "a complication event sharing a tag with it (docs/WORLD.md: failures).",
                "elements": [element_link(kind, owner)],
            })
    repeats = Counter((e.get("where"), e.get("action")) for e in journal)
    for (where, action), count in repeats.items():
        if count >= checks["repeat_action"]:
            turns = [e["turn"] for e in journal if (e.get("where"), e.get("action")) == (where, action)]
            owner = next((e.get("with") for e in journal if (e.get("where"), e.get("action")) == (where, action)
                          and e.get("with")), None)
            kind, name = ("npc", owner) if owner else ("location", where)
            segments.append({
                "kind": "loop", "turns": [turns[0], turns[-1]], "at": turns,  # the turns it was taken
                "why": f"\"{action}\" at `{where}` was taken {count} times (turns {', '.join(map(str, turns))}).",
                "suggestion": "Limit it (`repeat`), give it outcomes that change with each try, or make what it leads "
                              "to clearer, so a player has a reason to move on.",
                "elements": [element_link(kind, name)],
            })
    return segments


# ---- the judge (subjective) --------------------------------------------------------------------------

def judge_playthrough(run, judge):
    """The judge's view of a playthrough, by aspect (JUDGE_ASPECTS), kept apart from the measures:
    {aspects: {aspect: 1-10 or None}, engagement, verdict, best_moment, weakest_moment, spread, parsed}."""
    with run.context.bind():
        result = judge.judge_state(run.state)
    scores = dict(result.get("scores") or {})
    scores["engagement"] = result.get("engagement")
    return {"aspects": {aspect: scores.get(key) for aspect, key in JUDGE_ASPECTS.items()},
            "verdict": result.get("verdict"), "best_moment": result.get("best_moment"),
            "weakest_moment": result.get("weakest_moment"), "spread": result.get("spread"),
            "parsed": result.get("parsed"), "optional_content": result.get("optional_content")}


def playtest_judge(runtime=None):
    """The game judge asked also for the playtest's own criteria (character development)."""
    from evaluator.game_judge import PLAYTEST_CRITERIA, GameJudge

    return GameJudge(runtime=runtime, extra_criteria=PLAYTEST_CRITERIA)


# ---- comparing storytellers ----------------------------------------------------------------------------

COMPARED = {  # metric path -> higher is better (True), lower is better (False)
    "main_quest.completed": True, "main_quest.stages_done": True, "side_quests.completed": True,
    "choices.meaningful_share": True, "filler.share": False, "repetition.repeated_actions": False,
    "movement.backtracks": False, "encounters.count": None, "coherence.contradictions": False,
    "npcs.talked_to": True,
}


def _metric(metrics, path):
    value = metrics
    for part in path.split("."):
        value = (value or {}).get(part) if isinstance(value, dict) else None
    if isinstance(value, bool):
        return float(value)
    return value


def world_signature(state):
    """What makes two playthroughs' worlds the same game: the places, people, items, events, missions
    and side quests, by name."""
    world = state.world
    return {"locations": sorted(world.locations), "npcs": sorted(c.name for c in world.characters),
            "items": sorted(o.name for o in world.objects), "events": sorted(world.events),
            "missions": sorted(world.missions), "side_quests": sorted(getattr(world, "side_quests", None) or {})}


def compare(scenarios, modified, checks=None, judge=None):
    """Baseline against modified storyteller behaviour on the same scenarios (matched seeds, starts,
    quests, players). ``modified``: {genome: traits}, {engine: path}, {world: path}, {overrides: {...}}
    applied to each scenario. Pairs whose worlds differ (world_signature) are refused as incompatible.
    Returns {pairs: [{scenario, baseline, modified, compatible}], summary: {metric: {baseline, modified,
    delta, better}}, judge (pairwise, when a judge is given)}."""
    pairs, summary, duels = [], {}, []
    for scenario in scenarios:
        changed = dataclasses.replace(
            scenario, variant="modified", genome=modified.get("genome", scenario.genome),
            engine=modified.get("engine", scenario.engine), world=modified.get("world", scenario.world),
            overrides={**(scenario.overrides or {}), **(modified.get("overrides") or {})})
        base, other = run_scenario(scenario, checks), run_scenario(changed, checks)
        compatible = world_signature(base.state) == world_signature(other.state) and \
            base.state.start_location == other.state.start_location and \
            (base.state.mission.name if base.state.mission else None) == \
            (other.state.mission.name if other.state.mission else None)
        pairs.append({"scenario": scenario.name, "baseline": base, "modified": other, "compatible": compatible})
        if compatible and judge is not None:
            with base.context.bind():
                duel = judge.compare_states(base.state, other.state)
            duels.append({"scenario": scenario.name, "winner": duel.get("winner"), "reason": duel.get("reason")})
    usable = [p for p in pairs if p["compatible"]]
    for path, higher in COMPARED.items():
        a = [_metric(p["baseline"].metrics, path) for p in usable]
        b = [_metric(p["modified"].metrics, path) for p in usable]
        a, b = [x for x in a if x is not None], [x for x in b if x is not None]
        if not a or not b:
            continue
        mean_a, mean_b = statistics.mean(a), statistics.mean(b)
        delta = mean_b - mean_a
        better = None if higher is None or abs(delta) < 1e-9 else (delta > 0) == higher
        summary[path] = {"baseline": round(mean_a, 3), "modified": round(mean_b, 3), "delta": round(delta, 3),
                         "better": better}
    fit_a = [p["baseline"].fitness.get("total") for p in usable if p["baseline"].fitness.get("total") is not None]
    fit_b = [p["modified"].fitness.get("total") for p in usable if p["modified"].fitness.get("total") is not None]
    if fit_a and fit_b:
        summary["fitness.total"] = {"baseline": round(statistics.mean(fit_a), 3), "modified": round(statistics.mean(fit_b), 3),
                                    "delta": round(statistics.mean(fit_b) - statistics.mean(fit_a), 3),
                                    "better": statistics.mean(fit_b) > statistics.mean(fit_a)}
    return {"pairs": pairs, "summary": summary, "incompatible": [p["scenario"] for p in pairs if not p["compatible"]],
            "judge": duels or None}


# ---- reports -----------------------------------------------------------------------------------------

def _fmt(value):
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def report(runs, comparison=None, title="Playtest report"):
    """The playtest as markdown: scenarios, automated measures, issues with failure examples, weak
    segments with what to edit, the judge's view (kept apart), the comparison and recommendations."""
    lines = [f"# {title}", "",
             "Automated measures (counts and checks of what the engine recorded) and the judge's view "
             "(a language model's subjective reading) are reported separately.", "",
             "## Scenarios", "",
             "| Scenario | World / engine | Player | Seed | Start | Quest | Turns | Stopped |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for run in runs:
        s = run.scenario
        source = Path(s.engine).name if s.engine else Path(s.world).stem if s.world not in (None, "city") else "city"
        quest = (run.metrics.get("main_quest") or {}).get("key")
        lines.append(f"| {s.name} | {source} | {s.player} | {s.seed} | {run.state.start_location} | {quest or '–'} | "
                     f"{run.metrics['turns']} | {run.stopped} |")
    lines += ["", "## Automated measures", "",
              "| Scenario | Quest done | Stages | Side quests (acc/done/fail/decl) | Attacks (/10 turns) | "
              "Meaningful choices | Filler (longest) | Backtracks | Repeated actions | NPCs met/talked | "
              "Contradictions | Fitness |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for run in runs:
        m = run.metrics
        q, sq, enc, ch, fl = m["main_quest"], m["side_quests"], m["encounters"], m["choices"], m["filler"]
        lines.append(
            f"| {run.scenario.name} | {'yes' if q['completed'] else 'no'} | {q['stages_done']}/{q['stages_total']} | "
            f"{sq['accepted']}/{sq['completed']}/{sq['failed']}/{sq['declined']} | {enc['count']} ({enc['per_10_turns']}) | "
            f"{ch['meaningful_share']:.0%} | {fl['share']:.0%} ({fl['longest_streak']}) | {m['movement']['backtracks']} | "
            f"{m['repetition']['repeated_actions']} | {m['npcs']['met']}/{m['npcs']['talked_to']} | "
            f"{m['coherence']['contradictions']} | {_fmt(run.fitness.get('total'))} |")
    worlds = {}
    for run in runs:
        source = run.scenario.engine or run.scenario.world
        for issue in run.world_issues:
            worlds.setdefault(Path(str(source)).stem, {}).setdefault(issue["message"], issue)
    lines += ["", "## World checks (before the first turn; the same for every playthrough of a world)", ""]
    if not any(worlds.values()):
        lines.append("No problem found in the worlds' quests and side quests.")
    for world, found in worlds.items():
        for issue in found.values():
            edit = "; ".join(f"{e['kind']} `{e['name']}` ({e['api']})" for e in issue["elements"] if e.get("name"))
            lines.append(f"- **{issue['code']}** ({world}, {issue['severity']}): {issue['message']}"
                         + (f" Edit: {edit}." if edit else ""))
    by_code = Counter(i["code"] for run in runs for i in run.issues)
    lines += ["", "## Issues found while playing", ""]
    if not by_code:
        lines.append("No issue was detected.")
    else:
        lines += ["| Issue | Count | Severity | Scenarios |", "| --- | --- | --- | --- |"]
        for code, count in by_code.most_common():
            severity = next(i["severity"] for run in runs for i in run.issues if i["code"] == code)
            names = sorted({run.scenario.name for run in runs for i in run.issues if i["code"] == code})
            lines.append(f"| `{code}` | {count} | {severity} | {', '.join(names[:4])}{' …' if len(names) > 4 else ''} |")
        lines += ["", "### Failure examples", ""]
        shown = set()
        for run in runs:
            for issue in run.issues:
                key = (issue["code"], issue["message"])
                if key in shown or len(shown) >= 12:
                    continue
                shown.add(key)
                edit = "; ".join(f"{e['kind']} `{e['name']}` ({e['api']})" for e in issue["elements"] if e.get("name"))
                lines.append(f"- **{issue['code']}** ({run.scenario.name}"
                             f"{', turn ' + str(issue['turn']) if issue['turn'] else ''}): {issue['message']}"
                             + (f" Edit: {edit}." if edit else ""))
    segments = [(run, seg) for run in runs for seg in run.weak_segments]
    lines += ["", "## Weak segments (for authors)", ""]
    if not segments:
        lines.append("None found.")
    for run, seg in segments[:15]:
        edit = "; ".join(f"{e['kind']} `{e['name']}` → {e['studio']}, `{e['api']}`" for e in seg["elements"])
        lines += [f"- **{seg['kind']}**, {run.scenario.name}, turns {seg['turns'][0]}-{seg['turns'][1]}: {seg['why']}",
                  f"  - Suggestion: {seg['suggestion']}", f"  - Edit: {edit}"]
    judged = [run for run in runs if run.judge]
    lines += ["", "## The judge's view (subjective)", ""]
    if not judged:
        lines.append("Not run: no judge was asked (`--judge` needs the judge's server: docs/LLM.md).")
    else:
        lines += ["| Scenario | " + " | ".join(JUDGE_ASPECTS) + " | Verdict |",
                  "|" + " --- |" * (len(JUDGE_ASPECTS) + 2)]
        for run in judged:
            aspects = run.judge["aspects"]
            lines.append(f"| {run.scenario.name} | " + " | ".join(_fmt(aspects.get(a)) for a in JUDGE_ASPECTS)
                         + f" | {run.judge.get('verdict') or ''} |")
    if comparison:
        lines += ["", "## Baseline vs modified storyteller (matched seeds)", "",
                  "| Measure | Baseline | Modified | Δ | Better? |", "| --- | --- | --- | --- | --- |"]
        for path, row in comparison["summary"].items():
            better = {True: "yes", False: "no", None: "–"}[row["better"]]
            lines.append(f"| {path} | {_fmt(row['baseline'])} | {_fmt(row['modified'])} | {row['delta']:+.3f} | {better} |")
        if comparison["incompatible"]:
            lines.append(f"\nLeft out as incompatible (different worlds, starts or quests): "
                         f"{', '.join(comparison['incompatible'])}.")
        if comparison.get("judge"):
            wins = Counter(d["winner"] for d in comparison["judge"])
            lines.append(f"\nJudge, pairwise (A = baseline, B = modified): B won {wins.get('B', 0)}, A won "
                         f"{wins.get('A', 0)}, tie {wins.get('tie', 0)}.")
    lines += ["", "## Recommendations", ""] + [f"- {r}" for r in recommendations(runs)]
    return "\n".join(lines) + "\n"


def recommendations(runs):
    """Actionable recommendations from what the playtests found."""
    found, codes = [], Counter(i["code"] for run in runs for i in run.issues)
    world_codes = Counter(i["code"] for run in runs for i in run.world_issues)
    if codes.get("softlock") or codes.get("objective_unreachable"):
        found.append("Fix the softlocks first: each lists the place where no choice or route toward the quest was "
                     "left; give that place a way on, or open a route toward the quest's next site.")
    if world_codes.get("quest_not_viable_from_start"):
        found.append("Some quests cannot be worked toward from the default start (see World checks): give them a "
                     "route a player can discover from there, or a start of their own.")
    if world_codes.get("impossible_objective") or world_codes.get("missing_required_action"):
        found.append("Correct the quests and side quests whose steps name places, items, people, events or actions "
                     "the world does not have (see the failure examples for the entries to edit).")
    if codes.get("encounter_repetition") or codes.get("encounter_frequency") or codes.get("encounters_back_to_back"):
        found.append("Attacks repeat or cluster: raise `encounters.cooldown` / `min_separation`, lower `rate`, or "
                     "write more encounter templates for the same danger tags.")
    if codes.get("action_loop"):
        found.append("Players loop on the same action: give those actions a `repeat` limit or outcomes that change "
                     "with each try, and make the way toward the quest clearer at those places.")
    filler = [run.metrics["filler"]["share"] for run in runs if run.metrics.get("turns")]
    if filler and statistics.mean(filler) > 0.35:
        found.append(f"On average {statistics.mean(filler):.0%} of turns are filler: see the weak segments for the "
                     "places to enrich.")
    side = [run for run in runs if run.metrics["side_quests"]["offered"]]
    if side and not any(run.metrics["side_quests"]["completed"] for run in side):
        found.append("Side quests are offered but never completed in these playthroughs: check their steps' "
                     "reachability and how clearly the next step is shown.")
    if codes.get("gone_npc_speaks") or codes.get("side_quest_reopened") or codes.get("quest_step_after_completion"):
        found.append("Contradictions were found (people gone who speak again, quests told again after they ended): "
                     "these are engine bugs; add the failing scenario as a regression test.")
    return found or ["No action needed from these scenarios; widen them (more seeds, players, starts)."]


def write(out_dir, runs, comparison=None, title="Playtest report"):
    """Write report.md, playtest.json and replay.html into ``out_dir``; returns their paths."""
    from evaluator import replay

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data = {"title": title, "playthroughs": [run.to_dict() for run in runs]}
    if comparison:
        data["comparison"] = {"summary": comparison["summary"], "incompatible": comparison["incompatible"],
                              "judge": comparison.get("judge"),
                              "pairs": [{"scenario": p["scenario"], "compatible": p["compatible"]} for p in comparison["pairs"]]}
    paths = {"report": out / "report.md", "json": out / "playtest.json", "replay": out / "replay.html"}
    paths["report"].write_text(report(runs, comparison, title), encoding="utf-8")
    paths["json"].write_text(json.dumps(data, indent=1, default=_jsonable), encoding="utf-8")
    paths["replay"].write_text(replay.render(data), encoding="utf-8")
    return paths


def _jsonable(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return str(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("scenarios", help="a scenario file (YAML): evaluator/scenarios/*.yaml")
    parser.add_argument("--out", default="playtest-out", help="the folder for report.md, playtest.json, replay.html")
    parser.add_argument("--compare-genome", help="a JSON file of storyteller traits: compare it against each scenario's")
    parser.add_argument("--compare-engine", help="an engine model file: compare its storyteller against each scenario's")
    parser.add_argument("--compare-overrides", help="a JSON object of settings changed for the modified run")
    parser.add_argument("--judge", action="store_true", help="also ask the language-model judge (its server must run)")
    parser.add_argument("--title", default="Playtest report")
    args = parser.parse_args(argv)
    import os

    os.environ.setdefault("ANE_RECORDS", "off")  # playtests are not recorded
    scenarios, checks = load(args.scenarios)
    judge = playtest_judge() if args.judge else None
    runs = [run_scenario(s, checks, judge) for s in scenarios]
    comparison = None
    modified = {}
    if args.compare_genome:
        modified["genome"] = json.loads(Path(args.compare_genome).read_text(encoding="utf-8"))
    if args.compare_engine:
        modified["engine"] = args.compare_engine
    if args.compare_overrides:
        modified["overrides"] = json.loads(args.compare_overrides)
    if modified:
        comparison = compare(scenarios, modified, checks, judge)
    paths = write(args.out, runs, comparison, args.title)
    for kind, path in paths.items():
        print(f"{kind}: {path}")


if __name__ == "__main__":
    main()
