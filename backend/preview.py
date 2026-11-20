"""A read-only preview of an entry's draft (🌍 World → Entries' *Preview*): the entry as its unsaved draft
makes it, never written.

Two previews, kept apart:

- **static**: the content as written, read from the draft (content.compose_entry_draft, which applies the
  draft's sections to a copy of the stored entry and checks it): its descriptions, its actions with what
  each needs, their outcomes' text and what each changes. No engine runs.
- **simulated**: what the engine makes of it in a sample state, with every assumption said: a new story
  with the draft's world (every other entry as stored), a seeded random storyteller, the story's default
  character, persistent variables at their defaults (no profile's), started at a fitting place, the
  entry brought into the scene; each of the entry's actions tried on its own copy of that state. It
  runs in a separate (spawned) process, so the API's configuration is never touched; the state has no
  profile store (nothing is saved, no progression changes) and is no session (nothing is recorded, no
  session advances).
"""
from __future__ import annotations

import copy
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

import yaml

from utils.config_loader import config

from . import content

# The worker is given the composed world; it plays at most this many actions of the entry.
MAX_SIMULATED_ACTIONS = 12


def _flow(value) -> str:
    return yaml.safe_dump(value, default_flow_style=True, sort_keys=False, allow_unicode=True, width=10**9).strip()


def _sign(value) -> str:
    return f"{value:+g}" if isinstance(value, (int, float)) else str(value)


def outcome_effects(outcome: dict) -> list[str]:
    """What an outcome does, in words (its own fields, then its ``effects`` list)."""
    found = []
    if outcome.get("on_check"):
        found.append(f"told when the check {'passes' if outcome['on_check'] == 'pass' else 'fails'}")
    if outcome.get("success_prob") is not None:
        found.append(f"success chance {outcome['success_prob']:g}" if isinstance(outcome["success_prob"], (int, float))
                     else f"success chance {outcome['success_prob']}")
    for field, word in (("health_change", "health"), ("coin_change", "coins")):
        if outcome.get(field):
            found.append(f"{word} {_sign(outcome[field])}")
    for field, say in (("move_to", "moves to {}"), ("send_to", "sent to {} (one way)"),
                       ("reveal_location", "reveals the place {}"), ("reveal_npc", "reveals {}"),
                       ("reveal_object", "reveals the item {}"), ("unlock_exit", "opens the way to {}"),
                       ("setup_id", "plants the clue {}"), ("payoff_id", "pays off the clue {}"),
                       ("requires_setup", "needs the clue {}")):
        if outcome.get(field):
            found.append(say.format(outcome[field]))
    if outcome.get("spawn"):
        found.append("opens a new place")
    for field, say in (("move_to_tags", "moves to a place tagged {}"), ("target_tags", "finds a place tagged {}")):
        if outcome.get(field):
            found.append(say.format(", ".join(map(str, outcome[field]))))
    if outcome.get("return_to_previous"):
        found.append("back to the previous place")
    if outcome.get("lead_to_known"):
        found.append("leads to a known place")
    if outcome.get("remove_npc"):
        found.append("removes " + ("the person acting" if outcome["remove_npc"] is True else str(outcome["remove_npc"])))
    for name, value in (outcome.get("set_variables") or {}).items():
        found.append(f"sets `{name}` = {value}")
    for name, value in (outcome.get("change_variables") or {}).items():
        found.append(f"`{name}` {_sign(value)}")
    for name, value in (outcome.get("change_stats") or {}).items():
        found.append(f"{name} {_sign(value)}")
    for effect in outcome.get("effects") or []:
        if isinstance(effect, dict):
            found.extend(f"{key.replace('_', ' ')}: {value if not isinstance(value, (dict, list)) else _flow(value)}"
                         for key, value in effect.items())
    if outcome.get("tags"):
        found.append("tags: " + ", ".join(map(str, outcome["tags"])))
    return found


def action_requirements(action: dict) -> list[str]:
    """What an action needs, and what taking it does besides its outcomes, in words."""
    found = []
    check = action.get("skill_check") or {}
    if check.get("stat"):
        found.append(f"check: {check['stat']}" + (f" {_sign(check['modifier'])}" if check.get("modifier") else ""))
    for field, say in (("required_object", "needs {}"), ("required_tag", "only where tagged {}"),
                       ("required_coins", "costs {} coins"), ("consumes_object", "uses up {}"),
                       ("collects_object", "picks up {}"), ("triggers_event", "starts the event {}"),
                       ("repeat", "repeat: {}"), ("exclusive_group", "one of the group {}")):
        if action.get(field) not in (None, "", False):
            found.append(say.format(action[field]))
    if action.get("retry_on_failure"):
        found.append("a failed attempt can be retried")
    if action.get("enabled_if"):
        found.append(f"only when {_flow(action['enabled_if'])}")
    return found


