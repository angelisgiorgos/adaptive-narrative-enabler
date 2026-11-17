"""Events the ANE AI sends to the UNP (``ane.event/1``), POSTed as JSON to the UNP's callback
(``--unp-url``, ``ANE_UNP_URL`` or ``session_report.url``):

    {"format": "ane.event/1", "event": "session.started" | "session.cancelled" | "engine.stopping",
     "at": "...", "engine": {...}, "session_id": "...", "data": {...}}

A finished session's report (``ane.session_report/1``) is sent by core_engine/progression.py as
it always was. Without a callback nothing is sent; the UNP can still ask for everything.
"""
from __future__ import annotations

import datetime
import threading

FORMAT = "ane.event/1"
_THREADS = []
_LOCK = threading.Lock()


def send_event(event, data=None, session_id=None, engine=None, wait=False):
    from core_engine import progression

    if not progression.settings()["url"]:
        return None
    payload = {"format": FORMAT, "event": event,
               "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
               "engine": engine, "session_id": session_id, "data": data}
    if wait:
        return progression.send(payload)
    thread = threading.Thread(target=progression.send, args=(payload,), name=f"unp-event-{event}", daemon=True)
    with _LOCK:
        _THREADS[:] = [t for t in _THREADS if t.is_alive()] + [thread]
    thread.start()
    return thread


def wait_for_deliveries(timeout=10.0):
    """Wait for events and session reports still on their way (before the process exits)."""
    deadline = datetime.datetime.now().timestamp() + timeout
    for thread in list(_THREADS) + [t for t in threading.enumerate() if t.name.startswith("unp-report-")]:
        remaining = deadline - datetime.datetime.now().timestamp()
        if remaining > 0 and thread.is_alive():
            thread.join(remaining)
