"""Offline tests for bootstrap.py's path handling and file etiquette.

The failure that motivated this file: `Path("~/papers/kb").resolve()` does not
expand the tilde — no shell does that inside an argument's value — so
`--vault ~/papers/kb` resolved against the cwd and grew a literal `~` directory
tree, while settings.json recorded the same wrong path as if it were fine.

Run from repo root:  python -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bootstrap  # noqa: E402


class InterpreterPathTest(unittest.TestCase):
    def test_expands_a_leading_tilde(self):
        result = bootstrap.interpreter_path("~/papers/kb")
        self.assertFalse(result.replace("\\", "/").split("/")[0].startswith("~"))
        self.assertTrue(Path(result).is_absolute())

    def test_absolute_paths_pass_through_with_forward_slashes(self):
        result = bootstrap.interpreter_path(sys.executable)
        self.assertNotIn("\\", result)
        self.assertEqual(Path(result).name, Path(sys.executable).name)


class SeedVaultTest(unittest.TestCase):
    def test_existing_directory_is_never_touched(self):
        """An existing vault is somebody's real Obsidian vault — the template
        carries .obsidian/ config that would reconfigure it."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "kb"
            vault.mkdir()
            sentinel = vault / ".obsidian" / "app.json"
            sentinel.parent.mkdir()
            sentinel.write_text("{}", encoding="utf-8")

            self.assertEqual(bootstrap.seed_vault(vault), "exists")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "{}")

    def test_absent_template_reports_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(bootstrap, "VAULT_TEMPLATE",
                                            Path(tmp) / "no-such-template"):
                result = bootstrap.seed_vault(Path(tmp) / "kb")
        self.assertEqual(result, "no-template")


class WriteSettingsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "settings.json"
        self.addCleanup(setattr, bootstrap, "SETTINGS_FILE", bootstrap.SETTINGS_FILE)
        bootstrap.SETTINGS_FILE = self.path

    def test_existing_file_is_kept_without_force(self):
        self.path.write_text('{"vault_dir": "D:/real"}', encoding="utf-8")
        self.assertEqual(bootstrap.write_settings({"vault_dir": "D:/new"}), "kept")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                         {"vault_dir": "D:/real"})

    def test_force_overwrites_and_backs_up_first(self):
        self.path.write_text('{"vault_dir": "D:/real"}', encoding="utf-8")
        self.assertEqual(bootstrap.write_settings({"vault_dir": "D:/new"},
                                                  force=True), "overwritten")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                         {"vault_dir": "D:/new"})
        self.assertEqual(json.loads(
            self.path.with_name("settings.json.bak").read_text(encoding="utf-8")),
            {"vault_dir": "D:/real"})


if __name__ == "__main__":
    unittest.main()
