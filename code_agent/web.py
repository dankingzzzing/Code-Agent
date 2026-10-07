"""Local Web UI with in-memory model configuration and explicit desktop selections."""

from __future__ import annotations

import json
import secrets
import threading
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .agent import CodeReviewAgent
from .app import create_agent
from .config import Config
from .errors import AgentError, ConfigError
from .file_picker import pick_local_path
from .providers import LLMProvider
from .tools import ToolRegistry, is_private_path

STATIC = Path(__file__).parent / "static"


@dataclass(frozen=True)
class Selection:
    id: str
    root: Path
    target: str
    display_path: str

    def public(self) -> dict:
        return {"selection": self.id, "path": self.display_path, "target": self.target,
                "workspace": str(self.root)}


@dataclass
class BrowserProfile:
    config: Config = field(repr=False)
    demo: bool = False
    selections: dict[str, Selection] = field(default_factory=dict)


@dataclass
class WebSession:
    profile: str
    selection: str
    agent: CodeReviewAgent
    lock: threading.Lock = field(default_factory=threading.Lock)


def select_path(value: str, default_root: Path) -> Selection:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096 or "\x00" in value:
        raise AgentError("请提供有效的本地文件或文件夹路径。")
    path = Path(value.strip().strip('"'))
    try:
        resolved = (path if path.is_absolute() else default_root / path).resolve(strict=True)
        if not resolved.is_file() and not resolved.is_dir():
            raise OSError("not a file or directory")
    except (OSError, RuntimeError) as exc:
        raise AgentError("所选本地路径不存在或无法访问。") from exc
    if is_private_path(Path(resolved.name)):
        raise AgentError("不能选择凭据文件或内部目录。")
    root = resolved if resolved.is_dir() else resolved.parent
    target = "." if resolved.is_dir() else resolved.name
    ToolRegistry(root).resolve(target)
    return Selection(uuid.uuid4().hex, root, target, str(resolved))


