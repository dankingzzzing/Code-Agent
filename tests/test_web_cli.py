import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from code_agent.cli import main
from code_agent.web import create_server


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "demo.py").write_text("def f(x=[]):\n    return x\n")

    def tearDown(self):
        self.temp.cleanup()

    def test_cli_output_json_and_exit_codes(self):
        output = self.root / "report.json"
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["review", "demo.py", "--root", str(self.root), "--demo", "--json", "--output", str(output)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["status"], "completed")
        with contextlib.redirect_stderr(io.StringIO()), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(main(["review", "demo.py", "--root", str(self.root)]), 1)
            self.assertEqual(main(["review", "missing.py", "--root", str(self.root), "--demo"]), 1)

    def test_web_review_session_and_request_guards(self):
        server = create_server(self.root, demo=True, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def post(payload, token="", host=None):
            headers = {"Content-Type": "application/json", "X-Agent-Token": token}
            if host:
                headers["Host"] = host
            request = Request(base + "/api/review", data=json.dumps(payload).encode(), headers=headers)
            return urlopen(request, timeout=5)

        try:
            with urlopen(base + "/api/config", timeout=5) as response:
                config = json.load(response)
            with urlopen(base + "/", timeout=5) as response:
                self.assertIn("代码审查助手", response.read().decode())
                self.assertIn("frame-ancestors", response.headers["Content-Security-Policy"])
            payload = {"task": "审查", "target": "demo.py"}
            with self.assertRaises(HTTPError) as captured:
                post(payload)
            self.assertEqual(captured.exception.code, 403)
            with post(payload, config["csrf_token"]) as response:
                first = json.load(response)
            self.assertEqual(first["status"], "completed")
            with post({**payload, "session": first["session"]}, config["csrf_token"]) as response:
                self.assertEqual(json.load(response)["session"], first["session"])
            for invalid in ({"task": "", "target": "demo.py"}, {"task": "q", "target": "../outside"}, []):
                with self.assertRaises(HTTPError) as captured:
                    post(invalid, config["csrf_token"])
                self.assertEqual(captured.exception.code, 400)
            with self.assertRaises(HTTPError) as captured:
                post(payload, config["csrf_token"], "evil.example")
            self.assertEqual(captured.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
