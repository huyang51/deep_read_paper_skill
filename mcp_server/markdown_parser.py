import contextlib
import os
import re
import time
import uuid
import frontmatter
from datetime import date
from pathlib import Path
from typing import Optional
from mcp_server.config import PAPERS_DIR
from mcp_server.relations import (
    coerce_id, inverse_direction, merge_relation,
    normalize_relations, project_related_papers, relation_index, relations_of,
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


def _clean_link_name(raw: str) -> str:
    """Strip the alias and heading-anchor parts of a wikilink target.

    ``[[FLMR-v2|论文二]]`` names FLMR-v2; ``[[note#section]]`` names note.
    Hand-written links use aliases constantly (that is what they are for), and
    until here every resolver — cross_refs inference, _drop_links_to, the
    cleanup after a deletion — compared the raw ``a|b`` text against clean
    labels and missed, so deleting a paper left its aliased ``## 后续引用``
    edge as an Obsidian ghost while the cleanup cheerfully reported "nothing
    to do". Link TEXT is only ever rewritten from this cleaned form, matching
    what sync has always written (``- [[stem]]``).
    """
    return raw.split("|", 1)[0].split("#", 1)[0].strip()


def extract_wikilinks(content: str) -> list[str]:
    """Extract [[wikilinks]] from markdown content (targets, aliases stripped)."""
    return [_clean_link_name(m) for m in re.findall(r'\[\[([^\]]+)\]\]', content)]


# Simple TTL cache for get_all_papers to avoid re-parsing every .md file on each call.
# Cache is keyed by (papers_dir, mtime) so changing vault_dir via PAPER_KB_VAULT_DIR
# environment variable does not serve stale entries from the previous vault.
_all_papers_cache: dict = {"papers_dir": None, "data": None, "mtime": 0.0, "ttl": 5.0,
                           "errors": []}


def get_all_papers(papers_dir: Path = None) -> list[dict]:
    """Get all papers as list of metadata dicts. Results are cached with a short TTL.

    A file whose frontmatter does not parse (a stray tab, an unclosed quote)
    used to raise straight through this scan — and since every tool and the
    watcher go through it, one hand-edited note took the whole vault down.
    Now the file is skipped and the reason is kept in the scan cache, for
    get_scan_errors() and index_all_papers' skipped channel to report.
    """
    if papers_dir is None:
        papers_dir = PAPERS_DIR

    now = time.time()

    # Use cache if fresh AND for the same directory (avoids stale data after vault switch)
    if (_all_papers_cache["data"] is not None
            and _all_papers_cache["papers_dir"] == str(papers_dir)
            and (now - _all_papers_cache["mtime"]) < _all_papers_cache["ttl"]):
        return _all_papers_cache["data"]

    papers = []
    _all_papers_cache["errors"] = []
    if not papers_dir.exists():
        return papers

    for paper_file in sorted(papers_dir.glob("*.md")):
        try:
            parsed = parse_paper(paper_file)
        except Exception as e:
            _all_papers_cache["errors"].append(
                f"{paper_file.name}: {type(e).__name__}: {e}")
            continue
        if parsed:
            papers.append(parsed)

    _all_papers_cache["data"] = papers
    _all_papers_cache["mtime"] = now
    _all_papers_cache["papers_dir"] = str(papers_dir)
    return papers


def get_scan_errors() -> list:
    """Per-file parse failures from the most recent get_all_papers scan.

    Empty both when the scan was clean and when it hit the TTL cache — errors
    live in the same cache as the data they belong to, so a fresh cache hit
    keeps reporting the errors of the scan that populated it.
    """
    return list(_all_papers_cache.get("errors") or [])


def invalidate_papers_cache():
    """Invalidate the papers cache (call after file changes)."""
    _all_papers_cache["data"] = None
    _all_papers_cache["mtime"] = 0.0
    _all_papers_cache["papers_dir"] = None
    _all_papers_cache["errors"] = []


def get_paper_by_id(paper_id: int, papers_dir: Path = None) -> Optional[dict]:
    """Find a paper by its ID. Returns the first match; logs a warning if
    multiple files share the same ID (should not happen after idempotency fix)."""
    if papers_dir is None:
        papers_dir = PAPERS_DIR

    paper_id = coerce_id(paper_id)
    matches = []
    for paper_file in papers_dir.glob("*.md"):
        try:
            parsed = parse_paper(paper_file)
        except Exception:
            # Same tolerance as get_all_papers' scan: one unrelated file with
            # malformed frontmatter (a stray tab, an unclosed quote) must not
            # raise through a lookup of a perfectly valid id — it took
            # paper_get/paper_remove/paper_find_related down vault-wide.
            continue
        if parsed and coerce_id(parsed.get("id")) == paper_id:
            matches.append(parsed)

    if not matches:
        return None
    if len(matches) > 1:
        import logging
        logger = logging.getLogger("paper_kb_mcp")
        # Says which file actually won: this used to claim "returning newest"
        # while returning matches[0], i.e. whatever glob() happened to list
        # first — the one message a user debugging duplicate ids would trust.
        logger.warning(
            f"Duplicate paper_id={paper_id} in {len(matches)} files: "
            f"{[m.get('file', '?') for m in matches]}; 本次用的是 "
            f"{matches[0].get('file', '?')}（顺序取决于文件系统，请修掉重复 id）。")
    return matches[0]


def get_next_id(papers_dir: Path = None) -> int:
    """Get the next available paper ID.

    Ids are coerced and unusable ones skipped: a single hand-edited ``id: "7"``
    used to make ``max()`` raise TypeError, which took down every paper_index
    call for the whole vault.
    """
    ids = [i for i in (coerce_id(p.get("id")) for p in get_all_papers(papers_dir))
           if i is not None]
    return max(ids) + 1 if ids else 1


@contextlib.contextmanager
def _vault_write_lock(papers_dir: Path, timeout: float = 30.0):
    """Cross-process lock over read-then-write vault operations.

    The race this closes: two Claude Code sessions index papers at the same
    time, both run get_next_id's "scan the directory, take max+1" against the
    same snapshot, both pick id 7 — one paper's note silently lands on top of
    the other's in relations, wikilinks and the index. Allocation and write
    must therefore happen under one lock file in the vault. Advisory locks:
    msvcrt.locking on Windows, fcntl.flock elsewhere. Note fcntl locks are
    per-process, so this guards cross-process races, not same-process threads
    (Python's GIL + the short critical section make those benign).

    The lock file is `.paper-kb.lock` — deliberately not *.md, so no paper
    glob ever mistakes it for a note.
    """
    lock_path = papers_dir / ".paper-kb.lock"
    with open(lock_path, "a+") as f:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"获取知识库写入锁超时（{timeout:.0f}s）：另一个会话可能正在"
                        f"索引。锁文件：{lock_path} —— 若确认无并发会话，可删除它重试。")
                time.sleep(0.1)
        try:
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass  # already released or handle closed — nothing to recover