def create_server(root: Path, *, demo: bool, allow_exec: bool = False, port: int = 8765) -> ThreadingHTTPServer:
    root = root.resolve()
    if not root.is_dir():
        raise AgentError("工作区必须是存在的目录。")
    bootstrap_error = ""
    try:
        defaults = Config.from_env(root)
    except ConfigError as exc:
        defaults = Config()
        bootstrap_error = str(exc)
    token = secrets.token_urlsafe(32)
    profiles: dict[str, BrowserProfile] = {}
    sessions: dict[str, WebSession] = {}
    state_lock = threading.Lock()
    picker_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
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
                return self._send(200, {"mode": "请先配置模型", "demo": demo, "allow_exec": allow_exec,
                    "csrf_token": token, "api_key_configured": bool(defaults.api_key),
                    "model": defaults.model, "base_url": defaults.base_url, "api_style": "auto",
                    "root": str(root), "bootstrap_error": bootstrap_error})
            routes = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if self.path not in routes:
                return self._send(404, {"error": "页面不存在。"})
            name, content_type = routes[self.path]
            return self._send(200, (STATIC / name).read_bytes(), content_type)

        def _profile(self, payload) -> tuple[str, BrowserProfile]:
            profile_id = payload.get("profile")
            with state_lock:
                if not isinstance(profile_id, str) or profile_id not in profiles:
                    raise AgentError("请先在模型配置弹窗中完成设置。")
                return profile_id, profiles[profile_id]

        def _settings(self, payload):
            if set(payload) - {"profile", "api_key", "model", "base_url", "api_style", "demo", "verify"}:
                raise AgentError("模型配置字段无效。")
            demo_mode = payload.get("demo", False)
            verify = payload.get("verify", False)
            if not isinstance(demo_mode, bool) or not isinstance(verify, bool):
                raise AgentError("模型模式格式无效。")
            profile_id = payload.get("profile")
            with state_lock:
                if profile_id is not None and (not isinstance(profile_id, str) or profile_id not in profiles):
                    raise AgentError("模型设置已失效，请刷新页面。")
                previous = profiles[profile_id].config if profile_id else defaults
            config = previous.with_web_settings({k: v for k, v in payload.items()
                                                if k in {"api_key", "model", "base_url", "api_style"}})
            connection = None
            if not demo_mode:
                config.require_llm()
                if verify:
                    connection = LLMProvider(config).check_connection()
            with state_lock:
                if not profile_id:
                    if len(profiles) >= 64:
                        raise AgentError("页面配置数量过多，请重启服务。")
                    profile_id = uuid.uuid4().hex
                    profiles[profile_id] = BrowserProfile(config, demo_mode)
                else:
                    profiles[profile_id].config = config
                    profiles[profile_id].demo = demo_mode
                    for session_id in [s for s, record in sessions.items() if record.profile == profile_id]:
                        del sessions[session_id]
            label = "离线演示 · 规则规划器（非 LLM）" if demo_mode else f"LLM · {config.model}"
            return {"profile": profile_id, "mode": label, "demo": demo_mode,
                    "api_style": config.resolved_api_style, "api_key_configured": bool(config.api_key),
                    "base_url": config.base_url, "model": config.model, "connection": connection}

        def _select(self, payload, native=False):
            allowed = {"profile", "kind"} if native else {"profile", "path"}
            if set(payload) - allowed:
                raise AgentError("文件选择请求无效。")
            profile_id, profile = self._profile(payload)
            if native:
                if not isinstance(payload.get("kind"), str) or payload["kind"] not in {"file", "directory"}:
                    raise AgentError("请选择文件或文件夹。")
                if not picker_lock.acquire(blocking=False):
                    return 409, {"error": "文件选择窗口已打开，请完成当前选择。"}
                try:
                    value = pick_local_path(payload.get("kind"), root)
                finally:
                    picker_lock.release()
                if value is None:
                    return 200, {"cancelled": True}
            else:
                value = payload.get("path")
            selected = select_path(value, root)
            with state_lock:
                if len(profile.selections) >= 64:
                    raise AgentError("选择的目录过多，请刷新页面。")
                profile.selections[selected.id] = selected
            return 200, {**selected.public(), "cancelled": False}

        def _review(self, payload):
            if set(payload) - {"task", "target", "session", "profile", "selection"}:
                raise AgentError("请求格式无效。")
            profile_id, profile = self._profile(payload)
            selection_id = payload.get("selection")
            session_id = payload.get("session")
            with state_lock:
                if selection_id is None:
                    selected = Selection("default", root, payload.get("target", "."), str(root))
                elif not isinstance(selection_id, str) or selection_id not in profile.selections:
                    raise AgentError("所选目录已失效，请重新选择文件或文件夹。")
                else:
                    selected = profile.selections[selection_id]
                if session_id is not None:
                    if not isinstance(session_id, str) or session_id not in sessions:
                        raise AgentError("会话已失效，请新建会话。")
                    record = sessions[session_id]
                    if record.profile != profile_id or record.selection != selected.id:
                        raise AgentError("会话不属于当前模型或所选目录，请新建会话。")
                else:
                    if len(sessions) >= 64:
                        raise AgentError("已达到会话上限，请重启服务。")
                    agent = create_agent(selected.root, demo=profile.demo, allow_exec=allow_exec,
                                         config=profile.config)
                    session_id = uuid.uuid4().hex
                    record = WebSession(profile_id, selected.id, agent)
                    sessions[session_id] = record
            if not record.lock.acquire(blocking=False):
                return 409, {"error": "该会话正在审查，请等待结果。"}
            try:
                result = record.agent.run(payload.get("task"), payload.get("target", selected.target))
            except Exception:
                if payload.get("session") is None:
                    with state_lock:
                        sessions.pop(session_id, None)
                raise
            finally:
                record.lock.release()
            return 200, {**result.to_dict(), "session": session_id, "workspace": str(selected.root)}

        def do_POST(self):
            if not self._valid_host() or not secrets.compare_digest(
                    self.headers.get("X-Agent-Token", "").encode("utf-8"), token.encode("ascii")):
                return self._send(403, {"error": "请求验证失败，请刷新本机页面。"})
            if self.path not in {"/api/settings", "/api/select", "/api/pick", "/api/review"}:
                return self._send(404, {"error": "接口不存在。"})
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                return self._send(415, {"error": "请求必须使用 application/json。"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384:
                    return self._send(413, {"error": "请求为空或超过 16 KiB。"})
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise AgentError("请求格式无效。")
                if self.path == "/api/settings":
                    status, body = 200, self._settings(payload)
                elif self.path in {"/api/select", "/api/pick"}:
                    status, body = self._select(payload, native=self.path == "/api/pick")
                else:
                    status, body = self._review(payload)
                return self._send(status, body)
            except (AgentError, ValueError, UnicodeError) as exc:
                return self._send(400, {"error": str(exc)})
            except OSError:
                return self._send(500, {"error": "文件或模型服务访问失败，请检查设置。"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def serve(root: Path, *, demo: bool, allow_exec: bool, port: int) -> None:
    server = create_server(root, demo=demo, allow_exec=allow_exec, port=port)
    print(f"Code Agent：http://127.0.0.1:{server.server_port}（在网页弹窗中配置模型，Ctrl+C 停止）", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
