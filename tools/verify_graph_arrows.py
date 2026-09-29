"""Verify the vault graph: structured relations + arrow direction.

Three checks, in descending authority:

1. **Relations integrity** (``relations`` frontmatter) — the semantic layer.
   Reciprocity (A's "B is my successor" ⇔ B's "A is my predecessor"), type
   agreement, known targets, direction vs. publication order. This is the check
   that matters for a migrated vault; prose conventions below only cover what
   was never declared.
2. **Arrow direction** — the legacy typographic rule: if A.year < B.year then
   A may carry ``## 后续引用 [[B]]`` (old→new arrow) while B must reference A
   with ``**bold**`` only, never a wikilink, or the graph grows wrong-way edges.
   Applied **only to pairs with no declaration** — a declared pair's direction is
   data (a ``peer`` relation has no edge at all by design, and a declared
   direction may legitimately disagree with the years), so check 0 governs it and
   a violation here means a *hand-written* link, not a sync failure.
3. **Relevance** — every ``related_papers`` entry has a body justification.

Run this after indexing a new paper. Exits 0 if clean, 1 if anything failed.
"""
import sys
import re
from pathlib import Path
from typing import Optional

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))

from mcp_server.markdown_parser import get_all_papers
from mcp_server.relations import ERROR, relations_of, validate_relations


def extract_year(paper: dict) -> Optional[int]:
    """Extract year as int from paper frontmatter."""
    y = paper.get("year")
    if y is None:
        return None
    try:
        return int(y)
    except (ValueError, TypeError):
        return None


def get_body_wikilinks(paper: dict) -> list[str]:
    """Extract [[wikilinks]] from paper body."""
    body = paper.get("body", "")
    return re.findall(r"\[\[([^\]]+)\]\]", body)


def get_body_bold_refs(paper: dict, target_short_name: str) -> int:
    """Count **Target** bold references in body."""
    body = paper.get("body", "")
    return len(re.findall(rf"\*\*{re.escape(target_short_name)}\*\*", body))


def verify_relevance(papers: list[dict]) -> list[str]:
    """Check that every related_papers entry has been validated by paper_find_related.

    Specifically: for every (A, B) in related_papers, check that either:
    - A.body or B.body contains a clear relation justification mentioning both papers, OR
    - The shared keywords field documents the relevance

    Since we can't re-call paper_find_related here (it requires running MCP),
    we check for a body-level "## 与前人工作的关系" or "## 后续引用" section
    that mentions the related paper.
    """
    issues = []

    for paper in papers:
        cur_short = paper.get("short_name", "")
        related_ids = paper.get("related_papers", []) or []
        if not related_ids:
            continue

        body = paper.get("body", "")

        for rel_id in related_ids:
            related = next((p for p in papers if p.get("id") == rel_id), None)
            if not related:
                continue
            rel_short = related.get("short_name", "")

            # Check if this paper's body mentions the related paper in a
            # relation context (either ## 与前人工作的关系 or ## 后续引用).
            # We look for the related paper's short_name in bold or wikilink form
            # within these specific sections.
            has_relation_mention = False

            # Check "## 与前人工作的关系" section
            if "## 与前人工作的关系" in body:
                section = body.split("## 与前人工作的关系", 1)[1]
                if "## " in section:  # Until next section
                    section = section.split("## ", 1)[0]
                if rel_short in section:
                    has_relation_mention = True

            # Check "## 后续引用" section
            if not has_relation_mention and "## 后续引用" in body:
                section = body.split("## 后续引用", 1)[1]
                if f"[[{rel_short}]]" in section:
                    has_relation_mention = True

            if not has_relation_mention:
                issues.append(
                    f"❌ [{cur_short}] lists [{rel_short}] as related but has NO "
                    f"justification in body (no mention in '## 与前人工作的关系' "
                    f"or '## 后续引用' section). "
                    f"Use paper_find_related to find candidates, then add a "
                    f"justification in the paper body."
                )

    return issues


