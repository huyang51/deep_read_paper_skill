import re
import frontmatter
from datetime import date
from pathlib import Path
from typing import Optional
from mcp_server.config import PAPERS_DIR
from mcp_server.relations import (
    derive_relations, inverse_direction, merge_relation, normalize_relations,
    project_related_papers, relation_index, relations_of,
)

# The machine-managed section that carries Obsidian graph edges. Everything
# under it is written by sync_graph_edges() — never by prose.
FOLLOWUP_HEADER = "## 后续引用"


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


# Simple TTL cache for get_all_papers to avoid re-parsing every .md file on each call.
# Cache is keyed by (papers_dir, mtime) so changing vault_dir via PAPER_KB_VAULT_DIR
# environment variable does not serve stale entries from the previous vault.
_all_papers_cache: dict = {"papers_dir": None, "data": None, "mtime": 0.0, "ttl": 5.0}


def get_all_papers(papers_dir: Path = None) -> list[dict]:
    """Get all papers as list of metadata dicts. Results are cached with a short TTL."""
    if papers_dir is None:
        papers_dir = PAPERS_DIR

    import time
    now = time.time()

    # Use cache if fresh AND for the same directory (avoids stale data after vault switch)
    if (_all_papers_cache["data"] is not None
            and _all_papers_cache["papers_dir"] == str(papers_dir)
            and (now - _all_papers_cache["mtime"]) < _all_papers_cache["ttl"]):
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
    _all_papers_cache["papers_dir"] = str(papers_dir)
    return papers


def invalidate_papers_cache():
    """Invalidate the papers cache (call after file changes)."""
    _all_papers_cache["data"] = None
    _all_papers_cache["mtime"] = 0.0
    _all_papers_cache["papers_dir"] = None


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
    # The previous version of this file: read it BEFORE the idempotency delete
    # below, because this whitelist is a full rewrite and anything the caller
    # omits (relations, read_mode) must be carried over deliberately.
    prev = get_paper_by_id(paper_id, papers_dir) if paper_id is not None else None
    kept_newline = None
    if paper_id is None:
        paper_id = get_next_id(papers_dir)
        paper_data["id"] = paper_id
    else:
        # Idempotency check: if a file with this ID already exists, overwrite it
        if prev and prev.get("file"):
            existing_path = papers_dir.parent / prev["file"]
            if existing_path.exists():
                # Read the convention BEFORE deleting: an idempotent re-index of
                # an LF paper must not come back as a whole-file CRLF rewrite.
                kept_newline = _detect_newline(existing_path)
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

    # Structured relations carry the semantics; related_papers is the legacy
    # projection other queries still read. Declared relations win and the
    # projection is regenerated, so the two can never disagree in a fresh write.
    relations, _ = normalize_relations(paper_data.get("relations"))
    related_papers = paper_data.get("related_papers", [])
    if not relations and prev:
        # Legacy path: an update that does not mention relations must not erase
        # the ones already recorded (that would silently drop graph edges).
        relations = relations_of(prev)
        related_papers = related_papers or prev.get("related_papers", [])
    if relations:
        related_papers = project_related_papers(relations)

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
        "relations": relations,
        "related_papers": related_papers,
        "date_read": paper_data.get("date_read", date.today().isoformat()),
        # read_mode was accepted by the MCP tool / CLI and rendered in reports,
        # but this whitelist never carried it — so it was silently dropped on
        # write and *erased* on an idempotent re-index. Fixed here.
        "read_mode": (paper_data.get("read_mode")
                      or (prev or {}).get("read_mode") or "standard"),
        "aliases": paper_data.get("aliases", []),
        "tags": paper_data.get("tags", []),
    }

    body = paper_data.get("body", "")

    post = frontmatter.Post(body, **metadata)
    file_content = frontmatter.dumps(post)

    # frontmatter.dumps may add extra blank lines at the end; normalize
    newline = kept_newline or _detect_newline(filepath)
    with open(filepath, "w", encoding="utf-8", newline=newline) as f:
        f.write(file_content.rstrip() + "\n")

    invalidate_papers_cache()
    return filepath


def _paper_path(paper: dict, papers_dir: Path) -> Optional[Path]:
    file_rel = paper.get("file", "")
    if not file_rel:
        return None
    filepath = papers_dir.parent / file_rel
    return filepath if filepath.exists() else None


def _detect_newline(path: Path) -> str:
    """Return the newline convention of an existing file ("\\r\\n" or "\\n").

    Rewriting a paper must not re-line-end it. ``open(path, "w")`` in text mode
    translates every "\\n" to os.linesep, so on Windows a relation synced onto
    an LF paper (or a migration adding two frontmatter keys) rewrites all of its
    lines as CRLF — a whole-file diff for a two-line change. Detection is
    best-effort: a new or unreadable file writes LF, which is what the vault
    already tolerates and what keeps output platform-independent.
    """
    try:
        head = path.open("rb").read(4096)
    except OSError:
        return "\n"
    return "\r\n" if b"\r\n" in head else "\n"


