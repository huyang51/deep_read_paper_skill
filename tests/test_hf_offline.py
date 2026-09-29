"""Offline tests for hf_offline — the startup *decision*, not the model load.

The failure this guards: with offline mode unset, huggingface_hub checks the
Hub before falling back to the local cache. Where huggingface.co is slow or
unreachable that check waits out its own timeout, on every start — measured
here as 96.4s of a 97.9s MCP startup, against 12.7s with the model loaded
offline. Claude Code gives a server 30s to answer `initialize` (MCP_TIMEOUT,
default 30000ms), so the slow path does not read as "slow", it reads as "these
tools do not exist".

The other direction matters just as much: going offline on a cache that is
absent or half-written would turn a slow start into a hard failure, so every
test below has a counterpart asserting we stay online.

No model is loaded, no real cache is touched, nothing is downloaded.

Run from repo root:  python -m unittest discover -s tests -v
"""
import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_server.hf_offline import OFFLINE_VARS, prefer_cached_model  # noqa: E402

# Everything prefer_cached_model reads, so each test starts from a known empty
# environment no matter what the developer's shell has set.
READ_VARS = OFFLINE_VARS + (
    "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_HOME", "XDG_CACHE_HOME",
)

MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
REPO_DIR = f"models--sentence-transformers--{MODEL}"


@contextlib.contextmanager
def env(**values):
    """Run with exactly ``values`` set among the variables we read."""
    saved = {var: os.environ.pop(var, None) for var in READ_VARS}
    try:
        os.environ.update({k: v for k, v in values.items() if v is not None})
        yield
    finally:
        for var in READ_VARS:
            os.environ.pop(var, None)
        for var, value in saved.items():
            if value is not None:
                os.environ[var] = value


def write_snapshot(root, repo_dir=REPO_DIR, *, complete=True):
    """Lay out a cache entry shaped like huggingface_hub's."""
    snapshot = Path(root) / repo_dir / "snapshots" / ("0" * 40)
    snapshot.mkdir(parents=True)
    (snapshot / "modules.json").write_text("[]", encoding="utf-8")
    (snapshot / "config.json").write_text("{}", encoding="utf-8")
    if complete:
        (snapshot / "pytorch_model.bin").write_bytes(b"weights")
    return snapshot


class PreferCachedModelTest(unittest.TestCase):
    def test_empty_cache_stays_online(self):
        """A first run has to download — so the network must stay available."""
        with tempfile.TemporaryDirectory() as tmp, env(HF_HUB_CACHE=tmp):
            decision = prefer_cached_model(MODEL)

            self.assertFalse(decision.offline)
            self.assertFalse(decision.cached)
            for var in OFFLINE_VARS:
                self.assertNotIn(var, os.environ)

    def test_complete_snapshot_goes_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_snapshot(tmp)
            with env(HF_HUB_CACHE=tmp):
                decision = prefer_cached_model(MODEL)

                self.assertTrue(decision.offline)
                self.assertTrue(decision.cached)
                for var in OFFLINE_VARS:
                    self.assertEqual(os.environ[var], "1")

    def test_safetensors_counts_as_weights(self):
        """Either weight format is a complete cache; both are common."""
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = write_snapshot(tmp, complete=False)
            (snapshot / "model.safetensors").write_bytes(b"weights")
            with env(HF_HUB_CACHE=tmp):
                self.assertTrue(prefer_cached_model(MODEL).offline)

    def test_half_written_snapshot_stays_online(self):
        """The guard that keeps a broken cache from becoming a broken startup.

        A download interrupted before its weights landed still leaves
        modules.json and config.json behind. Loading that offline fails hard,
        where staying online merely re-fetches what is missing.
        """
        with tempfile.TemporaryDirectory() as tmp:
            write_snapshot(tmp, complete=False)
            with env(HF_HUB_CACHE=tmp):
                decision = prefer_cached_model(MODEL)

                self.assertFalse(decision.offline)
                self.assertNotIn("HF_HUB_OFFLINE", os.environ)

    def test_bare_name_matches_sentence_transformers_org(self):
        """Settings hold a bare name; the cache dir carries the org prefix.

        `embedding_model` in settings.json is written without an org, and
        sentence-transformers resolves it to sentence-transformers/<name>. If
        we only looked for the literal name, this — the default configuration —
        would never be found cached.
        """
        with tempfile.TemporaryDirectory() as tmp:
            write_snapshot(tmp, repo_dir=f"models--sentence-transformers--{MODEL}")
            with env(HF_HUB_CACHE=tmp):
                self.assertTrue(prefer_cached_model(MODEL).cached)

    def test_qualified_name_matches_as_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            qualified = "intfloat/multilingual-e5-small"
            write_snapshot(tmp, repo_dir="models--intfloat--multilingual-e5-small")
            with env(HF_HUB_CACHE=tmp):
                self.assertTrue(prefer_cached_model(qualified).cached)

    def test_explicit_setting_is_never_overridden(self):
        """HF_HUB_OFFLINE=0 is a deliberate "stay online", not an empty value."""
        with tempfile.TemporaryDirectory() as tmp:
            write_snapshot(tmp)
            with env(HF_HUB_CACHE=tmp, HF_HUB_OFFLINE="0"):
                decision = prefer_cached_model(MODEL)

                self.assertEqual(os.environ["HF_HUB_OFFLINE"], "0")
                self.assertNotIn("TRANSFORMERS_OFFLINE", os.environ)
                self.assertIn("already set", decision.reason)
                self.assertFalse(decision.offline)
                # The cache question is answered either way: "is this download
                # already paid for?" must not depend on who chose the offline
                # setting, or callers reading `offline` as `cached` will lie.
                self.assertTrue(decision.cached)

    def test_explicit_offline_on_an_empty_cache_reports_both(self):
        """The pair that made a caller print "already cached" for a bare machine.

        Offline can be forced on for a model that was never downloaded — the
        load will then fail, and a report that read `offline` as proof of a
        cache would have said startup was fine.
        """
        with tempfile.TemporaryDirectory() as tmp, env(HF_HUB_CACHE=tmp,
                                                       HF_HUB_OFFLINE="1"):
            decision = prefer_cached_model(MODEL)

            self.assertTrue(decision.offline)
            self.assertFalse(decision.cached)

    def test_local_directory_needs_no_hub(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "my-model"
            local.mkdir()
            with env():
                decision = prefer_cached_model(str(local))

                self.assertTrue(decision.offline)
                self.assertIn("local directory", decision.reason)

    def test_model_under_hf_home_is_found(self):
        """Cache root resolution follows huggingface_hub's precedence."""
        with tempfile.TemporaryDirectory() as tmp:
            write_snapshot(Path(tmp) / "hub")
            with env(HF_HOME=tmp):
                self.assertTrue(prefer_cached_model(MODEL).cached)

    def test_hf_hub_cache_wins_over_hf_home(self):
        """Over-reporting a cache is worse than under-reporting it.

        huggingface_hub reads the model from HF_HUB_CACHE when both are set. If
        we went offline on the strength of a model sitting in the *other*
        directory, the real load would find nothing and fail outright.
        """
        with tempfile.TemporaryDirectory() as tmp:
            hub_cache, hf_home = Path(tmp) / "explicit", Path(tmp) / "home"
            write_snapshot(hf_home / "hub")
            hub_cache.mkdir()
            with env(HF_HUB_CACHE=str(hub_cache), HF_HOME=str(hf_home)):
                self.assertFalse(prefer_cached_model(MODEL).cached)


if __name__ == "__main__":
    unittest.main()