def verify_arrows(papers: list[dict]) -> list[str]:
    """Check the legacy typographic arrow rule on pairs that carry no declaration.

    A pair with a ``relations`` declaration is governed by the declaration, not
    by the year heuristic: the sync already placed the edge, and a ``peer``
    relation deliberately has none. Re-applying the heuristic there only
    manufactures false positives (a peer pair with unequal years would be
    reported as a broken arrow), so those pairs are left to check 0.
    """
    issues = []
    declared = set()
    for paper in papers:
        pid = paper.get("id")
        for entry in relations_of(paper):
            declared.add((pid, entry["target"]))

    for paper in papers:
        new_year = extract_year(paper)
        related_ids = paper.get("related_papers", []) or []
        if not related_ids:
            continue

        for rel_id in related_ids:
            # Find related paper by id
            related = next(
                (p for p in papers if p.get("id") == rel_id),
                None,
            )
            if not related:
                continue

            rel_year = extract_year(related)
            rel_short = related.get("short_name", "")
            cur_short = paper.get("short_name", "")

            if new_year is None or rel_year is None:
                continue  # Skip if we can't determine order

            pair_declared = ((paper.get("id"), rel_id) in declared
                             or (rel_id, paper.get("id")) in declared)
            body = paper.get("body", "")

            if new_year < rel_year:
                # CURRENT paper is OLDER, related is NEWER.
                # Expected: current should have ## 后续引用 [[related]] section
                if not pair_declared and (
                        "## 后续引用" not in body or f"[[{rel_short}]]" not in body):
                    issues.append(
                        f"❌ [{cur_short}] (year {new_year}, OLDER) is related to "
                        f"[{rel_short}] (year {rel_year}, NEWER) but lacks "
                        f"## 后续引用 [[{rel_short}]] section. "
                        f"Graph arrow direction is broken."
                    )

                # Also: current paper's body should NOT have a standalone
                # [[related]] wikilink outside the 后续引用 section
                # (we allow it inside the section). Duplicate edges are wrong
                # however the direction was decided, so this one is not skipped
                # for declared pairs.
                post_section = body.split("## 后续引用", 1)[0] if "## 后续引用" in body else body
                if f"[[{rel_short}]]" in post_section:
                    issues.append(
                        f"⚠️  [{cur_short}] has [[{rel_short}]] wikilink OUTSIDE the "
                        f"## 后续引用 section — this creates duplicate graph edges. "
                        f"Replace with **bold** or move into the section."
                    )

            elif new_year > rel_year:
                # CURRENT paper is NEWER, related is OLDER.
                # Expected: current should reference related with **bold** only,
                # NOT with [[related]] wikilink.
                if not pair_declared and f"[[{rel_short}]]" in body:
                    # Check if this is inside 后续引用 (would be wrong since this paper is newer)
                    if "## 后续引用" in body:
                        # Check if [[related]] is before or after 后续引用
                        wikilink_pos = body.find(f"[[{rel_short}]]")
                        section_pos = body.find("## 后续引用")
                        if wikilink_pos < section_pos:
                            issues.append(
                                f"❌ [{cur_short}] (year {new_year}, NEWER) references "
                                f"[{rel_short}] (year {rel_year}, OLDER) with a "
                                f"[[wikilink]] before the 后续引用 section. "
                                f"This creates a wrong-direction graph edge (new→old). "
                                f"Replace with **bold** text."
                            )
                    else:
                        # No 后续引用 section, but has [[older]] wikilink - wrong
                        issues.append(
                            f"❌ [{cur_short}] (year {new_year}, NEWER) references "
                            f"[{rel_short}] (year {rel_year}, OLDER) with a "
                            f"[[wikilink]]. This creates a wrong-direction graph edge. "
                            f"Replace with **bold** text only."
                        )

                # Also: bold count check
                bold_count = get_body_bold_refs(paper, rel_short)
                if bold_count == 0:
                    issues.append(
                        f"⚠️  [{cur_short}] (year {new_year}, NEWER) is related to "
                        f"[{rel_short}] (year {rel_year}, OLDER) but has no "
                        f"**{rel_short}** bold reference in body. "
                        f"Recommend adding bold reference for context."
                    )

    return issues


def main():
    papers = get_all_papers()
    if not papers:
        print("No papers found in vault.")
        sys.exit(0)

    print(f"Checking {len(papers)} papers for graph arrow consistency...")
    print()

    # Check 0: structured relations (the semantic layer). Relation errors are
    # reported but the arrow/relevance checks still run — a vault mid-migration
    # should show the whole picture in one pass, not one category at a time.
    relation_issues = validate_relations(papers)
    relation_errors = [i for i in relation_issues if i["severity"] == ERROR]
    if relation_issues:
        print(f"{'❌' if relation_errors else '⚠️ '} Relation issues "
              f"({len(relation_errors)} errors / "
              f"{len(relation_issues) - len(relation_errors)} warnings):\n")
        for issue in relation_issues:
            mark = "❌" if issue["severity"] == ERROR else "⚠️ "
            print(f"  {mark} [{issue['code']}] {issue['message']}")
        print()
    else:
        print("✅ Structured relations: reciprocity, types and targets all consistent.")
        print()

    # Check 1: relevance (every related_papers has body justification)
    relevance_issues = verify_relevance(papers)
    if relevance_issues:
        print(f"❌ Relevance issues ({len(relevance_issues)}):\n")
        for issue in relevance_issues:
            print(f"  {issue}\n")

    # Check 2: arrow direction (old → new)
    arrow_issues = verify_arrows(papers)
    if arrow_issues:
        print(f"❌ Arrow direction issues ({len(arrow_issues)}):\n")
        for issue in arrow_issues:
            print(f"  {issue}\n")

    if not relevance_issues and not arrow_issues and not relation_errors:
        print("✅ All graph arrows follow the old→new direction rule,")
        print("   every related_papers entry has a body justification,")
        print("   and the structured relations are consistent!")
        sys.exit(0)

    print()
    print("How to fix (SKILL.md §4.5):")
    print("  1. paper_find_related gives candidates — DON'T guess relations")
    print("  2. Declare them in frontmatter `relations` "
          "(target / type / direction / note);")
    print("     the reciprocal entry and the ## 后续引用 edge are then written for you")
    print("  3. Legitimacy still lives in the body: '## 与前人工作的关系' + bold "
          "mentions of papers you built on")
    print("  4. Legacy vault without relations → python tools/migrate_relations.py --apply")
    sys.exit(1)


if __name__ == "__main__":
    main()
