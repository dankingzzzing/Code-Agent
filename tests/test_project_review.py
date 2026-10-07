import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from code_agent.agent import CodeReviewAgent
from code_agent.config import Config
from code_agent.memory import ConversationMemory
from code_agent.project_review import parse_batch
from code_agent.prompts import SYSTEM_PROMPT
from code_agent.providers import DemoProvider, Reply
from code_agent.tools import ToolRegistry


class ProjectProvider:
    label = "scripted project provider"
    identity = "project-test"

    def __init__(self, corrupt=False):
        self.inputs = []
        self.corrupt = corrupt

    def complete(self, messages, tools):
        self.inputs.append(messages)
        data = json.loads(messages[1]["content"])
        if "files" in data:
            if self.corrupt:
                return Reply('{"files": []}')
            return Reply(json.dumps({"files": [{"path": r["path"], "summary": "checked entire file",
                                               "findings": []} for r in data["files"]]}))
        return Reply("模块已按清单检查；未执行的行为仍需验证。")

    def stream_complete(self, messages, tools, on_text):
        reply = self.complete(messages, tools)
        for piece in (reply.content[:8], reply.content[8:]):
            on_text(piece)
        return reply


class ProjectReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for index in range(8):
            (self.root / f"file{index}.py").write_text("def append(x, items=[]):\n    items.append(x)\n    return items\n")

    def tearDown(self):
        self.temp.cleanup()

    def agent(self, provider, config=None, allow_exec=False):
        return CodeReviewAgent(provider, ToolRegistry(self.root, allow_exec=allow_exec),
                               ConversationMemory(SYSTEM_PROMPT), config or Config())

    def test_demo_reviews_more_than_six_files_and_reports_limits(self):
        result = self.agent(DemoProvider()).run("审查", ".")
        self.assertEqual(result.coverage["read"], 8)
        self.assertEqual(result.coverage["semantic_reviewed"], 0)
        self.assertIn("file7.py", result.answer)
        self.assertIn("可变对象作为默认参数", result.answer)
        self.assertIn("未调用真实 LLM", result.answer)
        limited = self.agent(DemoProvider(), replace(Config(), project_max_files=3)).run("审查", ".")
        self.assertEqual(limited.status, "limit")
        self.assertEqual(len(limited.coverage["skipped"]), 5)
        self.assertFalse(limited.coverage["complete"])

    def test_complete_files_and_public_progress_reach_provider_and_ui(self):
        provider, events, text = ProjectProvider(), [], []
        (self.root / "README.md").write_text("project context")
        result = self.agent(provider).run("审查", ".", on_event=events.append, on_text=text.append)
        self.assertEqual(result.coverage["semantic_reviewed"], 8)
        self.assertEqual(result.status, "completed")
        data = json.loads(provider.inputs[0][1]["content"])
        self.assertEqual(len(data["files"]), 8)
        self.assertIn("3:     return items", data["files"][0]["numbered_source"])
        self.assertEqual(data["project_context"][0]["content"], "project context")
        self.assertIn("模块已按清单检查", "".join(text))
        self.assertEqual(events[0].kind, "input")
        self.assertEqual(events[-1].progress, 1)
        self.assertIn("| 文件 |", result.answer)

    def test_bad_json_and_step_budget_do_not_claim_semantic_coverage(self):
        for config in (Config(), replace(Config(), max_steps=1)):
            result = self.agent(ProjectProvider(corrupt=True), config).run("审查", ".")
            self.assertEqual(result.coverage["semantic_reviewed"], 0)
            self.assertEqual(result.status, "limit")
            self.assertLessEqual(result.steps, config.max_steps)
            self.assertIn("语义未完成", result.answer)

    def test_directory_followup_retains_prior_question_and_report(self):
        provider = ProjectProvider()
        agent = self.agent(provider)
        agent.run("检查边界输入", ".")
        agent.run("解释刚才的检查边界", ".")
        context = json.loads(provider.inputs[2][1]["content"])["previous_conversation"]
        self.assertIn("检查边界输入", context[0]["content"])
        self.assertIn("模块已按清单检查", context[1]["content"])

    def test_oversize_source_is_reported_as_skipped(self):
        (self.root / "oversize.py").write_text("#" * 65537)
        result = self.agent(DemoProvider()).run("审查", ".")
        self.assertEqual(result.coverage["skipped"][0]["path"], "oversize.py")
        self.assertIn("64 KiB", result.answer)

    def test_no_tests_does_not_claim_success_or_failure(self):
        result = self.agent(DemoProvider(), allow_exec=True).run("审查并运行测试", ".")
        self.assertIn("未运行测试", result.answer)
        self.assertNotIn("状态：通过", result.answer)

    def test_only_actual_source_near_the_claimed_line_is_accepted(self):
        records = [{"path": "x.py", "source": "a = 1\n" * 10 + "eval(data)\n", "lines": 11}]
        valid = {"severity": "high", "line": 11, "title": "unsafe evaluation", "evidence": "eval(data)",
                 "explanation": "untrusted input executes code", "fix": "parse values", "test": "reject code"}
        findings = [valid, {**valid, "line": 1}, {**valid, "evidence": "os.system(data)"}]
        reviewed, rejected = parse_batch(json.dumps({"files": [{"path": "x.py", "findings": findings}]}), records)
        self.assertEqual(rejected, 2)
        self.assertEqual(reviewed[0]["findings"], [{"path": "x.py", **valid}])
        for files in ([], [{"path": "invented.py", "findings": []}],
                      [{"path": "x.py", "findings": []}] * 2):
            with self.assertRaises(ValueError):
                parse_batch(json.dumps({"files": files}), records)
