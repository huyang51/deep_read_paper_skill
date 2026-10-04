"""Offline tests for the structured cross-paper relation layer.

Covers the pure rules (mcp_server.relations) and the vault writes
(mcp_server.markdown_parser) against a temporary vault — no network, no
ChromaDB. The invariants that must never regress:

  * reciprocity — declaring a relation writes the inverse on the other side;
  * direction — the ## 后续引用 edge goes in the EARLIER paper's body, and a
    non-chronological index must not produce a wrong-way arrow;
  * losslessness — migrating or re-syncing must not delete relations,
    related_papers ids or unrelated frontmatter keys.

Run from repo root:  python -m unittest discover -s tests -v
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from mcp_server import relations as R  # noqa: E402
import migrate_relations as M  # noqa: E402
import verify_graph_arrows as V  # noqa: E402


class RuleTest(unittest.TestCase):
    def test_vocabularies_are_closed(self):
        self.assertEqual(set(R.RELATION_TYPE_CN), set(R.RELATION_TYPES))
        self.assertEqual(set(R.DIRECTION_CN), set(R.DIRECTIONS))
        for d in R.DIRECTIONS:
            self.assertEqual(R.inverse_direction(R.inverse_direction(d)), d)

    def test_derive_direction_follows_publication_order(self):
        """Regression: the first cut returned the opposite value, which would
        have written every backlink on the wrong paper."""
        self.assertEqual(R.derive_direction(2023, 2024), "successor")   # other is later
        self.assertEqual(R.derive_direction(2024, 2023), "predecessor")  # other is earlier
        self.assertEqual(R.derive_direction(2024, 2024), "peer")
        self.assertEqual(R.derive_direction(None, 2024), "peer")
        self.assertEqual(R.derive_direction("2020", "2019"), "predecessor")

    def test_normalize_is_forgiving_but_reports(self):
        raw = [{"target": "3", "type": "method_similar", "direction": "peer",
                "note": "  多   空格  "},
               {"target": 3, "type": "complementary", "direction": "peer"},  # dup
               {"target": None},                                            # no target
               "not-a-dict"]
        entries, warnings = R.normalize_relations(raw)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["target"], 3)
        self.assertEqual(entries[0]["note"], "多 空格")
        self.assertEqual(len(warnings), 3)

    def test_normalize_accepts_a_bare_mapping(self):
        entries, _ = R.normalize_relations(
            {"target": 2, "type": "evolutionary", "direction": "successor"})
        self.assertEqual(entries[0]["target"], 2)

    def test_merge_relation_is_idempotent_and_keeps_richer_notes(self):
        entries = [{"target": 1, "type": "method_similar", "direction": "peer", "note": "手写说明"}]
        entries, changed = R.merge_relation(entries, 1, "evolutionary", "successor")
        self.assertTrue(changed)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["note"], "手写说明")  # note not clobbered
        self.assertEqual(entries[0]["direction"], "successor")
        entries, changed = R.merge_relation(entries, 1, "evolutionary", "successor")
        self.assertFalse(changed)


class ValidateTest(unittest.TestCase):
    def _paper(self, pid, year, rel=None, **kw):
        base = {"id": pid, "year": year, "short_name": f"P{pid}", "title": f"T{pid}",
                "method_category": "M", "problem_domain": "D", "body": "", "related_papers": []}
        base.update(kw)
        if rel is not None:
            base["relations"] = rel
        return base

    def codes(self, papers):
        return sorted(i["code"] for i in R.validate_relations(papers))

    def test_reciprocal_pair_is_clean(self):
        a = self._paper(1, 2020, [{"target": 2, "type": "evolutionary",
                                    "direction": "successor", "note": "本文基于它"}],
                        related_papers=[2])
        b = self._paper(2, 2019, [{"target": 1, "type": "evolutionary",
                                   "direction": "predecessor", "note": "本文基于它"}],
                        related_papers=[1])
        self.assertEqual(self.codes([a, b]), [])

    def test_one_sided_declaration_is_an_error(self):
        a = self._paper(1, 2020, [{"target": 2, "type": "method_similar", "direction": "peer",
                                   "note": "n"}], related_papers=[2])
        b = self._paper(2, 2020)
        issues = R.validate_relations([a, b])
        self.assertIn("missing_reciprocal", [i["code"] for i in issues])
        self.assertEqual([i["severity"] for i in issues], [R.ERROR])

    def test_direction_and_type_conflicts_are_distinguished(self):
        a = self._paper(1, 2020, [{"target": 2, "type": "method_similar", "direction": "peer",
                                   "note": "n"}], related_papers=[2])
        b = self._paper(2, 2020, [{"target": 1, "type": "complementary",
                                   "direction": "successor", "note": "n"}],
                        related_papers=[1])
        codes = self.codes([a, b])
        self.assertIn("type_conflict", codes)
        self.assertIn("direction_conflict", codes)

    def test_unknown_target_and_self_reference(self):
        a = self._paper(1, 2020, [{"target": 99, "type": "method_similar", "direction": "peer"},
                                  {"target": 1, "type": "method_similar", "direction": "peer"}],
                        related_papers=[99, 1])
        codes = self.codes([a])
        self.assertIn("unknown_target", codes)
        self.assertIn("self_reference", codes)

    def test_legacy_vault_reports_unmigrated_not_broken(self):
        """A pre-migration vault must keep working: no relations means no
        reciprocity claim to violate — only a migration hint."""
        a = self._paper(1, 2020, related_papers=[2])
        b = self._paper(2, 2019, related_papers=[1])
        issues = R.validate_relations([a, b])
        self.assertEqual({i["code"] for i in issues}, {"unmigrated"})
        self.assertTrue(all(i["severity"] == R.WARN for i in issues))

    def test_year_conflict_is_a_warning_not_an_error(self):
        # A preprint posted after the "successor" it claims to precede is a real
        # situation; the years disagree but the declaration may still be right.
        a = self._paper(1, 2024, [{"target": 2, "type": "evolutionary",
                                   "direction": "successor", "note": "n"}],
                        related_papers=[2])
        b = self._paper(2, 2019, [{"target": 1, "type": "evolutionary",
                                   "direction": "predecessor", "note": "n"}],
                        related_papers=[1])
        issues = R.validate_relations([a, b])
        # Both sides of the pair are flagged — each declaration disagrees with
        # the years — but neither is an error.
        self.assertEqual([i["code"] for i in issues], ["year_conflict"] * 2)
        self.assertTrue(all(i["severity"] == R.WARN for i in issues))

    def test_missing_justification_detected_when_note_and_body_are_silent(self):
        a = self._paper(1, 2020, [{"target": 2, "type": "method_similar", "direction": "peer",
                                   "note": ""}], related_papers=[2])
        b = self._paper(2, 2020, [{"target": 1, "type": "method_similar", "direction": "peer",
                                   "note": "有说明"}], related_papers=[1])
        self.assertIn("no_justification", self.codes([a, b]))

    def test_related_papers_drift_is_flagged(self):
        a = self._paper(1, 2020, [{"target": 2, "type": "method_similar", "direction": "peer",
                                   "note": "n"}], related_papers=[2, 7])
        b = self._paper(2, 2020, [{"target": 1, "type": "method_similar", "direction": "peer",
                                   "note": "n"}], related_papers=[1])
        self.assertIn("legacy_drift", self.codes([a, b]))


class DeriveTest(unittest.TestCase):
    def test_derive_relations_from_legacy_list(self):
        a = {"id": 1, "year": 2024, "method_category": "X", "problem_domain": "D"}
        older = {"id": 2, "year": 2020, "method_category": "X", "problem_domain": "D"}
        newer = {"id": 3, "year": 2025, "method_category": "Y", "problem_domain": "D"}
        entries, orphans = R.derive_relations(
            dict(a, related_papers=[2, 3, 99]), {2: older, 3: newer})
        self.assertEqual(orphans, [99])
        by_target = {e["target"]: e for e in entries}
        self.assertEqual(by_target[2]["type"], "method_similar")
        self.assertEqual(by_target[2]["direction"], "predecessor")
        self.assertEqual(by_target[3]["type"], "problem_related")
        self.assertEqual(by_target[3]["direction"], "successor")


class ArrowCheckTest(unittest.TestCase):
    """The legacy year heuristic must defer to a declaration.

    Regression: a declared `peer` pair with unequal years was reported as a
    broken arrow ("lacks ## 后续引用") even though peer relations have no edge by
    design — the check was second-guessing data it had already been told.
    """

    def _paper(self, pid, year, body="", rel=None, related=None):
        p = {"id": pid, "year": year, "short_name": f"P{pid}", "body": body,
             "related_papers": related or []}
        if rel is not None:
            p["relations"] = rel
        return p

    def test_peer_pair_with_unequal_years_is_not_a_broken_arrow(self):
        a = self._paper(1, 2019, rel=[{"target": 2, "type": "method_similar",
                                       "direction": "peer", "note": "同期"}],
                        related=[2])
        b = self._paper(2, 2020, body="与 **P1** 并行。",
                        rel=[{"target": 1, "type": "method_similar",
                              "direction": "peer", "note": "同期"}], related=[1])
        self.assertEqual(V.verify_arrows([a, b]), [])

    def test_undeclared_pair_still_gets_the_legacy_check(self):
        a = self._paper(1, 2019, related=[2])            # older, no 后续引用
        b = self._paper(2, 2020, body="与 **P1** 并行。", related=[1])
        issues = V.verify_arrows([a, b])
        self.assertTrue(any("后续引用" in i for i in issues))

    def test_declaration_suppresses_the_wrong_way_complaint(self):
        """A declared direction may disagree with the years (preprints). The
        declaration wins; check 0 is the one that validates it."""
        x = self._paper(1, 2024, body="基于 [[P2]] 的改进。",
                        rel=[{"target": 2, "type": "evolutionary",
                              "direction": "predecessor", "note": "基于它"}],
                        related=[2])
        y = self._paper(2, 2019, body="原文。",
                        rel=[{"target": 1, "type": "evolutionary",
                              "direction": "successor", "note": "基于它"}], related=[1])
        issues = V.verify_arrows([x, y])
        self.assertFalse([i for i in issues if "❌" in i], issues)

    def test_relevance_accepts_the_same_evidence_as_the_validator(self):
        """口径统一：relations 校验器的 no_justification 用 _mentions（正文
        任何位置出现 short_name 或标题），verify_relevance 曾用更严的
        段落级匹配——同一对关系一边通过一边报错。"""
        a = self._paper(1, 2019, body="本文与 Title P2 的问题设定同源。",
                        related=[2])
        b = self._paper(2, 2020)
        self.assertEqual(V.verify_relevance([a, b]), [])

    def test_relevance_accepts_a_stem_form_followup_link(self):
        """机器写入的边可能用文件 stem 而非 short_name（空格折叠）。"""
        a = self._paper(1, 2019, body="正文。\n\n## 后续引用\n\n- [[P-2-v2]]\n",
                        related=[2])
        b = self._paper(2, 2020)
        b["short_name"] = "P 2 v2"
        b["file"] = "papers/P-2-v2.md"
        self.assertEqual(V.verify_relevance([a, b]), [])

    def test_related_ids_are_matched_through_coerce_id(self):
        """hand-written `related_papers: ["2"]`（字符串形式）与 int id 的
        纯 == 比较永远 miss，检查静默落空。"""
        a = self._paper(1, 2019, body="正文。\n\n## 后续引用\n\n- [[P2]]\n",
                        related=["2"])
        b = self._paper(2, 2020, body="与 **P1** 相关。", related=[1])
        self.assertEqual(V.verify_relevance([a, b]), [])
        issues = V.verify_arrows([a, b])          # newer side has bold ref; no ❌
        self.assertFalse([i for i in issues if "❌" in i], issues)


class TempVaultCase(unittest.TestCase):
    """Shared temp-vault fixture (no tests of its own).

    Both the config module and the parser are patched: ``markdown_parser``
    imports PAPERS_DIR by value, so patching only the config would leave the
    writes pointing at the real vault.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.vault = Path(self._tmp.name)
        self.papers = self.vault / "papers"
        self.papers.mkdir()

        import mcp_server.config as config
        import mcp_server.markdown_parser as mp
        self.mp = mp
        self._orig = config.PAPERS_DIR
        config.PAPERS_DIR = self.papers
        mp.PAPERS_DIR = self.papers
        mp.invalidate_papers_cache()

    def tearDown(self):
        import mcp_server.config as config
        self.mp.PAPERS_DIR = self._orig
        config.PAPERS_DIR = self._orig
        self.mp.invalidate_papers_cache()

    def _make(self, pid, short, year, body="B.", relations=None, related=None):
        data = {"id": pid, "title": f"Title {short}", "short_name": short, "year": year,
                "method_category": "IR", "problem_domain": "retrieval",
                "keywords": ["k"], "body": body, "read_mode": "deep"}
        if relations:
            data["relations"] = relations
        if related:
            data["related_papers"] = related
        return self.mp.create_paper_file(data, self.papers)

    def _read(self, pid):
        return self.mp.get_paper_by_id(pid, self.papers)


