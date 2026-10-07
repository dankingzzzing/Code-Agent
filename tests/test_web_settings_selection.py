import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from code_agent.errors import AgentError, ProviderError
from code_agent.web import create_server, select_path


class WebSettingsSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.external = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.outside = Path(self.external.name)
        (self.root / "demo.py").write_text("def f(x=[]):\n return x\n")
        (self.outside / "local.py").write_text("def local(x=[]):\n return x\n")
        # A selected code project's configuration must not replace the model credentials.
        (self.outside / ".env").write_text("this is an invalid untrusted config")
        with patch.dict(os.environ, {}, clear=True):
            self.server = create_server(self.root, demo=False, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        with urlopen(self.base + "/api/config", timeout=5) as response:
            self.defaults = json.load(response)
        self.token = self.defaults["csrf_token"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()
        self.external.cleanup()

    def post(self, route, payload):
        request = Request(self.base + route, data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json", "X-Agent-Token": self.token})
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def demo_profile(self):
        return self.post("/api/settings", {"demo": True})["profile"]

    def test_starts_without_key_and_requires_page_configuration(self):
        self.assertFalse(self.defaults["api_key_configured"])
        self.assertNotIn("api_key", self.defaults)
        with self.assertRaises(HTTPError) as captured:
            self.post("/api/review", {"task": "q", "target": "demo.py"})
        self.assertIn("配置", json.load(captured.exception)["error"])
        with self.assertRaises(HTTPError):
            self.post("/api/settings", {"model": "m", "api_key": "", "demo": False})

    def test_settings_are_verified_in_memory_and_never_return_secret(self):
        values = {"api_key": "not-a-real-secret", "model": "model", "base_url": "https://provider.example/v1",
                  "api_style": "auto", "verify": True}
        with patch("code_agent.web.LLMProvider.check_connection", return_value={"connected": True, "tool_calling": True}) as checked:
            result = self.post("/api/settings", values)
        self.assertEqual(checked.call_count, 1)
        self.assertEqual(result["api_style"], "chat_completions")
        self.assertNotIn(values["api_key"], json.dumps(result))
        self.assertFalse((self.root / ".env").exists())
        with patch("code_agent.web.LLMProvider.check_connection", side_effect=ProviderError("bad model")):
            with self.assertRaises(HTTPError) as captured:
                self.post("/api/settings", {**values, "profile": result["profile"]})
        self.assertIn("bad model", json.load(captured.exception)["error"])

    def test_native_file_and_directory_select_outside_original_workspace(self):
        profile = self.demo_profile()
        for kind, value in (("file", str(self.outside / "local.py")), ("directory", str(self.outside))):
            with patch("code_agent.web.pick_local_path", return_value=value) as picked:
                selected = self.post("/api/pick", {"profile": profile, "kind": kind})
            picked.assert_called_once_with(kind, self.root.resolve())
            result = self.post("/api/review", {"profile": profile, "selection": selected["selection"], "task": "审查"})
            self.assertEqual(result["workspace"], str(self.outside.resolve()))
            self.assertIn("可变对象作为默认参数", result["answer"])
            self.assertIn("local.py", result["answer"])
        with patch("code_agent.web.pick_local_path", return_value=None):
            self.assertTrue(self.post("/api/pick", {"profile": profile, "kind": "file"})["cancelled"])

    def test_selection_scopes_and_profiles_are_not_interchangeable(self):
        first, second = self.demo_profile(), self.demo_profile()
        selected = self.post("/api/select", {"profile": first, "path": str(self.outside)})
        with self.assertRaises(HTTPError):
            self.post("/api/review", {"profile": second, "selection": selected["selection"], "task": "q"})
        reviewed = self.post("/api/review", {"profile": first, "selection": selected["selection"], "task": "审查"})
        with self.assertRaises(HTTPError):
            self.post("/api/review", {"profile": second, "session": reviewed["session"], "task": "q"})
        with self.assertRaises(HTTPError):
            self.post("/api/review", {"profile": first, "selection": selected["selection"], "target": "../outside.py", "task": "q"})
        self.post("/api/settings", {"profile": first, "demo": True})
        with self.assertRaises(HTTPError):
            self.post("/api/review", {"profile": first, "selection": selected["selection"], "session": reviewed["session"], "task": "q"})

    def test_private_and_missing_paths_are_rejected(self):
        (self.root / ".env").write_text("SECRET")
        (self.root / ".git").mkdir()
        for value in (str(self.root / ".env"), str(self.root / ".git"), "missing.py", ""):
            with self.assertRaises(AgentError):
                select_path(value, self.root)
