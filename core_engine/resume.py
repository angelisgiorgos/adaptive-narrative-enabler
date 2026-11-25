"""A session as parameters the UNP can read, keep, change and give back (``ane.session_state/1``),
and the session they describe.

- ``parameters(state)``: where the player is and has been, stats, inventory, health, coins,
  variables, the goal and its stage, the one-time actions tried, the people met and beaten, the
  alert, the turn: a superset of the session report (core_engine/progression.py).
- ``rebuild(genome, params, ...)``: a new session standing exactly at those parameters. Nothing
  is narrated and nothing is paid out again: the parameters are set, not played.
- ``apply_player(state, player)``: the UNP sets the player's stats, inventory, coins, health or
  variables during a session (or overrides them when a save game is loaded).
- ``sync(state, event)``: what happened in the UNP (an action done, an item gained or lost, an
  NPC removed, a place visited) is taken into the session, so its goals and one-time actions
  stay consistent with the UNP's game.
"""
from __future__ import annotations

import random

from . import conditions, encounters, side_quests
from .context import with_state_context

FORMAT = "ane.session_state/1"
PLAYER_FIELDS = ("stat_points", "stat_changes", "inventory", "add_items", "remove_items", "coins", "health",
                 "max_health", "variables")
SYNC_TYPES = ("action_done", "item_gained", "item_lost", "npc_removed", "location_visited", "coins_changed",
              "health_changed")


class ParameterError(ValueError):
    """Parameters that do not fit this world (an unknown place, item, stat, ...)."""


def _whole(value, field):
    """A whole number from the UNP, or a ParameterError that names the field."""
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ParameterError(f"`{field}` must be a whole number, not {value!r}.")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ParameterError(f"`{field}` must be a whole number, not {value!r}.") from None


@with_state_context
def parameters(state):
    """The session's parameters (JSON-ready)."""
    mission = state.mission
    used = sorted({entry["item"] for entry in state.item_log if entry["what"] in ("used", "removed")}
                  - set(state.inventory))
    return {
        "format": FORMAT,
        "profile": state.profile,
        "session_id": state.__dict__.get("session_id"),
        "start_location": state.start_location,
        "current_location": state.current.name,
        "location_counts": {name: count for name, count in state.location_counts.items() if count},
        "path": list(state.path),
        "known_paths": {name: sorted(loc.connected) for name, loc in state.world.locations.items() if loc.connected},
        "turns": state.actions_taken,
        "health": state.health,
        "max_health": state.max_health,
        "coins": state.coins,
        "stat_points": dict(state.stats.points),
        "stat_changes": {k: v for k, v in state.stats.changes.items() if v},
        "inventory": sorted(state.inventory),
        "used_items": used,
        "variables": {k: v for k, v in state.variables.items() if k in state.variable_specs},
        "mission": None if mission is None else {"key": mission.name, "title": mission.title,
                                                 "stage": getattr(state, "mission_stage", 0), "won": bool(state.won)},
        "side_quests": side_quests.parameters(state),  # {key: {status, stage}}
        "encounters": encounters.parameters(state),  # {attacks, factions, rewards_paid}
        "actions_succeeded": sorted(state.succeeded_actions),
        "unique_actions": state.unique_actions(),
        "events": [{"event": e["event"], "result": e.get("result"), "location": e.get("location")}
                   for e in state.event_log if "action" in e],
        "people_met": [{"name": p["name"], "where": p["where"], "talks": p["talks"]} for p in state.people_met()],
        "removed_npcs": sorted(state.removed_npcs),
        "allies": sorted(state.__dict__.get("allies", ())),
        "defeated": [dict(d) for d in state.__dict__.get("defeated", [])],
        "alert": int(state.__dict__.get("alert", 0)),
        "ended": bool(state.ended),
    }


# ---- helpers that set, never play -------------------------------------------------------------

def _hold(state, name, how="received"):
    """The player holds the item (silently)."""
    if name in state.inventory:
        return
    instance = state.item_instances.get(name)
    if instance is not None:
        instance.state, instance.location = "held", None
        state._scene_dirty = True
    state.inventory.add(name)
    state._log_item(name, how, state.current.name if state.current else None)


