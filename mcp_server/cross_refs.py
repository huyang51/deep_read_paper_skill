from typing import Optional
from mcp_server.markdown_parser import get_paper_by_id, extract_wikilinks, build_relation_graph, get_all_papers


def find_related(paper_id: int, relation_type: Optional[str] = None) -> list[dict]:
    """Find papers related to the given paper ID."""
    paper = get_paper_by_id(paper_id)
    if not paper:
        return []

    all_papers = {p["id"]: p for p in get_all_papers()}
    graph = build_relation_graph()
    related_ids = set()

    # 1. From frontmatter related_papers
    for rp in paper.get("related_papers", []):
        if isinstance(rp, int):
            related_ids.add(rp)

    # 2. From wikilinks in body - match against paper titles/shortnames
    wikilinks = extract_wikilinks(paper.get("body", ""))
    for link in wikilinks:
        for pid, pdata in all_papers.items():
            title = pdata.get("title", "")
            keywords = " ".join(pdata.get("keywords", []))
            if link.lower() in title.lower() or link.lower() in keywords.lower():
                related_ids.add(pid)

    # 3. From shared keywords
    paper_kw = set(k.lower() for k in paper.get("keywords", []))
    for pid, pdata in all_papers.items():
        if pid == paper_id:
            continue
        other_kw = set(k.lower() for k in pdata.get("keywords", []))
        common = paper_kw & other_kw
        if len(common) >= 2:  # at least 2 shared keywords
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
