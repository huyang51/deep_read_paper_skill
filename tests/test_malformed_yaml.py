"""Offline tests for the malformed-frontmatter containment rules.

The failure this guards: ``frontmatter.load`` had no try/except, and neither
did the per-file loop in ``get_all_papers`` — so one hand-edited note with a
stray tab or an unclosed quote raised through every tool *and* the watcher.
One bad file took down the whole vault, with the traceback visible only in a
log nobody reads. The fix skips the bad file and reports it through the same
skipped channel that duplicate ids use; the companion rule in
``index_all_papers`` refuses to wipe the index when *every* file is
unreadable, because that state is indistinguishable from "the vault is
temporarily broken" and the wipe was meant for "the vault is truly empty".

Run from repo root:  python -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_server import config  # noqa: E402
from mcp_server import markdown_parser as mp  # noqa: E402
from mcp_server import chroma_store as cs  # noqa: E402


GOOD = """---
id: 1
title: Good Paper
short_name: Good
year: 2024
method_category: IR
problem_domain: retrieval
keywords: [k]
---

正文。
"""

BROKEN = """---
id: 2
title: "Unclosed quote
\tstray tab: [oops
short_name: Broken
---

正文。
"""


class TempVaultCase(unittest.TestCase):
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


class MalformedYamlContainmentTest(TempVaultCase):
    def test_one_bad_file_does_not_sink_the_scan(self):
        (self.papers / "Good.md").write_text(GOOD, encoding="utf-8")
        (self.papers / "Broken.md").write_text(BROKEN, encoding="utf-8")

        papers = mp.get_all_papers(self.papers)

        self.assertEqual([p["id"] for p in papers], [1])
        errors = mp.get_scan_errors()
        self.assertEqual(len(errors), 1)
        self.assertIn("Broken.md", errors[0])

    def test_clean_scan_reports_no_errors(self):
        (self.papers / "Good.md").write_text(GOOD, encoding="utf-8")

        mp.get_all_papers(self.papers)

        self.assertEqual(mp.get_scan_errors(), [])

    def test_errors_survive_a_ttl_cache_hit(self):
        """The 5s cache serves the data; the errors it was scanned with must
        not silently vanish on the second read within the window."""
        (self.papers / "Broken.md").write_text(BROKEN, encoding="utf-8")

        mp.get_all_papers(self.papers)
        fresh = mp.get_all_papers(self.papers)  # TTL cache hit

        self.assertEqual(fresh, [])
        self.assertEqual(len(mp.get_scan_errors()), 1)

    def test_invalidate_clears_errors(self):
        (self.papers / "Broken.md").write_text(BROKEN, encoding="utf-8")
        mp.get_all_papers(self.papers)
        self.assertEqual(len(mp.get_scan_errors()), 1)

        mp.invalidate_papers_cache()

        self.assertEqual(mp.get_scan_errors(), [])


class IndexAllPapersWipeGuardTest(unittest.TestCase):
    """``papers == []`` has two very different causes, and only one of them
    may wipe the index: a vault emptied on purpose, vs a vault whose every
    file is (even transiently) unparseable."""

    def _store_with_collection(self):
        store = cs.ChromaStore.__new__(cs.ChromaStore)
        store.collection = mock.Mock()
        store.collection.get.return_value = {"ids": ["1"]}
        return store

    def test_all_files_broken_does_not_wipe_the_index(self):
        store = self._store_with_collection()
        with mock.patch.object(cs, "get_all_papers", return_value=[]), \
             mock.patch.object(cs, "get_scan_errors",
                               return_value=["Broken.md: ParserError: boom"]):
            store.index_all_papers()

        store.collection.get.assert_not_called()
        store.collection.delete.assert_not_called()

    def test_truly_emptied_vault_still_clears_orphans(self):
        store = self._store_with_collection()
        with mock.patch.object(cs, "get_all_papers", return_value=[]), \
             mock.patch.object(cs, "get_scan_errors", return_value=[]):
            store.index_all_papers()

        store.collection.delete.assert_called_once_with(ids=["1"])

    def test_mixed_scan_indexes_survivors(self):
        store = self._store_with_collection()
        store.collection.get.return_value = {"ids": []}
        paper = {"id": 1, "title": "Good", "short_name": "Good", "year": 2024,
                 "keywords": [], "authors": [], "related_papers": []}
        with mock.patch.object(cs, "get_all_papers", return_value=[paper]), \
             mock.patch.object(cs, "get_scan_errors",
                               return_value=["Broken.md: ParserError: boom"]):
            store.index_all_papers()

        store.collection.upsert.assert_called_once()


if __name__ == "__main__":
    unittest.main()
