"""REST API for Adaptive Narrative Enabler.

Swagger is available at ``/docs``. Keep a single Uvicorn worker because runtime
configuration overrides and play sessions are intentionally held in process.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

import yaml

from backend import content, export, sessions, store
from utils.config_loader import config


class Entity(BaseModel):
    name: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    descriptions: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)
    unique: bool | None = Field(
        default=None,
        description="NPCs only. true: the NPC lives in one location for the whole game; "
        "false: a generic NPC (e.g. a guard) that may appear wherever it fits. "
        "Omit on PUT to keep the stored value; always null for other kinds.",
    )
    encounter_event: str | None = Field(
        default=None,
        description="NPCs only. Event that starts by itself when the NPC is in the scene (hostile NPCs). "
        "Omit on PUT to keep the stored value, send \"\" to remove it; always null for other kinds.",
    )
    encounter_repeat: bool | None = Field(
        default=None,
        description="NPCs only. true: the encounter happens on every visit; false: once per game.",
    )
    enabled_if: dict[str, Any] | None = Field(
        default=None,
        description="Locations, NPCs and items: persistent-variable conditions, e.g. "
        "{\"thief_caught\": false, \"reputation\": {\"gte\": 1}}. Omit on PUT to keep, send {} to remove.",
    )
    title: str | None = Field(default=None, description="Events only: the title shown while the event is active.")
    stat_bonus: dict[str, int] | None = Field(
        default=None, description="Items only: stat -> bonus while held. Omit on PUT to keep, send {} to remove.",
    )


class StatAllocation(BaseModel):
    mode: Literal["defined", "random", "balanced"] | None = Field(
        default=None, description="How to allocate when no points are given (default: character_stats.mode).",
    )
    points: dict[str, int] | None = Field(default=None, description="Stat -> points to check.")


class CharacterStatsSettings(BaseModel):
    character_stats: dict[str, Any] = Field(
        description="{stats, allocation: {total_points, min_points, max_points, value_per_point}, mode, "
        "defined_points, tag_checks, damage_reduction_stat}."
    )


class EntityActions(BaseModel):
    actions: list[dict[str, Any]] = Field(
        description="The entry's actions as stored in YAML (name, outcomes, repeat, exclusive_group, "
        "triggers_event, send_to, set_variables, change_variables, ...)."
    )


class Missions(BaseModel):
    missions: dict[str, dict[str, Any]] = Field(
        description="Mission id -> {title, description, goal, weight?, enabled_if?}."
    )


class StoryRules(BaseModel):
    missions_enabled: bool | None = None
    end_on_goal: bool | None = None
    pursuit_start_step: int | None = Field(default=None, ge=0)
    pursuit_full_step: int | None = Field(default=None, ge=1)
    route_weight: float | None = None
    repeat_location: Literal["once", "per_visit", "always"] | None = None
    repeat_npc: Literal["once", "per_visit", "always"] | None = None
    repeat_item: Literal["once", "per_visit", "always"] | None = None
    repeat_event: Literal["once", "per_visit", "always"] | None = None
    arrest_location: str | None = None
    arrest_lock_when: Literal["sent", "always", "never"] | None = None
    auto_travel_on_spawn: bool | None = None
    auto_travel_on_spawn_threshold: float | None = None
    profile: str | None = None
    save_on: Literal["change", "end"] | None = None
    simulate_with: Literal["defaults", "stored"] | None = None
    fallback_action: str | None = None
    fallback_description: str | None = None


class StoredValues(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict, description="Variable -> value to store.")
    playthroughs: int | None = Field(default=None, ge=0, description="Set the finished-playthrough count.")


class Settings(BaseModel):
    pop_size: int = Field(ge=1)
    generations: int = Field(ge=1)
    episodes_per_genome: int = Field(ge=1)
    max_steps: int = Field(ge=1)
    initial_health: int = Field(ge=1)
    max_health: int | None = Field(
        default=None, ge=1,
        description="Healing never raises health above this. Omit on PUT to keep the stored value.",
    )
    initial_coins: int = Field(ge=0)
    max_actions: int = Field(ge=100)
    deterministic: bool = True
    seed: int | None = 42


class SimulationRequest(BaseModel):
    candidate_count: int = Field(default=8, ge=1, le=50)
    generations: int = Field(default=5, ge=1, le=25)
    simulation_steps: int = Field(default=16, ge=1, le=60)
    deterministic: bool = True
    profile: str | None = Field(default=None, description="Player profile for persistent variables.")
    stats_mode: Literal["defined", "random", "balanced"] | None = Field(
        default=None, description="How the player's stats are allocated (default: character_stats.mode).",
    )
    stats: dict[str, int] | None = Field(default=None, description="The player's own stat points (overrides stats_mode).")
    episodes: int | None = Field(default=None, ge=1, le=10, description="Stories each storyteller plays per generation.")
    warm_start: bool | None = Field(default=None, description="Continue learning from the storytellers remembered from earlier runs.")


class ActionRequest(BaseModel):
    action: str = Field(min_length=1)


class WeightUpdate(BaseModel):
    values: dict[str, float]


class YAMLDocument(BaseModel):
    yaml: str


class VariableDefinitions(BaseModel):
    variables: dict[str, dict[str, Any]] = Field(
        description="Name -> {type: bool|int|float|str, default, min?, max?, description?}."
    )


class Messages(BaseModel):
    messages: dict[str, str | list[str]] = Field(
        description="Message key -> text (or list of texts used in turn). Keys left out use the default."
    )


class PlayState(BaseModel):
    session_id: str
    scene: str
    actions: list[str]
    last_turn: str = ""
    ended: bool
    event: str | None = Field(default=None, description="Title of the active event, if any.")
    mission: str | None = Field(default=None, description="Title of the story's mission, if any.")
    won: bool = Field(default=False, description="True once the mission goal has been achieved.")
    location: str | None = None
    health: int | None = None
    max_health: int | None = None
    coins: int | None = None
    power: float | None = None
    inventory: list[str] = Field(default_factory=list)
    mission_status: str | None = None
    end_reason: str | None = None
    profile: str | None = Field(default=None, description="Profile the game saves persistent variables to.")
    variables: dict[str, Any] = Field(default_factory=dict, description="Current persistent variables.")
    stats: dict[str, int] = Field(default_factory=dict, description="Skill values (1-100) including item bonuses.")
    stat_points: dict[str, int] = Field(default_factory=dict, description="Allocated points per stat.")
    last_check: dict[str, Any] | None = Field(default=None, description="{stat, roll, target, passed, step} of the last check.")


class SimulationResponse(PlayState):
    report: str
    run_id: str | None = Field(default=None, description="Use with POST /v1/exports to export this run or one of its generations.")
    learning: dict[str, Any] | None = Field(
        default=None,
        description="How the evolutionary AI learned: best_score, improvement, generations_run, early_stopped, "
        "warm_started_from_memory, history (per generation: best, mean, worst, spread, diversity, "
        "mutation_step, validation), best_genome and learned_traits.",
    )
    config_download_url: str
    engine_download_url: str


ARTIFACTS: dict[str, Path] = {}
ARTIFACT_DIR = Path(os.getenv("ANE_ARTIFACT_DIR", str(store.PROJECT_ROOT / "artifacts")))

api = FastAPI(
    title="Adaptive Narrative Enabler API",
    summary="World authoring, configuration, evolution, and interactive play",
    version="1.0.0",
    description=(
        "Every operation exposed by Adaptive Narrative Studio is available here. "
        "This service is stateful and must run with one worker."
    ),
)

origins = [item.strip() for item in os.getenv("ANE_CORS_ORIGINS", "").split(",") if item.strip()]
if origins:
    api.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "PUT", "POST", "DELETE"],
        allow_headers=["Content-Type", "Authorization"],
    )


def _fail(exc: Exception):
    message = getattr(exc, "message", None) or str(exc)
    raise HTTPException(status_code=400, detail=message) from exc


def _settings() -> Settings:
    values = store.load_settings()
    return Settings(
        pop_size=values[0], generations=values[1], episodes_per_genome=values[2],
        max_steps=values[3], initial_health=values[4], initial_coins=values[5],
        max_actions=values[6], deterministic=values[7], seed=values[8], max_health=values[9],
    )


def _play_state(token: str, last_turn: str = "") -> PlayState:
    with sessions.SESSION_LOCK:
        state = sessions.SESSIONS.get(token)
        if state is None:
            raise HTTPException(status_code=404, detail="Play session not found.")
        scene, actions = sessions._scene(state)
        return PlayState(
            session_id=token, scene=scene, actions=actions,
            last_turn=last_turn, ended=bool(state.ended), **sessions.play_state(state),
        )


def _register_artifact(path: str) -> str:
    artifact_id = uuid.uuid4().hex
    source = Path(path)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    destination = ARTIFACT_DIR / f"{artifact_id}{source.suffix}"
    destination.write_bytes(source.read_bytes())
    source.unlink(missing_ok=True)
    ARTIFACTS[artifact_id] = destination
    return f"/v1/artifacts/{artifact_id}"


def _save_upload(upload: UploadFile, suffixes: set[str], max_bytes: int) -> Path:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in suffixes:
        raise HTTPException(status_code=400, detail=f"Allowed file types: {', '.join(sorted(suffixes))}.")
    target = Path(tempfile.mkstemp(prefix="ane-upload-", suffix=suffix)[1])
    size = 0
    try:
        with target.open("wb") as stream:
            while chunk := upload.file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(status_code=413, detail="Uploaded file is too large.")
                stream.write(chunk)
        return target
    except Exception:
        target.unlink(missing_ok=True)
        raise


@api.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@api.get("/healthz", tags=["Operations"])
def health():
    return {"status": "ok", "sessions": len(sessions.SESSIONS)}


@api.get("/v1/config", tags=["Configuration"])
def get_config():
    return {"using_override": config.using_override, "config": config.as_dict()}


@api.post("/v1/config/upload", tags=["Configuration"])
def upload_config(file: UploadFile = File(...)):
    path = _save_upload(file, {".yaml", ".yml"}, store.MAX_CONFIG_UPLOAD_BYTES)
    try:
        store.upload_config(str(path))
        return {"message": "Configuration validated and activated.", "settings": _settings()}
    except Exception as exc:
        _fail(exc)
    finally:
        path.unlink(missing_ok=True)


@api.post("/v1/config/reset", tags=["Configuration"])
def reset_config():
    store.use_builtin_config()
    return {"message": "Built-in configuration restored.", "settings": _settings()}


@api.get("/v1/settings", response_model=Settings, tags=["Configuration"])
def get_settings():
    return _settings()


@api.put("/v1/settings", tags=["Configuration"])
def put_settings(body: Settings):
    try:
        message = store.save_settings(
            body.pop_size, body.generations, body.episodes_per_genome, body.max_steps,
            body.initial_health, body.initial_coins, body.max_actions,
            body.deterministic, body.seed, body.max_health,
        )
        return {"message": message, "settings": _settings()}
    except Exception as exc:
        _fail(exc)


@api.get("/v1/variables", tags=["Persistent variables"])
def get_variables():
    """Variable definitions and the stored values of the configured player profile."""
    return {"definitions": config.get("world_definition.variables") or {}, "stored": content.stored_variables()}


@api.put("/v1/variables", tags=["Persistent variables"])
def put_variables(body: VariableDefinitions):
    try:
        return {"message": content.save_variable_definitions(body.variables), "stored": content.stored_variables()}
    except Exception as exc:
        _fail(exc)


@api.get("/v1/variables/profiles", tags=["Persistent variables"])
def get_profiles():
    return {"profiles": content.player_profiles(), "default": config.get("persistence.profile", "default")}


@api.get("/v1/variables/values", tags=["Persistent variables"])
def get_variable_values(profile: str | None = None):
    try:
        return content.stored_variables(profile)
    except Exception as exc:
        _fail(exc)


@api.put("/v1/variables/values", tags=["Persistent variables"])
def put_variable_values(body: StoredValues, profile: str | None = None):
    """Overwrite stored values of a profile, e.g. to test a later playthrough."""
    try:
        return content.save_stored_values(profile, body.values, body.playthroughs)
    except Exception as exc:
        _fail(exc)


@api.post("/v1/variables/reset", tags=["Persistent variables"])
def reset_variable_values(profile: str | None = None):
    """Forget a profile's stored values and playthroughs (a new player)."""
    try:
        content.reset_stored_variables(profile)
        return content.stored_variables(profile)
    except Exception as exc:
        _fail(exc)


