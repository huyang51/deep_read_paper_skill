"""Covers the three test gaps from the 2026-10-01 five-way review:

  * watcher self-heal — a raising store call inside ``watch_vault`` must not
    kill the loop for the rest of the session;
  * empty-vault Chroma search — ``search``/``get_stats`` on a zero-document
    collection must return empty results, not raise;
  * PDF edge cases — corrupted input, out-of-range ``--pages``, and a
    >200-page document through the real figure pipeline.

All offline: the watcher's ``awatch`` is replaced by a synthetic async
iterator, the Chroma embedder by a registered 4-dim stub (no model), and the
PDFs are generated with PyMuPDF itself.
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import chromadb  # noqa: E402
from chromadb.api.shared_system_client import SharedSystemClient  # noqa: E402
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings  # noqa: E402
from chromadb.utils.embedding_functions import register_embedding_function  # noqa: E402
import fitz  # noqa: E402

from mcp_server import server  # noqa: E402
from mcp_server import chroma_store as cs  # noqa: E402
import extract_figures as ef  # noqa: E402


# ------------------------------------------------------------------ watcher

class _Store:
    """Stub store: records calls, raises for names in `boom`."""

    def __init__(self, boom=()):
        self.calls = []
        self.resyncs = 0
        self.boom = tuple(boom)

    def upsert_paper_by_file(self, filepath):
        self.calls.append(("upsert", filepath.name))
        if filepath.name in self.boom:
            raise OSError(f"locked: {filepath.name}")

    def index_all_papers(self):
        self.resyncs += 1
        if "resync" in self.boom:
            raise OSError("sqlite locked")


def _fake_awatch(batches):
    async def _agen():
        for b in batches:
            yield b
    def _factory(_papers_dir):
        return _agen()
    return _factory


class WatcherSelfHealTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.papers = Path(self.tmp.name) / "papers"
        self.papers.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, batches, store):
        async def _noop():
            pass
        with mock.patch.object(server, "PAPERS_DIR", self.papers), \
             mock.patch.object(server, "awatch", _fake_awatch(batches)), \
             mock.patch.object(server, "get_store", lambda: store), \
             mock.patch.object(server, "ensure_index_ready", _noop):
            asyncio.run(server.watch_vault())

    def test_store_error_does_not_kill_the_loop(self):
        """Regression: one transient failure (locked sqlite, antivirus scan)
        used to be able to end the async-for and silently stop every future
        re-index of the session."""
        store = _Store(boom=("bad.md",))
        self._run(
            [[(server.Change.modified, str(self.papers / "bad.md"))],
             [(server.Change.modified, str(self.papers / "good.md"))]],
            store)
        self.assertEqual(store.calls,
                         [("upsert", "bad.md"), ("upsert", "good.md")])

    def test_delete_triggers_resync_and_skips_rest_of_batch(self):
        store = _Store()
        self._run(
            [[(server.Change.added, str(self.papers / "a.md")),
              (server.Change.deleted, str(self.papers / "gone.md")),
              (server.Change.added, str(self.papers / "never.md"))]],
            store)
        self.assertEqual(store.resyncs, 1)
        self.assertEqual([c[1] for c in store.calls], ["a.md"])  # break after delete

    def test_resync_failure_does_not_propagate(self):
        store = _Store(boom=("resync",))
        self._run([[(server.Change.deleted, str(self.papers / "x.md"))]], store)
        self.assertEqual(store.resyncs, 1)  # attempted, raised, swallowed

    def test_non_md_files_ignored(self):
        store = _Store()
        self._run([[(server.Change.added, str(self.papers / "fig.png")),
                    (server.Change.modified, str(self.papers / "notes.txt"))]],
                  store)
        self.assertEqual(store.calls, [])


# ------------------------------------------------- empty-vault chroma search

class _StubEmbedder(EmbeddingFunction):
    def __call__(self, input: Documents) -> Embeddings:  # noqa: A002
        return [[float(len(d)), 1.0, 0.0, 0.0] for d in input]

    @staticmethod
    def name() -> str:
        return "stub-gap-embedder"

    def get_config(self) -> dict:
        return {}


try:
    register_embedding_function(_StubEmbedder)
except Exception:  # noqa: BLE001 — already registered in this process
    pass


class EmptyVaultSearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".chromadb"

    def tearDown(self):
        try:
            self.tmp.cleanup()
        except OSError:
            # Windows keeps chroma.sqlite3 open until the process exits
            self.tmp._finalizer.detach()  # noqa: SLF001

    def _store(self):
        SharedSystemClient.clear_system_cache()
        store = object.__new__(cs.ChromaStore)
        store.client = chromadb.PersistentClient(path=str(self.path))
        store.embedder = _StubEmbedder()
        store.collection = None
        return store

    def test_search_on_empty_collection_returns_empty(self):
        """Regression: n_results=min(n, max(1, count)) used to look suspect on
        a zero-document collection — confirm it answers [] rather than
        raising, which is what paper_search on a fresh vault needs."""
        store = self._store()
        self.assertEqual(store.search("任何查询"), [])
        self.assertEqual(store.get_stats(), {"total_papers": 0})

    def test_search_finds_after_upsert(self):
        store = self._store()
        store.upsert_paper("1", {"id": 1, "title": "Alpha", "short_name": "A",
                                 "year": 2024, "venue": "V", "authors": [],
                                 "method_category": "", "problem_domain": "",
                                 "core_contribution": "检索", "novelty_level": "",
                                 "date_read": "", "keywords": [], "aliases": [],
                                 "tags": [], "related_papers": [], "file": "A.md"})
        hits = store.search("检索", n_results=5)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["paper_id"], "1")
        self.assertEqual(hits[0]["title"], "Alpha")
        self.assertIsNotNone(hits[0]["similarity"])

    def test_search_n_results_above_count_is_clamped(self):
        store = self._store()
        store.upsert_paper("1", {"id": 1, "title": "Alpha", "short_name": "A",
                                 "year": 2024, "venue": "V", "authors": [],
                                 "method_category": "", "problem_domain": "",
                                 "core_contribution": "", "novelty_level": "",
                                 "date_read": "", "keywords": [], "aliases": [],
                                 "tags": [], "related_papers": [], "file": "A.md"})
        self.assertEqual(len(store.search("x", n_results=50)), 1)


# ------------------------------------------------------------- PDF hardship

class PdfEdgeCaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.outdir = Path(self.tmp.name) / "figs"

    def _write_pdf(self, name, pages=1, garbage=False):
        path = Path(self.tmp.name) / name
        if garbage:
            path.write_bytes(b"%PDF-1.4 this is not really a pdf \xff\xfe" * 20)
            return path
        doc = fitz.open()
        for i in range(pages):
            p = doc.new_page()
            p.insert_textbox(fitz.Rect(60, 40, 535, 60), f"Page {i + 1} title",
                             fontsize=14)
        doc.save(str(path))
        return path

    def test_corrupted_pdf_fails_with_actionable_cli_error(self):
        """A truncated/garbage .pdf must exit 1 with a one-line [ERROR], not a
        traceback — the reader mid-flow only needs to know the file is bad."""
        pdf = self._write_pdf("broken.pdf", garbage=True)
        code = ef.main(["--pdf", str(pdf), "--outdir", str(self.outdir)])
        self.assertEqual(code, 1)

    def test_pages_out_of_range_are_filtered_not_fatal(self):
        """parse_pages('250,1,999', 250-page doc) -> [1, 250]; an entirely
        out-of-range spec yields an empty selection instead of a crash."""
        self.assertEqual(ef.parse_pages("250,1,999", 250), [1, 250])
        self.assertEqual(ef.parse_pages("300-400", 250), [])

    def test_real_extract_on_220_page_document(self):
        """>200-page docs stay inside the pipeline's loops: full extraction
        over a 220-page synthetic PDF completes and only the figure-bearing
        page produces entries."""
        import io
        from contextlib import redirect_stdout
        pdf = self._write_pdf("long.pdf", pages=220)
        doc = fitz.open(str(pdf))
        page = doc[99]  # one figure on page 100
        page.draw_rect(fitz.Rect(150, 150, 400, 350))
        page.insert_textbox(fitz.Rect(150, 360, 500, 380),
                            "Figure 1: the only figure.", fontsize=10)
        doc.saveIncr()
        buf = io.StringIO()
        with redirect_stdout(buf):
            manifest = ef.extract_figures(pdf, self.outdir)
        self.assertEqual(manifest["n_pages"], 220)
        names = [f["name"] for f in manifest["figures"]]
        self.assertTrue(any("1" in n for n in names), names)
        self.assertTrue((self.outdir / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
