"""An exact snapshot of a story in progress, as JSON (``ane.snapshot/1``), and back.

A save game the UNP keeps and gives back (core_engine/savegame.py) carries one, so a session
continues exactly where it stopped: the same scene, the same threads, the same dice. It is
plain JSON, never pickle: loading it runs no code.

What is stored, and what is rebuilt:

- the world is rebuilt from the configuration and the storyteller (``build_world``), then the
  story's changes to it are applied: the paths found (every location's ``connected``) and the
  events the story made itself (a climax, a bargain);
- every attribute of the GameState is encoded by walking it (many are created during play),
  with type tags for what JSON lacks: ``$t`` tuple, ``$set``, ``$fs`` frozenset, ``$ctr``
  Counter, ``$dd`` defaultdict, ``$map`` a dict whose keys are not plain strings; references to
  the world by name (``$loc``, ``$npc``, ``$obj``, ``$event``, ``$mission``); the story's own
  objects whitelisted by class (``$o``); the dice (``$rng``);
- caches are kept (some are stale on purpose, and play depends on them), except the action
  index (``_site_index``), which is rebuilt from the world.

``dump(state)`` fails on a type it does not know, so nothing is silently lost; ``load``
refuses a snapshot whose code, configuration or storyteller differ (``SnapshotMismatch``).
"""
from __future__ import annotations

import collections
import copy
import hashlib
import json
import random
import sys

from utils.config_loader import config

from . import version
from .actions import Action, Outcome
from .combat import CheckResult, CombatSystem
from .context import EngineContext, with_state_context
from .conditions import Condition
from .effects import EffectList, writes
from .health import HealthPlayer
from .narration import Narrator
from .stats import CharacterStats
from .world import Character, Event, ItemInstance, Location, Mission, Object

FORMAT = "ane.snapshot/1"
SKIP = {"_context", "_decision_trace", "_site_index", "_question_cache", "_ranking_after_free_step", "_turn", "_store", "facts", "world", "genome", "rng", "combat", "health_player", "stats",
        "narrate"}
from .encounters import EncounterSpec  # noqa: E402  an event's encounter block (core_engine/encounters.py)

OBJECTS = {cls.__name__: cls for cls in (Action, Outcome, Event, ItemInstance, CheckResult, EncounterSpec)}


class SnapshotError(ValueError):
    """A snapshot that cannot be made or read."""


class SnapshotMismatch(SnapshotError):
    """A snapshot made with other code, another configuration or another storyteller."""


def config_hash(config_data=None):
    data = config.as_dict() if config_data is None else config_data
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:20]


# ---- encoding ---------------------------------------------------------------------------------

def _encode(value, path, dynamic_events):
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # NaN and infinity are not JSON; an integral float (1.0) is written 1 by many JSON
        # libraries (C#, JavaScript) and comes back an int: as text, every library keeps it.
        if value != value or value in (float("inf"), float("-inf")) or value.is_integer():
            return {"$f": repr(value)}
        return value
    if isinstance(value, list):
        return [_encode(v, f"{path}[{i}]", dynamic_events) for i, v in enumerate(value)]
    if isinstance(value, tuple):
        return {"$t": [_encode(v, f"{path}[{i}]", dynamic_events) for i, v in enumerate(value)]}
    if isinstance(value, frozenset):
        return {"$fs": _sorted([_encode(v, f"{path}{{}}", dynamic_events) for v in value])}
    if isinstance(value, set):
        return {"$set": _sorted([_encode(v, f"{path}{{}}", dynamic_events) for v in value])}
    if isinstance(value, collections.Counter):
        return {"$ctr": [[_encode(k, f"{path}<key>", dynamic_events), _encode(v, f"{path}[{k!r}]", dynamic_events)]
                         for k, v in value.items()]}
    if isinstance(value, collections.defaultdict):
        factory = {list: "list", set: "set", dict: "dict", int: "int"}.get(value.default_factory)
        if factory is None:
            raise SnapshotError(f"{path}: a defaultdict of {value.default_factory!r} cannot be saved.")
        return {"$dd": factory, "items": _encode(dict(value), path, dynamic_events)}
    if isinstance(value, dict):
        if all(isinstance(k, str) and not k.startswith("$") for k in value):
            return {k: _encode(v, f"{path}.{k}", dynamic_events) for k, v in value.items()}
        return {"$map": [[_encode(k, f"{path}<key>", dynamic_events), _encode(v, f"{path}[{k!r}]", dynamic_events)]
                         for k, v in value.items()]}
    if isinstance(value, Location):
        return {"$loc": value.name}
    if isinstance(value, Character):
        return {"$npc": value.name}
    if isinstance(value, Object):
        return {"$obj": value.name}
    if isinstance(value, Mission):
        return {"$mission": value.name}
    if isinstance(value, Event):
        return {"$event": value.name}  # dynamic events are saved whole with the world's changes
    if isinstance(value, random.Random):
        return {"$rng": _encode(value.getstate(), path, dynamic_events)}
    if isinstance(value, EffectList):
        return {"$effects": _encode(list(value.items), path, dynamic_events), "checked": bool(value.checked)}
    if isinstance(value, Condition):
        return {"$cond": _encode(value.source, path, dynamic_events)}
    name = type(value).__name__
    if name in OBJECTS and type(value) is OBJECTS[name]:
        return {"$o": name, "a": _encode(dict(vars(value)), path, dynamic_events)}
    raise SnapshotError(f"{path}: a {type(value).__name__} cannot be saved in a snapshot.")


