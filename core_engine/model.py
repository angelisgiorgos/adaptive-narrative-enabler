"""Saved engine models: a trained storyteller with everything it needs, in one file.

    model = EngineModel.from_training(evolution, result, source="studio run")
    model.save("engine.ane.zip")
    model = EngineModel.load("engine.ane.zip")   # checks format, version and checksums
    world = model.build_world()                 # the game world it was trained for
    state = model.new_game(seed=3)              # a game to play (GameState)
    result, newer = model.continue_training(generations=5)   # exactly where training stopped
    result, retrained = model.retrain({"generations": 10})   # a new run from this storyteller

A model file is a ZIP:

    manifest.json      format, version, id, parent, source, score, created, code
                       fingerprint, world summary, and a SHA-256 of every file below
    config.yaml        the complete configuration: world and all tuning (plus the
                       ``generated_run`` block older studio versions read)
    genome.json        the storyteller (its traits)
    training.json      the evolution checkpoint (population, generation, mutation
                       step, random state, benchmark, history), if it was trained here
    memory.json        its hall of fame (the storytellers retraining starts from)
    tag_vectors.json   the tag vectors it was trained with (identical behaviour anywhere)
    best_model.yaml    the genome in the older bundle format
    README.txt

It is also a valid studio bundle: "Load a saved engine" accepts it.
"""
import datetime
import hashlib
import io
import json
import os
import tempfile
import uuid
import zipfile
from pathlib import Path

import yaml

from utils.config_loader import config
from utils.tag_similarity import export_tag_vectors, load_tag_vectors

from .genome import TRAIT_BOUNDS, Genome

FORMAT = "ane-engine-model"
VERSION = 1
SUFFIX = ".ane.zip"
# Tags the engine adds by itself, besides those written in the configuration (milestone: a
# quest stage completed; finale: the director's finale began); exported with every model.
ENGINE_TAGS = ["failure", "unexpected", "event", "conversation", "social", "neutral", "success", "milestone", "finale"]
CODE_DIR = Path(__file__).resolve().parent


def _collect_tags(value, key=""):
    tags = set()
    if isinstance(value, dict):
        for child_key, child in value.items():
            tags |= _collect_tags(child, str(child_key))
    elif isinstance(value, list):
        if key == "tags" or key.endswith("tags") or key.endswith("_patterns"):
            tags |= {str(item) for item in value if isinstance(item, (str, int, float))}
        for item in value:
            if isinstance(item, (dict, list)):
                tags |= _collect_tags(item, key)
    return tags


def world_tags(cfg):
    """Every tag the world can produce: written tags, generated clue tags, engine tags."""
    tags = _collect_tags(cfg) | set(ENGINE_TAGS)
    variants = int((cfg.get("fitness") or {}).get("setup_payoff", {}).get("clue_variants", 3))
    tags |= {f"{kind}_{index}" for kind in ("setup", "payoff") for index in range(1, variants + 1)}
    return {tag for tag in tags if tag}


