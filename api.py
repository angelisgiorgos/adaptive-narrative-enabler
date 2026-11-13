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

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

import yaml

from backend import content, evaluation, export, records, sessions, store
from core_engine import Mission
from core_engine.actions import REPEAT_RULES
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
    enabled_if: dict[str, Any] | list[Any] | None = Field(
        default=None,
        description="Locations, NPCs, items and events: a condition (shorthand "
        "{\"thief_caught\": false, \"reputation\": {\"gte\": 1}} or a tree such as "
        "{\"any\": [{\"has_item\": \"Royal Pass\"}, {\"stat\": \"persuasion\", \"op\": \"gte\", \"value\": 60}]}). "
        "Omit on PUT to keep, send {} to remove.",
    )
    title: str | None = Field(default=None, description="Events only: the title shown while the event is active.")
    stat_bonus: dict[str, int] | None = Field(
        default=None, description="Items only: stat -> bonus while held. Omit on PUT to keep, send {} to remove.",
    )
    role: str | None = Field(
        default=None, description="NPCs only: the NPC's own role in every story (a key of npc_roles.roles, e.g. "
        "nemesis, lieutenant, informant); null: inferred from its tags. Omit on PUT to keep, send \"\" to remove.",
    )
    secret: dict[str, Any] | None = Field(
        default=None, description="NPCs only: a secret that comes out once spoken with `talks` times and `clues` "
        "things learned: {text ({npc} names them), talks, clues, effects}; actions it unlocks use "
        "enabled_if: {secret_known: <NPC>}. Omit on PUT to keep, send {} to remove.",
    )
    start_eligible: bool | None = Field(
        default=None, description="Locations only: a story may start here. Omit on PUT to keep the stored value.",
    )
    start_weight: float | None = Field(
        default=None, gt=0,
        description="Locations only: relative chance of starting here among the starting locations (default 1). "
        "Omit on PUT to keep the stored value.",
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
        "triggers_event, enabled_if, send_to, set_variables, change_variables, effects, ...)."
    )


class Missions(BaseModel):
    missions: dict[str, dict[str, Any]] = Field(
        description="Mission id -> {title, description, goal, weight?, enabled_if?, rewards?}. Goal keys: action, "
        "location, item, event, npc_removed, outcome_tag, coins, variables, condition (any condition tree); "
        "rewards: an effect list applied when the mission is completed."
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
    candidate_count: int = Field(default=8, ge=1, le=store.RUN_LIMITS["candidate_limit"])
    generations: int = Field(default=5, ge=1, le=store.RUN_LIMITS["generation_limit"])
    simulation_steps: int = Field(default=16, ge=1, le=store.RUN_LIMITS["simulation_step_limit"],
                                  description="Steps per evaluated story; with "
                                  "evolution.full_length_stories every story is played to its end.")
    deterministic: bool = True
    profile: str | None = Field(default=None, description="Player profile for persistent variables.")
    stats_mode: Literal["defined", "random", "balanced"] | None = Field(
        default=None, description="How the player's stats are allocated (default: character_stats.mode).",
    )
    stats: dict[str, int] | None = Field(default=None, description="The player's own stat points (overrides stats_mode).")
    episodes: int | None = Field(default=None, ge=1, le=10, description="Stories each storyteller plays per generation.")
    warm_start: bool | None = Field(default=None, description="Continue learning from the storytellers remembered from earlier runs.")
    mission: str | None = Field(default=None, description="The mission to play (its key or title, see GET "
                                "/v1/missions/playable); left out, the story draws one.")


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
    start_location: str | None = Field(default=None, description="Where this story started.")
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
    quest: dict[str, Any] | None = Field(
        default=None, description="The mission as the player follows it: {key, title, description, status, stages: "
        "[{title, description, state: done|current|todo}], todo: what the current step still needs, in words}.")
    people: list[dict[str, Any]] = Field(
        default_factory=list, description="The people here: [{name, called, description, wants, hostile, talks, "
        "quest, helped, ally, role, importance}] (talks: times spoken with; quest: they can help with the mission now; "
        "helped: times helped with their goal; ally: their goal is fulfilled; role and importance 0-3: how much they "
        "matter in this story, npc_roles).")
    map: dict[str, Any] | None = Field(
        default=None, description="The map as the player knows it: {current, previous, places: [{name, visited, locked}], "
        "paths: [[a, b]] (both ways), one_way: [[from, to]]}.")
    choices: list[dict[str, Any]] = Field(
        default_factory=list, description="The offered actions with the check each involves: [{name, check: {stat, "
        "chance (0-100), bonus, risk: {health, coins, sent_to} (what failing costs) or null} or null}].")
    people_met: list[dict[str, Any]] = Field(
        default_factory=list, description="Everyone met in this story: [{name, called, where, talks, hostile, gone, ally, "
        "role, importance}].")
    ways_on: list[dict[str, Any]] = Field(
        default_factory=list, description="The places one can travel to from here now: [{name, visited, toward_quest}].")
    cast: dict[str, Any] | None = Field(
        default=None, description="The session's cast (session_scope): {left_out: {locations, npcs, items, events}, "
        "kept: {kind: count}}: what the session's goal does not need may stay out.")
    unique_actions: list[dict[str, Any]] = Field(
        default_factory=list, description="Every attempt at a one-time action: [{turn, action, owner, where, succeeded, "
        "kept}] (kept: a failed attempt at a retryable action, still on offer; anything else is used up).")
    report: dict[str, Any] | None = Field(
        default=None, description="The session's report once it has ended (as sent to the UNP): goal and outcome "
        "(achieved | failed), the player's stats, inventory, health and coins, and the save game's progression.")
    stakes: dict[str, Any] | None = Field(
        default=None, description="What is at stake: {alert, max, warn_at} (the watch's alert level; at max a manhunt "
        "starts), or null when stakes.alert is off.")


class SimulationResponse(PlayState):
    report: str
    run_id: str | None = Field(default=None, description="Use with POST /v1/exports to export this run or one of its generations.")
    learning: dict[str, Any] | None = Field(
        default=None,
        description="How the evolutionary AI learned: best_score, improvement, generations_run, early_stopped, "
        "warm_started_from_memory, history (per generation: best, mean, worst, spread, diversity, "
        "mutation_step, validation), best_genome and learned_traits.",
    )
    engine_id: str | None = Field(default=None, description="The engine in the records database (GET /v1/records/engines/{id}).")
    config_download_url: str
    engine_download_url: str = Field(description="The engine model file: world, storyteller, training state, memory "
                                     "and tag vectors. Load it with POST /v1/engines/load.")


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
        allow_headers=["Content-Type", "Authorization", "X-ANE-Client"],
    )