def static_preview(kind: str, record: dict, name: str) -> dict:
    """The entry's content as its draft writes it: {kind, name, title, tags, texts: [{label, lines}],
    facts, actions: [{name, requirements, outcomes: [{text, effects, variants}]}], dialogue}."""
    texts = []
    for field, label in (("entered_descriptions", "On arrival"), ("descriptions", "Descriptions"),
                         ("distant_descriptions", "Seen from afar"), ("interact_prompts", "Ways to go there"),
                         ("goals", "Goals")):
        lines = [content._text_of(line) if not isinstance(line, dict) or "text" in line else content._goal_text(line)
                 for line in record.get(field) or []]
        if lines:
            texts.append({"label": label, "lines": [line for line in lines if line]})
    facts = []
    if kind == "Locations" and record.get("start_eligible"):
        facts.append(f"a story can start here (weight {record.get('start_weight', 1.0):g})")
    if kind == "NPCs":
        facts.append("unique" if record.get("unique") else "generic")
        if record.get("role"):
            facts.append(f"role: {record['role']}")
        if record.get("encounter_event"):
            facts.append(f"hostile: starts {record['encounter_event']}"
                         + (" on every visit" if record.get("encounter_repeat") else " once"))
    if kind == "Items" and record.get("stat_bonus"):
        facts.append("bonus " + ", ".join(f"{stat} {_sign(value)}" for stat, value in record["stat_bonus"].items()))
    if kind in ("Events", "Items"):
        for field in content.POLICY_FIELDS.get(kind, ()):
            if field in record:
                facts.append(f"{field.replace('_', ' ')}: {record[field]}")
    if record.get("enabled_if"):
        facts.append(f"only when {_flow(record['enabled_if'])}")
    actions = []
    for action in record.get("actions") or []:
        if not isinstance(action, dict):
            continue
        actions.append({"name": str(action.get("name") or "(unnamed action)"),
                        "requirements": action_requirements(action),
                        "outcomes": [{"text": str(outcome.get("desc") or ""), "effects": outcome_effects(outcome),
                                      "variants": len(outcome.get("variants") or [])}
                                     for outcome in action.get("outcomes") or [] if isinstance(outcome, dict)]})
    dialogue = [{"text": " / ".join(map(str, line["text"])) if isinstance(line.get("text"), list) else str(line.get("text")),
                 "when": _flow(line["enabled_if"]) if line.get("enabled_if") else None}
                for line in record.get("dialogue") or [] if isinstance(line, dict)]
    return {"kind": kind, "name": name, "title": record.get("title"),
            "tags": [str(t) for t in (record.get("tags") or record.get("associated_tags") or [])],
            "texts": texts, "facts": facts, "actions": actions, "dialogue": dialogue}


# ---- the simulated preview (a spawned process: the API's configuration is never touched) -------------

def _json_safe(values: dict) -> dict:
    return {key: value for key, value in values.items() if isinstance(value, (int, float, str, bool, type(None)))}


