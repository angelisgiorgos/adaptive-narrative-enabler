"""Smoke-test a running deployment: the API and the studio built from the Dockerfile.

Checks that the API is healthy, serves the world (missions, failure lint), runs a tiny
evolution that starts a game, and plays one turn of it; and that the studio is up.
Only the standard library is used, so it runs on any CI runner or host.

    python packaging/docker_smoke_test.py --api http://127.0.0.1:8005 --ui http://127.0.0.1:8003
"""
import argparse
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile


def request(method, url, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    # A backend calling the API says whose runs it starts (X-ANE-User: an opaque id).
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"content-type": "application/json", "X-ANE-User": "docker-smoke-test"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            text = response.read().decode()
            return response.status, text
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


def wait_for(url, seconds):
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        try:
            status, _ = request("GET", url, timeout=10)
            if status == 200:
                return
            last = f"HTTP {status}"
        except (urllib.error.URLError, OSError) as exc:
            last = str(exc)
        time.sleep(3)
    raise SystemExit(f"{url} did not answer within {seconds}s ({last})")


def check(label, condition, detail=""):
    print(f"{'ok  ' if condition else 'FAIL'} {label}")
    if not condition:
        raise SystemExit(f"{label} failed: {detail[:2000]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--api", default="http://127.0.0.1:8005")
    parser.add_argument("--ui", default="http://127.0.0.1:8003", help="the studio's URL ('' to skip it)")
    parser.add_argument("--wait", type=int, default=300, help="seconds to wait for each service")
    parser.add_argument("--export-platform", help="also export an ANE AI for this platform (e.g. linux-x64)...")
    parser.add_argument("--export-to", help="...and unzip it here (execute bits kept)")
    args = parser.parse_args()
    api = args.api.rstrip("/")

    wait_for(f"{api}/healthz", args.wait)
    status, text = request("GET", f"{api}/healthz")
    check("API health", status == 200 and json.loads(text).get("status") == "ok", text)

    status, text = request("GET", f"{api}/v1/missions/playable")
    check("the world's missions", status == 200 and json.loads(text)["missions"], text)

    status, text = request("GET", f"{api}/v1/lint/failures")
    check("failure lint", status == 200 and isinstance(json.loads(text)["dead_ends"], list), text)

    status, text = request("GET", f"{api}/v1/reference")
    check("authoring reference (NPC roles, judge criteria)",
          status == 200 and json.loads(text)["npc_roles"] and json.loads(text)["judge_criteria"], text)

    status, text = request("GET", f"{api}/v1/entities/NPCs/Merchant/yaml")
    check("an entry with every field", status == 200 and "name: Merchant" in json.loads(text)["yaml"], text)

    status, text = request("GET", f"{api}/v1/documents/map")
    check("the world's map", status == 200 and "routes" in json.loads(text)["yaml"], text)

    status, text = request("GET", f"{api}/v1/fitness/term-weights")
    check("fitness term weights", status == 200 and isinstance(json.loads(text)["weights"], dict), text)

    # A tiny evolution: it trains, records and starts a game with the best storyteller.
    status, text = request("POST", f"{api}/v1/simulations", {
        "candidate_count": 2, "generations": 1, "simulation_steps": 5, "episodes": 1,
        "deterministic": True, "warm_start": False,
    }, timeout=900)
    check("a short evolution starts a game", status == 200, text)
    game = json.loads(text)
    status, text = request("GET", f"{api}/v1/records/runs?user_id=docker-smoke-test")
    check("the run is the caller's user's", status == 200 and (
        not json.loads(text)["runs"] or json.loads(text)["runs"][0]["user_id"] == "docker-smoke-test"), text)
    check("the game offers choices", bool(game["actions"]) and not game["ended"], text)

    status, text = request("POST", f"{api}/v1/sessions/{game['session_id']}/actions", {"action": game["actions"][0]})
    check("one turn is played", status == 200 and json.loads(text)["last_turn"], text)

    status, text = request("GET", f"{api}/v1/exports/platforms")
    check("export platforms", status == 200 and json.loads(text)["platforms"][0]["platform"] == "python", text)
    if args.export_platform:
        available = {p["platform"]: p for p in json.loads(text)["platforms"]}
        check(f"the {args.export_platform} runtime is in the image",
              available.get(args.export_platform, {}).get("available"), text)
        status, text = request("POST", f"{api}/v1/exports", {"source": "memory", "platform": args.export_platform})
        check("no ANE AI from a storyteller alone (a complete engine only)", status == 409, text)
        status, text = request("GET", f"{api}/v1/exports/engines")
        check("the run's engine is a complete engine", status == 200 and json.loads(text)["engines"], text)
        status, text = request("POST", f"{api}/v1/exports", {"source": "run", "platform": args.export_platform,
                                                            "name": "ane-ai-smoke"}, timeout=300)
        check(f"export an ANE AI for {args.export_platform}", status == 200, text)
        with urllib.request.urlopen(api + json.loads(text)["download_url"], timeout=300) as response:
            archive = zipfile.ZipFile(io.BytesIO(response.read()))
        for info in archive.infolist():
            path = archive.extract(info, args.export_to)
            mode = info.external_attr >> 16
            if mode and os.name != "nt":
                os.chmod(path, mode & 0o777)
        check("the export holds the standalone program",
              any(name.endswith("/ane-ai") or name.endswith("/ane-ai.exe") for name in archive.namelist()))

    if args.ui:
        ui = args.ui.rstrip("/")
        wait_for(f"{ui}/gradio_api/startup-events", args.wait)
        status, text = request("GET", f"{ui}/")
        check("studio page", status == 200 and "gradio" in text.lower(), text)
    print("Smoke test passed.")


if __name__ == "__main__":
    sys.exit(main())
