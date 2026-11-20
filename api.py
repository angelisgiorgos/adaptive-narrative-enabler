"""REST API for Adaptive Narrative Enabler.

Swagger is available at ``/docs``. Keep a single Uvicorn worker because runtime
configuration overrides and play sessions are intentionally held in process.
"""

from __future__ import annotations

import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, Field

import yaml

from backend import content, engine_meta, evaluation, export, records, sessions, store, tuning
from backend.errors import ConflictError
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


class TrainingOverrides(BaseModel):
    """What one run sets for itself; anything left out (or null) is the saved engine default
    (evolution.*, reproducibility.*: ⚙ Advanced → Simulation). POST /v1/simulations/resolve shows
    what a run with these would use."""
    candidate_count: int | None = Field(default=None, ge=1, le=store.RUN_LIMITS["candidate_limit"],
                                        description="Population: storytellers per generation (default evolution.pop_size).")
    generations: int | None = Field(default=None, ge=1, le=store.RUN_LIMITS["generation_limit"],
                                    description="Default evolution.generations.")
    simulation_steps: int | None = Field(default=None, ge=1, le=store.RUN_LIMITS["simulation_step_limit"],
                                         description="Steps per evaluated story (default evolution.max_steps); with "
                                         "evolution.full_length_stories every story is played to its end.")
    episodes: int | None = Field(default=None, ge=1, le=10, description="Stories each storyteller plays per generation "
                                 "(default evolution.episodes_per_genome).")
    deterministic: bool | None = Field(default=None, description="Default reproducibility.deterministic.")
    seed: int | None = Field(default=None, description="The seed of a deterministic run (default reproducibility.seed).")


class TrainingGame(BaseModel):
    start_game: bool = Field(default=True, description="Start a game with the best storyteller when the training "
                             "finishes (the response's session). false: train only; no session is created and nothing "
                             "is written to the player's profile (the response's session_id is null).")


class SimulationRequest(TrainingOverrides, TrainingGame):
    profile: str | None = Field(default=None, description="Player profile for persistent variables.")
    stats_mode: Literal["defined", "random", "balanced"] | None = Field(
        default=None, description="How the player's stats are allocated (default: character_stats.mode).",
    )
    stats: dict[str, int] | None = Field(default=None, description="The player's own stat points (overrides stats_mode).")
    warm_start: bool | None = Field(default=None, description="Continue learning from the storytellers remembered from "
                                    "earlier runs (default evolution.warm_start).")
    mission: str | None = Field(default=None, description="The mission to play (its key or title, see GET "
                                "/v1/missions/playable); left out, the story draws one.")


class ActionRequest(BaseModel):
    action: str = Field(min_length=1)


class WeightUpdate(BaseModel):
    values: dict[str, float]
    expected: dict[str, float] | None = Field(
        default=None, description="Optional: the value each changed parameter had when the form was loaded; a "
        "parameter changed since (by someone else) is refused (409) instead of overwritten.")


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
    talking_to: str | None = Field(default=None, description="The person the player is talking with (their choices "
                                                             "and leaving the conversation are offered), if any.")
    turn_summary: dict[str, Any] | None = Field(
        default=None, description="The last turn, as a player follows it (GET /v1/sessions/{id}/journal: every turn): "
        "{turn, action, with, where, moved_to, check {stat, roll, needed, passed}, succeeded, told, health, "
        "health_change, coins, coin_change, items_gained, items_lost, met, new_places, event, quest_done, quest_now, "
        "quest_complete, ended, end_reason}.")
    mission: str | None = Field(default=None, description="Title of the story's mission, if any.")
    won: bool = Field(default=False, description="True once the mission goal has been achieved.")
    location: str | None = None
    scene_name: str | None = Field(default=None, description="Where the player is, as the story names it "
                                                             "(an event: its title at the place).")
    description: str | None = Field(default=None, description="The place (or event) as the player sees it now.")
    items_here: list[dict[str, Any]] = Field(default_factory=list, description="Items the player can see here: [{name, text}].")
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
    session_report: dict[str, Any] | None = Field(
        default=None, description="The session's report once it has ended (as sent to the UNP): goal and outcome "
        "(achieved | failed), the player's stats, inventory, health and coins, and the save game's progression.")
    stakes: dict[str, Any] | None = Field(
        default=None, description="What is at stake: {alert, max, warn_at} (the watch's alert level; at max a manhunt "
        "starts), or null when stakes.alert is off.")


class SimulationResponse(PlayState):
    session_id: str | None = Field(default=None, description="The game started with the result; null when a "
                                   "training was asked for with start_game false.")
    scene: str = ""
    actions: list[str] = Field(default_factory=list)
    ended: bool = False
    game_started: bool = Field(default=True, description="False for a training asked for with start_game false: "
                               "the play fields are then empty.")
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
    training: dict[str, Any] | None = Field(
        default=None, description="A training's settings: `overrides` (what the request set), `sources` (each value: "
        "override or default), `resolved` (as POST /v1/simulations/resolve gives them) and `executed` (read back "
        "from the evolution that ran): population, generations, stories_per_storyteller, requested_steps, "
        "story_turns, full_length_stories, story_length, deterministic, seed, warm_start, warm_start_mode.")


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

@api.exception_handler(StarletteHTTPException)
async def _public_http_errors(request, exc):
    """Every error the API answers, without this server's file paths (public_message)."""
    if isinstance(exc.detail, str):
        exc = StarletteHTTPException(status_code=exc.status_code, detail=public_message(exc.detail),
                                     headers=getattr(exc, "headers", None))
    return await http_exception_handler(request, exc)


origins = [item.strip() for item in os.getenv("ANE_CORS_ORIGINS", "").split(",") if item.strip()]
if origins:
    api.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "PUT", "PATCH", "POST", "DELETE"],
        allow_headers=["Content-Type", "Authorization", "X-ANE-Client", "X-ANE-User"],
    )


_SERVER_PATH = re.compile(r"(?<![\w.:/-])/(?!v1/)[\w.@+-]+(?:/[\w.@+-]+)+")