def _simulate(config_data, tag_vectors, kind, name, record, seed):
    """Run in a spawned process: the sample scene and the entry's actions tried (see the module's doc)."""
    import random

    import main as engine
    from core_engine.game_state import GameState
    from core_engine.genome import Genome

    engine._init_spawned_worker(config_data, tag_vectors)
    teller = Genome(rng=random.Random(seed))
    world = engine.build_world(teller)
    places = list(world.locations)
    if kind == "Locations":
        start, where = name, f"at {name} itself"
    elif kind == "NPCs":
        known = [place for place in record.get("known_locations") or [] if place in world.locations]
        start = known[0] if known else places[0]
        where = f"at {start}, " + ("a place they know" if known else "the world's first place (they know none)")
    elif kind == "Items":
        start = record.get("location") if record.get("location") in world.locations else places[0]
        where = f"at {start}, " + ("its place" if record.get("location") == start else "the world's first place")
    else:
        start, where = places[0], f"at {places[0]}, the world's first place"
    state = GameState(world, teller, start=start, rng=random.Random(seed), variables={}, profile="preview", store=None)
    bring = {"NPCs": ("reveal_npc", "brought into the scene (as the effect `reveal_npc` would)"),
             "Items": ("reveal_item", "revealed in the scene (as the effect `reveal_item` would)"),
             "Events": ("start_event", "started here (as the effect `start_event` would)")}.get(kind)
    setup_note = None
    if bring:
        try:
            state.apply_effects([{bring[0]: name}])
            setup_note = f"{name} is {bring[1]}."
        except Exception as exc:  # noqa: BLE001 - said in the preview, the static part stands
            setup_note = f"{name} could not be brought into the scene: {exc}"
    options = state.player_options()  # also sets the scene up (people, items, encounters)
    description, people, items = state._describe_visible_scene()

    def snapshot(st):
        return {"place": st.current.name, "health": st.health, "coins": st.coins, "inventory": sorted(st.inventory),
                "variables": {k: v for k, v in st.variables.items() if k != "playthroughs"}, "ended": bool(st.ended)}

    def changes(before, after):
        found = []
        if after["place"] != before["place"]:
            found.append(f"moves to {after['place']}")
        for field in ("health", "coins"):
            if after[field] != before[field]:
                found.append(f"{field} {before[field]} → {after[field]}")
        gained = sorted(set(after["inventory"]) - set(before["inventory"]))
        lost = sorted(set(before["inventory"]) - set(after["inventory"]))
        found += [f"gains {item}" for item in gained] + [f"loses {item}" for item in lost]
        found += [f"`{key}` {before['variables'].get(key)} → {value}" for key, value in after["variables"].items()
                  if before["variables"].get(key) != value]
        if after["ended"] and not before["ended"]:
            found.append("the story ends")
        return found

    available = state._actions_now()
    shown = {option.name for option in options}
    tried = []
    for action in (record.get("actions") or [])[:MAX_SIMULATED_ACTIONS]:
        label = str((action or {}).get("name") or "")
        match = next((a for a in available if a.name == label and a.owner == name), None) \
            or next((a for a in available if a.name == label), None)
        if match is None:
            tried.append({"name": label, "offered": False, "shown": False})
            continue
        clone = copy.deepcopy(state)
        option = next(a for a in clone._actions_now() if a.name == label and a.owner == match.owner)
        before, lines, checks = snapshot(clone), len(clone.history), len(clone.combat.history)
        clone.take_option(option)
        check = clone.combat.history[-1] if len(clone.combat.history) > checks else None
        tried.append({"name": label, "offered": True, "shown": label in shown,
                      "text": "\n".join(line.strip("\n") for line in clone.history[lines:]).strip(),
                      "check": _json_safe(vars(check)) if check is not None else None,
                      "changes": changes(before, snapshot(clone))})
    present = (kind == "Locations" or (kind == "NPCs" and name in [n for n, _ in people])
               or (kind == "Items" and name in [n for n, _ in items])
               or (kind == "Events" and getattr(state.active_event, "name", None) == name))
    stats = state.stats.values(state.combat.item_bonuses(state.inventory))
    assumptions = [
        "The unsaved draft, in the world as stored otherwise (every other entry as saved).",
        f"A new story {where}, turn 1; seed {seed}: the same seed gives the same sample.",
        "Storyteller: a random one drawn from the seed (no trained engine).",
        f"Player: the story's default character ({', '.join(f'{k} {v}' for k, v in stats.items())}), "
        f"health {state.health}/{state.max_health}, {state.coins} coins, items: "
        f"{', '.join(sorted(state.inventory)) or 'none'} (as a new story starts).",
        "Persistent variables: their defaults, not any profile's stored values; nothing is saved.",
        f"Quest: {state.mission.title if state.mission else 'none'} (as the story draws it for this seed).",
        *([setup_note] if setup_note else []),
        "Each of the entry's actions is tried on its own copy of this state: what the engine tells, the check "
        "it rolls with this seed, and what it changes. Another seed or state can tell it differently.",
    ]
    return {"start": start, "present": bool(present), "description": description,
            "people": [n for n, _ in people], "items": [n for n, _ in items],
            "choices": [{"name": o.name, "check": state.check_hint(o), "entry": o.owner == name} for o in options],
            "actions": tried, "assumptions": assumptions}


def simulate(kind: str, name: str, record: dict, world: dict, seed: int = 0) -> dict:
    """The simulated preview of the composed entry, in a spawned process (see _simulate)."""
    from core_engine.model import world_tags
    from utils.tag_similarity import export_tag_vectors

    config_data = copy.deepcopy(config.as_dict())
    config_data["world_definition"] = copy.deepcopy(world)
    vectors = export_tag_vectors(world_tags(config_data))
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
        return pool.submit(_simulate, config_data, vectors, kind, name, copy.deepcopy(record), int(seed)).result()


def preview(kind: str, name: str | None, draft: dict, simulate_scene: bool = False, seed: int = 0) -> dict:
    """The preview of an entry's draft (``name`` None: a new entry, from its fields): {name, sections
    (the draft's), problems (what a save would refuse; the preview is shown anyway), static, simulated
    (None unless ``simulate_scene``; also None with ``simulated_error`` when the draft's world cannot be
    built), saved: False}. Nothing is written."""
    composed = content.compose_entry_draft(kind, name, draft)
    final, record = composed["name"], composed["record"]
    result = {"name": final, "sections": composed["sections"], "problems": composed["problems"],
              "static": static_preview(kind, record, final), "simulated": None, "simulated_error": None,
              "saved": False}
    if simulate_scene:
        if not final:
            result["simulated_error"] = "A new entry needs a name to be simulated."
        else:
            try:
                result["simulated"] = simulate(kind, final, record, composed["world"], seed)
            except Exception as exc:  # noqa: BLE001 - the draft's world could not be played: said, not raised
                result["simulated_error"] = f"The draft's world could not be played: {exc}"
    return result
