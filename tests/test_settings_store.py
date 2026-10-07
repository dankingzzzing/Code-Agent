import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from code_agent.config import Config
from code_agent.errors import ConfigError
from code_agent.settings_store import SettingsStore
from code_agent.tools import ToolRegistry


class SettingsStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = SettingsStore(self.root)
        self.config = Config(api_key="local-test-secret", model="test-model",
                             base_url="https://provider.example/v1")

    def tearDown(self):
        self.temp.cleanup()

    def test_round_trip_and_private_tool_boundary(self):
        self.assertIsNone(self.store.load())
        self.store.save(self.config, False)
        restored, demo = SettingsStore(self.root).load()
        self.assertEqual(restored.with_web_settings({}), self.config.with_web_settings({}))
        self.assertFalse(demo)
        self.assertNotIn(self.config.api_key, self.store.path.read_text())
        self.assertNotIn(".codeagent/web-settings.json", ToolRegistry(self.root).list_files(self.root)["files"])
        self.assertFalse((self.root / ".env").exists())
        if os.name != "nt":
            self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        else:
            document = json.loads(self.store.path.read_text())
            self.assertEqual(document["key_protection"], "windows-dpapi")
        self.store.clear()
        self.assertIsNone(self.store.load())

    def test_corrupt_or_invalid_saved_settings_are_actionable(self):
        for mutate in (
            lambda d: d.update(key_data="!!!"),
            lambda d: d["config"].update(model=3),
            lambda d: d["config"].update(project_max_files=0),
            lambda d: d.update(demo="yes"),
        ):
            self.store.save(self.config, False)
            document = json.loads(self.store.path.read_text())
            mutate(document)
            self.store.path.write_text(json.dumps(document))
            with self.assertRaises(ConfigError):
                self.store.load()
        self.store.path.write_text("invalid json")
        with self.assertRaises(ConfigError):
            self.store.load()

    def test_demo_can_be_saved_without_key(self):
        self.store.save(replace(self.config, api_key=""), True)
        restored, demo = self.store.load()
        self.assertTrue(demo)
        self.assertEqual(restored.api_key, "")
