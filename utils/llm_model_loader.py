"""Shared Gemma runtime backed by vLLM.

The singleton prevents the evaluator, augmenter, and editor suggester from loading
separate model copies. It supports an embedded vLLM engine and an optional
OpenAI-compatible vLLM server.
"""

import json
import threading
import urllib.error
import urllib.request

from utils.config_loader import config


class VLLMRuntime:
    def __init__(self, model_name=None):
        self.model_name = model_name or config.get(
            "vllm.model_name",
            "google/gemma-4-E2B-it",
        )
        self.mode = config.get("vllm.mode", "embedded")
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
                "vLLM is required for LLM features. Install the environment or "
                "set vllm.mode to 'server' and start a vLLM server."
            ) from exc

        engine_kwargs = {
            "model": self.model_name,
            "dtype": config.get("vllm.dtype", "auto"),
            "trust_remote_code": config.get("vllm.trust_remote_code", False),
            "gpu_memory_utilization": float(
                config.get("vllm.gpu_memory_utilization", 0.55)
            ),
            "max_model_len": int(config.get("vllm.max_model_len", 4096)),
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
        base_url = config.get("vllm.base_url", "http://127.0.0.1:8000/v1").rstrip("/")
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
                "Authorization": f"Bearer {config.get('vllm.api_key', 'local-vllm')}",
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
                f"Could not reach the vLLM server at {base_url}: {exc}"
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
    active_model = model_name or config.get(
        "vllm.model_name",
        "google/gemma-4-E2B-it",
    )
    key = (config.get("vllm.mode", "embedded"), active_model)
    with _RUNTIMES_LOCK:
        if key not in _RUNTIMES:
            _RUNTIMES[key] = VLLMRuntime(model_name=active_model)
        return _RUNTIMES[key]
