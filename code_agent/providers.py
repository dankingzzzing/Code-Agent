"""Native HTTP LLM adapters and an explicitly labelled deterministic demo."""

from __future__ import annotations

import json
import random
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import Config
from .errors import ProviderError


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str

    def wire(self) -> dict:
        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": self.arguments}}


@dataclass
class Reply:
    content: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    provider_items: list[dict] | None = None
    provider_fields: dict = field(default_factory=dict)

    def message(self) -> dict:
        message = {"role": "assistant", "content": self.content or None, **self.provider_fields}
        if self.calls:
            message["tool_calls"] = [call.wire() for call in self.calls]
        if self.provider_items is not None:
            message["_provider_items"] = self.provider_items
        return message


class Provider(Protocol):
    identity: str
    label: str

    def complete(self, messages: list[dict], tools: list[dict]) -> Reply: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LLMProvider:
    """No SDK or framework dependency; accepts two explicit API protocols."""

    def __init__(self, config: Config, *, sleeper=time.sleep):
        config.require_llm()
        self.config = replace(config, api_style=config.resolved_api_style)
        self.identity = f"llm:{self.config.api_style}:{config.base_url}:{config.model}"
        self.label = f"LLM · {config.model}"
        self.sleeper = sleeper
        self.opener = build_opener(_NoRedirect())

    def _post(self, endpoint: str, payload: dict) -> dict:
        request = Request(f"{self.config.base_url}/{endpoint}",
                          data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers={"Content-Type": "application/json",
                                   "Authorization": f"Bearer {self.config.api_key}"}, method="POST")
        for attempt in range(self.config.max_retries + 1):
            try:
                with self.opener.open(request, timeout=self.config.timeout) as response:
                    raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ProviderError("模型响应超过 2 MiB 限制。")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ProviderError("模型服务返回了无效的 JSON 对象。")
                return value
            except HTTPError as exc:
                retryable = exc.code in {408, 429, 500, 502, 503, 504}
                if not retryable or attempt == self.config.max_retries:
                    hints = {401: "请检查 API key。", 403: "请检查模型和接口权限。",
                             404: "请检查 LLM_BASE_URL、模型名和 LLM_API_STYLE。",
                             400: "请检查模型是否支持所选 API 和 function calling。"}
                    detail = self._error_detail(exc)
                    hint = hints.get(exc.code, "模型服务暂不可用，重试次数已用完。")
                    raise ProviderError(f"LLM HTTP {exc.code}。{hint}{detail}", http_status=exc.code) from exc
                try:
                    delay = max(0, min(float(exc.headers.get("Retry-After", "0")), 5))
                except (ValueError, TypeError):
                    delay = 0
                exc.close()
                self.sleeper(max(delay, min(0.5 * 2 ** attempt, 5)) + random.uniform(0, 0.1))
            except (URLError, TimeoutError, OSError) as exc:
                if attempt == self.config.max_retries:
                    raise ProviderError("无法连接模型服务或请求超时，重试次数已用完。") from exc
                self.sleeper(min(0.5 * 2 ** attempt, 5) + random.uniform(0, 0.1))
            except (ValueError, UnicodeError) as exc:
                raise ProviderError("模型服务返回的内容不是有效 UTF-8 JSON。") from exc
        raise ProviderError("LLM 请求失败。")

    def _error_detail(self, error: HTTPError) -> str:
        try:
            payload = json.loads(error.read(4096))
            value = payload.get("error", payload)
            detail = value.get("message", value.get("msg", "")) if isinstance(value, dict) else value
            if not isinstance(detail, str):
                return ""
            detail = detail.replace(self.config.api_key, "[已隐藏密钥]")
            detail = re.sub(r"(?i)bearer\s+\S+", "Bearer [已隐藏密钥]", detail)
            return " 服务提示：" + " ".join(detail.split())[:400] if detail else ""
        except (OSError, ValueError, AttributeError):
            return ""
        finally:
            error.close()

    def check_connection(self) -> dict:
        """Check the actual tool call and tool-result round trip, without local file access."""
        tool = {"type": "function", "function": {"name": "connection_check",
                "description": "Check the connection. Call this once with value OK.", "strict": True,
                "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                               "required": ["value"], "additionalProperties": False}}}
        messages = [{"role": "system", "content": "This is a connection test. You must call connection_check once with value OK, then reply OK after its result. Do not use other tools."},
                    {"role": "user", "content": "Call connection_check now."}]
        reply = self.stream_complete(messages, [tool], lambda text: None, tool_choice="connection_check")
        if len(reply.calls) != 1 or reply.calls[0].name != "connection_check":
            raise ProviderError("模型未返回验证工具调用。请检查该模型是否支持工具调用，或切换 API 协议。")
        call = reply.calls[0]
        try:
            if json.loads(call.arguments) != {"value": "OK"}:
                raise ValueError("unexpected arguments")
        except ValueError as exc:
            raise ProviderError("模型返回的验证工具参数无效。") from exc
        messages.extend([reply.message(), {"role": "tool", "tool_call_id": call.id, "content": "OK"}])
        final = self.stream_complete(messages, [tool], lambda text: None, tool_choice="none")
        if not final.content or final.calls:
            raise ProviderError("模型没有完成验证工具结果的处理，请检查模型或 API 协议。")
        return {"connected": True, "tool_calling": True, "api_style": self.config.api_style,
                "model": self.config.model}

    def complete(self, messages: list[dict], tools: list[dict]) -> Reply:
        if self.config.api_style == "responses":
            return self._responses(messages, tools)
        return self._chat(messages, tools)

    def stream_complete(self, messages: list[dict], tools: list[dict], on_text: Callable[[str], None],
                        *, on_activity: Callable[[], None] | None = None, tool_choice: str | None = None) -> Reply:
        """Stream public answer text; keep provider reasoning private to protocol state."""
        try:
            return self._stream_complete(messages, tools, on_text, on_activity, tool_choice)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise ProviderError("模型流式响应格式无效。") from exc

    def _stream_complete(self, messages, tools, on_text, on_activity, tool_choice):
        if self.config.api_style == "responses":
            payload = self._responses_payload(messages, tools)
            if tool_choice:
                payload["tool_choice"] = (tool_choice if tool_choice in {"none", "auto", "required"}
                                          else {"type": "function", "name": tool_choice})
            payload["stream"] = True
            final = None
            for event in self._stream_events("responses", payload):
                if on_activity:
                    on_activity()
                if event.get("type") == "response.output_text.delta":
                    delta = event.get("delta", "")
                    if not isinstance(delta, str):
                        raise ValueError("invalid text delta")
                    on_text(delta)
                elif event.get("type") == "response.completed":
                    final = event["response"]
                elif event.get("type") in {"error", "response.failed", "response.incomplete"}:
                    raise ProviderError("模型流式响应失败或未完整生成。")
            if final is None:
                raise ProviderError("模型流中缺少完成事件。")
            return self._parse_responses(final)
        payload = self._chat_payload(messages, tools)
        if tool_choice:
            payload["tool_choice"] = (tool_choice if tool_choice in {"none", "auto", "required"}
                                      else {"type": "function", "function": {"name": tool_choice}})
        payload["stream"] = True
        text, reasoning, calls = [], [], {}
        finished = False
        for event in self._stream_events("chat/completions", payload):
            if on_activity:
                on_activity()
            if event.get("error"):
                raise ProviderError("模型流式响应返回了服务错误。")
            for choice in event.get("choices", []):
                finish = choice.get("finish_reason")
                if finish in {"length", "content_filter"}:
                    raise ProviderError("模型流式输出被截断或过滤。")
                finished = finished or finish is not None
                delta = choice.get("delta", {})
                if isinstance(delta.get("content"), str):
                    text.append(delta["content"])
                    on_text(delta["content"])
                if isinstance(delta.get("reasoning_content"), str):
                    reasoning.append(delta["reasoning_content"])
                for fragment in delta.get("tool_calls") or []:
                    index = fragment["index"]
                    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < 12:
                        raise ValueError("invalid tool index")
                    call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    call["id"] += fragment.get("id") or ""
                    function = fragment.get("function", {})
                    call["name"] += function.get("name") or ""
                    call["arguments"] += function.get("arguments") or ""
        if not finished:
            raise ProviderError("模型流提前断开，未获得完整报告。")
        assembled = [ToolCall(**calls[i]) for i in sorted(calls)]
        self._validate_calls(assembled)
        return Reply("".join(text), assembled, provider_fields={"reasoning_content": "".join(reasoning)} if reasoning else {})

    def _stream_events(self, endpoint: str, payload: dict):
        request = Request(f"{self.config.base_url}/{endpoint}", data=json.dumps(payload, ensure_ascii=False).encode(),
                          headers={"Content-Type": "application/json", "Accept": "text/event-stream",
                                   "Authorization": f"Bearer {self.config.api_key}"}, method="POST")
        response = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self.opener.open(request, timeout=self.config.timeout)
                break
            except HTTPError as exc:
                retry = exc.code in {408, 429, 500, 502, 503, 504} and attempt < self.config.max_retries
                if not retry:
                    detail = self._error_detail(exc)
                    raise ProviderError(f"LLM HTTP {exc.code}。请检查密钥、模型及协议。{detail}", http_status=exc.code) from exc
                exc.close()
                self.sleeper(min(.5 * 2 ** attempt, 5))
            except (URLError, TimeoutError, OSError) as exc:
                if attempt == self.config.max_retries:
                    raise ProviderError("无法连接模型服务或请求超时。") from exc
                self.sleeper(min(.5 * 2 ** attempt, 5))
        total, data = 0, []
        try:
            with response:
                for raw in response:
                    total += len(raw)
                    if total > 4 * 1024 * 1024 or len(raw) > 1024 * 1024:
                        raise ProviderError("模型流超过输出大小上限。")
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        value = "\n".join(data)
                        data = []
                        if value == "[DONE]":
                            break
                        parsed = json.loads(value)
                        if not isinstance(parsed, dict):
                            raise ValueError("invalid stream event")
                        yield parsed
        except (OSError, ValueError, UnicodeError) as exc:
            raise ProviderError("模型流传输中断或响应格式无效。") from exc

    @staticmethod
    def _validate_calls(calls: list[ToolCall]) -> None:
        if len(calls) > 12 or len({call.id for call in calls}) != len(calls):
            raise ProviderError("模型返回了过多或重复 ID 的工具调用。")
        for call in calls:
            if (not isinstance(call.id, str) or not call.id or not isinstance(call.name, str)
                    or not call.name or not isinstance(call.arguments, str)
                    or len(call.arguments) > 20000):
                raise ProviderError("模型返回了无效的工具调用格式。")

    def _chat_payload(self, messages: list[dict], tools: list[dict]) -> dict:
        cleaned = [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]
        payload = {"model": self.config.model, "messages": cleaned}
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        return payload

    def _chat(self, messages: list[dict], tools: list[dict]) -> Reply:
        value = self._post("chat/completions", self._chat_payload(messages, tools))
        try:
            choice = value["choices"][0]
            if choice.get("finish_reason") in {"length", "content_filter"}:
                raise ProviderError("模型输出被截断或过滤，无法完成本轮审查。")
            message = choice["message"]
            content = message.get("content") or message.get("refusal") or ""
            if not isinstance(content, str):
                raise ValueError("invalid content")
            raw_calls = message.get("tool_calls") or []
            if not isinstance(raw_calls, list) or any(c.get("type") != "function" for c in raw_calls):
                raise ValueError("invalid calls")
            calls = [ToolCall(c["id"], c["function"]["name"], c["function"]["arguments"]) for c in raw_calls]
            self._validate_calls(calls)
            fields = {}
            if isinstance(message.get("reasoning_content"), str):
                fields["reasoning_content"] = message["reasoning_content"]
            return Reply(content=content, calls=calls, provider_fields=fields)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise ProviderError("Chat Completions 响应格式无效。") from exc

    def _responses_payload(self, messages: list[dict], tools: list[dict]) -> dict:
        inputs = []
        for message in messages:
            if message["role"] == "system":
                continue
            if message["role"] == "tool":
                inputs.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                               "output": message["content"]})
            elif message.get("_provider_items"):
                inputs.extend(message["_provider_items"])
            elif message["role"] == "assistant" and message.get("tool_calls"):
                raise ProviderError("会话包含另一种 API 的工具调用，请使用新的会话名。")
            else:
                inputs.append({"role": message["role"], "content": message.get("content") or ""})
        native_tools = [{"type": "function", **tool["function"]} for tool in tools]
        payload = {"model": self.config.model, "instructions": messages[0]["content"],
                   "input": inputs, "tools": native_tools, "tool_choice": "auto", "store": False,
                   "include": ["reasoning.encrypted_content"]}
        return payload

    def _responses(self, messages: list[dict], tools: list[dict]) -> Reply:
        return self._parse_responses(self._post("responses", self._responses_payload(messages, tools)))

    def _parse_responses(self, value: dict) -> Reply:
        if value.get("status") in {"failed", "incomplete", "cancelled"} or value.get("error"):
            raise ProviderError("Responses API 未完整生成结果。请检查模型、输入长度或服务状态。")
        try:
            items = value["output"]
            if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
                raise ValueError("invalid output")
            calls = [ToolCall(i["call_id"], i["name"], i["arguments"])
                     for i in items if i.get("type") == "function_call"]
            content = []
            for item in items:
                if item.get("type") == "message":
                    for part in item.get("content", []):
                        if part.get("type") == "output_text":
                            content.append(part["text"])
                        elif part.get("type") == "refusal":
                            content.append(part["refusal"])
            self._validate_calls(calls)
            return Reply(content="\n".join(content), calls=calls, provider_items=items)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ProviderError("Responses API 响应格式无效。") from exc