@api.get("/v1/messages", tags=["World authoring"])
def get_messages():
    """Every story message, its default, and the placeholders it may use."""
    from core_engine.narration import DEFAULT_MESSAGES, allowed_fields
    return {
        "messages": content.effective_messages(),
        "defaults": DEFAULT_MESSAGES,
        "placeholders": {key: sorted(allowed_fields(key)) for key in DEFAULT_MESSAGES},
    }


@api.put("/v1/messages", tags=["World authoring"])
def put_messages(body: Messages):
    try:
        message = content.save_messages(body.messages)
        return {"message": message, "messages": content.effective_messages()}
    except Exception as exc:
        _fail(exc)


@api.get("/v1/hyperparameters", tags=["Configuration"])
def get_hyperparameters():
    return {"yaml": store.load_hyperparameters_yaml()}


@api.put("/v1/hyperparameters", tags=["Configuration"])
def put_hyperparameters(body: YAMLDocument):
    try:
        result = store.save_all_hyperparameters(body.yaml)
        return {"message": result[-1], "yaml": result[-2], "settings": _settings()}
    except Exception as exc:
        _fail(exc)


@api.get("/v1/weights", tags=["Configuration"])
def get_weights():
    return {"values": dict(store._weight_entries())}


@api.put("/v1/weights", tags=["Configuration"])
def put_weights(body: WeightUpdate):
    known = dict(store._weight_entries())
    unknown = sorted(set(body.values) - set(known))
    if unknown:
        raise HTTPException(status_code=400, detail="Unknown weight paths: " + ", ".join(unknown))
    merged = {**known, **body.values}
    paths = list(known)
    try:
        message = store.save_weights(paths, *(merged[path] for path in paths))
        return {"message": message, "values": dict(store._weight_entries())}
    except Exception as exc:
        _fail(exc)


