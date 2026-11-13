"""Smoke-test a running deployment: the API and the studio built from the Dockerfile.

Checks that the API is healthy, serves the world (missions, failure lint), runs a tiny
evolution that starts a game, and plays one turn of it; and that the studio is up.
Only the standard library is used, so it runs on any CI runner or host.

    python .github/scripts/docker_smoke_test.py --api http://127.0.0.1:8004 --ui http://127.0.0.1:8003
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def request(method, url, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"content-type": "application/json"})
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
    parser.add_argument("--api", default="http://127.0.0.1:8004")
    parser.add_argument("--ui", default="http://127.0.0.1:8003", help="the studio's URL ('' to skip it)")
    parser.add_argument("--wait", type=int, default=300, help="seconds to wait for each service")
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
    check("the game offers choices", bool(game["actions"]) and not game["ended"], text)

    status, text = request("POST", f"{api}/v1/sessions/{game['session_id']}/actions", {"action": game["actions"][0]})
    check("one turn is played", status == 200 and json.loads(text)["last_turn"], text)

    if args.ui:
        ui = args.ui.rstrip("/")
        wait_for(f"{ui}/gradio_api/startup-events", args.wait)
        status, text = request("GET", f"{ui}/")
        check("studio page", status == 200 and "gradio" in text.lower(), text)
    print("Smoke test passed.")


if __name__ == "__main__":
    sys.exit(main())
