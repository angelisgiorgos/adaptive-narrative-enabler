"""Start a vLLM server that takes turns on its GPU with another (compose.yaml: the judge and the augmenter).

    python3 vllm_turn_start.py [vllm serve arguments...]

Before loading its model, it puts the other servers named in ANE_LLM_PEERS (their addresses, comma
separated, e.g. http://hamlet-ane-judge:8000) to sleep, so that their GPU memory is free while this one
loads: vLLM sleep level 1 keeps a sleeping model's weights in CPU memory and frees its GPU memory. The
API wakes whichever model it needs (utils/llm_model_loader.Turns). ANE_LLM_SHARING=off (the models on
GPUs of their own) leaves the peers awake. Then it becomes `vllm serve` with the arguments given.
"""
import os
import sys
import urllib.request


def sleep_peers(peers, api_key, level=1, timeout=120.0):
    """Ask each peer to sleep; a peer not running (or without sleep mode) is left as it is. Returns
    {peer: "asleep" | why not}."""
    done = {}
    for peer in [p.strip().rstrip("/") for p in peers if p.strip()]:
        if peer.endswith("/v1"):
            peer = peer[:-3]
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