def _write_paper(path: Path, metadata: dict, body: str) -> None:
    post = frontmatter.Post(body, **metadata)
    text = frontmatter.dumps(post).rstrip() + "\n"
    with open(path, "w", encoding="utf-8", newline=_detect_newline(path)) as f:
        f.write(text)


def _split_followup(body: str) -> tuple[str, list[str], str]:
    """Split a body into (before, followup_links, after) around FOLLOWUP_HEADER."""
    if FOLLOWUP_HEADER not in body:
        return body, [], ""
    before, rest = body.split(FOLLOWUP_HEADER, 1)
    m = re.search(r"\n## ", rest)
    section, after = (rest[:m.start()], rest[m.start():]) if m else (rest, "")
    return before, re.findall(r"\[\[([^\]]+)\]\]", section), after


def _rebuild_followup(before: str, links: list[str], after: str) -> str:
    before, after = before.rstrip(), after.rstrip()
    if not links:
        joined = "\n\n".join(x for x in (before, after) if x)
        return (joined + "\n") if joined else ""
    block = FOLLOWUP_HEADER + "\n\n" + "\n".join(f"- [[{s}]]" for s in sorted(links))
    parts = [p for p in (before, block, after) if p]
    return "\n\n".join(parts) + "\n"


def update_paper_relations(paper_id: int, entries: list[dict],
                           papers_dir: Path = None) -> bool:
    """Write ``relations`` (+ the ``related_papers`` projection) into an existing
    paper, leaving body and every other frontmatter key untouched.

    Surgically updating matters for migration: a full rewrite through
    create_paper_file would re-derive the filename and drop any field the
    whitelist does not know about.
    """
    if papers_dir is None:
        papers_dir = PAPERS_DIR

    paper = get_paper_by_id(paper_id, papers_dir)
    if not paper:
        return False
    path = _paper_path(paper, papers_dir)
    if path is None:
        return False

    metadata = {k: v for k, v in paper.items() if k not in ("body", "file")}
    metadata["relations"] = entries
    metadata["related_papers"] = project_related_papers(entries)
    _write_paper(path, metadata, paper.get("body", ""))
    invalidate_papers_cache()
    return True


def sync_relations(paper: dict, papers_dir: Path = None) -> dict:
    """Mirror a paper's declared relations onto the papers it points at.

    Reciprocity is what keeps a hand-maintained graph from rotting: A's
    "B is my successor" is only trustworthy if B's frontmatter says "A is my
    predecessor" with the same type. Doing that by hand is what the old
    workflow asked for (and rarely got), so it happens on write instead.

    Also regenerates ``related_papers`` on both sides as the projection of the
    relations, so the legacy field can never drift from the semantics.

    Returns ``{"updated": [ids], "missing": [ids], "derived": [ids]}`` where
    ``derived`` marks papers whose legacy ``related_papers`` had to be migrated
    on the fly (they had no relations yet).
    """
    if papers_dir is None:
        papers_dir = PAPERS_DIR

    result = {"updated": [], "missing": [], "derived": []}
    entries = relations_of(paper)
    if not entries:
        return result

    index = relation_index(get_all_papers(papers_dir))
    self_id, self_short = paper.get("id"), paper.get("short_name", "")

    for entry in entries:
        other = index.get(entry["target"])
        if other is None:
            result["missing"].append(entry["target"])
            continue
        path = _paper_path(other, papers_dir)
        if path is None:
            result["missing"].append(entry["target"])
            continue

        back = relations_of(other)
        if not back and other.get("related_papers"):
            # Touch-migrate: the reciprocal cannot be added to a legacy paper
            # without first materialising its own relations, or the projection
            # below would delete the ids it still carries.
            back, _ = derive_relations(other, index)
            result["derived"].append(other["id"])

        back, changed = merge_relation(back, self_id, entry["type"],
                                       inverse_direction(entry["direction"]),
                                       note=entry["note"])
        projection = project_related_papers(back)
        if changed or other.get("related_papers") != projection:
            metadata = {k: v for k, v in other.items()
                        if k not in ("body", "file", "relations", "related_papers")}
            metadata["relations"] = back
            metadata["related_papers"] = projection
            _write_paper(path, metadata, other.get("body", ""))
            result["updated"].append(other["id"])

    if result["updated"] or result["derived"]:
        invalidate_papers_cache()
    return result


