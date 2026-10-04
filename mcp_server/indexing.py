"""The index write path, shared by the MCP tool and the CLI.

``paper_index`` and ``tools/index_paper.py`` each ran the same four steps —
write the note, re-read it, mirror its relations + graph edges, then upsert the
vector — with the same five-key ``relations_synced`` payload and the same
degrade-instead-of-fail rule for the vector step. Two copies of one ordering
invariant is how they drifted: the CLI's vector-failure branch never got the path
redaction the tool's got (so absolute vault paths could reach a shared transcript
through the CLI, which is exactly what the tool's comment said must not happen),
and the two unresolved-target warnings had lost a sentence to each other.

What stays per-caller is deliberately at the edges: the dialect (the tool answers
in Chinese, the CLI in English), the relation gate (the CLI rejects malformed
``--relations`` with exit 1, the tool records a warning and writes the clean
subset), and which store object to use (the server's lazily-built singleton vs a
fresh one per CLI run).
"""
from typing import Callable, Iterable

from mcp_server.config import sanitize_error
from mcp_server.markdown_parser import (
    create_paper_file, parse_paper, sync_paper_relations,
)
from mcp_server.relations import relations_of


def index_paper(paper_data: dict, open_store: Callable[[], object], message: str,
                extra_warnings: Iterable[str] = ()) -> dict:
    """Write the note, sync what it declares, then index it. Returns the payload.

    Raises what ``create_paper_file`` raises (the caller words that failure for
    its own audience) and ``IndexWriteError`` when the written note cannot be
    read back. The vector step never raises: the file and its relations are the
    durable part of an index and are already on disk by then, so a store failure
    comes back as ``vector_index: "failed: ..."`` plus a warning.
    """
    filepath = create_paper_file(paper_data)
    paper = parse_paper(filepath)
    if not paper:
        raise IndexWriteError("创建论文文件失败")

    # Relations before vectors, on purpose: frontmatter and graph edges are what
    # must survive an unusable store (missing embedding model, offline download).
    # This runs even when the paper declares nothing — clearing relations is
    # exactly when stale mirrors on the other side need the self-heal. Neither
    # half invalidates the papers cache here: sync_paper_relations' two halves
    # each do that when they write.
    sync = sync_paper_relations(paper)

    payload = {
        "status": "ok",
        "paper_id": paper["id"],
        "file": paper.get("file") or str(filepath),
        "message": message,
    }
    warnings = list(extra_warnings)
    if relations_of(paper) or sync.get("healed"):
        payload["relations_synced"] = {
            "mirrored_to": sync.get("mirrored", []),
            "unresolved_targets": sync.get("missing", []),
            "graph_edges_updated": sync.get("edges_updated", []),
            "stale_mirrors_removed": sync.get("healed", []),
        }
        if sync.get("missing"):
            warnings.append(
                f"relations 指向的论文 {sync['missing']} 不在库中——这些关系只有单向声明，"
                f"图谱里不会出现对应节点。先在 vault 里索引对方论文，再重跑一次同步"
                f"（关系与图谱边才会补齐）。"
            )

    try:
        open_store().upsert_paper(str(paper["id"]), paper)
        payload["vector_index"] = "ok"
    except Exception as e:
        # Sanitized on both paths: this text lands in a tool RESULT (a JSON-RPC
        # success) or in the CLI's stdout, and either can carry the vault path.
        payload["vector_index"] = sanitize_error(f"failed: {type(e).__name__}: {e}")
        warnings.append(sanitize_error(
            f"向量索引失败（论文文件与关系已正常写入，语义检索暂不可用）：{e}"))

    if warnings:
        payload["warning"] = " ".join(warnings)
    return payload


class IndexWriteError(RuntimeError):
    """The note was written but cannot be read back — nothing further is safe."""
