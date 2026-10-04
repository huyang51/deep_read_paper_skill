"""Contract regressions from the 2026-10-04 three-way fresh audit.

The shared failure mode of everything here: two ends of a seam each look
correct in isolation, and only the join is wrong —

  * `PaperIndexInput.paper_id` → `create_paper_file` reads `paper_data["id"]`:
    every MCP "update" silently allocated a new id, wrote a SECOND note, and
    replied 「论文已更新」 (schema cross-checks compare constraints, not keys);
  * `verify_refs` gates on the author-matched candidate but renders the ledger
    row from matches[0] — the lookalike's venue/year/citations shipped to §6;
  * the sync paths rewrite whole files from the ≤5 s TTL cache snapshot,
    rolling back external edits;
  * malformed sibling frontmatter, bool/float relation targets, `[[a|b]]`
    aliases, non-dict JSON bodies: one bad edge case took down operations on
    perfectly valid neighbours, silently.

Run from repo root:  python -m unittest discover -s tests -v
"""
import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from mcp_server import config, cite_api  # noqa: E402
from mcp_server import markdown_parser as mp  # noqa: E402
from mcp_server import server  # noqa: E402
from mcp_server.relations import normalize_relations  # noqa: E402
import test_server_protocol as tsp  # noqa: E402


def _paper(pid, short, **kw):
    data = {"id": pid, "title": f"Title {short}", "short_name": short,
            "year": 2023, "method_category": "IR", "problem_domain": "retrieval",
            "keywords": ["k"], "body": "本体。"}
    data.update(kw)
    return data


class _VaultCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.papers = Path(self._tmp.name) / "papers"
        self.papers.mkdir()
        self._orig = (config.PAPERS_DIR, mp.PAPERS_DIR)
        config.PAPERS_DIR = self.papers
        mp.PAPERS_DIR = self.papers
        mp.invalidate_papers_cache()
        self.addCleanup(lambda: (
            setattr(config, "PAPERS_DIR", self._orig[0]),
            setattr(mp, "PAPERS_DIR", self._orig[1]),
            mp.invalidate_papers_cache()))


class PaperIdKeyContractTest(_VaultCase):
    """`model_dump()` keys must land where create_paper_file actually reads."""

    def _index(self, **params):
        base = {"title": "Alpha", "short_name": "Alpha", "year": 2020,
                "body": "b"}
        base.update(params)

        class _Store:
            class collection:
                @staticmethod
                def count():
                    return 0

            def upsert_paper(self, *a, **k):
                return None

        with mock.patch.object(server, "get_store", lambda: _Store()):
            return json.loads(asyncio.run(server.handle_paper_index(base)))

    def test_explicit_paper_id_updates_in_place(self):
        first = self._index(paper_id=5)
        self.assertEqual(first["paper_id"], 5)
        second = self._index(paper_id=5, title="Alpha v2")
        self.assertEqual(second["paper_id"], 5)
        files = list(self.papers.glob("*.md"))
        self.assertEqual(len(files), 1,
                         f"显式 paper_id 的更新必须原地覆盖，实际有 {len(files)} 个文件")
        self.assertEqual(mp.get_paper_by_id(5, self.papers)["title"], "Alpha v2")

    def test_omitted_paper_id_still_allocates(self):
        result = self._index()
        self.assertEqual(result["paper_id"], 1)


class BrokenSiblingToleranceTest(_VaultCase):
    def test_get_paper_by_id_skips_unparseable_files(self):
        mp.create_paper_file(_paper(1, "One"), self.papers)
        (self.papers / "Broken.md").write_text(
            '---\nid: 9\ntitle: "未闭合\n---\n\nbody\n', encoding="utf-8")
        mp.invalidate_papers_cache()
        self.assertIsNotNone(mp.get_paper_by_id(1, self.papers))

    def test_sync_relation_survives_broken_sibling(self):
        mp.create_paper_file(_paper(1, "One"), self.papers)
        mp.create_paper_file(_paper(2, "Two"), self.papers)
        (self.papers / "Broken.md").write_text(
            '---\nid: 9\ntitle: "未闭合\n---\n\nbody\n', encoding="utf-8")
        mp.invalidate_papers_cache()
        paper = mp.get_paper_by_id(1, self.papers)
        result = mp.sync_paper_relations(paper, self.papers)
        self.assertIsInstance(result, dict)


class StaleCacheMirrorTest(_VaultCase):
    """The mirror write must merge onto what is ON DISK, not the ≤5 s snapshot."""

    def test_external_body_edit_survives_relation_mirror(self):
        mp.create_paper_file(_paper(1, "One"), self.papers)
        mp.create_paper_file(_paper(2, "Two"), self.papers)
        one = mp.get_paper_by_id(1, self.papers)      # warms the TTL cache
        # An external editor (Obsidian) appends to paper 2 while the snapshot
        # is still fresh:
        b_path = self.papers / "Two.md"
        b_path.write_text(b_path.read_text(encoding="utf-8")
                          + "\nIMPORTANT USER NOTE\n", encoding="utf-8")
        one["relations"] = [{"target": 2, "type": "complementary",
                             "direction": "successor", "note": "n"}]
        mp.sync_relations(one, self.papers)
        self.assertIn("IMPORTANT USER NOTE", b_path.read_text(encoding="utf-8"),
                      "关系镜像用陈旧缓存整文件重写，外部编辑被静默回滚")


