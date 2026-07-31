from utils.config_loader import config


ACCESS_ERROR_MARKERS = (
    "401",
    "403",
    "gated",
    "restricted",
    "not authorized",
    "unauthorized",
    "access token",
    "authentication",
    "permission",
)


def is_access_error(exc):
    text = str(exc).lower()
    return any(marker in text for marker in ACCESS_ERROR_MARKERS)


def candidate_model_names(primary_model):
    fallback_enabled = config.get("llm_fallback.enabled", True)
    fallback_model = config.get("llm_fallback.model_name", "Qwen/Qwen2.5-0.5B-Instruct")

    names = []
    if primary_model:
        names.append(primary_model)
    if fallback_enabled and fallback_model and fallback_model not in names:
        names.append(fallback_model)
    return names


def load_causal_lm_with_fallback(primary_model, *, purpose="LLM", prefer_quantized=True):
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("Transformers and torch are required for local LLM features.") from exc

    last_error = None
    local_files_only = config.get("llm_fallback.local_files_only", False)
    trust_remote_code = config.get("llm_fallback.trust_remote_code", False)

    for index, model_name in enumerate(candidate_model_names(primary_model)):
        is_fallback = index > 0
        if is_fallback:
            print(f"[{purpose}] Falling back to public model: {model_name}")
        else:
            print(f"[{purpose}] Loading model: {model_name}")

        try:
            tokenizer = AutoTokenizer.from_pretrained(
                model_name,
                local_files_only=local_files_only,
                trust_remote_code=trust_remote_code,
            )
        except Exception as exc:
            last_error = exc
            if is_access_error(exc) and not is_fallback:
                print(f"[{purpose}] Access denied for {model_name}; trying configured fallback.")
                continue
            print(f"[{purpose}] Tokenizer load failed for {model_name}: {exc}")
            continue

        load_attempts = []
        if prefer_quantized and torch.cuda.is_available():
            try:
                from transformers import BitsAndBytesConfig
                load_attempts.append({
                    "label": "4-bit GPU",
                    "kwargs": {
                        "quantization_config": BitsAndBytesConfig(
                            load_in_4bit=True,
                            bnb_4bit_compute_dtype=torch.float16,
                            bnb_4bit_quant_type="nf4",
                        ),
                        "device_map": "auto",
                    },
                })
            except Exception:
                pass

        if torch.cuda.is_available():
            load_attempts.append({
                "label": "GPU/auto",
                "kwargs": {
                    "torch_dtype": torch.float16,
                    "low_cpu_mem_usage": True,
                    "device_map": "auto",
                },
            })

        load_attempts.append({
            "label": "CPU",
            "kwargs": {
                "torch_dtype": torch.float32,
                "low_cpu_mem_usage": True,
                "device_map": {"": "cpu"},
            },
        })

        for attempt in load_attempts:
            try:
                print(f"[{purpose}] Attempting {attempt['label']} load...")
                model = AutoModelForCausalLM.from_pretrained(
                    model_name,
                    local_files_only=local_files_only,
                    trust_remote_code=trust_remote_code,
                    **attempt["kwargs"],
                )
                if tokenizer.pad_token_id is None:
                    tokenizer.pad_token = tokenizer.eos_token
                print(f"[{purpose}] Active model: {model_name}")
                return tokenizer, model, model_name
            except Exception as exc:
                last_error = exc
                if is_access_error(exc) and not is_fallback:
                    print(f"[{purpose}] Access denied for {model_name}; trying configured fallback.")
                    break
                print(f"[{purpose}] {attempt['label']} load failed for {model_name}: {exc}")

    raise RuntimeError(f"{purpose} model loading failed. Last error: {last_error}")
