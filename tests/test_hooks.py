"""Offline tests for the two Claude Code hooks.

The hooks run as fresh processes per event and talk over stdin/stdout JSON;
before this module they had zero coverage and a regression (e.g. a traceback
on a non-object stdin payload) would only surface in a live session as a
broken or missing context injection.

  * user_prompt_submit — keyword match, keyword search over a temp vault,
    and the three output shapes ({}, no-hint, hint);
  * session_start — empty-vault and populated-vault context, JSON shape.
"""
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from mcp_server import config  # noqa: E402
from mcp_server import markdown_parser as mp  # noqa: E402


def _load_hook(name):
    """Import a hook module by path (hooks/ is not a package)."""
    spec = importlib.util.spec_from_file_location(
        f"hook_{name}", REPO / "hooks" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_main(mod, stdin_obj):
    """Run a hook's main() with patched stdin/stdout; return parsed stdout."""
    saved_in, saved_out = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(json.dumps(stdin_obj)), io.StringIO()
    try:
        mod.main()
        return json.loads(sys.stdout.getvalue())
    finally:
        sys.stdin, sys.stdout = saved_in, saved_out


class PromptHookTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hook = _load_hook("user_prompt_submit")

    def test_match_keywords(self):
        hits = self.hook.match_keywords("帮我读一下这篇论文", ["论文", "paper"])
        self.assertEqual(hits, ["论文"])
        self.assertEqual(self.hook.match_keywords("无关输入", ["论文"]), [])

    def test_quick_keyword_search_scores_and_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            papers = Path(tmp) / "papers"
            papers.mkdir()
            (papers / "A.md").write_text(
                "---\nid: 1\ntitle: Alpha\n---\n\n检索 检索 检索 其他\n", encoding="utf-8")
            (papers / "B.md").write_text(
                "---\nid: 2\ntitle: Beta\n---\n\n检索 一次\n", encoding="utf-8")
            saved = self.hook.PAPERS_DIR
            self.hook.PAPERS_DIR = papers
            try:
                results = self.hook.quick_keyword_search("帮我查 检索 相关论文")
            finally:
                self.hook.PAPERS_DIR = saved
            self.assertEqual([r["paper_id"] for r in results], ["1", "2"])
            self.assertEqual(results[0]["score"], 3)

    def test_quick_keyword_search_missing_vault(self):
        saved = self.hook.PAPERS_DIR
        self.hook.PAPERS_DIR = Path(tempfile.gettempdir()) / "no-such-vault-dir"
        try:
            self.assertEqual(self.hook.quick_keyword_search("论文 检索"), [])
        finally:
            self.hook.PAPERS_DIR = saved

    def test_non_object_stdin_declines_with_empty_object(self):
        """Regression: valid JSON that is not an object (`"hi"`) used to raise
        .get() out of main() — a traceback instead of the empty result that
        means 'no opinion'."""
        for payload in ("hi", [1, 2]):
            with self.subTest(payload=payload):
                saved_in, saved_out = sys.stdin, sys.stdout
                sys.stdin, sys.stdout = io.StringIO(json.dumps(payload)), io.StringIO()
                try:
                    self.hook.main()
                    out = sys.stdout.getvalue()
                finally:
                    sys.stdin, sys.stdout = saved_in, saved_out
                self.assertEqual(json.loads(out), {})

    def test_matching_prompt_injects_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            papers = Path(tmp) / "papers"
            papers.mkdir()
            (papers / "A.md").write_text(
                "---\nid: 7\ntitle: Alpha\n---\n\n检索\n", encoding="utf-8")
            saved = self.hook.PAPERS_DIR
            self.hook.PAPERS_DIR = papers
            try:
                out = _run_main(self.hook, {"prompt": "检索这篇论文的方法"})
            finally:
                self.hook.PAPERS_DIR = saved
        ctx = out["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "UserPromptSubmit")
        self.assertIn("paper_search", ctx["additionalContext"])

    def test_unmatched_prompt_is_silent(self):
        out = _run_main(self.hook, {"prompt": "帮我写个排序算法"})
        self.assertEqual(out, {})


class SessionStartHookTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hook = _load_hook("session_start")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        papers = Path(self._tmp.name) / "papers"
        papers.mkdir()
        # The hook itself has no PAPERS_DIR: it calls get_all_papers(), which
        # reads markdown_parser's module global at call time. Only mp.PAPERS_DIR
        # redirects it (config.PAPERS_DIR is patched alongside so the two never
        # disagree about which vault is "current" mid-test).
        self._orig = (config.PAPERS_DIR, mp.PAPERS_DIR)
        config.PAPERS_DIR = papers
        mp.PAPERS_DIR = papers
        mp.invalidate_papers_cache()
        self.addCleanup(self._restore)

    def _restore(self):
        (config.PAPERS_DIR, mp.PAPERS_DIR) = self._orig
        mp.invalidate_papers_cache()

    def test_empty_vault_context(self):
        out = _run_main(self.hook, {})
        ctx = out["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "SessionStart")
        self.assertIn("总论文数: 0", ctx["additionalContext"])
        self.assertIn("paper_search", ctx["additionalContext"])  # tool list

    def test_recent_papers_listed_with_tool_list(self):
        mp.create_paper_file(
            {"id": 3, "title": "Gamma", "year": 2024, "read_mode": "deep",
             "date_read": "2026-10-01", "body": ""}, config.PAPERS_DIR)
        mp.create_paper_file(
            {"id": 1, "title": "Alpha", "year": 2020, "read_mode": "quick",
             "date_read": "2026-09-20", "body": ""}, config.PAPERS_DIR)

        out = _run_main(self.hook, {})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("总论文数: 2", ctx)
        self.assertIn("[3] **Gamma**", ctx)          # newest first
        self.assertIn("[1] **Alpha**", ctx)
        self.assertIn("`paper_search`", ctx)         # TOOLS-derived list


if __name__ == "__main__":
    unittest.main()