def _drop(state, name, how="removed"):
    if name not in state.inventory:
        return
    state.inventory.remove(name)
    instance = state.item_instances.get(name)
    if instance is not None:
        instance.state = "gone"
    state._log_item(name, how, state.current.name if state.current else None)


def _known_item(state, name):
    if not any(o.name == name for o in state.world.objects):
        raise ParameterError(f"Unknown item `{name}`.")


def _place(state, name, count_visit=True):
    """Stand the player at a place (silently): visited, on the path, the scene set anew."""
    if name not in state.world.locations:
        raise ParameterError(f"Unknown location `{name}`.")
    previous = state.current.name if state.current else None
    state.current = state.world.locations[name]
    if count_visit:
        state.location_counts[name] = state.location_counts.get(name, 0) + 1
        if not state.path or state.path[-1] != name:
            state.path.append(name)
    state.last_location_name = previous
    state.consecutive_steps_in_location = 0
    state.visit_actions_performed = set()
    state._scene_dirty = True


def _find_action(state, name, owner=None, source=None):
    keys = conditions.action_keys(name)
    holders = [(loc.name, "location", loc.actions) for loc in state.world.locations.values()]
    holders += [(c.name, "npc", c.actions) for c in state.world.characters]
    holders += [(o.name, "item", o.actions) for o in state.world.objects]
    holders += [(e.name, "event", e.actions) for e in state.world.events.values()]
    for holder, kind, actions in holders:
        if owner and holder != owner:
            continue
        if source and kind != source and source not in ("conversation", "navigation"):
            continue
        for action in actions or ():
            if keys & conditions.action_keys(action.name):
                return action
    return None


@with_state_context  # it calls the story's own rules (GameState._repeat_rule, _action_scope)
def _mark_done(state, name, where=None, owner=None, source=None, succeeded=True, kept=False, turn=None):
    """A one-time action (or any action) taken: used up as its repeat rule says, remembered."""
    state.succeeded_actions |= conditions.action_keys(name) if succeeded else set()
    action = _find_action(state, name, owner, source)
    if action is None:
        return False
    place = where or state.current.name
    if state._repeat_rule(action) == "once":
        scope = state._action_scope(action, place)
        if succeeded or not kept:
            state.spent_actions.add((scope, action.name))
        if succeeded and action.exclusive_group:
            state.closed_groups.add((scope, action.exclusive_group))
        state.__dict__.setdefault("unique_log", []).append({
            "turn": state.actions_taken if turn is None else turn, "action": action.name, "source": action.source,
            "owner": action.owner, "where": place, "succeeded": bool(succeeded), "kept": bool(kept)})
    elif succeeded and action.exclusive_group:
        state.closed_groups.add((state._action_scope(action, place), action.exclusive_group))
    return True


def _clear(state):
    state._clear_planning_caches()
    state._variables_version = state.__dict__.get("_variables_version", 0) + 1


# ---- the player, set by the UNP ---------------------------------------------------------------

