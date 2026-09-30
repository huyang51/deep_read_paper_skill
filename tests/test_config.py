"""Offline tests for settings.json handling.

A *missing* settings.json is a documented state — a fresh clone has none, and
the env var / cwd fallbacks are meant to cover it. A *present but unusable* one
is the opposite, and it used to be swallowed by a bare `except Exception: pass`
in `_read_settings`. That silently redirected the whole vault to
`<cwd>/knowledge-base`: semantic search answered "no papers", newly indexed
papers were written into a directory nobody configured, and nothing anywhere
said so. Since a user-scope MCP registration has no meaningful cwd, even the
wrong path was not stable between sessions.

The failure is recorded in CONFIG_ERROR rather than raised, because importing
this module must not kill the server before the MCP handshake — a process that
dies pre-`initialize` registers zero tools and shows the user nothing. server.py
turns the recorded error into a message the user actually sees.

Nothing here touches the real settings.json: SETTINGS_FILE is patched.

Run from repo root:  python -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_server import config  # noqa: E402


class SettingsTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "settings.json"
        self.addCleanup(setattr, config, "CONFIG_ERROR", config.CONFIG_ERROR)
        config.CONFIG_ERROR = None

    def read(self, content: bytes = None):
        """Call _read_settings against the temp file (absent when None)."""
        if content is not None:
            self.path.write_bytes(content)
        with mock.patch.object(config, "SETTINGS_FILE", self.path):
            return config._read_settings()


class ReadSettingsTest(SettingsTestCase):
    def test_missing_file_is_a_supported_state(self):
        self.assertEqual(self.read(), {})
        self.assertIsNone(config.CONFIG_ERROR)

    def test_normal_file_is_parsed(self):
        self.assertEqual(self.read(b'{"vault_dir": "D:/papers"}'),
                         {"vault_dir": "D:/papers"})
        self.assertIsNone(config.CONFIG_ERROR)

    def test_bom_is_tolerated(self):
        """Notepad and several editors write one; it is not a JSON syntax
        error the user should have to diagnose."""
        content = '\ufeff{"vault_dir": "D:/papers"}'.encode("utf-8")
        self.assertEqual(self.read(content), {"vault_dir": "D:/papers"})
        self.assertIsNone(config.CONFIG_ERROR)

    def test_broken_json_is_recorded_not_swallowed(self):
        self.assertEqual(self.read(b'{"vault_dir": "D:\\papers"}'), {})
        self.assertIsNotNone(config.CONFIG_ERROR)
        # The single most common cause on Windows, named in the message.
        self.assertIn("正斜杠", config.CONFIG_ERROR)
        self.assertIn(str(self.path), config.CONFIG_ERROR)

    def test_non_object_top_level_is_recorded(self):
        self.assertEqual(self.read(b'["D:/papers"]'), {})
        self.assertIsNotNone(config.CONFIG_ERROR)


class VaultDirTest(SettingsTestCase):
    def test_env_var_wins(self):
        self.path.write_bytes(b'{"vault_dir": "D:/from-settings"}')
        with mock.patch.object(config, "SETTINGS_FILE", self.path), \
             mock.patch.object(config.os, "environ",
                               {"PAPER_KB_VAULT_DIR": "D:/from-env"}):
            self.assertEqual(config._load_vault_dir(), Path("D:/from-env"))

    def test_settings_json_is_used_when_the_env_var_is_unset(self):
        self.path.write_bytes(b'{"vault_dir": "D:/from-settings"}')
        with mock.patch.object(config, "SETTINGS_FILE", self.path), \
             mock.patch.object(config.os, "environ", {}):
            self.assertEqual(config._load_vault_dir(), Path("D:/from-settings"))

    def test_fallback_is_cwd_relative_and_says_so(self):
        """A fresh clone with no settings.json gets the cwd fallback — and a
        warning, because papers indexed there are easy to lose track of."""
        import io
        import logging

        with mock.patch.object(config, "SETTINGS_FILE", self.path):
            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            logger = logging.getLogger("paper_kb_mcp")
            logger.addHandler(handler)
            self.addCleanup(logger.removeHandler, handler)
            self.assertEqual(config._load_vault_dir(),
                             Path.cwd() / "knowledge-base")

        self.assertIn("knowledge-base", stream.getvalue())

    def test_no_fallback_warning_when_settings_json_is_the_problem(self):
        """Telling the user "settings.json is not configured" while their
        settings.json sits there broken would send them the wrong way."""
        import io
        import logging

        self.path.write_bytes(b"{not json")
        with mock.patch.object(config, "SETTINGS_FILE", self.path):
            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            logger = logging.getLogger("paper_kb_mcp")
            logger.addHandler(handler)
            self.addCleanup(logger.removeHandler, handler)
            config._read_settings()          # sets CONFIG_ERROR
            config._load_vault_dir()

        self.assertNotIn("knowledge-base", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
