import re
import frontmatter
from datetime import date
from pathlib import Path
from typing import Optional
from mcp_server.config import PAPERS_DIR


def parse_paper(path: Path) -> dict:
    """Parse a paper markdown file, extracting YAML frontmatter and body."""
    if not path.exists():
        return None
    post = frontmatter.load(str(path))
    metadata = dict(post.metadata)
    try:
        metadata["file"] = str(path.relative_to(PAPERS_DIR.parent))
    except ValueError:
        metadata["file"] = str(path)
    metadata["body"] = post.content
    return metadata


def extract_wikilinks(content: str) -> list[str]:
    """Extract [[wikilinks]] from markdown content."""
    return re.findall(r'\[\[([^\]]+)\]\]', content)


def build_relation_graph() -> dict:
    """Scan all papers, build {paper_id: [related_paper_ids]} graph."""
    graph = {}
    if not PAPERS_DIR.exists():
        return graph

    for paper_file in PAPERS_DIR.glob("*.md"):
        post = frontmatter.load(str(paper_file))
        paper_id = post.metadata.get("id")
        if paper_id is None:
            continue
        related = post.metadata.get("related_papers", [])
        if isinstance(related, list):
            graph[paper_id] = related

    return graph


# Simple TTL cache for get_all_papers to avoid re-parsing every .md file on each call
_all_papers_cache: dict = {"data": None, "mtime": 0.0, "ttl": 5.0}


def get_all_papers(papers_dir: Path = None) -> list[dict]:
    """Get all papers as list of metadata dicts. Results are cached with a short TTL."""
    if papers_dir is None:
        papers_dir = PAPERS_DIR

    import time
    now = time.time()

    # Use cache if fresh
    if _all_papers_cache["data"] is not None and (now - _all_papers_cache["mtime"]) < _all_papers_cache["ttl"]:
        return _all_papers_cache["data"]

    papers = []
    if not papers_dir.exists():
        return papers

    for paper_file in sorted(papers_dir.glob("*.md")):
        parsed = parse_paper(paper_file)
        if parsed:
            papers.append(parsed)

    _all_papers_cache["data"] = papers
    _all_papers_cache["mtime"] = now
    return papers


def invalidate_papers_cache():
    """Invalidate the papers cache (call after file changes)."""
    _all_papers_cache["data"] = None
    _all_papers_cache["mtime"] = 0.0


def get_paper_by_id(paper_id: int, papers_dir: Path = None) -> Optional[dict]:
    """Find a paper by its ID. Returns the first match; logs a warning if
    multiple files share the same ID (should not happen after idempotency fix)."""
    if papers_dir is None:
        papers_dir = PAPERS_DIR

    matches = []
    for paper_file in papers_dir.glob("*.md"):
        parsed = parse_paper(paper_file)
        if parsed and parsed.get("id") == paper_id:
            matches.append(parsed)

    if not matches:
        return None
    if len(matches) > 1:
        import logging
        logger = logging.getLogger("paper_kb_mcp")
        logger.warning(f"Duplicate paper_id={paper_id} found in {len(matches)} files: "
                       f"{[m.get('file', '?') for m in matches]}; returning newest.")
    return matches[0]


def get_next_id(papers_dir: Path = None) -> int:
    """Get the next available paper ID."""
    papers = get_all_papers(papers_dir)
    if not papers:
        return 1
    return max(p.get("id", 0) for p in papers) + 1


def _make_safe_filename(name: str) -> str:
    """Convert a short name or title to a safe filename fragment. Preserves case.
    Falls back to 'untitled' if the name consists entirely of punctuation/symbols."""
    safe = re.sub(r'[^\w\s-]', '', name)
    safe = re.sub(r'[-\s]+', '-', safe).strip('-')
    return safe[:50] if safe else "untitled"


