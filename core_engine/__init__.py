from .genome import Genome
from .world import Location, WorldGraph, Character, Object, Event, Mission
from .actions import Action, Outcome
from .health import HealthPlayer
from .game_state import GameState


def __getattr__(name):
    # The LLM augmenter (torch, transformers) is only loaded when it is used, so the
    # engine itself runs without them (e.g. in an offline engine export).
    if name == "WorldAugmenter":
        from .augmenter import WorldAugmenter
        return WorldAugmenter
    raise AttributeError(f"module 'core_engine' has no attribute {name!r}")
