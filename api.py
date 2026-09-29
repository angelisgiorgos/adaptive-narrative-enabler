"""REST API for Adaptive Narrative Enabler.

Swagger is available at ``/docs``. Keep a single Uvicorn worker because runtime
configuration overrides and play sessions are intentionally held in process.
"""

from __future__ import annotations

import os
import secrets
import tempfile
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

import app as studio
import ui_authoring


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


class ActionRequest(BaseModel):
    action: str = Field(min_length=1)


class WeightUpdate(BaseModel):
    values: dict[str, float]


class YAMLDocument(BaseModel):
    yaml: str


class Messages(BaseModel):
    messages: dict[str, str | list[str]] = Field(
        description="Message key -> text (or list of texts used in turn). Keys left out use the default."
    )


class ActionsSource(BaseModel):
    source: str = Field(min_length=1, description="Complete core_engine/actions.py Python source.")
    revision: str = Field(pattern=r"^[0-9a-f]{64}$", description="Revision returned when the source was loaded.")


class ActionsSourceSaved(BaseModel):
    revision: str
    message: str
    restart_required: bool = True


class PlayState(BaseModel):
    session_id: str
    scene: str
    actions: list[str]
    last_turn: str = ""
    ended: bool
    event: str | None = Field(default=None, description="Title of the active event, if any.")
    mission: str | None = Field(default=None, description="Title of the story's mission, if any.")
    won: bool = Field(default=False, description="True once the mission goal has been achieved.")


class SimulationResponse(PlayState):
    report: str
    config_download_url: str
    engine_download_url: str


ARTIFACTS: dict[str, Path] = {}
ARTIFACT_DIR = Path(os.getenv("ANE_ARTIFACT_DIR", str(studio.PROJECT_ROOT / "artifacts")))

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
        allow_methods=["GET", "PUT", "POST"],
        allow_headers=["Content-Type", "Authorization"],
    )


def _fail(exc: Exception):
    message = getattr(exc, "message", None) or str(exc)
    raise HTTPException(status_code=400, detail=message) from exc


def _choices(component: Any) -> list[str]:
    choices = getattr(component, "choices", None)
    if choices is None and isinstance(component, dict):
        choices = component.get("choices", [])
    return [str(item[1] if isinstance(item, (tuple, list)) and len(item) == 2 else item) for item in (choices or [])]


def _settings() -> Settings:
    values = studio.load_settings()
    return Settings(
        pop_size=values[0], generations=values[1], episodes_per_genome=values[2],
        max_steps=values[3], initial_health=values[4], initial_coins=values[5],
        max_actions=values[6], deterministic=values[7], seed=values[8], max_health=values[9],
    )


