"""Keep tests out of the real records database (backend/records.py) and the
repository's artifacts folder.

Imported by every test module that runs simulations, loads engines, plays or
exports: recording and the API's downloads then go to temporary folders for the
whole test run.
"""
import os
import tempfile

os.environ.setdefault("ANE_RECORDS_DIR", tempfile.mkdtemp(prefix="ane-records-tests-"))
os.environ.setdefault("ANE_ARTIFACT_DIR", tempfile.mkdtemp(prefix="ane-artifacts-tests-"))
# Saved play sessions (backend/persistence.py): a temporary folder too, never the real saves.
os.environ.setdefault("ANE_SESSIONS_DIR", tempfile.mkdtemp(prefix="ane-sessions-tests-"))

# The studio (app.py) only talks HTTP to an API (studio_client.py). In tests it talks to the API
# served in this process instead (the same app, through FastAPI's TestClient), unless a test sets
# ANE_API_URL or gives the client a URL.
import studio_client  # noqa: E402


def _in_process_api(headers):
    from fastapi.testclient import TestClient

    import api

    return TestClient(api.api, raise_server_exceptions=False, headers=headers)


studio_client.use_test_client(_in_process_api)
