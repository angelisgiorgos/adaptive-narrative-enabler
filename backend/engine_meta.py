"""What the studio and the API say about a completed engine besides its model file: an optional
human-readable name, the evaluations made of it (the judge's engagement), and its result card.

The engine id (and its run id) stays the identifier everywhere: a name is only shown. Names and
evaluation summaries are kept with the saves (``ANE_SAVE_DIR/engine_meta.json``), so they work the
same with recording on or off, and survive a restart:

    {"format": 1, "engines": {engine_id: {"name": str, "named_at": iso,
                                          "evaluations": [{kind, at, stories, judged, engagement, ...}]}}}

An older or missing file is read as empty (format 0: a bare {engine_id: name} mapping is taken as
names); the file is rewritten in the current format on the next change. An engine without a name
gets a label made from what it is (a run, a continued or retrained run, a loaded engine), when it
was made, its generations and its training fitness.

The card (``card``) is read from the engine's model file (its manifest: creation time, world,
fitness, learning; its training state: the settings it ran with), so it is the same for a recorded
engine and one kept with the saves. Training fitness (the evolution's objective: the story score on
the run's benchmark) and judge engagement (an LLM judge's 1–10 rating of played stories) are kept
apart: they are different measures on different scales.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import threading
import zipfile
from functools import lru_cache
from pathlib import Path

LOG = logging.getLogger("ane.engine_meta")
FORMAT = 1
FILE_NAME = "engine_meta.json"
MAX_NAME = 80
MAX_EVALUATIONS = 10  # per engine, newest kept
_LOCK = threading.RLock()

# The training settings a card shows first (the others follow, alphabetically).
MAIN_SETTINGS = ("pop_size", "generations", "episodes_per_genome", "max_steps", "validation_episodes", "warm_start",
                 "crossover_rate", "mutation_rate", "elitism_rate", "player")


class EngineNameError(ValueError):
    """A name that cannot be kept (too long, control characters)."""


def meta_path() -> Path:
    return Path(os.environ.get("ANE_SAVE_DIR") or (Path(__file__).resolve().parent.parent / "saves")) / FILE_NAME


def migrate(data) -> dict:
    """Any stored form as the current one: {"format": 1, "engines": {...}}. Format 0 (a bare mapping
    of engine id to name, or nothing) is converted; a newer format is read for what it shares."""
    if not isinstance(data, dict):
        return {"format": FORMAT, "engines": {}}
    if "engines" not in data:  # format 0: {engine_id: name}
        engines = {str(key): {"name": value} for key, value in data.items() if isinstance(value, str) and value.strip()}
        return {"format": FORMAT, "engines": engines}
    engines = {}
    for engine_id, entry in (data.get("engines") or {}).items():
        if isinstance(entry, dict):
            engines[str(engine_id)] = {key: entry[key] for key in ("name", "named_at", "evaluations") if key in entry}
    return {"format": FORMAT, "engines": engines}


def _read() -> dict:
    try:
        return migrate(json.loads(meta_path().read_text(encoding="utf-8")))
    except FileNotFoundError:
        return migrate(None)
    except (OSError, ValueError) as exc:
        LOG.warning("The engine names could not be read (%s): engines are shown unnamed.", exc)
        return migrate(None)


def _write(data) -> None:
    path = meta_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def entry(engine_id) -> dict:
    with _LOCK:
        return dict(_read()["engines"].get(str(engine_id)) or {})


def names() -> dict:
    """{engine_id: name} of every named engine."""
    with _LOCK:
        return {key: value["name"] for key, value in _read()["engines"].items() if value.get("name")}


def clean_name(name) -> str | None:
    """A name as kept: spaces collapsed; empty is no name. Refused: over MAX_NAME characters,
    control characters."""
    text = " ".join(str(name or "").split())
    if not text:
        return None
    if len(text) > MAX_NAME:
        raise EngineNameError(f"A name has at most {MAX_NAME} characters ({len(text)} given).")
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise EngineNameError("A name cannot contain control characters.")
    return text


def set_name(engine_id, name) -> str | None:
    """Name an engine (an empty name removes it). The id is unchanged. Returns the name kept."""
    text = clean_name(name)
    with _LOCK:
        data = _read()
        current = data["engines"].setdefault(str(engine_id), {})
        if text:
            current.update(name=text, named_at=_now())
        else:
            current.pop("name", None)
            current.pop("named_at", None)
        if not current:
            data["engines"].pop(str(engine_id), None)
        _write(data)
    return text


def record_evaluation(engine_id, kind, result) -> None:
    """Keep the summary of an evaluation of an engine (``kind``: judge (the judge benchmark) or
    calibration (stories judged to fit the fitness)): when, how many stories, the mean judged
    engagement (1–10) and, for the benchmark, the mean per criterion and the stories' mean training
    fitness for comparison. A failure to keep it is logged, never raised (the evaluation stands)."""
    if not engine_id or not isinstance(result, dict):
        return
    summary = {"kind": kind, "at": _now(), "stories": result.get("stories", result.get("n")),
               "judged": result.get("judged", result.get("n")), "engagement": result.get("engagement")}
    if result.get("criteria"):
        summary["criteria"] = result["criteria"]
    fitness = [row.get("fitness") for row in result.get("rows") or [] if isinstance(row.get("fitness"), (int, float))]
    if fitness:
        summary["story_fitness"] = round(sum(fitness) / len(fitness), 2)
    if kind == "calibration":
        summary["cv_spearman"] = result.get("cv_spearman")
    try:
        with _LOCK:
            data = _read()
            current = data["engines"].setdefault(str(engine_id), {})
            current["evaluations"] = ([summary] + list(current.get("evaluations") or []))[:MAX_EVALUATIONS]
            _write(data)
    except OSError as exc:
        LOG.warning("The evaluation of engine %s could not be kept (%s).", engine_id, exc)


# ---- labels ---------------------------------------------------------------------------------------

def kind_of(source, loaded=False) -> str:
    source = str(source or "")
    if source.startswith("continued"):
        return "Continued run"
    if source.startswith("retrained"):
        return "Retrained run"
    if source == "generation_champion":
        return "Generation champion"
    if loaded or source.startswith("loaded") or source == "import":
        return "Loaded engine"
    return "Run"


def _when(created) -> str:
    return str(created or "")[:16].replace("T", " ")


def default_label(info) -> str:
    """The label of an unnamed engine: what it is, when it was made, its generations and its
    training fitness (e.g. *Run of 2026-11-18 14:02 · 25 generations · fitness 41.2*)."""
    parts = [f"{kind_of(info.get('source'), info.get('loaded'))} of {_when(info.get('created_at')) or 'unknown date'}"]
    generations = info.get("generations") or info.get("trained_generations")  # a recorded engine: trained_
    if info.get("generation") is not None:
        parts.append(f"generation {info['generation']}")
    elif generations:
        parts.append(f"{generations} generation{'s' if generations != 1 else ''}")
    if isinstance(info.get("score"), (int, float)):
        parts.append(f"fitness {info['score']:.1f}")
    return " · ".join(parts)


def label(info, known_names=None) -> str:
    """The one label an engine has in Train, Play, Evaluate and Export: its name (with when it was
    made), or the default label; always ending with its short id, which identifies it."""
    engine_id = str(info.get("engine_id") or info.get("id") or "")
    name = (known_names if known_names is not None else names()).get(engine_id)
    text = f"{name} · {_when(info.get('created_at'))}" if name else default_label(info)
    return f"{text} · {engine_id[:8]}"


def annotate(info, known_names=None) -> dict:
    """``info`` with its ``name`` (None when unnamed) and ``label``."""
    known = known_names if known_names is not None else names()
    engine_id = str(info.get("engine_id") or info.get("id") or "")
    return {**info, "name": known.get(engine_id), "label": label(info, known)}


# ---- the result card ---------------------------------------------------------------------------------

@lru_cache(maxsize=64)
def _model_facts(path, modified, size):
    """What a model file says about itself (cached per file version): its manifest, the settings its
    training ran with and the digest of its world (world_definition only: the tuning is not the world)."""
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        settings = {}
        if "training.json" in archive.namelist():
            settings = dict((json.loads(archive.read("training.json")) or {}).get("settings") or {})
        world = None
        if "config.yaml" in archive.namelist():
            import yaml

            cfg = yaml.safe_load(archive.read("config.yaml")) or {}
            world = world_digest(cfg)
    return manifest, settings, world


@lru_cache(maxsize=16)
def _model_config(path, modified, size):
    import yaml

    with zipfile.ZipFile(path) as archive:
        data = yaml.safe_load(archive.read("config.yaml")) or {}
    return {key: value for key, value in data.items() if key != "generated_run"}


def model_config(path) -> dict:
    """The world and tuning saved in an engine's model file (its config.yaml), read only: a copy, so
    the caller cannot change the cached one, and nothing is made active."""
    import copy

    stat = Path(path).stat()
    return copy.deepcopy(_model_config(str(path), stat.st_mtime_ns, stat.st_size))


def world_digest(cfg) -> str:
    """A digest of a configuration's world (places, people, items, events, missions, ...), without
    the tuning: two configurations with this digest have the same world."""
    payload = json.dumps((cfg or {}).get("world_definition") or {}, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def scaling_traits(genome) -> dict:
    """{trait: value} of a storyteller's traits that scale the Weights tab's Basic controls (how strong
    pacing, repetition and event frequency are with this storyteller)."""
    from . import tuning

    wanted = []
    for area in tuning.basic_areas():
        for control in area["controls"]:
            scaled = control.get("scaled_by") or {}
            wanted += [scaled["trait"], *(scaled.get("with") or {})] if scaled else []
    genome = genome if isinstance(genome, dict) else {}
    return {trait: round(float(genome[trait]), 3) for trait in dict.fromkeys(wanted)
            if isinstance(genome.get(trait), (int, float))}


def card(engine_id, model_path, info=None, current_world=None) -> dict:
    """The result card of a completed engine: {engine_id, run_id, name, label, created_at, kind,
    source, parent_id, world: {id, digest, summary, current}, generations, settings: {main, other},
    fitness: {training, improvement, early_stopped, history: [best per generation], version}, evaluations,
    resumable}. ``info``: what the listing knows (run id, ...); ``current_world``: the digest of the
    active world (whether this engine was trained on it)."""
    info = dict(info or {})
    stat = Path(model_path).stat()
    manifest, settings, digest = _model_facts(str(model_path), stat.st_mtime_ns, stat.st_size)
    learning = manifest.get("learning") or {}
    rows = [item for item in learning.get("history") or [] if isinstance(item, dict)]
    # Per generation: the champion's score on the fixed benchmark (what the run's fitness is), else its best.
    history = [round(float(item.get("validation") or item.get("best") or 0.0), 2) for item in rows]
    in_loop = [item["judge_engagement"] for item in rows if isinstance(item.get("judge_engagement"), (int, float))]
    known = entry(engine_id)
    listing = {"engine_id": engine_id, "created_at": manifest.get("created") or info.get("created_at"),
               "source": manifest.get("source") or info.get("source"), "score": manifest.get("score"),
               "generations": learning.get("generations_run") or info.get("generations") or len(history) or None,
               "loaded": info.get("loaded", False)}
    name = known.get("name")
    main = {key: settings[key] for key in MAIN_SETTINGS if key in settings}
    other = {key: settings[key] for key in sorted(settings) if key not in main
             and isinstance(settings[key], (int, float, str, bool)) and not isinstance(settings[key], dict)}
    return {
        "engine_id": engine_id, "run_id": info.get("run_id"), "name": name,
        "label": label(listing, {engine_id: name} if name else {}),
        "default_label": default_label(listing),
        "created_at": listing["created_at"], "kind": kind_of(listing["source"], listing["loaded"]),
        "source": listing["source"], "parent_id": manifest.get("parent_id"),
        "world": {"id": manifest.get("world_id"), "digest": digest, "summary": manifest.get("world") or {},
                  "current": (digest == current_world) if digest and current_world else None},
        "generations": listing["generations"], "trained_generations": manifest.get("trained_generations"),
        "settings": {"main": main, "other": other},
        "fitness": {"training": manifest.get("score"), "improvement": learning.get("improvement"),
                    "early_stopped": learning.get("early_stopped"), "history": history,
                    # The scoring it was trained with (fitness.version; engines from before versions: 1).
                    "version": int(learning.get("fitness_version") or 1)},
        # The judge in the loop of evolution (evolution.judge_weight > 0): engagement while training.
        "judge_in_training": round(float(in_loop[-1]), 2) if in_loop else None,
        "evaluations": list(known.get("evaluations") or []),
        "resumable": bool(manifest.get("resumable")),
        "scaling_traits": scaling_traits(manifest.get("genome")),
    }
