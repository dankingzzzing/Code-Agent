"""Composition root: interfaces use the same agent, tools and memory."""

from __future__ import annotations

from pathlib import Path

from .agent import CodeReviewAgent
from .config import Config
from .memory import ConversationMemory, session_path
from .prompts import SYSTEM_PROMPT
from .providers import DemoProvider, LLMProvider
from .tools import ToolRegistry


def create_agent(root: Path, *, demo: bool, allow_exec: bool = False,
                 session: str | None = None) -> CodeReviewAgent:
    root = root.resolve()
    config = Config.from_env(root)
    provider = DemoProvider() if demo else LLMProvider(config)
    memory = ConversationMemory(SYSTEM_PROMPT, max_turns=config.memory_turns)
    tools = ToolRegistry(root, allow_exec=allow_exec, test_timeout=config.test_timeout)
    if session:
        memory.load(session_path(root, session), root, provider.identity)
    return CodeReviewAgent(provider, tools, memory, config)


def save_session(agent: CodeReviewAgent, session: str | None) -> None:
    if session:
        agent.memory.save(session_path(agent.tools.root, session), agent.tools.root, agent.provider.identity)
