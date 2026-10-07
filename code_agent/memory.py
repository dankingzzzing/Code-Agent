"""Whole-turn memory and atomic local persistence, scoped to a workspace."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path

from .errors import MemoryError


class ConversationMemory:
    def __init__(self, system_prompt: str, *, max_turns: int = 6, max_chars: int = 120000):
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.max_chars = max_chars
        self.turns: list[list[dict]] = []

    def messages(self) -> list[dict]:
        return [{"role": "system", "content": self.system_prompt},
                *copy.deepcopy([message for turn in self.turns for message in turn])]

    def add_turn(self, turn: list[dict]) -> None:
        if not turn or turn[0].get("role") != "user" or turn[-1].get("role") != "assistant":
            raise MemoryError("会话轮次必须包含完整的用户请求和最终回答。")
        self.turns.append(copy.deepcopy(turn))
        while len(self.turns) > self.max_turns or (
                len(self.turns) > 1 and len(json.dumps(self.turns, ensure_ascii=False)) > self.max_chars):
            self.turns.pop(0)

    def save(self, path: Path, root: Path, provider: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {"version": 1, "root": str(root.resolve()), "provider": provider, "turns": self.turns}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                             suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(document, handle, ensure_ascii=False, indent=2)
            os.replace(temporary, path)
        except OSError as exc:
            if temporary:
                temporary.unlink(missing_ok=True)
            raise MemoryError("无法保存会话文件，请检查目录权限。") from exc

    def load(self, path: Path, root: Path, provider: str) -> None:
        if not path.exists():
            return
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("oversized session")
            document = json.loads(path.read_text(encoding="utf-8"))
            if document["version"] != 1 or document["root"] != str(root.resolve()):
                raise ValueError("workspace mismatch")
            if document["provider"] != provider:
                raise ValueError("provider mismatch")
            turns = document["turns"]
            if not isinstance(turns, list):
                raise ValueError("invalid turns")
            candidate = ConversationMemory(self.system_prompt, max_turns=self.max_turns,
                                           max_chars=self.max_chars)
            for turn in turns:
                if not isinstance(turn, list) or any(not isinstance(m, dict) for m in turn):
                    raise ValueError("invalid messages")
                if any(m.get("role") not in {"user", "assistant", "tool"} for m in turn):
                    raise ValueError("invalid role")
                candidate.add_turn(turn)
            self.turns = candidate.turns
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, MemoryError) as exc:
            raise MemoryError("会话文件损坏、工作区/模型模式不匹配。请更换 --session 名称。") from exc


def session_path(root: Path, session: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session):
        raise MemoryError("会话名只能包含 1 至 64 个英文字母、数字、下划线或连字符。")
    base = root.resolve() / ".codeagent" / "sessions"
    candidate = base / f"{session}.json"
    try:
        candidate.resolve().relative_to(root.resolve())
    except (ValueError, OSError, RuntimeError) as exc:
        raise MemoryError("会话目录超出了工作区。") from exc
    return candidate
