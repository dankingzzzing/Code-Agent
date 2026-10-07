"""Bounded tools with workspace validation; execution requires an explicit flag."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .analysis import analyze_source
from .errors import ToolError


SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".codeagent", ".qa",
             ".pytest_cache", "dist", "build", "artifacts"}
TEXT_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".h",
                 ".go", ".rs", ".md", ".txt", ".json", ".toml", ".yaml", ".yml", ".html", ".css", ".sql", ".sh"}


def is_private_path(path: Path) -> bool:
    return any(part.lower() in SKIP_DIRS or part.lower().startswith(".env")
               for part in path.parts) or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"} or (
        path.name.lower() in {"id_rsa", "id_ed25519", "credentials", "credentials.json"})


class ToolRegistry:
    def __init__(self, root: Path, *, allow_exec: bool = False, test_timeout: float = 10):
        self.root = root.resolve()
        if not self.root.is_dir():
            raise ToolError("工作区必须是存在的目录。")
        self.allow_exec = allow_exec
        self.test_timeout = test_timeout

    def resolve(self, value: str) -> Path:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ToolError("path 必须是非空路径字符串。")
        supplied = Path(value)
        candidate = supplied if supplied.is_absolute() else self.root / supplied
        try:
            relative = candidate.absolute().relative_to(self.root)
            resolved = candidate.resolve(strict=True)
            real_relative = resolved.relative_to(self.root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ToolError("路径不存在，或路径/符号链接超出了工作区。") from exc
        if is_private_path(relative) or is_private_path(real_relative):
            raise ToolError("该路径属于配置、凭据或内部目录，工具不允许访问。")
        return resolved

    def schemas(self) -> list[dict]:
        descriptions = {
            "list_files": "列出目标目录中的代码和文本文件，最多 100 个。path 必须位于工作区内。",
            "read_file": "读取 UTF-8 文本并附带行号。最多 64 KiB，不读取凭据文件。",
            "analyze_python": "静态解析一个 Python 文件，返回语法、函数和带行号的风险发现。",
        }
        if self.allow_exec:
            descriptions["run_tests"] = "执行可信的 unittest 测试文件或测试目录，返回退出码和输出。"
        return [{"type": "function", "function": {
            "name": name, "description": description, "strict": True,
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                           "required": ["path"], "additionalProperties": False}}}
                for name, description in descriptions.items()]

    def execute(self, name: str, arguments: str) -> dict:
        """Return tool failures as observations so the agent can recover."""
        try:
            if name not in {s["function"]["name"] for s in self.schemas()}:
                raise ToolError(f"工具 {name} 不可用。测试执行需启动时显式开启 --allow-exec。")
            try:
                args = json.loads(arguments)
            except (ValueError, TypeError) as exc:
                raise ToolError("工具参数必须是有效 JSON。") from exc
            if not isinstance(args, dict) or set(args) != {"path"}:
                raise ToolError("工具参数必须是仅包含 path 的 JSON 对象。")
            path = self.resolve(args["path"])
            result = getattr(self, name)(path)
            return {"ok": True, "data": result}
        except (ToolError, OSError, UnicodeError, ValueError) as exc:
            return {"ok": False, "error": str(exc), "tool": name}

    def list_files(self, path: Path, max_files: int = 100) -> dict:
        if path.is_file():
            return {"files": [path.relative_to(self.root).as_posix()], "truncated": False}
        paths, examined = [], 0
        pending = [path]
        while pending:
            directory = pending.pop()
            for entry in sorted(directory.iterdir(), key=lambda p: p.name):
                examined += 1
                if examined > 5000 or len(paths) >= max_files:
                    return {"files": sorted(paths), "truncated": True}
                if is_private_path(entry.relative_to(self.root)) or entry.is_symlink():
                    continue
                try:
                    safe = self.resolve(str(entry))
                except ToolError:
                    continue
                if safe.is_dir():
                    pending.append(safe)
                elif safe.is_file() and safe.suffix.lower() in TEXT_SUFFIXES:
                    paths.append(safe.relative_to(self.root).as_posix())
        return {"files": sorted(set(paths)), "truncated": False}

    def _source(self, path: Path) -> str:
        if not path.is_file():
            raise ToolError("请选择一个文本文件。")
        with path.open("rb") as handle:
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise ToolError("文件超过 64 KiB，请缩小审查范围。")
        if b"\x00" in raw:
            raise ToolError("不支持读取二进制文件。")
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ToolError("文件必须使用 UTF-8 编码。") from exc

    def read_file(self, path: Path) -> dict:
        source = self._source(path)
        numbered = "\n".join(f"{i:4d} | {line}" for i, line in enumerate(source.splitlines(), 1))
        return {"path": path.relative_to(self.root).as_posix(), "content": numbered[:16000],
                "lines": len(source.splitlines()), "truncated": len(numbered) > 16000}

    def analyze_python(self, path: Path) -> dict:
        if path.suffix.lower() != ".py":
            raise ToolError("analyze_python 只接受 .py 文件；其他语言可使用 read_file。")
        return analyze_source(self._source(path), path.relative_to(self.root).as_posix())

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=5)

    def run_tests(self, path: Path) -> dict:
        if not self.allow_exec:
            raise ToolError("测试执行未开启。")
        pattern, directory = "test*.py", path
        if path.is_file():
            if not path.name.startswith("test") or path.suffix != ".py":
                raise ToolError("执行单个文件时必须选择 test*.py。")
            pattern, directory = path.name, path.parent
        environment = {key: value for key, value in os.environ.items() if key.upper() in {
            "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "LANG", "LC_ALL"}}
        environment.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
        command = [sys.executable, "-m", "unittest", "discover", "-s", str(directory),
                   "-p", pattern, "-v"]
        started = time.monotonic()
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(command, cwd=self.root, env=environment,
                                       stdout=output, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, start_new_session=os.name != "nt")
            reason = ""
            try:
                while process.poll() is None:
                    if time.monotonic() - started > self.test_timeout:
                        reason = "timeout"
                        break
                    if os.fstat(output.fileno()).st_size > 2 * 1024 * 1024:
                        reason = "output_limit"
                        break
                    time.sleep(0.03)
            except BaseException:
                if process.poll() is None:
                    self._stop(process)
                raise
            if reason:
                self._stop(process)
            output.seek(0)
            raw = output.read(16001)
        text = raw[:16000].decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        summary = re.search(r"Ran (\d+) tests? in", text)
        count = int(summary.group(1)) if summary else None
        if not reason and count == 0:
            reason = "no_tests"
        return {"path": path.relative_to(self.root).as_posix(), "exit_code": process.returncode,
                "passed": process.returncode == 0 and not reason and count is not None,
                "tests_run": count,
                "timed_out": reason == "timeout", "stop_reason": reason or "completed",
                "output": text,
                "truncated": len(raw) > 16000,
                "duration_seconds": round(time.monotonic() - started, 3)}