@with_state_context
def apply_player(state, player):
    """Set the player's state from the UNP: stat_points (a full allocation), stat_changes, inventory
    (exactly these items), add_items / remove_items, coins, health, max_health, variables.
    Returns the fields applied."""
    player = dict(player or {})
    unknown = sorted(set(player) - set(PLAYER_FIELDS))
    if unknown:
        raise ParameterError(f"Unknown player field(s) {', '.join(unknown)}; use {', '.join(PLAYER_FIELDS)}.")
    applied = []
    if "stat_points" in player:
        points = {str(k): _whole(v, f"stat_points.{k}") for k, v in dict(player["stat_points"]).items()}
        errors = state.stats.validate_points(points, state.stats.names, state.stats.budget)
        if errors:
            raise ParameterError("; ".join(errors))
        state.stats.points = {name: points[name] for name in state.stats.names}
        applied.append("stat_points")
    if "stat_changes" in player:
        changes = dict(player["stat_changes"])
        bad = sorted(set(changes) - set(state.stats.names))
        if bad:
            raise ParameterError(f"Unknown stat(s) {', '.join(bad)}.")
        state.stats.changes = {name: _whole(changes.get(name, 0), f"stat_changes.{name}") for name in state.stats.names}
        applied.append("stat_changes")
    if "inventory" in player:
        wanted = [str(i) for i in player["inventory"]]
        for name in wanted:
            _known_item(state, name)
        for name in sorted(set(state.inventory) - set(wanted)):
            _drop(state, name)
        for name in wanted:
            _hold(state, name)
        applied.append("inventory")
    for name in player.get("add_items") or []:
        _known_item(state, str(name))
        _hold(state, str(name))
    for name in player.get("remove_items") or []:
        _drop(state, str(name))
    applied += [f for f in ("add_items", "remove_items") if f in player]
    if "max_health" in player:
        state.health_player.max_health = max(1, _whole(player["max_health"], "max_health"))
        state.health_player.set(min(state.health_player.health, state.health_player.max_health))
        applied.append("max_health")
    if "health" in player:
        state.health_player.set(_whole(player["health"], "health"))
        applied.append("health")
    if "coins" in player:
        state.coins = max(0, _whole(player["coins"], "coins"))
        applied.append("coins")
    if "variables" in player:
        for name, value in dict(player["variables"]).items():
            if name not in state.variable_specs:
                raise ParameterError(f"Unknown variable `{name}`.")
            state._write_variable(name, value)
        applied.append("variables")
    state.update_power()
    _clear(state)
    state._changed()  # the options shown before are of an older state now
    return applied


# ---- what happened in the UNP ------------------------------------------------------------------

@with_state_context
def sync(state, event):
    """Take one event from the UNP into the session: {"type": action_done, "action", "owner"?,
    "where"?, "succeeded"?} | item_gained/item_lost {"item"} | npc_removed {"npc"} |
    location_visited {"location"} | coins_changed {"amount"} | health_changed {"amount"}.
    Goals are checked afterwards: an action done in the UNP can complete the session's goal."""
    kind = event.get("type")
    if kind not in SYNC_TYPES:
        raise ParameterError(f"Unknown event type `{kind}`; use {', '.join(SYNC_TYPES)}.")
    if state.ended:
        raise ParameterError("The session has ended.")
    if kind == "action_done":
        if not event.get("action"):
            raise ParameterError("`action` is required.")
        _mark_done(state, str(event["action"]), event.get("where"), event.get("owner"), event.get("source"),
                   bool(event.get("succeeded", True)), bool(event.get("kept", False)))
    elif kind == "item_gained":
        _known_item(state, str(event.get("item")))
        _hold(state, str(event["item"]), "received")
    elif kind == "item_lost":
        _drop(state, str(event.get("item")), "removed")
    elif kind == "npc_removed":
        name = str(event.get("npc"))
        if not any(c.name == name for c in state.world.characters):
            raise ParameterError(f"Unknown NPC `{name}`.")
        state._remove_npc(name)  # a real event of the story: its consequences follow
    elif kind == "location_visited":
        _place(state, str(event.get("location")))
    elif kind == "coins_changed":
        state.coins = max(0, state.coins + _whole(event.get("amount", 0), "amount"))
    elif kind == "health_changed":
        state.health_player.change(_whole(event.get("amount", 0), "amount"))
    state.update_power()
    state.__dict__.setdefault("sync_log", []).append({"turn": state.actions_taken, **event})
    _clear(state)
    state._changed()  # the options shown before are of an older state now
    state._finalize_if_ended()  # the goal may be reached by what the player did in the UNP
    return kind


# ---- a session rebuilt from parameters ---------------------------------------------------------