def _make_safe_filename(name: str) -> str:
    """Convert a short name or title to a safe filename fragment. Preserves case.
    Falls back to 'untitled' if the name consists entirely of punctuation/symbols."""
    safe = re.sub(r'[^\w\s-]', '', name)
    safe = re.sub(r'[-\s]+', '-', safe).strip('-')
    return safe[:50] if safe else "untitled"


def create_paper_file(paper_data: dict, papers_dir: Path = None) -> Path:
    """Create a paper markdown file with YAML frontmatter. Returns the file path.

    Idempotent: if a paper with the same ID already exists, the existing file is
    overwritten (updated) rather than creating a duplicate file. A filename that
    belongs to a *different* paper is a hard error, never an overwrite.

    When the caller supplies no id, allocation and write run under the vault
    write lock: two sessions indexing concurrently must not both observe the
    same max+1 and hand two papers the same id.
    """
    if papers_dir is None:
        papers_dir = PAPERS_DIR

    papers_dir.mkdir(parents=True, exist_ok=True)

    if coerce_id(paper_data.get("id")) is not None:
        # Explicit id: caller-owned, no allocation to race over.
        return _create_paper_file_impl(paper_data, papers_dir)
    with _vault_write_lock(papers_dir):
        return _create_paper_file_impl(paper_data, papers_dir)


