import json
import tempfile
import unittest
from pathlib import Path

from code_agent.tools import ToolRegistry


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tools = ToolRegistry(self.root)
        (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def call(self, name, path, **other):
        return self.tools.execute(name, json.dumps({"path": path, **other}))

    def test_read_lines_and_analysis(self):
        self.assertIn("1 | x = 1", self.call("read_file", "a.py")["data"]["content"])
        self.assertTrue(self.call("analyze_python", "a.py")["data"]["syntax_ok"])

    def test_escape_and_missing_path(self):
        for path in ("../outside.py", "missing.py", "", "a.py\x00", ".git/config"):
            with self.subTest(path=path):
                self.assertFalse(self.call("read_file", path)["ok"])

    def test_absolute_outside_workspace(self):
        with tempfile.TemporaryDirectory() as outside:
            file = Path(outside) / "x.py"
            file.write_text("x=1")
            self.assertFalse(self.call("read_file", str(file))["ok"])

    def test_symlink_escape(self):
        with tempfile.TemporaryDirectory() as outside:
            file = Path(outside) / "x.py"
            file.write_text("x=1")
            try:
                (self.root / "link.py").symlink_to(file)
            except OSError:
                self.skipTest("Symlink creation is unavailable on this host.")
            self.assertFalse(self.call("read_file", "link.py")["ok"])

    def test_private_files_not_read_or_listed(self):
        for name in (".env", "secret.key", "credentials.json"):
            (self.root / name).write_text("SECRET")
            self.assertFalse(self.call("read_file", name)["ok"])
        self.assertEqual(self.call("list_files", ".")["data"]["files"], ["a.py"])

    def test_json_and_tool_allowlist(self):
        for arguments in ("oops", "[]", "{}", '{"path":"a.py","extra":true}', '{"path":null}'):
            self.assertFalse(self.tools.execute("read_file", arguments)["ok"])
        self.assertFalse(self.call("shell", "a.py")["ok"])
        self.assertFalse(self.call("run_tests", ".")["ok"])
        self.assertNotIn("run_tests", [s["function"]["name"] for s in self.tools.schemas()])

    def test_oversized_binary_and_wrong_language(self):
        (self.root / "large.py").write_bytes(b"x" * 65537)
        (self.root / "binary.py").write_bytes(b"\x00\x01")
        (self.root / "data.txt").write_text("hello")
        for name in ("large.py", "binary.py"):
            self.assertFalse(self.call("read_file", name)["ok"])
        self.assertFalse(self.call("analyze_python", "data.txt")["ok"])

    def test_listing_is_bounded(self):
        for index in range(110):
            (self.root / f"p{index}.py").write_text("pass")
        result = self.call("list_files", ".")["data"]
        self.assertEqual(len(result["files"]), 100)
        self.assertTrue(result["truncated"])

    def test_passing_and_failing_tests_are_observed(self):
        test = self.root / "test_sample.py"
        test.write_text("import unittest\nclass T(unittest.TestCase):\n def test_a(self): self.assertEqual(1,1)\n")
        self.tools = ToolRegistry(self.root, allow_exec=True)
        good = self.call("run_tests", "test_sample.py")["data"]
        self.assertTrue(good["passed"])
        test.write_text("import unittest\nclass T(unittest.TestCase):\n def test_a(self): self.fail('expected failure')\n")
        bad = self.call("run_tests", "test_sample.py")["data"]
        self.assertFalse(bad["passed"])
        self.assertIn("expected failure", bad["output"])
        self.assertFalse(self.call("run_tests", "a.py")["ok"])

    def test_test_process_timeout(self):
        (self.root / "test_wait.py").write_text("import time\ntime.sleep(5)\n")
        self.tools = ToolRegistry(self.root, allow_exec=True, test_timeout=0.15)
        result = self.call("run_tests", ".")["data"]
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["passed"])

    def test_no_tests_is_inconclusive(self):
        self.tools = ToolRegistry(self.root, allow_exec=True)
        result = self.call("run_tests", ".")["data"]
        self.assertEqual(result["tests_run"], 0)
        self.assertEqual(result["stop_reason"], "no_tests")
        self.assertFalse(result["passed"])

    def test_test_environment_does_not_inherit_api_key(self):
        import os
        from unittest.mock import patch
        (self.root / "test_environment.py").write_text(
            "import os, unittest\nclass T(unittest.TestCase):\n"
            " def test_key(self): self.assertNotIn('LLM_API_KEY', os.environ)\n")
        self.tools = ToolRegistry(self.root, allow_exec=True)
        with patch.dict(os.environ, {"LLM_API_KEY": "not-a-real-key"}):
            result = self.call("run_tests", ".")["data"]
        self.assertTrue(result["passed"])
