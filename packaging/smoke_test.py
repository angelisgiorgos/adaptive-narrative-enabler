"""Drive an ANE AI like a UNP would, through the whole protocol, and fail loudly if anything is off.

    python packaging/smoke_test.py --folder <runtime + package folder> [--command "python run.py"]

Without --command it starts the standalone program in the folder (ane-ai / ane-ai.exe). Every
request goes to 127.0.0.1 only. Checks: ready line and file; token required; health; the world
catalogue; a new session with the UNP's player state; player update; sync; turns; a refused
and an accepted world patch; save; session state; shutdown (exit code 0, the session saved);
relaunch; load (exact) and continue; load from parameters only; cancel; stop when stdin closes;
stop when the parent process ends.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

TOKEN = "smoke-test-token"


class Runtime:
    def __init__(self, command, folder, data_dir, parent_pid=None):
        self.ready_file = Path(tempfile.mkdtemp()) / "ready.json"
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["ANE_RUNTIME_TOKEN"] = TOKEN
        args = command + ["--data-dir", str(data_dir), "serve", "--port", "0", "--ready-file", str(self.ready_file),
                          "--exit-on-stdin-eof"]
        if parent_pid:
            args += ["--parent-pid", str(parent_pid)]
        self.process = subprocess.Popen(args, cwd=folder, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding="utf-8")
        line = self.process.stdout.readline()
        if not line:
            raise SystemExit(f"No ready line; stderr: {self.process.stderr.read()[-2000:]}")
        self.info = json.loads(line)
        check("ready line", self.info.get("event") == "ready" and self.info.get("protocol") == 1, line)
        check("ready file", json.loads(self.ready_file.read_text())["port"] == self.info["port"])

    def call(self, path, body=None, method=None, token=TOKEN):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.info["url"] + path, data=data, headers=headers,
                                         method=method or ("POST" if data is not None else "GET"))
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def wait(self, timeout=60):
        try:
            out, err = self.process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            raise SystemExit("The runtime did not stop.")
        return self.process.returncode, out, err


def check(label, condition, detail=""):
    print(f"{'ok  ' if condition else 'FAIL'} {label}", flush=True)
    if not condition:
        raise SystemExit(f"{label} failed: {str(detail)[:2000]}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--folder", required=True)
    parser.add_argument("--command", help='how to start it, e.g. "python run.py" (default: the standalone program)')
    args = parser.parse_args(argv)
    folder = Path(args.folder).resolve()
    if args.command:
        command = shlex.split(args.command)
    else:
        executable = folder / ("ane-ai.exe" if os.name == "nt" else "ane-ai")
        check("standalone program", executable.exists(), executable)
        command = [str(executable)]
    data_dir = Path(tempfile.mkdtemp(prefix="ane-smoke-data-"))

    runtime = Runtime(command, folder, data_dir, parent_pid=os.getpid())
    check("token required", runtime.call("/health", token=None)[0] == 401)
    check("health", runtime.call("/health")[1].get("status") == "ok")
    status, world = runtime.call("/world")
    check("world catalogue", status == 200 and world["locations"] and world["npcs"] and world["missions"], world)
    npc = world["npcs"][0]["name"]
    check("one entity", runtime.call(f"/world/npcs/{urllib.request.quote(npc)}")[1].get("name") == npc)
    item = world["items"][0]["name"]
    status, game = runtime.call("/games", {"profile": "smoke", "seed": 3, "player": {"coins": 40, "add_items": [item]}})
    check("new session with the UNP's player", status == 201 and game["coins"] == 40 and item in game["inventory"], game)
    gid = game["game_id"]
    check("player update", runtime.call(f"/games/{gid}/player", {"health": 3}, method="PATCH")[1].get("health") == 3)
    check("sync", runtime.call(f"/games/{gid}/sync", {"type": "coins_changed", "amount": -4})[1].get("coins") == 36)
    for _ in range(3):
        status, view = runtime.call(f"/games/{gid}/choose", {"action": 0})
    check("turns", status == 200 and view["turn_number"] == 3, view)
    bad = runtime.call("/world/patch", {"upsert": {"items": [{"name": "Lamp", "actions": [
        {"name": "Go", "outcomes": [{"desc": "Off.", "move_to": "Nowhere-at-all"}]}]}]}})
    check("refused world patch", bad[0] == 422 and bad[1].get("errors"), bad)
    good = runtime.call("/world/patch", {"upsert": {"items": [{"name": "Lamp", "associated_tags": ["light"]}]}})
    check("accepted world patch", good[0] == 200 and good[1]["revision"] == 1, good)
    status, saved = runtime.call(f"/games/{gid}/save", {})
    check("save game", status == 200 and saved["format"] == "ane.savegame/1" and saved["snapshot"], status)
    check("session state", runtime.call(f"/games/{gid}/state")[1].get("format") == "ane.session_state/1")
    status, stopped = runtime.call("/shutdown", {"save": True})
    check("shutdown", status == 200 and [s["game_id"] for s in stopped["saved"]] == [gid], stopped)
    code, out, err = runtime.wait()
    check("exit code 0", code == 0 and '"event": "stopped"' in out, (code, out[-500:], err[-1500:]))
    suspended = data_dir / "suspended" / f"{gid}.json"
    check("session saved on shutdown", suspended.exists())

    runtime = Runtime(command, folder, data_dir, parent_pid=os.getpid())
    doc = json.loads(suspended.read_text(encoding="utf-8"))
    status, loaded = runtime.call("/games", {"savegame": doc})
    check("load game (exact)", status == 201 and loaded["restored"] == "exact" and loaded["turn_number"] == 3, loaded)
    check("continue", runtime.call(f"/games/{loaded['game_id']}/choose", {"action": 0})[1].get("turn_number") == 4)
    params_only = {k: v for k, v in doc.items() if k not in ("snapshot", "checksum")}
    params_only["parameters"] = {**doc["parameters"], "coins": 77}
    status, rebuilt = runtime.call("/games", {"savegame": params_only})
    check("load game (parameters)", status == 201 and rebuilt["restored"] == "parameters" and rebuilt["coins"] == 77,
          rebuilt)
    check("cancel", runtime.call(f"/games/{rebuilt['game_id']}/cancel", {})[1]["status"]["outcome"] == "suspended")
    check("world patch kept", any(i["name"] == "Lamp" for i in runtime.call("/world")[1]["items"]))
    runtime.process.stdin.close()
    runtime.process.stdin = None  # closed: communicate() must not flush it
    code, _, err = runtime.wait()
    check("stops when stdin closes", code == 0, err[-1500:])

    # Stops when its parent (the UNP) ends: a parent process that starts it and exits.
    parent = subprocess.Popen([sys.executable, "-c", (
        "import os, subprocess, sys, time, json; "
        f"p = subprocess.Popen({command!r} + ['--data-dir', {str(data_dir)!r}, 'serve', '--port', '0', "
        "'--parent-pid', str(os.getpid())], cwd=" + repr(str(folder)) + ", stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, "
        "env={**os.environ, 'ANE_RUNTIME_TOKEN': 'x'}); "
        "print(json.loads(p.stdout.readline())['pid'], flush=True)")], stdout=subprocess.PIPE, text=True)
    child = int(parent.stdout.readline())
    parent.wait(timeout=60)
    deadline = time.time() + 30
    alive = True
    while time.time() < deadline and alive:
        alive = _alive(child)
        time.sleep(0.5)
    check("stops when the UNP process ends", not alive)
    print("Smoke test passed.")


def _alive(pid):
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie (ended, not reaped yet) counts as ended
        with open(f"/proc/{pid}/stat") as stream:
            return stream.read().split()[2] != "Z"
    except OSError:
        return True


if __name__ == "__main__":
    main()
