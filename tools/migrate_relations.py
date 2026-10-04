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

The inferred ``type`` is a guess (category equality); ``--set-type
PAPER_ID=TYPE`` pins it when the prose says otherwise (e.g. the bodies call a
pair complementary while both share a method category). It pins BOTH sides of
that paper's relations in the plan itself, so the corrected class reaches the
other paper through the normal reciprocal sync instead of being hand-edited
into disagreement — and survives the next run (a declaration always beats a
re-derivation, since ``related_papers`` is a projection that carries ids only).

Exit codes: 0 nothing to do / all clean | 2 plan differs from vault or warnings
            remain | 1 errors found (unresolved side of a relation) or bad args.
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
    RELATION_TYPE_CN, RELATION_TYPES, coerce_id, derive_direction,
    derive_relations, describe, infer_type, relation_index, relation_targets,
    relations_of, validate_relations, ERROR, WARN,
)


def parse_type_overrides(pairs: list) -> dict:
    """Parse ``["1=complementary"]`` into ``{1: "complementary"}``.

    Raises ValueError on anything malformed — a mistyped override that silently
    did nothing would write the inferred type into the vault, which is exactly
    the guess the caller was trying to correct.
    """
    overrides = {}
    for raw in pairs or []:
        target, sep, rtype = str(raw).partition("=")
        rtype = rtype.strip()
        if not sep or not target.strip().isdigit() or rtype not in RELATION_TYPES:
            raise ValueError(
                f"--set-type 需要 TARGET=TYPE 形式（TYPE ∈ "
                f"{'/'.join(RELATION_TYPES)}），收到 {raw!r}")
        overrides[int(target)] = rtype
    return overrides


def plan_for(paper: dict, index: dict) -> dict:
    """Propose ``relations`` for one paper.

    Returns ``{"entries", "orphans", "recovered", "linked", "kept"}``:

    * ``orphans`` — ids in ``related_papers`` that are not in the vault;
    * ``recovered`` — rows that exist *only* as a ``## 后续引用`` link;
    * ``linked`` — targets whose **direction** came from a link, not from years;
    * ``kept`` — targets carried over from an existing declaration untouched.

    Three sources, strongest first:

    1. an existing declaration. ``related_papers`` is a *projection* — ids and
       nothing else — so re-deriving a type for a pair that already declares one
       replaces the authored class on every run (a hand-set ``complementary``
       lasted exactly until the next migration);
    2. a ``## 后续引用`` link — the convention already states the direction
       ("the target came after me"), so it is recovered, not guessed;
    3. the year comparison — a guess, used only where neither says anything.
    """
    inferred, orphans = derive_relations(paper, index)
    declared = {e["target"]: e for e in relations_of(paper)}

    by_target = {t: dict(e) for t, e in declared.items()}
    kept = set(by_target)
    for e in inferred:
        by_target.setdefault(e["target"], e)

    recovered, linked = [], set()
    followup_links = _split_followup(paper.get("body", ""))[1]
    shorts = {p.get("short_name", ""): pid for pid, p in index.items() if p.get("short_name")}
    for link in followup_links:
        target = shorts.get(link)
        # coerce_id on BOTH sides of the comparison: a hand-edited `id: "9"`
        # made paper.get("id") the string "9" while targets are ints, so the
        # self-link check silently failed and the migration "recovered" a
        # successor relation of a paper to ITSELF (live repro 2026-10-04).
        if target is None or target == coerce_id(paper.get("id")):
            # A link to a paper that is not in the vault: an Obsidian ghost node.
            continue
        if target in kept:
            # A declaration beats the link, including a link left over from
            # before the pair was declared `peer` — otherwise re-running the
            # migration would invent an arrow the declaration denies.
            continue
        linked.add(target)
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
    return {"entries": entries, "orphans": orphans, "recovered": recovered,
            "linked": linked, "kept": kept}


