"""Migrate a legacy vault to structured cross-paper relations.

The pre-relations vault kept relations in two half-languages: a flat
``related_papers: [id]`` list (no type, no direction) and body prose where the
direction was encoded typographically (``**bold**`` = newer→older,
``[[link]]`` under ``## 后续引用`` = older→newer). This tool reads both and
writes the single structured form:

    relations:
      - target: 3
        type: method_similar      # declared; legacy rows get the inferred value
        direction: predecessor    # from publication order
        note: ""

Dry-run by default — a migration that rewrites every paper's frontmatter must
show its work first. ``--apply`` then writes the relations, mirrors the
reciprocal entries onto the other papers, regenerates the ``related_papers``
projection, and places the ``## 后续引用`` edges by declared direction.

Usage:
  python tools/migrate_relations.py                 # plan only (safe)
  python tools/migrate_relations.py --apply         # write frontmatter + edges
  python tools/migrate_relations.py --check         # integrity check only

Exit codes: 0 nothing to do / all clean | 2 plan differs from vault or warnings
            remain | 1 errors found (unresolved side of a relation).
"""
import argparse
import json
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows (GBK console mangles CJK + arrows)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))

from mcp_server.markdown_parser import (  # noqa: E402
    FOLLOWUP_HEADER, _split_followup, get_all_papers, sync_paper_relations,
    update_paper_relations,
)
from mcp_server.relations import (  # noqa: E402
    RELATION_TYPE_CN, derive_direction, derive_relations, describe, infer_type,
    relation_index, relation_targets, relations_of, validate_relations,
    ERROR, WARN,
)


def plan_for(paper: dict, index: dict) -> tuple[list[dict], list[int], list[int]]:
    """Propose ``relations`` for one paper. Returns (entries, orphans, recovered).

    Two sources are merged:

    * ``related_papers`` — legacy rows; type by category equality, direction by
      publication order. This is inference, and the plan marks it as such.
    * ``## 后续引用`` links — an edge whose direction is *already* stated by the
      convention (the link means "the target came after me"), so these are
      recovered as ``direction: successor`` rather than guessed. A pair found in
      both sources keeps the followup direction.
    """
    entries, orphans = derive_relations(paper, index)
    by_target = {e["target"]: e for e in entries}
    recovered = []

    followup_links = _split_followup(paper.get("body", ""))[1]
    shorts = {p.get("short_name", ""): pid for pid, p in index.items() if p.get("short_name")}
    for link in followup_links:
        target = shorts.get(link)
        if target is None or target == paper.get("id"):
            # A link to a paper that is not in the vault: an Obsidian ghost node.
            continue
        if target in by_target:
            # The convention states the direction outright — trust it over the
            # year comparison when the two disagree (preprints, merged records).
            by_target[target]["direction"] = "successor"
            continue
        by_target[target] = {
            "target": target,
            "type": infer_type(paper, index[target]),
            "direction": "successor",
            "note": "",
        }
        recovered.append(target)

    entries = sorted(by_target.values(), key=lambda e: e["target"])
    return entries, orphans, recovered


def build_plan(papers: list[dict]) -> dict:
    index = relation_index(papers)
    plans = {}
    for paper in papers:
        pid = paper.get("id")
        existing = relations_of(paper)
        entries, orphans, recovered = plan_for(paper, index)
        plans[pid] = {
            "short_name": paper.get("short_name", ""),
            "existing": existing,
            "proposed": entries,
            "orphans": orphans,          # listed but not in the vault (ghost nodes)
            "recovered": recovered,      # found only as a ## 后续引用 edge
            "needs_write": bool(entries) and entries != existing,
        }
    return plans


def print_plan(plans: dict, index: dict) -> None:
    pending = {pid: p for pid, p in plans.items() if p["needs_write"]}
    if not pending:
        print("✅ 无需迁移：所有论文的 relations 已是最新（或本就没有关联）。")
    for pid, p in sorted(pending.items(), key=lambda kv: str(kv[1]["short_name"])):
        print(f"\n[{pid}] {p['short_name'] or '?'}  —— {len(p['proposed'])} 条关系")
        for e in p["proposed"]:
            other = index.get(e["target"], {})
            tag = "（按年份推断）"
            if e["target"] in p["recovered"]:
                tag = "（由 ## 后续引用 恢复方向）"
            print(f"    → [{e['target']}] {other.get('short_name', '?'):<16} "
                  f"{RELATION_TYPE_CN.get(e['type'], e['type'])}"
                  f"·{describe(e).split('·', 1)[-1]}{tag}")
        if p["orphans"]:
            print(f"    ⚠️  related_papers 里的 {p['orphans']} 不在库中（幽灵节点）——"
                  f"索引对方论文后重跑，或从 related_papers 移除")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate legacy related_papers / prose edges to structured relations.")
    parser.add_argument("--apply", action="store_true",
                        help="写入 frontmatter、互指条目与图谱边（默认只出计划，不写盘）")
    parser.add_argument("--check", action="store_true",
                        help="只跑关系完整性校验（等同 verify_graph_arrows 的关系面）")
    parser.add_argument("--out", default="", help="把计划/校验结果写成 JSON")
    args = parser.parse_args(argv)

    papers = get_all_papers()
    if not papers:
        print("vault 中没有论文。")
        return 0
    index = relation_index(papers)

    if args.check:
        issues = validate_relations(papers)
        errors = [i for i in issues if i["severity"] == ERROR]
        for i in issues:
            print(("❌ " if i["severity"] == ERROR else "⚠️  ") + i["message"])
        print(f"\n关系校验：{len(papers)} 篇论文，{len(errors)} 个错误，"
              f"{len(issues) - len(errors)} 个提示。")
        if args.out:
            Path(args.out).write_text(json.dumps(issues, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
        return 1 if errors else 0

    plans = build_plan(papers)
    print(f"扫描 {len(papers)} 篇论文"
          f"（已有 relations：{sum(1 for p in papers if relations_of(p))} 篇）。")
    print_plan(plans, index)

    written = 0
    if args.apply:
        for pid, plan in plans.items():
            if not plan["needs_write"]:
                continue
            if update_paper_relations(pid, plan["proposed"]):
                written += 1
        # Second pass: reciprocals + graph edges need every paper's relations in
        # place first, otherwise half the pairs would look one-sided.
        for paper in get_all_papers():
            if relations_of(paper):
                sync_paper_relations(paper)
        print(f"\n✅ 已写入 {written} 篇论文的 relations，并同步互指条目与图谱边。")

    issues = validate_relations(get_all_papers() if args.apply else papers)
    errors = [i for i in issues if i["severity"] == ERROR]
    warns = [i for i in issues if i["severity"] == WARN]

    print()
    print(validate_summary(issues, errors, warns))

    if args.out:
        Path(args.out).write_text(json.dumps(
            {"plans": plans, "issues": issues}, ensure_ascii=False, indent=2,
            default=str), encoding="utf-8")

    if errors:
        return 1
    if warns or (not args.apply and any(p["needs_write"] for p in plans.values())):
        return 2
    return 0


def validate_summary(issues: list, errors: list, warns: list) -> str:
    if not issues:
        return "✅ 关系完整性校验通过：互指对称、类型一致、依据齐备。"
    lines = [f"关系完整性：{len(errors)} 个错误、{len(warns)} 个提示。"]
    for i in errors + warns:
        lines.append(("  ❌ " if i["severity"] == ERROR else "  ⚠️  ") + i["message"])
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