def sync_graph_edges(paper: dict, papers_dir: Path = None) -> dict:
    """Keep ``## 后续引用`` sections matching the declared directions.

    Obsidian draws an arrow from the file that *contains* ``[[X]]`` to X. The
    academic-influence reading (early → late) therefore requires the link to sit
    in the earlier paper. The old workflow assumed the paper being indexed was
    the later one and asked the agent to repair non-chronological reads by hand;
    with a declared direction the link simply goes where the data says.

    Repair scope is deliberately conservative: a link is only *removed* from a
    section when a declaration exists for that pair and points the other way.
    Links to papers with no declaration are legacy and left untouched.

    ``peer`` relations produce no edge at all — parallel work has no influence
    direction, and inventing one is what the arrow convention was fighting.
    """
    if papers_dir is None:
        papers_dir = PAPERS_DIR

    index = relation_index(get_all_papers(papers_dir))
    by_short = {p.get("short_name", ""): pid for pid, p in index.items() if p.get("short_name")}
    by_short.update({p.get("title", ""): pid for pid, p in index.items() if p.get("title")})

    # ── desired state ────────────────────────────────────────────────────
    wanted: dict[int, set[str]] = {}
    declared: dict[tuple[int, int], dict] = {}
    for pid, p in index.items():
        for e in relations_of(p):
            declared[(pid, e["target"])] = e
            other_short = index.get(e["target"], {}).get("short_name", "")
            self_short = p.get("short_name", "")
            if e["direction"] == "successor" and other_short:
                wanted.setdefault(pid, set()).add(other_short)      # self is earlier
            elif e["direction"] == "predecessor" and self_short:
                wanted.setdefault(e["target"], set()).add(self_short)  # other is earlier

    # ── apply ────────────────────────────────────────────────────────────
    # Visit every paper that declares something, not only the ones that want an
    # edge: a stale link to a pair declared `peer` wants no edge anywhere, so
    # under the narrower rule nobody would ever look at it and the declaration
    # would keep losing to a link nobody cleaned up.
    owners = [pid for pid, p in index.items() if relations_of(p)]
    moved, updated = [], []
    for pid in dict.fromkeys(owners + list(wanted)):
        want = wanted.get(pid, set())
        p = index.get(pid)
        if p is None:
            continue
        path = _paper_path(p, papers_dir)
        if path is None:
            continue
        before, existing, after = _split_followup(p.get("body", ""))
        keep = []
        for link in existing:
            tid = by_short.get(link)
            rel = declared.get((pid, tid)) if tid is not None else None
            if rel is None or link in want:
                keep.append(link)          # legacy link, or still correct
            else:
                moved.append((pid, link))
        links = sorted(set(keep) | want)
        if links != sorted(set(existing)):
            if links != existing:
                metadata = {k: v for k, v in p.items() if k not in ("body", "file")}
                _write_paper(path, metadata, _rebuild_followup(before, links, after))
                updated.append(pid)

    if updated:
        invalidate_papers_cache()
    return {"updated": updated, "moved": moved}


def sync_paper_relations(paper: dict, papers_dir: Path = None) -> dict:
    """Both halves of the structured-relation write path, in one call.

    Callers that create or update a paper should use this instead of either
    half: frontmatter without edges leaves the Obsidian graph stale, edges
    without frontmatter leave the semantics missing.

    Returns a distinctly-named key per half — ``mirrored`` (papers that received
    a reciprocal entry) vs ``edges_updated`` (papers whose ``## 后续引用`` was
    rewritten). They answer different questions, and both halves calling their
    list ``updated`` is how the first version of this function reported the edge
    target as the mirrored paper.
    """
    mirrored = sync_relations(paper, papers_dir)
    edges = sync_graph_edges(paper, papers_dir)
    return {
        "mirrored": mirrored["updated"],
        "missing": mirrored["missing"],
        "derived": mirrored["derived"],
        "edges_updated": edges["updated"],
        "edges_moved": edges["moved"],
    }


def add_backlinks_to_referenced_papers(new_paper_id: int, new_short_name: str,
                                       related_paper_ids: list[int],
                                       papers_dir: Path = None,
                                       new_paper_year: int = None) -> list[int]:
    """Deprecated legacy wrapper — use sync_paper_relations().

    Kept because older callers pass a flat id list with no semantics. Direction
    is derived from years (the best available guess) and the legacy
    ``related_papers`` entries are migrated on the fly, so the outcome is the
    same as a declared relation whose direction came from publication order.
    """
    if papers_dir is None:
        papers_dir = PAPERS_DIR

    paper = get_paper_by_id(new_paper_id, papers_dir)
    if not paper:
        return []

    index = relation_index(get_all_papers(papers_dir))
    entries, _ = derive_relations(paper, index)
    if not entries:
        return []
    paper = dict(paper, relations=entries)
    return sync_relations(paper, papers_dir)["updated"]


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
