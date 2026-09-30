"""Offline tests for ChromaStore.init_collection's model-switch diagnosis.

The failure this guards: a user changes `embedding_model` in settings.json, the
next start opens the existing collection with a different embedder, ChromaDB
refuses (correctly — vectors from two models are not comparable), and the
fallback create then raises "Collection [paper_memories] already exists".
Nothing in that traceback says "your index was built by another model, rebuild
it", which is exactly the action required.

No model is loaded and nothing is downloaded: ChromaStore.__init__ is bypassed
and stub embedders are injected, so these tests stay offline like the rest.

Run from repo root:  python -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chromadb  # noqa: E402
from chromadb.api.shared_system_client import SharedSystemClient  # noqa: E402
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings  # noqa: E402
from chromadb.utils.embedding_functions import register_embedding_function  # noqa: E402

from mcp_server import chroma_store as cs  # noqa: E402


def _embedder_class(label: str):
    """An EmbeddingFunction class with `label` as its ChromaDB-visible name.

    Registration is what makes it behave like a real embedder: ChromaDB only
    persists (and therefore only compares) the identity of *registered*
    classes — an unregistered one is stored as `embedding_function: null`, and
    then every embedder looks alike and no conflict is ever detected.
    """
    class _Embedder(EmbeddingFunction):
        def __call__(self, input: Documents) -> Embeddings:  # noqa: A002
            return [[0.0] * 4 for _ in input]

        @staticmethod
        def name() -> str:
            return label

        def get_config(self) -> dict:
            return {}

        @classmethod
        def build_from_config(cls, config: dict) -> "_Embedder":
            return cls()

    _Embedder.__name__ = "Stub_" + label.replace("-", "_")
    return _Embedder


EMBEDDER_A = _embedder_class("stub-embedder-a")
EMBEDDER_B = _embedder_class("stub-embedder-b")
for _cls in (EMBEDDER_A, EMBEDDER_B):
    try:
        register_embedding_function(_cls)
    except Exception:  # noqa: BLE001 — already registered in this process
        pass


def make_store(path: Path, embedder_cls) -> cs.ChromaStore:
    """A ChromaStore wired to `path`, without the real config or any model.

    ChromaDB caches its system per path inside the process, so a second client
    on the same directory would hand back the already-instantiated collection
    and never re-check the embedder. Clearing that cache makes each store here
    do the real thing: read the persisted configuration and compare.
    """
    SharedSystemClient.clear_system_cache()
    store = object.__new__(cs.ChromaStore)
    store.client = chromadb.PersistentClient(path=str(path))
    store.embedder = embedder_cls()
    store.collection = None
    return store


class InitCollectionTest(unittest.TestCase):
    def setUp(self):
        try:
            self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        except TypeError:  # Python 3.9
            self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".chromadb"

    def tearDown(self):
        try:
            self.tmp.cleanup()
        except OSError:
            # Windows keeps chroma.sqlite3 open until the process exits; a
            # leftover temp dir is not a test failure. Drop the finalizer so it
            # does not raise again at interpreter exit.
            self.tmp._finalizer.detach()  # noqa: SLF001

    def test_creates_the_collection_when_the_index_is_empty(self):
        store = make_store(self.path, EMBEDDER_A)
        store.init_collection()
        self.assertEqual(store.collection.count(), 0)

    def test_reopening_with_the_same_embedder_is_fine(self):
        make_store(self.path, EMBEDDER_A).init_collection()
        store = make_store(self.path, EMBEDDER_A)
        store.init_collection()
        self.assertEqual(store.collection.count(), 0)

    def test_switching_embedder_says_the_index_must_be_rebuilt(self):
        """The whole point: a model switch must produce an actionable message,
        not "Collection [paper_memories] already exists"."""
        make_store(self.path, EMBEDDER_A).init_collection()

        with self.assertRaises(RuntimeError) as ctx:
            make_store(self.path, EMBEDDER_B).init_collection()

        message = str(ctx.exception)
        self.assertIn(cs.COLLECTION_NAME, message)
        self.assertIn("嵌入模型", message)
        self.assertIn(str(cs.CHROMA_DIR), message)  # what to delete
        self.assertIn("重建", message)              # what happens next


class EmptyVaultTest(unittest.TestCase):
    """An emptied vault must still empty the index.

    index_all_papers used to early-return on zero papers, leaving every
    existing entry in place — search kept answering for papers whose notes
    were all gone (the watcher hits exactly this path on a file deletion).
    """

    def setUp(self):
        try:
            self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        except TypeError:  # Python 3.9
            self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".chromadb"

    def tearDown(self):
        try:
            self.tmp.cleanup()
        except OSError:
            self.tmp._finalizer.detach()  # noqa: SLF001

    def test_emptied_vault_empties_the_index(self):
        store = make_store(self.path, EMBEDDER_A)
        store.init_collection()
        store.collection.upsert(ids=["9"], documents=["ghost"],
                                metadatas=[{"id": "9"}])

        with mock.patch.object(cs, "get_all_papers", return_value=[]):
            store.index_all_papers()

        self.assertEqual(store.collection.count(), 0)


if __name__ == "__main__":
    unittest.main()
