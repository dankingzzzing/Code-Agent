"""One agent loop shared by CLI and Web, with explicit budgets and observations."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Callable

from .config import Config
from .errors import AgentError, ProviderError
from .memory import ConversationMemory
from .providers import Provider
from .tools import ToolRegistry


@dataclass
class Event:
    step: int
    kind: str
    summary: str
    tool: str | None = None
    arguments: dict | None = None
    ok: bool | None = None


@dataclass
class AgentResult:
    answer: str
    status: str
    provider: str
    steps: int
    tool_calls: int
    events: list[Event] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class CodeReviewAgent:
    def __init__(self, provider: Provider, tools: ToolRegistry, memory: ConversationMemory,
                 config: Config):
        self.provider, self.tools, self.memory, self.config = provider, tools, memory, config

    def run(self, task: str, target: str = ".", *, on_event: Callable[[Event], None] | None = None) -> AgentResult:
        if not isinstance(task, str) or not task.strip() or len(task) > 4000:
            raise AgentError("请求必须是 1 至 4000 个字符。")
        resolved = self.tools.resolve(target)
        normalized = resolved.relative_to(self.tools.root).as_posix()
        user = {"role": "user", "content": json.dumps({"task": task.strip(), "target": normalized}, ensure_ascii=False)}
        messages = self.memory.messages()
        turn = [user]
        messages.append(user)
        events = []
        calls_used = 0

        def emit(step, kind, summary, **details):
            event = Event(step, kind, summary, **details)
            events.append(event)
            if on_event:
                on_event(event)

        emit(0, "input", f"目标 {normalized}；模式 {self.provider.label}")
        status, answer, step = "limit", "", 0
        for step in range(1, self.config.max_steps + 1):
            if len(json.dumps(messages, ensure_ascii=False)) > 120000:
                emit(step, "limit", "达到上下文字符上限，停止调用模型。")
                break
            emit(step, "model", "规划下一步操作 / 根据已有证据生成报告。")
            reply = self.provider.complete(messages, self.tools.schemas())
            if not reply.content and not reply.calls:
                raise ProviderError("模型返回了空响应，请重试或检查模型配置。")
            assistant = reply.message()
            messages.append(assistant)
            turn.append(assistant)
            if not reply.calls:
                answer, status = reply.content, "completed"
                emit(step, "output", "已根据工具证据生成最终报告。")
                break
            if reply.content:
                emit(step, "plan", reply.content[:500])
            budget_hit = False
            for call in reply.calls:
                try:
                    parsed = json.loads(call.arguments)
                    arguments = parsed if isinstance(parsed, dict) else {"invalid": True}
                except (TypeError, ValueError):
                    arguments = {"invalid": True}
                if calls_used >= self.config.max_tool_calls:
                    observation = {"ok": False, "error": "已达到工具调用上限。"}
                    budget_hit = True
                else:
                    emit(step, "tool_call", f"调用 {call.name}", tool=call.name, arguments=arguments)
                    observation = self.tools.execute(call.name, call.arguments)
                    calls_used += 1
                content = json.dumps(observation, ensure_ascii=False)
                if len(content) > 24000:
                    observation = {"ok": False, "error": "工具结果过大，请缩小范围。"}
                    content = json.dumps(observation, ensure_ascii=False)
                message = {"role": "tool", "tool_call_id": call.id, "content": content}
                messages.append(message)
                turn.append(message)
                summary = "工具完成，证据已加入上下文。" if observation["ok"] else observation["error"]
                emit(step, "observation", summary, tool=call.name, ok=observation["ok"])
            if budget_hit:
                emit(step, "limit", "达到工具预算，停止循环。")
                break
        if status == "limit":
            answer = ("# 审查未完成\n\n达到步骤、工具或上下文预算，已停止循环。"
                      f"已执行 {calls_used} 次工具调用。请缩小目标范围，或合理调整 AGENT_MAX_STEPS。"
                      "本轮没有完整审查结论。")
            turn.append({"role": "assistant", "content": answer})
        self.memory.add_turn(turn)
        return AgentResult(answer, status, self.provider.label, step, calls_used, events)
