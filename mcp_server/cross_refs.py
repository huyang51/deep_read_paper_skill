import re
from typing import Optional
from mcp_server.markdown_parser import (
    coerce_id, get_paper_by_id, extract_wikilinks, build_relation_graph, get_all_papers,
)
from mcp_server.relations import derive_direction, infer_type, relation_index, relations_of

# Minimum number of shared keywords (case-insensitive) to consider two papers related.
# Two papers sharing 1 keyword often happens by chance (e.g., "deep learning" appears in
# many unrelated papers); requiring 2+ reduces false positives significantly.
MIN_SHARED_KEYWORDS = 2

# Result ordering: a declared relation is a judgement someone made on purpose;
# a legacy related_papers row is a weaker claim; keyword overlap is a hint.
_SOURCE_RANK = {"declared": 0, "legacy": 1, "inferred": 2}


def find_related(paper_id: int, relation_type: Optional[str] = None) -> list[dict]:
    """Find papers related to the given paper ID.

    Three sources feed the result, in descending trust:

    1. ``relations`` frontmatter — type and direction are *declared* facts.
    2. ``related_papers`` without a declaration — legacy rows; type and
       direction are derived (category equality / publication order).
    3. Body wikilinks and shared keywords — inferred candidates.

    ``source`` in each result says which one it came from, so a caller can tell
    a curated relation from a keyword coincidence. The old behaviour (every type
    re-derived from two string equalities) is preserved for sources 2 and 3.
    """
    paper = get_paper_by_id(paper_id)
    if not paper:
        return []

    # coerce_id + skip, not p["id"]: papers/ may hold a .md with no id (a
    # hand-written note, or one whose id was quoted), and a bare KeyError here
    # failed paper_find_related for every paper in the vault until it was fixed.
    all_papers = {}
    for candidate in get_all_papers():
        candidate_id = coerce_id(candidate.get("id"))
        if candidate_id is not None:
            all_papers[candidate_id] = candidate
    index = relation_index(all_papers.values())

    declared = {e["target"]: e for e in relations_of(paper)}
    legacy_ids = {int(r) for r in (paper.get("related_papers") or [])
                  if isinstance(r, int) or (isinstance(r, str) and str(r).isdigit())}

    # ── inferred candidates: wikilinks in the body + shared keywords ──────
    inferred: set[int] = set()
    wikilinks = extract_wikilinks(paper.get("body", ""))
    for link in wikilinks:
        link_lower = link.lower()
        for pid, pdata in all_papers.items():
            short_name = (pdata.get("short_name") or "").lower()
            if short_name == link_lower:
                inferred.add(pid)
                continue
            # Whole-word match in title
            title = pdata.get("title", "")
            if title and re.search(r'\b' + re.escape(link_lower) + r'\b', title.lower()):
                inferred.add(pid)
                continue
            # Whole-word match in keywords
            for kw in pdata.get("keywords", []):
                if re.search(r'\b' + re.escape(link_lower) + r'\b', kw.lower()):
                    inferred.add(pid)
                    break

    paper_kw = set(k.lower() for k in paper.get("keywords", []))
    for pid, pdata in all_papers.items():
        if pid == paper_id:
            continue
        if len(paper_kw & set(k.lower() for k in pdata.get("keywords", []))) >= MIN_SHARED_KEYWORDS:
            inferred.add(pid)

    results = []
    for rid in (set(declared) | legacy_ids | inferred):
        if rid == paper_id:
            continue
        rpaper = index.get(rid)
        if not rpaper:
            continue

        if rid in declared:
            entry = declared[rid]
            source = "declared"
            rel_type = entry["type"] or infer_type(paper, rpaper)
            direction = entry["direction"] or derive_direction(paper.get("year"), rpaper.get("year"))
            note = entry["note"]
        else:
            source = "legacy" if rid in legacy_ids else "inferred"
            rel_type = infer_type(paper, rpaper)
            direction = derive_direction(paper.get("year"), rpaper.get("year"))
            note = ""

        if relation_type and rel_type != relation_type:
            continue

        results.append({
            "paper_id": rid,
            "title": rpaper.get("title", ""),
            "relation_type": rel_type,
            "direction": direction,
            "note": note,
            "source": source,
            "shared_keywords": sorted(
                set(k.lower() for k in paper.get("keywords", [])) &
                set(k.lower() for k in rpaper.get("keywords", []))
            ),
            "method_category": rpaper.get("method_category", ""),
            "problem_domain": rpaper.get("problem_domain", ""),
            "year": rpaper.get("year", ""),
        })

    results.sort(key=lambda r: (_SOURCE_RANK.get(r["source"], 9), r["paper_id"]))
    return results


def _determine_relation_type(paper_a: dict, paper_b: dict) -> str:
    """Deprecated alias kept for callers outside this module (see
    mcp_server.relations.infer_type for why declared types beat this)."""
    return infer_type(paper_a, paper_b)