def _sorted(items):
    """A set's elements in a fixed order (sets have none worth keeping): the snapshot is canonical."""
    return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, default=str))


def _event_whole(event):
    return {"$o": "Event", "a": _encode(dict(vars(event)), f"world.events[{event.name}]", None)}


@with_state_context
def dump(state, extra=None):
    """The snapshot of a story in progress: a JSON-ready dict."""
    if state.__dict__.get("_turn") is not None:
        raise SnapshotError("A story cannot be saved in the middle of a turn.")
    if not isinstance(state.rng, random.Random):
        raise SnapshotError("The story's dice must be a random.Random to be saved.")
    authored = set((config.get("world_definition") or {}).get("events") or {})
    dynamic = {name: _event_whole(event) for name, event in state.world.events.items() if name not in authored}
    attributes = {}
    for key, value in state.__dict__.items():
        if key in SKIP:
            continue
        attributes[key] = _encode(value, key, dynamic)
    combat = {k: _encode(v, f"combat.{k}", dynamic) for k, v in vars(state.combat).items()
              if k not in ("stats", "world", "rng")}
    return {
        "format": FORMAT,
        "code_version": version.code_version(),
        "python": ".".join(map(str, sys.version_info[:2])),
        "config_hash": config_hash(),
        "genome": state.genome.traits(),
        "class": type(state).__name__,
        "world": {
            "connected": {name: sorted(location.connected) for name, location in state.world.locations.items()},
            "events": dynamic,
        },
        "rng": _encode(state.rng.getstate(), "rng", dynamic),
        "health": dict(vars(state.health_player)),
        "stats": {"mode": state.stats.mode, "points": dict(state.stats.points), "changes": dict(state.stats.changes),
                  "names": list(state.stats.names)},
        "combat": combat,
        "narrate": {"uses": dict(state.narrate.uses)},
        "attributes": attributes,
        **(extra or {}),
    }


def _plain_numbers(value):
    """Integral floats as ints, so that the checksum does not depend on how the UNP's JSON library
    writes 1.0 (C# and JavaScript write 1)."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _plain_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain_numbers(v) for v in value]
    return value


def canonical(data):
    """The snapshot as canonical JSON text (for its checksum)."""
    return json.dumps(_plain_numbers(data), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def checksum(data):
    return hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()


# ---- decoding ---------------------------------------------------------------------------------

class _Decoder:
    def __init__(self, world):
        self.world = world
        self.characters = {c.name: c for c in world.characters}
        self.objects = {o.name: o for o in world.objects}

    def __call__(self, value):
        if isinstance(value, list):
            return [self(v) for v in value]
        if not isinstance(value, dict):
            return value
        tag = next((k for k in value if k.startswith("$")), None)  # real "$" keys are escaped ($map)
        if tag is None:
            return {k: self(v) for k, v in value.items()}
        body = value[tag]
        if tag == "$t":
            return tuple(self(v) for v in body)
        if tag == "$set":
            return {self(v) for v in body}
        if tag == "$fs":
            return frozenset(self(v) for v in body)
        if tag == "$ctr":
            counter = collections.Counter()
            for k, v in body:
                counter[self(k)] = self(v)
            return counter
        if tag == "$dd":
            factory = {"list": list, "set": set, "dict": dict, "int": int}[body]
            result = collections.defaultdict(factory)
            result.update(self(value["items"]))
            return result
        if tag == "$map":
            return {self(k): self(v) for k, v in body}
        if tag == "$f":
            return float(body)
        if tag == "$loc":
            return self._named(self.world.locations, body, "location")
        if tag == "$npc":
            return self._named(self.characters, body, "NPC")
        if tag == "$obj":
            return self._named(self.objects, body, "item")
        if tag == "$mission":
            return self._named(self.world.missions, body, "mission")
        if tag == "$event":
            return self._named(self.world.events, body, "event")
        if tag == "$rng":
            rng = random.Random()
            rng.setstate(_state_tuple(self(body)))
            return rng
        if tag == "$effects":
            effect_list = EffectList.__new__(EffectList)
            effect_list.items = [tuple(item) if isinstance(item, list) else item for item in self(body)]
            effect_list.checked = bool(value.get("checked"))
            effect_list.writes = writes(effect_list.items)
            return effect_list
        if tag == "$cond":
            return Condition(self(body))
        if tag == "$o":
            cls = OBJECTS.get(body)
            if cls is None:
                raise SnapshotError(f"Unknown object `{body}` in the snapshot.")
            obj = cls.__new__(cls)
            obj.__dict__.update(self(value["a"]))
            return obj
        raise SnapshotError(f"Unknown tag `{tag}` in the snapshot.")

    @staticmethod
    def _named(table, name, what):
        if name not in table:
            raise SnapshotMismatch(f"The snapshot names the {what} `{name}`, which this world does not have.")
        return table[name]


def _state_tuple(value):
    """random.Random's state as setstate wants it (tuples all the way down)."""
    if isinstance(value, (list, tuple)):
        return tuple(_state_tuple(v) for v in value)
    return value


