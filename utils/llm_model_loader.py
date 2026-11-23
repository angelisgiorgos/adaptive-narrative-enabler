"""Language model runtimes backed by vLLM, one per role.

- ``judge`` (vllm.*, ANE_JUDGE_*): Gemma 4, shared by the judge and the editor's suggestions.
- ``augmentation`` (augmentation.llm.*, ANE_AUGMENT_*): Qwen3.5-4B, for world augmentation.

A runtime is shared per role and model, so a model is loaded once. Each supports an embedded vLLM
engine and an OpenAI-compatible vLLM server.

Taking turns on one GPU (llm_sharing, ``Turns``): the roles' models take turns, so both fit on one
GPU in one session. Before a model generates, the other is put to sleep (vLLM sleep level 1: its
weights to CPU memory, its GPU memory freed) once it has nothing running, and its own is woken; a
model that has stopped running for ``idle_seconds`` is put to sleep too. Off (ANE_LLM_SHARING=off, or
llm_sharing.enabled: false) when the models are on GPUs of their own (several sessions).
"""

import contextlib
import importlib.util
import json
import os
import threading
import time
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


# How to make the augmentation model available.
HOW_TO_ENABLE_AUGMENTATION = (
    "Start the augmentation model (Qwen3.5-4B on vLLM) and point the API at it: with Docker, "
    "`docker compose --profile augmenter up -d augmenter` (the API finds it at "
    "http://hamlet-ane-augmenter:8000/v1, set by ANE_AUGMENT_URL); without Docker, `vllm serve "
    "/data/angelisg/llm_models/Qwen3.5-4B --served-model-name Qwen/Qwen3.5-4B --dtype float16 --port 8001` and "
    "augmentation.llm.base_url http://127.0.0.1:8001/v1."
)

ROLES = ("judge", "augmentation")

AUGMENTATION_DEFAULTS = {  # augmentation.llm.* when hyperparameters.yaml leaves them out
    "mode": "server", "base_url": "http://127.0.0.1:8001/v1", "api_key": "local-vllm",
    "model_name": "Qwen/Qwen3.5-4B", "model_path": "/data/angelisg/llm_models/Qwen3.5-4B",
    "dtype": "float16", "gpu_memory_utilization": 0.6, "max_model_len": 8192,
    "limit_mm_per_prompt": {"image": 0, "video": 0}, "enable_thinking": False, "json_output": True,
}


def settings(role="judge"):
    """A role's language model settings. ``judge``: vllm.* (hyperparameters.yaml), with the
    environment over them: ANE_JUDGE_URL (an OpenAI-compatible vLLM server's /v1 address: server
    mode), ANE_JUDGE_API_KEY and ANE_JUDGE_MODEL. ``augmentation``: augmentation.llm.* (Qwen3.5-4B
    by default, served from a local folder: model_path), with ANE_AUGMENT_URL, ANE_AUGMENT_API_KEY,
    ANE_AUGMENT_MODEL and ANE_AUGMENT_MODEL_PATH over them. ``engine``: what an embedded engine is
    built with; ``chat_template_kwargs``: what each request passes to the chat template."""
    if role == "augmentation":
        own = {**AUGMENTATION_DEFAULTS, **(config.get("augmentation.llm") or {})}
        url = os.environ.get("ANE_AUGMENT_URL", "").strip()
        model_path = os.environ.get("ANE_AUGMENT_MODEL_PATH", "").strip() or own.get("model_path")
        return {
            "role": role,
            "mode": "server" if url else own["mode"],
            "base_url": (url or own["base_url"]).rstrip("/"),
            "api_key": os.environ.get("ANE_AUGMENT_API_KEY", "").strip() or own["api_key"],
            "model_name": os.environ.get("ANE_AUGMENT_MODEL", "").strip() or own["model_name"],
            "model_path": model_path,
            "engine": {key: own.get(key) for key in ("dtype", "gpu_memory_utilization", "max_model_len",
                                                      "limit_mm_per_prompt", "enforce_eager", "max_num_seqs")},
            "chat_template_kwargs": {"enable_thinking": bool(own.get("enable_thinking", False))},
            "json_output": bool(own.get("json_output", True)),
            "how": HOW_TO_ENABLE_AUGMENTATION,
            "names": ("augmentation.llm", "ANE_AUGMENT"),
        }
    url = os.environ.get("ANE_JUDGE_URL", "").strip()
    return {
        "role": "judge",
        "mode": "server" if url else config.get("vllm.mode", "embedded"),
        "base_url": (url or config.get("vllm.base_url", "http://127.0.0.1:8000/v1")).rstrip("/"),
        "api_key": os.environ.get("ANE_JUDGE_API_KEY", "").strip() or config.get("vllm.api_key", "local-vllm"),
        "model_name": os.environ.get("ANE_JUDGE_MODEL", "").strip()
        or config.get("vllm.model_name", "google/gemma-4-E4B-it"),
        "model_path": None,
        "engine": {},
        "chat_template_kwargs": {},
        "json_output": False,
        "how": HOW_TO_ENABLE,
        "names": ("vllm", "ANE_JUDGE"),
    }


