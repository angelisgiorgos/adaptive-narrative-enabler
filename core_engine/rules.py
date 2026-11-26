"""Story rules: how the engine keeps a story coherent, paced and varied.

Configured under ``story_rules`` in hyperparameters.yaml (docs/AI.md, "Story rules").
Each rule answers a failure measured in simulated stories:

  failure_tags          Outcomes with one of these tags (or ``on_check: fail``) are
                        the failure branch of their action: told only when the attempt
                        fails, so a passed check never tells a failure and a failed
                        one never tells a success. An arrest tells no success text.
  follow_outcome_text   A route that an outcome (``spawn``) or an NPC's lead opens is
                        the place the outcome's text names, if it names one.
  early_goal_avoidance  Before mission_settings.pursuit_start_step the storyteller
                        avoids completing the mission (fading out at that step).
  on_stall              ``move``: after narrative_end.max_consecutive_steps_in_location
                        in one place the traveler moves on; ``end``: the story ends
                        ("stalled").
  repetition_power      Repeating a choice costs ``uses ** power`` x novelty_bias.
  backtrack_penalty     Going straight back to the place one just left (x novelty_bias).
  tie_epsilon           Choices this close to the best are near-ties, ordered by story
                        logic (fewer uses, more unseen outcomes, then a per-story order).
  collect_weight        Picking up an item that is not held (x discovery_bias).
  payoff_weight         An action that pays off an open clue (x setup_prob).
  payoff_route_weight   Travelling to a place where an open clue can be paid off.
  max_open_clues        While this many clues are open, no new clue is pursued.
  clue_matching         ``any``: a payoff closes the oldest open clue; ``numbered``:
                        payoff_N only closes setup_N.
  gate_payoffs          A payoff (payoff_* tag) is told only when a planted clue waits for
                        it (with ``numbered``: its own setup_N); named threads (an outcome's
                        ``requires_setup``) always wait for their ``setup_id``.
  failure_counts        An action with no skill check whose told outcome is a failure-branch
                        outcome (a surrender, a capture) counts as failed: the event it ends
                        is ``event_failed``. ``false``: the old behaviour (counted as succeeded).
"""
import math
import re

from utils.config_loader import config

DEFAULTS = {
    "failure_tags": ["failure"],
    "follow_outcome_text": True,
    "early_goal_avoidance": 1.0,
    "on_stall": "move",
    "repetition_power": 1.5,
    "backtrack_penalty": 1.5,
    "tie_epsilon": 0.1,
    "collect_weight": 1.0,
    "payoff_weight": 2.0,
    "payoff_route_weight": 1.5,
    "max_open_clues": 2,
    "clue_matching": "any",
    "gate_payoffs": True,
    "failure_counts": True,
}
NUMBERS = ("early_goal_avoidance", "repetition_power", "backtrack_penalty", "tie_epsilon", "collect_weight",
           "payoff_weight", "payoff_route_weight")


def settings(cfg=None):
    """``story_rules`` with defaults for the keys a configuration leaves out."""
    cfg = config if cfg is None else cfg
    return {**DEFAULTS, **(cfg.get("story_rules") or {})}


def validate_settings(values):
    errors = []
    for key in NUMBERS:
        value = values.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
            errors.append(f"`story_rules.{key}` must be a number of 0 or more.")
    if not isinstance(values.get("repetition_power"), bool) and isinstance(values.get("repetition_power"), (int, float)) \
            and values["repetition_power"] < 1:
        errors.append("`story_rules.repetition_power` must be at least 1 (1: every use costs the same).")
    if values.get("on_stall") not in ("move", "end"):
        errors.append("`story_rules.on_stall` must be move or end.")
    if values.get("clue_matching") not in ("any", "numbered"):
        errors.append("`story_rules.clue_matching` must be any or numbered.")
    if not isinstance(values.get("gate_payoffs", True), bool):
        errors.append("`story_rules.gate_payoffs` must be true or false.")
    if not isinstance(values.get("failure_counts", True), bool):
        errors.append("`story_rules.failure_counts` must be true or false.")
    if not isinstance(values.get("follow_outcome_text"), bool):
        errors.append("`story_rules.follow_outcome_text` must be true or false.")
    tags = values.get("failure_tags")
    if not isinstance(tags, list) or not all(isinstance(tag, str) and tag for tag in tags):
        errors.append("`story_rules.failure_tags` must be a list of tags.")
    open_clues = values.get("max_open_clues")
    if isinstance(open_clues, bool) or not isinstance(open_clues, int) or open_clues < 1:
        errors.append("`story_rules.max_open_clues` must be a whole number of 1 or more.")
    return errors


def is_failure(outcome, failure_tags):
    """Whether an outcome is its action's failure branch (told when the attempt fails)."""
    if outcome.on_check == "fail":
        return True
    if outcome.on_check == "pass":
        return False
    return any(tag.lower() in failure_tags for tag in outcome.tags)


_PATTERNS = {}


def mentioned_location(text, names):
    """The location a story text names (the longest name that appears), or None."""
    lowered = str(text or "").lower()
    for name in sorted(names, key=len, reverse=True):
        pattern = _PATTERNS.get(name)
        if pattern is None:
            pattern = _PATTERNS[name] = re.compile(rf"\b{re.escape(name.lower())}\b")
        if pattern.search(lowered):
            return name
    return None


