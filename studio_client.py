"""The studio's connection to the REST API (api.py): HTTP, and nothing else.

The studio (app.py) does everything through this client. It never imports the
API's code or serves the API itself, so it and every other client (Unity, a web
frontend, scripts) share one engine, one set of sessions, runs and records.

Where the API is:

* ``ANE_API_URL`` (e.g. ``http://hamlet-ane-api:8005`` inside Compose, or the
  public deployment ``http://ane.hamlet.iti.gr``);
* not set or empty: ``DEFAULT_API_URL`` (``http://127.0.0.1:8005``, the API run
  locally with ``python -m uvicorn api:app --port 8005``).

Every request says it comes from the studio (``X-ANE-Client: studio``), so runs and
stories are recorded with the studio as their origin. Errors arrive as ``APIError``
with the API's message. Files the API offers for download are fetched into a
temporary folder, since the studio's file widgets need local paths.

Tests replace the HTTP connection with ``use_test_client`` (an httpx-compatible
client such as FastAPI's TestClient); a deployment never does.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

CLIENT_HEADER = {"X-ANE-Client": "studio"}
DEFAULT_API_URL = "http://127.0.0.1:8005"  # the API run locally (Compose: the api service on 8005)
PUBLIC_API_URL = "http://ane.hamlet.iti.gr"  # the public deployment (documentation at /docs)

_TEST_CLIENT_FACTORY = None


def use_test_client(factory):
    """Tests only: ``factory(headers)`` returns the client a StudioAPI without an explicit URL (and with
    ANE_API_URL unset) uses instead of HTTP to DEFAULT_API_URL. None restores HTTP."""
    global _TEST_CLIENT_FACTORY
    _TEST_CLIENT_FACTORY = factory


def api_url(environ=None):
    """The API the studio uses: ANE_API_URL, or DEFAULT_API_URL when it is unset or empty."""
    environ = os.environ if environ is None else environ
    return (environ.get("ANE_API_URL") or "").strip().rstrip("/") or DEFAULT_API_URL


class APIError(Exception):
    """A request the API refused or could not serve; the message is the API's."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class JobCancelledError(APIError):
    """A job that was cancelled (POST /v1/jobs/{id}/cancel): what it had done is in ``partial``."""

    def __init__(self, job):
        partial = job.get("partial") or {}
        super().__init__(f"The job was cancelled. {partial.get('note') or ''}".strip(), None)
        self.job = job


class StudioAPI:
    def __init__(self, base_url=None, timeout=900):
        explicit = base_url is not None or (os.environ.get("ANE_API_URL") or "").strip()
        if not explicit and _TEST_CLIENT_FACTORY is not None:
            self.mode = "test"
            self.base_url = "test"
            self._client = _TEST_CLIENT_FACTORY(dict(CLIENT_HEADER))
        else:
            import httpx

            self.mode = "http"
            self.base_url = (base_url.strip().rstrip("/") if base_url is not None else "") or api_url()
            self._client = httpx.Client(base_url=self.base_url, timeout=timeout, headers=CLIENT_HEADER)
        self._downloads = Path(tempfile.mkdtemp(prefix="ane-studio-"))
        atexit.register(shutil.rmtree, self._downloads, ignore_errors=True)

    def close(self):
        self._client.close()

    # ---- requests --------------------------------------------------------------------------

    def _send(self, method, path, **kwargs):
        """The response of a request; APIError when it fails or the API refuses it."""
        try:
            response = self._client.request(method, path, **kwargs)
        except Exception as exc:  # connection refused, timeout, ...
            raise APIError(f"The API at {self.base_url} could not be reached: {exc}") from exc
        if response.status_code >= 400:
            raise APIError(_error_message(response), response.status_code)
        return response

    def request(self, method, path, **kwargs):
        """The JSON a request returns (or the response, for anything else)."""
        response = self._send(method, path, **kwargs)
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response

    def get(self, path, **params):
        return self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def put(self, path, body=None, **params):
        return self.request("PUT", path, json=body, params={k: v for k, v in params.items() if v is not None})

    def patch(self, path, body=None, **params):
        return self.request("PATCH", path, json=body, params={k: v for k, v in params.items() if v is not None})

    def post(self, path, body=None, files=None, **params):
        params = {k: v for k, v in params.items() if v is not None}
        if files is not None:
            return self.request("POST", path, files=files, params=params)
        return self.request("POST", path, json=body, params=params)

    def delete(self, path, **params):
        return self.request("DELETE", path, params={k: v for k, v in params.items() if v is not None})

    def upload(self, path, files, **params):
        """POST local files: ``files`` maps a form field to a local path (None: left out)."""
        handles = {field: open(local, "rb") for field, local in files.items() if local}
        try:
            return self.post(path, files={field: (Path(handle.name).name, handle) for field, handle in handles.items()},
                             **params)
        finally:
            for handle in handles.values():
                handle.close()

    def download(self, url, filename=None):
        """Fetch a file the API offers (a path such as /v1/artifacts/<id>) into a local file."""
        if not url:
            return None
        response = self._send("GET", url)
        name = filename or _filename(response) or Path(url.split("?")[0]).name or "download"
        target = Path(tempfile.mkdtemp(dir=self._downloads)) / name  # the API's file name, never overwritten
        target.write_bytes(response.content)
        return str(target)

    def wait(self, job, on_progress=None, interval=0.5, patience=60):
        """Poll a job (POST /v1/jobs/...) until it ends: its result when finished; APIError when it failed
        (or is no longer known: 404); JobCancelledError when it was cancelled (cancel_requested goes on
        until it stops). A lost connection to the API is tried again for ``patience`` seconds."""
        lost = None
        while True:
            try:
                job = self.get(f"/v1/jobs/{job['id']}")
                lost = None
            except APIError as exc:
                if exc.status is not None:  # the API answered: e.g. 404, the job is gone
                    raise
                lost = lost or time.monotonic()
                if time.monotonic() - lost > patience:
                    raise
                time.sleep(max(interval, 1.0))
                continue
            if on_progress is not None and job.get("progress"):
                on_progress(job["progress"])
            if job["status"] == "finished":
                return job["result"]
            if job["status"] == "failed":
                raise APIError(job["error"] or "The job failed.")
            if job["status"] == "cancelled":
                raise JobCancelledError(job)
            time.sleep(interval)


def _error_message(response):
    try:
        detail = response.json().get("detail")
    except ValueError:
        return f"{response.status_code}: {response.text[:500]}"
    if isinstance(detail, list):  # request validation errors
        return "; ".join(f"{'.'.join(str(p) for p in item.get('loc', [])[1:])}: {item.get('msg')}" for item in detail)
    return str(detail)


def _filename(response):
    match = re.search(r'filename="?([^";]+)"?', response.headers.get("content-disposition", ""))
    return match.group(1) if match else None