def find_override_conflict(plans: dict, overrides: dict) -> tuple:
    """Return ``(owner, target, a, b)`` when one pair is pinned two ways.

    ``--set-type 1=complementary --set-type 2=evolutionary`` on a pair means
    the two sides would be written with different classes — the exact
    disagreement the migration exists to remove — so it is refused rather than
    resolved by argument order.
    """
    for pid, p in plans.items():
        for e in p["proposed"]:
            a, b = overrides.get(e["target"]), overrides.get(pid)
            if a and b and a != b:
                return (pid, e["target"], a, b)
    return ()


def build_plan(papers: list[dict], overrides: dict = None) -> dict:
    overrides = overrides or {}
    index = relation_index(papers)
    plans = {}
    for paper in papers:
        # overrides (from --set-type) and entry targets are ints; a raw
        # frontmatter id can be the string "5" — uncoerced, overrides.get(pid)
        # missed its own row (one-sided pin → the reciprocal reverted the
        # class, and the "unused override" warning never fired either).
        pid = coerce_id(paper.get("id"))
        if pid is None:
            continue
        existing = relations_of(paper)
        plan = plan_for(paper, index)
        entries = plan["entries"]
        forced = []
        for e in entries:
            # An override names ONE paper and pins every relation it is part of
            # — both the rows pointing at it and the rows it declares itself.
            # Pinning only one side is not enough: the reciprocal sync writes
            # whichever plan it reaches last, so a one-sided pin gets reverted
            # by the other side's stale entry.
            pinned = overrides.get(e["target"]) or overrides.get(pid)
            if pinned and e["type"] != pinned:
                e["type"] = pinned
                forced.append(e["target"])
        plans[pid] = {
            "short_name": paper.get("short_name", ""),
            "existing": existing,
            "proposed": entries,
            "orphans": plan["orphans"],      # listed but not in the vault (ghost nodes)
            "recovered": plan["recovered"],  # found only as a ## 后续引用 edge
            "linked": sorted(plan["linked"]),  # direction stated by a link, not guessed
            "kept": sorted(plan["kept"]),      # type/direction already declared
            "forced": forced,                  # type pinned by --set-type, not inferred
            "needs_write": bool(entries) and entries != existing,
        }
    return plans