class DemoProvider:
    """A rule-based tool planner, never presented as a real LLM."""

    identity = "demo:v1"
    label = "离线演示 · 规则规划器（非 LLM）"

    @staticmethod
    def _call(name: str, path: str) -> ToolCall:
        return ToolCall("demo_" + uuid.uuid4().hex[:16], name,
                        json.dumps({"path": path}, ensure_ascii=False))

    def complete(self, messages: list[dict], tools: list[dict]) -> Reply:
        start = max(i for i, m in enumerate(messages) if m["role"] == "user")
        request = json.loads(messages[start]["content"])
        observations = []
        pending = {}
        for message in messages[start + 1:]:
            for call in message.get("tool_calls", []):
                pending[call["id"]] = (call["function"]["name"], json.loads(call["function"]["arguments"])["path"])
            if message["role"] == "tool":
                name, path = pending[message["tool_call_id"]]
                observations.append((name, path, json.loads(message["content"])))
        if not observations:
            return Reply("先列出目标文件，定位可审查的代码。", [self._call("list_files", request["target"])])
        listing = next((o for n, _, o in observations if n == "list_files"), {})
        if not listing.get("ok"):
            return Reply(f"无法列出目标文件：{listing.get('error', '未知错误')}。请检查路径。")
        files = listing["data"]["files"]
        python_files = [p for p in files if p.endswith(".py")]
        selected = sorted(python_files, key=lambda p: (PurePosixPath(p).name.startswith("test"), p))[:6]
        if not selected:
            if files and not any(n == "read_file" for n, _, _ in observations):
                return Reply("读取文本以确认目标类型。", [self._call("read_file", files[0])])
            return Reply("# 离线审查报告\n\n目标中没有 Python 文件。离线演示仅支持 Python 静态规则，"
                         "其他语言和任意自然语言问答请使用真实 LLM 模式。未运行测试。")
        completed = {(n, p) for n, p, _ in observations}
        for path in selected:
            calls = [self._call(n, path) for n in ("read_file", "analyze_python") if (n, path) not in completed]
            if calls:
                return Reply(f"读取并静态分析 {path}，获取行号和风险证据。", calls)
        wants_tests = bool(re.search(r"测试|验证|test|verify", request["task"], flags=re.I))
        enabled = any(t["function"]["name"] == "run_tests" for t in tools)
        if wants_tests and enabled and not any(n == "run_tests" for n, _, _ in observations):
            target = request["target"]
            directory = str(PurePosixPath(selected[0]).parent) if target.endswith(".py") else target
            return Reply("运行已授权的 unittest 测试，验证运行时行为。", [self._call("run_tests", directory)])
        return Reply(self._report(request, observations, len(python_files) > len(selected), listing["data"].get("truncated", False)))

    @staticmethod
    def _report(request: dict, observations: list, capped: bool, listing_capped: bool) -> str:
        analyzed = [o["data"] for n, _, o in observations if n == "analyze_python" and o.get("ok")]
        findings = [f for item in analyzed for f in item["findings"]]
        lines = ["# 代码审查报告", "", "> 离线演示：规则规划器 + AST 工具，未调用真实 LLM。", "",
                 "## 审查范围", "", f"目标：`{request['target']}`。分析了 {len(analyzed)} 个 Python 文件。", "",
                 "## 发现与证据", ""]
        for index, finding in enumerate(findings, 1):
            severity = {"high": "高", "medium": "中", "low": "低"}[finding["severity"]]
            lines += [f"### {index}. [{severity}] {finding['title']}", "",
                      f"位置：`{finding['path']}:{finding['line']}`，规则 `{finding['rule']}`。", "",
                      finding["detail"], "", f"建议：{finding['suggestion']}", ""]
        if not findings:
            lines += ["所覆盖的静态规则没有发现风险。此结果不保证程序正确。", ""]
        errors = [(n, p, o["error"]) for n, p, o in observations if not o.get("ok")]
        if errors:
            lines += ["## 工具错误", ""] + [f"- `{n}` / `{p}`：{e}" for n, p, e in errors] + [""]
        lines += ["## 修复与边界测试建议", ""]
        if any(f["rule"] == "mutable-default" for f in findings):
            lines += ["可变默认参数修复示例：", "", "```python", "def append_item(item, items=None):",
                      "    if items is None:", "        items = []", "    items.append(item)",
                      "    return items", "```", "", "测试两次独立调用，确认返回列表不会共享上次的内容。", ""]
        lines += ["覆盖空输入、零值、非法类型和异常分支；结合业务语义确认风险并补充回归测试。", "",
                  "## 测试状态", ""]
        tests = [o["data"] for n, _, o in observations if n == "run_tests" and o.get("ok")]
        if tests:
            for test in tests:
                state = "通过" if test["passed"] else "失败或未完成"
                lines += [f"`{test['path']}`：{state}，退出码 {test['exit_code']}，"
                          f"停止原因 `{test['stop_reason']}`。", "", "```text", test["output"].strip(), "```", ""]
        else:
            lines += ["未运行测试。执行可信测试需 --allow-exec，且请求中包含“测试”或“验证”。", ""]
        lines += ["## 限制", "", "静态规则只能覆盖预设风险，动态执行和反序列化发现需要人工核查输入来源。",
                  "离线模式不理解任意业务语义；真实 LLM 模式可综合代码、测试和上下文给出进一步建议。"]
        if capped or listing_capped:
            lines.append("本次达到演示文件数量/目录扫描上限，请缩小目标范围继续审查。")
        return "\n".join(lines)