def _create_paper_file_impl(paper_data: dict, papers_dir: Path) -> Path:
    paper_id = coerce_id(paper_data.get("id"))
    # The previous version of this file: read it BEFORE the idempotency delete
    # below, because this whitelist is a full rewrite and anything the caller
    # omits (relations, read_mode) must be carried over deliberately.
    prev = get_paper_by_id(paper_id, papers_dir) if paper_id is not None else None

    # The file this paper already occupies — remembered, not deleted. The old
    # order unlinked it here and only wrote the replacement ~70 lines later, so
    # anything that failed in between (a locked file, a disk error, an
    # unserializable metadata value) left the note destroyed and the update
    # failed. Deleting after the new file is safely on disk makes the rewrite
    # atomic from the reader's point of view.
    old_path = None
    if prev and prev.get("file"):
        candidate = _safe_vault_path(prev["file"], papers_dir)
        if candidate is not None and candidate.exists():
            old_path = candidate
    # Read the convention before writing: an idempotent re-index of an LF paper
    # must not come back as a whole-file CRLF rewrite.
    kept_newline = _detect_newline(old_path) if old_path else None

    if paper_id is None:
        paper_id = get_next_id(papers_dir)
        paper_data["id"] = paper_id

    short_name = paper_data.get("short_name", "")
    if short_name:
        safe_name = _make_safe_filename(short_name)
    else:
        title = paper_data.get("title", "Untitled")
        safe_name = _make_safe_filename(title)
    filename = f"{safe_name}.md"
    filepath = papers_dir / filename

    # Refuse the collision; do not warn and write. This filename may already
    # belong to a *different* paper, and the old code logged a warning and
    # truncated it: the note became a description of another paper while its
    # ChromaDB entry, every `relations` entry naming its id and every [[wikilink]]
    # to its short_name stayed put — a vault that quietly lies, returned as
    # {"status": "ok"}. A clash is a naming mistake the caller can fix by picking
    # another short_name; a destroyed note is not recoverable. Rewriting the
    # paper that already owns this filename is the idempotent update this
    # function exists for, and is exempt.
    if filepath.exists() and (old_path is None or filepath != old_path):
        existing = parse_paper(filepath)
        existing_id = coerce_id((existing or {}).get("id"))
        holder = f"论文 ID={existing_id}" if existing_id is not None else "另一个没有 id 的条目"
        raise FileExistsError(
            f"文件名冲突：{filename} 已属于{holder}，不能覆盖。"
            f"请给当前这篇（ID={paper_id}）换一个 short_name 后重试。"
        )

    # `relations` is the only relation input there is: the projection is
    # regenerated from it on every write, so the two fields cannot disagree and a
    # hand-typed `related_papers` can never reach the file. A partial update that
    # omits relations keeps whatever the note already declared — that is the
    # note's own data, not an input field, and dropping it on a partial write
    # would delete graph edges nobody meant to touch.
    relations, _ = normalize_relations(paper_data.get("relations"))
    if not relations and prev:
        relations = relations_of(prev)
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

    # frontmatter.dumps may add extra blank lines at the end; normalize.
    # Written aside and swapped in: os.replace is atomic within a filesystem, so
    # readers see either the old note or the new one, never a half-written file.
    newline = kept_newline or _detect_newline(filepath)
    tmp_path = filepath.with_name(f"{filepath.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8", newline=newline) as f:
            f.write(file_content.rstrip() + "\n")
        os.replace(tmp_path, filepath)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    # A renamed short_name moves the paper: drop the file it used to live in now
    # that the new one is on disk, so the id never spans two files.
    if old_path is not None and old_path != filepath and old_path.exists():
        old_path.unlink()

    invalidate_papers_cache()
    return filepath