def print_plan(plans: dict, index: dict) -> None:
    pending = {pid: p for pid, p in plans.items() if p["needs_write"]}
    if not pending:
        print("✅ 无需迁移：所有论文的 relations 已是最新（或本就没有关联）。")
    # Row-level provenance, so the footer can state plainly how much of the
    # proposal is authored and how much is this tool's guess.
    tallies = {"declared": 0, "linked": 0, "guessed": 0, "set": 0}
    for pid, p in sorted(pending.items(), key=lambda kv: str(kv[1]["short_name"])):
        print(f"\n[{pid}] {p['short_name'] or '?'}  —— {len(p['proposed'])} 条关系")
        for e in p["proposed"]:
            other = index.get(e["target"], {})
            if e["target"] in p["kept"]:
                tag = "（沿用已声明值）"
                tallies["declared"] += 1
            elif e["target"] in p["linked"]:
                tag = "（方向由 ## 后续引用 恢复）"
                tallies["linked"] += 1
            else:
                tag = "（方向按年份推断）"
                tallies["guessed"] += 1
            if e["target"] in p["forced"]:
                tag += "（type 由 --set-type 指定）"
                tallies["set"] += 1
            print(f"    → [{e['target']}] {other.get('short_name', '?'):<16} "
                  f"{RELATION_TYPE_CN.get(e['type'], e['type'])}"
                  f"·{describe(e).split('·', 1)[-1]}{tag}")
        if p["orphans"]:
            print(f"    ⚠️  related_papers 里的 {p['orphans']} 不在库中（幽灵节点）——"
                  f"索引对方论文后重跑，或从 related_papers 移除")
    if pending:
        # A migration that writes inferred semantics into a hand-curated vault
        # deserves a read-back. Saying which parts are inference is the whole
        # point of a dry run; only `note` stays empty either way.
        t = tallies
        typed = t["declared"] - t["set"]
        bits = []
        if typed > 0:
            bits.append(f"{typed} 行沿用已声明值")
        if t["set"]:
            bits.append(f"{t['set']} 行由 --set-type 指定")
        inferred = len([1 for p in pending.values() for _ in p["proposed"]]) - typed - t["set"]
        if not bits:
            type_note = "均为推断（method_category + problem_domain 相等即判方法相似）"
        else:
            type_note = "、".join(bits) + ("，其余为推断" if inferred else "，无推断成分")
        print(f"\nℹ️  以上 type {type_note}；方向：{t['declared']} 行为已声明值、"
              f"{t['linked']} 行由 ## 后续引用 恢复、{t['guessed']} 行按年份推断。")
        if inferred:
            print("   apply 前请逐行核对：方向不对改 direction，实为互补/发展就改 type"
                  "（四类口径见 SKILL §4.5.2），需要时补一句 note。")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate legacy related_papers / prose edges to structured relations.")
    parser.add_argument("--apply", action="store_true",
                        help="写入 frontmatter、互指条目与图谱边（默认只出计划，不写盘）")
    parser.add_argument("--check", action="store_true",
                        help="只跑关系完整性校验（等同 verify_graph_arrows 的关系面）")
    parser.add_argument("--set-type", action="append", default=[], metavar="PAPER_ID=TYPE",
                        help="把论文 PAPER_ID 参与的关系类别固定为 TYPE（可重复）："
                             "--set-type 1=complementary 会同时钉住指向它的关系与它自己"
                             "声明的关系，两侧不会各写一个类别")
    parser.add_argument("--out", default="", help="把计划/校验结果写成 JSON")
    args = parser.parse_args(argv)

    try:
        overrides = parse_type_overrides(args.set_type)
    except ValueError as e:
        print(f"❌ {e}")
        return 1

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
        # Docstring contract: 2 = warnings remain (1 was reserved for errors).
        return 1 if errors else (2 if issues else 0)

    plans = build_plan(papers, overrides)
    conflict = find_override_conflict(plans, overrides)
    if conflict:
        pid, target, a, b = conflict
        print(f"❌ --set-type 给同一对关系指定了两个类别：[{pid}] 与 [{target}] 之间"
              f"既是 {a} 又是 {b}。请只保留一个。")
        return 1

    print(f"扫描 {len(papers)} 篇论文"
          f"（已有 relations：{sum(1 for p in papers if relations_of(p))} 篇）。")
    print_plan(plans, index)

    seen = {e["target"] for p in plans.values() for e in p["proposed"]}
    seen |= {pid for pid, p in plans.items() if p["proposed"]}
    unused = sorted(set(overrides) - seen)
    if unused:
        # Silently ignoring this would leave the vault with the inferred type
        # while the operator believes they corrected it.
        print(f"⚠️  --set-type 指定的 {unused} 在任何计划里都没有出现（目标 id 写错？）")

    written = 0
    if args.apply:
        for pid, plan in plans.items():
            if not plan["needs_write"]:
                continue
            if update_paper_relations(pid, plan["proposed"]):
                written += 1
        # Second pass: reciprocals + graph edges need every paper's relations in
        # place first, otherwise half the pairs would look one-sided.
        synced = 0
        for paper in get_all_papers():
            if not relations_of(paper):
                continue
            r = sync_paper_relations(paper)
            if r.get("mirrored") or r.get("edges_updated") or r.get("edges_moved"):
                synced += 1
        # Only claim work that happened: a re-run of --apply reports 0/0 rather
        # than announcing a sync that touched nothing.
        if written or synced:
            print(f"\n✅ 已写入 {written} 篇论文的 relations，"
                  f"同步 {synced} 篇的互指条目与图谱边。")
        else:
            print("\n✅ --apply 无待写内容（vault 已是最新）。")

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
