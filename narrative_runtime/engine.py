"""Embed an exported Adaptive Narrative engine in a game.

    from narrative_runtime import NarrativeEngine

    engine = NarrativeEngine()                 # the export this package belongs to
    game = engine.new_game(profile="player-1", stats_mode="random")   # start="Harbor" to choose the start
    view = game.view()                         # scene, choices, health, mission, ...
    turn = game.choose(view["actions"][0])     # by name or index
    game.save("slot1.game"); game = engine.load_game("slot1.game")

Everything runs offline: the export carries the engine code, the world and
tuning (config/), the evolved storyteller (engine.json), the tag vectors it
was trained with (data/tag_vectors.json) and its learning memory (saves/).
"""
from __future__ import annotations

import json
import pickle
import random
import uuid
from pathlib import Path

FORMAT = "adaptive-narrative-engine"


class Game:
    """One playthrough. All methods return plain JSON-serialisable data."""

    def __init__(self, engine, state, game_id=None):
        self.engine = engine
        self.state = state
        self.id = game_id or uuid.uuid4().hex

    def actions(self) -> list[str]:
        """The choices the player sees now: the most relevant ones (player_options in the tuning)."""
        if self.state.ended:
            return []
        if hasattr(self.state, "player_options"):
            return [action.name for action in self.state.player_options()]
        return [action.name for action in self.state.ranked_available_actions()]

    def view(self) -> dict:
        """What the player sees now."""
        state = self.state
        actions = self.actions()  # also sets up the scene and starts encounters
        description, npcs, items = state._describe_visible_scene()
        bonuses = state.combat.item_bonuses(state.inventory)
        last = state.combat.history[-1] if state.combat.history else None
        return {
            "game_id": self.id,
            "location": state.current.name,
            "start_location": state.start_location,
            "scene": state.scene_name,
            "description": description,
            "npcs": [name for name, _ in npcs],
            "items": [name for name, _ in items],
            "actions": actions,
            "health": state.health,
            "max_health": state.max_health,
            "coins": state.coins,
            "power": round(state.power, 3),
            "inventory": sorted(state.inventory),
            "stats": state.stats.values(bonuses),
            "last_check": vars(last).copy() if last else None,
            "mission": state.mission.title if state.mission else None,
            "mission_description": state.mission.description if state.mission else None,
            "mission_status": state.mission_status,
            "quest": state.quest_log() if hasattr(state, "quest_log") else None,
            "people": state.people_here() if hasattr(state, "people_here") and not state.ended else [],
            "people_met": state.people_met() if hasattr(state, "people_met") else [],
            "ways_on": state.ways_on() if hasattr(state, "ways_on") and not state.ended else [],
            "map": state.map_view() if hasattr(state, "map_view") else None,
            "choices": [{"name": name, "check": state.check_hint(a)} for name, a in
                        zip(actions, state.player_options())] if hasattr(state, "check_hint") and actions else [],
            "event": state.active_event.title if state.active_event else None,
            "variables": {k: v for k, v in state.variables.items() if k in state.variable_specs or k == "playthroughs"},
            "ended": bool(state.ended),
            "won": bool(state.won),
            "end_reason": state.end_reason,
            "turn_number": state.actions_taken,
        }

    def choose(self, action) -> dict:
        """Take an action (its name or its index in ``actions()``)."""
        names = self.actions()
        if not names:
            raise ValueError("The story has ended.")
        index = action if isinstance(action, int) else names.index(action) if action in names else -1
        if not 0 <= index < len(names):
            raise ValueError(f"Unknown action {action!r}. Available: {names}")
        ranked = [option.name for option in self.state.ranked_available_actions()]  # step() indexes the ranked order
        before = len(self.state.history)
        self.state.step(ranked.index(names[index]))
        turn = "\n".join(line.strip("\n") for line in self.state.history[before:]).strip()
        return {**self.view(), "turn": turn}  # "turn": the story text of this turn

    def transcript(self) -> str:
        return self.state.get_full_story()

    def save(self, path) -> str:
        """Save the game (same engine export required to load it)."""
        path = Path(path)
        store, self.state._store = self.state._store, None  # the store is re-attached on load
        try:
            path.write_bytes(pickle.dumps({"format": FORMAT, "game_id": self.id, "state": self.state}))
        finally:
            self.state._store = store
        return str(path)


