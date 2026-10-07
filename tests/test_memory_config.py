import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from code_agent.config import Config, load_env
from code_agent.errors import ConfigError, MemoryError
from code_agent.memory import ConversationMemory, session_path


class MemoryConfigTests(unittest.TestCase):
    def test_trim_preserves_complete_tool_turns(self):
        memory = ConversationMemory("system", max_turns=1)
        first = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "answer"}]
        latest = [{"role": "user", "content": "new"},
                  {"role": "assistant", "tool_calls": [{"id": "c"}]},
                  {"role": "tool", "tool_call_id": "c", "content": "result"},
                  {"role": "assistant", "content": "final"}]
        memory.add_turn(first)
        memory.add_turn(latest)
        self.assertEqual(memory.messages()[1:], latest)
        snapshot = memory.messages()
        snapshot[-1]["content"] = "mutated"
        self.assertEqual(memory.messages()[-1]["content"], "final")

    def test_persistence_and_workspace_provider_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = session_path(root, "homework")
            memory = ConversationMemory("policy")
            memory.add_turn([{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}])
            memory.save(path, root, "demo")
            loaded = ConversationMemory("policy")
            loaded.load(path, root, "demo")
            self.assertEqual(loaded.turns, memory.turns)
            with self.assertRaises(MemoryError):
                loaded.load(path, root, "llm")
            path.write_text("not json")
            with self.assertRaises(MemoryError):
                loaded.load(path, root, "demo")

    def test_session_name_escape(self):
        for name in ("../secret", "a/b", "", "x" * 65):
            with self.assertRaises(MemoryError):
                session_path(Path.cwd(), name)

    def test_env_does_not_execute_and_process_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("LLM_MODEL='from-file'\nLLM_API_KEY=not-a-real-key\nOTHER=$(echo unsafe)\n")
            self.assertEqual(load_env(root / ".env")["OTHER"], "$(echo unsafe)")
            with patch.dict(os.environ, {"LLM_MODEL": "from-process"}, clear=True):
                config = Config.from_env(root)
            self.assertEqual(config.model, "from-process")
            self.assertNotIn("not-a-real-key", repr(config))

    def test_numeric_and_endpoint_validation(self):
        for variables in ({"AGENT_MAX_STEPS": "0"}, {"LLM_TIMEOUT": "bad"}, {"LLM_TIMEOUT": "nan"},
                          {"LLM_BASE_URL": "http://remote.example/v1"}, {"LLM_BASE_URL": "https://key@host/v1"},
                          {"LLM_API_STYLE": "wrong"}):
            with self.subTest(variables=variables), patch.dict(os.environ, variables, clear=True):
                with self.assertRaises(ConfigError):
                    Config.from_env(Path.cwd())

    def test_missing_key_is_explicit(self):
        with self.assertRaises(ConfigError):
            Config().require_llm()