class VaultTest(TempVaultCase):
    """End-to-end writes against a temp vault directory."""

    def test_declaring_relations_writes_reciprocal_and_projection(self):
        self._make(1, "Old", 2020)
        self._make(2, "New", 2024, relations=[
            {"target": 1, "type": "evolutionary", "direction": "predecessor",
             "note": "本文扩展了它"}])

        self.mp.sync_paper_relations(self._read(2), self.papers)

        old = self._read(1)
        back = R.relations_of(old)
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0]["target"], 2)
        self.assertEqual(back[0]["direction"], "successor")      # inverted
        self.assertEqual(back[0]["type"], "evolutionary")
        self.assertEqual(back[0]["note"], "本文扩展了它")
        self.assertEqual(old["related_papers"], [2])             # projection
        self.assertEqual(self._read(2)["related_papers"], [1])

    def test_edge_lands_in_the_earlier_paper_even_when_indexed_second(self):
        """The non-chronological case that used to be a manual repair: index the
        2024 paper first, then the 2023 one. The arrow must still be 2023→2024,
        i.e. the wikilink lives in the 2023 paper."""
        self._make(1, "ReT", 2024)
        self._make(2, "UniIR", 2023, relations=[
            {"target": 1, "type": "evolutionary", "direction": "successor",
             "note": "ReT 沿用了它的设置"}])
        self.mp.sync_paper_relations(self._read(2), self.papers)

        uniir = self._read(2)
        ret = self._read(1)
        self.assertIn("## 后续引用", uniir["body"])
        self.assertIn("[[ReT]]", uniir["body"])      # earlier paper links forward
        self.assertNotIn("[[UniIR]]", ret["body"])   # newer paper gets no edge

    def test_peer_relations_create_no_edge(self):
        self._make(1, "Alpha", 2023)
        self._make(2, "Beta", 2023, relations=[
            {"target": 1, "type": "method_similar", "direction": "peer", "note": "同期"}])
        self.mp.sync_paper_relations(self._read(2), self.papers)
        self.assertNotIn("后续引用", self._read(1)["body"])
        self.assertNotIn("后续引用", self._read(2)["body"])
        self.assertEqual(R.relations_of(self._read(1))[0]["direction"], "peer")

    def test_wrong_way_edge_is_moved_when_the_pair_has_a_declaration(self):
        """A hand-written edge pointing the wrong way — the old failure mode —
        is repaired by the sync, but only for pairs that carry a declaration."""
        self._make(1, "Old", 2020, body="分析。\n\n## 后续引用\n\n- [[Legacy]]\n")
        self._make(2, "Legacy", 2019, body="Old work.")
        self._make(3, "New", 2024, body="新工作。", relations=[
            {"target": 1, "type": "evolutionary", "direction": "predecessor",
             "note": "基于它"}])
        self.mp.sync_paper_relations(self._read(3), self.papers)

        old = self._read(1)
        self.assertIn("[[New]]", old["body"])        # correct edge added
        self.assertIn("[[Legacy]]", old["body"])     # undeclared legacy link kept

    def test_migration_preserves_relations_to_unlisted_papers(self):
        """Updating a paper without mentioning relations must not erase the ones
        already recorded (that would silently drop graph edges)."""
        self._make(1, "Old", 2020)
        self._make(2, "New", 2024, relations=[
            {"target": 1, "type": "method_similar", "direction": "peer", "note": "n"}])
        self.mp.sync_paper_relations(self._read(2), self.papers)

        self.mp.create_paper_file(  # a plain metadata update: no relations key
            {"id": 2, "title": "Title New v2", "short_name": "New", "year": 2024,
             "body": "updated body", "read_mode": "deep"}, self.papers)

        paper = self._read(2)
        self.assertEqual(R.relation_targets(paper), [1])
        self.assertEqual(paper["read_mode"], "deep")   # was dropped before the fix

    def test_update_paper_relations_is_surgical(self):
        self._make(1, "Old", 2020, body="正文保留。", related=[2])
        self._make(2, "New", 2024)
        self.mp.update_paper_relations(1, [{"target": 2, "type": "method_similar",
                                            "direction": "peer", "note": "n"}], self.papers)
        paper = self._read(1)
        self.assertEqual(paper["body"].strip(), "正文保留。")
        self.assertEqual(paper["related_papers"], [2])
        self.assertEqual(R.relations_of(paper)[0]["target"], 2)

    def test_sync_result_reports_mirror_and_edge_separately(self):
        """Regression: both halves of the sync named their list `updated`, so
        merging them with dict.update() reported the edge target as if it were
        the mirrored paper."""
        self._make(1, "New", 2024)
        self._make(2, "Mid", 2023, relations=[
            {"target": 1, "type": "evolutionary", "direction": "successor",
             "note": "New 沿用了它的设置"}])
        result = self.mp.sync_paper_relations(self._read(2), self.papers)
        self.assertEqual(result["mirrored"], [1])       # New got the reciprocal
        self.assertEqual(result["edges_updated"], [2])  # Mid got the [[New]] edge
        self.assertEqual(result["missing"], [])

    def test_removing_a_declaration_heals_the_stale_mirror(self):
        """The mirror and the graph edge a declaration produced must not outlive
        the declaration: 2 declares 1 its predecessor, then drops the relation —
        re-syncing 2 must remove 1's mirror entry, its projection and [[New]]."""
        self._make(1, "Old", 2020)
        self._make(2, "New", 2024, relations=[
            {"target": 1, "type": "evolutionary", "direction": "predecessor",
             "note": "基于它"}])
        self.mp.sync_paper_relations(self._read(2), self.papers)
        self.assertEqual(R.relation_targets(self._read(1)), [2])   # mirror in place
        self.assertIn("[[New]]", self._read(1)["body"])            # edge in place

        self.assertTrue(self.mp.update_paper_relations(2, [], self.papers))  # cleared
        result = self.mp.sync_paper_relations(self._read(2), self.papers)

        self.assertEqual(result["healed"], [1])
        old = self._read(1)
        self.assertEqual(R.relations_of(old), [])                  # mirror gone
        self.assertEqual(old["related_papers"], [])                # projection gone
        self.assertNotIn("[[New]]", old["body"])                   # edge gone too

    def test_healing_keeps_unrelated_mirrors_and_undeclared_entries(self):
        """Only the stale pair is touched: a mirror for a still-declared pair
        and an entry the other side declared on its own initiative both stay."""
        self._make(1, "Old", 2020)
        self._make(2, "New", 2024, relations=[
            {"target": 1, "type": "evolutionary", "direction": "predecessor",
             "note": "基于它"}])
        self._make(3, "Side", 2024)
        self.mp.sync_paper_relations(self._read(2), self.papers)
        # a hand declaration on 3 toward 1 — written raw, no synced flag
        self.mp.update_paper_relations(3, [
            {"target": 1, "type": "complementary", "direction": "peer",
             "note": "手工添加"}], self.papers)

        self.assertTrue(self.mp.update_paper_relations(2, [], self.papers))
        result = self.mp.sync_paper_relations(self._read(2), self.papers)

        self.assertEqual(result["healed"], [1])
        self.assertEqual(R.relations_of(self._read(1)), [])        # stale mirror gone
        self.assertEqual(R.relation_targets(self._read(3)), [1])   # hand entry kept

    def test_reindex_without_the_relations_key_never_heals(self):
        """An update that omits relations preserves them (the losslessness rule)
        — so it must not strip the other side's mirror either."""
        self._make(1, "Old", 2020)
        self._make(2, "New", 2024, relations=[
            {"target": 1, "type": "evolutionary", "direction": "predecessor",
             "note": "基于它"}])
        self.mp.sync_paper_relations(self._read(2), self.papers)

        self._make(2, "New", 2024, body="换了个正文。")              # no relations key
        result = self.mp.sync_paper_relations(self._read(2), self.papers)

        self.assertEqual(result["healed"], [])
        self.assertEqual(R.relation_targets(self._read(1)), [2])

    def test_heal_strips_the_edge_self_owns_too(self):
        """When the cleared paper was the EARLIER one, the edge lived in its own
        body — the heal must clean self's followup section, not just the other
        side's frontmatter."""
        self._make(1, "Old", 2020)
        self._make(2, "New", 2024)
        self.mp.update_paper_relations(1, [
            {"target": 2, "type": "evolutionary", "direction": "successor",
             "note": "它基于本文"}], self.papers)
        self.mp.sync_paper_relations(self._read(1), self.papers)
        self.assertIn("[[New]]", self._read(1)["body"])            # self owns the edge

        self.assertTrue(self.mp.update_paper_relations(1, [], self.papers))
        result = self.mp.sync_paper_relations(self._read(1), self.papers)

        self.assertEqual(result["healed"], [2])
        self.assertNotIn("[[New]]", self._read(1)["body"])         # self-side strip
        self.assertEqual(R.relations_of(self._read(2)), [])        # mirror gone

    def test_sync_reports_unresolved_targets_instead_of_silently_dropping(self):
        self._make(1, "Solo", 2024, relations=[
            {"target": 42, "type": "method_similar", "direction": "peer", "note": "n"}])
        result = self.mp.sync_paper_relations(self._read(1), self.papers)
        self.assertEqual(result["missing"], [42])

    def test_new_papers_are_written_lf(self):
        """Byte output must not depend on os.linesep: text-mode writes used to
        translate every "\\n" to CRLF on Windows, so the same paper came out
        different depending on who created it."""
        path = self._make(1, "Solo", 2024)
        self.assertNotIn(b"\r", path.read_bytes())

    def test_rewrite_keeps_the_files_newline_convention(self):
        """Regression from a real vault migration: mirroring a relation onto an
        LF paper reported a whole-file diff, because the rewrite re-line-ended
        every line as CRLF. A two-line frontmatter change must stay two lines."""
        self._make(1, "Old", 2020)
        self._make(2, "New", 2024, relations=[
            {"target": 1, "type": "evolutionary", "direction": "predecessor",
             "note": "本文扩展了它"}])
        path = self.papers / "Old.md"
        crlf = path.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8")
        path.write_bytes(crlf)
        self.mp.invalidate_papers_cache()

        self.mp.sync_paper_relations(self._read(2), self.papers)

        raw = path.read_bytes()
        self.assertIn(b"relations:", raw)                       # it was rewritten
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))  # ...and stayed CRLF