def _safe_vault_path(file_rel: str, papers_dir: Path) -> Optional[Path]:
    """Resolve a frontmatter ``file:`` value, refusing paths outside the vault.

    The field is hand-editable in Obsidian and an imported vault may carry any
    value at all; without this guard an ``update_paper_relations`` writes and a
    ``delete_paper_file`` unlinks wherever the string points. An escape resolves
    to None — every caller already treats that as "nothing to do here" — so the
    write/delete through the poisoned path is blocked while one bad note cannot
    break operations on the rest of the vault.
    """
    if not file_rel:
        return None
    vault_dir = papers_dir.parent
    try:
        resolved = (vault_dir / file_rel).resolve()
        if not resolved.is_relative_to(vault_dir.resolve()):
            return None
    except (OSError, ValueError):
        return None
    return resolved


def _paper_path(paper: dict, papers_dir: Path) -> Optional[Path]:
    filepath = _safe_vault_path(paper.get("file", ""), papers_dir)
    return filepath if filepath is not None and filepath.exists() else None


def _fresh_paper(paper: dict, papers_dir: Path) -> Optional[dict]:
    """Re-read a paper's note from disk before a read-modify-write on it.

    The sync/cleanup paths receive their papers from the ≤5 s TTL cache
    (`get_all_papers`). A whole-file rewrite built from that snapshot silently
    ROLLED BACK any external edit (Obsidian, another session) made after the
    scan — live repro 2026-10-04: a body line appended between cache-fill and
    mirror-write simply disappeared. Re-read the file first and operate on the
    fresh content; None means "note gone or unparseable" — a hand-broken file
    must be left alone, not repaired from a stale copy. The write lock inside
    _write_paper plus this narrow reload window is the guard; a true
    cross-process read-modify-write would need per-file versions, which the
    vault model (single-writer-per-note, script-managed edges) does not pay.
    """
    path = _paper_path(paper, papers_dir)
    if path is None:
        return None
    try:
        fresh = parse_paper(path)
    except Exception:
        return None
    return fresh or None


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
        # Closed explicitly, not left to refcounting: a dangling handle on
        # Windows keeps the file locked, and the caller's next act on an update
        # is to unlink the previous path.
        with path.open("rb") as handle:
            head = handle.read(4096)
    except OSError:
        return "\n"
    return "\r\n" if b"\r\n" in head else "\n"


def _write_paper(path: Path, metadata: dict, body: str) -> None:
    """Atomically rewrite an existing paper note under the vault write lock.

    Same discipline as create's write path, for the two ways it used to differ:
    a direct `open(path, "w")` truncates first, so a concurrent reader (the
    watcher, Obsidian, another session) could see a half-written file; and the
    fixed `name + ".tmp"` sidecar collided across the sync/cleanup paths, so
    two writers could step on each other's temp file before either replace.
    """
    post = frontmatter.Post(body, **metadata)
    text = frontmatter.dumps(post).rstrip() + "\n"
    newline = _detect_newline(path)
    with _vault_write_lock(path.parent):
        tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8", newline=newline) as f:
                f.write(text)
            os.replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise


def _link_text(paper: dict) -> str:
    """The text a ``[[wikilink]]`` to this paper must use to resolve in Obsidian.

    Wikilinks match the *file stem*, not the short name: ``_make_safe_filename``
    collapses whitespace to ``-``, so short_name ``FLMR v2`` lives in
    ``FLMR-v2.md`` and a ``[[FLMR v2]]`` link only draws a ghost node in the
    graph. Falls back to the short name when the file field is absent.
    """
    f = paper.get("file", "")
    if f:
        return Path(f).stem
    return paper.get("short_name", "")