class DuplicateDeleteTest(_VaultCase):
    def test_delete_removes_every_copy_of_the_id(self):
        path = mp.create_paper_file(_paper(1, "One"), self.papers)
        dup = self.papers / "One-copy.md"
        dup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        self.assertTrue(mp.delete_paper_file(1, self.papers))
        self.assertFalse(path.exists())
        self.assertFalse(dup.exists(),
                         "重复 id 的第二个文件残留会被 watcher 重新索引回库里")


class WikilinkAliasTest(_VaultCase):
    def test_extract_strips_alias_and_anchor(self):
        self.assertEqual(mp.extract_wikilinks("[[One|论文一]] 与 [[Two#图2]]"),
                         ["One", "Two"])

    def test_cleanup_removes_aliased_followup_link(self):
        mp.create_paper_file(_paper(1, "One"), self.papers)
        mp.create_paper_file(_paper(2, "Two",
                                    body="正文。\n\n## 后续引用\n\n- [[One|论文一]]"),
                             self.papers)
        touched = mp.cleanup_after_deletion(1, "One", papers_dir=self.papers,
                                            deleted_file_stem="One")
        self.assertIn(2, touched, "别名链接的幽灵边被漏删，清理却报告无事发生")
        two = self.papers / "Two.md"
        self.assertNotIn("[[One", two.read_text(encoding="utf-8"))


class NormalizeTargetTest(unittest.TestCase):
    """int(True) == 1 and int(3.7) == 3 — silent wrong-paper pointers."""

    def test_bool_and_fractional_targets_dropped_with_warnings(self):
        for bad in (True, 3.7):
            entries, warns = normalize_relations(
                [{"target": bad, "type": "peer" if False else "method_similar",
                  "direction": "predecessor"}])
            self.assertEqual(entries, [], f"target={bad!r} 不该被静默接受")
            self.assertTrue(any("target" in w for w in warns),
                            f"target={bad!r} 必须留下警告: {warns}")

    def test_valid_forms_still_accepted(self):
        entries, warns = normalize_relations(
            [{"target": 3, "type": "method_similar", "direction": "peer"},
             {"target": "4", "type": "complementary", "direction": "successor"}])
        self.assertEqual([e["target"] for e in entries], [3, 4])
        self.assertEqual(warns, [])

    def test_non_string_note_warns_instead_of_silent_blank(self):
        entries, warns = normalize_relations(
            [{"target": 2, "type": "peer", "direction": "peer",
              "note": ["a", "b"]}])
        self.assertEqual(entries[0]["note"], "")
        self.assertTrue(any("note" in w for w in warns), warns)


class CiteApiShapeTest(unittest.TestCase):
    """A gateway answering 200 with `null` must not raise through the module."""

    class _Resp:
        def __init__(self, payload):
            self._p = payload

        def read(self):
            return self._p

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _get(self, payload):
        with mock.patch("urllib.request.urlopen",
                        lambda *a, **k: CiteApiShapeTest._Resp(payload)):
            return cite_api._http_get_json("https://api.example/x")

    def test_non_object_bodies_become_error_strings(self):
        for payload in (b"null", b"[1,2]", b"false"):
            with self.subTest(payload=payload):
                data, err = self._get(payload)
                self.assertIsNone(data)
                self.assertIn("unexpected JSON shape", err)

    def test_object_body_still_ok(self):
        data, err = self._get(b'{"results": []}')
        self.assertEqual(data, {"results": []})
        self.assertIsNone(err)


class StartupErrorGateTest(unittest.TestCase):
    """A dead store blocks the tools that NEED it — not the file/network ones."""

    NAMES = ["cite_verify", "paper_get", "paper_index", "paper_search",
             "paper_index_stats"]

    def test_only_store_required_tools_are_gated(self):
        seen = {}

        def fake(name):
            async def handler(args):
                seen[name] = True
                return json.dumps({"ran": name})
            return handler

        msgs = [{"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call",
                 "params": {"name": n, "arguments": {}}}
                for i, n in enumerate(self.NAMES)]
        from mcp_server import config as cfg
        orig = cfg.CONFIG_ERROR
        cfg.CONFIG_ERROR = "向量索引初始化失败：模拟"
        try:
            with mock.patch.dict(server.TOOL_DISPATCH,
                                 {n: fake(n) for n in self.NAMES}):
                replies = tsp.drive(tsp.conversation(*msgs))
        finally:
            cfg.CONFIG_ERROR = orig

        by_id = {r.get("id"): r for r in replies}
        for i, name in enumerate(self.NAMES):
            reply = by_id[10 + i]
            if name in ("paper_search", "paper_index_stats"):
                self.assertIn("error", reply, f"{name} 应被 STARTUP_ERROR 挡下")
            else:
                self.assertNotIn("error", reply,
                                 f"{name} 不该被向量索引的失败封死")
                self.assertTrue(seen.get(name), f"{name} 的 handler 没有运行")

    def test_file_only_tools_never_trigger_the_model_init(self):
        self.assertNotIn("paper_find_related", server._INDEX_TOOLS)
        self.assertNotIn("paper_search_by_method", server._INDEX_TOOLS)
        self.assertNotIn("paper_get", server._INDEX_TOOLS)


