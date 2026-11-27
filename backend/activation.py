"""When a saved setting takes effect: the activation information of the settings forms and their saves.

Two kinds of settings:

- **The configuration** (config/*.yaml: hyperparameters, weights, engine logic, story rules, messages,
  variables, stats, training defaults; an uploaded configuration). A save writes the file and makes the
  files the active configuration of the API's process at once (config.clear_override: an uploaded
  configuration, or a trained engine's world and tuning, in use until then is set aside, and its trained
  tag vectors with it). What starts afterwards reads it: new games, training runs, evaluations, and jobs
  still queued (a job reads the configuration when it starts, not when it was submitted). Nothing running
  changes: every game, training run and evaluation runs with its own frozen copy of the configuration
  (core_engine/context.py, an EngineContext taken when it started), so a game in progress or a running
  job keeps the configuration it started with until it ends. A game with a trained engine runs with that
  engine's own saved world, tuning and tag vectors, in its own context from the start; starting one
  changes nothing process-wide. Making an engine's world the studio's defaults (what the editors show and
  what starts next) is a separate, explicit operation (POST /v1/engines/{id}/activate, or ``activate`` on
  a play request), which never reaches a session already going either.
- **The app parameters** (config/ui_settings.yml). The API reads its own when it starts, the studio its
  own when it starts (its launch settings also from ANE_UI_HOST, ANE_UI_PORT and ANE_UI_SHARE, which
  take precedence over the saved values): a save changes nothing that is running, and the saved value
  and the running value differ until the restart.
"""
from __future__ import annotations

# The app parameters: who reads each (and when: when it starts) and what it is for.
API, STUDIO = "API", "studio"
APP_PARAMETERS = {
    "max_web_sessions": ((API,), "how many games the API keeps (the oldest is dropped)"),
    "max_simulation_work": ((API,), "the most simulated turns a training run may ask for"),
    "max_config_upload_bytes": ((API,), "the largest configuration upload"),
    "max_engine_upload_bytes": ((API,), "the largest engine upload"),
    "candidate_limit": ((API, STUDIO), "the largest population (the API refuses more; the studio's slider)"),
    "generation_limit": ((API, STUDIO), "the most generations (the API refuses more; the studio's slider)"),
    "simulation_step_limit": ((API, STUDIO), "the most steps per story (the API refuses more; the studio's slider)"),
    "queue_concurrency": ((STUDIO,), "how many requests of one kind the studio runs at once"),
    "queue_max_size": ((STUDIO,), "how many requests may wait in the studio's queue"),
    "server_host": ((STUDIO,), "the address the studio listens on"),
    "server_port": ((STUDIO,), "the port the studio listens on"),
    "share": ((STUDIO,), "a public Gradio share link for the studio"),
}
# The studio's launch settings that its own environment sets instead of the saved value (app.py's launch;
# only the studio can see its environment, so it says so itself).
ENVIRONMENT = {"server_host": "ANE_UI_HOST", "server_port": "ANE_UI_PORT", "share": "ANE_UI_SHARE"}


def readers(key):
    return APP_PARAMETERS[key][0]


def takes_effect(key):
    """When a saved app parameter takes effect: a restart of the API, of the studio, or of both."""
    return "a restart of " + " and of ".join(f"the {who}" for who in readers(key))


CONFIG_NOTE = (
    "**When it takes effect:** at once for what starts from now on (new games, training runs, evaluations, "
    "and jobs still queued: a job reads the configuration when it starts). Nothing running changes: a game in "
    "progress or a running job keeps the configuration it started with (its own frozen copy) until it ends. A "
    "game with a trained engine uses that engine's own saved world and tuning instead.")


def config_saved(what, file, override_was_active=False):
    """The save confirmation of a configuration setting: what was saved where, and when it takes effect."""
    set_aside = (" An uploaded configuration (or a trained engine's) that was in use is set aside: the saved "
                 "files are the active configuration again." if override_was_active else "")
    return (f"Saved {what} to `{file}`. In use now for what starts from here: new games, training runs and "
            "evaluations, and jobs still queued (they read it when they start). A game in progress or a job "
            "already running keeps the configuration it started with (its own frozen copy) until it ends. A game "
            f"with a trained engine uses that engine's own saved world and tuning instead.{set_aside}")


def override_active():
    """Whether an uploaded configuration, or a trained engine's, is the active configuration now."""
    from utils.config_loader import config

    return getattr(config, "_override", None) is not None


def app_parameters_state(saved, running_api):
    """For each app parameter: {key, purpose, saved, readers, takes_effect, running_api (the API's value
    now, or None when the API does not read it), environment (the studio's variable that can set it
    instead, or None), pending (whether the API runs another value than the saved one)}.
    ``running_api``: the values the API's process uses (store.APP_SETTINGS, read when it started)."""
    rows = []
    for key, (who, purpose) in APP_PARAMETERS.items():
        running = running_api.get(key) if API in who else None
        rows.append({"key": key, "purpose": purpose, "saved": saved.get(key), "readers": list(who),
                     "takes_effect": takes_effect(key), "running_api": running,
                     "environment": ENVIRONMENT.get(key),
                     "pending": API in who and running != saved.get(key)})
    return rows


def app_saved_message(before_running, saved):
    """The API's save confirmation of the app parameters: nothing running changes; what a restart of
    the API would change (the studio says what its own restart would)."""
    api_changes = [k for k, (who, _) in APP_PARAMETERS.items() if API in who and before_running.get(k) != saved.get(k)]
    message = ("Saved to `config/ui_settings.yml`. Nothing that is running changes: the API and the studio read "
               "these parameters when they start, so games in progress, submitted jobs and an open studio go on "
               "with the values they started with.")
    if api_changes:
        message += " A restart of the API will use: " + ", ".join(
            f"{k} {before_running.get(k)!r} → {saved.get(k)!r}" for k in api_changes) + "."
    else:
        message += " The API already runs every value it reads."
    return message
