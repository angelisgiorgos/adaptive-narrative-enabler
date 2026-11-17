"""The life of an ANE AI process started by a UNP: the ready handshake, and stopping when told to,
when the UNP goes away, or on a signal. Standard library only, Windows, Linux and macOS.

- ``emit(event)``: one JSON line on stdout (stdout carries only these: ready, stopped, error).
- ``write_ready_file(path, info)``: the same JSON, written atomically (temporary file + replace).
- ``watch_parent(pid, stop)``: call ``stop`` when the process ``pid`` (the UNP) ends.
- ``watch_stdin(stop)``: call ``stop`` when stdin closes (the UNP kept a pipe open, then went away).
- ``install_signals(stop)``: SIGTERM / SIGINT / SIGHUP (SIGBREAK on Windows) call ``stop``.

Events the ANE AI sends to the UNP (``ane.event/1``), POSTed as JSON to the UNP's callback
(``--unp-url``, ``ANE_UNP_URL`` or ``session_report.url``):

    {"format": "ane.event/1", "event": "session.started" | "session.cancelled" | "engine.stopping",
     "at": "...", "engine": {...}, "session_id": "...", "data": {...}}

A finished session's report (``ane.session_report/1``) is sent by core_engine/progression.py as
it always was. Without a callback nothing is sent; the UNP can still ask for everything.
"""
from __future__ import annotations

import datetime
import json
import os
import signal
import sys
import threading
import time


def emit(event):
    """Write one lifecycle event as a JSON line on stdout (nothing when the UNP has gone and the
    pipe with it)."""
    try:
        sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.flush()
    except (BrokenPipeError, OSError, ValueError):
        try:  # stdout leads nowhere now: point it at the null device (no "broken pipe" at exit)
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass


def write_ready_file(path, info):
    path = os.path.abspath(path)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(info, stream)
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def _pid_alive_posix(pid):
    try:
        os.kill(pid, 0)  # POSIX only: on Windows os.kill terminates the process
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def watch_parent(pid, stop, interval=0.5):
    """Call ``stop(reason)`` once the process ``pid`` has ended."""
    pid = int(pid)

    def windows():
        import ctypes

        synchronize, infinite = 0x00100000, 0xFFFFFFFF
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if handle:
            kernel32.WaitForSingleObject(handle, infinite)
            kernel32.CloseHandle(handle)
        stop("the UNP process ended")

    def posix():
        while True:
            if not _pid_alive_posix(pid) or (os.getppid() != pid and os.getppid() == 1):
                stop("the UNP process ended")
                return
            time.sleep(interval)

    thread = threading.Thread(target=windows if os.name == "nt" else posix, name="ane-parent-watch", daemon=True)
    thread.start()
    return thread


def watch_stdin(stop):
    """Call ``stop(reason)`` when stdin reaches its end (the UNP closed the pipe or ended)."""
    def run():
        stream = sys.stdin.buffer if hasattr(sys.stdin, "buffer") else sys.stdin
        try:
            while stream.read(1024):
                pass
        except (OSError, ValueError):
            pass
        stop("stdin closed")

    thread = threading.Thread(target=run, name="ane-stdin-watch", daemon=True)
    thread.start()
    return thread


def install_signals(stop):
    """Stop gracefully on SIGTERM, SIGINT, SIGHUP (and SIGBREAK on Windows). The handler runs in
    the main thread, so the stop runs in its own thread."""
    def handler(signum, _frame):
        threading.Thread(target=stop, args=(f"signal {signum}",), name="ane-signal-stop", daemon=True).start()

    for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is not None:
            try:
                signal.signal(number, handler)
            except (OSError, ValueError, RuntimeError):
                pass


# ---- events sent to the UNP's callback -------------------------------------------------------

EVENT_FORMAT = "ane.event/1"
_THREADS = []
_LOCK = threading.Lock()


def send_event(event, data=None, session_id=None, engine=None, wait=False):
    from core_engine import progression

    if not progression.settings()["url"]:
        return None
    payload = {"format": EVENT_FORMAT, "event": event,
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