def check(data, genome_traits=None):
    """Raise SnapshotMismatch unless the snapshot fits this code, configuration and storyteller."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise SnapshotError("Not an ane.snapshot/1 snapshot.")
    if data.get("code_version") != version.code_version():
        raise SnapshotMismatch(f"made by engine code {data.get('code_version')}, this is {version.code_version()}")
    if data.get("config_hash") != config_hash():
        raise SnapshotMismatch("made with another world or tuning (configuration)")
    from .genome import Genome

    # A snapshot made before a trait existed has its default (Genome.added_field_defaults).
    if genome_traits is not None and not Genome.same(data.get("genome"), genome_traits):
        raise SnapshotMismatch("made with another storyteller")


def load(data, store=None, genome=None):
    """The GameState a snapshot describes, ready to play on (``store``: the profile store of
    real play, None for none). Raises SnapshotMismatch/SnapshotError."""
    from .builder import build_world
    from .conditions import StoryFacts
    from .game_state import GameState
    from .genome import Genome

    genome = genome or Genome.from_traits(data.get("genome") or {})
    check(data, genome.traits())
    world = build_world(genome)
    decode = _Decoder(world)
    for name, event in (data["world"].get("events") or {}).items():
        world.events[name] = decode(event)
    for name, connected in (data["world"].get("connected") or {}).items():
        if name not in world.locations:
            raise SnapshotMismatch(f"The snapshot names the location `{name}`, which this world does not have.")
        world.locations[name].connected = set(connected)
    state = GameState.__new__(GameState)
    state.__dict__["_context"] = EngineContext.current(genome)  # the configuration it was checked against
    state.__dict__.update({key: decode(value) for key, value in data["attributes"].items() if key != "_context"})
    state.world = world
    state.genome = genome
    state.rng = random.Random()
    state.rng.setstate(_state_tuple(decode(data["rng"])))
    state.health_player = HealthPlayer.__new__(HealthPlayer)
    state.health_player.__dict__.update(copy.deepcopy(data["health"]))
    stats = CharacterStats.__new__(CharacterStats)
    stats_cfg = CharacterStats(mode="balanced")  # the configuration's stats, budget and settings
    if list(stats_cfg.names) != list(data["stats"]["names"]):
        raise SnapshotMismatch("made with other character stats")
    stats.__dict__.update({"settings": stats_cfg.settings, "names": list(stats_cfg.names), "budget": stats_cfg.budget,
                           "mode": data["stats"]["mode"], "points": dict(data["stats"]["points"]),
                           "changes": dict(data["stats"]["changes"])})
    state.stats = stats
    combat = CombatSystem.__new__(CombatSystem)
    combat.__dict__.update({key: decode(value) for key, value in data["combat"].items()})
    combat.stats, combat.world, combat.rng = stats, world, state.rng
    state.combat = combat
    state.narrate = Narrator()
    state.narrate.uses = dict(data["narrate"]["uses"])
    state.facts = StoryFacts(state)
    state._store = store
    state._turn = None
    return state
