"""Embed an exported Adaptive Narrative engine (an "ANE AI") in a game, or let a UNP drive it.

    from narrative_runtime import NarrativeEngine

    engine = NarrativeEngine()                 # the export this package belongs to
    game = engine.new_game(profile="player-1", stats_mode="random")   # start="Harbor" to choose the start
    view = game.view()                         # scene, choices, health, mission, ...
    turn = game.choose(view["actions"][0])     # by name or index
    game.save("slot1.json"); game = engine.load_game("slot1.json")   # an ane.savegame/1 document

Everything runs offline: the export carries the world and tuning (config/), the evolved
storyteller (engine.json) and the tag vectors it was trained with (data/tag_vectors.json); a
standalone build also carries the engine code and Python itself. Profiles, saves and the
content a UNP sends are kept in the data folder (``data_dir``), never inside the package.

For a UNP (core of narrative_runtime/server.py):
- ``catalogue()``: the world as parameters (locations, NPCs, items, events, missions, stats, ...);
- ``patch_world(patch)``: world content from the UNP (validated like the studio does);
- ``new_game(player=...)``, ``Game.update_player``, ``Game.sync``: the player's state from the UNP;
- ``Game.savegame()``, ``Game.cancel()``, ``load_savegame(doc)``: save games (ane.savegame/1);
- ``shutdown()``: every active session saved, everything delivered, ready to exit.
"""
from __future__ import annotations

import copy
import datetime
import json
import logging
import os
import pickle
import random
import shutil
import sys
import threading
import uuid
from pathlib import Path

FORMAT = "adaptive-narrative-engine"
LOGGER = logging.getLogger(__name__)


def package_root():
    """The folder of the engine package: next to the program for a standalone build, else the
    folder this package was exported into."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def default_data_dir(name):
    """Where a standalone build keeps its data: the user's application-data folder."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "ANE-AI" / name


class PatchError(ValueError):
    """World content from the UNP that does not fit (every problem is listed)."""

    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = list(errors)


class Game:
    """One playthrough (a session). All methods return plain JSON-serialisable data."""

    def __init__(self, engine, state, game_id=None):
        self.engine = engine
        self.state = state
        self.id = game_id or uuid.uuid4().hex
        state.session_id = self.id  # the session's id in its report and save game

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
            "stakes": state.stakes() if hasattr(state, "stakes") else None,
            "cast": state.scope() if hasattr(state, "scope") else None,
            "unique_actions": state.unique_actions() if hasattr(state, "unique_actions") else [],
            "session_report": state.__dict__.get("session_report"),
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

    # ---- the session as parameters, set by the UNP ------------------------------------------

    def parameters(self) -> dict:
        """The session's parameters (ane.session_state/1) and what this session made of the world."""
        from core_engine import resume

        state = self.state
        self.actions()  # the scene the player sees
        return {**resume.parameters(state), "session": {
            "cast": state.scope() if hasattr(state, "scope") else None,
            "npc_roles": dict(state.__dict__.get("npc_roles") or {}),
            "npc_homes": dict(state.npc_homes),
        }}

    def update_player(self, player) -> dict:
        """Set the player's stats, inventory, coins, health or variables (from the UNP)."""
        from core_engine import resume

        applied = resume.apply_player(self.state, player)
        return {**self.view(), "applied": applied}

    def sync(self, events) -> dict:
        """Take what happened in the UNP into the session: one event or {"events": [...]}."""
        from core_engine import resume

        events = events.get("events") if isinstance(events, dict) and "events" in events else events
        events = events if isinstance(events, list) else [events]
        taken = [resume.sync(self.state, dict(event)) for event in events]
        return {**self.view(), "synced": taken}

    # ---- save games -------------------------------------------------------------------------

    def savegame(self) -> dict:
        """The session's save game (ane.savegame/1): its parameters and an exact snapshot."""
        from core_engine import savegame

        self.actions()  # the scene as the player sees it is part of what is saved
        return savegame.make(self.state, self.engine.describe())

    def cancel(self, release=True) -> dict:
        """The UNP cancels the session: its save game, its status (outcome `suspended`, not a
        finished playthrough) kept in the profile and sent to the UNP; then it is released."""
        from core_engine import progression

        doc = self.savegame()
        status = None
        if not self.state.ended:
            status = progression.build_report(self.state)
            status["outcome"] = "suspended"
            status["savegame_id"] = doc["checksum"][:16]
            store = self.state.__dict__.get("_store")
            if store is not None:
                store.record_session(self.state.profile, status)
        self.engine.notify("session.cancelled", {"status": status, "savegame": doc}, self.id)
        if release:
            self.engine.games.pop(self.id, None)
        return {"game_id": self.id, "status": status, "savegame": doc}

    def end(self) -> dict:
        """End the session where it stands (the player stops): the player's progression is saved
        and the session reported (to the UNP when ``session_report.url`` or ANE_UNP_URL is set)."""
        if hasattr(self.state, "end_session"):
            self.state.end_session()
        return self.view()

    def report(self) -> dict | None:
        """The session's report once it has ended (None while it goes on)."""
        return self.state.__dict__.get("session_report") if self.state.ended else None

    def transcript(self) -> str:
        return self.state.get_full_story()

    def save(self, path) -> str:
        """Save the game as an ane.savegame/1 JSON document (load it with NarrativeEngine.load_game)."""
        path = Path(path)
        path.write_text(json.dumps(self.savegame()), encoding="utf-8")
        return str(path)


