"""Structured cross-paper relations — the semantic layer behind the graph.

Before this module, a vault recorded *that* two papers were related
(``related_papers: [3]``) but never *how*: the relation type was re-derived at
query time from two string equalities (``method_category`` / ``problem_domain``)
and the direction lived in a typographic convention (a ``**bold**`` mention meant
"new → old", a ``[[wikilink]]`` under ``## 后续引用`` meant "old → new"). That
made the graph unqueryable and forced a whole tool plus a manual-fix section to
police arrow direction.

Here the same facts become data:

    relations:
      - target: 3
        type: method_similar          # 语义类别（词表见 RELATION_TYPES）
        direction: predecessor        # 相对本文：前作 / 后继 / 同期并列
        note: "本文沿用其 max-sim 打分形式"

``related_papers`` stays as the machine-readable projection of ``relations``
(kept in sync by the writer), so existing queries and Dataview snippets that
read the old field keep working.

Pure functions only — no file I/O, so every rule below is unit-testable.
"""
from typing import Iterable, Optional

RELATION_TYPES = ("method_similar", "problem_related", "complementary", "evolutionary")


def coerce_id(value) -> Optional[int]:
    """Frontmatter ``id`` as an int, or None when it is not a usable id.

    YAML decides the type from how it was written, and these files are routinely
    hand-edited: ``id: 7`` is an int, ``id: "7"`` is a str. Comparing the two
    misses, and mixing them in ``max()`` raises TypeError — so everything that
    reads an id goes through here first. ``True`` is rejected explicitly because
    ``isinstance(True, int)`` is true in Python and ``id: yes`` would otherwise
    silently become paper 1.

    Lives here rather than in markdown_parser so the validators can use it
    without an import cycle (markdown_parser already imports this module).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
    return None


RELATION_TYPES = ("method_similar", "problem_related", "complementary", "evolutionary")

# Display vocabulary shared with references/report_template.md (§ 跨论文对比)
RELATION_TYPE_CN = {
    "method_similar": "🔗 方法相似",
    "problem_related": "🎯 问题相通",
    "complementary": "🧩 互补",
    "evolutionary": "📈 发展关系",
}

# Direction is expressed RELATIVE TO THE OWNING PAPER, deliberately: an
# absolute "earlier → later" flag would read differently depending on which
# file you open, and that ambiguity is exactly what produced wrong arrows.
DIRECTIONS = ("predecessor", "successor", "peer")

DIRECTION_CN = {
    "predecessor": "它是本文的前作（本文建立于它）",
    "successor": "它是本文的后继（它建立于本文）",
    "peer": "同期/并列（无影响方向）",
}

INVERSE_DIRECTION = {"predecessor": "successor", "successor": "predecessor", "peer": "peer"}

NOTE_MAX = 200

# Severity levels used by validate_relations(). "error" breaks a tool or the
# graph; "warn" is a quality/hygiene signal that must not block indexing.
ERROR = "error"
WARN = "warn"


def inverse_direction(direction: str) -> str:
    """The direction the *other* paper must record for the same pair."""
    return INVERSE_DIRECTION.get(direction, "")


def derive_direction(self_year, other_year) -> str:
    """Direction implied by publication order — used for migration and for the
    legacy path where no direction was ever declared. Equal or unknown years
    yield "peer" (parallel work is the honest reading of same-year papers)."""
    try:
        a, b = int(self_year), int(other_year)
    except (TypeError, ValueError):
        return "peer"
    if b < a:
        return "predecessor"   # the other paper came earlier -> it is our predecessor
    if b > a:
        return "successor"     # the other paper came later -> it is our successor
    return "peer"


def _clean_note(note) -> str:
    if not isinstance(note, str):
        return ""
    return " ".join(note.split())[:NOTE_MAX]


def normalize_relations(raw) -> tuple[list[dict], list[str]]:
    """Coerce a frontmatter ``relations`` value into a clean list of entries.

    Returns ``(entries, warnings)``. Forgiving on purpose: a malformed entry is
    dropped (or its bad field kept verbatim so validate_relations can report it)
    rather than raising, because a vault must never become unreadable over one
    typo. Duplicate targets keep the first entry — a pair carries one relation.
    """
    entries: list[dict] = []
    warnings: list[str] = []
    if raw is None:
        return entries, warnings
    if isinstance(raw, dict):          # tolerate a single relation written bare
        raw = [raw]
    if not isinstance(raw, list):
        return entries, [f"relations 字段不是列表（{type(raw).__name__}），已忽略"]

    seen: set[int] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            warnings.append(f"relations[{i}] 不是映射，已忽略")
            continue
        # coerce_id, not bare int(): int(True) == 1 silently pointed the
        # relation at a DIFFERENT paper, and int(3.7) == 3 silently truncated
        # the target — both with no warning at all (live repro 2026-10-04).
        # Same口径 as frontmatter `id: yes` rejection above.
        target = coerce_id(item.get("target"))
        if target is None:
            warnings.append(f"relations[{i}] 缺少合法 target（论文 ID），已忽略")
            continue
        if target in seen:
            warnings.append(f"relations[{i}] target={target} 重复，已保留首条")
            continue
        seen.add(target)

        rtype = item.get("type")
        rtype = rtype.strip() if isinstance(rtype, str) else ""
        direction = item.get("direction")
        direction = direction.strip() if isinstance(direction, str) else ""
        note = item.get("note")
        if note is not None and not isinstance(note, str):
            # _clean_note would blank it without a word; the caller must see
            # its note did not survive.
            warnings.append(f"relations[{i}] 的 note 不是字符串（{type(note).__name__}），已置空")
        entries.append({"target": target, "type": rtype,
                        "direction": direction, "note": _clean_note(note),
                        **({"synced": True} if item.get("synced") else {})})
    return entries, warnings


def relations_of(paper: dict) -> list[dict]:
    """Normalized ``relations`` of one parsed paper (empty list for legacy files)."""
    if not paper:
        return []
    return normalize_relations(paper.get("relations"))[0]


def relation_targets(paper: dict) -> list[int]:
    """Sorted target IDs — the projection that ``related_papers`` must equal."""
    return sorted({e["target"] for e in relations_of(paper)})


def relation_index(papers: Iterable[dict]) -> dict[int, dict]:
    """``{paper_id: paper}`` with non-integer ids skipped (vault hygiene)."""
    index = {}
    for p in papers:
        try:
            index[int(p.get("id"))] = p
        except (TypeError, ValueError):
            continue
    return index


def _mentions(paper: dict, other: dict) -> bool:
    """Does the body justify this relation? Mirrors the old verify_relevance
    check (body mention) but also accepts a non-empty frontmatter note, which is
    the structured form of the same justification."""
    other_short = (other.get("short_name") or "").strip()
    other_title = (other.get("title") or "").strip()
    body = paper.get("body") or ""
    if other_short and other_short in body:
        return True
    return bool(other_title and other_title in body)


def validate_relations(papers: Iterable[dict]) -> list[dict]:
    """Check the whole vault's structured relations. Returns issue dicts:
    ``{code, severity, paper_id, target, message}``.

    The load-bearing invariant is *reciprocity*: if A says "B is my successor",
    B must say "A is my predecessor" with the same type. One-sided or
    contradicting declarations are what make a hand-maintained graph rot, so
    they are errors, not warnings.
    """
    papers = list(papers)
    index = relation_index(papers)
    issues: list[dict] = []

    def add(code, severity, paper, message, target=None):
        issues.append({"code": code, "severity": severity,
                       "paper_id": paper.get("id"), "target": target,
                       "message": message})

    for paper in papers:
        entries, warnings = normalize_relations(paper.get("relations"))
        short = paper.get("short_name") or paper.get("title") or f"id={paper.get('id')}"
        for w in warnings:
            add("bad_entry", ERROR, paper, f"[{short}] {w}")

        declared_targets = {e["target"] for e in entries}
        legacy = paper.get("related_papers") or []
        legacy_targets = {int(x) for x in legacy if isinstance(x, int)
                          or (isinstance(x, str) and x.isdigit())}

        # migration hints: a vault that still only has related_papers
        if legacy_targets and not entries:
            add("unmigrated", WARN, paper,
                f"[{short}] 只有 related_papers {sorted(legacy_targets)}，尚未迁移为 "
                f"relations（无类型/方向语义）——运行 tools/migrate_relations.py")
        elif declared_targets != legacy_targets:
            add("legacy_drift", WARN, paper,
                f"[{short}] related_papers={sorted(legacy_targets)} 与 relations 投影"
                f"{sorted(declared_targets)} 不一致（related_papers 应由工具自动同步）")

        for e in entries:
            target_id = e["target"]
            other = index.get(target_id)
            if target_id == coerce_id(paper.get("id")):
                add("self_reference", ERROR, paper,
                    f"[{short}] 把自身列为关联论文", target_id)
                continue
            if other is None:
                add("unknown_target", ERROR, paper,
                    f"[{short}] relations 指向不存在的论文 id={target_id}", target_id)
                continue
            if e["type"] not in RELATION_TYPES:
                add("unknown_type", ERROR, paper,
                    f"[{short}] → [{other.get('short_name')}] type='{e['type']}' "
                    f"不在词表 {list(RELATION_TYPES)}", target_id)
            if e["direction"] not in DIRECTIONS:
                add("unknown_direction", ERROR, paper,
                    f"[{short}] → [{other.get('short_name')}] direction="
                    f"'{e['direction']}' 不在词表 {list(DIRECTIONS)}", target_id)
                continue

            # ---- year sanity (warning: influence direction is the intent, a
            # year disagreement usually means one of the two is a preprint or a
            # mis-typed year, not that the relation is wrong) ----
            try:
                self_year, other_year = int(paper.get("year")), int(other.get("year"))
                if e["direction"] == "predecessor" and other_year > self_year + 1:
                    add("year_conflict", WARN, paper,
                        f"[{short}]({self_year}) 称 [{other.get('short_name')}]"
                        f"({other_year}) 为前作，但对方更晚发表", target_id)
                elif e["direction"] == "successor" and other_year < self_year - 1:
                    add("year_conflict", WARN, paper,
                        f"[{short}]({self_year}) 称 [{other.get('short_name')}]"
                        f"({other_year}) 为后继，但对方更早发表", target_id)
            except (TypeError, ValueError):
                pass

            # ---- the invariant ----
            back = next((r for r in relations_of(other)
                         if r["target"] == coerce_id(paper.get("id"))), None)
            if back is None:
                add("missing_reciprocal", ERROR, paper,
                    f"[{short}] 声明 → [{other.get('short_name')}]（{e['type']}/"
                    f"{e['direction']}），但对方没有回写该关系——运行即同步或跑 "
                    f"tools/migrate_relations.py", target_id)
            else:
                if back["type"] != e["type"]:
                    add("type_conflict", ERROR, paper,
                        f"[{short}] 声明 {e['type']}，[{other.get('short_name')}] "
                        f"回写 {back['type']}", target_id)
                if back["direction"] != inverse_direction(e["direction"]):
                    add("direction_conflict", ERROR, paper,
                        f"[{short}] 声明 direction={e['direction']}，"
                        f"[{other.get('short_name')}] 回写 {back['direction']}"
                        f"（应为 {inverse_direction(e['direction'])}）", target_id)

            if not e["note"] and not _mentions(paper, other):
                add("no_justification", WARN, paper,
                    f"[{short}] → [{other.get('short_name')}] 既无 note，正文也未提及"
                    f"——关系缺依据，补 note 或正文说明", target_id)

    return issues


def format_issues(issues: Iterable[dict]) -> str:
    """Human-readable dump grouped by severity (used by the CLI tools)."""
    issues = list(issues)
    if not issues:
        return "✅ 关系完整性检查通过：条目合法、互指对称、依据齐备。"
    errors = [i for i in issues if i["severity"] == ERROR]
    warns = [i for i in issues if i["severity"] != ERROR]
    lines = []
    if errors:
        lines.append(f"❌ 关系错误 {len(errors)} 条：")
        lines += [f"  - [{i['code']}] {i['message']}" for i in errors]
    if warns:
        lines.append(f"⚠️  关系提示 {len(warns)} 条：")
        lines += [f"  - [{i['code']}] {i['message']}" for i in warns]
    return "\n".join(lines)


def infer_type(paper_a: dict, paper_b: dict) -> str:
    """Category-equality heuristic, kept from the pre-relations implementation
    for the *legacy* path only (migration + un-annotated ``related_papers``).

    A declared ``type`` always wins: this heuristic cannot see the difference
    between "B extends A" and "B competes with A" — both land in
    ``complementary``/``method_similar`` purely by shared category strings.
    """
    same_domain = paper_a.get("problem_domain") == paper_b.get("problem_domain")
    same_method_cat = paper_a.get("method_category") == paper_b.get("method_category")
    if same_method_cat and same_domain:
        return "method_similar"
    if same_method_cat:
        return "complementary"
    if same_domain:
        return "problem_related"
    return "evolutionary"


def derive_relations(paper: dict, index: dict) -> tuple[list[dict], list[int]]:
    """Build ``relations`` for a legacy paper from its ``related_papers`` list.

    Returns ``(entries, orphan_ids)`` — orphan = a listed paper that is not in
    the vault (a ghost node: the old workflow tolerated them silently).
    """
    entries: list[dict] = []
    orphans: list[int] = []
    raw = paper.get("related_papers") or []
    for value in raw:
        try:
            target = int(value)
        except (TypeError, ValueError):
            continue
        if target == paper.get("id"):
            continue
        other = index.get(target)
        if other is None:
            orphans.append(target)
            continue
        entries.append({
            "target": target,
            "type": infer_type(paper, other),
            "direction": derive_direction(paper.get("year"), other.get("year")),
            "note": "",
        })
    entries.sort(key=lambda e: e["target"])
    return entries, orphans


def project_related_papers(entries: Iterable[dict]) -> list[int]:
    """``relations`` -> the legacy ``related_papers`` projection."""
    return sorted({e["target"] for e in entries})


def describe(entry: dict) -> str:
    """One-line Chinese rendering, shared by CLI reports and tool output."""
    return (f"{RELATION_TYPE_CN.get(entry['type'], entry['type'])}·"
            f"{DIRECTION_CN.get(entry['direction'], entry['direction'])}"
            + (f"：{entry['note']}" if entry.get("note") else ""))


def merge_relation(entries: list[dict], target: int, rtype: str,
                   direction: str, note: str = "",
                   mark_synced: bool = False) -> tuple[list[dict], bool]:
    """Add-or-update one relation inside ``entries`` (in place, sorted).

    Idempotent by target, which is what the reciprocal sync needs: syncing the
    same pair twice must not append a duplicate row. An existing entry is only
    overwritten when the caller supplies a different non-empty value, so a
    sync never silently erases a richer hand-written note.

    ``mark_synced`` flags the entry as a *mirror* — written by sync_relations
    on behalf of the other side's declaration rather than declared by this
    paper's own frontmatter. That flag is what makes relation removal
    self-healing safe: when the declaration disappears, only flagged mirrors
    may be removed, never an entry the other paper declared on its own.
    """
    for e in entries:
        if e["target"] == target:
            changed = False
            if rtype and e["type"] != rtype:
                e["type"], changed = rtype, True
            if direction and e["direction"] != direction:
                e["direction"], changed = direction, True
            if note and e["note"] != note:
                e["note"], changed = _clean_note(note), True
            if mark_synced and not e.get("synced"):
                e["synced"], changed = True, True
            return entries, changed
    new = {"target": int(target), "type": rtype,
           "direction": direction, "note": _clean_note(note)}
    if mark_synced:
        new["synced"] = True
    entries.append(new)
    entries.sort(key=lambda e: e["target"])
    return entries, True
