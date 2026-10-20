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
        direction: predecessor        # 相对本文：前作 / 后继 / 同期并列；可省略——
                                      # 年份可排序时按发表年自动推导（见 resolve_direction）
        note: "本文沿用其 max-sim 打分形式"

``related_papers`` stays as the machine-readable projection of ``relations``
(kept in sync by the writer), so existing queries and Dataview snippets that
read the old field keep working.

Pure functions only — no file I/O, so every rule below is unit-testable.
"""
import re
from typing import Iterable, Optional


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


# An explicitly-declared direction that contradicts the publication years is
# not automatically wrong — preprint/venue-year lags exist — but it must own
# that contradiction in the note. This marker is what "owns" looks like;
# anything else is treated as the perspective-inversion bug this guard exists
# for (live repro 2026-10-20).
YEAR_EXCEPTION_RE = re.compile("年份例外|年份倒挂")

# Note phrases that describe the TARGET as this paper's base/predecessor.
# Checked against the target's own names so the plain word "基于" in unrelated
# contexts ("基于 PPO 的优势估计" with target=SearchR1) cannot fire alone.
BASE_KEYWORDS = ("基座", "前身", "沿用", "基于", "借鉴")

_NAME_WINDOW = 20  # normalized chars a target name may sit from the keyword
_SELF_BASE_WINDOW = 8  # normalized chars before the keyword that may carry 以本文为


def _norm_name(text: str) -> str:
    """Lowercase letters+digits+CJK only — so ``Search-R1`` matches ``SearchR1``."""
    return re.sub(r"[^0-9a-z一-鿿]+", "", (text or "").lower())


def _target_name_variants(other: dict) -> list[str]:
    """Normalized names the note might use for the target paper."""
    variants = []
    for key in ("short_name", "title"):
        value = (other.get(key) or "").strip()
        if value:
            variants.append(_norm_name(value))
    for alias in other.get("aliases") or []:
        if isinstance(alias, str) and alias.strip():
            variants.append(_norm_name(alias))
    return [v for v in variants if len(v) >= 3]  # "R1" alone would match anywhere


def note_base_side(note: str, other: dict) -> Optional[str]:
    """Which side the note says is the pair's base/predecessor.

    Returns ``"target"`` (note describes the other paper as this paper's base),
    ``"self"`` (note describes THIS paper as the other's base — e.g. 「对方以
    本文为基座」, which supports ``successor``), or None. Skips ``synced``
    mirrors at the call site: the same note text is mirrored verbatim to the
    other paper, where both readings flip.

    The 「以本文为」 guard exists because of a live false positive
    (2026-10-20): 「DeepMMSearch-R1 直接以本文为基座」 names the target right
    before the keyword, but the base in that sentence is *this* paper — the
    naive keyword+name match read it backwards.
    """
    norm_note = _norm_name(note)
    if not norm_note:
        return None
    variants = _target_name_variants(other)
    for kw in BASE_KEYWORDS:
        start = 0
        while (idx := norm_note.find(kw, start)) != -1:
            before = norm_note[max(0, idx - _SELF_BASE_WINDOW): idx]
            after = norm_note[idx + len(kw): idx + len(kw) + _NAME_WINDOW]
            if "以本文为" in before or "本文为" in before:
                return "self"
            if variants and any(v in after or v in before for v in variants):
                return "target"
            start = idx + 1
    return None


def resolve_direction(entry: dict, self_year, other_year) -> tuple[str, str]:
    """``(direction, source)`` with declared beating derived.

    ``source`` is ``"declared"`` (entry carried an explicit direction),
    ``"derived"`` (publication order decided — strictly different years), or
    ``"defaulted"`` (equal/unknown years fell back to peer). Callers surface
    ``defaulted``: a same-year base/followup pair silently becoming ``peer``
    loses both its edge and its semantics.
    """
    if entry.get("direction"):
        return entry["direction"], "declared"
    derived = derive_direction(self_year, other_year)
    return derived, ("derived" if derived != "peer" else "defaulted")


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


def index_papers(papers: Iterable[dict]) -> tuple:
    """``({paper_id: paper}, duplicates)`` — the only id→paper index builder.

    Ids that ``coerce_id`` rejects (missing, ``id: yes``, ``id: 2.0``, ``id:
    "7.5"``) are skipped rather than folded onto a real paper. This used to be a
    bare ``int(p["id"])``, which mapped ``id: yes`` onto paper 1 — the exact
    failure coerce_id exists to block, and it disagreed with the ``_by_id`` copy
    in tools/verify_graph_arrows.py about which notes even exist.

    ``duplicates`` is ``[(pid, dropped_file, kept_file)]``: two notes sharing an
    id overwrite each other here, and the dropped one's relations would vanish
    from the rendered graph without a word, so callers that *present* the index
    get told about it (see get_paper_by_id, which warns on the same condition).
    """
    index, dupes = {}, []
    for p in papers:
        pid = coerce_id(p.get("id"))
        if pid is None:
            continue
        if pid in index:
            dupes.append((pid, index[pid].get("file", "?"), p.get("file", "?")))
        index[pid] = p
    return index, dupes


def relation_index(papers: Iterable[dict]) -> dict[int, dict]:
    """``{paper_id: paper}`` — id rules in index_papers."""
    return index_papers(papers)[0]


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

        # `related_papers` is written by the projection and never read as input,
        # so an entry here that no declaration covers means one of two kinds of
        # loss: a hand edit (silently overwritten by the next sync) or a note that
        # was never migrated (its ids are dropped on its first write). Neither is
        # a hint, so this is an error and it names the fix. Entries that do not
        # coerce to an id count as uncovered too — a bare `int()` comparison would
        # have let `related_papers: ["3"]` and a typo slip through unnoticed.
        raw_projection = paper.get("related_papers") or []
        uncovered = [x for x in raw_projection if coerce_id(x) not in declared_targets]
        if uncovered:
            add("unmigrated" if not entries else "projection_drift", ERROR, paper,
                f"[{short}] related_papers 有 relations 不覆盖的条目 {uncovered}"
                f"（{'该笔记只有旧字段，尚未迁移为 relations' if not entries else '投影字段被手改'}）"
                f"—— related_papers 由工具按 relations 重写，下次同步会丢弃这些条目；"
                f"要保留的关系先写成 relations（旧 vault 跑 tools/migrate_relations.py）")

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
            if e["direction"] and e["direction"] not in DIRECTIONS:
                add("unknown_direction", ERROR, paper,
                    f"[{short}] → [{other.get('short_name')}] direction="
                    f"'{e['direction']}' 不在词表 {list(DIRECTIONS)}", target_id)
                continue

            # ---- direction resolution: declared beats derived ----
            # An omitted direction is filled from publication order — the
            # objective field decides, and the perspective-inversion bug class
            # (live repro 2026-10-20) becomes structurally impossible for
            # entries that simply leave the field out. Equal/unknown years
            # fall back to peer with a warning: a same-year base/followup pair
            # silently losing its edge is exactly what the agent must see.
            direction, dsource = resolve_direction(e, paper.get("year"), other.get("year"))
            if dsource == "defaulted":
                add("direction_defaulted_peer", WARN, paper,
                    f"[{short}] → [{other.get('short_name')}] 未声明 direction 且双方"
                    f"同年/年份缺失——已按 peer 处理（无图谱边）；若实为前作/后继，"
                    f"请显式声明 direction", target_id)

            # ---- year sanity: a DECLARED direction that crosses years against
            # publication order is almost always the perspective inversion the
            # declaring agent makes while filling the field — read "successor"
            # as "I am the successor" instead of "the target is" (live repro
            # 2026-10-20: DGPO 2026 declared its own predecessor SearchR1 2025
            # as successor). Zero tolerance, ERROR unless the note owns the
            # contradiction via a 「年份例外:…」 marker — preprint/venue-year
            # lags are real, but they must be declared, not silent. Derived
            # directions cannot contradict the years by construction.
            if dsource == "declared":
                try:
                    self_year, other_year = int(paper.get("year")), int(other.get("year"))
                    contradicts = ((e["direction"] == "predecessor" and other_year > self_year)
                                   or (e["direction"] == "successor" and other_year < self_year))
                    if contradicts:
                        if YEAR_EXCEPTION_RE.search(e.get("note") or ""):
                            add("year_conflict", WARN, paper,
                                f"[{short}]({self_year}) 称 [{other.get('short_name')}]"
                                f"({other_year}) 为{'前作' if e['direction'] == 'predecessor' else '后继'}"
                                f"，与发表年矛盾——note 已声明年份例外，以声明为准", target_id)
                        else:
                            add("year_conflict", ERROR, paper,
                                f"[{short}]({self_year}) 称 [{other.get('short_name')}]"
                                f"({other_year}) 为{'前作' if e['direction'] == 'predecessor' else '后继'}"
                                f"，与发表年矛盾且 note 无「年份例外」说明——确属倒挂"
                                f"（preprint/会议年差）就在 note 补「年份例外：原因」，"
                                f"否则把 direction 改为与年份一致的方向", target_id)
                except (TypeError, ValueError):
                    pass

            # ---- note-vs-direction semantic cross-check ----
            # The note text is independent evidence the year check cannot see.
            # Mirrors are skipped: the same note is written verbatim to the
            # other side, where the base relationship flips and the keywords
            # would false-positive (live repro 2026-10-20: DGPO's mirrored
            # note in SearchR1.md still says "直接基座：DGPO 整体沿用…").
            if not e.get("synced") and direction in ("predecessor", "successor"):
                side = note_base_side(e.get("note") or "", other)
                if ((side == "target" and direction == "successor")
                        or (side == "self" and direction == "predecessor")):
                    add("note_direction_mismatch", WARN, paper,
                        f"[{short}] → [{other.get('short_name')}] 的 note 把"
                        f"{'对方' if side == 'target' else '本文'}描述为基座/前作，"
                        f"但 direction 解析为 {direction}——疑似视角反转，请核对方向",
                        target_id)

            # ---- the invariant ----
            back = next((r for r in relations_of(other)
                         if r["target"] == coerce_id(paper.get("id"))), None)
            if back is None:
                add("missing_reciprocal", ERROR, paper,
                    f"[{short}] 声明 → [{other.get('short_name')}]（{e['type']}/"
                    f"{direction}），但对方没有回写该关系——运行即同步或跑 "
                    f"tools/migrate_relations.py", target_id)
            else:
                back_direction, _ = resolve_direction(back, other.get("year"),
                                                      paper.get("year"))
                if back["type"] != e["type"]:
                    add("type_conflict", ERROR, paper,
                        f"[{short}] 声明 {e['type']}，[{other.get('short_name')}] "
                        f"回写 {back['type']}", target_id)
                if back_direction != inverse_direction(direction):
                    add("direction_conflict", ERROR, paper,
                        f"[{short}] 声明 direction={direction}，"
                        f"[{other.get('short_name')}] 回写 {back_direction}"
                        f"（应为 {inverse_direction(direction)}）", target_id)

            if not e["note"] and not _mentions(paper, other):
                add("no_justification", WARN, paper,
                    f"[{short}] → [{other.get('short_name')}] 既无 note，正文也未提及"
                    f"——关系缺依据，补 note 或正文说明", target_id)

    return issues


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
    """``relations`` -> the ``related_papers`` projection.

    The one writer of that field: ``related_papers`` is derived here on every
    write and read by nothing as input, so Obsidian and Dataview queries keep
    working while the semantics live only in ``relations``.
    """
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
