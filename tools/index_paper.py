"""CLI helper for paper_index — avoids subprocess stdin/stdout encoding issues on Windows.

Usage:
  python index_paper.py \
    --title "Paper Title" \
    --year 2025 \
    --venue "CVPR" \
    --authors "Author1,Author2" \
    --method_category "Category" \
    --problem_domain "Domain" \
    --keywords "kw1,kw2,kw3" \
    --core_contribution "One-sentence contribution" \
    --date_read "2026-05-15" \
    --read_mode "deep" \
    --tags "tag1,tag2" \
    --body_file "/path/to/body.md"

Output: JSON on stdout (ASCII-safe).

Exit codes: 0 = paper file + relations written (a failed vector index is
reported inside the JSON as ``vector_index`` + ``warning``, not as a non-zero
exit, because the vault-side result is durable either way) | 1 = bad body file,
malformed ``--relations`` JSON, or an unwritable paper file.
"""
import sys
import json
import argparse
from pathlib import Path
from datetime import date

# Ensure UTF-8 encoding on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))

from mcp_server.config import PAPERS_DIR
from mcp_server.markdown_parser import create_paper_file, parse_paper, sync_paper_relations
from mcp_server.chroma_store import ChromaStore
from mcp_server.relations import relations_of, normalize_relations


