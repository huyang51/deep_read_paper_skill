"""Offline tests for deploy.py's interpreter preflight and MCP registration.

The first failure this guards against is silent: deploy renders a valid-looking
config for an interpreter that cannot import the server's dependencies, and
then the MCP server *and* both hooks die at startup with the traceback buried
in a log nobody reads. It happened for real — the vault pointed at an
interpreter without torch while `embedding_model` asked for a
SentenceTransformer model.

The second is the one that hid the server for days: a project-scoped
`.mcp.json` sits at "Pending approval" until the user clicks it, and a
repository cannot approve its own servers, so no file deploy writes can lift
that. User scope is not gated — but it is outranked by project scope, so a
leftover `.mcp.json` silently puts the gate back.

Run from repo root:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import deploy  # noqa: E402

BASE_MODULES = {"chromadb", "frontmatter", "watchfiles", "pydantic"}


def capture(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue()


class ProbePythonTest(unittest.TestCase):
    def test_missing_interpreter_reports_every_module(self):
        report = deploy.probe_python("D:/definitely/not/here/python.exe",
                                     "paraphrase-multilingual-MiniLM-L12-v2")
        self.assertEqual(set(report), BASE_MODULES | {"sentence_transformers"})
        self.assertTrue(all(v != "ok" for v in report.values()), report)

    def test_onnx_model_does_not_require_sentence_transformers(self):
        """all-MiniLM-L6-v2 goes through ChromaDB's bundled ONNX embedder, so
        probing it must not demand torch — that is exactly what keeps a
        torch-free install viable (and the multilingual default is what makes
        an install without torch a deploy-time warning instead of a mystery)."""
        report = deploy.probe_python(sys.executable, "all-MiniLM-L6-v2")
        self.assertNotIn("sentence_transformers", report)
        self.assertTrue(all(v == "ok" for v in report.values()), report)


class RegistrationCommandTest(unittest.TestCase):
    def test_registers_at_user_scope_not_project(self):
        """User scope is the whole point: project scope is the gated one."""
        argv = deploy.registration_command("paper_kb_mcp", "D:/skill", "D:/py.exe")

        self.assertEqual(argv[:5], ["claude", "mcp", "add", "--scope", "user"])
        self.assertIn("paper_kb_mcp", argv)

    def test_locates_the_server_without_cwd(self):
        """`claude mcp add` has no --cwd, and the server must still be importable.

        config.py resolves settings.json from __file__, and PYTHONPATH is what
        makes `-m mcp_server` work from whatever directory Claude Code happens
        to be launched in.
        """
        argv = deploy.registration_command("n", "D:/skill", "D:/py.exe")

        self.assertNotIn("cwd", argv)
        self.assertIn("PYTHONPATH=D:/skill", argv)
        self.assertEqual(argv[-2:], ["-m", "mcp_server"])

    def test_flags_after_double_dash_reach_the_server(self):
        """`-m mcp_server` must be the server's argv, not claude's own."""
        argv = deploy.registration_command("n", "D:/skill", "D:/py.exe")

        self.assertLess(argv.index("--"), argv.index("-m"))

    def test_hf_endpoint_is_baked_into_the_registration(self):
        """The *server process* downloads the embedding model, so exporting
        HF_ENDPOINT in the shell that runs deploy.py never reaches it — it
        must be injected as an env of the registration itself."""
        argv = deploy.registration_command("n", "D:/skill", "D:/py.exe",
                                           hf_endpoint="https://hf-mirror.com")
        self.assertIn("HF_ENDPOINT=https://hf-mirror.com", argv)
        self.assertIn("PYTHONPATH=D:/skill", argv)
        self.assertLess(argv.index("-e"), argv.index("--"))

    def test_no_hf_endpoint_adds_nothing(self):
        """Default (no flag) keeps the command exactly as before — mirrors and
        proxies are an opt-in, not a silent global."""
        argv = deploy.registration_command("n", "D:/skill", "D:/py.exe")
        self.assertFalse(any(a.startswith("HF_ENDPOINT") for a in argv))


class RunnableTest(unittest.TestCase):
    def test_missing_command_reports_none(self):
        self.assertIsNone(deploy.runnable(["definitely-not-on-path-xyz"]))

    @unittest.skipUnless(os.name == "nt", "the .cmd shim is a Windows problem")
    def test_windows_cmd_shim_is_launched_through_cmd(self):
        """CreateProcess cannot start a .cmd, and npm ships Claude Code as one.

        A bare list would work on a machine with claude.exe and fail on the
        npm install — the difference being invisible until someone else runs it.
        """
        with mock.patch.object(deploy.shutil, "which",
                               return_value=r"C:\npm\claude.cmd"):
            self.assertEqual(deploy.runnable(["claude", "mcp", "list"]),
                             ["cmd.exe", "/c", r"C:\npm\claude.cmd", "mcp", "list"])

    def test_real_executable_is_used_directly(self):
        with mock.patch.object(deploy.shutil, "which",
                               return_value=r"C:\bin\claude.exe"):
            self.assertEqual(deploy.runnable(["claude", "mcp", "list"]),
                             [r"C:\bin\claude.exe", "mcp", "list"])


class ShadowWarningTest(unittest.TestCase):
    """A project `.mcp.json` outranks user scope (Local > Project > User)."""

    def write_project(self, servers):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        project = Path(tmp.name)
        (project / ".mcp.json").write_text(
            json.dumps({"mcpServers": servers}), encoding="utf-8")
        return project

    def test_flags_same_named_server(self):
        project = self.write_project({"paper_kb_mcp": {"command": "py"}})

        out = capture(deploy.warn_if_shadowed, project, "paper_kb_mcp")

        self.assertIn("paper_kb_mcp", out)
        self.assertIn(".mcp.json", out)

    def test_no_warning_without_a_project_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(capture(deploy.warn_if_shadowed, Path(tmp), "x"), "")

    def test_no_warning_for_a_different_server(self):
        project = self.write_project({"someone_else": {"command": "py"}})

        self.assertEqual(capture(deploy.warn_if_shadowed, project, "paper_kb_mcp"), "")

    def test_spares_a_file_holding_other_servers(self):
        """The user's own entries are theirs — name the one to remove, don't
        tell them to delete the file."""
        project = self.write_project({"paper_kb_mcp": {}, "mine": {}})

        out = capture(deploy.warn_if_shadowed, project, "paper_kb_mcp")

        self.assertIn("mine", out)
        self.assertNotIn("rm ", out)

    def test_unreadable_file_is_not_guessed_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".mcp.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(capture(deploy.warn_if_shadowed, project, "x"), "")


class RegisterMcpServerTest(unittest.TestCase):
    ARGS = ("paper_kb_mcp", "D:/skill", "D:/py.exe")

    def test_default_only_prints(self):
        """Printing is the default: registering edits the user's global config."""
        with mock.patch.object(deploy.subprocess, "run") as run:
            out = capture(deploy.register_mcp_server, *self.ARGS, False)

        run.assert_not_called()
        self.assertIn("claude mcp add --scope user", out)
        self.assertIn("--register", out)

    def test_register_flag_invokes_the_cli(self):
        with mock.patch.object(deploy.shutil, "which", return_value="claude"), \
             mock.patch.object(deploy.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="added", stderr="")
            out = capture(deploy.register_mcp_server, *self.ARGS, True)

        self.assertEqual(run.call_args[0][0][:2], ["claude", "mcp"])
        self.assertIn("[OK]", out)

    def test_cli_absent_falls_back_to_printing(self):
        with mock.patch.object(deploy.shutil, "which", return_value=None), \
             mock.patch.object(deploy.subprocess, "run") as run:
            out = capture(deploy.register_mcp_server, *self.ARGS, True)

        run.assert_not_called()
        self.assertIn("[WARN]", out)
        self.assertIn("claude mcp add", out)

    def test_cli_failure_is_reported_not_swallowed(self):
        with mock.patch.object(deploy.shutil, "which", return_value="claude"), \
             mock.patch.object(deploy.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="boom")
            out = capture(deploy.register_mcp_server, *self.ARGS, True)

        self.assertIn("[WARN]", out)
        self.assertIn("boom", out)