def _split_followup(body: str) -> tuple[str, list[str], str]:
    """Split a body into (before, followup_links, after) around FOLLOWUP_HEADER."""
    if FOLLOWUP_HEADER not in body:
        return body, [], ""
    before, rest = body.split(FOLLOWUP_HEADER, 1)
    m = re.search(r"\n## ", rest)
    section, after = (rest[:m.start()], rest[m.start():]) if m else (rest, "")
    return before, [_clean_link_name(x) for x in
                    re.findall(r"\[\[([^\]]+)\]\]", section)], after


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


def _link_resolver(index: dict) -> dict:
    """Link text (or legacy label) -> paper id, first form wins.

    Shared by everything that must decide which paper a ``[[wikilink]]`` names:
    the canonical file stem first, then the short name and the title as the
    forms older vaults wrote.
    """
    resolver: dict[str, int] = {}
    for pid, p in index.items():
        for label in (_link_text(p), p.get("short_name", ""), p.get("title", "")):
            if label:
                resolver.setdefault(label, pid)
    return resolver


def _drop_links_to(body: str, resolver: dict, dead_ids: set) -> tuple[str, bool]:
    """Remove ``## 后续引用`` links that resolve to any of ``dead_ids``.

    Returns ``(body, changed)``; bodies without the section come back untouched.
    """
    before, links, after = _split_followup(body)
    if not links:
        return body, False
    kept = [l for l in links if resolver.get(l) not in dead_ids]
    if kept == links:
        return body, False
    return _rebuild_followup(before, kept, after), True


def sync_relations(paper: dict, papers_dir: Path = None) -> dict:
    """Mirror a paper's declared relations onto the papers it points at.

    Reciprocity is what keeps a hand-maintained graph from rotting: A's
    "B is my successor" is only trustworthy if B's frontmatter says "A is my
    predecessor" with the same type. Doing that by hand is what the old
    workflow asked for (and rarely got), so it happens on write instead.

    Also regenerates ``related_papers`` on both sides as the projection of the
    relations, so the legacy field can never drift from the semantics.

    And it self-heals removal: a mirror this sync (or an earlier one) wrote
    carries ``synced: true``; when the owning declaration is gone, the stale
    mirror — and the followup edge that went with it — is cleaned up. Entries
    the *other* paper declared on its own initiative carry no flag and are
    never touched.

    Returns ``{"updated": [ids], "missing": [ids], "healed": [ids]}``, where
    ``healed`` marks papers whose stale mirror was removed.
    """
    if papers_dir is None:
        papers_dir = PAPERS_DIR

    result = {"updated": [], "missing": [], "healed": []}
    entries = relations_of(paper)

    index = relation_index(get_all_papers(papers_dir))
    self_id = coerce_id(paper.get("id"))

    for entry in entries:
        other = index.get(entry["target"])
        if other is None:
            result["missing"].append(entry["target"])
            continue
        path = _paper_path(other, papers_dir)
        if path is None:
            result["missing"].append(entry["target"])
            continue
        # The mirror rewrites the OTHER file wholesale — merge onto what is on
        # disk right now, never onto the ≤5 s cache snapshot (see _fresh_paper).
        fresh = _fresh_paper(other, papers_dir)
        if fresh is None:
            result["missing"].append(entry["target"])
            continue
        other = fresh

        back = relations_of(other)
        back, changed = merge_relation(back, self_id, entry["type"],
                                       inverse_direction(entry["direction"]),
                                       note=entry["note"], mark_synced=True)
        projection = project_related_papers(back)
        if changed or other.get("related_papers") != projection:
            metadata = {k: v for k, v in other.items()
                        if k not in ("body", "file", "relations", "related_papers")}
            metadata["relations"] = back
            metadata["related_papers"] = projection
            _write_paper(path, metadata, other.get("body", ""))
            result["updated"].append(other["id"])

    # ── self-heal: mirrors whose declaration is gone ─────────────────────
    # Runs even when self declares nothing at all — clearing relations is
    # exactly when stale mirrors are left behind. The followup edge goes with
    # the mirror: sync_graph_edges would keep it as "legacy" (no declaration
    # points the other way any more), so the heal strips it on both sides.
    declared_ids = {e["target"] for e in entries}
    resolver = _link_resolver(index)
    if self_id is not None:
        for pid, other in index.items():
            if pid == self_id or pid in declared_ids:
                continue
            path = _paper_path(other, papers_dir)
            if path is None:
                continue
            fresh = _fresh_paper(other, papers_dir)
            if fresh is None:
                continue
            other = fresh                    # heal writes whole files too
            back = relations_of(other)
            kept = [e for e in back
                    if not (e["target"] == self_id and e.get("synced"))]
            if len(kept) == len(back):
                continue
            body, _ = _drop_links_to(other.get("body", ""), resolver, {self_id})
            metadata = {k: v for k, v in other.items()
                        if k not in ("body", "file", "relations", "related_papers")}
            metadata["relations"] = kept
            metadata["related_papers"] = project_related_papers(kept)
            _write_paper(path, metadata, body)
            result["healed"].append(pid)
        if result["healed"]:
            # self may own the edge too (self was the earlier paper of a pair)
            me = index.get(self_id)
            my_path = _paper_path(me, papers_dir) if me else None
            if my_path is not None:
                me = _fresh_paper(me, papers_dir) or me
                body, dropped = _drop_links_to(me.get("body", ""), resolver,
                                               set(result["healed"]))
                if dropped:
                    metadata = {k: v for k, v in me.items()
                                if k not in ("body", "file")}
                    _write_paper(my_path, metadata, body)

    if result["updated"] or result["healed"]:
        invalidate_papers_cache()
    return result


