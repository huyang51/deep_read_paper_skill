"""Offline tests for the write path's data-safety rules.

Everything here guards a way the vault could silently lose or misattribute a
note. The banded failures, in the order they appear below:

  * a *collision* — ``create_paper_file`` used to log a warning and write
    anyway, truncating a different paper's note while its ChromaDB entry,
    every ``relations`` entry naming its id and every ``[[wikilink]]`` to its
    short_name stayed put. The vault then described one paper and answered
    as another, and the tool returned ``{"status": "ok"}``;
  * a *torn update* — the old file was unlinked ~70 lines before the new one
    was written, so any failure in between destroyed the note;
  * a *stale cache* — deleting a paper left it in the 5-second paper cache,
    which the vault watcher then re-indexed, so search kept returning a note
    that no longer existed;
  * a *ghost reference* — deleting the note does not delete the relations and
    ``## 后续引用`` links that other papers hold, which become unknown_target
    errors and Obsidian ghost nodes.

Also covers the hand-edited-frontmatter coercions (``id`` as a quoted string,
``year`` as ``"2023a"``) that used to raise out of a whole-vault query.

Run from repo root:  python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_server import config  # noqa: E402
from mcp_server import markdown_parser as mp  # noqa: E402
from mcp_server.models import SearchInput  # noqa: E402


def paper(pid, short, year=2024, **kw):
    data = {"id": pid, "title": f"Title {short}", "short_name": short, "year": year,
            "method_category": "IR", "problem_domain": "retrieval",
            "keywords": ["k"], "body": "本体。", "read_mode": "deep"}
    data.update(kw)
    return data


class TempVaultCase(unittest.TestCase):
    """Temp vault with both config.PAPERS_DIR and the parser's copy patched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.vault = Path(self._tmp.name)
        self.papers = self.vault / "papers"
        self.papers.mkdir()

        self._orig = config.PAPERS_DIR
        config.PAPERS_DIR = self.papers
        mp.PAPERS_DIR = self.papers
        mp.invalidate_papers_cache()

    def tearDown(self):
        mp.PAPERS_DIR = self._orig
        config.PAPERS_DIR = self._orig
        mp.invalidate_papers_cache()

    def make(self, *args, **kw):
        return mp.create_paper_file(paper(*args, **kw), self.papers)

    def read(self, pid):
        return mp.get_paper_by_id(pid, self.papers)


class CoerceIdTest(unittest.TestCase):
    """Frontmatter is hand-editable, so `id` arrives in whatever form."""

    def test_accepts_int_and_quoted_forms(self):
        for value, expected in [(7, 7), ("7", 7), (" 7 ", 7), ("-3", -3)]:
            with self.subTest(value=value):
                self.assertEqual(mp.coerce_id(value), expected)

    def test_rejects_what_is_not_an_id(self):
        """`True` is an int in Python — index 1 of the vault it is not."""
        for value in (None, True, False, "abc", "7.5", "", [], {}):
            with self.subTest(value=value):
                self.assertIsNone(mp.coerce_id(value))


class QuotedIdVaultTest(TempVaultCase):
    """A quoted id must be findable, not a whole-vault failure."""

    def write_raw(self, filename, pid_literal):
        path = self.papers / filename
        path.write_text(
            f"---\nid: {pid_literal}\ntitle: Raw\nshort_name: {path.stem}\n"
            f"year: 2023\n---\n\nbody\n", encoding="utf-8")
        mp.invalidate_papers_cache()
        return path

    def test_quoted_id_is_found_by_int_lookup(self):
        self.write_raw("Raw.md", '"4"')
        self.assertIsNotNone(mp.get_paper_by_id(4, self.papers))

    def test_next_id_survives_a_quoted_id(self):
        """max() over mixed int/str raised TypeError, breaking every
        paper_index for the vault."""
        self.write_raw("Raw.md", '"9"')
        self.assertEqual(mp.get_next_id(self.papers), 10)

    def test_next_id_skips_files_without_an_id(self):
        self.write_raw("NoId.md", "")          # `id:` present but empty
        self.assertEqual(mp.get_next_id(self.papers), 1)


