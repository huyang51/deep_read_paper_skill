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
    --tags "tag1,tag2" \
    --body_file "/path/to/body.md"

Output: JSON on stdout (ASCII-safe).
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
from mcp_server.markdown_parser import create_paper_file, parse_paper, add_backlinks_to_referenced_papers
from mcp_server.chroma_store import ChromaStore


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
    parser.add_argument("--related_papers", default="", help="Comma-separated related paper IDs")
    parser.add_argument("--date_read", default=date.today().isoformat(), help="Read date YYYY-MM-DD")
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
        "related_papers": related,
        "date_read": args.date_read,
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

    # Index in ChromaDB
    paper = parse_paper(filepath)
    if paper:
        store = ChromaStore()
        store.init_collection()
        store.upsert_paper(str(paper["id"]), paper)

        # Add reverse wikilinks from old papers → new paper so Obsidian graph
        # arrows show the direction of academic influence (old → new).
        # NOTE: When reading papers NON-chronologically, the script will now
        # warn and skip — manually fix the direction per SKILL.md §4.5.
        new_id = paper["id"]
        short_name = paper.get("short_name", "")
        new_year = paper.get("year")
        related = paper.get("related_papers", [])
        if not args.paper_id and short_name and related:
            add_backlinks_to_referenced_papers(
                new_id, short_name, related, new_paper_year=new_year
            )

        result = {
            "status": "ok",
            "paper_id": paper["id"],
            "file": str(filepath),
            "message": f"Paper indexed: {paper.get('title')}"
        }
    else:
        result = {"status": "error", "message": "Failed to parse created paper file"}

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