def clue_pairs(setup_indices, payoff_indices, matching="any"):
    """Match planted clues (setup_* tags) with their payoffs (payoff_* tags).

    ``setup_indices`` / ``payoff_indices``: tag -> step it first appeared. Returns
    (pairs [(setup, setup step, payoff, payoff step)], open setups, unmatched payoffs).
    ``any``: each payoff closes the oldest clue still open before it; ``numbered``:
    payoff_N closes setup_N.
    """
    setups = sorted(setup_indices.items(), key=lambda item: (item[1], item[0]))
    payoffs = sorted(payoff_indices.items(), key=lambda item: (item[1], item[0]))
    open_setups, pairs, unmatched = list(setups), [], []
    for payoff, step in payoffs:
        wanted = payoff.replace("payoff_", "setup_")
        match = next((s for s in open_setups if s[1] < step and (matching == "any" or s[0] == wanted)), None)
        if match is None:
            unmatched.append(payoff)
            continue
        open_setups.remove(match)
        pairs.append((match[0], match[1], payoff, step))
    return pairs, [tag for tag, _ in open_setups], unmatched


# ---- what people and places do, by their tags (npc_behaviour, location_behaviour) ---------------

BEHAVIOURS = {
    "npc": {
        # they come up to the player and open a conversation: never, the first time they meet, every visit
        "starts_conversation": ("never", "first_meeting", "every_visit"),
        # in how many visits of a story they are shown: always (whenever they fit the scene), once, or a number
        "appearances": ("always", "once"),
    },
    "location": {
        # on arrival its own activities are offered first ("Look around" for the rest)
        "draws_in": ("never", "first_visit", "every_visit"),
        # how many visits a story may have there: always, once, or a number
        "visits": ("always", "once"),
    },
}
BEHAVIOUR_DEFAULTS = {"starts_conversation": "never", "appearances": "always", "draws_in": "never", "visits": "always"}
COUNTED = {"appearances", "visits"}  # behaviours that may also be a number of visits
SECTIONS = {"npc": "npc_behaviour", "location": "location_behaviour"}


def behaviour(kind, tags, key, cfg=None):
    """How an NPC or a place (``kind``) with ``tags`` behaves (npc_behaviour, location_behaviour in
    hyperparameters.yaml): for ``key``, the first rule that sets it and shares a tag with them,
    else the default."""
    if cfg is None:
        cfg = config.get(SECTIONS[kind]) or {}
    tags = {str(tag).lower() for tag in tags}
    for rule in cfg.get("rules") or []:
        if key in rule and tags & {str(tag).lower() for tag in rule.get("tags") or []}:
            return rule[key]
    return (cfg.get("default") or {}).get(key, BEHAVIOUR_DEFAULTS[key])


def npc_behaviour(tags, key, cfg=None):
    return behaviour("npc", tags, key, cfg)


def location_behaviour(tags, key, cfg=None):
    return behaviour("location", tags, key, cfg)


def behaviour_options(kind, cfg=None):
    """{behaviour: [{tag, value}]}: every tag a rule gives a behaviour, in the rules' order, for
    the studio's choices (choosing one sets that tag)."""
    if cfg is None:
        cfg = config.get(SECTIONS[kind]) or {}
    options = {key: [] for key in BEHAVIOURS[kind]}
    for rule in cfg.get("rules") or []:
        for key in BEHAVIOURS[kind]:
            if key in rule:
                for tag in rule.get("tags") or []:
                    if all(option["tag"] != str(tag) for option in options[key]):
                        options[key].append({"tag": str(tag), "value": rule[key]})
    return options


def appearance_limit(value):
    """The most visits (None: no limit) for an ``appearances`` or ``visits`` value."""
    if value == "once":
        return 1
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def validate_behaviour(kind, cfg):
    """Problems of npc_behaviour / location_behaviour (empty when it can be used)."""
    section = SECTIONS[kind]
    if cfg is None:
        return []
    if not isinstance(cfg, dict):
        return [f"{section} must be a mapping with `default` and `rules`."]
    errors = []
    known = BEHAVIOURS[kind]

    def check(values, where):
        for key, value in values.items():
            if key == "tags":
                continue
            if key not in known:
                errors.append(f"{where}: unknown behaviour `{key}`; use {', '.join(known)}.")
            elif key in COUNTED and isinstance(value, int) and not isinstance(value, bool):
                if value < 1:
                    errors.append(f"{where}: {key} must be always, once or a number of visits (1 or more).")
            elif value not in known[key]:
                choices = ", ".join(known[key]) + (" or a number of visits" if key in COUNTED else "")
                errors.append(f"{where}: {key} must be {choices}, not {value!r}.")

    if not isinstance(cfg.get("default") or {}, dict):
        errors.append(f"{section}.default must be a mapping.")
    else:
        check(cfg.get("default") or {}, f"{section}.default")
    rules_ = cfg.get("rules") or []
    if not isinstance(rules_, list):
        return errors + [f"{section}.rules must be a list of {{tags, behaviour: value}}."]
    for number, rule in enumerate(rules_, start=1):
        where = f"{section}.rules[{number}]"
        if not isinstance(rule, dict) or not isinstance(rule.get("tags"), list) or not rule["tags"]:
            errors.append(f"{where} needs `tags`, a list of the tags it is for.")
            continue
        check(rule, where)
    return errors


def validate_npc_behaviour(cfg):
    return validate_behaviour("npc", cfg)