class NarrativeEngine:
    """An exported engine: world, tuning, and the evolved storyteller."""

    def __init__(self, root=None):
        self.root = Path(root or Path(__file__).resolve().parent.parent).resolve()
        manifest_path = self.root / "engine.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"No engine.json in {self.root}: not an Adaptive Narrative engine export.")
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if self.manifest.get("format") != FORMAT:
            raise ValueError("engine.json is not an Adaptive Narrative engine manifest.")

        # Point the engine at this export's configuration and training tag vectors.
        from utils.config_loader import config
        from utils.tag_similarity import load_tag_vectors

        config._config_dir = str(self.root / "config")
        config.clear_override()
        vectors = self.root / "data" / "tag_vectors.json"
        if vectors.exists():
            load_tag_vectors(json.loads(vectors.read_text(encoding="utf-8")))

        from core_engine import Genome
        from core_engine.evolution import LearningMemory
        from core_engine.variables import VariableStore

        self.genome = Genome.from_traits(self.manifest["genome"])
        self.store = VariableStore(str(self.root / "saves" / "persistent_variables.json"))
        self.memory = LearningMemory(str(self.root / "saves" / "evolution_memory.json"))
        self.games: dict[str, Game] = {}

    @classmethod
    def load(cls, root=None) -> "NarrativeEngine":
        return cls(root)

    def info(self) -> dict:
        from utils.config_loader import config

        world = config.get("world_definition") or {}
        return {
            "name": self.manifest.get("name"),
            "created": self.manifest.get("created"),
            "source": self.manifest.get("source"),
            "score": self.manifest.get("score"),
            "genome": self.genome.traits(),
            "locations": list(world.get("locations") or {}),
            "starting_locations": self.starting_locations(),
            "npcs": [c.get("name") for c in world.get("characters") or []],
            "missions": {k: v.get("title", k) for k, v in (world.get("missions") or {}).items()},
            "stats": list((world.get("character_stats") or {}).get("stats") or {}),
            "profiles": self.store.profiles(),
            "remembered_storytellers": len(self.memory.load()),
        }

    def starting_locations(self) -> list[str]:
        """Where a story may start (``start_eligible`` locations, or the single
        default start of a world that marks none)."""
        from utils.config_loader import config
        from core_engine import starts

        locations = (config.get("world_definition") or {}).get("locations") or {}
        eligible = [name for name, data in locations.items() if (data or {}).get("start_eligible")]
        default = starts.settings()["default_location"]
        return eligible or [default if default in locations else next(iter(locations), default)]

    def new_game(self, profile=None, stats=None, stats_mode=None, seed=None, mission=None, persist=True,
                 start=None) -> Game:
        """Start a playthrough. ``persist`` saves persistent variables and the start
        history to ``profile``; ``stats`` are the player's own points, or
        ``stats_mode`` random/balanced/defined.

        ``start`` picks one of ``starting_locations()``. By default the start is
        drawn among them, weighted by ``start_weight``, less often where the
        profile's recent stories began, and only where the mission can be
        reached. A requested start that the profile's persistent variables
        switch off (``enabled_if``) is drawn the same way instead."""
        from core_engine import GameState
        from core_engine.builder import build_world

        if start is not None and start not in self.starting_locations():
            raise ValueError(f"{start!r} is not a starting location. Choose one of: {', '.join(self.starting_locations())}.")
        state = GameState(
            build_world(self.genome), self.genome, rng=random.Random(seed), mission=mission, start=start,
            store=self.store if persist else None, profile=profile, stats=stats, stats_mode=stats_mode,
        )
        game = Game(self, state)
        self.games[game.id] = game
        return game

    def load_game(self, path) -> Game:
        data = pickle.loads(Path(path).read_bytes())
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise ValueError("Not a saved Adaptive Narrative game.")
        state = data["state"]
        state._store = self.store
        game = Game(self, state, data.get("game_id"))
        self.games[game.id] = game
        return game

    def evolve(self, generations=5, pop_size=12, episodes=3, max_steps=60, seed=None, adopt=True, progress=None,
               resume=False) -> dict:
        """Keep training offline, starting from what the engine remembers. With
        ``resume`` (and a saved training state, data/training.json) the training
        continues exactly where it stopped, for ``generations`` more generations.
        With ``adopt`` the engine plays with the new champion if it beats the current one."""
        from core_engine.builder import build_world
        from core_engine.evolution import NarrativeEvolution, evaluate_task
        from core_engine.fitness import fitness

        training = self.root / "data" / "training.json"
        if resume:
            if not training.exists():
                raise ValueError("This engine has no saved training state to resume.")
            checkpoint = json.loads(training.read_text(encoding="utf-8"))
            evolution = NarrativeEvolution.from_checkpoint(build_world, fitness, checkpoint, generations=generations,
                                                           memory=self.memory)
            result = evolution.run(progress=progress, resume=checkpoint)
        else:
            settings = {"generations": generations, "pop_size": pop_size, "episodes_per_genome": episodes,
                        "max_steps": max_steps, "warm_start": True}
            evolution = NarrativeEvolution(build_world, fitness, settings=settings, seed=seed, memory=self.memory)
            result = evolution.run(progress=progress)
        training.parent.mkdir(parents=True, exist_ok=True)
        training.write_text(json.dumps(evolution.last_checkpoint), encoding="utf-8")
        summary = result.summary()
        # Compare the current and the new storyteller on the same stories.
        # The same benchmark stories (seeds, situations, length) for both storytellers.
        current = evaluate_task(evolution.benchmark_task(self.genome.traits()))
        challenger = evaluate_task(evolution.benchmark_task(result.best_genome.traits()))
        summary["current_storyteller_score"] = round(current, 3)
        summary["new_storyteller_score"] = round(challenger, 3)
        summary["adopted"] = bool(adopt and challenger > current)
        if summary["adopted"]:
            self.genome = result.best_genome
            self.manifest.update(genome=self.genome.traits(), score=result.best_score,
                                 source="offline training", learning=summary)
            (self.root / "engine.json").write_text(json.dumps(self.manifest, indent=2), encoding="utf-8")
        return summary
