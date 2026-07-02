"""Verify that all cross-paper graph arrows follow the old→new direction rule.

The rule: if paper A.year < paper B.year (A is older), then
- A's body may have ## 后续引用 [[B]] (creates old→new arrow ✓)
- A's body must NOT have a body-level [[B]] outside that section + bold A references
- B's body may reference A with **A** (bold) but NOT with [[A]] wikilink
- related_papers frontmatter can be bidirectional (it's metadata only)

Run this after indexing a new paper to catch direction errors early.
Exits with code 0 if all OK, 1 if any violations found.
"""
import sys
import re
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))

from mcp_server.markdown_parser import get_all_papers


def extract_year(paper: dict) -> int | None:
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


def verify_arrows(papers: list[dict]) -> list[str]:
    """Check all cross-paper graph arrows. Return list of violations."""
    issues = []
    paper_by_short = {p.get("short_name", ""): p for p in papers if p.get("short_name")}

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

            if new_year < rel_year:
                # CURRENT paper is OLDER, related is NEWER.
                # Expected: current should have ## 后续引用 [[related]] section
                body = paper.get("body", "")
                if "## 后续引用" not in body or f"[[{rel_short}]]" not in body:
                    issues.append(
                        f"❌ [{cur_short}] (year {new_year}, OLDER) is related to "
                        f"[{rel_short}] (year {rel_year}, NEWER) but lacks "
                        f"## 后续引用 [[{rel_short}]] section. "
                        f"Graph arrow direction is broken."
                    )

                # Also: current paper's body should NOT have a standalone
                # [[related]] wikilink outside the 后续引用 section
                # (we allow it inside the section).
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
                body = paper.get("body", "")
                if f"[[{rel_short}]]" in body:
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

    issues = verify_arrows(papers)
    if not issues:
        print("✅ All graph arrows follow the old→new direction rule!")
        sys.exit(0)

    print(f"❌ Found {len(issues)} issue(s):\n")
    for issue in issues:
        print(f"  {issue}\n")
    print()
    print("Fix manually per SKILL.md §4.5 '时间线校验':")
    print("  - OLD paper should have ## 后续引用 [[NEW]] section")
    print("  - NEW paper should reference OLD with **bold** only")
    sys.exit(1)


if __name__ == "__main__":
    main()