def sync_graph_edges(papers_dir: Path = None) -> dict:
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
    by_short = _link_resolver(index)

    # ── desired state ────────────────────────────────────────────────────
    wanted: dict[int, set[str]] = {}
    wanted_ids: dict[int, set] = {}
    declared: dict[tuple[int, int], dict] = {}
    for pid, p in index.items():
        for e in relations_of(p):
            declared[(pid, e["target"])] = e
            other_link = _link_text(index.get(e["target"], {}))
            self_link = _link_text(p)
            if e["direction"] == "successor" and other_link:
                wanted.setdefault(pid, set()).add(other_link)      # self is earlier
                wanted_ids.setdefault(pid, set()).add(e["target"])
            elif e["direction"] == "predecessor" and self_link:
                wanted.setdefault(e["target"], set()).add(self_link)  # other is earlier
                wanted_ids.setdefault(e["target"], set()).add(pid)

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
        fresh = _fresh_paper(p, papers_dir)
        if fresh is None:
            continue
        p = fresh                # edge sections are rewritten whole-file
        before, existing, after = _split_followup(p.get("body", ""))
        keep = []
        for link in existing:
            tid = by_short.get(link)
            rel = declared.get((pid, tid)) if tid is not None else None
            if rel is None or link in want:
                keep.append(link)          # legacy link, or already canonical
            else:
                moved.append((pid, link))  # declared, and this text is not wanted
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
    target as the mirrored paper. ``healed`` (from the mirror half) lists papers
    whose stale mirror was removed after the owning declaration disappeared.
    """
    mirrored = sync_relations(paper, papers_dir)
    edges = sync_graph_edges(papers_dir)
    return {
        "mirrored": mirrored["updated"],
        "missing": mirrored["missing"],
        "healed": mirrored["healed"],
        "edges_updated": edges["updated"],
        "edges_moved": edges["moved"],
    }


def delete_paper_file(paper_id: int, papers_dir: Path = None) -> bool:
    """Delete EVERY note carrying this ID. Returns True if anything was deleted.

    The old code deleted the first match and returned. Duplicate ids do survive
    in hand-maintained vaults (get_paper_by_id warns about exactly that), and
    the leftover note made the watcher cheerfully re-index the "deleted" paper
    straight back into the vector store — deletion said ok while search kept
    returning it.
    """
    if papers_dir is None:
        papers_dir = PAPERS_DIR

    paper = get_paper_by_id(paper_id, papers_dir)
    if not paper:
        return False

    targets: set[Path] = set()
    # The guard refuses a ``file:`` that escapes the vault, so a hand-edited or
    # imported frontmatter cannot make this delete an arbitrary file; the
    # ID scan below still finds the real note inside papers_dir.
    filepath = _safe_vault_path(paper.get("file", ""), papers_dir)
    if filepath is not None and filepath.exists():
        targets.add(filepath)

    target_id = coerce_id(paper_id)
    for f in papers_dir.glob("*.md"):
        try:
            parsed = parse_paper(f)
        except Exception:
            continue
        if parsed and coerce_id(parsed.get("id")) == target_id:
            targets.add(f)

    deleted = False
    for f in targets:
        try:
            # The cache outlives the file (5s TTL), and the watcher reacts to
            # this deletion by re-indexing from it — without invalidation the
            # paper was upserted straight back into ChromaDB and paper_search
            # kept returning a paper whose note no longer existed.
            f.unlink()
            deleted = True
        except OSError:
            pass
    if deleted:
        invalidate_papers_cache()
    return deleted


def cleanup_after_deletion(deleted_id: int, deleted_short_name: str = "",
                           papers_dir: Path = None,
                           deleted_file_stem: str = "") -> list:
    """Strip every reference to a just-deleted paper from the papers that remain.

    deleting the note does not delete the references to it: the other papers keep
    ``relations`` entries naming its id, the ``related_papers`` projection, and
    ``## 后续引用`` links naming it. Left alone those become ``unknown_target``
    errors in tools/verify_graph_arrows.py and ghost nodes in the Obsidian graph,
    and an edge the deleted paper used to *own* simply disappears with no record
    anywhere.

    Links may name the paper either by its file stem (the canonical link text)
    or by its short name (what older vaults wrote), so both are matched.

    Graph edges are repaired here rather than by sync_graph_edges(), which is
    deliberately conservative: it only removes a link when a *declaration* points
    the other way, and the declaration is exactly what has just been deleted —
    so it would classify the stale link as "legacy, leave it".

    Returns the ids of the papers that were rewritten.
    """
    if papers_dir is None:
        papers_dir = PAPERS_DIR

    deleted_id = coerce_id(deleted_id)
    gone_links = {s for s in (deleted_short_name, deleted_file_stem) if s}
    touched = []
    for paper in get_all_papers(papers_dir):
        pid = coerce_id(paper.get("id"))
        if pid is None or pid == deleted_id:
            continue
        fresh = _fresh_paper(paper, papers_dir)
        if fresh is None:
            continue
        paper = fresh            # cleanup rewrites survivors whole-file
        pid = coerce_id(paper.get("id"))
        if pid is None or pid == deleted_id:
            continue

        entries = relations_of(paper)
        kept_entries = [e for e in entries if e["target"] != deleted_id]
        # The projection is always derived: a survivor that ends up with no
        # declared relations ends up with an empty list too, rather than keeping
        # ids no declaration covers.
        projection = paper.get("related_papers") or []
        kept_projection = project_related_papers(kept_entries)

        path = _paper_path(paper, papers_dir)
        if path is None:
            continue
        body = paper.get("body", "")
        before, links, after = _split_followup(body)
        kept_links = ([l for l in links if l not in gone_links]
                      if gone_links else links)

        if (kept_entries == entries and kept_projection == projection
                and kept_links == links):
            continue

        metadata = {k: v for k, v in paper.items() if k not in ("body", "file")}
        metadata["relations"] = kept_entries
        metadata["related_papers"] = kept_projection
        _write_paper(path, metadata,
                     _rebuild_followup(before, kept_links, after)
                     if kept_links != links else body)
        touched.append(pid)

    if touched:
        invalidate_papers_cache()
    return touched