def _fail(exc: Exception):
    message = getattr(exc, "message", None) or str(exc)
    raise HTTPException(status_code=400, detail=message) from exc


def _origin(x_ane_client: str | None = Header(default=None)) -> str:
    """Where a request comes from, for the records: the studio says so with the
    ``X-ANE-Client: studio`` header; anything else is the API."""
    return "studio" if (x_ane_client or "").strip().lower() == "studio" else "api"


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
        message = store.upload_config(str(path))[-1]
        return {"message": message.lstrip("✅ ").strip(), "settings": _settings()}
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
def list_entities(kind: Literal["Locations", "NPCs", "Items", "Events"], fields: Literal["all", "names"] = "all"):
    """The entries of a kind (``fields=names``: their names only)."""
    names, _ = content._entity_name_values(kind)
    if fields == "names":
        return {"kind": kind, "names": names}
    return {"kind": kind, "entities": [get_entity(kind, name) for name in names]}


@api.get("/v1/entities/{kind}/{name}", response_model=Entity, tags=["World authoring"])
def get_entity(kind: Literal["Locations", "NPCs", "Items", "Events"], name: str):
    names, _ = content._entity_name_values(kind)
    if name not in names:
        raise HTTPException(status_code=404, detail="Entity not found.")
    (entity_name, tags, descriptions, goals, unique, encounter_event, encounter_repeat, enabled_if, title, stat_bonus,
     start_eligible, start_weight, role, secret) = content.load_entity(kind, name)
    return Entity(
        name=entity_name, tags=list(tags), descriptions=store._lines(descriptions),
        goals=store._lines(goals), unique=unique,
        encounter_event=encounter_event, encounter_repeat=encounter_repeat, enabled_if=enabled_if, title=title,
        stat_bonus=stat_bonus, start_eligible=start_eligible, start_weight=start_weight, role=role, secret=secret,
    )


