import re
from typing import Optional
from mcp_server.markdown_parser import get_paper_by_id, extract_wikilinks, build_relation_graph, get_all_papers

# Minimum number of shared keywords (case-insensitive) to consider two papers related.
# Two papers sharing 1 keyword often happens by chance (e.g., "deep learning" appears in
# many unrelated papers); requiring 2+ reduces false positives significantly.
MIN_SHARED_KEYWORDS = 2


def find_related(paper_id: int, relation_type: Optional[str] = None) -> list[dict]:
    """Find papers related to the given paper ID."""
    paper = get_paper_by_id(paper_id)
    if not paper:
        return []

    all_papers = {p["id"]: p for p in get_all_papers()}
    related_ids = set()

    # 1. From frontmatter related_papers
    for rp in paper.get("related_papers", []):
        if isinstance(rp, int):
            related_ids.add(rp)

    # 2. From wikilinks in body — match against short_name, title (whole-word), or keywords.
    #    Use whole-word matching (\\b) to avoid e.g. "BERT" matching "RoBERTa" / "ALBERT".
    wikilinks = extract_wikilinks(paper.get("body", ""))
    for link in wikilinks:
        link_lower = link.lower()
        for pid, pdata in all_papers.items():
            short_name = (pdata.get("short_name") or "").lower()
            if short_name == link_lower:
                related_ids.add(pid)
                continue
            # Whole-word match in title
            title = pdata.get("title", "")
            if title and re.search(r'\b' + re.escape(link_lower) + r'\b', title.lower()):
                related_ids.add(pid)
                continue
            # Whole-word match in keywords
            for kw in pdata.get("keywords", []):
                if re.search(r'\b' + re.escape(link_lower) + r'\b', kw.lower()):
                    related_ids.add(pid)
                    break

    # 3. From shared keywords
    paper_kw = set(k.lower() for k in paper.get("keywords", []))
    for pid, pdata in all_papers.items():
        if pid == paper_id:
            continue
        other_kw = set(k.lower() for k in pdata.get("keywords", []))
        common = paper_kw & other_kw
        if len(common) >= MIN_SHARED_KEYWORDS:
            related_ids.add(pid)

    results = []
    for rid in related_ids:
        if rid == paper_id:
            continue
        rpaper = all_papers.get(rid)
        if not rpaper:
            continue

        rel_type = _determine_relation_type(paper, rpaper)
        if relation_type and rel_type != relation_type:
            continue

        results.append({
            "paper_id": rid,
            "title": rpaper.get("title", ""),
            "relation_type": rel_type,
            "shared_keywords": list(
                set(k.lower() for k in paper.get("keywords", [])) &
                set(k.lower() for k in rpaper.get("keywords", []))
            ),
            "method_category": rpaper.get("method_category", ""),
            "problem_domain": rpaper.get("problem_domain", ""),
            "year": rpaper.get("year", ""),
        })

    return results


def _determine_relation_type(paper_a: dict, paper_b: dict) -> str:
    """Determine the relation type between two papers."""
    same_domain = paper_a.get("problem_domain") == paper_b.get("problem_domain")
    same_method_cat = paper_a.get("method_category") == paper_b.get("method_category")

    if same_method_cat and same_domain:
        return "method_similar"
    elif same_method_cat:
        return "complementary"
    elif same_domain:
        return "problem_related"
    else:
        return "evolutionary"