class HookMergeTest(unittest.TestCase):
    """Hooks are merged into a project's settings.json, never copied over it.

    The failure this guards: `shutil.copy(output/.claude-settings.json, dest)`
    on a real project root — which already has a settings.json holding the
    user's permissions, model, env and hooks — replaced all of it, and the
    documented install path is exactly where that happened.
    """

    OURS = {"hooks": {"UserPromptSubmit": [
        {"hooks": [{"type": "command",
                    "command": "python D:/skill/hooks/user_prompt_submit.py"}]}]}}

    def project(self, settings=None) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        if settings is not None:
            (root / "settings.json").write_text(
                json.dumps(settings), encoding="utf-8")
        return root

    def test_first_deploy_creates_the_file(self):
        root = self.project()
        deploy.deploy_project_settings(root / "settings.json", self.OURS)
        written = json.loads((root / "settings.json").read_text(encoding="utf-8"))
        self.assertIn("UserPromptSubmit", written["hooks"])

    def test_existing_settings_are_preserved(self):
        theirs = {"permissions": {"allow": ["Bash(ls:*)"]}, "model": "opus",
                  "hooks": {"Stop": [{"hooks": [{"type": "command",
                                                 "command": "python mine.py"}]}]}}
        root = self.project(theirs)

        deploy.deploy_project_settings(root / "settings.json", self.OURS)

        merged = json.loads((root / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(merged["permissions"], theirs["permissions"])
        self.assertEqual(merged["model"], "opus")
        self.assertEqual(len(merged["hooks"]["Stop"]), 1)          # theirs, kept
        self.assertEqual(len(merged["hooks"]["UserPromptSubmit"]), 1)  # ours
        self.assertTrue((root / "settings.json.bak").exists())     # and a backup

    def test_second_deploy_adds_nothing(self):
        """Idempotent, and it must not stack a duplicate hook that then runs
        the keyword injector twice on every prompt."""
        root = self.project()
        deploy.deploy_project_settings(root / "settings.json", self.OURS)
        before = (root / "settings.json").read_text(encoding="utf-8")

        out = capture(deploy.deploy_project_settings,
                      root / "settings.json", self.OURS)

        self.assertEqual((root / "settings.json").read_text(encoding="utf-8"), before)
        self.assertIn("无需改动", out)

    def test_our_hook_is_recognized_through_an_edited_interpreter(self):
        """Identity is the script name: a user who repointed the interpreter at
        their own python must not get a second copy of the same hook."""
        theirs = {"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command",
                        "command": "D:/other/python.exe D:\\skill\\hooks\\user_prompt_submit.py"}]}]}}
        root = self.project(theirs)

        deploy.deploy_project_settings(root / "settings.json", self.OURS)

        merged = json.loads((root / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(len(merged["hooks"]["UserPromptSubmit"]), 1)

    def test_unparseable_settings_are_left_alone(self):
        """A file we cannot read is a file we must not overwrite."""
        root = self.project()
        broken = root / "settings.json"
        broken.write_text('{"permissions": {"allow": [}},', encoding="utf-8")

        out = capture(deploy.deploy_project_settings, broken, self.OURS)

        self.assertEqual(broken.read_text(encoding="utf-8"),
                         '{"permissions": {"allow": [}},')
        self.assertIn("[WARN]", out)

    def test_unrelated_keys_of_a_non_dict_file_survive(self):
        root = self.project()
        broken = root / "settings.json"
        broken.write_text('["not", "an", "object"]', encoding="utf-8")

        capture(deploy.deploy_project_settings, broken, self.OURS)

        self.assertEqual(broken.read_text(encoding="utf-8"),
                         '["not", "an", "object"]')


class PlaceholderSettingsTest(unittest.TestCase):
    """The shipped example must not be deployable as-is.

    settings.example.json carries literal placeholder paths; left unedited on
    Linux, "D:/my-papers/knowledge-base" is a *relative* path and deploy would
    create a directory literally named "D:" in the cwd.
    """

    def run_load(self, settings):
        """Drive load_settings over a temp settings.json.

        Returns (output, result). load_settings reports fatal problems with
        sys.exit, so SystemExit is captured into `result` — the tests below
        assert on it directly rather than treating "did not raise" as success.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "settings.json"
        path.write_text(json.dumps(settings), encoding="utf-8")
        buf = io.StringIO()
        result = None
        try:
            with contextlib.redirect_stdout(buf), \
                 mock.patch.object(deploy, "SETTINGS_FILE", path):
                result = deploy.load_settings()
        except SystemExit as e:
            result = e
        return buf.getvalue(), result

    def test_placeholder_vault_dir_is_refused(self):
        out, result = self.run_load({"vault_dir": deploy.SETTINGS_EXAMPLE_VAULT,
                                     "python_cmd": sys.executable})
        self.assertIsInstance(result, SystemExit)
        self.assertIn("[ERROR]", out)
        self.assertIn("示例路径", out)

    def test_a_real_vault_dir_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, result = self.run_load({"vault_dir": tmp,
                                         "python_cmd": sys.executable})
        self.assertNotIn("[ERROR]", out)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["vault_dir"], tmp)

    def test_missing_file_mentions_the_example_to_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf), \
                     mock.patch.object(deploy, "SETTINGS_FILE",
                                       Path(tmp) / "settings.json"):
                    deploy.load_settings()
            except SystemExit:
                pass
        self.assertIn("settings.example.json", buf.getvalue())


class SkillDiscoveryTest(unittest.TestCase):
    """A repo outside the skills discovery paths can never trigger.

    Every other deploy step only makes the tools work; the skill itself fires
    only when SKILL.md sits in ~/.claude/skills/ (or the project's). This was
    the one step no error message ever covered: a 7-step install into a
    non-discovered clone ended in a skill that silently never ran.
    """

    def run_check(self, skill_dir, project_dir=None):
        with mock.patch.object(deploy, "SKILL_DIR", Path(skill_dir)):
            return capture(deploy.check_skill_discovery, project_dir)

    def test_repo_in_personal_skills_is_ok(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        repo = home / ".claude" / "skills" / "deep_read_paper_skill"
        repo.mkdir(parents=True)
        with mock.patch("deploy.Path") as MockPath:
            MockPath.home.return_value = home
            MockPath.side_effect = Path
            out = self.run_check(repo)
        self.assertIn("[OK]", out)

    def test_repo_in_project_skills_is_ok(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        project = Path(tmp.name)
        repo = project / ".claude" / "skills" / "deep_read_paper_skill"
        repo.mkdir(parents=True)
        out = self.run_check(repo, project_dir=str(project))
        self.assertIn("[OK]", out)

    def test_repo_outside_discovery_paths_warns_with_the_clone_target(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name) / "code" / "deep_read_paper_skill"
        repo.mkdir(parents=True)
        out = self.run_check(repo, project_dir=str(Path(tmp.name) / "proj"))
        self.assertIn("[注意]", out)
        self.assertIn("永远不会触发", out)
        self.assertIn(".claude", out)  # the guidance names the skills directory

    def test_no_project_dir_still_warns(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name) / "deep_read_paper_skill"
        repo.mkdir(parents=True)
        out = self.run_check(repo, project_dir=None)
        self.assertIn("[注意]", out)


if __name__ == "__main__":
    unittest.main()
