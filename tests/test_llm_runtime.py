"""The shared Gemma 4 runtime on vLLM (utils/llm_model_loader.py): embedded and server
modes, one runtime for the evaluator and the editor's suggester, and evolution's worker
processes spawned (not forked) once an embedded engine holds a GPU context. No GPU or vLLM
install is needed: vLLM and the server are replaced by fakes."""
try:  # recording goes to a temporary folder; the studio talks to the API in this process
    import records_sandbox  # noqa: F401
except ImportError:
    from tests import records_sandbox  # noqa: F401
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


def _own_turns(test):
    """A test's own turn coordinator, its idle timers cancelled when the test ends: a timer of one test
    must not put to sleep the servers another test points at (they are found by the settings in force)."""
    turns = llm_model_loader.Turns()
    patcher = patch.object(llm_model_loader, "TURNS", turns)
    patcher.start()
    test.addCleanup(patcher.stop)
    test.addCleanup(turns.close)
    return turns


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(llm_model_loader._RUNTIMES, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        _own_turns(self)

    def test_embedded_gemma4_is_text_only_and_bounded(self):
        fake = _fake_vllm()
        runtime = VLLMRuntime()
        with patch.dict(sys.modules, {"vllm": fake}):
            text = runtime.generate("Judge this story.", max_new_tokens=64, temperature=0.2)
        self.assertEqual(text, '{"score": 7}')
        self.assertEqual(runtime.model_name, "google/gemma-4-E4B-it")
        kwargs = fake.LLM.last.kwargs
        self.assertEqual(kwargs["limit_mm_per_prompt"], {"image": 0, "audio": 0})
        self.assertEqual(kwargs["max_model_len"], 8192)
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
        self.assertEqual(sent["body"]["model"], "google/gemma-4-E4B-it")
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
        self.assertEqual(config_data["vllm"]["model_name"], "google/gemma-4-E4B-it")
        self.assertTrue(vectors)


class JudgeStatusTests(unittest.TestCase):
    """Whether the judge can be used is known before anyone asks it for a judgement, with what to
    do about it (GET /v1/judge/status, the studio's judge views)."""

    def setUp(self):
        patcher = patch.dict(llm_model_loader._RUNTIMES, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        _own_turns(self)

    def _server(self, handler_code=200, models=("google/gemma-4-E4B-it",)):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(handler_code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"data": [{"id": name} for name in models]}).encode())

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/v1"

    def test_the_environment_points_the_judge_at_a_server(self):
        with patch.dict("os.environ", {"ANE_JUDGE_URL": "http://judge.example:8000/v1/", "ANE_JUDGE_MODEL": "m"}):
            active = llm_model_loader.settings()
            runtime = get_llm_runtime()
        self.assertEqual((active["mode"], active["base_url"], active["model_name"]),
                         ("server", "http://judge.example:8000/v1", "m"))
        self.assertEqual((runtime.mode, runtime.model_name), ("server", "m"))
        with patch.dict("os.environ", {"ANE_JUDGE_URL": ""}):
            self.assertEqual(llm_model_loader.settings()["mode"], config.get("vllm.mode", "embedded"))

    def test_the_status_says_why_the_judge_cannot_be_used_and_how_to_fix_it(self):
        with patch.dict("os.environ", {"ANE_JUDGE_URL": "http://127.0.0.1:9/v1"}):  # nothing listens there
            status = llm_model_loader.status(timeout=1)
        self.assertFalse(status["available"])
        self.assertIn("No vLLM server answers at http://127.0.0.1:9/v1", status["reason"])
        self.assertIn("docker compose --profile judge up -d judge", status["how"])
        with patch.dict("os.environ", {"ANE_JUDGE_URL": self._server(models=("other/model",))}):
            self.assertIn("serves other/model", llm_model_loader.status()["reason"])
        with patch.dict("os.environ", {"ANE_JUDGE_URL": self._server(handler_code=401)}):
            self.assertIn("HTTP 401", llm_model_loader.status()["reason"])
        with patch.dict("os.environ", {"ANE_JUDGE_URL": self._server()}):
            ready = llm_model_loader.status()
        self.assertTrue(ready["available"])
        self.assertIsNone(ready["reason"])
        with patch.dict("os.environ", {"ANE_JUDGE_URL": ""}), patch.dict(config._config, {"vllm": {"mode": "embedded"}}), \
                patch.object(llm_model_loader.importlib.util, "find_spec", return_value=None):
            embedded = llm_model_loader.status()
        self.assertFalse(embedded["available"])
        self.assertIn("not installed", embedded["reason"])

    def test_the_api_and_the_studio_show_the_status(self):
        from fastapi.testclient import TestClient

        import api
        import app

        with patch.dict("os.environ", {"ANE_JUDGE_URL": "http://127.0.0.1:9/v1"}):
            body = TestClient(api.api).get("/v1/judge/status").json()
            shown = app.judge_status_ui()
        self.assertFalse(body["available"])
        self.assertIn("not available here", shown)
        self.assertIn("--profile judge", shown)


