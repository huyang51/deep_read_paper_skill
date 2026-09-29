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
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from mcp_server import relations as R  # noqa: E402
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


class VaultTest(unittest.TestCase):
    """End-to-end writes against a temp vault directory."""

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

    def test_sync_reports_unresolved_targets_instead_of_silently_dropping(self):
        self._make(1, "Solo", 2024, relations=[
            {"target": 42, "type": "method_similar", "direction": "peer", "note": "n"}])
        result = self.mp.sync_paper_relations(self._read(1), self.papers)
        self.assertEqual(result["missing"], [42])


if __name__ == "__main__":
    unittest.main()