def public_message(message: str) -> str:
    """An error message without this server's file paths (only a file's name): where the records,
    saves and models are on the server is not the client's business. API routes (/v1/...) stay."""
    return _SERVER_PATH.sub(lambda match: Path(match.group(0)).name, str(message))


def _fail(exc: Exception):
    if isinstance(exc, PermissionError):
        message = (f"This server is not allowed to use {Path(exc.filename).name if exc.filename else 'a file it needs'} "
                   "(permission denied).")
    else:
        message = getattr(exc, "message", None) or str(exc)
    status = 409 if isinstance(exc, (export.RuntimeUnavailable, export.EngineRequired, ConflictError)) else 400
    raise HTTPException(status_code=status, detail=public_message(message)) from exc


def _origin(x_ane_client: str | None = Header(default=None)) -> str:
    """Where a request comes from, for the records: the studio says so with the
    ``X-ANE-Client: studio`` header; anything else is the API."""
    return "studio" if (x_ane_client or "").strip().lower() == "studio" else "api"


USER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _run_user(x_ane_client: str | None = Header(default=None), x_ane_user: str | None = Header(default=None)) -> str:
    """Whose run it is, for the records (runs.user_id). The studio's runs are the user ``test``
    (records.DEFAULT_USER). An API client gives the id of its user in the ``X-ANE-User`` header,
    as the backend that calls the API knows them: an opaque id (a UUID, an account number), never a
    name or an e-mail address. A run without one is refused (400)."""
    if (x_ane_client or "").strip().lower() == "studio":
        return records.DEFAULT_USER
    user = (x_ane_user or "").strip()
    if not user:
        raise HTTPException(status_code=400, detail=(
            "X-ANE-User is required to start a run: the id of the user it is for, given by the backend that calls "
            "the API (an opaque id such as a UUID; never a name or an e-mail address)."))
    if not USER_ID.match(user) or "@" in user:
        raise HTTPException(status_code=400, detail=(
            "X-ANE-User must be an opaque id: 1-128 letters, digits and . _ : - (never a name or an e-mail address)."))
    return user


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


def _weights():
    entries = store._weight_entries()
    shipped = tuning.shipped_values()
    return {"values": dict(entries),
            "parameters": [{**tuning.describe(path, value), "shipped": shipped.get(path)} for path, value in entries],
            "basic": tuning.basic_view(path for path, _ in entries),
            "shipped": {path: shipped[path] for path, _ in entries if path in shipped}}


@api.get("/v1/weights", tags=["Configuration"])
def get_weights():
    """The tuning parameters (every weight, bonus, penalty, threshold, chance, rate, ... of
    hyperparameters.yaml): ``values`` {path: value}, and ``parameters``, how each is presented:
    [{path, value, section, label, explanation, unit, type: integer | float, minimum, maximum, default
    (the engine's, when the parameter is left out), source (the code reading it), described}]. Units,
    bounds and defaults come from the engine's code; a parameter without metadata (``described``
    false) has a label from its name and the type it is written with, nothing more. ``requires``: the
    true/false setting a parameter needs, ``inactive``: why it has no effect now (that setting is off).
    ``basic``: the Basic view, a chosen set of common gameplay controls, each ONE of these parameters
    (no presets): [{key, title, summary, controls: [{path, effect, scaled_by?, scaling?}]}] (``scaling``:
    how far each storyteller's learned traits move the value). ``shipped``: {path: value} as this release
    ships them (config/default_history.json), to tell which saved values were changed."""
    return _weights()


@api.put("/v1/weights", tags=["Configuration"])
def put_weights(body: WeightUpdate):
    """Change tuning parameters (paths left out keep their value). Every value is checked first: a
    finite number, a whole number for an integer parameter (a fraction is refused, not cut down),
    within the bounds the engine defines; one wrong value saves nothing (400, each problem named). A
    value that did not change is kept exactly as it is written. With ``expected`` (the values the form
    was loaded with), a changed parameter whose stored value moved since is refused (409)."""
    known = dict(store._weight_entries())
    unknown = sorted(set(body.values) - set(known))
    if unknown:
        raise HTTPException(status_code=400, detail="Unknown weight paths: " + ", ".join(unknown))
    moved = [f"`{path}` is now {known[path]:g} (it was {body.expected[path]:g} when loaded)"
             for path in body.values if body.expected and path in body.expected
             and float(known[path]) != float(body.expected[path])]
    if moved:
        raise HTTPException(status_code=409, detail="Changed since it was loaded, so nothing was saved; reload "
                            "to see the newer values: " + "; ".join(moved) + ".")
    merged = {**known, **body.values}
    paths = list(known)
    try:
        message = store.save_weights(paths, *(merged[path] for path in paths))
        return {"message": message, **_weights()}
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


@api.get("/v1/entities/{kind}/{name}/references", tags=["World authoring"])
def get_entity_references(kind: Literal["Locations", "NPCs", "Items", "Events"], name: str):
    """What refers to the entry and would break if it were deleted (missions, actions, conditions,
    dialogue, routes, ...), as the engine's checks word it: {references: [...]} (empty: nothing).
    Deleting is still allowed; a deleted entry's restore is checked against them."""
    names, _ = content._entity_name_values(kind)
    if name not in names:
        raise HTTPException(status_code=404, detail="Entity not found.")
    return {"references": content.entity_references(kind, name)}


@api.delete("/v1/entities/{kind}/{name}", tags=["World authoring"])
def delete_entity(kind: Literal["Locations", "NPCs", "Items", "Events"], name: str):
    """Delete an entry (at least one location, NPC and item remains: 400 for the last). ``undo``: what
    POST /v1/deletions/{token}/restore puts back (the entry whole, where it was)."""
    names, _ = content._entity_name_values(kind)
    if name not in names:
        raise HTTPException(status_code=404, detail="Entity not found.")
    try:
        result = content.remove_entity(kind, name)
        return {"message": f"Deleted {name}.", "entities": result["entities"], "undo": result["undo"]}
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


class LoadedState(PlayState):
    restored: Literal["exact", "parameters"] = Field(
        description="exact: the save game's snapshot fits this engine and world, and the story goes on exactly; "
        "parameters: the session was rebuilt from its parameters (`note` says why).")
    note: str | None = None