def rebuild(genome, params, store=None, profile=None, seed=None, world=None):
    """A new session standing at ``params`` (ane.session_state/1)."""
    from .builder import build_world
    from .game_state import GameState

    params = dict(params or {})
    if params.get("ended"):
        raise ParameterError("The session has ended: start a new one (its progression is already saved).")
    world = world or build_world(genome)
    location = params.get("current_location") or params.get("start_location")
    if location not in world.locations:
        raise ParameterError(f"Unknown location `{location}`.")
    mission = (params.get("mission") or {}).get("key")
    if mission is not None and mission not in world.missions:
        raise ParameterError(f"Unknown mission `{mission}`.")
    points = params.get("stat_points") or None
    state = GameState(world, genome, start=location, rng=random.Random(seed), mission=mission,
                      variables=params.get("variables") or None, store=None, profile=profile or params.get("profile"),
                      stats=points, recent_starts=[])
    # Real play from here: the profile's store, and what it implies.
    state._store = store
    if store is not None:
        from utils.config_loader import config

        directed = str((config.get("navigation") or {}).get("player_directed", "play")).lower()
        state.player_directed = directed in ("always", "play")
        state.completed_goals = store.completed_goals(state.profile)
    # What the parameters name stays in the session's cast.
    scope = state.__dict__.get("session_scope")
    if scope:
        named = {
            "locations": set(params.get("location_counts") or {}) | {location, params.get("start_location")},
            "npcs": {p["name"] for p in params.get("people_met") or []} | set(params.get("removed_npcs") or [])
            | set(params.get("allies") or []),
            "items": set(params.get("inventory") or []) | set(params.get("used_items") or []),
            "events": {e["event"] for e in params.get("events") or []},
        }
        for kind, names in named.items():
            scope[kind] -= names
        state._settle_scope()
    if params.get("session_id"):
        state.session_id = params["session_id"]
    if params.get("start_location") in world.locations:
        state.start_location = params["start_location"]
    counts = {name: int(count) for name, count in (params.get("location_counts") or {}).items()
              if name in world.locations and int(count) > 0}
    counts[location] = max(1, counts.get(location, 0))
    state.location_counts = counts
    path = [name for name in params.get("path") or [] if name in world.locations]
    state.path = path if path and path[-1] == location else path + [location]
    for name, others in (params.get("known_paths") or {}).items():
        if name in world.locations:
            world.locations[name].connected |= {o for o in others if o in world.locations}
    apply_player(state, {k: params[k] for k in ("stat_changes", "inventory", "coins", "max_health", "health")
                         if k in params})
    for name in params.get("used_items") or []:
        if any(o.name == name for o in world.objects):
            _hold(state, name)
            _drop(state, name, "used")
    if state.mission is not None and params.get("mission"):
        stages = getattr(state.mission, "stages", None) or []
        state.mission_stage = max(0, min(int(params["mission"].get("stage", 0)), max(0, len(stages) - 1)))
    side_quests.restore(state, params.get("side_quests"))  # as they stood: nothing told or paid again
    encounters.restore(state, params.get("encounters"))    # the attacks, standing changes and rewards paid
    state.succeeded_actions |= {str(key) for key in params.get("actions_succeeded") or []}
    for entry in params.get("unique_actions") or []:
        _mark_done(state, entry["action"], entry.get("where"), entry.get("owner"), entry.get("source"),
                   bool(entry.get("succeeded")), bool(entry.get("kept")), entry.get("turn"))
    for entry in params.get("events") or []:
        if entry.get("event") in world.events:
            state.event_log.append({"event": entry["event"], "location": entry.get("location"), "visit": 0, "npc": None,
                                    "step": 0, "cause": "resumed", "action": "(resumed)",
                                    "succeeded": entry.get("result") == "success", "result": entry.get("result"),
                                    "ended_step": 0})
    met = state.__dict__.setdefault("met", {})
    for person in params.get("people_met") or []:
        met[person["name"]] = {"where": list(person.get("where") or []), "first_turn": 0}
        if person.get("talks"):
            state.character_interactions[person["name"]] = int(person["talks"])
    state.removed_npcs |= {n for n in params.get("removed_npcs") or [] if any(c.name == n for c in world.characters)}
    state.allies = set(params.get("allies") or [])
    state.defeated = [dict(d) for d in params.get("defeated") or []]
    state.actions_taken = int(params.get("turns", 0))
    state.alert = int(params.get("alert", 0))
    state._alert_quiet = 0
    _clear(state)
    state.history.append(state.narrate("resumed", location=state.current.name, turns=state.actions_taken))
    return state
