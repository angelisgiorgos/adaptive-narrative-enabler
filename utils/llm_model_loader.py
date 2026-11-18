"""Shared Gemma runtime backed by vLLM.

The singleton prevents the evaluator, augmenter, and editor suggester from loading
separate model copies. It supports an embedded vLLM engine and an optional
OpenAI-compatible vLLM server.
"""

import importlib.util
import json
import os
import threading
import urllib.error
import urllib.request

from utils.config_loader import config


# How to make the judge available, for messages that say it is not.
HOW_TO_ENABLE = (
    "Start a vLLM server with the judge model and point the API at it: with Docker, "
    "`docker compose --profile judge up -d judge` (the API finds it at http://hamlet-ane-judge:8000/v1, "
    "set by ANE_JUDGE_URL); otherwise set ANE_JUDGE_URL to the server's /v1 address, or vllm.mode: server "
    "and vllm.base_url in 5 · Engine logic."
)


def settings():
    """The language model's settings: vllm.* (hyperparameters.yaml), with the environment over
    them: ANE_JUDGE_URL (an OpenAI-compatible vLLM server's /v1 address: server mode),
    ANE_JUDGE_API_KEY and ANE_JUDGE_MODEL."""
    url = os.environ.get("ANE_JUDGE_URL", "").strip()
    return {
        "mode": "server" if url else config.get("vllm.mode", "embedded"),
        "base_url": (url or config.get("vllm.base_url", "http://127.0.0.1:8000/v1")).rstrip("/"),
        "api_key": os.environ.get("ANE_JUDGE_API_KEY", "").strip() or config.get("vllm.api_key", "local-vllm"),
        "model_name": os.environ.get("ANE_JUDGE_MODEL", "").strip()
        or config.get("vllm.model_name", "google/gemma-4-E4B-it"),
    }