def _play_state(token: str, last_turn: str = "") -> PlayState:
    with studio.SESSION_LOCK:
        state = studio.SESSIONS.get(token)
        if state is None:
            raise HTTPException(status_code=404, detail="Play session not found.")
        scene, actions = studio._scene(state)
        return PlayState(
            session_id=token, scene=scene, actions=actions,
            last_turn=last_turn, ended=bool(state.ended),
            event=state.active_event.title if state.active_event else None,
            mission=state.mission.title if state.mission else None,
            won=bool(state.won),
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
    return {"status": "ok", "sessions": len(studio.SESSIONS)}


@api.get("/v1/config", tags=["Configuration"])
def get_config():
    return {"using_override": studio.config.using_override, "config": studio.config.as_dict()}


@api.post("/v1/config/upload", tags=["Configuration"])
def upload_config(file: UploadFile = File(...)):
    path = _save_upload(file, {".yaml", ".yml"}, studio.MAX_CONFIG_UPLOAD_BYTES)
    try:
        studio.upload_config(str(path))
        return {"message": "Configuration validated and activated.", "settings": _settings()}
    except Exception as exc:
        _fail(exc)
    finally:
        path.unlink(missing_ok=True)


@api.post("/v1/config/reset", tags=["Configuration"])
def reset_config():
    studio.use_builtin_config()
    return {"message": "Built-in configuration restored.", "settings": _settings()}


@api.get("/v1/settings", response_model=Settings, tags=["Configuration"])
def get_settings():
    return _settings()


@api.put("/v1/settings", tags=["Configuration"])
def put_settings(body: Settings):
    try:
        message = studio.save_settings(
            body.pop_size, body.generations, body.episodes_per_genome, body.max_steps,
            body.initial_health, body.initial_coins, body.max_actions,
            body.deterministic, body.seed, body.max_health,
        )
        return {"message": message, "settings": _settings()}
    except Exception as exc:
        _fail(exc)


@api.get("/v1/messages", tags=["World authoring"])
def get_messages():
    """Every story message, its default, and the placeholders it may use."""
    from core_engine.narration import DEFAULT_MESSAGES, allowed_fields
    return {
        "messages": studio.effective_messages(),
        "defaults": DEFAULT_MESSAGES,
        "placeholders": {key: sorted(allowed_fields(key)) for key in DEFAULT_MESSAGES},
    }


@api.put("/v1/messages", tags=["World authoring"])
def put_messages(body: Messages):
    try:
        message = studio.save_messages(body.messages)
        return {"message": message, "messages": studio.effective_messages()}
    except Exception as exc:
        _fail(exc)


@api.get("/v1/hyperparameters", tags=["Configuration"])
def get_hyperparameters():
    return {"yaml": studio.load_hyperparameters_yaml()}


@api.put("/v1/hyperparameters", tags=["Configuration"])
def put_hyperparameters(body: YAMLDocument):
    try:
        result = studio.save_all_hyperparameters(body.yaml)
        return {"message": result[-1], "yaml": result[-2], "settings": _settings()}
    except Exception as exc:
        _fail(exc)


_authoring_bearer = HTTPBearer(auto_error=False)


def _require_action_author(credential: HTTPAuthorizationCredentials | None = Depends(_authoring_bearer)):
    token = os.getenv("ANE_AUTHORING_TOKEN", "")
    if not token.strip():
        raise HTTPException(status_code=503, detail="Action authoring is disabled. Configure ANE_AUTHORING_TOKEN to enable it.")
    if credential is None or not secrets.compare_digest(credential.credentials.encode(), token.encode()):
        raise HTTPException(status_code=401, detail="A valid authoring bearer token is required.", headers={"WWW-Authenticate": "Bearer"})


@api.get("/v1/actions", response_model=ActionsSource, tags=["Action authoring"],
         dependencies=[Depends(_require_action_author)])
def get_actions_source():
    """Read the current action logic and its revision from disk."""
    source, revision = ui_authoring.load_actions_source()
    return ActionsSource(source=source, revision=revision)


@api.put("/v1/actions", response_model=ActionsSourceSaved, tags=["Action authoring"],
         dependencies=[Depends(_require_action_author)])
def put_actions_source(body: ActionsSource):
    """Validate and save action logic, backing up the previous source.

    Requires ANE_AUTHORING_TOKEN as a bearer token. Restart engine processes to apply
    saved code. A stale revision returns 409; invalid Python or missing classes returns 400.
    """
    try:
        revision = ui_authoring.save_actions_source(body.source, body.revision)
    except ui_authoring.ActionsRevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (SyntaxError, ValueError) as exc:
        _fail(exc)
    return ActionsSourceSaved(
        revision=revision,
        message="Saved actions.py; previous version is actions.py.bak. Restart engine processes to apply. Syntax checks do not verify runtime behavior.",
    )


@api.get("/v1/weights", tags=["Configuration"])
def get_weights():
    return {"values": dict(studio._weight_entries())}


@api.put("/v1/weights", tags=["Configuration"])
def put_weights(body: WeightUpdate):
    known = dict(studio._weight_entries())
    unknown = sorted(set(body.values) - set(known))
    if unknown:
        raise HTTPException(status_code=400, detail="Unknown weight paths: " + ", ".join(unknown))
    merged = {**known, **body.values}
    paths = list(known)
    try:
        message = studio.save_weights(paths, *(merged[path] for path in paths))
        return {"message": message, "values": dict(studio._weight_entries())}
    except Exception as exc:
        _fail(exc)


@api.get("/v1/entities/{kind}", tags=["World authoring"])
def list_entities(kind: Literal["Locations", "NPCs", "Items", "Events"]):
    names, _ = studio._entity_name_values(kind)
    return {"kind": kind, "entities": [get_entity(kind, name) for name in names]}


@api.get("/v1/entities/{kind}/{name}", response_model=Entity, tags=["World authoring"])
def get_entity(kind: Literal["Locations", "NPCs", "Items", "Events"], name: str):
    names, _ = studio._entity_name_values(kind)
    if name not in names:
        raise HTTPException(status_code=404, detail="Entity not found.")
    entity_name, tags, descriptions, goals, unique, encounter_event, encounter_repeat = studio.load_entity(kind, name)
    return Entity(
        name=entity_name, tags=list(tags), descriptions=studio._lines(descriptions),
        goals=studio._lines(goals), unique=unique,
        encounter_event=encounter_event, encounter_repeat=encounter_repeat,
    )


@api.put("/v1/entities/{kind}/{name}", response_model=Entity, tags=["World authoring"])
def put_entity(kind: Literal["Locations", "NPCs", "Items", "Events"], name: str, body: Entity):
    try:
        studio.save_entity(
            kind, name, body.name, body.tags,
            "\n".join(body.descriptions), "\n".join(body.goals), body.unique,
            body.encounter_event, body.encounter_repeat,
        )
        return get_entity(kind, body.name)
    except Exception as exc:
        _fail(exc)


@api.post("/v1/simulations", response_model=SimulationResponse, tags=["Evolution and play"])
def simulate(body: SimulationRequest):
    try:
        token, report, scene, actions_component, last_turn, config_path, engine_path = studio.run_simulation(
            body.candidate_count, body.generations, body.simulation_steps, body.deterministic,
            progress=_DirectProgress(),
        )
        state = _play_state(token, last_turn)
        return SimulationResponse(
            **state.model_dump(), report=report,
            config_download_url=_register_artifact(config_path),
            engine_download_url=_register_artifact(engine_path),
        )
    except HTTPException:
        raise
    except Exception as exc:
        _fail(exc)


@api.post("/v1/engines/load", response_model=SimulationResponse, tags=["Evolution and play"])
def load_engine(file: UploadFile = File(...)):
    path = _save_upload(file, {".yaml", ".yml", ".zip"}, studio.MAX_ENGINE_UPLOAD_BYTES)
    try:
        token, report, scene, actions_component, last_turn, config_path, engine_path = studio.load_best_engine(str(path))
        state = _play_state(token, last_turn)
        return SimulationResponse(
            **state.model_dump(), report=report,
            config_download_url=_register_artifact(config_path),
            engine_download_url=_register_artifact(engine_path),
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
        _, _, turn = studio.play_action(session_id, body.action)
        return _play_state(session_id, turn)
    except Exception as exc:
        _fail(exc)


@api.get("/v1/artifacts/{artifact_id}", tags=["Artifacts"])
def download_artifact(artifact_id: str):
    path = ARTIFACTS.get(artifact_id)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found.")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


class _DirectProgress:
    """Progress adapter for API runs outside a Gradio event context."""

    @staticmethod
    def tqdm(iterable, **_kwargs):
        return iterable


app = api