@api.get("/v1/entities/{kind}", tags=["World authoring"])
def list_entities(kind: Literal["Locations", "NPCs", "Items", "Events"]):
    names, _ = content._entity_name_values(kind)
    return {"kind": kind, "entities": [get_entity(kind, name) for name in names]}


@api.get("/v1/entities/{kind}/{name}", response_model=Entity, tags=["World authoring"])
def get_entity(kind: Literal["Locations", "NPCs", "Items", "Events"], name: str):
    names, _ = content._entity_name_values(kind)
    if name not in names:
        raise HTTPException(status_code=404, detail="Entity not found.")
    entity_name, tags, descriptions, goals, unique, encounter_event, encounter_repeat, enabled_if, title, stat_bonus = (
        content.load_entity(kind, name)
    )
    return Entity(
        name=entity_name, tags=list(tags), descriptions=store._lines(descriptions),
        goals=store._lines(goals), unique=unique,
        encounter_event=encounter_event, encounter_repeat=encounter_repeat, enabled_if=enabled_if, title=title,
        stat_bonus=stat_bonus,
    )


@api.put("/v1/entities/{kind}/{name}", response_model=Entity, tags=["World authoring"])
def put_entity(kind: Literal["Locations", "NPCs", "Items", "Events"], name: str, body: Entity):
    try:
        content.save_entity(
            kind, name, body.name, body.tags,
            "\n".join(body.descriptions), "\n".join(body.goals), body.unique,
            body.encounter_event, body.encounter_repeat, body.enabled_if, body.title, body.stat_bonus,
        )
        return get_entity(kind, body.name)
    except Exception as exc:
        _fail(exc)