class CollisionTest(TempVaultCase):
    """A filename that belongs to another paper is never reused."""

    def test_second_paper_may_not_take_an_existing_filename(self):
        self.make(1, "ReT", 2024, title="First")
        with self.assertRaises(FileExistsError) as ctx:
            self.make(2, "ReT", 2025, title="Second")

        self.assertIn("ReT.md", str(ctx.exception))
        self.assertIn("short_name", str(ctx.exception))         # names the fix
        first = self.read(1)
        self.assertEqual(first["title"], "First")               # untouched
        self.assertIsNone(self.read(2))

    def test_idempotent_update_of_the_same_paper_is_allowed(self):
        """The exemption that keeps paper_index re-runs working."""
        self.make(1, "ReT", 2024, title="First")
        self.make(1, "ReT", 2024, title="Second")
        self.assertEqual(self.read(1)["title"], "Second")
        self.assertEqual(len(list(self.papers.glob("*.md"))), 1)


class AtomicWriteTest(TempVaultCase):
    """The old file must survive anything that happens before the new one is
    complete."""

    def test_failed_write_leaves_the_original_note_intact(self):
        self.make(1, "ReT", 2024, title="Original")
        original = (self.papers / "ReT.md").read_text(encoding="utf-8")

        with mock.patch.object(mp.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                mp.create_paper_file(paper(1, "ReT", 2024, title="Rewritten"),
                                     self.papers)

        self.assertEqual((self.papers / "ReT.md").read_text(encoding="utf-8"),
                         original)
        self.assertEqual(list(self.papers.glob("*.tmp")), [])   # no debris

    def test_renaming_short_name_moves_the_note_and_frees_the_old_name(self):
        self.make(1, "Old", 2024)
        self.make(1, "New", 2024)

        self.assertFalse((self.papers / "Old.md").exists())
        self.assertTrue((self.papers / "New.md").exists())
        self.assertEqual(len(list(self.papers.glob("*.md"))), 1)
        self.assertEqual(self.read(1)["short_name"], "New")

    def test_newline_convention_survives_an_update(self):
        """An LF note re-indexed must not come back as a whole-file CRLF diff."""
        self.make(1, "ReT", 2024)
        path = self.papers / "ReT.md"
        path.write_bytes(path.read_text(encoding="utf-8").replace("\r\n", "\n")
                         .encode("utf-8"))
        self.assertNotIn(b"\r\n", path.read_bytes())

        self.make(1, "ReT", 2024, title="Updated")

        self.assertNotIn(b"\r\n", path.read_bytes())


class DeletionTest(TempVaultCase):
    """Deleting a note must invalidate the cache and clear what pointed at it."""

    def test_deleted_paper_disappears_from_the_listing_immediately(self):
        """The 5s cache outlives the file, so the watcher used to re-index it."""
        self.make(1, "ReT", 2024)
        self.assertEqual(len(mp.get_all_papers(self.papers)), 1)

        self.assertTrue(mp.delete_paper_file(1, self.papers))

        self.assertEqual(mp.get_all_papers(self.papers), [])
        self.assertFalse((self.papers / "ReT.md").exists())

    def test_missing_paper_reports_failure(self):
        self.assertFalse(mp.delete_paper_file(99, self.papers))


class PathEscapeTest(TempVaultCase):
    """A hand-edited (or imported) frontmatter ``file:`` that escapes the vault
    must never become a write or an unlink outside it.

    Two layers protect this: ``parse_paper`` overwrites the field with the
    note's real location, and ``_safe_vault_path`` refuses to resolve anything
    that would land outside the vault root — the guard is what keeps a future
    caller that trusts raw frontmatter from reopening the hole.
    """

    def poison(self, pid, target):
        """Point an existing paper's ``file:`` at an arbitrary path."""
        path = self.papers / f"{self.read(pid)['short_name']}.md"
        text = path.read_text(encoding="utf-8")
        poisoned = text.replace("---\n", f"---\nfile: {target}\n", 1)
        if poisoned == text:
            self.fail("template changed: no frontmatter to poison")
        path.write_text(poisoned, encoding="utf-8")
        mp.invalidate_papers_cache()

    def test_guard_rejects_escapes(self):
        inside = mp._safe_vault_path("papers/ReT.md", self.papers)
        self.assertEqual(inside, (self.vault / "papers" / "ReT.md").resolve())
        self.assertIsNone(mp._safe_vault_path("", self.papers))
        self.assertIsNone(mp._safe_vault_path("../precious.txt", self.papers))
        self.assertIsNone(mp._safe_vault_path("a/../../precious.txt",
                                              self.papers))
        self.assertIsNone(
            mp._safe_vault_path(str(self.vault.parent / "x.md"), self.papers))

    def test_guard_allows_absolute_paths_inside_the_vault(self):
        """tools/index_paper.py records absolute file paths; they must pass."""
        target = self.vault / "papers" / "ReT.md"
        resolved = mp._safe_vault_path(str(target), self.papers)
        self.assertEqual(resolved, target.resolve())

    def test_delete_ignores_a_file_field_pointing_outside(self):
        sentinel = self.vault / "precious.txt"
        sentinel.write_text("keep me", encoding="utf-8")
        self.make(1, "ReT", 2024)
        self.poison(1, "../precious.txt")

        self.assertTrue(mp.delete_paper_file(1, self.papers))

        self.assertTrue(sentinel.exists())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep me")
        self.assertFalse((self.papers / "ReT.md").exists())  # real note still
        # went — via the sanitized path / frontmatter-ID scan

    def test_relation_sync_writes_through_the_real_path_not_the_poisoned_one(self):
        """parse_paper re-derives ``file:`` from the note's location, so a
        poisoned field neither redirects the write nor breaks the sync."""
        sentinel = self.vault / "precious.txt"
        sentinel.write_text("keep me", encoding="utf-8")
        self.make(1, "Old", 2020)
        self.poison(1, "../precious.txt")
        self.make(2, "New", 2024, relations=[
            {"target": 1, "type": "evolutionary", "direction": "predecessor",
             "note": "基于它"}])

        result = mp.sync_paper_relations(self.read(2), self.papers)

        self.assertEqual(result["mirrored"], [1])
        self.assertEqual(result["missing"], [])
        self.assertTrue(sentinel.exists())
        self.assertIn("[[New]]", self.read(1)["body"])

    def test_reindex_does_not_unlink_through_an_escaped_previous_file(self):
        sentinel = self.vault / "precious.txt"
        sentinel.write_text("keep me", encoding="utf-8")
        self.make(1, "ReT", 2024)
        self.poison(1, "../precious.txt")

        self.make(1, "ReT", 2024, title="Rewritten")

        self.assertTrue(sentinel.exists())
        self.assertEqual(self.read(1)["title"], "Rewritten")

    def test_cleanup_removes_relations_projection_and_edge(self):
        """Both directions: 3 points back at the deleted paper, and 1 — the
        earlier one — owns the `## 后续引用` edge naming it."""
        self.make(1, "Old", 2020, body="正文保留。\n")
        self.make(2, "Gone", 2023, body="将被删除。")
        self.make(3, "New", 2024, relations=[
            {"target": 2, "type": "evolutionary", "direction": "predecessor",
             "note": "基于它"}])
        mp.sync_paper_relations(self.read(3), self.papers)
        self.make(1, "Old", 2020, body="正文保留。\n", relations=[
            {"target": 2, "type": "evolutionary", "direction": "successor",
             "note": "它基于本文"}])
        mp.sync_paper_relations(self.read(1), self.papers)

        self.assertIn("[[Gone]]", self.read(1)["body"])   # edge exists first
        self.assertEqual(sorted(e["target"] for e in mp.relations_of(self.read(2))),
                         [1, 3])

        mp.delete_paper_file(2, self.papers)
        touched = mp.cleanup_after_deletion(2, "Gone", self.papers)

        self.assertEqual(sorted(touched), [1, 3])
        old = self.read(1)
        self.assertEqual(mp.relations_of(old), [])
        self.assertEqual(old["related_papers"], [])
        self.assertIn("正文保留。", old["body"])          # body not collateral
        self.assertNotIn("[[Gone]]", old["body"])         # stale edge gone
        new = self.read(3)
        self.assertEqual(mp.relations_of(new), [])
        self.assertEqual(new["related_papers"], [])

    def test_cleanup_reports_papers_it_rewrote(self):
        """A paper with no reference to the deleted id is left alone — and says
        so by not appearing in the returned list."""
        self.make(1, "Old", 2020)
        self.make(2, "Gone", 2023)

        self.assertEqual(mp.cleanup_after_deletion(2, "Gone", self.papers), [])

    def test_cleanup_keeps_a_legacy_related_papers_entry_for_others(self):
        """`related_papers` without a matching relation is still a reference,
        and the ones pointing elsewhere must survive the strip."""
        self.make(1, "Old", 2020)
        self.make(2, "Gone", 2023)
        self.make(3, "Other", 2024, related_papers=[1, 2])

        mp.cleanup_after_deletion(2, "Gone", self.papers)

        self.assertEqual(self.read(3)["related_papers"], [1])


class WikilinkStemTest(TempVaultCase):
    """``[[wikilink]]`` text must be the file stem, not the short name.

    ``_make_safe_filename`` collapses whitespace to ``-``, so short_name
    ``FLMR v2`` lives in ``FLMR-v2.md``; a ``[[FLMR v2]]`` link drawn from the
    short name resolves to nothing and only renders as an Obsidian ghost node.
    """

    def make_spaced(self, pid, short, year, **kw):
        """Create a paper whose short name contains a space (→ dashed file)."""
        path = self.make(pid, short, year, **kw)
        self.assertEqual(path.stem, short.replace(" ", "-"))
        return path

    def test_edge_uses_the_file_stem_as_link_text(self):
        self.make_spaced(1, "FLMR v1", 2023)
        self.make_spaced(2, "FLMR v2", 2024, relations=[
            {"target": 1, "type": "evolutionary", "direction": "predecessor",
             "note": "基于它"}])

        mp.sync_paper_relations(self.read(2), self.papers)

        body = self.read(1)["body"]
        self.assertIn("[[FLMR-v2]]", body)          # resolves to the real file
        self.assertNotIn("[[FLMR v2]]", body)       # would be a ghost node

    def test_legacy_short_name_link_is_migrated_to_the_stem(self):
        self.make_spaced(1, "FLMR v1", 2023)
        self.make_spaced(2, "FLMR v2", 2024, relations=[
            {"target": 1, "type": "evolutionary", "direction": "predecessor",
             "note": "基于它"}])
        mp.sync_paper_relations(self.read(2), self.papers)   # edge written first
        old = self.read(1)
        path = mp._paper_path(old, self.papers)
        path.write_text(path.read_text(encoding="utf-8")
                        .replace("[[FLMR-v2]]", "[[FLMR v2]]"), encoding="utf-8")
        mp.invalidate_papers_cache()
        self.assertIn("[[FLMR v2]]", self.read(1)["body"])   # stale form in place

        mp.sync_paper_relations(self.read(2), self.papers)

        body = self.read(1)["body"]
        self.assertIn("[[FLMR-v2]]", body)          # rewritten, not duplicated
        self.assertNotIn("[[FLMR v2]]", body)
        self.assertEqual(body.count("## 后续引用"), 1)

    def test_cleanup_removes_both_link_forms(self):
        """Deletion cleans links naming the stem and the short name alike."""
        self.make_spaced(1, "FLMR v1", 2023, body="正文。\n")
        self.make_spaced(2, "Gone Note", 2023)
        self.make_spaced(3, "FLMR v2", 2024, relations=[
            {"target": 1, "type": "evolutionary", "direction": "predecessor"}])
        mp.sync_paper_relations(self.read(3), self.papers)   # 1 gains [[FLMR-v2]]
        # Inject the two stale forms a pre-fix vault would hold.
        path1 = self.papers / "FLMR-v1.md"
        text = path1.read_text(encoding="utf-8")
        before, links, after = mp._split_followup(text)
        self.assertEqual(links, ["FLMR-v2"])
        path1.write_text(
            mp._rebuild_followup(before, links + ["Gone Note", "FLMR v2"], after),
            encoding="utf-8")
        mp.invalidate_papers_cache()

        touched = mp.cleanup_after_deletion(2, "Gone Note", self.papers,
                                            deleted_file_stem="Gone-Note")

        self.assertEqual(touched, [1])
        body = self.read(1)["body"]
        self.assertNotIn("[[Gone Note]]", body)
        self.assertNotIn("[[Gone-Note]]", body)
        self.assertIn("[[FLMR-v2]]", body)          # unrelated edge survives


class YearCoercionTest(unittest.TestCase):
    """One hand-written `year: 2023a` killed paper_index_stats for the vault."""

    def test_year_forms(self):
        from mcp_server.chroma_store import _as_year
        for value, expected in [(2023, 2023), ("2023", 2023), ("2023a", 2023),
                                ("2023-06", 2023), ("arXiv 2019", 2019),
                                ("preprint", None), (None, None), (True, None),
                                (["2020"], None), (1800, 1800)]:
            with self.subTest(value=value):
                self.assertEqual(_as_year(value), expected)


class SearchBoundsTest(unittest.TestCase):
    """ChromaDB rejects n_results < 1 with its own error; the whole vault is
    not a search either."""

    def test_range(self):
        self.assertEqual(SearchInput(query="q").n_results, 5)
        for bad in (0, -1, 51):
            with self.subTest(n=bad):
                with self.assertRaises(Exception):
                    SearchInput(query="q", n_results=bad)


if __name__ == "__main__":
    unittest.main()