class ArrowCliTest(TempVaultCase):
    """verify_graph_arrows as a *process*, on a GBK console.

    The tool had no stdout.reconfigure and no argparse, so a fully consistent
    vault crashed with UnicodeEncodeError on the ✅ line (exit 1 — the exact
    opposite of its verdict), and `--help` silently ran the check instead.
    In-process calls can't reproduce either: the encoding is fixed at import
    and main() ignores argv. Hence subprocess with PYTHONIOENCODING=gbk,
    which forces the pre-fix crash even on UTF-8 dev machines.
    """

    TOOL = Path(__file__).resolve().parents[1] / "tools" / "verify_graph_arrows.py"

    def _run(self, *args):
        env = dict(os.environ, PAPER_KB_VAULT_DIR=str(self.vault),
                   PYTHONIOENCODING="gbk")
        return subprocess.run([sys.executable, str(self.TOOL)] + list(args),
                              capture_output=True, text=True, timeout=120, env=env,
                              encoding="utf-8", errors="replace")

    def test_clean_vault_exits_zero_under_gbk(self):
        self._make(1, "Old", 2023, body="与 **New** 互补。\n\n## 后续引用\n\n- [[New]]",
                   related=[2])
        self._make(2, "New", 2025, body="与 **Old** 互补。", related=[1])

        r = self._run()
        self.assertNotIn("UnicodeEncodeError", r.stderr)
        self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)
        self.assertIn("✅", r.stdout)

    def test_broken_vault_reports_under_gbk_without_crashing(self):
        """A missing reciprocal must surface as the tool's own ❌/How-to-fix
        output, not as a traceback — the exit code alone can't tell the two
        apart (both are 1), so assert the report actually reached stdout."""
        self._make(1, "Old", 2023, body="与 **New** 互补。")
        self._make(2, "New", 2025, body="与 **Old** 互补。", relations=[
            {"target": 1, "type": "evolutionary", "direction": "predecessor",
             "note": "扩展了它"}])

        r = self._run()
        self.assertNotIn("UnicodeEncodeError", r.stderr)
        self.assertEqual(r.returncode, 1, msg=r.stdout + r.stderr)
        self.assertIn("How to fix", r.stdout)

    def test_help_is_usage_not_a_run(self):
        r = self._run("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage", r.stdout.lower())

    def test_unknown_flag_is_rejected(self):
        r = self._run("--bogus")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Checking", r.stdout)  # the check must not have started