if __name__ == "__main__":
    unittest.main()


class AugmentationModelTests(unittest.TestCase):
    """World augmentation runs on its own language model (augmentation.llm: Qwen3.5-4B served by vLLM,
    float16, text only, without its thinking), not the judge's Gemma 4."""

    setUp = JudgeStatusTests.setUp
    _server = JudgeStatusTests._server

    def test_the_augmentation_role_has_its_own_model_and_settings(self):
        with patch.dict("os.environ", {"ANE_AUGMENT_URL": "", "ANE_AUGMENT_MODEL": "", "ANE_AUGMENT_MODEL_PATH": ""}):
            own = llm_model_loader.settings("augmentation")
            judge = llm_model_loader.settings()
        self.assertEqual((own["model_name"], own["mode"], own["base_url"], own["model_path"]),
                         ("Qwen/Qwen3.5-4B", "server", "http://127.0.0.1:8001/v1",
                          "/data/angelisg/llm_models/Qwen3.5-4B"))
        self.assertEqual((own["engine"]["dtype"], own["engine"]["gpu_memory_utilization"]), ("float16", 0.6))                      # not quantized, Turing
        self.assertEqual(own["engine"]["limit_mm_per_prompt"], {"image": 0, "video": 0})
        self.assertEqual(own["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(judge["model_name"], "google/gemma-4-E4B-it")          # the judge is unchanged
        with patch.dict("os.environ", {"ANE_AUGMENT_URL": "http://hamlet-ane-augmenter:8000/v1/",
                                       "ANE_AUGMENT_MODEL": "q"}):
            pointed = llm_model_loader.settings("augmentation")
        self.assertEqual((pointed["mode"], pointed["base_url"], pointed["model_name"]),
                         ("server", "http://hamlet-ane-augmenter:8000/v1", "q"))
        # One runtime per role: augmentation never loads or asks the judge's model.
        self.assertIsNot(get_llm_runtime(role="augmentation"), get_llm_runtime())
        self.assertIs(get_llm_runtime(role="augmentation"), get_llm_runtime(role="augmentation"))

    def test_the_augmenter_asks_qwen_on_its_server_without_thinking(self):
        from core_engine.augmenter import WorldAugmenter

        sent = {}

        def fake_urlopen(request, timeout=None):
            sent["url"], sent["body"] = request.full_url, json.loads(request.data)
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode())

        with patch.dict("os.environ", {"ANE_AUGMENT_URL": "", "ANE_AUGMENT_MODEL": ""}), \
                patch("urllib.request.urlopen", fake_urlopen):
            llm = WorldAugmenter().llm
            self.assertEqual((llm.role, llm.model_name, llm.runtime.role), ("augmentation", "Qwen/Qwen3.5-4B",
                                                                            "augmentation"))
            llm.generate("Propose new places.")
        self.assertEqual(sent["url"], "http://127.0.0.1:8001/v1/chat/completions")
        self.assertEqual(sent["body"]["model"], "Qwen/Qwen3.5-4B")
        self.assertEqual(sent["body"]["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(sent["body"]["response_format"], {"type": "json_object"})   # valid JSON, by decoding
        self.assertEqual(sent["body"]["max_tokens"], config.get("augmentation.llm.max_new_tokens"))
        # The judge's requests carry nothing of it.
        with patch.dict(config._config["vllm"], {"mode": "server", "base_url": "http://vllm:8000/v1"}), \
                patch("urllib.request.urlopen", fake_urlopen):
            get_llm_runtime().generate("Judge.")
        self.assertNotIn("chat_template_kwargs", sent["body"])
        self.assertNotIn("response_format", sent["body"])

    def test_embedded_it_loads_the_folder_in_float16_text_only(self):
        fake = _fake_vllm()
        chats = []
        fake.LLM.chat = lambda self, messages, sampling_params=None, use_tqdm=False, **kw: (
            chats.append(kw), [types.SimpleNamespace(outputs=[types.SimpleNamespace(text="{}")])])[1]
        with patch.dict(config._config["augmentation"]["llm"], {"mode": "embedded"}), \
                patch.dict("os.environ", {"ANE_AUGMENT_URL": "", "ANE_AUGMENT_MODEL_PATH": "/models/qwen"}), \
                patch.dict(sys.modules, {"vllm": fake}):
            get_llm_runtime(role="augmentation").generate("Propose.")
        kwargs = fake.LLM.last.kwargs
        self.assertEqual((kwargs["model"], kwargs["served_model_name"], kwargs["dtype"]),
                         ("/models/qwen", "Qwen/Qwen3.5-4B", "float16"))
        self.assertEqual(kwargs["limit_mm_per_prompt"], {"image": 0, "video": 0})
        self.assertNotIn("quantization", kwargs)
        self.assertEqual(chats, [{"chat_template_kwargs": {"enable_thinking": False}}])

    def test_its_status_names_its_own_server_and_model(self):
        url = self._server(models=("google/gemma-4-E4B-it",))              # a server with the judge's model only
        with patch.dict("os.environ", {"ANE_AUGMENT_URL": url, "ANE_AUGMENT_MODEL": ""}):
            found = llm_model_loader.status(role="augmentation")
        self.assertFalse(found["available"])
        self.assertIn("not Qwen/Qwen3.5-4B (augmentation.llm.model_name or ANE_AUGMENT_MODEL)", found["reason"])
        self.assertIn("--profile augmenter", found["how"])
        url = self._server(models=("Qwen/Qwen3.5-4B",))
        with patch.dict("os.environ", {"ANE_AUGMENT_URL": url, "ANE_AUGMENT_MODEL": ""}):
            self.assertTrue(llm_model_loader.status(role="augmentation")["available"])


class SharingTests(unittest.TestCase):
    """The judge and the augmenter take turns on one GPU (llm_sharing, Turns): before one generates, the
    other is put to sleep (vLLM sleep level 1: weights to CPU memory) once nothing of its is running, and
    its own is woken; idle, it goes to sleep too. Two fake vLLM servers record what they are asked."""

    def setUp(self):
        patcher = patch.dict(llm_model_loader._RUNTIMES, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        _own_turns(self)
        self.events = []
        self.judge, self.augmenter = self._server("judge"), self._server("augmenter")
        env = {"ANE_JUDGE_URL": self.judge + "/v1", "ANE_JUDGE_MODEL": "google/gemma-4-E4B-it",
               "ANE_AUGMENT_URL": self.augmenter + "/v1", "ANE_AUGMENT_MODEL": "Qwen/Qwen3.5-4B",
               "ANE_LLM_SHARING": ""}
        patcher = patch.dict("os.environ", env)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.dict(config._config, {"llm_sharing": {"enabled": True, "idle_seconds": 0.2, "level": 1,
                                                               "wait_seconds": 30}})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _server(self, name, sleep_mode=True, slow=0.0):
        import http.server
        import threading
        import time as clock

        events, state = self.events, {"sleeping": False}

        class Handler(http.server.BaseHTTPRequestHandler):
            def _reply(self, code, body=b"{}"):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                path = self.path.split("?")[0]
                if path in ("/sleep", "/wake_up"):
                    if not sleep_mode:
                        return self._reply(404)
                    state["sleeping"] = path == "/sleep"
                    events.append((name, path[1:]))
                    return self._reply(200)
                if state["sleeping"]:
                    events.append((name, "ASKED WHILE ASLEEP"))
                events.append((name, "generate"))
                clock.sleep(slow)
                events.append((name, "generated"))
                self._reply(200, json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())

            def do_GET(self):
                if self.path == "/is_sleeping" and sleep_mode:
                    return self._reply(200, json.dumps({"is_sleeping": state["sleeping"]}).encode())
                self._reply(200, json.dumps({"data": [{"id": "google/gemma-4-E4B-it"}, {"id": "Qwen/Qwen3.5-4B"}]}).encode())

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def test_one_model_awake_at_a_time_and_idle_ones_go_to_sleep(self):
        import time as clock

        get_llm_runtime(role="augmentation").generate("Propose.")
        self.assertEqual(self.events[:3], [("judge", "sleep"), ("augmenter", "wake_up"), ("augmenter", "generate")])
        get_llm_runtime().generate("Judge.")                                     # the judge: its turn
        self.assertEqual(self.events[4:7], [("augmenter", "sleep"), ("judge", "wake_up"), ("judge", "generate")])
        self.assertEqual(llm_model_loader.status(role="augmentation")["sleeping"], True)  # in CPU memory
        clock.sleep(0.6)                                                       # idle: asleep too
        self.assertEqual(self.events[-1], ("judge", "sleep"))
        self.assertEqual(llm_model_loader.status()["sleeping"], True)
        self.assertNotIn("ASKED WHILE ASLEEP", [event for _, event in self.events])

    def test_a_model_waits_while_the_other_is_running_and_concurrent_calls_share_a_turn(self):
        import threading

        slow = self._server("slow-judge", slow=0.5)
        with patch.dict("os.environ", {"ANE_JUDGE_URL": slow + "/v1"}):
            judges = [threading.Thread(target=get_llm_runtime().generate, args=("Judge.",)) for _ in range(3)]
            for thread in judges:
                thread.start()
            clock_wait = threading.Event()
            clock_wait.wait(0.2)                                                 # the judges are running
            get_llm_runtime(role="augmentation").generate("Propose.")           # waits for all three
            for thread in judges:
                thread.join()
        events = self.events
        self.assertEqual(events.count(("slow-judge", "wake_up")), 1)            # one wake for the three
        last_judge = max(i for i, e in enumerate(events) if e == ("slow-judge", "generated"))
        self.assertLess(last_judge, events.index(("slow-judge", "sleep")))      # put to sleep after them
        self.assertLess(events.index(("slow-judge", "sleep")), events.index(("augmenter", "generate")))

    def test_off_nothing_is_put_to_sleep_and_a_server_without_sleep_mode_still_answers(self):
        with patch.dict("os.environ", {"ANE_LLM_SHARING": "off"}):
            get_llm_runtime(role="augmentation").generate("Propose.")
            get_llm_runtime().generate("Judge.")
        self.assertEqual([e for _, e in self.events if e in ("sleep", "wake_up")], [])
        plain = self._server("plain", sleep_mode=False)
        with patch.dict("os.environ", {"ANE_JUDGE_URL": plain + "/v1"}):
            self.assertEqual(get_llm_runtime().generate("Judge."), "ok")
            self.assertEqual(get_llm_runtime().generate("Again."), "ok")
        self.assertEqual(llm_model_loader.TURNS.state["judge"], "always")        # never asked to sleep again
        self.assertEqual(self.events.count(("plain", "generate")), 2)

    def test_models_that_loaded_awake_are_put_to_sleep_in_the_background(self):
        import threading
        import time as clock

        turns = llm_model_loader.TURNS
        self.assertEqual(sorted(turns.sleep_idle_servers()), ["augmentation", "judge"])  # both loaded awake
        self.assertEqual(sorted(self.events), [("augmenter", "sleep"), ("judge", "sleep")])
        self.assertEqual(turns.sleep_idle_servers(), [])                                # asleep already
        self.assertEqual(get_llm_runtime().generate("Judge."), "ok")                    # woken on demand
        self.assertIn(("judge", "wake_up"), self.events)
        # Never a model while it runs, nor before its idle time is over (its idle timer pending).
        with turns._cond:
            turns._active["judge"] = 1
        self.events.clear()
        with patch.object(turns, "_server_sleeping", return_value=False):
            self.assertEqual(turns.sleep_idle_servers(), ["augmentation"])
            with turns._cond:
                turns._active["judge"] = 0
                turns._timers["judge"] = threading.Timer(60, lambda: None)
            self.assertNotIn("judge", turns.sleep_idle_servers())
            turns._timers.pop("judge")
            self.assertIn("judge", turns.sleep_idle_servers())                          # idle now: asleep
        self.assertEqual([e for e in self.events if e[0] == "judge"], [("judge", "sleep")])
        # In the background: started once, it puts them to sleep by itself, and stops.
        get_llm_runtime().generate("Judge.")
        turns.close()
        with patch.dict(config._config, {"llm_sharing": {**config.get("llm_sharing"), "idle_seconds": 60}}):
            get_llm_runtime().generate("Judge again.")                                  # awake, timer pending
        turns._timers.pop("judge").cancel()                                              # as if the timer were lost
        self.events.clear()
        self.assertTrue(turns.start_watcher(every=0.05))
        self.assertFalse(turns.start_watcher(every=0.05))                               # one watcher
        clock.sleep(0.3)
        turns.stop_watcher()
        self.assertIn(("judge", "sleep"), self.events)

    def test_no_watcher_when_sharing_is_off_or_it_is_not_wanted(self):
        turns = llm_model_loader.TURNS
        with patch.dict("os.environ", {"ANE_LLM_SHARING": "off"}):
            self.assertEqual(turns.sleep_idle_servers(), [])                            # nothing would wake them
        self.assertFalse(turns.start_watcher(every=0))
        self.assertEqual(self.events, [])

    def test_the_api_starts_and_stops_the_watcher(self):
        from fastapi.testclient import TestClient

        import api

        turns = llm_model_loader.TURNS
        with patch.dict(config._config, {"llm_sharing": {**config.get("llm_sharing"), "watch_seconds": 30}}):
            with TestClient(api.api):
                self.assertTrue(turns._watcher is not None and turns._watcher.is_alive())
            self.assertIsNone(turns._watcher)

    def test_the_container_start_puts_its_peer_to_sleep_first(self):
        sys.path.insert(0, "packaging")
        self.addCleanup(sys.path.remove, "packaging")
        import vllm_turn_start

        with patch.dict("os.environ", {"ANE_LLM_PEERS": f"{self.judge}, http://127.0.0.1:9", "ANE_LLM_SHARING": "on"}), \
                patch("os.execvp") as start, patch("sys.stdout", new_callable=io.StringIO) as said:
            vllm_turn_start.main(["--model=/models/Qwen3.5-4B", "--enable-sleep-mode"])
        self.assertEqual(self.events, [("judge", "sleep")])
        self.assertIn("not put to sleep", said.getvalue())                       # a peer not running: left
        start.assert_called_once_with("vllm", ["vllm", "serve", "--model=/models/Qwen3.5-4B", "--enable-sleep-mode"])
        with patch.dict("os.environ", {"ANE_LLM_PEERS": self.judge, "ANE_LLM_SHARING": "off"}), patch("os.execvp"):
            vllm_turn_start.main([])
        self.assertEqual(self.events, [("judge", "sleep")])                      # off: not asked
        # Started together: it waits for a peer that exists until it serves; one that does not, it skips.
        self.assertEqual(vllm_turn_start.wait_for_peer(self.judge, 1), "serving")
        self.assertEqual(vllm_turn_start.wait_for_peer("http://no-such-peer.invalid:8000", 1), "absent")
        self.assertEqual(vllm_turn_start.wait_for_peer("http://127.0.0.1:9", 0.5, step=0.1), "not serving")
        with patch.dict("os.environ", {"ANE_LLM_PEERS": f"{self.judge},http://no-such-peer.invalid:8000",
                                       "ANE_LLM_SHARING": "on", "ANE_LLM_WAIT_FOR_PEERS": "2"}), \
                patch("os.execvp"), patch("sys.stdout", new_callable=io.StringIO) as said:
            vllm_turn_start.main([])
        self.assertEqual(self.events[-1], ("judge", "sleep"))
        self.assertIn("no-such-peer.invalid:8000: absent", said.getvalue())
