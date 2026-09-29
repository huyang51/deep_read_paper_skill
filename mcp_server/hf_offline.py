"""Load the embedding model from the local cache instead of asking the Hub first.

Why this exists
---------------
Before falling back to the local cache, huggingface_hub checks the Hub for a
newer revision. Where huggingface.co is slow or unreachable that check does not
fail fast — it waits out its own timeout, on every single start. Measured with
this skill's default model already on disk: 96.4s of a 97.9s server startup,
against 12.7s with offline mode on.

For an MCP server that difference is fatal rather than merely annoying. Claude
Code bounds a server's startup — process spawn through the `initialize`
handshake — with MCP_TIMEOUT (30s by default). A server that answers later is
reported as failed and registers no tools, so the symptom is not "slow", it is
"these tools do not exist".

How
---
`huggingface_hub.constants.HF_HUB_OFFLINE` is evaluated once, at import, from
the environment — so the variables set here only count if they are set before
huggingface_hub is first imported. That rules out importing it to ask about the
cache, hence the filesystem probe below.

Offline mode is enabled only when the model looks genuinely present. A missing
or half-written cache leaves the network untouched, so a first run still
downloads normally.
"""
import os
import sys
from pathlib import Path
from typing import NamedTuple

# huggingface_hub treats either variable as enabling offline mode.
OFFLINE_VARS = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")

# A cached sentence-transformers snapshot must carry all of these before we
# dare load it with the network switched off. `modules.json` is the
# sentence-transformers pipeline itself; `config.json` is the transformer under
# it. Weights live in one of two formats depending on how the model was saved.
REQUIRED_FILES = ("modules.json", "config.json")
WEIGHT_FILES = ("model.safetensors", "pytorch_model.bin")


# huggingface_hub's own reading of a boolean environment variable
# (ENV_VARS_TRUE_VALUES), so "0" and "" mean the opposite of "1".
TRUE_VALUES = ("1", "ON", "YES", "TRUE")


class Decision(NamedTuple):
    """What was decided, so callers can report it without parsing a sentence.

    The two flags are independent: offline mode can be forced on by the user for
    a model that was never cached, and a cached model can be left online on
    purpose. Callers that mean "will this start hit the network?" want
    ``offline``; callers that mean "is the download already paid for?" want
    ``cached``.
    """

    offline: bool   # offline mode is in effect for this process
    cached: bool    # the model was found on disk (probed, not assumed)
    reason: str     # one line, for logs and deploy output


def _cache_roots():
    """HF cache roots in huggingface_hub's order of precedence.

    Mirrors constants.py (HF_HUB_CACHE → HUGGINGFACE_HUB_CACHE → HF_HOME/hub →
    XDG_CACHE_HOME/huggingface/hub → ~/.cache/huggingface/hub). Only the first
    root that exists is meaningful: if the model were cached somewhere else,
    huggingface_hub would not look there either, and going offline on the
    strength of it would turn a slow start into a hard failure.
    """
    for var in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        value = os.environ.get(var)
        if value:
            return [Path(value)]

    hf_home = os.environ.get("HF_HOME")
    if not hf_home:
        base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
        hf_home = str(Path(base) / "huggingface")
    return [Path(hf_home) / "hub"]


def _repo_dir_names(model_name: str):
    """Directory names the model may be cached under.

    sentence-transformers resolves a bare name to its own org, so
    "paraphrase-multilingual-MiniLM-L12-v2" is cached under
    models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2 while
    an already-qualified name is used as given.
    """
    names = [model_name]
    if "/" not in model_name:
        names.append(f"sentence-transformers/{model_name}")
    return [f"models--{name.replace('/', '--')}" for name in names]


def _snapshot_is_complete(snapshot: Path) -> bool:
    if not all((snapshot / f).exists() for f in REQUIRED_FILES):
        return False
    return any((snapshot / f).exists() for f in WEIGHT_FILES)


def _is_cached(model_name: str) -> bool:
    for root in _cache_roots():
        for repo in _repo_dir_names(model_name):
            snapshots = root / repo / "snapshots"
            if not snapshots.is_dir():
                continue
            for snapshot in snapshots.iterdir():
                if snapshot.is_dir() and _snapshot_is_complete(snapshot):
                    return True
    return False


def _patch_if_already_imported() -> bool:
    """Switch an already-imported huggingface_hub over too, if there is one.

    The constant is frozen at import, but its consumers read the attribute at
    call time (`huggingface_hub/utils/_http.py`, "if constants.HF_HUB_OFFLINE"),
    so an import that happened before us can still be turned around. Best
    effort: on its own this is not a substitute for running early.
    """
    hub = sys.modules.get("huggingface_hub")
    if hub is None:
        return False
    constants = getattr(hub, "constants", None)
    if constants is None or not hasattr(constants, "HF_HUB_OFFLINE"):
        return False
    constants.HF_HUB_OFFLINE = True
    return True


def prefer_cached_model(model_name: str) -> Decision:
    """Go offline when ``model_name`` is already cached locally.

    Must run before huggingface_hub (or transformers / sentence_transformers) is
    imported; on the embedding path that means before ChromaStore builds its
    embedder. Calling it twice is harmless — the second call reports that the
    variables were already set.
    """
    if not model_name:
        return Decision(False, False, "no embedding model configured")

    if Path(model_name).is_dir():
        return Decision(True, True,
                        f"'{model_name}' is a local directory — nothing to fetch")

    cached = _is_cached(model_name)

    already = [var for var in OFFLINE_VARS if os.environ.get(var)]
    if already:
        # An explicit setting wins, including "0" (force online) — the user may
        # have set it precisely because this cache is not what they want used.
        # Still probe the cache, so `cached` stays true regardless of who
        # decided the offline question.
        first = already[0]
        return Decision(os.environ[first].upper() in TRUE_VALUES, cached,
                        f"offline mode already set via {', '.join(already)}")

    if not cached:
        return Decision(
            False, False,
            f"'{model_name}' is not in the local HF cache — staying online so "
            f"the first run can download it",
        )

    for var in OFFLINE_VARS:
        os.environ[var] = "1"
    late = _patch_if_already_imported()
    reason = (f"'{model_name}' found in {_cache_roots()[0]} — offline mode on, "
              f"skipping the Hub check")
    if late:
        reason += " (huggingface_hub was already imported; constant patched live)"
    return Decision(True, True, reason)