class MigrateCliTest(TempVaultCase):
    """The migration CLI: inference, overrides and the writes they produce."""

    def test_set_type_overrides_the_inferred_class_on_both_sides(self):
        """Regression for a real vault: two papers share a method category, so
        the inference says method_similar, while both bodies call the pair
        complementary. The override must land before the reciprocal is derived,
        or the two sides disagree on the type."""
        # Bodies name each other, exactly as the real pair does: a relation with
        # neither a note nor prose to back it is a warning (exit 2), which is a
        # separate rule and would otherwise mask what this test is about.
        self._make(1, "Old", 2023, body="与 **New** 方向互补。", related=[2])
        self._make(2, "New", 2025, body="与 **Old** 方向互补。")

        rc = M.main(["--set-type", "2=complementary", "--apply"])

        self.assertEqual(rc, 0)
        fwd = R.relations_of(self._read(1))[0]
        back = R.relations_of(self._read(2))[0]
        self.assertEqual((fwd["target"], fwd["type"], fwd["direction"]),
                         (2, "complementary", "successor"))
        self.assertEqual((back["target"], back["type"], back["direction"]),
                         (1, "complementary", "predecessor"))

    def test_without_the_override_the_inferred_type_is_written(self):
        self._make(1, "Old", 2023, related=[2])
        self._make(2, "New", 2025)
        M.main(["--apply"])
        self.assertEqual(R.relations_of(self._read(1))[0]["type"], "method_similar")

    def test_declared_type_survives_a_plain_rerun(self):
        """Regression, caught on a real vault: the plan re-derived from
        ``related_papers`` — a projection that carries ids and nothing else — so
        a declared type the inference disagreed with was replaced on the very
        next run. A --set-type correction lasted exactly one migration."""
        self._make(1, "Old", 2023, body="与 **New** 互补。", related=[2])
        self._make(2, "New", 2025, body="与 **Old** 互补。")
        M.main(["--set-type", "2=complementary", "--apply"])

        self.assertEqual(M.main(["--apply"]), 0)     # plain re-run: nothing to do
        self.assertEqual(R.relations_of(self._read(1))[0]["type"], "complementary")
        self.assertEqual(R.relations_of(self._read(2))[0]["type"], "complementary")

    def test_override_repins_both_sides_on_an_already_declared_pair(self):
        """Regression, on the real vault: re-running --set-type over a pair that
        already declares the inferred class pinned only the row pointing at the
        target. The reciprocal sync then reached the other paper last and wrote
        the stale class back, so the correction silently undid itself."""
        # Ids mirror the real vault (UniIR=2 declares, ReT=1 is pointed at).
        self._make(1, "ReT", 2025, body="与 **UniIR** 互补。",
                   relations=[{"target": 2, "type": "method_similar",
                               "direction": "predecessor", "note": ""}])
        self._make(2, "UniIR", 2023, body="与 **ReT** 互补。",
                   relations=[{"target": 1, "type": "method_similar",
                               "direction": "successor", "note": ""}])

        self.assertEqual(M.main(["--set-type", "1=complementary", "--apply"]), 0)

        self.assertEqual(R.relations_of(self._read(1))[0]["type"], "complementary")
        self.assertEqual(R.relations_of(self._read(2))[0]["type"], "complementary")

    def test_two_classes_for_one_pair_are_refused(self):
        """Pinning both endpoints differently would write the pair with two
        classes — the disagreement the migration exists to remove."""
        self._make(1, "Old", 2023, body="与 **New** 互补。", related=[2])
        self._make(2, "New", 2025, body="与 **Old** 互补。")

        rc = M.main(["--set-type", "1=complementary", "--set-type", "2=evolutionary",
                     "--apply"])

        self.assertEqual(rc, 1)
        self.assertEqual(R.relations_of(self._read(1)), [])   # nothing written

    def test_declaration_beats_a_stale_followup_link(self):
        """The link convention says "the target came after me"; a declaration
        that denies it (``peer``) must not be re-flipped into an arrow by a
        migration run over a body nobody cleaned up."""
        self._make(1, "Old", 2023, body="尾注\n\n## 后续引用\n\n- [[New]]",
                   relations=[{"target": 2, "type": "complementary",
                               "direction": "peer", "note": "同期并行"}])
        self._make(2, "New", 2025, body="与 **Old** 互补。")

        M.main(["--apply"])

        self.assertEqual(R.relations_of(self._read(1))[0]["direction"], "peer")
        # ...and no graph edge was invented for the peer pair.
        self.assertNotIn("[[", self._read(1)["body"])

    def test_malformed_override_is_a_bad_args_exit(self):
        self._make(1, "Old", 2023)
        self.assertEqual(M.parse_type_overrides(["1=complementary"]), {1: "complementary"})
        for bad in ("complementary", "1=", "=complementary", "1=similar", "x=peer"):
            with self.assertRaises(ValueError, msg=bad):
                M.parse_type_overrides([bad])
            # Exit 1 = bad args, and it must happen before anything is written.
            self.assertEqual(M.main(["--set-type", bad, "--apply"]), 1)
        self.assertEqual(R.relations_of(self._read(1)), [])


if __name__ == "__main__":
    unittest.main()
