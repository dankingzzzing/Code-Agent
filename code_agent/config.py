"""Minimal configuration loader. It never executes .env contents."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .errors import ConfigError


def load_env(path: Path) -> dict[str, str]:
    """Read KEY=value pairs; existing process variables win in from_env()."""
    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ConfigError("无法读取 UTF-8 .env 配置文件。") from exc
    values = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            raise ConfigError(".env 每个配置项都必须使用 KEY=value 格式。")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or not key.replace("_", "a").isalnum() or key[0].isdigit():
            raise ConfigError(".env 含无效的变量名。")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ConfigError(f".env 配置 {key} 的引号未闭合。")
            value = value[1:-1]
        values[key] = value
    return values


@dataclass(frozen=True)
class Config:
    api_key: str = field(default="", repr=False)
    model: str = ""
    base_url: str = "https://api.openai.com/v1"
    api_style: str = "responses"
    timeout: float = 45
    max_retries: int = 2
    max_steps: int = 12
    max_tool_calls: int = 32
    memory_turns: int = 6
    test_timeout: float = 10

    @classmethod
    def from_env(cls, root: Path) -> "Config":
        values = {**load_env(root / ".env"), **os.environ}

        def number(name: str, default: int | float, low: float, high: float):
            try:
                value = type(default)(values.get(name, default))
            except (ValueError, TypeError) as exc:
                raise ConfigError(f"{name} 必须是数值。") from exc
            if not low <= value <= high:
                raise ConfigError(f"{name} 必须在 {low:g} 至 {high:g} 之间。")
            return value

        style = values.get("LLM_API_STYLE", "responses").strip()
        if style not in {"responses", "chat_completions"}:
            raise ConfigError("LLM_API_STYLE 只能是 responses 或 chat_completions。")
        base_url = values.get("LLM_BASE_URL", cls.base_url).strip().rstrip("/")
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ConfigError("LLM_BASE_URL 必须是不含凭据、查询参数的 HTTP(S) 基础地址。")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ConfigError("远程模型服务必须使用 HTTPS；HTTP 仅支持本机服务。")
        return cls(
            api_key=values.get("LLM_API_KEY", values.get("OPENAI_API_KEY", "")).strip(),
            model=values.get("LLM_MODEL", "").strip(),
            base_url=base_url,
            api_style=style,
            timeout=number("LLM_TIMEOUT", 45.0, 1, 300),
            max_retries=number("LLM_MAX_RETRIES", 2, 0, 5),
            max_steps=number("AGENT_MAX_STEPS", 12, 1, 30),
            max_tool_calls=number("AGENT_MAX_TOOL_CALLS", 32, 1, 100),
            memory_turns=number("AGENT_MEMORY_TURNS", 6, 1, 20),
            test_timeout=number("AGENT_TEST_TIMEOUT", 10.0, 0.1, 60),
        )

    def require_llm(self) -> None:
        if not self.api_key or not self.model:
            raise ConfigError("真实 LLM 模式需要 LLM_API_KEY 和 LLM_MODEL。可先使用 --demo 离线演示。")