class SaveGameLoad(BaseModel):
    savegame: dict[str, Any] = Field(description="An ane.savegame/1 document (GET /v1/sessions/{id}/savegame, or the "
                                                 "ANE AI's POST /games/{id}/save).")
    profile: str | None = Field(default=None, description="The profile to continue it in (default: the save game's).")
    session_id: str | None = Field(default=None, description="For a save game without a snapshot: the play session "
                                                             "whose storyteller continues it.")


class ExportRequest(BaseModel):
    source: Literal["run", "memory", "game", "engine"] = Field(
        default="run",
        description="run: an evolution run (run_id; latest if omitted); memory: the best storyteller the AI "
        "remembers; game: the storyteller of a play session (session_id); engine: a recorded engine (engine_id), "
        "with its own world and training state. An ANE AI (a standalone platform or unity) is made only from a "
        "complete engine: run (the run's engine, no generation) or engine (engine_id, default the newest; "
        "GET /v1/exports/engines); 409 otherwise.",
    )
    run_id: str | None = None
    engine_id: str | None = None
    generation: int | None = Field(default=None, ge=0, description="Export this generation's champion instead of the run's best.")
    session_id: str | None = None
    include_memory: bool = Field(default=True, description="Ship the learning memory so the engine can keep training offline.")
    name: str | None = Field(default=None, description="Folder name of the export.")
    platform: Literal["python", "windows-x64", "linux-x64", "macos-arm64", "macos-x64", "unity"] = Field(
        default="python", description="What runs the ANE AI: python (the engine code, Python 3.10+ with pyyaml and "
        "numpy), a standalone runtime for that system (no Python needed), or unity (a .unitypackage: the C# library "
        "and every standalone runtime available here). See GET /v1/exports/platforms; 409 when the runtime is missing "
        "or was built from other engine code, or when there is no complete engine to make the ANE AI from.")


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


@api.get("/v1/behaviours", tags=["Engine logic"])
def get_behaviours():
    """What people and places do, by their tags (`npc_behaviour`, `location_behaviour`): for each
    behaviour its values, its default and every tag a rule gives it ({tag, value}), as {npcs: {...},
    locations: {...}}. An NPC or a place behaves as the first such tag it has says (the studio's
    choices set the tag); the rules are edited with `PUT /v1/logic/npc_behaviour` and
    `PUT /v1/logic/location_behaviour`."""
    from core_engine import rules
    from utils.config_loader import config

    found = {}
    for kind, plural in (("npc", "npcs"), ("location", "locations")):
        cfg = config.get(rules.SECTIONS[kind]) or {}
        options = rules.behaviour_options(kind, cfg)
        found[plural] = {key: {"values": list(values) + (["a number of visits"] if key in rules.COUNTED else []),
                               "default": (cfg.get("default") or {}).get(key, rules.BEHAVIOUR_DEFAULTS[key]),
                               "tags": options[key]}
                         for key, values in rules.BEHAVIOURS[kind].items()}
    return found


@api.post("/v1/exports", tags=["Engine export"])
def export_offline_engine(body: ExportRequest):
    """Export a trained engine (code, world, storyteller, tag vectors, learning memory) as a ZIP
    that runs offline and integrates in a game: Python embedding, HTTP/JSON server, command line."""
    try:
        result = export.export_engine(
            source=body.source, run_id=body.run_id, generation=body.generation, token=body.session_id,
            include_memory=body.include_memory, name=body.name, engine_id=body.engine_id, platform=body.platform,
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
        "platform": manifest["platform"],
        "code_version": manifest["code_version"],
        "engine_id": manifest["engine_id"],
    }


@api.get("/v1/exports/platforms", tags=["Engine export"])
def export_platforms():
    """What an ANE AI export can run on: python (always), and each standalone runtime
    (windows-x64, linux-x64, macos-arm64, macos-x64) with whether it is available here and fits
    this API's engine code (``code_version``)."""
    from core_engine import version

    return {"platforms": export.platforms(), "code_version": version.code_version(), "protocol": version.PROTOCOL}


@api.get("/v1/exports/engines", tags=["Engine export"])
def export_engines():
    """The complete engines an ANE AI (a standalone or Unity export) can be made from, newest
    first: each finished run's engine model and each loaded engine (not a generation's champion,
    the remembered storyteller or a play session). Empty until a training finishes or an engine
    is loaded."""
    return {"engines": export.complete_engines()}


@api.get("/v1/runs", tags=["Engine export"])
def list_runs():
    """Recent evolution runs that can be exported (best or any generation), each with its engine's
    id, name (None: unnamed) and label."""
    known = engine_meta.names()
    runs = []
    for run_id, run in reversed(sessions.RUNS.items()):
        engine_id = run.get("engine_id")
        kept = sessions.ENGINES.get(engine_id) or {}
        info = {"engine_id": engine_id, "created_at": kept.get("created_at"), "source": kept.get("source"),
                "score": run["learning"]["best_score"],
                "generations": len(run["learning"]["history"])}
        runs.append({"run_id": run_id, "best_score": run["learning"]["best_score"],
                     "generations": len(run["learning"]["history"]), "engine_id": engine_id,
                     "name": known.get(engine_id) if engine_id else None,
                     "label": engine_meta.label(info, known) if engine_id else None})
    return {"runs": runs}


class EngineName(BaseModel):
    name: str | None = Field(default=None, description=f"The engine's name (at most {engine_meta.MAX_NAME} "
                             "characters); empty or null removes it. The engine id does not change.")


