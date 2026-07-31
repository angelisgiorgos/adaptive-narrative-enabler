import json
import re

from utils.config_loader import config
from utils.llm_model_loader import load_causal_lm_with_fallback

try:
    import torch
except ImportError:
    torch = None


class LLMSuggester:
    def __init__(self):
        self.model_name = config.get("llm_evaluator.model_name", "meta-llama/Llama-3.2-3B-Instruct")
        self.max_new_tokens = config.get("editor_suggester.max_new_tokens", 256)
        self.temperature = config.get("editor_suggester.temperature", 0.7)
        self._tokenizer = None
        self._model = None
        self._ready = False
        self._error = None

    def available(self):
        self._ensure_loaded()
        return self._model is not None

    def last_error(self):
        return self._error

    def suggest_list(self, field_name, context):
        self._ensure_loaded()
        if self._model is None or self._tokenizer is None:
            return None

        prompt = self._build_prompt(field_name, context)
        try:
            inputs = self._tokenizer(prompt, return_tensors="pt")
            if hasattr(self._model, "device"):
                inputs = {k: v.to(self._model.device) for k, v in inputs.items()}

            prompt_length = inputs["input_ids"].shape[1]
            with torch.no_grad():
                outputs = self._model.generate(
                    **inputs,
                    max_new_tokens=self.max_new_tokens,
                    temperature=self.temperature,
                    do_sample=True,
                    pad_token_id=self._tokenizer.eos_token_id,
                )

            new_tokens = outputs[0][prompt_length:]
            text = self._tokenizer.decode(new_tokens, skip_special_tokens=True)
            suggestions = self._extract_suggestions(text)
            if not suggestions:
                self._error = f"Model returned no parseable suggestions. Raw output: {text[:500]}"
            return suggestions[:5] if suggestions else None
        except Exception as exc:
            if self._is_oom_error(exc):
                self._clear_cuda_cache()
                self._error = "Model ran out of memory while generating suggestions."
                return None
            self._error = f"Suggestion generation failed: {exc}"
            return None

    def _ensure_loaded(self):
        if self._ready:
            return
        self._ready = True

        if torch is None:
            self._error = "Torch is not installed."
            return

        self._clear_cuda_cache()

        try:
            self._tokenizer, self._model, self.model_name = load_causal_lm_with_fallback(
                self.model_name,
                purpose="editor suggester",
                prefer_quantized=False,
            )
        except Exception as exc:
            if self._is_oom_error(exc):
                self._clear_cuda_cache()
                self._error = "Model load failed due to OOM."
            else:
                self._error = f"Model unavailable: {exc}"
            self._model = None

    def _build_prompt(self, field_name, context):
        payload = json.dumps(context, ensure_ascii=True, indent=2)
        field_rules = {
            "Tags": "Return concise lowercase tag names, usually single words or snake_case phrases.",
            "Outcome tags": "Return concise outcome tag names such as threat, social, stealth, combat, spawn, success, failure, setup_clue, or fitting custom snake_case tags.",
            "Goals": "Return short goal phrases, 2 to 6 words each.",
            "Interaction prompts": "Return direct player-facing interaction lines that start with a verb.",
            "Interaction point labels": "Return short noun phrases suitable for clickable interaction-point labels.",
            "Interaction point prompts": "Return direct player-facing interaction lines that start with a verb.",
            "Interaction point notes": "Return short authoring notes about narrative purpose or linked story function.",
            "Distant descriptions": "Return atmospheric observations seen or heard before entering.",
            "Entered descriptions": "Return immediate scene-setting text for the moment the traveler arrives.",
            "Descriptions": "Return short flavorful descriptive lines.",
            "Known locations": "Return concrete place names that fit this character.",
            "Known objects": "Return concrete object names that fit this character.",
            "Action names": "Return concise player-facing action labels that start with a verb.",
            "Outcome descriptions": "Return short consequence descriptions for an interactive story action.",
        }
        specific_rule = field_rules.get(field_name, "Return concise, setting-appropriate suggestions.")
        return f"""
You are helping design an interactive narrative world.

Generate 5 concise suggestions for the field "{field_name}".
Return ONLY valid JSON in this format:
{{"suggestions": ["...", "...", "..."]}}

Guidelines:
- Keep each suggestion distinct.
- Match the tone and tags of the selected entry.
- Avoid repeating existing values.
- Use the whole current case below, not just one field in isolation.
- Base the result on the selected field and selected entry.
- {specific_rule}

Context:
{payload}
"""

    def _extract_suggestions(self, text):
        suggestions = self._extract_json_suggestions(text)
        if suggestions:
            return suggestions
        return self._extract_line_suggestions(text)

    def _extract_json_suggestions(self, text):
        try:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start == -1 or end <= start:
                return None
            parsed = json.loads(text[start:end])
            suggestions = parsed.get("suggestions", [])
            if not isinstance(suggestions, list):
                return None
            cleaned = []
            for suggestion in suggestions:
                if isinstance(suggestion, str):
                    stripped = suggestion.strip()
                    if stripped:
                        cleaned.append(stripped)
            return cleaned
        except Exception:
            return None

    def _extract_line_suggestions(self, text):
        lines = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            line = re.sub(r"^[-*]\s+", "", line)
            line = re.sub(r"^\d+[\).\-\:]\s*", "", line)
            if not line:
                continue
            lowered = line.lower()
            if lowered.startswith("suggestions"):
                continue
            if lowered.startswith("here are"):
                continue
            if lowered.startswith('{"suggestions"'):
                continue
            lines.append(line.strip(' "\''))

        cleaned = []
        seen = set()
        for line in lines:
            if len(line) < 3:
                continue
            if line in seen:
                continue
            seen.add(line)
            cleaned.append(line)
        return cleaned or None

    def _is_oom_error(self, exc):
        if torch is not None and isinstance(exc, torch.cuda.OutOfMemoryError):
            return True
        text = str(exc).lower()
        return "out of memory" in text or "oom" in text

    def _clear_cuda_cache(self):
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()
