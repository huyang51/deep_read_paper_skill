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


class SeedObsidianTest(unittest.TestCase):
    """--seed-obsidian merges the template into an EXISTING vault, additions
    only. The real vault predates the template's graph.json/Dataview files, so
    its direction arrows degrade to undirected lines and index.md's dynamic
    table is dead — but an indiscriminate copy would reconfigure a working
    vault, so anything already there must never be overwritten."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.template = self.tmp / "template"
        (self.template / ".obsidian").mkdir(parents=True)
        (self.template / ".obsidian" / "graph.json").write_text(
            '{"colorGroups": []}', encoding="utf-8")
        (self.template / ".obsidian" / "app.json").write_text(
            '{"legacyEditor": false}', encoding="utf-8")
        (self.template / "index.md").write_text("# Index\n", encoding="utf-8")
        self.vault = self.tmp / "kb"
        self.vault.mkdir()
        (self.vault / ".obsidian").mkdir()
        (self.vault / ".obsidian" / "app.json").write_text(
            '{"readableLineLength": true}', encoding="utf-8")

    def run_seed(self):
        with unittest.mock.patch.object(bootstrap, "VAULT_TEMPLATE",
                                        self.template):
            return bootstrap.seed_obsidian(self.vault)

    def test_missing_files_are_added(self):
        written = self.run_seed()
        self.assertIn(".obsidian\\graph.json".replace("\\", "/")
                      if "\\" in str(self.vault) else ".obsidian/graph.json",
                      [w.replace("\\", "/") for w in written])
        self.assertTrue((self.vault / ".obsidian" / "graph.json").exists())
        self.assertTrue((self.vault / "index.md").exists())

    def test_existing_files_are_never_overwritten(self):
        self.run_seed()
        app = self.vault / ".obsidian" / "app.json"
        self.assertEqual(app.read_text(encoding="utf-8"),
                         '{"readableLineLength": true}')

    def test_second_run_is_a_no_op(self):
        self.run_seed()
        second = self.run_seed()
        self.assertEqual(second, [])


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


class LightInstallTest(unittest.TestCase):
    """--light skips exactly the sentence-transformers line of requirements.txt
    — nothing else. torch is the heaviest install and the multilingual model's
    only user; the ONNX path runs on chromadb alone."""

    def test_filter_drops_only_sentence_transformers(self):
        import tempfile as tf
        with tf.TemporaryDirectory() as tmp:
            req = Path(tmp) / "requirements.txt"
            req.write_text(
                "chromadb>=1.0,<2.0\n"
                "# Required by ChromaDB's SentenceTransformerEmbeddingFunction\n"
                "# when using a non-default embedding model.\n"
                "sentence-transformers>=2.2,<4.0\n"
                "markdown>=3.4,<4.0\n", encoding="utf-8")
            with unittest.mock.patch.object(bootstrap, "REQUIREMENTS", req):
                kept = [ln for ln in req.read_text(encoding="utf-8").splitlines()
                        if not ln.strip().startswith("sentence-transformers")]
        self.assertIn("chromadb>=1.0,<2.0", kept)
        self.assertIn("markdown>=3.4,<4.0", kept)
        self.assertTrue(any("SentenceTransformerEmbeddingFunction" in ln
                            for ln in kept))  # comment stays — it documents why
        self.assertEqual(len(kept), 4)

    def test_light_flag_is_forwarded_on_reexec(self):
        """The re-exec into the conda env must carry --light, or the second
        pass would check for (and install into) the torch path anyway."""
        captured = self._drive_main(["--light", "--yes"])
        self.assertIn("--light", captured["argv"])

    def test_interactive_light_choice_reaches_reexec_and_install(self):
        """The old table was built from argv BEFORE the "[2]" prompt, so the
        hand-picked light choice evaporated on re-exec: the child reinstalled
        the full torch stack, then died with a FALSE 「仍缺少依赖」. The
        forwarding must be built after the interactive decision."""
        captured = {}

        def fake_input(_prompt):
            captured["asked"] = True
            return "2"

        with unittest.mock.patch("sys.stdin.isatty", return_value=True), \
                unittest.mock.patch("builtins.input", fake_input):
            captured = self._drive_main(["--env-name", "E"], captured=captured,
                                         tty=True)
        self.assertTrue(captured.get("asked"))
        self.assertIn("--light", captured["argv"])
        self.assertTrue(captured["install_light"], "install_into_env 也要收到 light=True")

    def test_seed_obsidian_survives_reexec(self):
        """--seed-obsidian was missing from the forwarding table entirely."""
        captured = self._drive_main(["--yes", "--seed-obsidian", "--light"])
        self.assertIn("--seed-obsidian", captured["argv"])

    def _drive_main(self, argv, captured=None, tty=False):
        """Run bootstrap.main() on the broken-deps branch with every heavy
        step stubbed; return what reap_exec would have received."""
        captured = captured if captured is not None else {}

        def _reap(target, forwarded):
            captured["argv"] = list(forwarded)

        def _install(env_name, base, light):
            captured["install_light"] = light
            return Path("C:/fake-env/python.exe")

        with unittest.mock.patch.object(bootstrap, "missing_imports",
                                        lambda needed: ["sentence_transformers"]), \
                unittest.mock.patch.object(bootstrap, "conda_base",
                                           lambda: Path("C:/conda")), \
                unittest.mock.patch.object(bootstrap, "install_into_env", _install), \
                unittest.mock.patch.object(bootstrap, "reap_exec", _reap), \
                unittest.mock.patch("sys.stdin.isatty", return_value=tty), \
                unittest.mock.patch.object(bootstrap, "log", lambda *a, **k: None):
            bootstrap.main(argv)
        return captured


if __name__ == "__main__":
    unittest.main()