@api.get("/v1/engines/{engine_id}/card", tags=["Evolution and play"])
def get_engine_card(engine_id: str):
    """A completed engine's result card, recorded or kept with the saves (recording off): {engine_id,
    run_id, name, label, default_label, created_at, kind, source, parent_id, world: {id, digest,
    summary, current (trained on the active world)}, generations, trained_generations, settings:
    {main, other} (what its training ran with), fitness: {training (the evolution's objective: the
    story score on the run's benchmark), improvement, early_stopped, history}, judge_in_training (the
    judge's engagement in the loop of evolution, when it ran), evaluations (judge benchmarks and
    calibrations of this engine: the judge's 1–10 engagement, newest first), resumable}."""
    try:
        return export.engine_card(engine_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@api.put("/v1/engines/{engine_id}/name", tags=["Evolution and play"])
def put_engine_name(engine_id: str, body: EngineName):
    """Name a completed (or recorded) engine, or remove its name. Only what is shown changes: the
    engine and run ids stay its identifiers. Kept with the saves, with recording on or off."""
    if not export.known_engine(engine_id):
        raise HTTPException(status_code=404, detail=f"Unknown engine `{engine_id}`.")
    try:
        name = engine_meta.set_name(engine_id, body.name)
    except engine_meta.EngineNameError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    card = export.engine_card(engine_id)
    return {"engine_id": engine_id, "name": name, "label": card["label"]}


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
def simulate(body: SimulationRequest, origin: str = Depends(_origin), user_id: str = Depends(_run_user)):
    try:
        game = sessions.run_simulation(
            body.candidate_count, body.generations, body.simulation_steps, body.deterministic,
            profile=body.profile, episodes=body.episodes, warm_start=body.warm_start, seed=body.seed,
            stats_choice="defined" if body.stats is not None else body.stats_mode,
            stats_points=yaml.safe_dump(body.stats) if body.stats is not None else None,
            progress=None, origin=origin, mission=body.mission, user_id=user_id, start_game=body.start_game,
        )
        return _game_response(game)
    except HTTPException:
        raise
    except Exception as exc:
        _fail(exc)


def _game_response(game) -> "SimulationResponse":
    if game.get("token") is None:  # a training without a game (start_game false)
        state = {"session_id": None, "game_started": False}
    else:
        state = _play_state(game["token"], game["last_turn"]).model_dump()
    return SimulationResponse(
        **state, report=game["report"], learning=game["learning"], run_id=game.get("run_id"),
        engine_id=game.get("engine_id"), training=game.get("training"),
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


def _session_call(session_id, work):
    """Run ``work`` on a play session: 404 when there is none, 400 for parameters that do not fit."""
    try:
        return work()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Play session not found.") from exc
    except Exception as exc:
        _fail(exc)


class WorkOverride(BaseModel):
    setting: str = Field(description="The setting that decides a story's length instead of the steps: "
                                     "evolution.full_length_stories or narrative_end.max_actions.")
    reason: str


class SimulationWork(BaseModel):
    work: int = Field(description="Simulated turns at most: storytellers x generations x stories per storyteller x "
                                  "effective_max_turns (many stories end sooner).")
    limit: int = Field(description="The app parameter max_simulation_work.")
    allowed: bool = Field(description="work <= limit: a run asking for more is refused (400).")
    over_by: int = Field(description="How far work is above the limit (0 when allowed).")
    requested_steps: int = Field(description="The steps per story asked for (simulation_steps).")
    effective_max_turns: int = Field(description="The most turns a story can take in this run.")
    story_turns: int = Field(description="The same as effective_max_turns (kept for older clients).")
    full_length_stories: bool = Field(description="evolution.full_length_stories: every story is played to its end.")
    story_length: int = Field(description="Turns after which a story ends by itself (narrative_end.max_actions, at "
                                          "least 100).")
    steps_decide: bool = Field(description="Whether the requested steps set effective_max_turns.")
    override: WorkOverride | None = Field(default=None, description="Why the steps do not set it, when they do not.")
    factors: dict[str, int] = Field(description="The multiplication: storytellers, generations, "
                                                "stories_per_storyteller, turns_per_story.")


@api.get("/v1/simulations/work", response_model=SimulationWork, tags=["Evolution and play"])
def simulation_work(candidate_count: int | None = None, generations: int | None = None,
                    simulation_steps: int | None = None, episodes: int | None = None):
    """What a run would ask for before it starts, as the run itself counts it (each value left out:
    the saved engine default): the steps asked for against the turns a story can really take (with
    full-length stories every story is played to its end whatever the steps; otherwise the steps,
    though no story outlasts its end) and why they differ, and the ``work`` (storytellers x
    generations x stories per storyteller x turns per story) against the ``limit`` (app parameter
    ``max_simulation_work``). The work part of POST /v1/simulations/resolve."""
    given = [value for value in (candidate_count, generations, simulation_steps, episodes) if value is not None]
    if given and min(given) < 1:
        raise HTTPException(status_code=422, detail="Every value must be at least 1.")
    return sessions.resolve_training({"candidate_count": candidate_count, "generations": generations,
                                      "simulation_steps": simulation_steps, "episodes": episodes})["work"]


class TrainingResolveRequest(TrainingOverrides):
    warm_start: bool | None = Field(default=None, description="Default evolution.warm_start.")
    engine_id: str | None = Field(default=None, description="Resolve a retraining of this recorded engine: its saved "
                                  "defaults, and a warm start from it.")


class ResolvedTraining(BaseModel):
    defaults: dict[str, Any] = Field(description="The saved engine defaults: candidate_count, generations, episodes, "
                                     "simulation_steps, deterministic, seed, warm_start, full_length_stories, "
                                     "story_length (and story_length_setting, narrative_end.max_actions as saved).")
    overrides: dict[str, Any] = Field(description="What the request sets for this run.")
    sources: dict[str, str] = Field(description="Each effective value: override, default (or the engine it starts from).")
    effective: dict[str, Any] = Field(description="What the run executes: population, generations, "
                                      "stories_per_storyteller, requested_steps, story_turns, full_length_stories, "
                                      "story_length, deterministic, seed (null: random), warm_start, warm_start_mode "
                                      "(memory, engine or off).")
    work: SimulationWork
    notes: list[str] = Field(description="Values held to the app's limits, a seed a random run does not use.")
    errors: list[str] = Field(description="Why the run would be refused besides its work (a deterministic run "
                              "without a seed); too much work: work.allowed is false.")
    ready: bool = Field(description="The run would start: no errors and its work allowed.")
    remembered: int | None = Field(default=None, description="Storytellers in the learning memory a warm start "
                                   "begins from (now).")


def _overrides(body) -> dict:
    return {field: getattr(body, field, None) for field in sessions.TRAINING_OVERRIDES}


@api.post("/v1/simulations/resolve", response_model=ResolvedTraining, tags=["Evolution and play"])
def resolve_simulation(body: TrainingResolveRequest):
    """What a run with these settings would execute, before it starts: the saved engine defaults
    (⚙ Advanced → Simulation) with what the request sets for itself, held to the app's limits, with
    the seed, how long a story is, the warm start and the work. POST /v1/simulations (or the job, or
    a retraining with ``engine_id``) with the same body runs exactly ``effective``; its response's
    ``training`` says what was resolved and what the evolution ran with."""
    source = None
    if body.engine_id:
        _known_engine(body.engine_id)
        source = sessions._recorded_model(body.engine_id).config
    try:
        return sessions.resolve_training(_overrides(body), source=source, warm_start_from=body.engine_id)
    except Exception as exc:
        _fail(exc)


@api.get("/v1/sessions/{session_id}/state", tags=["Evolution and play"])
def get_session_parameters(session_id: str):
    """The session's parameters (ane.session_state/1), as the exported ANE AI sends them to a UNP:
    where the player is, the way taken, items, stats, health, coins, variables, mission stage,
    one-time actions, people met. See docs/UNP.md."""
    return _session_call(session_id, lambda: sessions.session_parameters(session_id))


@api.patch("/v1/sessions/{session_id}/player", response_model=PlayState, tags=["Evolution and play"])
def patch_session_player(session_id: str, player: dict[str, Any] = Body(
        ..., examples=[{"coins": 20, "add_items": ["Ancient Map"], "health": 8}])):
    """Set the player's state as a UNP does: ``stat_points`` (a whole allocation),
    ``stat_changes``, ``inventory`` (exactly these items), ``add_items``, ``remove_items``,
    ``coins``, ``health``, ``max_health``, ``variables``. Set, not played: nothing is narrated
    or paid."""
    applied = _session_call(session_id, lambda: sessions.update_player(session_id, player))
    return _play_state(session_id, f"Player set by the UNP: {', '.join(applied) or 'nothing'}.")


@api.post("/v1/sessions/{session_id}/sync", response_model=PlayState, tags=["Evolution and play"])
def sync_session(session_id: str, events: dict[str, Any] = Body(
        ..., examples=[{"type": "item_gained", "item": "Ancient Map"}])):
    """What happened in the UNP's game: one event, or ``{"events": [...]}``. Types:
    ``action_done`` (``action``, ``owner``, ``where``, ``succeeded``), ``item_gained`` /
    ``item_lost`` (``item``), ``npc_removed`` (``npc``), ``location_visited`` (``location``),
    ``coins_changed`` / ``health_changed`` (``amount``). Goals are checked afterwards."""
    taken = _session_call(session_id, lambda: sessions.sync_session(session_id, events))
    return _play_state(session_id, f"From the UNP's game: {', '.join(taken)}.")


@api.get("/v1/sessions/{session_id}/savegame", tags=["Evolution and play"])
def get_session_savegame(session_id: str):
    """The session's save game (ane.savegame/1): its parameters and an exact snapshot, as the
    ANE AI's save. The session goes on. ``POST /v1/sessions/load`` continues it."""
    return _session_call(session_id, lambda: sessions.session_savegame(session_id))


@api.post("/v1/sessions/load", response_model=LoadedState, tags=["Evolution and play"])
def load_session(body: SaveGameLoad):
    """Continue a save game as a new play session: exactly when its snapshot fits this engine and
    world (``restored: exact``), else rebuilt from its parameters (``restored: parameters``,
    ``note`` says why). A save game from the ANE AI or from another engine is accepted."""
    try:
        token, how, note = sessions.load_savegame(body.savegame, profile=body.profile, session_id=body.session_id)
    except Exception as exc:
        _fail(exc)
    state = _play_state(token, "[RESUMED] The story goes on." if how == "parameters" else "")
    return LoadedState(**state.model_dump(), restored=how, note=note)


@api.get("/v1/world/catalogue", tags=["Engine export"])
def get_world_catalogue():
    """The world as the exported ANE AI sends it to a UNP (``GET /world``, ane.world/1): every
    location, NPC, item, event and mission with all its fields, the stats, variables, routes and
    starting locations."""
    return sessions.world_catalogue()


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


class EngineContinueRequest(EnginePlayRequest, TrainingGame):
    generations: int = Field(default=1, ge=1, le=store.RUN_LIMITS["generation_limit"],
                             description="Further generations, continuing exactly where training stopped.")


class EngineRetrainRequest(EnginePlayRequest, TrainingOverrides, TrainingGame):
    """A retraining sets what it wants for itself; the rest is the recorded engine's saved defaults."""


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


def _evaluable_engine(engine_id):
    """404 for an engine the judge cannot play: neither recorded nor a finished run kept without the
    records (GET /v1/exports/engines lists those)."""
    if export._kept_engine(engine_id) is None:
        _known_engine(engine_id)


@api.get("/v1/records", tags=["Records"])
def records_info():
    """Where the records database is, whether recording is on, and why not (``reason``). When the
    folder is not writable by this process, recording is off and the records already there
    are still read (``readable``)."""
    enabled = records.enabled()
    reason = records.disabled_reason()
    return {"enabled": enabled, "reason": public_message(reason) if reason else None, "directory": str(records.directory()),
            "database": str(records.database_path()), "models": str(records.models_dir()),
            "readable": records.database_path().exists()}


@api.get("/v1/records/runs", tags=["Records"])
def list_recorded_runs(limit: int = 50, offset: int = 0, user_id: str | None = None):
    """The recorded runs, newest first, each with the user it is for (``user_id``: the studio's and
    earlier runs are ``test``); ``user_id`` lists one user's runs only."""
    return {"runs": _records(records.list_runs, limit, offset, user_id)}


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
    """The recorded engines, newest first, each with its name (None: unnamed) and label."""
    known = engine_meta.names()
    return {"engines": [engine_meta.annotate(row, known) for row in
                        _records(records.list_engines, run_id=run_id, source=source, limit=limit, offset=offset)]}


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


@api.post("/v1/engines/{engine_id}/play", response_model=SimulationResponse, tags=["Evolution and play"])
def play_finished_run(engine_id: str, body: EnginePlayRequest | None = None, origin: str = Depends(_origin)):
    """Play the world a finished run was trained on, with its storyteller (``latest``: the newest
    finished run; GET /v1/exports/engines lists them). Works without the records: finished runs
    are kept with the saves. Its world and tuning become the active ones, as when an engine is
    loaded. 409 before any run has finished."""
    try:
        return _game_response(sessions.play_finished_run(engine_id, origin=origin, **_player(body or EnginePlayRequest())))
    except HTTPException:
        raise
    except Exception as exc:
        _fail(exc)


@api.get("/v1/sessions/{session_id}/journal", tags=["Evolution and play"])
def get_session_journal(session_id: str):
    """A play session turn by turn, as a player follows it: every turn's choice, check, what happened,
    what it changed (health, coins, items, places, people) and the quest steps it completed
    ({journal: [...]}, oldest first; the play state's `turn_summary` is the last)."""
    return {"session_id": session_id, "journal": _session_call(session_id, lambda: sessions.session_journal(session_id))}


@api.get("/v1/sessions/{session_id}/transcript", tags=["Evolution and play"])
def get_session_transcript(session_id: str):
    """The story of a play session so far, as text."""
    return {"session_id": session_id,
            "transcript": _session_call(session_id, lambda: sessions.session_transcript(session_id))}


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
def continue_recorded_engine(engine_id: str, body: EngineContinueRequest, origin: str = Depends(_origin),
                             user_id: str = Depends(_run_user)):
    """Continue the engine's training exactly where it stopped; records the run and a new engine."""
    _known_engine(engine_id)
    try:
        return _game_response(sessions.continue_engine(engine_id, body.generations, origin=origin, user_id=user_id,
                                                       start_game=body.start_game, **_player(body)))
    except HTTPException:
        raise
    except Exception as exc:
        _fail(exc)


@api.post("/v1/records/engines/{engine_id}/retrain", response_model=SimulationResponse, tags=["Records"])
def retrain_recorded_engine(engine_id: str, body: EngineRetrainRequest, origin: str = Depends(_origin),
                            user_id: str = Depends(_run_user)):
    """A new run that starts from the engine's storyteller and memory, on its world."""
    _known_engine(engine_id)
    try:
        return _game_response(sessions.retrain_engine(
            engine_id, body.candidate_count, body.generations, body.simulation_steps, body.episodes,
            body.deterministic, origin=origin, user_id=user_id, seed=body.seed, start_game=body.start_game,
            **_player(body)))
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
    retired = sorted(set(body.settings) & set(store.RETIRED_APP_SETTINGS))
    unknown = sorted(set(body.settings) - set(store.APP_DEFAULTS) - set(retired))
    if unknown:
        raise HTTPException(status_code=400, detail="Unknown app parameters: " + ", ".join(unknown))
    try:
        store.save_app_settings({**store.load_app_settings(), **body.settings})
    except (ValueError, OSError) as exc:
        _fail(exc)
    note = (f" Ignored {', '.join(retired)}: a run's defaults are the saved engine defaults (evolution.*, "
            "crossover included: evolution.crossover_rate), and each run may set its own sizes." if retired else "")
    return {"message": "App parameters saved. Restart the studio and the API to apply them." + note,
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


# ---- the studio's forms: one action, one mission, an entry's details, the map, the rules ----------

class OneAction(BaseModel):
    action: dict[str, Any] = Field(description="One action: name, outcomes and the action's fields, as stored "
                                               "(GET .../actions/{index}); fields left out are not kept.")


@api.get("/v1/entities/{kind}/{name}/actions/{index}", tags=["World authoring"])
def get_one_action(kind: Literal["Locations", "NPCs", "Items", "Events"], name: str, index: int):
    """One action of an entry (``index``: its place in the list, from 0), as stored."""
    _known_entity(kind, name)
    try:
        return {"index": index, "action": content.load_entity_action(kind, name, index)}
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@api.put("/v1/entities/{kind}/{name}/actions/{index}", tags=["World authoring"])
def put_one_action(kind: Literal["Locations", "NPCs", "Items", "Events"], name: str, index: int, body: OneAction):
    """Replace one action (``index`` = the number of actions: add one at the end), checked with the
    others as a whole save is. Returns every action."""
    _known_entity(kind, name)
    try:
        return {"actions": content.save_entity_action(kind, name, index, body.action)}
    except Exception as exc:
        _fail(exc)


@api.delete("/v1/entities/{kind}/{name}/actions/{index}", tags=["World authoring"])
def delete_one_action(kind: Literal["Locations", "NPCs", "Items", "Events"], name: str, index: int,
                      expected_name: str | None = None):
    """Delete one action (the others are checked as a save checks them). ``expected_name``: the name the
    action at ``index`` must have, else 409 and nothing is deleted (the actions changed since they were
    read). ``undo``: what POST /v1/deletions/{token}/restore puts back."""
    _known_entity(kind, name)
    try:
        result = content.remove_entity_action(kind, name, index, expected=expected_name)
        return {"actions": result["actions"], "message": result["message"], "undo": result["undo"]}
    except Exception as exc:
        _fail(exc)


class EntityDetails(BaseModel):
    details: dict[str, Any] = Field(description="NPCs: known_locations, known_objects, article (the | a | an | "
                                                "none: a proper name | empty: automatic), dialogue [{text, enabled_if}]. "
                                                "Locations: distant_descriptions, entered_descriptions, interact_prompts, "
                                                "exit_locked. An empty value removes the field.")


@api.get("/v1/entities/{kind}/{name}/details", tags=["World authoring"])
def get_entity_details(kind: Literal["Locations", "NPCs", "Items", "Events"], name: str):
    """The fields of an entry the entry form does not show: an NPC's known places and items, how the
    story names them and what they say; a place's distant views, arrival texts, ways in and exits."""
    _known_entity(kind, name)
    return {"details": content.load_entity_details(kind, name), "fields": list(content.DETAIL_FIELDS.get(kind, ()))}


@api.put("/v1/entities/{kind}/{name}/details", tags=["World authoring"])
def put_entity_details(kind: Literal["Locations", "NPCs", "Items", "Events"], name: str, body: EntityDetails):
    _known_entity(kind, name)
    try:
        return {"details": content.save_entity_details(kind, name, body.details), "message": f"Saved `{name}`."}
    except Exception as exc:
        _fail(exc)


class OneMission(BaseModel):
    mission: dict[str, Any] = Field(description="title, description, goal or stages [{title, description, goal, "
                                                "rewards}], weight, enabled_if, rewards")
    rename: str | None = Field(default=None, description="A new key for it (letters, digits, _).")


@api.get("/v1/missions/{key}", tags=["World authoring"])
def get_one_mission(key: str):
    missions = content.load_missions()
    if key not in missions:
        raise HTTPException(status_code=404, detail=f"No mission `{key}`.")
    return {"key": key, "mission": missions[key]}


@api.put("/v1/missions/{key}", tags=["World authoring"])
def put_one_mission(key: str, body: OneMission):
    """Add or replace one mission (``rename``: give it a new key), checked with every other one."""
    try:
        missions = content.save_mission(key, body.mission, body.rename)
        return {"missions": missions, "message": f"Saved the mission `{body.rename or key}`."}
    except Exception as exc:
        _fail(exc)


@api.delete("/v1/missions/{key}", tags=["World authoring"])
def delete_one_mission(key: str):
    """Delete a mission (the others are checked as a save checks them). ``undo``: what
    POST /v1/deletions/{token}/restore puts back."""
    try:
        result = content.remove_mission(key)
        return {"missions": result["missions"], "message": result["message"], "undo": result["undo"]}
    except Exception as exc:
        _fail(exc)


@api.post("/v1/deletions/{token}/restore", tags=["World authoring"])
def restore_deletion(token: str):
    """Undo a deletion: ``token`` is the ``undo.token`` of a DELETE of an entry, an action or a mission
    (kept in this process, for its recent deletions). The record goes back whole, and only it (never a
    whole file): where it was when its list is as the deletion left it, else next to its old neighbour
    (``changed_since``, ``moved``). 404: no such deletion (undone already, or the API restarted); 409: its
    name is taken again, or its entry is gone (nothing is overwritten); 400: it would now be invalid."""
    try:
        return content.restore_deletion(token)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        _fail(exc)


class MapRoutes(BaseModel):
    routes: dict[str, list[str]] | None = Field(description="place: [the places it borders] (a route holds both "
                                                            "ways); null: every story draws its own map.")


class Borders(BaseModel):
    borders: list[str] = Field(description="Every place this one borders; routes change both ways.")


@api.get("/v1/map", tags=["World authoring"])
def get_map():
    """The world's map as data: {routes: {place: [borders]} or null, places: [every location]}."""
    return content.load_map()


@api.put("/v1/map", tags=["World authoring"])
def put_map(body: MapRoutes):
    try:
        found, message = content.save_map(body.routes)
        return {**found, "message": message}
    except Exception as exc:
        _fail(exc)


@api.put("/v1/map/{place}", tags=["World authoring"])
def put_borders(place: str, body: Borders):
    """The places ``place`` borders: a route added or removed here changes the other place too."""
    try:
        found, message = content.set_borders(place, body.borders)
        return {**found, "message": message}
    except Exception as exc:
        _fail(exc)


class BehaviourRules(BaseModel):
    default: dict[str, Any] = Field(default_factory=dict, description="What everyone (everywhere) does without a rule.")
    rules: list[dict[str, Any]] = Field(default_factory=list, description="[{tags: [...], <behaviour>: <value>}]")


@api.get("/v1/behaviours/rules", tags=["Engine logic"])
def get_behaviour_rules():
    """The rules that give people and places their behaviour by tags, as data: {npcs: {default, rules},
    locations: {default, rules}} (GET /v1/behaviours: the values, defaults and the tags of each)."""
    return store.load_behaviour_rules()


@api.put("/v1/behaviours/rules/{group}", tags=["Engine logic"])
def put_behaviour_rules(group: Literal["npcs", "locations"], body: BehaviourRules):
    """Replace the people's or the places' rules, checked as rules (400 says what is wrong)."""
    try:
        return {"message": store.save_behaviour_rules(group, body.model_dump()), **store.load_behaviour_rules()}
    except Exception as exc:
        _fail(exc)


# ---- with the language model: suggestions and a bigger world ------------------------------

class SuggestionRequest(BaseModel):
    kind: Literal["Locations", "NPCs", "Items", "Events"]
    name: str | None = Field(default=None, description="The entry (none: a new one).")
    field: str = Field(description="descriptions, goals, tags, distant_descriptions, entered_descriptions, "
                                   "interact_prompts or dialogue")


@api.post("/v1/suggestions", tags=["World authoring"])
def suggest_values(body: SuggestionRequest):
    """Gemma's suggestions for a text field of an entry, in the world's voice (the terminal editor's
    suggestions): {suggestions: [...]}. Nothing is saved. 503 without the language model."""
    if body.name:
        _known_entity(body.kind, body.name)
    try:
        return {"suggestions": content.suggest(body.kind, body.name, body.field), "field": body.field}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        _fail(exc)


@api.get("/v1/world/size", tags=["World authoring"])
def get_world_size():
    """How big the world is against the augmentation targets (augmentation.min_*): {have, want, enough}."""
    return content.world_size()


class WorldAdditions(BaseModel):
    locations: dict[str, dict[str, Any]] = Field(default_factory=dict)
    objects: list[dict[str, Any]] = Field(default_factory=list, description="Items, each with a name.")
    characters: list[dict[str, Any]] = Field(default_factory=list, description="NPCs, each with a name.")


@api.post("/v1/jobs/world-proposal", status_code=202, tags=["Jobs", "World authoring"])
def submit_world_proposal():
    """Gemma proposes new places, people and items for this world, in the background (poll GET
    /v1/jobs/{id}; the result is {locations, objects, characters}). Nothing is written: add the
    ones wanted with POST /v1/world/additions."""
    def work(progress):
        return content.propose_additions()
    return sessions.submit_job("world-proposal", work, "new places, people and items from the language model")


@api.post("/v1/world/additions", tags=["World authoring"])
def add_to_world(body: WorldAdditions):
    """Add new places, people and items (e.g. some of a proposal). Each must be new and the world
    is checked with them in place; nothing is written when it is invalid (400 says why)."""
    try:
        added = content.add_world_entries(body.model_dump())
        return {"added": added, "message": "Added " + (", ".join(added["locations"] + added["objects"]
                                                              + added["characters"]) or "nothing") + "."}
    except Exception as exc:
        _fail(exc)


# ---- jobs: long runs with progress -------------------------------------------------------

def _job_or_404(job_id):
    job = sessions.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@api.post("/v1/jobs/simulations", status_code=202, tags=["Jobs"])
def submit_simulation(body: SimulationRequest, origin: str = Depends(_origin), user_id: str = Depends(_run_user)):
    """Run an evolution in the background (as POST /v1/simulations); poll GET /v1/jobs/{id}
    for its progress per generation and, when finished, the playable game."""
    def work(progress):
        game = sessions.run_simulation(
            body.candidate_count, body.generations, body.simulation_steps, body.deterministic,
            profile=body.profile, episodes=body.episodes, warm_start=body.warm_start, seed=body.seed,
            stats_choice="defined" if body.stats is not None else body.stats_mode,
            stats_points=yaml.safe_dump(body.stats) if body.stats is not None else None,
            progress=progress, origin=origin, mission=body.mission, user_id=user_id, start_game=body.start_game,
        )
        return _game_response(game).model_dump()
    run = sessions.resolve_training(_overrides(body))["effective"]
    return sessions.submit_job("simulation", work,
                               f"{run['generations']} generation(s) of {run['population']} storytellers")


@api.post("/v1/jobs/engines/{engine_id}/continue", status_code=202, tags=["Jobs"])
def submit_continue(engine_id: str, body: EngineContinueRequest, origin: str = Depends(_origin),
                    user_id: str = Depends(_run_user)):
    """Continue a recorded engine's training in the background (as POST /v1/records/engines/{id}/continue)."""
    _known_engine(engine_id)
    def work(progress):
        return _game_response(sessions.continue_engine(engine_id, body.generations, origin=origin, progress=progress,
                                                       user_id=user_id, start_game=body.start_game,
                                                       **_player(body))).model_dump()
    return sessions.submit_job("continue", work, f"{body.generations} more generation(s) of engine {engine_id}")


@api.post("/v1/jobs/engines/{engine_id}/retrain", status_code=202, tags=["Jobs"])
def submit_retrain(engine_id: str, body: EngineRetrainRequest, origin: str = Depends(_origin),
                   user_id: str = Depends(_run_user)):
    """Retrain from a recorded engine in the background (as POST /v1/records/engines/{id}/retrain)."""
    _known_engine(engine_id)
    def work(progress):
        return _game_response(sessions.retrain_engine(
            engine_id, body.candidate_count, body.generations, body.simulation_steps, body.episodes,
            body.deterministic, origin=origin, progress=progress, user_id=user_id, seed=body.seed,
            start_game=body.start_game, **_player(body))).model_dump()
    return sessions.submit_job("retrain", work, f"{body.generations or 'the default'} generation(s) from engine "
                                                f"{engine_id}")


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


@api.get("/v1/judge/status", tags=["Evaluation"])
def judge_status():
    """Whether the language model judge (Gemma 4 through vLLM) can be used: {available, mode
    (server | embedded), model, server (its /v1 address in server mode), reason (why not),
    how (how to make it available)}. A server is asked which models it serves (3 s at most)."""
    from utils.llm_model_loader import status

    return status()


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
        _evaluable_engine(body.engine_id)

    def work(progress):
        result = evaluation.benchmark(body.stories, body.seed, body.engine_id, body.variants or None, body.pairwise,
                                      body.workers, progress=progress)
        if body.engine_id and not body.pairwise and not body.variants:  # the engine as it is: on its card
            engine_meta.record_evaluation(body.engine_id, "judge", result)
        return result
    kind = "pairwise A/B" if body.pairwise else "judge benchmark"
    return sessions.submit_job("judge-benchmark", work, f"{kind} of {body.stories} stories")


@api.post("/v1/jobs/calibrate-fitness", status_code=202, tags=["Jobs", "Evaluation"])
def submit_calibration(body: CalibrationRequest):
    """Fit the fitness to the judge in the background: play and judge `stories` stories, fit the
    judged engagement on the fitness terms and suggest a multiplier per term ({term_weights,
    terms, spearman, cv_spearman (how well it predicts stories it was not fitted on), alpha, n,
    yaml, applied}); `apply` saves them as fitness.term_weights."""
    if body.engine_id:
        _evaluable_engine(body.engine_id)

    def work(progress):
        result = evaluation.calibrate(body.stories, body.seed, body.engine_id, body.alpha, body.apply, body.workers,
                                      progress=progress)
        if body.engine_id:
            engine_meta.record_evaluation(body.engine_id, "calibration", result)
        return result
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


class RecordsImport(BaseModel):
    move: bool = Field(default=False, description="Move the files into the records folder (else copy them).")
    preview: bool = Field(default=True, description="Play a preview story of each new engine.")


@api.get("/v1/records/import", tags=["Records"])
def get_import_folder():
    """The server's folder of earlier engines and games (ANE_ARTIFACT_DIR, else artifacts/): {folder, files}."""
    return sessions.import_folder()


@api.post("/v1/jobs/records-import", status_code=202, tags=["Jobs", "Records"])
def submit_records_import(body: RecordsImport):
    """Import every engine and game in the server's import folder into the records, in the background
    (as `python main.py --import-artifacts`): poll GET /v1/jobs/{id} for the report."""
    found = sessions.import_folder()
    if not found["files"]:
        raise HTTPException(status_code=400, detail=f"There is nothing to import in {found['folder']}.")
    folder = found["folder"]

    def work(progress):
        return sessions.import_artifacts(folder, move=body.move, preview=body.preview,
                                         progress=lambda text: progress(0, 1, text))
    return sessions.submit_job("records-import", work, f"import the engines and games in {folder}")


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
