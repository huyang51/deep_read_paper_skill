"""Offline tests for the vault write lock.

The race this guards: get_next_id is "scan the directory, take max+1" with no
serialization, so two Claude Code sessions indexing at the same time both
observed the same snapshot and handed two papers the same id — relations,
wikilinks and the ChromaDB index then described one paper and answered as
another. Allocation and write now run under an advisory file lock in the
vault; these tests exercise it with real threads and real lock handles, no
mocking of the lock itself.

Run from repo root:  python -m unittest discover -s tests -v
"""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_server import config  # noqa: E402
from mcp_server import markdown_parser as mp  # noqa: E402


def paper_data(**kw):
    data = {"title": "T", "short_name": kw.pop("short", "S"), "year": 2024,
            "method_category": "IR", "problem_domain": "retrieval",
            "keywords": ["k"], "body": "本体。"}
    data.update(kw)
    return data


class TempVaultCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.vault = Path(self._tmp.name)
        self.papers = self.vault / "papers"
        self.papers.mkdir()
        self._orig = (config.PAPERS_DIR, mp.PAPERS_DIR)
        config.PAPERS_DIR = self.papers
        mp.PAPERS_DIR = self.papers
        mp.invalidate_papers_cache()

    def tearDown(self):
        (config.PAPERS_DIR, mp.PAPERS_DIR) = self._orig
        mp.invalidate_papers_cache()


class VaultWriteLockTest(TempVaultCase):
    def test_concurrent_allocation_hands_out_distinct_ids(self):
        """Two simultaneous no-id creates must not both see max+1 as the same
        number — that is the exact duplicate-id race the lock exists for."""
        results, errors = [], []

        def create(short):
            try:
                path = mp.create_paper_file(paper_data(short=short), self.papers)
                results.append(path)
            except Exception as e:  # pragma: no cover — surfaces real failures
                errors.append(e)

        threads = [threading.Thread(target=create, args=(s,))
                   for s in ("Alpha", "Beta", "Gamma", "Delta")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 4)
        ids = sorted(mp.coerce_id(mp.parse_paper(p)["id"]) for p in results)
        self.assertEqual(ids, [1, 2, 3, 4])

    def test_held_lock_blocks_and_times_out(self):
        """A held lock must make a second acquirer wait, then fail loudly with
        a message that names the lock file — a silent overwrite would be the
        same disaster the lock prevents."""
        holder_acquired = threading.Event()
        release = threading.Event()

        def hold():
            with mp._vault_write_lock(self.papers, timeout=5.0):
                holder_acquired.set()
                release.wait(timeout=5.0)

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(holder_acquired.wait(timeout=5.0))
        try:
            with self.assertRaises(TimeoutError) as cm:
                with mp._vault_write_lock(self.papers, timeout=0.2):
                    pass
            self.assertIn(".paper-kb.lock", str(cm.exception))
        finally:
            release.set()
            thread.join(timeout=5.0)

    def test_lock_releases_on_body_exception(self):
        """An exception inside the critical section must not leave the vault
        locked for every future write — the finally has to run."""
        with self.assertRaises(RuntimeError):
            with mp._vault_write_lock(self.papers, timeout=5.0):
                raise RuntimeError("boom")

        # Would raise TimeoutError if the lock was still held (Windows region
        # locks conflict across handles even within one process).
        with mp._vault_write_lock(self.papers, timeout=0.2):
            pass

    def test_lock_file_is_never_parsed_as_a_paper(self):
        """The lock file lives in papers/ but must stay invisible to every
        *.md glob that feeds get_all_papers."""
        with mp._vault_write_lock(self.papers, timeout=5.0):
            pass

        (self.papers / "Real.md").write_text(
            "---\nid: 1\ntitle: Real\n---\n\n正文。\n", encoding="utf-8")
        papers = mp.get_all_papers(self.papers)
        self.assertEqual([p["id"] for p in papers], [1])


if __name__ == "__main__":
    unittest.main()
