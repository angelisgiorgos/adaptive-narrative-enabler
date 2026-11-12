"""The shared Gemma 4 runtime on vLLM (utils/llm_model_loader.py): embedded and server
modes, one runtime for the evaluator and the editor's suggester, and evolution's worker
processes spawned (not forked) once an embedded engine holds a GPU context. No GPU or vLLM
install is needed: vLLM and the server are replaced by fakes."""
import io
import json
import sys
import types
import unittest
from unittest.mock import patch

import main as engine
from utils import llm_model_loader
from utils.config_loader import config
from utils.llm_model_loader import VLLMRuntime, embedded_engine_active, get_llm_runtime


def _fake_vllm():
    module = types.ModuleType("vllm")

    class SamplingParams:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class LLM:
        last = None

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls = []
            LLM.last = self

        def chat(self, messages, sampling_params=None, use_tqdm=False):
            self.calls.append((messages, sampling_params.kwargs))
            reply = types.SimpleNamespace(text='{"score": 7}')
            return [types.SimpleNamespace(outputs=[reply])]

    module.LLM, module.SamplingParams = LLM, SamplingParams
    return module


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(llm_model_loader._RUNTIMES, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_embedded_gemma4_is_text_only_and_bounded(self):
        fake = _fake_vllm()
        runtime = VLLMRuntime()
        with patch.dict(sys.modules, {"vllm": fake}):
            text = runtime.generate("Judge this story.", max_new_tokens=64, temperature=0.2)
        self.assertEqual(text, '{"score": 7}')
        self.assertEqual(runtime.model_name, "google/gemma-4-E2B-it")
        kwargs = fake.LLM.last.kwargs
        self.assertEqual(kwargs["limit_mm_per_prompt"], {"image": 0, "audio": 0})
        self.assertEqual(kwargs["max_model_len"], 4096)
        messages, sampling = fake.LLM.last.calls[0]
        self.assertEqual(messages, [{"role": "user", "content": "Judge this story."}])
        self.assertEqual((sampling["max_tokens"], sampling["temperature"]), (64, 0.2))

    def test_server_mode_calls_an_openai_compatible_vllm_server(self):
        sent = {}

        def fake_urlopen(request, timeout=None):
            sent["url"], sent["body"] = request.full_url, json.loads(request.data)
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": "fine"}}]}).encode())

        with patch.dict(config._config["vllm"], {"mode": "server", "base_url": "http://vllm:8000/v1"}), \
                patch("urllib.request.urlopen", fake_urlopen):
            self.assertEqual(get_llm_runtime().generate("Hello", max_new_tokens=8), "fine")
        self.assertEqual(sent["url"], "http://vllm:8000/v1/chat/completions")
        self.assertEqual(sent["body"]["model"], "google/gemma-4-E2B-it")
        self.assertEqual(sent["body"]["max_tokens"], 8)

    def test_one_runtime_is_shared(self):
        from evaluator.llm_eval import LLMNarrativeEvaluator

        self.assertIs(LLMNarrativeEvaluator().runtime, get_llm_runtime())

    def test_without_vllm_the_features_say_so(self):
        from utils.llm_suggester import LLMSuggester

        with patch.dict(sys.modules, {"vllm": None}):
            with self.assertRaises(RuntimeError) as raised:
                VLLMRuntime().generate("Hi")
            self.assertIn("vLLM is required", str(raised.exception))
            suggester = LLMSuggester()
            self.assertIsNone(suggester.suggest_list("goals", {"name": "Priest"}))
        self.assertFalse(embedded_engine_active())

    def test_workers_are_spawned_once_a_gpu_engine_is_loaded(self):
        self.assertFalse(embedded_engine_active())
        pool = engine._new_process_pool(1)
        self.addCleanup(pool.shutdown)
        self.assertNotEqual(pool._mp_context.get_start_method(), "spawn")  # forked: inherits the config

        runtime = get_llm_runtime()
        with patch.dict(sys.modules, {"vllm": _fake_vllm()}):
            runtime.generate("Hi")
        self.assertTrue(embedded_engine_active())
        spawned = engine._new_process_pool(1)
        self.addCleanup(spawned.shutdown)
        self.assertEqual(spawned._mp_context.get_start_method(), "spawn")
        self.assertIs(spawned._initializer, engine._init_spawned_worker)
        config_data, vectors = spawned._initargs
        self.assertEqual(config_data["vllm"]["model_name"], "google/gemma-4-E2B-it")
        self.assertTrue(vectors)


if __name__ == "__main__":
    unittest.main()