@api.delete("/v1/entities/{kind}/{name}", tags=["World authoring"])
def delete_entity(kind: Literal["Locations", "NPCs", "Items", "Events"], name: str):
    names, _ = content._entity_name_values(kind)
    if name not in names:
        raise HTTPException(status_code=404, detail="Entity not found.")
    try:
        content.delete_entity(kind, name)
        return {"message": f"Deleted {name}.", "entities": content._entity_name_values(kind)[0]}
    except Exception as exc:
        _fail(exc)


@api.get("/v1/entities/{kind}/{name}/actions", response_model=EntityActions, tags=["World authoring"])
def get_entity_actions(kind: Literal["Locations", "NPCs", "Items", "Events"], name: str):
    """The entry's actions and outcomes, exactly as stored."""
    names, _ = content._entity_name_values(kind)
    if name not in names:
        raise HTTPException(status_code=404, detail="Entity not found.")
    return EntityActions(actions=content.load_entity_actions(kind, name))


@api.put("/v1/entities/{kind}/{name}/actions", response_model=EntityActions, tags=["World authoring"])
def put_entity_actions(kind: Literal["Locations", "NPCs", "Items", "Events"], name: str, body: EntityActions):
    """Replace the entry's actions after checking fields, references and variables."""
    names, _ = content._entity_name_values(kind)
    if name not in names:
        raise HTTPException(status_code=404, detail="Entity not found.")
    try:
        content.save_entity_actions(kind, name, body.actions)
        return get_entity_actions(kind, name)
    except Exception as exc:
        _fail(exc)


