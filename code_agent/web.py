"""Local Web UI with protected remembered settings, live progress and desktop selections."""

from __future__ import annotations

import json
import secrets
import threading
import uuid
from dataclasses import asdict, dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .agent import CodeReviewAgent
from .app import create_agent
from .config import Config
from .errors import AgentError, ConfigError, ProviderError
from .file_picker import pick_local_path
from .providers import LLMProvider
from .settings_store import SettingsStore
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
    remembered: bool = False


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
    store = SettingsStore(root)
    remembered_settings = None
    try:
        remembered_settings = store.load()
        if remembered_settings:
            defaults = remembered_settings[0]
    except ConfigError as exc:
        bootstrap_error = str(exc)
    token = secrets.token_urlsafe(32)
    profiles: dict[str, BrowserProfile] = {}
    sessions: dict[str, WebSession] = {}
    state_lock = threading.Lock()
    picker_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _send(self, status, body, content_type="application/json; charset=utf-8", cookie=None):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if cookie:
                self.send_header("Set-Cookie", f"code_agent_profile={cookie}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000")
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
                cookies = SimpleCookie()
                try:
                    cookies.load(self.headers.get("Cookie", ""))
                except Exception:
                    pass
                profile_id = cookies["code_agent_profile"].value if "code_agent_profile" in cookies else None
                with state_lock:
                    profile = profiles.get(profile_id)
                    if profile is None and remembered_settings:
                        profile_id = uuid.uuid4().hex
                        profile = BrowserProfile(remembered_settings[0], remembered_settings[1], remembered=True)
                        profiles[profile_id] = profile
                    current = profile.config if profile else defaults
                current_demo = profile.demo if profile else demo
                label = ("离线演示 · 规则规划器（非 LLM）" if current_demo else f"LLM · {current.model}") if profile else "请先配置模型"
                return self._send(200, {"mode": label, "demo": current_demo, "allow_exec": allow_exec,
                    "csrf_token": token, "api_key_configured": bool(current.api_key),
                    "model": current.model, "base_url": current.base_url, "api_style": current.api_style,
                    "profile": profile_id if profile else None, "configured": bool(profile),
                    "remembered": profile.remembered if profile else False,
                    "root": str(root), "bootstrap_error": bootstrap_error}, cookie=profile_id if profile else None)
            routes = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/markdown.js": ("markdown.js", "text/javascript; charset=utf-8"),
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
            nonlocal remembered_settings
            if set(payload) - {"profile", "api_key", "model", "base_url", "api_style", "demo", "verify", "remember"}:
                raise AgentError("模型配置字段无效。")
            demo_mode = payload.get("demo", False)
            verify = payload.get("verify", False)
            remember = payload.get("remember", True)
            if not isinstance(demo_mode, bool) or not isinstance(verify, bool) or not isinstance(remember, bool):
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
            remembered = remember and (demo_mode or verify)
            if remembered:
                store.save(config, demo_mode)
                remembered_settings = (config, demo_mode)
            elif not remember:
                store.clear()
                remembered_settings = None
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
                profiles[profile_id].remembered = remembered
            label = "离线演示 · 规则规划器（非 LLM）" if demo_mode else f"LLM · {config.model}"
            return {"profile": profile_id, "mode": label, "demo": demo_mode,
                    "api_style": config.resolved_api_style, "api_key_configured": bool(config.api_key),
                    "base_url": config.base_url, "model": config.model, "connection": connection,
                    "remembered": remembered}

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

        def _review(self, payload, on_event=None, on_text=None):
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
                result = record.agent.run(payload.get("task"), payload.get("target", selected.target),
                                          on_event=on_event, on_text=on_text)
            except Exception:
                if payload.get("session") is None:
                    with state_lock:
                        sessions.pop(session_id, None)
                raise
            finally:
                record.lock.release()
            return 200, {**result.to_dict(), "session": session_id, "workspace": str(selected.root)}

        def _stream_review(self, payload):
            # Validate the profile before sending a successful streaming response.
            self._profile(payload)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            def send(kind, data):
                self.wfile.write((json.dumps({"type": kind, "data": data}, ensure_ascii=False) + "\n").encode())
                self.wfile.flush()

            try:
                status, result = self._review(payload,
                    on_event=lambda event: send("event", asdict(event)),
                    on_text=lambda value: send("text", value))
                send("result" if status == 200 else "error", result)
            except (BrokenPipeError, ConnectionResetError):
                return
            except (AgentError, ValueError, OSError) as exc:
                try:
                    send("error", {"error": str(exc), "needs_settings": isinstance(exc, ProviderError)
                                    and exc.http_status in {401, 403}})
                except (BrokenPipeError, ConnectionResetError):
                    return

        def do_POST(self):
            if not self._valid_host() or not secrets.compare_digest(
                    self.headers.get("X-Agent-Token", "").encode("utf-8"), token.encode("ascii")):
                return self._send(403, {"error": "请求验证失败，请刷新本机页面。"})
            if self.path not in {"/api/settings", "/api/select", "/api/pick", "/api/review", "/api/review/stream"}:
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
                    return self._send(status, body, cookie=body["profile"])
                elif self.path in {"/api/select", "/api/pick"}:
                    status, body = self._select(payload, native=self.path == "/api/pick")
                elif self.path == "/api/review/stream":
                    return self._stream_review(payload)
                else:
                    status, body = self._review(payload)
                return self._send(status, body)
            except (AgentError, ValueError, UnicodeError) as exc:
                return self._send(400, {"error": str(exc), "needs_settings": isinstance(exc, ProviderError)
                                        and exc.http_status in {401, 403}})
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
