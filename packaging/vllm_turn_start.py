"""Start a vLLM server that takes turns on its GPU with another (compose.yaml: the judge and the augmenter).

    python3 vllm_turn_start.py [vllm serve arguments...]

Before loading its model, it puts the other servers named in ANE_LLM_PEERS (their addresses, comma
separated, e.g. http://hamlet-ane-judge:8000) to sleep, so that their GPU memory is free while this one
loads: vLLM sleep level 1 keeps a sleeping model's weights in CPU memory and frees its GPU memory. The
API wakes whichever model it needs (utils/llm_model_loader.Turns). ANE_LLM_SHARING=off (the models on
GPUs of their own) leaves the peers awake. Then it becomes `vllm serve` with the arguments given.

Started together, both would load at once and one would run out of memory. ANE_LLM_WAIT_FOR_PEERS
(seconds; set on one of the two only) makes this one wait first until a peer that exists (its name
resolves: its container is up) is serving: the peer loads first, is then put to sleep, and this one
loads. A peer that does not exist is not waited for.
"""
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def wait_for_peer(peer, seconds, step=5.0):
    """Wait until ``peer`` serves (GET /health), at most ``seconds``: "serving", "absent" (its name does
    not resolve: not started), or "not serving" (still loading or failing when the time is up)."""
    host = urllib.parse.urlsplit(peer).hostname
    deadline = time.monotonic() + seconds
    while True:
        try:
            socket.getaddrinfo(host, None)
        except OSError:
            return "absent"
        try:
            with urllib.request.urlopen(f"{peer}/health", timeout=10):
                return "serving"
        except (OSError, urllib.error.URLError):
            if time.monotonic() >= deadline:
                return "not serving"
        time.sleep(step)


def sleep_peers(peers, api_key, level=1, timeout=120.0):
    """Ask each peer to sleep; a peer not running (or without sleep mode) is left as it is. Returns
    {peer: "asleep" | why not}."""
    done = {}
    for peer in [p.strip().rstrip("/") for p in peers if p.strip()]:
        if peer.endswith("/v1"):
            peer = peer[:-3]
        wait = float(os.environ.get("ANE_LLM_WAIT_FOR_PEERS", "0") or 0)
        if wait > 0:
            found = wait_for_peer(peer, wait)
            print(f"[turns] {peer}: {found}", flush=True)
            if found == "absent":
                done[peer] = "not started: nothing to put to sleep"
                continue
        request = urllib.request.Request(f"{peer}/sleep?level={int(level)}", data=b"", method="POST",
                                         headers={"Authorization": f"Bearer {api_key}"})
        try:
            with urllib.request.urlopen(request, timeout=timeout):
                done[peer] = "asleep"
        except Exception as exc:  # noqa: BLE001 - not running yet, or no sleep mode: nothing to free
            done[peer] = f"not put to sleep ({getattr(exc, 'reason', exc)})"
    return done


def main(argv):
    if os.environ.get("ANE_LLM_SHARING", "on").strip().lower() not in {"0", "false", "no", "off"}:
        peers = os.environ.get("ANE_LLM_PEERS", "").split(",")
        for peer, said in sleep_peers(peers, os.environ.get("ANE_LLM_PEER_API_KEY", "local-vllm")).items():
            print(f"[turns] {peer}: {said}", flush=True)
    os.execvp("vllm", ["vllm", "serve", *argv])


if __name__ == "__main__":
    main(sys.argv[1:])