class ExportRequest(BaseModel):
    source: Literal["run", "memory", "game"] = Field(
        default="run",
        description="run: an evolution run (run_id; latest if omitted); memory: the best storyteller the AI "
        "remembers; game: the storyteller of a play session (session_id).",
    )
    run_id: str | None = None
    generation: int | None = Field(default=None, ge=0, description="Export this generation's champion instead of the run's best.")
    session_id: str | None = None
    include_memory: bool = Field(default=True, description="Ship the learning memory so the engine can keep training offline.")
    name: str | None = Field(default=None, description="Folder name of the export.")


class LogicSection(BaseModel):
    parameters: str = Field(description="The section as YAML text, starting with `<section>:` (comments are kept).")


@api.get("/v1/logic", tags=["Engine logic"])
def get_logic_sections():
    """The parameter sections that control the engine's logic, with what each controls."""
    return {"sections": store.logic_sections()}


@api.get("/v1/logic/{section}", response_model=LogicSection, tags=["Engine logic"])
def get_logic_section(section: str):
    try:
        return LogicSection(parameters=store.load_logic_section(section))
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@api.put("/v1/logic/{section}", tags=["Engine logic"])
def put_logic_section(section: str, body: LogicSection):
    """Change the engine's logic through its parameters (same keys and value types required)."""
    try:
        return {"message": store.save_logic_section(section, body.parameters)}
    except Exception as exc:
        _fail(exc)


@api.post("/v1/exports", tags=["Engine export"])
def export_offline_engine(body: ExportRequest):
    """Export a trained engine (code, world, storyteller, tag vectors, learning memory) as a ZIP
    that runs offline and integrates in a game: Python embedding, HTTP/JSON server, command line."""
    try:
        result = export.export_engine(
            source=body.source, run_id=body.run_id, generation=body.generation, token=body.session_id,
            include_memory=body.include_memory, name=body.name,
        )
    except Exception as exc:
        _fail(exc)
    manifest = result["manifest"]
    return {
        "download_url": _register_artifact(result["path"]),
        "name": manifest["name"],
        "source": manifest["source"],
        "score": manifest["score"],
        "genome": manifest["genome"],
        "world": manifest["world"],
    }


@api.get("/v1/runs", tags=["Engine export"])
def list_runs():
    """Recent evolution runs that can be exported (best or any generation)."""
    return {"runs": [
        {"run_id": run_id, "best_score": run["learning"]["best_score"],
         "generations": len(run["learning"]["history"])}
        for run_id, run in reversed(sessions.RUNS.items())
    ]}


@api.get("/v1/learning", tags=["Evolution and play"])
def get_learning():
    """The storytellers the evolutionary AI remembers from earlier runs."""
    return sessions.learning_memory()


@api.delete("/v1/learning", tags=["Evolution and play"])
def forget_learning():
    """Forget what the AI learned; the next run starts from random storytellers."""
    return sessions.forget_learning()