def main():
    parser = argparse.ArgumentParser(description="Index a paper into the knowledge base.")
    parser.add_argument("--paper_id", type=int, default=None, help="Paper ID (auto-assigned if omitted)")
    parser.add_argument("--title", required=True, help="Paper title")
    parser.add_argument("--short_name", required=True, help="Model/method short name (e.g. ReT, PreFLMR) for filename and graph node")
    parser.add_argument("--year", type=int, required=True, help="Publication year")
    parser.add_argument("--venue", default="", help="Conference/journal")
    parser.add_argument("--authors", default="", help="Comma-separated author list")
    parser.add_argument("--method_category", default="", help="Method category")
    parser.add_argument("--problem_domain", default="", help="Problem domain")
    parser.add_argument("--keywords", default="", help="Comma-separated keywords")
    parser.add_argument("--core_contribution", default="", help="One-sentence core contribution")
    parser.add_argument("--novelty_level", default="", choices=["", "incremental", "substantial", "breakthrough"], help="Novelty level: incremental | substantial | breakthrough")
    parser.add_argument("--relations", default="", help='JSON array of structured relations: [{"target":3,"type":"method_similar","direction":"predecessor","note":"..."}] — type: method_similar|problem_related|complementary|evolutionary; direction: predecessor|successor|peer')
    parser.add_argument("--related_papers", default="", help="Comma-separated related paper IDs (legacy: prefer --relations)")
    parser.add_argument("--date_read", default=date.today().isoformat(), help="Read date YYYY-MM-DD")
    parser.add_argument("--read_mode", default="standard", choices=["quick", "standard", "deep"], help="Phase-0 triage mode recorded in frontmatter")
    parser.add_argument("--aliases", default="", help="Comma-separated aliases for Obsidian graph display and search")
    parser.add_argument("--tags", default="", help="Comma-separated tags")
    parser.add_argument("--body_file", required=True, help="Path to file containing body markdown")

    args = parser.parse_args()

    # Read body from file (avoids encoding issues with piped stdin)
    body_path = Path(args.body_file)
    if not body_path.exists():
        result = {"status": "error", "message": f"Body file not found: {args.body_file}"}
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)

    with open(body_path, "r", encoding="utf-8") as f:
        body = f.read()

    # Parse comma-separated fields
    authors = [a.strip() for a in args.authors.split(",") if a.strip()]
    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    aliases = [a.strip() for a in args.aliases.split(",") if a.strip()]
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    related = [int(r.strip()) for r in args.related_papers.split(",") if r.strip().isdigit()]

    # Structured relations may also be passed as a path to a JSON file — that
    # keeps multi-relation papers readable (and avoids a shell-quoting lottery
    # on Windows) while staying a single argument.
    relations = []
    if args.relations:
        raw_relations = args.relations
        candidate = Path(raw_relations)
        if candidate.is_file():
            raw_relations = candidate.read_text(encoding="utf-8-sig")
        try:
            relations = json.loads(raw_relations)
        except json.JSONDecodeError as e:
            print(json.dumps({"status": "error",
                              "message": f"--relations 不是合法 JSON：{e}"},
                             ensure_ascii=False))
            sys.exit(1)
        # Valid JSON can still carry invalid ENTRIES: normalize_relations drops
        # them (non-mapping rows, missing/bool/truncated targets), and
        # create_paper_file discards the warning tuple — so without this check
        # the CLI promised exit 1 for "malformed --relations" while quietly
        # writing `"status": "ok"` with the relations evaporated.
        _, rel_warns = normalize_relations(relations)
        if rel_warns:
            print(json.dumps({"status": "error",
                              "message": "--relations 有条目不合法：" + "；".join(rel_warns)},
                             ensure_ascii=False))
            sys.exit(1)

    paper_data = {
        "id": args.paper_id,
        "title": args.title,
        "short_name": args.short_name,
        "year": args.year,
        "venue": args.venue,
        "authors": authors,
        "method_category": args.method_category,
        "problem_domain": args.problem_domain,
        "keywords": keywords,
        "core_contribution": args.core_contribution,
        "novelty_level": args.novelty_level,
        "relations": relations,
        "related_papers": related,
        "date_read": args.date_read,
        "read_mode": args.read_mode,
        "aliases": aliases,
        "tags": tags,
        "body": body,
    }

    try:
        filepath = create_paper_file(paper_data)
    except Exception as e:
        result = {"status": "error", "message": f"Failed to create paper file: {e}"}
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)

    paper = parse_paper(filepath)
    if not paper:
        print(json.dumps({"status": "error",
                          "message": "Failed to parse created paper file"},
                         ensure_ascii=False))
        sys.exit(1)

    # Relations come FIRST: the frontmatter entries and the graph edges are the
    # durable part of an index, and must land even when the vector index cannot
    # be built (missing embedding model, offline download, broken store).
    #
    # Mirror the declared relations onto the papers this one points at
    # (reciprocal frontmatter + the related_papers projection) and place the
    # Obsidian graph edges by DECLARED direction — reading order no longer
    # decides which way an arrow points, so non-chronological reads need no
    # manual repair (the old add_backlinks_to_referenced_papers assumed the
    # paper being indexed was the newest).
    # Runs even when the paper declares nothing: clearing relations is exactly
    # when stale mirrors on the other side need the self-heal.
    sync = sync_paper_relations(paper)

    result = {
        "status": "ok",
        "paper_id": paper["id"],
        "file": str(filepath),
        "message": f"Paper indexed: {paper.get('title')}"
    }
    if relations_of(paper) or sync.get("healed"):
        result["relations_synced"] = {
            "mirrored_to": sync.get("mirrored", []),
            "unresolved_targets": sync.get("missing", []),
            "legacy_migrated": sync.get("derived", []),
            "graph_edges_updated": sync.get("edges_updated", []),
            "stale_mirrors_removed": sync.get("healed", []),
        }

    warnings = []
    if sync.get("missing"):
        warnings.append(
            f"relations 指向的论文 {sync['missing']} 不在库中——先在 vault 里"
            f"索引对方论文，再重跑一次同步（关系与图谱边才会补齐）。"
        )

    try:
        store = ChromaStore()
        store.init_collection()
        store.upsert_paper(str(paper["id"]), paper)
        result["vector_index"] = "ok"
    except Exception as e:
        # Degrade instead of crashing: the vault file and its relations are
        # already on disk, only semantic search is unavailable until re-indexed.
        result["vector_index"] = f"failed: {type(e).__name__}: {e}"
        warnings.append(f"向量索引失败（论文文件与关系已正常写入，语义检索暂不可用）：{e}")

    if warnings:
        result["warning"] = " ".join(warnings)

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