SHARING_DEFAULTS = {"enabled": True, "idle_seconds": 5.0, "level": 1, "wait_seconds": 900.0}


def sharing():
    """llm_sharing.* (the roles' models taking turns on one GPU): {enabled, idle_seconds (how long a
    model that has stopped running stays awake), level (vLLM's sleep level: 1, weights to CPU memory),
    wait_seconds (the longest a model waits for the other to finish)}. ANE_LLM_SHARING=on|off
    overrides ``enabled``."""
    own = {**SHARING_DEFAULTS, **(config.get("llm_sharing") or {})}
    env = os.environ.get("ANE_LLM_SHARING", "").strip().lower()
    if env:
        own["enabled"] = env in {"1", "true", "yes", "on"}
    return own


def _root(base_url):
    """A server's address without its /v1 (where vLLM's /sleep, /wake_up and /is_sleeping are)."""
    base_url = base_url.rstrip("/")
    return base_url[:-3] if base_url.endswith("/v1") else base_url


def _post(url, api_key, timeout=120.0):
    request = urllib.request.Request(url, data=b"", method="POST", headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status


class Turns:
    """The roles' models taking turns on one GPU (llm_sharing): one awake at a time, the other asleep in
    CPU memory. State per role: "awake", "asleep", "always" (a server without sleep mode: never put to
    sleep) or None (not known yet)."""

    def __init__(self):
        self._cond = threading.Condition()
        self._active = {role: 0 for role in ROLES}
        self.state = {role: None for role in ROLES}
        self._timers = {}
        self._absent_until = {}  # a role whose server did not answer: not asked again before this time
        self.log = []  # (role, "sleep" | "wake", ok): what was done, the latest last (status, tests)

    def close(self):
        """Cancel the pending idle timers (a model then stays as it is): before this coordinator is
        replaced or the process stops, so that no timer acts on servers configured afterwards."""
        with self._cond:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()

    def _engines(self, role):
        with _RUNTIMES_LOCK:
            return [rt for rt in _RUNTIMES.values() if rt.role == role and rt._engine is not None]

    def _switch(self, role, wake):
        """Put a role's model to sleep or wake it: its server (POST /sleep?level=, /wake_up) or its
        embedded engines. A server without sleep mode (404) stays as it is ("always")."""
        active = settings(role)
        what = "wake" if wake else "sleep"
        if active["mode"] == "server":
            path = "/wake_up" if wake else f"/sleep?level={int(sharing()['level'])}"
            try:
                _post(_root(active["base_url"]) + path, active["api_key"])
                self.state[role] = "awake" if wake else "asleep"
                ok = True
            except urllib.error.HTTPError as exc:
                ok = False
                if exc.code in (404, 405):  # no sleep mode on that server (VLLM_SERVER_DEV_MODE, --enable-sleep-mode)
                    self.state[role] = "always"
            except (OSError, ValueError):  # not running: nothing to put to sleep, or to wake (asked again later)
                ok = False
                self.state[role] = None
                self._absent_until[role] = time.monotonic() + 60.0
            except Exception:  # noqa: BLE001 - anything else: left as it is; a request is never failed by it
                ok = False
                self.state[role] = None
        else:
            engines = self._engines(role)
            ok = bool(engines)
            for runtime in engines:
                if not hasattr(runtime._engine, "sleep"):  # an engine without sleep mode: always awake
                    self.state[role] = "always"
                    self.log.append((role, what, False))
                    return
                try:
                    (runtime._engine.wake_up() if wake else runtime._engine.sleep(level=int(sharing()["level"])))
                except Exception:  # noqa: BLE001 - a model that cannot sleep stays awake
                    ok = False
            self.state[role] = (("awake" if wake else "asleep") if ok else "always") if engines else None
        self.log.append((role, what, ok))

    def _sleep_when_idle(self, role):
        try:
            self._sleep_now_if_idle(role)
        except Exception as exc:  # noqa: BLE001 - a timer must not fail: the model stays awake
            print(f"[vLLM] Could not put the {role} model to sleep: {exc}")

    def _sleep_now_if_idle(self, role):
        with self._cond:
            self._timers.pop(role, None)
            if self._active[role] == 0 and self.state[role] in ("awake", None) and sharing()["enabled"]:
                self._switch(role, wake=False)
                self._cond.notify_all()

    @contextlib.contextmanager
    def turn(self, role):
        """While ``role``'s model generates: the other roles' models asleep (once nothing of theirs is
        running), its own awake; afterwards, asleep again once it has been idle for idle_seconds."""
        shared = sharing()
        if not shared["enabled"]:
            yield
            return
        others = [other for other in ROLES if other != role]
        deadline = time.monotonic() + float(shared["wait_seconds"])
        with self._cond:
            while any(self._active[other] for other in others):
                left = deadline - time.monotonic()
                if left <= 0:
                    raise RuntimeError(f"The GPU is busy with the {', '.join(others)} model for more than "
                                       f"{shared['wait_seconds']:.0f} s (llm_sharing.wait_seconds).")
                self._cond.wait(min(left, 1.0))
            timer = self._timers.pop(role, None)
            if timer:
                timer.cancel()
            for other in others:
                absent = self._absent_until.get(other, 0) > time.monotonic()
                if self.state[other] not in ("asleep", "always") and not absent:
                    timer = self._timers.pop(other, None)
                    if timer:
                        timer.cancel()
                    self._switch(other, wake=False)
            if self.state[role] not in ("awake", "always"):
                self._switch(role, wake=True)
            self._active[role] += 1
        try:
            yield
        finally:
            with self._cond:
                self._active[role] -= 1
                if self._active[role] == 0:
                    timer = threading.Timer(float(shared["idle_seconds"]), self._sleep_when_idle, args=(role,))
                    timer.daemon = True
                    self._timers[role] = timer
                    timer.start()
                self._cond.notify_all()


TURNS = Turns()


def status(timeout=3.0, role="judge"):
    """Whether a role's language model can be used from here: {available, mode, model, server,
    reason, how}; a server is asked which models it serves."""
    active = settings(role)
    section, env = active["names"]
    found = {"available": False, "mode": active["mode"], "model": active["model_name"],
             "server": active["base_url"] if active["mode"] == "server" else None, "reason": None, "how": active["how"]}
    if active["mode"] == "server":
        request = urllib.request.Request(f"{active['base_url']}/models",
                                         headers={"Authorization": f"Bearer {active['api_key']}"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                served = [model.get("id") for model in json.loads(response.read().decode("utf-8")).get("data") or []]
        except urllib.error.HTTPError as exc:  # it answers, but refuses (a wrong API key, ...)
            found["reason"] = (f"The vLLM server at {active['base_url']} refused the request (HTTP {exc.code}); "
                               f"check {section}.api_key or {env}_API_KEY.")
            return found
        except (OSError, ValueError) as exc:  # not running, unreachable, refused, not a vLLM server
            found["reason"] = f"No vLLM server answers at {active['base_url']} ({getattr(exc, 'reason', exc)})."
            return found
        if served and active["model_name"] not in served:
            found["reason"] = (f"The vLLM server at {active['base_url']} serves {', '.join(map(str, served))}, "
                               f"not {active['model_name']} ({section}.model_name or {env}_MODEL).")
            return found
        try:  # asleep (its weights in CPU memory: llm_sharing), or awake on the GPU; None: no sleep mode
            sleeping = urllib.request.Request(f"{_root(active['base_url'])}/is_sleeping",
                                              headers={"Authorization": f"Bearer {active['api_key']}"})
            with urllib.request.urlopen(sleeping, timeout=timeout) as response:
                found["sleeping"] = bool(json.loads(response.read().decode("utf-8")).get("is_sleeping"))
        except (OSError, ValueError):
            found["sleeping"] = None
    elif active["mode"] == "embedded":
        if importlib.util.find_spec("vllm") is None:
            found["reason"] = "vLLM is not installed here, so the model cannot run inside this process."
            return found
        if active["model_path"] and not os.path.isdir(active["model_path"]):
            found["reason"] = f"The model's folder {active['model_path']} is not there ({section}.model_path)."
            return found
    else:
        found["reason"] = f"{section}.mode must be either 'embedded' or 'server'."
        return found
    found["available"], found["how"] = True, None
    found.setdefault("sleeping", None)
    found["sharing"] = bool(sharing()["enabled"])
    return found


class VLLMRuntime:
    def __init__(self, model_name=None, role="judge"):
        active = settings(role)
        self.role = role
        self.model_name = model_name or active["model_name"]
        self.mode = active["mode"]
        self.chat_template_kwargs = dict(active["chat_template_kwargs"])
        self.json_output = bool(active.get("json_output"))  # replies held to valid JSON by vLLM's decoding
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

        active = settings(self.role)
        own = {key: value for key, value in active["engine"].items() if value is not None}
        engine_kwargs = {
            # A role served from a local folder (model_path) loads it from there, under its name.
            "model": active["model_path"] or self.model_name,
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
        if quantization and self.role == "judge":
            engine_kwargs["quantization"] = quantization
        if active["model_path"]:
            engine_kwargs["served_model_name"] = self.model_name
        if sharing()["enabled"]:  # so that it can be put to sleep while the other role's model runs
            engine_kwargs["enable_sleep_mode"] = True
        for key in ("dtype", "gpu_memory_utilization", "max_model_len", "enforce_eager", "max_num_seqs"):
            if key in own:
                engine_kwargs[key] = own[key]
        if own.get("limit_mm_per_prompt"):
            engine_kwargs["limit_mm_per_prompt"] = {str(k): int(v) for k, v in own["limit_mm_per_prompt"].items()}

        print(f"[vLLM] Initializing shared model: {self.model_name}")
        self._engine = LLM(**engine_kwargs)
        self._sampling_params_class = SamplingParams
        print(f"[vLLM] Active model: {self.model_name}")

    def _generate_embedded(self, prompt, max_new_tokens, temperature, **kwargs):
        self._ensure_embedded_engine()
        extra = {}
        if self.json_output:  # vLLM's structured output: the reply is valid JSON (augmentation.llm.json_output)
            try:
                from vllm.sampling_params import StructuredOutputsParams

                extra["structured_outputs"] = StructuredOutputsParams(json_object=True)
            except ImportError:  # an older vLLM (or a stand-in): plain decoding
                pass
        params = self._sampling_params_class(
            max_tokens=int(max_new_tokens),
            temperature=float(temperature),
            top_p=float(kwargs.get("top_p", 0.95)),
            **extra,
        )
        messages = [{"role": "user", "content": prompt}]

        with self._lock:
            if hasattr(self._engine, "chat"):
                outputs = self._engine.chat(
                    messages,
                    sampling_params=params,
                    use_tqdm=False,
                    **({"chat_template_kwargs": self.chat_template_kwargs} if self.chat_template_kwargs else {}),
                )
            else:
                outputs = self._engine.generate(
                    [prompt],
                    sampling_params=params,
                    use_tqdm=False,
                )
        return outputs[0].outputs[0].text

    def _generate_server(self, prompt, max_new_tokens, temperature, **kwargs):
        active = settings(self.role)
        base_url = active["base_url"]
        body = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": int(max_new_tokens),
            "temperature": float(temperature),
            "top_p": float(kwargs.get("top_p", 0.95)),
        }
        if self.chat_template_kwargs:  # e.g. Qwen3.5 without its thinking (augmentation.llm.enable_thinking)
            body["chat_template_kwargs"] = self.chat_template_kwargs
        if self.json_output:  # vLLM's structured output: the reply is valid JSON (augmentation.llm.json_output)
            body["response_format"] = {"type": "json_object"}
        request_body = json.dumps(body).encode("utf-8")
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
                + active["how"]
            ) from exc
        return payload["choices"][0]["message"]["content"]

    def generate(self, prompt, max_new_tokens=512, temperature=0.3, **kwargs):
        if self.mode not in ("server", "embedded"):
            raise ValueError(f"{settings(self.role)['names'][0]}.mode must be either 'embedded' or 'server'.")
        with TURNS.turn(self.role):  # the other role's model asleep, this one awake (llm_sharing)
            if self.mode == "server":
                return self._generate_server(prompt, max_new_tokens, temperature, **kwargs)
            return self._generate_embedded(prompt, max_new_tokens, temperature, **kwargs)


_RUNTIMES = {}
_RUNTIMES_LOCK = threading.Lock()


def get_llm_runtime(model_name=None, role="judge"):
    """The runtime of a role (``judge`` or ``augmentation``) and model, shared: loaded once."""
    active = settings(role)
    active_model = model_name or active["model_name"]
    key = (role, active["mode"], active["base_url"], active_model)
    with _RUNTIMES_LOCK:
        if key not in _RUNTIMES:
            _RUNTIMES[key] = VLLMRuntime(model_name=active_model, role=role)
        return _RUNTIMES[key]


def embedded_engine_active():
    """Whether this process holds an embedded vLLM engine (and so a GPU context): worker
    processes must then be spawned, not forked (main._new_process_pool)."""
    with _RUNTIMES_LOCK:
        return any(runtime._engine is not None for runtime in _RUNTIMES.values())