class NarrativeEngine:
    """An exported engine (an ANE AI): world, tuning, and the evolved storyteller."""

    def __init__(self, root=None, data_dir=None):
        self.root = Path(root or package_root()).resolve()
        manifest_path = self.root / "engine.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"No engine.json in {self.root}: not an Adaptive Narrative engine export.")
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if self.manifest.get("format") != FORMAT:
            raise ValueError("engine.json is not an Adaptive Narrative engine manifest.")
        name = str(self.manifest.get("name") or "engine")
        if data_dir is None:
            data_dir = default_data_dir(name) if getattr(sys, "frozen", False) else self.root / "saves"
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

        # Point the engine at this export's configuration and training tag vectors.
        from utils.config_loader import config
        from utils.tag_similarity import load_tag_vectors

        config._config_dir = str(self.root / "config")
        config.clear_override()
        vectors = self.root / "data" / "tag_vectors.json"
        self.vector_tags = set()
        if vectors.exists():
            data = json.loads(vectors.read_text(encoding="utf-8"))
            load_tag_vectors(data)
            self.vector_tags = set(data)
        os.environ.setdefault("ANE_SAVE_DIR", str(self.data_dir))

        from core_engine import Genome
        from core_engine.evolution import LearningMemory
        from core_engine.variables import VariableStore

        shipped_memory = self.root / "saves" / "evolution_memory.json"
        memory_path = self.data_dir / "evolution_memory.json"
        if shipped_memory.exists() and not memory_path.exists() and shipped_memory.resolve() != memory_path.resolve():
            shutil.copy2(shipped_memory, memory_path)  # the learning memory shipped with the export
        self.genome = Genome.from_traits(self.manifest["genome"])
        self.store = VariableStore(str(self.data_dir / "persistent_variables.json"))
        self.memory = LearningMemory(str(memory_path))
        self.games: dict[str, Game] = {}
        self.base_config = config.as_dict()
        self.revision = 0
        self.patches = self._load_patches()
        self._apply_patches()

    @classmethod
    def load(cls, root=None, data_dir=None) -> "NarrativeEngine":
        return cls(root, data_dir)

    def describe(self) -> dict:
        """Who this engine is, for save games and events."""
        from core_engine import version

        return {"package": self.manifest.get("name"), "created": self.manifest.get("created"),
                "code_version": version.code_version(), "revision": self.revision}

    def info(self) -> dict:
        from core_engine import version
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
            "code_version": version.code_version(),
            "protocol": version.PROTOCOL,
            "data_dir": str(self.data_dir),
            "world_revision": self.revision,
            "games": sorted(self.games),
        }

    def progression(self, profile=None) -> dict:
        """A player's save game: goals achieved (each once), playthroughs, session reports."""
        from utils.config_loader import config

        return self.store.progression(profile or config.get("persistence.profile", "default"))

    def starting_locations(self) -> list[str]:
        """Where a story may start (``start_eligible`` locations, or the single
        default start of a world that marks none)."""
        from utils.config_loader import config
        from core_engine import starts

        locations = (config.get("world_definition") or {}).get("locations") or {}
        eligible = [name for name, data in locations.items() if (data or {}).get("start_eligible")]
        default = starts.settings()["default_location"]
        return eligible or [default if default in locations else next(iter(locations), default)]

    # ---- the world as parameters (ANE -> UNP), and world content from the UNP --------------------

    def catalogue(self) -> dict:
        from utils.config_loader import config

        from .world import world_catalogue

        return world_catalogue(config.as_dict(), self.starting_locations(), revision=self.revision,
                               engine=self.manifest.get("name"))

    def entity(self, kind, name) -> dict:
        from utils.config_loader import config

        from .world import world_entity

        return world_entity(config.as_dict(), kind, name)

    def _patches_path(self):
        return self.data_dir / "unp_patches.json"

    def _load_patches(self):
        path = self._patches_path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return list(data.get("patches") or [])
        except (OSError, ValueError) as exc:
            LOGGER.warning("Ignoring unreadable UNP patches %s: %s", path, exc)
            return []

    def _apply_patches(self):
        """Apply the kept patches on the package's configuration, in order (a patch that no
        longer fits is skipped and reported in the log)."""
        from utils.config_loader import config

        from .world import apply_patch, patch_errors

        active, kept = copy.deepcopy(self.base_config), []
        for record in self.patches:
            candidate = apply_patch(active, record["patch"])
            errors = patch_errors(candidate)
            if errors:
                LOGGER.warning("Skipping UNP patch %s: %s", record.get("id"), "; ".join(errors[:3]))
                continue
            active = candidate
            kept.append(record)
        self.patches = kept
        self.revision = len(kept)
        if kept:
            config.use_override(active)

    def patch_world(self, patch) -> dict:
        """World content from the UNP: validated like the studio does; new sessions use it.
        ``persist`` writes it into the package; otherwise it is kept in the data folder."""
        from core_engine.builder import build_world
        from utils.config_loader import config

        from .world import apply_patch, new_tags, patch_errors

        with self.lock:
            current = config.as_dict()
            candidate = apply_patch(current, patch)
            errors = patch_errors(candidate)
            if errors:
                raise PatchError(errors)
            config.use_override(candidate)
            try:
                build_world(self.genome)
            except Exception as exc:  # noqa: BLE001 - reported to the UNP
                config.use_override(current)
                raise PatchError([f"The world cannot be built with this content: {exc}"]) from exc
            warnings = [f"The tag `{tag}` has no trained vector: it is compared as unknown."
                        for tag in sorted(new_tags(candidate, self.vector_tags))]
            record = {"id": uuid.uuid4().hex, "at": datetime.datetime.now(datetime.timezone.utc).isoformat(
                timespec="seconds"), "patch": {k: v for k, v in patch.items() if k != "persist"}}
            if patch.get("persist"):
                import yaml

                (self.root / "config" / "engine.yaml").write_text(
                    yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True), encoding="utf-8")
                self.base_config = candidate
                self.patches = []
            else:
                self.patches.append(record)
            self._patches_path().write_text(json.dumps({"patches": self.patches}, indent=2), encoding="utf-8")
            self.revision += 1
            return {"revision": self.revision, "persisted": bool(patch.get("persist")), "warnings": warnings,
                    "patch_id": record["id"]}

    def reset_patches(self) -> dict:
        """Forget the UNP's content kept in the data folder (the package's own world again)."""
        from utils.config_loader import config

        with self.lock:
            self.patches = []
            self._patches_path().write_text(json.dumps({"patches": []}), encoding="utf-8")
            config.use_override(self.base_config)
            self.revision += 1
            return {"revision": self.revision}

    # ---- sessions ---------------------------------------------------------------------------------

    def new_game(self, profile=None, stats=None, stats_mode=None, seed=None, mission=None, persist=True,
                 start=None, player=None) -> Game:
        """Start a playthrough. ``persist`` saves persistent variables and the start
        history to ``profile``; ``stats`` are the player's own points, or
        ``stats_mode`` random/balanced/defined; ``player`` sets the player's state from the UNP
        (stat_points, stat_changes, inventory, coins, health, max_health, variables).

        ``start`` picks one of ``starting_locations()``. By default the start is
        drawn among them, weighted by ``start_weight``, less often where the
        profile's recent stories began, and only where the mission can be
        reached. A requested start that the profile's persistent variables
        switch off (``enabled_if``) is drawn the same way instead."""
        from core_engine import GameState
        from core_engine.builder import build_world

        if start is not None and start not in self.starting_locations():
            raise ValueError(f"{start!r} is not a starting location. Choose one of: {', '.join(self.starting_locations())}.")
        player = dict(player or {})
        if "stat_points" in player and stats is None:
            stats = player.pop("stat_points")
        state = GameState(
            build_world(self.genome), self.genome, rng=random.Random(seed), mission=mission, start=start,
            store=self.store if persist else None, profile=profile, stats=stats, stats_mode=stats_mode,
        )
        if player:
            from core_engine import resume

            resume.apply_player(state, player)
        game = Game(self, state)
        self.games[game.id] = game
        self.notify("session.started", {"game_id": game.id, "profile": state.profile, "start": state.start_location,
                                        "goal": state.mission.name if state.mission else None}, game.id)
        return game

    def load_savegame(self, doc, profile=None, persist=True, seed=None) -> tuple[Game, str, str | None]:
        """Continue a session from a save game (ane.savegame/1): exactly when its snapshot fits
        this engine, else rebuilt from its parameters. Returns (game, how, note)."""
        from core_engine import savegame

        state, how, note = savegame.load(doc, self.genome, store=self.store if persist else None,
                                         profile=profile, seed=seed)
        game = Game(self, state, doc.get("session_id") or None)
        self.games[game.id] = game
        self.notify("session.started", {"game_id": game.id, "profile": state.profile, "restored": how}, game.id)
        return game, how, note

    def load_game(self, path) -> Game:
        """A game saved with Game.save (an ane.savegame/1 JSON document). Games saved by older
        versions as pickles still load from a local file (never from data the UNP sends)."""
        raw = Path(path).read_bytes()
        if raw.lstrip()[:1] == b"{":
            return self.load_savegame(json.loads(raw.decode("utf-8")))[0]
        LOGGER.warning("Loading %s, a game saved in the old pickle format: save it again to convert it.", path)
        data = pickle.loads(raw)
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise ValueError("Not a saved Adaptive Narrative game.")
        state = data["state"]
        state._store = self.store
        game = Game(self, state, data.get("game_id"))
        self.games[game.id] = game
        return game

    def save_suspended(self, game) -> str:
        """Keep a session's save game in the data folder (data_dir/suspended/<game id>.json)."""
        folder = self.data_dir / "suspended"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{game.id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(game.savegame()), encoding="utf-8")
        os.replace(temporary, path)
        return str(path)

    # ---- talking to the UNP, and stopping ---------------------------------------------------------

    def notify(self, event, data=None, session_id=None, wait=False):
        """Send an event to the UNP's callback (session_report.url / ANE_UNP_URL), if one is set."""
        from .lifecycle import send_event

        return send_event(event, data, session_id=session_id, engine=self.describe(), wait=wait)

    def shutdown(self, save=True, end_sessions=False) -> dict:
        """Before the process stops: every active session saved (or ended, with ``end_sessions``)
        and every report and event delivered. Safe to call twice."""
        from .lifecycle import wait_for_deliveries

        result = {"saved": [], "ended": []}
        with self.lock:
            for game in list(self.games.values()):
                try:
                    if end_sessions and not game.state.ended:
                        game.end()
                        result["ended"].append({"game_id": game.id, "report": game.report()})
                    elif save and not game.state.ended:
                        outcome = game.cancel(release=False)
                        result["saved"].append({"game_id": game.id, "path": self.save_suspended(game),
                                                "status": outcome["status"]})
                except Exception as exc:  # noqa: BLE001 - one session must not keep the others from being saved
                    LOGGER.warning("Could not save session %s: %s", game.id, exc)
                    result.setdefault("errors", []).append({"game_id": game.id, "error": str(exc)})
            self.games.clear()
        self.notify("engine.stopping", {"saved": [s["game_id"] for s in result["saved"]],
                                        "ended": [e["game_id"] for e in result["ended"]]}, wait=True)
        wait_for_deliveries()
        return result

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
