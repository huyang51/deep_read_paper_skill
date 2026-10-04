import re
from typing import Optional
from mcp_server.markdown_parser import get_paper_by_id, extract_wikilinks, get_all_papers
from mcp_server.relations import (
    derive_direction, infer_type, relation_index, relations_of,
)

# Minimum number of shared keywords (case-insensitive) to consider two papers related.
# Two papers sharing 1 keyword often happens by chance (e.g., "deep learning" appears in
# many unrelated papers); requiring 2+ reduces false positives significantly.
MIN_SHARED_KEYWORDS = 2

# Result ordering: a declared relation is a judgement someone made on purpose;
# keyword/wikilink overlap is only a hint. `related_papers` is not a source at
# all — it is the projection of the declarations, regenerated on every write.
_SOURCE_RANK = {"declared": 0, "inferred": 1}


def find_related(paper_id: int, relation_type: Optional[str] = None) -> list[dict]:
    """Find papers related to the given paper ID.

    Two sources feed the result, in descending trust:

    1. ``relations`` frontmatter — type and direction are *declared* facts.
    2. Body wikilinks and shared keywords — inferred candidates.

    ``source`` in each result says which one it came from, so a caller can tell
    a curated relation from a keyword coincidence. ``related_papers`` is not a
    source: it is the projection the writer keeps in step with ``relations``, so
    reading it would add nothing that ``relations`` does not already say.
    """
    paper = get_paper_by_id(paper_id)
    if not paper:
        return []

    # One index for everything below: relation_index skips what coerce_id
    # rejects (a note with no id, a quoted one) — papers/ may hold either, and a
    # bare KeyError here used to fail paper_find_related for every paper in the
    # vault until it was fixed. It replaces two chained builds of the same map:
    # a local coerce_id loop whose values were then re-indexed with int().
    index = relation_index(get_all_papers())

    declared = {e["target"]: e for e in relations_of(paper)}

    # ── inferred candidates: wikilinks in the body + shared keywords ──────
    inferred: set[int] = set()
    wikilinks = extract_wikilinks(paper.get("body", ""))
    for link in wikilinks:
        link_lower = link.lower()
        for pid, pdata in index.items():
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
    for pid, pdata in index.items():
        if pid == paper_id:
            continue
        if len(paper_kw & set(k.lower() for k in pdata.get("keywords", []))) >= MIN_SHARED_KEYWORDS:
            inferred.add(pid)

    results = []
    for rid in (set(declared) | inferred):
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
            source = "inferred"
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
