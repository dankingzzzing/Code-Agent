import json
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from code_agent.agent import AgentResult, Event
from code_agent.errors import ProviderError
from code_agent.settings_store import SettingsStore
from code_agent.web import create_server


class WebPersistenceStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "file.py").write_text("value = 1\n")
        self.client = build_opener(HTTPCookieProcessor(CookieJar()))
        self.start()

    def start(self):
        with patch.dict("os.environ", {}, clear=True):
            self.server = create_server(self.root, demo=False, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.current = self.get_config()
        self.token = self.current["csrf_token"]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def tearDown(self):
        self.stop()
        self.temp.cleanup()

    def get_config(self):
        with self.client.open(self.base + "/api/config", timeout=5) as response:
            return json.load(response)

    def request(self, route, payload):
        return Request(self.base + route, data=json.dumps(payload).encode(),
                       headers={"Content-Type": "application/json", "X-Agent-Token": self.token})

    def post(self, route, payload):
        with self.client.open(self.request(route, payload), timeout=5) as response:
            return json.load(response)

    def test_refresh_and_restart_restore_verified_config_without_secret_in_browser(self):
        values = {"api_key": "persisted-test-secret", "model": "model", "base_url": "https://provider.example/v1",
                  "api_style": "chat_completions", "verify": True, "remember": True}
        with patch("code_agent.web.LLMProvider.check_connection", return_value={"connected": True}):
            saved = self.post("/api/settings", values)
        refreshed = self.get_config()
        self.assertEqual(refreshed["profile"], saved["profile"])
        self.assertTrue(refreshed["configured"])
        self.assertTrue(refreshed["remembered"])
        self.assertNotIn(values["api_key"], json.dumps(refreshed))
        self.stop()
        self.start()
        self.assertTrue(self.current["configured"])
        self.assertEqual(self.current["model"], "model")
        self.assertEqual(self.current["api_style"], "chat_completions")
        self.assertNotIn(values["api_key"], json.dumps(self.current))
        self.assertEqual(SettingsStore(self.root).load()[0].api_key, values["api_key"])

    def test_failed_verification_keeps_last_good_settings_and_returns_auth_signal(self):
        values = {"api_key": "good-test-key", "model": "model", "verify": True}
        with patch("code_agent.web.LLMProvider.check_connection", return_value={}):
            saved = self.post("/api/settings", values)
        with patch("code_agent.web.LLMProvider.check_connection", side_effect=ProviderError("invalid key", http_status=401)):
            with self.assertRaises(HTTPError) as failure:
                self.post("/api/settings", {**values, "profile": saved["profile"], "api_key": "bad-test-key"})
        error = json.load(failure.exception)
        self.assertTrue(error["needs_settings"])
        self.assertEqual(SettingsStore(self.root).load()[0].api_key, "good-test-key")
        self.assertEqual(self.get_config()["profile"], saved["profile"])

    def test_unremembered_config_survives_refresh_but_not_server_restart(self):
        profile = self.post("/api/settings", {"demo": True, "remember": False})
        self.assertTrue(self.get_config()["configured"])
        self.assertFalse(profile["remembered"])
        self.assertIsNone(SettingsStore(self.root).load())
        self.stop()
        self.start()
        self.assertFalse(self.current["configured"])

    def test_stream_sends_events_before_review_is_complete(self):
        profile = self.post("/api/settings", {"demo": True, "remember": False})["profile"]
        release, waiting = threading.Event(), threading.Event()

        class DelayedAgent:
            def run(self, task, target, *, on_event, on_text):
                on_event(Event(0, "plan", "读取完整源码", progress=.1))
                waiting.set()
                if not release.wait(5):
                    raise AssertionError("client did not receive the live event")
                on_text("公开报告正文")
                return AgentResult("公开报告正文", "completed", "test", 1, 0)

        try:
            with patch("code_agent.web.create_agent", return_value=DelayedAgent()):
                with self.client.open(self.request("/api/review/stream", {"profile": profile, "task": "审查", "target": "file.py"}), timeout=5) as response:
                    self.assertIn("application/x-ndjson", response.headers["Content-Type"])
                    first = json.loads(response.readline())
                    self.assertEqual(first["type"], "event")
                    self.assertFalse(release.is_set())
                    self.assertTrue(waiting.wait(1))
                    release.set()
                    rest = [json.loads(line) for line in response]
            self.assertEqual([frame["type"] for frame in rest], ["text", "result"])
            self.assertEqual(rest[-1]["data"]["answer"], "公开报告正文")
        finally:
            release.set()

    def test_stream_auth_error_requests_model_setup(self):
        profile = self.post("/api/settings", {"demo": True, "remember": False})["profile"]
        with patch("code_agent.agent.CodeReviewAgent.run", side_effect=ProviderError("invalid key", http_status=401)):
            with self.client.open(self.request("/api/review/stream", {"profile": profile, "task": "审查", "target": "file.py"}), timeout=5) as response:
                frame = json.loads(response.readline())
        self.assertEqual(frame["type"], "error")
        self.assertTrue(frame["data"]["needs_settings"])
