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

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))

from mcp_server.chroma_store import ChromaStore
from mcp_server.config import sanitize_error
from mcp_server.console import force_utf8  # noqa: E402
from mcp_server.indexing import IndexWriteError, index_paper
from mcp_server.relations import normalize_relations

# This CLI reads --relations as JSON on stdin: a GBK stdin used to raise on
# CJK note text before the file was ever written, and the old copy of the
# preamble here also lacked errors="replace".
force_utf8(stdin=True)


def _open_store():
    """A fresh store per CLI run; the MCP server hands the same pipeline its
    lazily-built singleton instead. Both are just a callable to it."""
    store = ChromaStore()
    store.init_collection()
    return store


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

    # Structured relations may also be passed as a path to a JSON file — that
    # keeps multi-relation papers readable (and avoids a shell-quoting lottery
    # on Windows) while staying a single argument.
    relations = []
    if args.relations:
        raw_relations = args.relations
        candidate = Path(raw_relations)
        # Inline JSON longer than a single path component (NAME_MAX, 255 bytes)
        # makes the is_file() probe raise OSError (ENAMETOOLONG) instead of
        # returning False — guard it so long inline JSON is parsed directly.
        try:
            is_file = candidate.is_file()
        except OSError:
            is_file = False
        if is_file:
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
        "date_read": args.date_read,
        "read_mode": args.read_mode,
        "aliases": aliases,
        "tags": tags,
        "body": body,
    }

    try:
        result = index_paper(paper_data, _open_store, f"Paper indexed: {args.title}")
    except IndexWriteError as e:
        # Written but unreadable: nothing further is safe to report.
        print(json.dumps({"status": "error", "message": sanitize_error(str(e))},
                         ensure_ascii=False))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"status": "error",
                          "message": sanitize_error(f"Failed to create paper file: {e}")},
                         ensure_ascii=False))
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