@api.get("/v1/stats", tags=["Character stats"])
def get_stats():
    """The story's character stat definitions, budget, mode, tag checks and damage-reduction stat."""
    return {"character_stats": content.stats_config()}


@api.put("/v1/stats", tags=["Character stats"])
def put_stats(body: CharacterStatsSettings):
    try:
        return {"message": content.save_stats_settings(body.character_stats), "character_stats": content.stats_config()}
    except Exception as exc:
        _fail(exc)


@api.post("/v1/stats/allocate", tags=["Character stats"])
def allocate_stats(body: StatAllocation):
    """Check the player's own points, or preview a random/balanced split, with the 1-100 values."""
    return content.allocate_stats(mode=body.mode, points=body.points)


@api.get("/v1/missions", response_model=Missions, tags=["World authoring"])
def get_missions():
    return Missions(missions=content.load_missions())


@api.put("/v1/missions", response_model=Missions, tags=["World authoring"])
def put_missions(body: Missions):
    try:
        content.save_missions(body.missions)
        return get_missions()
    except Exception as exc:
        _fail(exc)


@api.get("/v1/rules", response_model=StoryRules, tags=["Configuration"])
def get_rules():
    """Story rules: mission pacing, repeat defaults, arrests, discovery, persistence, events."""
    return StoryRules(**store.load_rules())


@api.put("/v1/rules", response_model=StoryRules, tags=["Configuration"])
def put_rules(body: StoryRules):
    """Change any story rules; fields left out keep their current value."""
    try:
        store.save_rules(body.model_dump(exclude_none=True))
        return get_rules()
    except Exception as exc:
        _fail(exc)


@api.post("/v1/simulations", response_model=SimulationResponse, tags=["Evolution and play"])
def simulate(body: SimulationRequest):
    try:
        game = sessions.run_simulation(
            body.candidate_count, body.generations, body.simulation_steps, body.deterministic,
            profile=body.profile, episodes=body.episodes, warm_start=body.warm_start,
            stats_choice="defined" if body.stats is not None else body.stats_mode,
            stats_points=yaml.safe_dump(body.stats) if body.stats is not None else None,
            progress=None,
        )
        state = _play_state(game["token"], game["last_turn"])
        return SimulationResponse(
            **state.model_dump(), report=game["report"], learning=game["learning"], run_id=game.get("run_id"),
            config_download_url=_register_artifact(game["config_path"]),
            engine_download_url=_register_artifact(game["engine_path"]),
        )
    except HTTPException:
        raise
    except Exception as exc:
        _fail(exc)


@api.post("/v1/engines/load", response_model=SimulationResponse, tags=["Evolution and play"])
def load_engine(file: UploadFile = File(...), profile: str | None = None,
                stats_mode: Literal["defined", "random", "balanced"] | None = None):
    path = _save_upload(file, {".yaml", ".yml", ".zip"}, store.MAX_ENGINE_UPLOAD_BYTES)
    try:
        game = sessions.load_best_engine(
            str(path), profile=profile, stats_choice=stats_mode,
        )
        state = _play_state(game["token"], game["last_turn"])
        return SimulationResponse(
            **state.model_dump(), report=game["report"], learning=game["learning"], run_id=game.get("run_id"),
            config_download_url=_register_artifact(game["config_path"]),
            engine_download_url=_register_artifact(game["engine_path"]),
        )
    except Exception as exc:
        _fail(exc)
    finally:
        path.unlink(missing_ok=True)


@api.get("/v1/sessions/{session_id}", response_model=PlayState, tags=["Evolution and play"])
def get_session(session_id: str):
    return _play_state(session_id)


@api.post("/v1/sessions/{session_id}/actions", response_model=PlayState, tags=["Evolution and play"])
def take_action(session_id: str, body: ActionRequest):
    try:
        _, _, turn = sessions.play_action(session_id, body.action)
        return _play_state(session_id, turn)
    except Exception as exc:
        _fail(exc)


@api.get("/v1/artifacts/{artifact_id}", tags=["Artifacts"])
def download_artifact(artifact_id: str):
    path = ARTIFACTS.get(artifact_id)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found.")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


app = api