class NotificationWithIdTest(unittest.TestCase):
    def test_id_carrying_notification_gets_an_error_reply(self):
        """The dispatcher answers the prefix with None; a message WITH an id
        is a request — silence would hang its caller forever."""
        replies = tsp.drive(tsp.conversation(
            {"jsonrpc": "2.0", "id": 9, "method": "notifications/foo"},
        ))
        by_id = {r.get("id"): r for r in replies}
        self.assertIn(9, by_id)
        self.assertEqual(by_id[9]["error"]["code"], -32601)


class ToolContentSanitizationTest(_VaultCase):
    def test_vector_degrade_text_is_masked(self):
        leak = str(self.papers / "x.sqlite3")

        class _Store:
            class collection:
                @staticmethod
                def count():
                    return 0

            def upsert_paper(self, *a, **k):
                raise FileNotFoundError(leak)

        params = {"title": "Alpha", "short_name": "Alpha", "year": 2020, "body": "b"}
        with mock.patch.object(server, "get_store", lambda: _Store()):
            payload = json.loads(asyncio.run(server.handle_paper_index(params)))
        self.assertTrue(payload["vector_index"].startswith("failed:"))
        self.assertNotIn(str(self.papers), json.dumps(payload, ensure_ascii=False),
                         "工具 RESULT（JSON-RPC success）里泄漏了 vault 绝对路径")


class CliRelationsGateTest(unittest.TestCase):
    """index_paper's docstring already promises exit 1 for malformed
    --relations — non-mapping rows must honour it, not `"status": "ok"`."""

    def test_non_mapping_entries_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("正文", encoding="utf-8")
            env = dict(os.environ, PAPER_KB_VAULT_DIR=tmp)
            # encoding="utf-8": the tools reconfigure their own stdout to
            # UTF-8, while text=True would decode with the GBK locale — the
            # reader thread dies and .stdout comes back None.
            proc = subprocess.run(
                [sys.executable, str(REPO / "tools" / "index_paper.py"),
                 "--title", "T", "--short_name", "S", "--year", "2020",
                 "--body_file", str(body), "--relations", "[3,5]"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=env)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("条目不合法", proc.stdout)
            self.assertEqual(list((Path(tmp) / "papers").glob("*.md")), [],
                             "被拒绝的关系不该留下半写的论文")


class HookTextTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "hook_ups", REPO / "hooks" / "user_prompt_submit.py")
        cls.hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.hook)

    def test_duplicate_keywords_collapse(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "settings.json").write_text(json.dumps(
                {"trigger_keywords_cn": ["论文", "paper"],
                 "trigger_keywords_en": ["paper", "literature"]}), encoding="utf-8")
            orig = self.hook.SKILL_DIR
            self.hook.SKILL_DIR = Path(tmp)
            try:
                kws = self.hook.load_keywords()
            finally:
                self.hook.SKILL_DIR = orig
            self.assertEqual(kws.count("paper"), 1, kws)

    def test_hint_shows_a_real_call_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            papers = Path(tmp) / "papers"
            papers.mkdir()
            (papers / "A.md").write_text(
                "---\nid: 7\ntitle: Alpha\n---\n\n检索\n", encoding="utf-8")
            orig = self.hook.PAPERS_DIR
            self.hook.PAPERS_DIR = papers
            try:
                saved_in, saved_out = sys.stdin, sys.stdout
                sys.stdin, sys.stdout = (
                    # "alpha 检索" hits the frequency search (it tokenizes on
                    # whitespace; pure-Chinese prompts never match).
                    io.StringIO(json.dumps(
                        {"prompt": "帮我看看 alpha 检索 这篇论文"})),
                    io.StringIO())
                try:
                    self.hook.main()
                    out = json.loads(sys.stdout.getvalue())
                finally:
                    sys.stdin, sys.stdout = saved_in, saved_out
            finally:
                self.hook.PAPERS_DIR = orig
            ctx = out["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("{{", ctx)
            self.assertIn("paper_get(paper_id=", ctx)


if __name__ == "__main__":
    unittest.main()