@api.put("/v1/entities/{kind}/{name}", response_model=Entity, tags=["World authoring"])
def put_entity(kind: Literal["Locations", "NPCs", "Items", "Events"], name: str, body: Entity):
    try:
        content.save_entity(
            kind, name, body.name, body.tags,
            "\n".join(body.descriptions), "\n".join(body.goals), body.unique,
            body.encounter_event, body.encounter_repeat, body.enabled_if, body.title, body.stat_bonus,
            body.start_eligible, body.start_weight,
            body.role if "role" in body.model_fields_set else None,
            body.secret if "secret" in body.model_fields_set else None,
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


class EntityPolicy(BaseModel):
    policy: dict[str, Any] = Field(
        description="Events: when the event may start (trigger: action|random, chance, weight, cooldown, "
        "max_occurrences, repeat: once|per_location|per_visit|always). Items: unique, location. "
        "Fields left out use their defaults; a PUT replaces the whole policy.",
    )


@api.get("/v1/entities/{kind}/{name}/policy", response_model=EntityPolicy, tags=["World authoring"])
def get_entity_policy(kind: Literal["Events", "Items"], name: str):
    """An event's start rules or an item's uniqueness, as stored."""
    names, _ = content._entity_name_values(kind)
    if name not in names:
        raise HTTPException(status_code=404, detail="Entity not found.")
    return EntityPolicy(policy=content.load_entity_policy(kind, name))


@api.put("/v1/entities/{kind}/{name}/policy", response_model=EntityPolicy, tags=["World authoring"])
def put_entity_policy(kind: Literal["Events", "Items"], name: str, body: EntityPolicy):
    """Replace an event's start rules or an item's uniqueness after validation."""
    names, _ = content._entity_name_values(kind)
    if name not in names:
        raise HTTPException(status_code=404, detail="Entity not found.")
    try:
        return EntityPolicy(policy=content.save_entity_policy(kind, name, body.policy))
    except Exception as exc:
        _fail(exc)


class ExportRequest(BaseModel):
    source: Literal["run", "memory", "game", "engine"] = Field(
        default="run",
        description="run: an evolution run (run_id; latest if omitted); memory: the best storyteller the AI "
        "remembers; game: the storyteller of a play session (session_id); engine: a recorded engine (engine_id), "
        "with its own world and training state.",
    )
    run_id: str | None = None
    engine_id: str | None = None
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
            include_memory=body.include_memory, name=body.name, engine_id=body.engine_id,
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


@api.get("/v1/missions/playable", tags=["Evolution and play"])
def get_playable_missions(profile: str | None = None):
    """The missions a player may choose when starting a game (``mission`` in the play requests):
    [{key, title, description, stages, available}], ``available`` for this profile now."""
    try:
        return {"missions": sessions.playable_missions(profile)}
    except Exception as exc:
        _fail(exc)


@api.get("/v1/lint/failures", tags=["World authoring"])
def get_failure_lint():
    """The failures this world leaves as dead ends: {dead_ends: [{action, owner, why}]}, each an
    action that can fail (a skill check, or failure outcomes) where failing changes nothing but
    health or coins and no complication event (tagged `complication`) shares a tag with it.
    During play the engine still shows another way (director.another_way); these are the
    failures worth writing a consequence for."""
    try:
        return {"dead_ends": sessions.failure_lint()}
    except Exception as exc:
        _fail(exc)


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
def simulate(body: SimulationRequest, origin: str = Depends(_origin)):
    try:
        game = sessions.run_simulation(
            body.candidate_count, body.generations, body.simulation_steps, body.deterministic,
            profile=body.profile, episodes=body.episodes, warm_start=body.warm_start,
            stats_choice="defined" if body.stats is not None else body.stats_mode,
            stats_points=yaml.safe_dump(body.stats) if body.stats is not None else None,
            progress=None, origin=origin, mission=body.mission,
        )
        return _game_response(game)
    except HTTPException:
        raise
    except Exception as exc:
        _fail(exc)


def _game_response(game) -> "SimulationResponse":
    state = _play_state(game["token"], game["last_turn"])
    return SimulationResponse(
        **state.model_dump(), report=game["report"], learning=game["learning"], run_id=game.get("run_id"),
        engine_id=game.get("engine_id"),
        config_download_url=_register_artifact(game["config_path"]),
        engine_download_url=_register_artifact(game["engine_path"]),
    )


@api.post("/v1/engines/load", response_model=SimulationResponse, tags=["Evolution and play"])
def load_engine(file: UploadFile = File(...), parameters: UploadFile | None = File(default=None),
                profile: str | None = None, stats_mode: Literal["defined", "random", "balanced"] | None = None,
                stats: str | None = None, mission: str | None = None, origin: str = Depends(_origin)):
    """Start a game with a saved engine: an engine model file (.ane.zip), an older engine ZIP, or a
    configuration YAML. ``parameters``: a separate model-parameters YAML for a configuration without
    ``generated_run.genome``. ``stats``: the player's own points as a JSON object (overrides ``stats_mode``).
    ``mission``: the mission to play (key or title); left out, the story draws one."""
    path = _save_upload(file, {".yaml", ".yml", ".zip"}, store.MAX_ENGINE_UPLOAD_BYTES)
    parameters_path = None
    try:
        if parameters is not None:
            parameters_path = _save_upload(parameters, {".yaml", ".yml"}, 100_000)
        points = _json_object(stats, "stats")
        game = sessions.load_best_engine(
            str(path), str(parameters_path) if parameters_path else None, profile=profile,
            stats_choice="defined" if points is not None else stats_mode,
            stats_points=yaml.safe_dump(points) if points is not None else None, origin=origin, mission=mission,
        )
        return _game_response(game)
    except HTTPException:
        raise
    except Exception as exc:
        _fail(exc)
    finally:
        path.unlink(missing_ok=True)
        if parameters_path is not None:
            parameters_path.unlink(missing_ok=True)


def _json_object(text, name):
    if text in (None, ""):
        return None
    import json

    try:
        value = json.loads(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"`{name}` must be a JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail=f"`{name}` must be a JSON object.")
    return value


@api.get("/v1/sessions/{session_id}", response_model=PlayState, tags=["Evolution and play"])
def get_session(session_id: str):
    return _play_state(session_id)


@api.post("/v1/sessions/{session_id}/actions", response_model=PlayState, tags=["Evolution and play"])
def take_action(session_id: str, body: ActionRequest):
    """Take one of the actions the last response offered; anything else is refused (400)."""
    with sessions.SESSION_LOCK:
        if session_id not in sessions.SESSIONS:
            raise HTTPException(status_code=404, detail="Play session not found.")
    try:
        _, _, turn = sessions.play_action(session_id, body.action)
        return _play_state(session_id, turn)
    except Exception as exc:
        _fail(exc)


@api.post("/v1/sessions/{session_id}/end", response_model=PlayState, tags=["Evolution and play"])
def end_session(session_id: str):
    """End the session where it stands (the player stops). Like a story that ends by itself,
    it counts as a session: the player's progression is saved and the session's report goes to
    the UNP (``report`` in the response)."""
    with sessions.SESSION_LOCK:
        if session_id not in sessions.SESSIONS:
            raise HTTPException(status_code=404, detail="Play session not found.")
    try:
        sessions.end_session(session_id)
        return _play_state(session_id)
    except Exception as exc:
        _fail(exc)


@api.get("/v1/sessions/{session_id}/report", tags=["Evolution and play"])
def get_session_report(session_id: str):
    """The report of an ended session (as sent to the UNP); 409 while the session goes on."""
    with sessions.SESSION_LOCK:
        if session_id not in sessions.SESSIONS:
            raise HTTPException(status_code=404, detail="Play session not found.")
    report = sessions.session_report(session_id)
    if report is None:
        raise HTTPException(status_code=409, detail="The session has not ended; end it with POST .../end.")
    return report


@api.get("/v1/progression", tags=["Evolution and play"])
def get_progression(profile: str | None = None):
    """A player's save game: {profile, playthroughs, goals_achieved (each goal once per save
    game), sessions (their reports, with their delivery to the UNP), values, updated}."""
    try:
        return sessions.progression(profile)
    except Exception as exc:
        _fail(exc)


@api.post("/v1/progression/reports/{report_id}/send", tags=["Evolution and play"])
def resend_session_report(report_id: str, profile: str | None = None):
    """Send a kept session report to the UNP again (session_report.url or ANE_UNP_URL):
    {sent, status, error, at}."""
    try:
        return sessions.resend_report(profile, report_id)
    except Exception as exc:
        _fail(exc)


class EnginePlayRequest(BaseModel):
    profile: str | None = Field(default=None, description="Player profile for persistent variables.")
    stats_mode: Literal["defined", "random", "balanced"] | None = None
    stats: dict[str, int] | None = None
    mission: str | None = Field(default=None, description="The mission to play (its key or title, see GET "
                                "/v1/missions/playable); left out, the story draws one.")


class EngineContinueRequest(EnginePlayRequest):
    generations: int = Field(default=1, ge=1, le=store.RUN_LIMITS["generation_limit"],
                             description="Further generations, continuing exactly where training stopped.")


class EngineRetrainRequest(EnginePlayRequest):
    candidate_count: int = Field(default=8, ge=1, le=store.RUN_LIMITS["candidate_limit"])
    generations: int = Field(default=5, ge=1, le=store.RUN_LIMITS["generation_limit"])
    simulation_steps: int = Field(default=16, ge=1, le=store.RUN_LIMITS["simulation_step_limit"])
    episodes: int | None = Field(default=None, ge=1, le=10)
    deterministic: bool = True


def _player(body):
    return {"profile": body.profile, "mission": body.mission,
            "stats_choice": "defined" if body.stats is not None else body.stats_mode,
            "stats_points": yaml.safe_dump(body.stats) if body.stats is not None else None}


def _records(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _known_engine(engine_id):
    """404 for an engine that is not in the records (before any work starts)."""
    _records(records.get_engine, engine_id)


@api.get("/v1/records", tags=["Records"])
def records_info():
    """Where the records database is, and whether recording is on."""
    return {"enabled": records.enabled(), "directory": str(records.directory()),
            "database": str(records.database_path()), "models": str(records.models_dir())}


@api.get("/v1/records/runs", tags=["Records"])
def list_recorded_runs(limit: int = 50, offset: int = 0):
    return {"runs": _records(records.list_runs, limit, offset)}


@api.get("/v1/records/runs/{run_id}", tags=["Records"])
def get_recorded_run(run_id: str):
    """A run with every setting, every generation, its engines, stories and files."""
    return _records(records.get_run, run_id)


@api.get("/v1/records/worlds", tags=["Records"])
def list_recorded_worlds(limit: int = 50, offset: int = 0):
    return {"worlds": _records(records.list_worlds, limit, offset)}


@api.get("/v1/records/worlds/{world_id}", tags=["Records"])
def get_recorded_world(world_id: str, format: Literal["json", "yaml"] = "json"):
    """A recorded world with all its tuning: JSON, or ``format=yaml`` for a combined
    configuration file that POST /v1/config/upload accepts."""
    world = _records(records.get_world, world_id)
    if format == "yaml":
        handle, path = tempfile.mkstemp(prefix=f"world-{world_id[:8]}-", suffix=".yaml")
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            yaml.safe_dump(world["config"], stream, sort_keys=False, allow_unicode=True)
        return FileResponse(path, filename=f"world-{world_id}.yaml", media_type="application/x-yaml")
    return world


@api.get("/v1/records/engines", tags=["Records"])
def list_recorded_engines(run_id: str | None = None, source: str | None = None, limit: int = 200, offset: int = 0):
    return {"engines": _records(records.list_engines, run_id=run_id, source=source, limit=limit, offset=offset)}


@api.get("/v1/records/engines/{engine_id}", tags=["Records"])
def get_recorded_engine(engine_id: str):
    return _records(records.get_engine, engine_id)


@api.get("/v1/records/engines/{engine_id}/model", tags=["Records"])
def download_engine_model(engine_id: str):
    """The engine model file (for a generation's champion, assembled from its run)."""
    model = _records(records.load_engine, engine_id)
    handle, path = tempfile.mkstemp(prefix=f"engine-{engine_id[:8]}-", suffix=".ane.zip")
    os.close(handle)
    model.save(path)
    return FileResponse(path, filename=f"engine-{engine_id}.ane.zip", media_type="application/zip")


@api.post("/v1/records/engines/{engine_id}/play", response_model=SimulationResponse, tags=["Records"])
def play_recorded_engine(engine_id: str, body: EnginePlayRequest | None = None, origin: str = Depends(_origin)):
    """Start a game with a recorded engine (its own world, tuning and storyteller)."""
    _known_engine(engine_id)
    try:
        return _game_response(sessions.play_engine(engine_id, origin=origin, **_player(body or EnginePlayRequest())))
    except HTTPException:
        raise
    except Exception as exc:
        _fail(exc)


@api.post("/v1/records/engines/{engine_id}/continue", response_model=SimulationResponse, tags=["Records"])
def continue_recorded_engine(engine_id: str, body: EngineContinueRequest, origin: str = Depends(_origin)):
    """Continue the engine's training exactly where it stopped; records the run and a new engine."""
    _known_engine(engine_id)
    try:
        return _game_response(sessions.continue_engine(engine_id, body.generations, origin=origin, **_player(body)))
    except HTTPException:
        raise
    except Exception as exc:
        _fail(exc)


@api.post("/v1/records/engines/{engine_id}/retrain", response_model=SimulationResponse, tags=["Records"])
def retrain_recorded_engine(engine_id: str, body: EngineRetrainRequest, origin: str = Depends(_origin)):
    """A new run that starts from the engine's storyteller and memory, on its world."""
    _known_engine(engine_id)
    try:
        return _game_response(sessions.retrain_engine(
            engine_id, body.candidate_count, body.generations, body.simulation_steps, body.episodes,
            body.deterministic, origin=origin, **_player(body)))
    except HTTPException:
        raise
    except Exception as exc:
        _fail(exc)


@api.get("/v1/records/stories", tags=["Records"])
def list_recorded_stories(run_id: str | None = None, engine_id: str | None = None, kind: str | None = None,
                          origin: str | None = None, session_id: str | None = None, limit: int = 200, offset: int = 0):
    return {"stories": _records(records.list_stories, run_id=run_id, engine_id=engine_id, kind=kind, origin=origin,
                                session_id=session_id, limit=limit, offset=offset)}


@api.get("/v1/records/stories/{story_id}", tags=["Records"])
def get_recorded_story(story_id: str):
    """A story with its parameters, result, transcript and every turn."""
    return _records(records.get_story, story_id)


def _judged(work):
    """A judgement of the LLM judge (evaluator/game_judge.py); 503 when no model is available."""
    try:
        result = work()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"The language model judge is not available: {exc}") from exc
    result.pop("raw", None)
    return result


@api.post("/v1/records/stories/{story_id}/judge", tags=["Records"])
def judge_recorded_story(story_id: str):
    """Gemma 4 as a game critic: how engaging a recorded playthrough is for a player. Returns
    ``engagement`` (1-10), ``scores`` (agency, pacing, surprise, payoff, exploration, stakes,
    coherence, replayability; 1-10), ``spread`` (between the judge's samples), ``verdict``,
    ``best_moment`` and ``weakest_moment``. 503 when no language model is available."""
    from evaluator.game_judge import GameJudge, digest_turns

    story = _records(records.get_story, story_id)
    meta = {key: story.get(key) for key in ("mission", "start_location", "end_kind", "end_reason", "won")}
    return _judged(lambda: GameJudge().judge_digest(digest_turns(story["turn_records"], meta)))


@api.post("/v1/sessions/{session_id}/judge", tags=["Evolution and play"])
def judge_session(session_id: str):
    """The LLM judge on the game played so far in this session (as for a recorded story)."""
    from evaluator.game_judge import GameJudge, digest_state

    with sessions.SESSION_LOCK:
        state = sessions.SESSIONS.get(session_id)
        if state is None:
            raise HTTPException(status_code=404, detail="Play session not found.")
        digest = digest_state(state)
    return _judged(lambda: GameJudge().judge_digest(digest))


# ---- studio services: everything the studio does goes through these and the endpoints above ----

def _known_entity(kind, name):
    names, _ = content._entity_name_values(kind)
    if name not in names:
        raise HTTPException(status_code=404, detail="Entity not found.")


@api.get("/v1/reference", tags=["Studio"])
def get_reference():
    """What a client needs to build an authoring UI: entry kinds, suggested tags, the
    action and condition reference, repeat rules, mission goal keys, rule choices,
    stat choices, editable documents, and the defaults of a new run."""
    return {
        "entity_kinds": list(content.ENTITY_FILES),
        "common_tags": content.COMMON_TAGS,
        "action_reference": content.ACTION_REFERENCE,
        "repeat_rules": list(REPEAT_RULES),
        "repeat_default": content.REPEAT_DEFAULT,
        "goal_keys": list(Mission.GOAL_KEYS),
        "rule_choices": store.RULE_CHOICES,
        "rule_fields": list(store.RULE_FIELDS),
        "player_stat_choices": content.PLAYER_STAT_CHOICES,
        "documents": {name: description for name, (description, _, _) in DOCUMENTS.items()},
        "npc_roles": content.role_names(),
        "judge_criteria": _judge_criteria(),
        "defaults": {
            "episodes_per_genome": int(config.get("evolution.episodes_per_genome", 3)),
            "warm_start": bool(config.get("evolution.warm_start", True)),
            "profile": config.get("persistence.profile", "default"),
        },
    }


def _judge_criteria():
    from evaluator.game_judge import CRITERIA

    return dict(CRITERIA)


class AppSettingsBody(BaseModel):
    settings: dict[str, Any] = Field(description="Parameter -> value; parameters left out keep their value.")


@api.get("/v1/app-settings", tags=["Studio"])
def get_app_settings():
    """The studio's app parameters (config/ui_settings.yml): limits, defaults, queue, launch."""
    return {"settings": store.load_app_settings(), "defaults": store.APP_DEFAULTS}


@api.put("/v1/app-settings", tags=["Studio"])
def put_app_settings(body: AppSettingsBody):
    """Validate and save app parameters; they apply when the studio and the API restart."""
    unknown = sorted(set(body.settings) - set(store.APP_DEFAULTS))
    if unknown:
        raise HTTPException(status_code=400, detail="Unknown app parameters: " + ", ".join(unknown))
    try:
        store.save_app_settings({**store.load_app_settings(), **body.settings})
    except (ValueError, OSError) as exc:
        _fail(exc)
    return {"message": "App parameters saved. Restart the studio and the API to apply them.",
            "settings": store.load_app_settings()}


# YAML documents edited as text, comments kept: (description, load(profile), save(profile, text) -> (yaml, message))
DOCUMENTS = {
    "missions": ("Missions and their goals and rewards (config/missions.yaml).",
                 lambda profile: content.load_missions_yaml(), lambda profile, text: content.save_missions_yaml(text)),
    "messages": ("Every sentence the engine writes into a story (config/messages.yaml).",
                 lambda profile: content.load_messages_yaml(), lambda profile, text: content.save_messages_yaml(text)),
    "variables": ("Persistent variable definitions (config/variables.yaml).",
                  lambda profile: content.load_variables_yaml(), lambda profile, text: content.save_variables_yaml(text)),
    "map": ("The world's map: which places border which (world_definition.routes in config/locations.yaml).",
            lambda profile: content.load_map_yaml(), lambda profile, text: content.save_map_yaml(text)),
    "stats": ("Character stats, budget and checks (config/stats.yaml).",
              lambda profile: content.load_stats_yaml(), lambda profile, text: content.save_stats_yaml(text)),
    "stored_values": ("The stored persistent values and playthroughs of a player profile (?profile=).",
                      lambda profile: content.stored_values_yaml(profile),
                      lambda profile, text: content.save_stored_values_yaml(profile, text)),
}


@api.get("/v1/documents", tags=["Studio"])
def list_documents():
    return {"documents": [{"name": name, "description": description} for name, (description, _, _) in DOCUMENTS.items()]}


@api.get("/v1/documents/{name}", tags=["Studio"])
def get_document(name: str, profile: str | None = None):
    """A configuration document as YAML text, comments included."""
    if name not in DOCUMENTS:
        raise HTTPException(status_code=404, detail=f"Unknown document `{name}`; use {', '.join(DOCUMENTS)}.")
    try:
        return {"name": name, "yaml": DOCUMENTS[name][1](profile)}
    except Exception as exc:
        _fail(exc)


@api.put("/v1/documents/{name}", tags=["Studio"])
def put_document(name: str, body: YAMLDocument, profile: str | None = None):
    """Validate and save a configuration document (comments kept); nothing is written when it is invalid."""
    if name not in DOCUMENTS:
        raise HTTPException(status_code=404, detail=f"Unknown document `{name}`; use {', '.join(DOCUMENTS)}.")
    try:
        text, message = DOCUMENTS[name][2](profile, body.yaml)
        return {"name": name, "yaml": text, "message": message}
    except Exception as exc:
        _fail(exc)


@api.post("/v1/variables/reset/yaml", tags=["Persistent variables"])
def reset_variable_values_yaml(profile: str | None = None):
    """Forget a profile's stored values (a new player); returns the stored-values document."""
    try:
        text, message = content.reset_stored_values_ui(profile)
        return {"name": "stored_values", "yaml": text, "message": message}
    except Exception as exc:
        _fail(exc)


@api.get("/v1/entities/{kind}/{name}/actions/yaml", tags=["World authoring"])
def get_entity_actions_yaml(kind: Literal["Locations", "NPCs", "Items", "Events"], name: str):
    _known_entity(kind, name)
    return {"yaml": content.load_entity_actions_yaml(kind, name)}


@api.put("/v1/entities/{kind}/{name}/actions/yaml", tags=["World authoring"])
def put_entity_actions_yaml(kind: Literal["Locations", "NPCs", "Items", "Events"], name: str, body: YAMLDocument):
    """Replace the entry's actions from YAML text (validated like PUT .../actions)."""
    _known_entity(kind, name)
    try:
        text, message = content.save_entity_actions_ui(kind, name, body.yaml)
        return {"yaml": text, "message": message}
    except Exception as exc:
        _fail(exc)


@api.get("/v1/entities/{kind}/{name}/yaml", tags=["World authoring"])
def get_entity_yaml(kind: Literal["Locations", "NPCs", "Items", "Events"], name: str):
    """Every field of the entry as YAML, as stored: what the form shows and everything else
    (an NPC's role, secret, dialogue, known places and items, a location's distant views and
    exits, its actions...)."""
    _known_entity(kind, name)
    return {"yaml": content.load_entity_yaml(kind, name)}


@api.put("/v1/entities/{kind}/{name}/yaml", tags=["World authoring"])
def put_entity_yaml(kind: Literal["Locations", "NPCs", "Items", "Events"], name: str, body: YAMLDocument):
    """Replace every field of the entry from YAML (the entry keeps its name). The whole world
    is checked with the entry in place; nothing is written when it is invalid."""
    _known_entity(kind, name)
    try:
        message = content.save_entity_yaml(kind, name, body.yaml)
        return {"yaml": content.load_entity_yaml(kind, name), "message": message}
    except Exception as exc:
        _fail(exc)


@api.get("/v1/entities/{kind}/{name}/policy/yaml", tags=["World authoring"])
def get_entity_policy_yaml(kind: Literal["Locations", "NPCs", "Items", "Events"], name: str):
    _known_entity(kind, name)
    return {"yaml": content.load_entity_policy_yaml(kind, name)}


@api.put("/v1/entities/{kind}/{name}/policy/yaml", tags=["World authoring"])
def put_entity_policy_yaml(kind: Literal["Events", "Items"], name: str, body: YAMLDocument):
    _known_entity(kind, name)
    try:
        text, message = content.save_entity_policy_ui(kind, name, body.yaml)
        return {"yaml": text, "message": message}
    except Exception as exc:
        _fail(exc)


class ActionRepetition(BaseModel):
    repeat: Literal["default", "once", "per_visit", "always"] = Field(
        default="default", description="default: action_rules.default_repeat decides for its source.")
    retry_on_failure: bool = Field(default=False, description="A failed attempt does not use the action up.")


@api.get("/v1/entities/{kind}/{name}/actions/{index}/repetition", tags=["World authoring"])
def get_action_repetition(kind: Literal["Locations", "NPCs", "Items", "Events"], name: str, index: int):
    """How often the entry's action number ``index`` (from 0) is offered, and whether a failure uses it up."""
    _known_entity(kind, name)
    choices = content.entity_action_choices(kind, name)
    if not 0 <= index < len(choices):
        raise HTTPException(status_code=404, detail=f"`{name}` has no action {index}.")
    repeat, retry = content.load_action_repetition(kind, name, index)
    return {"index": index, "label": choices[index][0], "repeat": repeat, "retry_on_failure": retry}


@api.put("/v1/entities/{kind}/{name}/actions/{index}/repetition", tags=["World authoring"])
def put_action_repetition(kind: Literal["Locations", "NPCs", "Items", "Events"], name: str, index: int,
                          body: ActionRepetition):
    _known_entity(kind, name)
    try:
        message = content.save_action_repetition(kind, name, index, body.repeat, body.retry_on_failure)
        return {"message": message, "yaml": content.load_entity_actions_yaml(kind, name),
                **get_action_repetition(kind, name, index)}
    except HTTPException:
        raise
    except Exception as exc:
        _fail(exc)


# ---- jobs: long runs with progress -------------------------------------------------------

def _job_or_404(job_id):
    job = sessions.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@api.post("/v1/jobs/simulations", status_code=202, tags=["Jobs"])
def submit_simulation(body: SimulationRequest, origin: str = Depends(_origin)):
    """Run an evolution in the background (as POST /v1/simulations); poll GET /v1/jobs/{id}
    for its progress per generation and, when finished, the playable game."""
    def work(progress):
        game = sessions.run_simulation(
            body.candidate_count, body.generations, body.simulation_steps, body.deterministic,
            profile=body.profile, episodes=body.episodes, warm_start=body.warm_start,
            stats_choice="defined" if body.stats is not None else body.stats_mode,
            stats_points=yaml.safe_dump(body.stats) if body.stats is not None else None,
            progress=progress, origin=origin, mission=body.mission,
        )
        return _game_response(game).model_dump()
    return sessions.submit_job("simulation", work, f"{body.generations} generation(s) of {body.candidate_count} storytellers")


@api.post("/v1/jobs/engines/{engine_id}/continue", status_code=202, tags=["Jobs"])
def submit_continue(engine_id: str, body: EngineContinueRequest, origin: str = Depends(_origin)):
    """Continue a recorded engine's training in the background (as POST /v1/records/engines/{id}/continue)."""
    _known_engine(engine_id)
    def work(progress):
        return _game_response(sessions.continue_engine(engine_id, body.generations, origin=origin, progress=progress,
                                                       **_player(body))).model_dump()
    return sessions.submit_job("continue", work, f"{body.generations} more generation(s) of engine {engine_id}")


@api.post("/v1/jobs/engines/{engine_id}/retrain", status_code=202, tags=["Jobs"])
def submit_retrain(engine_id: str, body: EngineRetrainRequest, origin: str = Depends(_origin)):
    """Retrain from a recorded engine in the background (as POST /v1/records/engines/{id}/retrain)."""
    _known_engine(engine_id)
    def work(progress):
        return _game_response(sessions.retrain_engine(
            engine_id, body.candidate_count, body.generations, body.simulation_steps, body.episodes,
            body.deterministic, origin=origin, progress=progress, **_player(body))).model_dump()
    return sessions.submit_job("retrain", work, f"{body.generations} generation(s) from engine {engine_id}")


class StoryRef(BaseModel):
    session_id: str | None = Field(default=None, description="A game in progress.")
    story_id: str | None = Field(default=None, description="A recorded story (GET /v1/records/stories).")


class CompareRequest(BaseModel):
    a: StoryRef
    b: StoryRef


class JudgeBenchmarkRequest(BaseModel):
    stories: int = Field(default=10, ge=1, le=evaluation.MAX_STORIES)
    seed: int = Field(default=0, ge=0, description="The first story's seed (stories use seed, seed+1, ...).")
    engine_id: str | None = Field(default=None, description="A recorded engine's world and storyteller; "
                                  "left out: the current settings with random storytellers.")
    pairwise: bool = Field(default=False, description="Compare the current settings (A) with `variants` (B) on "
                           "the same seeds, each pair judged in both orders.")
    variants: dict[str, Any] = Field(default_factory=dict, description="Settings of version B: {dotted.path: value}, "
                                     "e.g. {\"climax.enabled\": false}.")
    workers: int = Field(default=4, ge=1, le=8, description="Judgements sent to the model at once.")


class CalibrationRequest(BaseModel):
    stories: int = Field(default=30, ge=10, le=evaluation.MAX_STORIES)
    seed: int = Field(default=0, ge=0)
    engine_id: str | None = None
    alpha: float | None = Field(default=None, gt=0, description="Ridge strength; left out: the one that predicts "
                                "left-out stories best.")
    apply: bool = Field(default=False, description="Save the suggested multipliers as fitness.term_weights.")
    workers: int = Field(default=4, ge=1, le=8)


class TermWeights(BaseModel):
    weights: dict[str, float] = Field(description="Fitness term -> multiplier (1: unchanged); {} removes them all.")


def _evaluation_error(exc):
    if isinstance(exc, evaluation.EvaluationError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, RuntimeError):
        raise HTTPException(status_code=503, detail=f"The language model judge is not available: {exc}") from exc
    _fail(exc)


@api.post("/v1/judge/compare", tags=["Evaluation"])
def compare_stories(body: CompareRequest):
    """Gemma 4 as a game critic on two stories (each a game in progress or a recorded story):
    which is the better game, judged in both orders so that the judge's preference for a
    position cancels. {winner: A | B | tie (null: unreadable), criteria: {criterion: A | B |
    tie}, reason, agreed}. 400 for an unknown story, 503 without a language model."""
    try:
        return evaluation.compare(body.a.model_dump(), body.b.model_dump())
    except Exception as exc:
        _evaluation_error(exc)


@api.post("/v1/jobs/judge-benchmark", status_code=202, tags=["Jobs", "Evaluation"])
def submit_judge_benchmark(body: JudgeBenchmarkRequest):
    """The judge on the engine's stories, in the background (poll GET /v1/jobs/{id}). Absolute:
    each story judged ({engagement, criteria, rows}). Pairwise: the same seeds played with the
    current settings (A) and with `variants` (B), every pair judged in both orders ({pairs,
    a_wins, b_wins, ties, b_win_rate, wilson_95, criteria, rows}). Stories are played in a
    separate process: the settings of games in progress never change."""
    if body.pairwise and not body.variants:
        raise HTTPException(status_code=400, detail="A pairwise benchmark needs the settings of version B (`variants`).")
    if body.engine_id:
        _known_engine(body.engine_id)

    def work(progress):
        return evaluation.benchmark(body.stories, body.seed, body.engine_id, body.variants or None, body.pairwise,
                                    body.workers, progress=progress)
    kind = "pairwise A/B" if body.pairwise else "judge benchmark"
    return sessions.submit_job("judge-benchmark", work, f"{kind} of {body.stories} stories")


@api.post("/v1/jobs/calibrate-fitness", status_code=202, tags=["Jobs", "Evaluation"])
def submit_calibration(body: CalibrationRequest):
    """Fit the fitness to the judge in the background: play and judge `stories` stories, fit the
    judged engagement on the fitness terms and suggest a multiplier per term ({term_weights,
    terms, spearman, cv_spearman (how well it predicts stories it was not fitted on), alpha, n,
    yaml, applied}); `apply` saves them as fitness.term_weights."""
    if body.engine_id:
        _known_engine(body.engine_id)

    def work(progress):
        return evaluation.calibrate(body.stories, body.seed, body.engine_id, body.alpha, body.apply, body.workers,
                                    progress=progress)
    return sessions.submit_job("calibrate-fitness", work, f"fit the fitness to the judge on {body.stories} stories")


@api.get("/v1/fitness/term-weights", tags=["Evaluation"])
def get_term_weights():
    """fitness.term_weights: the multiplier of each fitness term (missing: 1)."""
    return {"weights": dict(config.get("fitness.term_weights") or {})}


@api.put("/v1/fitness/term-weights", tags=["Evaluation"])
def put_term_weights(body: TermWeights):
    """Save fitness.term_weights (e.g. those a calibration suggested); {} removes them."""
    try:
        weights, message = store.set_term_weights(body.weights)
        return {"weights": weights, "message": message}
    except Exception as exc:
        _fail(exc)


@api.get("/v1/jobs", tags=["Jobs"])
def list_jobs():
    return {"jobs": sessions.list_jobs()}


@api.get("/v1/jobs/{job_id}", tags=["Jobs"])
def get_job(job_id: str):
    """status (queued, running, finished, failed), progress (generation, total, best, mean,
    validation), and the result (as the synchronous endpoint returns it) or the error."""
    return _job_or_404(job_id)


@api.get("/v1/artifacts/{artifact_id}", tags=["Artifacts"])
def download_artifact(artifact_id: str):
    path = ARTIFACTS.get(artifact_id)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found.")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


app = api
