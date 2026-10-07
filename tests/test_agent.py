import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from code_agent.agent import CodeReviewAgent
from code_agent.config import Config
from code_agent.errors import AgentError, ProviderError
from code_agent.memory import ConversationMemory
from code_agent.prompts import SYSTEM_PROMPT
from code_agent.providers import DemoProvider, Reply, ToolCall
from code_agent.tools import ToolRegistry


class ScriptedProvider:
    identity = "test"
    label = "test"

    def __init__(self, replies):
        self.replies = iter(replies)
        self.inputs = []

    def complete(self, messages, tools):
        self.inputs.append(list(messages))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "example.py").write_text("def append(x, items=[]):\n    items.append(x)\n    return items\n")
        self.memory = ConversationMemory(SYSTEM_PROMPT)

    def tearDown(self):
        self.temp.cleanup()

    def agent(self, provider, config=None):
        return CodeReviewAgent(provider, ToolRegistry(self.root), self.memory, config or Config())

    def test_observation_and_history_return_to_model(self):
        call = ToolCall("c1", "read_file", '{"path":"example.py"}')
        provider = ScriptedProvider([Reply("读取文件", [call]), Reply("最终报告"), Reply("追问回答")])
        agent = self.agent(provider)
        result = agent.run("审查", "example.py")
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(result.status, "completed")
        self.assertEqual(provider.inputs[1][-1]["role"], "tool")
        self.assertTrue(json.loads(provider.inputs[1][-1]["content"])["ok"])
        agent.run("解释刚才的问题", "example.py")
        self.assertIn("最终报告", [m.get("content") for m in provider.inputs[2]])
        self.assertEqual(len(self.memory.turns), 2)

    def test_tool_error_is_recoverable(self):
        provider = ScriptedProvider([
            Reply(calls=[ToolCall("c1", "read_file", "not json")]),
            Reply(calls=[ToolCall("c2", "read_file", '{"path":"example.py"}')]),
            Reply("成功恢复")])
        result = self.agent(provider).run("检查", "example.py")
        self.assertEqual(result.answer, "成功恢复")
        self.assertFalse(json.loads(provider.inputs[1][-1]["content"])["ok"])

    def test_provider_failure_does_not_persist_partial_turn(self):
        provider = ScriptedProvider([Reply(calls=[ToolCall("c", "read_file", '{"path":"example.py"}')]),
                                     ProviderError("unavailable")])
        with self.assertRaises(ProviderError):
            self.agent(provider).run("检查", "example.py")
        self.assertEqual(self.memory.turns, [])

    def test_loop_limit_and_tool_budget(self):
        calls = [ToolCall("a", "read_file", '{"path":"example.py"}'),
                 ToolCall("b", "read_file", '{"path":"example.py"}')]
        provider = ScriptedProvider([Reply(calls=calls)])
        result = self.agent(provider, replace(Config(), max_tool_calls=1)).run("检查", "example.py")
        self.assertEqual(result.status, "limit")
        self.assertEqual(result.tool_calls, 1)
        # Even a rejected call has its corresponding observation before the final answer.
        self.assertEqual(self.memory.turns[0][-2]["tool_call_id"], "b")
        self.assertEqual(self.memory.turns[0][-1]["role"], "assistant")
        provider = ScriptedProvider([Reply(calls=[calls[0]])])
        self.assertEqual(self.agent(provider, replace(Config(), max_steps=1)).run("检查", "example.py").status, "limit")

    def test_empty_response_and_invalid_request(self):
        with self.assertRaises(ProviderError):
            self.agent(ScriptedProvider([Reply()])).run("检查", "example.py")
        for task in ("", "x" * 4001, None):
            with self.assertRaises(AgentError):
                self.agent(DemoProvider()).run(task, "example.py")

    def test_demo_end_to_end_and_clear_label(self):
        result = self.agent(DemoProvider()).run("审查", "example.py")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.tool_calls, 3)
        self.assertIn("可变对象作为默认参数", result.answer)
        self.assertIn("未调用真实 LLM", result.answer)
        self.assertIn("未运行测试", result.answer)
        self.assertEqual({e.kind for e in result.events}, {"input", "model", "plan", "tool_call", "observation", "output"})

    def test_demo_other_languages_are_not_fake_reviews(self):
        (self.root / "source.js").write_text("const x = 1;")
        result = self.agent(DemoProvider()).run("审查", "source.js")
        self.assertIn("仅支持 Python", result.answer)
