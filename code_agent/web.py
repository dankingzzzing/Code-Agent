"""Small local-only HTTP interface. No external scripts or remote assets."""

from __future__ import annotations

import json
import secrets
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .app import create_agent
from .errors import AgentError


STATIC = Path(__file__).parent / "static"


def create_server(root: Path, *, demo: bool, allow_exec: bool = False, port: int = 8765) -> ThreadingHTTPServer:
    probe = create_agent(root, demo=demo, allow_exec=allow_exec)
    token = secrets.token_urlsafe(32)
    sessions: dict[str, tuple] = {}
    sessions_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Avoid logging user requests or source content.
            pass

        def _send(self, status, body, content_type="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _valid_host(self):
            return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}",
                                                f"localhost:{self.server.server_port}"}

        def do_GET(self):
            if not self._valid_host():
                return self._send(403, {"error": "无效的本机 Host。"})
            if self.path == "/api/config":
                return self._send(200, {"mode": probe.provider.label, "demo": demo, "allow_exec": allow_exec,
                                        "csrf_token": token})
            routes = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if self.path not in routes:
                return self._send(404, {"error": "页面不存在。"})
            name, content_type = routes[self.path]
            return self._send(200, (STATIC / name).read_bytes(), content_type)

        def do_POST(self):
            if not self._valid_host() or not secrets.compare_digest(
                    self.headers.get("X-Agent-Token", "").encode("utf-8"), token.encode("ascii")):
                return self._send(403, {"error": "请求验证失败，请刷新本机页面。"})
            if self.path != "/api/review":
                return self._send(404, {"error": "接口不存在。"})
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                return self._send(415, {"error": "请求必须使用 application/json。"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384:
                    return self._send(413, {"error": "请求为空或超过 16 KiB。"})
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict) or set(payload) - {"task", "target", "session"}:
                    raise AgentError("请求格式无效。")
                session = payload.get("session")
                if session is not None and (not isinstance(session, str) or len(session) > 64):
                    raise AgentError("会话 ID 格式无效。")
                with sessions_lock:
                    if session:
                        if session not in sessions:
                            raise AgentError("会话已失效，请新建会话。")
                        agent, lock = sessions[session]
                    else:
                        if len(sessions) >= 32:
                            raise AgentError("已达到 32 个会话上限，请重启服务。")
                        session = uuid.uuid4().hex
                        agent = create_agent(root, demo=demo, allow_exec=allow_exec)
                        lock = threading.Lock()
                        sessions[session] = (agent, lock)
                if not lock.acquire(blocking=False):
                    return self._send(409, {"error": "该会话正在审查，请等待结果。"})
                try:
                    result = agent.run(payload.get("task"), payload.get("target", "."))
                finally:
                    lock.release()
                return self._send(200, {**result.to_dict(), "session": session})
            except (AgentError, ValueError, UnicodeError) as exc:
                return self._send(400, {"error": str(exc)})
            except OSError:
                return self._send(500, {"error": "文件或模型服务访问失败，请查看配置。"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def serve(root: Path, *, demo: bool, allow_exec: bool, port: int) -> None:
    server = create_server(root, demo=demo, allow_exec=allow_exec, port=port)
    print(f"Code Agent：http://127.0.0.1:{server.server_port}（Ctrl+C 停止）", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