def create_paper_file(paper_data: dict, papers_dir: Path = None) -> Path:
    """Create a paper markdown file with YAML frontmatter. Returns the file path.

    Idempotent: if a paper with the same ID already exists, the existing file is
    overwritten (updated) rather than creating a duplicate file.
    """
    import logging
    logger = logging.getLogger("paper_kb_mcp")

    if papers_dir is None:
        papers_dir = PAPERS_DIR

    papers_dir.mkdir(parents=True, exist_ok=True)

    paper_id = paper_data.get("id")
    if paper_id is None:
        paper_id = get_next_id(papers_dir)
        paper_data["id"] = paper_id
    else:
        # Idempotency check: if a file with this ID already exists, overwrite it
        existing = get_paper_by_id(paper_id, papers_dir)
        if existing and existing.get("file"):
            existing_path = papers_dir.parent / existing["file"]
            if existing_path.exists():
                # Delete old file first to avoid duplicate ID files
                existing_path.unlink()

    short_name = paper_data.get("short_name", "")
    if short_name:
        safe_name = _make_safe_filename(short_name)
    else:
        title = paper_data.get("title", "Untitled")
        safe_name = _make_safe_filename(title)
    filename = f"{safe_name}.md"
    filepath = papers_dir / filename

    # Collision check: if a DIFFERENT-ID paper already uses this filename, warn
    if filepath.exists():
        try:
            existing = parse_paper(filepath)
            existing_id = existing.get("id") if existing else None
            if existing_id is not None and existing_id != paper_id:
                logger.warning(
                    f"Filename collision: '{filename}' already used by paper_id={existing_id}, "
                    f"now being overwritten by paper_id={paper_id}. Consider using unique short_names."
                )
        except Exception:
            pass

    # Build frontmatter metadata
    metadata = {
        "id": paper_id,
        "title": paper_data.get("title", ""),
        "short_name": short_name,
        "year": paper_data.get("year", ""),
        "venue": paper_data.get("venue", ""),
        "authors": paper_data.get("authors", []),
        "method_category": paper_data.get("method_category", ""),
        "problem_domain": paper_data.get("problem_domain", ""),
        "keywords": paper_data.get("keywords", []),
        "core_contribution": paper_data.get("core_contribution", ""),
        "novelty_level": paper_data.get("novelty_level", ""),
        "related_papers": paper_data.get("related_papers", []),
        "date_read": paper_data.get("date_read", date.today().isoformat()),
        "aliases": paper_data.get("aliases", []),
        "tags": paper_data.get("tags", []),
    }

    body = paper_data.get("body", "")

    post = frontmatter.Post(body, **metadata)
    file_content = frontmatter.dumps(post)

    # frontmatter.dumps may add extra blank lines at the end; normalize
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(file_content.rstrip() + "\n")

    invalidate_papers_cache()
    return filepath


def add_backlinks_to_referenced_papers(new_paper_id: int, new_short_name: str,
                                         related_paper_ids: list[int],
                                         papers_dir: Path = None) -> list[int]:
    """Add reverse wikilinks from old referenced papers back to the new paper.

    Obsidian graph arrows point FROM the file containing a [[wikilink]] TO the
    target.  To achieve "old paper → new paper" direction (academic influence
    flow), we add [[new_short_name]] wikilinks inside each old paper's body.

    Returns the list of paper IDs that were successfully updated.
    """
    if papers_dir is None:
        papers_dir = PAPERS_DIR

    if not related_paper_ids or not new_short_name:
        return []

    backlink = f"[[{new_short_name}]]"
    updated = []

    for ref_id in related_paper_ids:
        if ref_id == new_paper_id:
            continue

        paper = get_paper_by_id(ref_id, papers_dir)
        if not paper:
            continue

        file_rel = paper.get("file", "")
        if not file_rel:
            continue

        filepath = papers_dir.parent / file_rel
        if not filepath.exists():
            continue

        try:
            post = frontmatter.load(str(filepath))
            body = post.content

            if backlink in body:
                continue

            if "## 后续引用" in body:
                body = body.rstrip() + f"\n- {backlink}\n"
            else:
                body = body.rstrip() + f"\n\n## 后续引用\n\n- {backlink}\n"

            new_post = frontmatter.Post(body, **post.metadata)
            file_content = frontmatter.dumps(new_post)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(file_content.rstrip() + "\n")

            updated.append(ref_id)
        except Exception:
            continue

    if updated:
        invalidate_papers_cache()

    return updated


def delete_paper_file(paper_id: int, papers_dir: Path = None) -> bool:
    """Delete a paper markdown file by its ID. Returns True if deleted."""
    if papers_dir is None:
        papers_dir = PAPERS_DIR

    paper = get_paper_by_id(paper_id, papers_dir)
    if not paper:
        return False

    file_rel = paper.get("file", "")
    if file_rel:
        # Construct path relative to the papers_dir's parent vault
        vault_dir = papers_dir.parent
        filepath = vault_dir / file_rel
        if filepath.exists():
            filepath.unlink()
            return True

    # Fallback: scan all .md files in papers_dir by frontmatter ID
    for f in papers_dir.glob("*.md"):
        try:
            parsed = parse_paper(f)
            if parsed and parsed.get("id") == paper_id:
                f.unlink()
                invalidate_papers_cache()
                return True
        except Exception:
            continue

    return False