def status(timeout=3.0):
    """Whether the language model can be used from here: {available, mode, model, server,
    reason, how}; a server is asked which models it serves."""
    active = settings()
    found = {"available": False, "mode": active["mode"], "model": active["model_name"],
             "server": active["base_url"] if active["mode"] == "server" else None, "reason": None, "how": HOW_TO_ENABLE}
    if active["mode"] == "server":
        request = urllib.request.Request(f"{active['base_url']}/models",
                                         headers={"Authorization": f"Bearer {active['api_key']}"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                served = [model.get("id") for model in json.loads(response.read().decode("utf-8")).get("data") or []]
        except urllib.error.HTTPError as exc:  # it answers, but refuses (a wrong API key, ...)
            found["reason"] = (f"The vLLM server at {active['base_url']} refused the request (HTTP {exc.code}); "
                               "check vllm.api_key or ANE_JUDGE_API_KEY.")
            return found
        except (OSError, ValueError) as exc:  # not running, unreachable, refused, not a vLLM server
            found["reason"] = f"No vLLM server answers at {active['base_url']} ({getattr(exc, 'reason', exc)})."
            return found
        if served and active["model_name"] not in served:
            found["reason"] = (f"The vLLM server at {active['base_url']} serves {', '.join(map(str, served))}, "
                               f"not {active['model_name']} (vllm.model_name or ANE_JUDGE_MODEL).")
            return found
    elif active["mode"] == "embedded":
        if importlib.util.find_spec("vllm") is None:
            found["reason"] = "vLLM is not installed here, so the model cannot run inside this process."
            return found
    else:
        found["reason"] = "vllm.mode must be either 'embedded' or 'server'."
        return found
    found["available"], found["how"] = True, None
    return found


class VLLMRuntime:
    def __init__(self, model_name=None):
        active = settings()
        self.model_name = model_name or active["model_name"]
        self.mode = active["mode"]
        self._engine = None
        self._sampling_params_class = None
        self._lock = threading.Lock()

    def _ensure_embedded_engine(self):
        if self._engine is not None:
            return

        try:
            from vllm import LLM, SamplingParams
        except ImportError as exc:
            raise RuntimeError(
                "vLLM is required for LLM features and is not installed here. " + HOW_TO_ENABLE
            ) from exc

        engine_kwargs = {
            "model": self.model_name,
            "dtype": config.get("vllm.dtype", "auto"),
            "trust_remote_code": config.get("vllm.trust_remote_code", False),
            "gpu_memory_utilization": float(
                config.get("vllm.gpu_memory_utilization", 0.55)
            ),
            "max_model_len": int(config.get("vllm.max_model_len", 8192)),
            "max_num_seqs": int(config.get("vllm.max_num_seqs", 1)),
            "enforce_eager": bool(config.get("vllm.enforce_eager", True)),
            "enable_prefix_caching": bool(
                config.get("vllm.enable_prefix_caching", True)
            ),
        }
        cpu_offload_gb = float(config.get("vllm.cpu_offload_gb", 0) or 0)
        if cpu_offload_gb > 0:
            engine_kwargs["cpu_offload_gb"] = cpu_offload_gb
        limit_mm = config.get("vllm.limit_mm_per_prompt", {})
        if limit_mm:
            engine_kwargs["limit_mm_per_prompt"] = {
                str(modality): int(limit)
                for modality, limit in limit_mm.items()
            }
        quantization = config.get("vllm.quantization")
        if quantization:
            engine_kwargs["quantization"] = quantization

        print(f"[vLLM] Initializing shared model: {self.model_name}")
        self._engine = LLM(**engine_kwargs)
        self._sampling_params_class = SamplingParams
        print(f"[vLLM] Active model: {self.model_name}")

    def _generate_embedded(self, prompt, max_new_tokens, temperature, **kwargs):
        self._ensure_embedded_engine()
        params = self._sampling_params_class(
            max_tokens=int(max_new_tokens),
            temperature=float(temperature),
            top_p=float(kwargs.get("top_p", 0.95)),
        )
        messages = [{"role": "user", "content": prompt}]

        with self._lock:
            if hasattr(self._engine, "chat"):
                outputs = self._engine.chat(
                    messages,
                    sampling_params=params,
                    use_tqdm=False,
                )
            else:
                outputs = self._engine.generate(
                    [prompt],
                    sampling_params=params,
                    use_tqdm=False,
                )
        return outputs[0].outputs[0].text

    def _generate_server(self, prompt, max_new_tokens, temperature, **kwargs):
        active = settings()
        base_url = active["base_url"]
        request_body = json.dumps({
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": int(max_new_tokens),
            "temperature": float(temperature),
            "top_p": float(kwargs.get("top_p", 0.95)),
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=request_body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {active['api_key']}",
            },
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=float(config.get("vllm.timeout_seconds", 180)),
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(
                f"Could not reach the vLLM server at {base_url} ({getattr(exc, 'reason', exc)}). "
                + HOW_TO_ENABLE
            ) from exc
        return payload["choices"][0]["message"]["content"]

    def generate(self, prompt, max_new_tokens=512, temperature=0.3, **kwargs):
        if self.mode == "server":
            return self._generate_server(
                prompt,
                max_new_tokens,
                temperature,
                **kwargs,
            )
        if self.mode != "embedded":
            raise ValueError("vllm.mode must be either 'embedded' or 'server'.")
        return self._generate_embedded(
            prompt,
            max_new_tokens,
            temperature,
            **kwargs,
        )


_RUNTIMES = {}
_RUNTIMES_LOCK = threading.Lock()


def get_llm_runtime(model_name=None):
    active = settings()
    active_model = model_name or active["model_name"]
    key = (active["mode"], active_model)
    with _RUNTIMES_LOCK:
        if key not in _RUNTIMES:
            _RUNTIMES[key] = VLLMRuntime(model_name=active_model)
        return _RUNTIMES[key]


def embedded_engine_active():
    """Whether this process holds an embedded vLLM engine (and so a GPU context): worker
    processes must then be spawned, not forked (main._new_process_pool)."""
    with _RUNTIMES_LOCK:
        return any(runtime._engine is not None for runtime in _RUNTIMES.values())