def code_fingerprint():
    """SHA-256 of the engine code (core_engine/*.py): models trained with other code
    may behave differently."""
    digest = hashlib.sha256()
    for path in sorted(CODE_DIR.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def config_id(cfg):
    """A stable id of a configuration (its content), used to recognise the same world."""
    payload = json.dumps(cfg, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:20]


def world_summary(cfg):
    world = (cfg or {}).get("world_definition") or {}
    return {
        "locations": len(world.get("locations") or {}),
        "npcs": len(world.get("characters") or []),
        "items": len(world.get("objects") or []),
        "events": len(world.get("events") or {}),
        "missions": len(world.get("missions") or {}),
        "stats": list((world.get("character_stats") or {}).get("stats") or {}),
    }


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


class EngineModel:
    """A trained storyteller and everything it needs: configuration (world and
    tuning), genome, training state, memory and tag vectors."""

    def __init__(self, config_data, genome, score=None, checkpoint=None, memory=None, tag_vectors=None,
                 model_id=None, parent_id=None, source="", created=None, learning=None, code=None):
        self.config = {k: v for k, v in dict(config_data).items() if k != "generated_run"}
        self.genome_traits = {name: float(value) for name, value in dict(genome).items()}
        self.score = None if score is None else float(score)
        self.checkpoint = checkpoint
        self.memory = list(memory or [])
        self.tag_vectors = dict(tag_vectors or {})
        self.id = model_id or uuid.uuid4().hex
        self.parent_id = parent_id
        self.source = source
        self.created = created or _now()
        self.learning = learning
        self.code = code or code_fingerprint()

    # ---- creating -------------------------------------------------------------------

    @classmethod
    def capture(cls, genome, score=None, checkpoint=None, memory=None, source="", parent_id=None, learning=None):
        """A model of ``genome`` with the active configuration and its tag vectors."""
        active = config.as_dict()
        traits = genome.traits() if isinstance(genome, Genome) else dict(genome)
        return cls(active, traits, score=score, checkpoint=checkpoint, memory=memory,
                   tag_vectors=export_tag_vectors(world_tags(active)), source=source, parent_id=parent_id,
                   learning=learning)

    @classmethod
    def from_training(cls, evolution, result, source="", parent_id=None):
        """The best storyteller of a finished run, with the run's complete training state."""
        memory = evolution.memory.load() if evolution.memory is not None else []
        return cls.capture(result.best_genome, score=result.best_score, checkpoint=evolution.last_checkpoint,
                           memory=memory, source=source, parent_id=parent_id, learning=result.summary())

    # ---- the storyteller and its world ---------------------------------------------------

    @property
    def genome(self):
        return Genome.from_traits(self.genome_traits)

    @property
    def world_id(self):
        return config_id(self.config)

    def activate(self):
        """Make this model's world and tuning the active configuration, with the tag
        vectors it was trained with (process-wide, like the studio's upload)."""
        config.use_override(self.config)
        if self.tag_vectors:
            load_tag_vectors(self.tag_vectors)

    def build_world(self):
        """The game world (WorldGraph) this storyteller plays."""
        from .builder import build_world

        self.activate()
        return build_world(self.genome)

    def new_game(self, seed=None, **kwargs):
        """A new game (GameState) told by this storyteller; ``kwargs`` as GameState
        (start, mission, profile, store, stats, stats_mode, ...)."""
        import random

        from .game_state import GameState

        world = self.build_world()
        return GameState(world, self.genome, rng=random.Random(seed), **kwargs)

    def world_yaml(self):
        """The world and tuning as one YAML document (a combined configuration)."""
        return yaml.safe_dump(self.config, sort_keys=False, allow_unicode=True)

    # ---- training -------------------------------------------------------------------------

    def continue_training(self, generations=1, progress=None, map_fn=None, checkpoint=None):
        """Continue the saved training for ``generations`` more generations, exactly
        as if it had never stopped. Returns (EvolutionResult, the new EngineModel)."""
        from .builder import build_world
        from .evolution import ModelMemory, NarrativeEvolution
        from .fitness import fitness

        if not self.checkpoint:
            raise ValueError("This model has no training state to continue; retrain it instead.")
        self.activate()
        memory = ModelMemory(self.memory) if self.checkpoint["settings"].get("warm_start") or self.memory else None
        evolution = NarrativeEvolution.from_checkpoint(build_world, fitness, self.checkpoint, generations=generations,
                                                       memory=memory, map_fn=map_fn)
        result = evolution.run(progress=progress, resume=self.checkpoint, checkpoint=checkpoint)
        return result, EngineModel.from_training(evolution, result, source=f"continued from {self.id}", parent_id=self.id)

    def retrain(self, settings=None, seed=None, progress=None, map_fn=None, checkpoint=None):
        """A new run (``settings`` as evolution settings) whose first population starts
        from this storyteller and its memory. Returns (EvolutionResult, the new EngineModel)."""
        from .builder import build_world
        from .evolution import ModelMemory, NarrativeEvolution
        from .fitness import fitness

        self.activate()
        memory = ModelMemory(self.memory)
        memory.remember([(self.score if self.score is not None else 0.0, self.genome)],
                        size=max(len(self.memory) + 1, 12))
        evolution = NarrativeEvolution(build_world, fitness, settings={**(settings or {}), "warm_start": True},
                                       seed=seed, memory=memory, map_fn=map_fn)
        result = evolution.run(progress=progress, checkpoint=checkpoint)
        return result, EngineModel.from_training(evolution, result, source=f"retrained from {self.id}", parent_id=self.id)

    # ---- saving and loading ----------------------------------------------------------------

    def manifest(self):
        return {
            "format": FORMAT,
            "version": VERSION,
            "id": self.id,
            "parent_id": self.parent_id,
            "source": self.source,
            "created": self.created,
            "score": self.score,
            "genome": self.genome_traits,
            "world_id": self.world_id,
            "world": world_summary(self.config),
            "trained_generations": (self.checkpoint or {}).get("next_generation"),
            "resumable": bool(self.checkpoint),
            "code": self.code,
            "learning": self.learning,
        }

    def _files(self):
        legacy = {"best_score": self.score, "genome": self.genome_traits}
        files = {
            "config.yaml": yaml.safe_dump({**self.config, "generated_run": legacy}, sort_keys=False, allow_unicode=True),
            "genome.json": json.dumps({"traits": self.genome_traits, "score": self.score}, indent=2),
            "memory.json": json.dumps({"hall_of_fame": self.memory}, indent=2),
            "tag_vectors.json": json.dumps(self.tag_vectors),
            "best_model.yaml": yaml.safe_dump(legacy, sort_keys=False),
            "README.txt": ("Adaptive Narrative engine model: a trained storyteller with its world, tuning, training "
                           "state, memory and tag vectors. Load it in the studio (Load a saved engine), the API "
                           "(POST /v1/engines/load) or Python (core_engine.model.EngineModel.load).\n"),
        }
        if self.checkpoint:
            files["training.json"] = json.dumps(self.checkpoint)
        return {name: text.encode("utf-8") for name, text in files.items()}

    def save(self, path):
        """Write the model file atomically; returns its path."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        files = self._files()
        manifest = {**self.manifest(), "files": {name: _sha256(data) for name, data in files.items()}}
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        os.close(handle)
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest, indent=2))
                for name, data in files.items():
                    archive.writestr(name, data)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return str(path)

    @staticmethod
    def is_model(path):
        """Whether ``path`` is an engine model file (a ZIP with a model manifest)."""
        try:
            with zipfile.ZipFile(path) as archive:
                return json.loads(archive.read("manifest.json")).get("format") == FORMAT
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            return False

    @classmethod
    def load(cls, path):
        """Read a model file, checking its format, version, checksums, genome and world."""
        from .builder import logic_errors

        try:
            with zipfile.ZipFile(path) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                if manifest.get("format") != FORMAT:
                    raise ValueError("Not an Adaptive Narrative engine model.")
                if int(manifest.get("version", 0)) > VERSION:
                    raise ValueError(f"Engine model version {manifest.get('version')} is newer than this engine "
                                     f"supports ({VERSION}).")
                files = {name: archive.read(name) for name in manifest.get("files", {})}
        except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not read the engine model: {exc}") from exc
        for name, digest in manifest["files"].items():
            if _sha256(files[name]) != digest:
                raise ValueError(f"The engine model is damaged: `{name}` does not match its checksum.")
        config_data = yaml.safe_load(io.BytesIO(files["config.yaml"]))
        genome = json.loads(files["genome.json"])
        missing = sorted(set(TRAIT_BOUNDS) - set(genome["traits"]) - set(Genome.added_field_defaults()))
        if missing:
            raise ValueError("The engine model's genome is missing: " + ", ".join(missing))
        errors = logic_errors((config_data or {}).get("world_definition"))
        if errors:
            raise ValueError("The engine model's world is invalid: " + "; ".join(errors[:5]))
        traits = {**Genome.added_field_defaults(), **genome["traits"]}
        return cls(
            config_data, traits, score=genome.get("score"),
            checkpoint=json.loads(files["training.json"]) if "training.json" in files else None,
            memory=json.loads(files["memory.json"]).get("hall_of_fame", []) if "memory.json" in files else [],
            tag_vectors=json.loads(files["tag_vectors.json"]) if "tag_vectors.json" in files else {},
            model_id=manifest.get("id"), parent_id=manifest.get("parent_id"), source=manifest.get("source", ""),
            created=manifest.get("created"), learning=manifest.get("learning"), code=manifest.get("code"),
        )

    def code_matches(self):
        """Whether this model was trained with the engine code running now."""
        return self.code == code_fingerprint()
