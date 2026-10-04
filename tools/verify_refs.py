#!/usr/bin/env python3
"""verify_refs.py — existence gate for the external works a report names.

A report leans on other people's papers: base methods (报告 §2.3), baselines
(§3.2), concurrent work (§4.5), stronger-baseline counterfactuals (§4.8). Those
identities come from the paper's bibliography + model memory, and model memory
is demonstrably unreliable (this repo has turned up both a fabricated
attribution and a title-lookalike trap — see mcp_server/cite_api.py). This tool
batch-runs cite_api.cite_verify over that set and writes a ledger the report §6
table and the QA agent both consume.

It is an EXISTENCE gate, NOT a fake-reference detector: OpenAlex/Semantic
Scholar under-cover workshops, theses, non-English venues and very recent
preprints. "Not indexed" is therefore never reported as "does not exist" —
the gate labels below keep that distinction, and the readout repeats it.

Input (auto-detected from the first non-blank character):
  plain text  one ref per line:
                Title | Author | Year | doi:10.… | arxiv:1706.03762
              (`#` comments / blank lines skipped; only Title is required;
              author & year are the values the PAPER claims — they are what
              the gate cross-checks, so fill them when the bibliography gives
              them. `doi:`/`arxiv:` fields anywhere strengthen the lookup; an
              arxiv.org URL is converted to an id, other URLs are recorded but
              never sent as bogus identifiers)
  JSON        [{"title":…, "author":…, "year":…, "doi":…, "arxiv_id":…}, …]
              (a top-level {"refs": [...]} wrapper is also accepted)

Gate labels:
  exists_ok    exact/probable AND the claimed author agrees with the record.
               A year disagreement does NOT downgrade this — OpenAlex merges
               preprint + conference versions and re-indexes them under a
               later year (the real "Attention Is All You Need" record says
               2025), so a year mismatch is surfaced as a note on the row.
  discrepancy  the claimed author is absent from EVERY title-level candidate —
               that is the misattribution signal (it is what separates the real
               paper from a title-lookalike). The report must SHOW the split,
               never silently trust either side. The gate reads the whole
               candidate pool, not matches[0]: live 2026-09-29 OpenAlex ranked
               a 53-cite lookalike above the real Transformer record.
  defer        an identifier was supplied but the lookup was incomplete
               (OpenAlex coverage gap / S2 rate-limit) → retry these rows
  unresolved   no confident record: "not indexed", NOT "does not exist"
  network      the lookup could not complete (transient/link failure), or the
               row was never queried because of the quota circuit-breaker
  empty        the row had no title (skipped)

Quota reality (live 2026-10-04): OpenAlex free tier is one-time credits per IP
(~a hundred lookups) + S2 keyless shares a global pool — a big existence batch
CAN exhaust them mid-run. The tool then trips a circuit-breaker after
QUOTA_HALT_AFTER consecutive 429s (remaining rows get network rows marked
"配额熔断", ledger carries quota_halt with Retry-After when the API provides
one) instead of hammering the door. Finish later with:
  python tools/verify_refs.py --refs refs.txt --out ledger.json --resume
which carries every already-judged row over without spending quota and
re-verifies only network/defer/missing rows.

Outputs:
  --out  ledger JSON (default: cite_ledger.json next to the refs file)
  --md   Markdown table ready to paste into 报告 §6 (optional)

Usage:
  python tools/verify_refs.py --refs refs.txt --md refs_table.md

Exit codes: 0 = every row resolved on this pass | 2 = ran, but ≥1 row is
network/defer (the ledger is partial — --resume in a fresh window) | 1 = bad args/input.
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp_server import cite_api  # noqa: E402
from mcp_server.console import force_utf8  # noqa: E402

GATES = ("exists_ok", "discrepancy", "defer", "unresolved", "network", "empty")

ICON = {"exists_ok": "✅", "discrepancy": "⚠️", "defer": "⏸",
        "unresolved": "❓", "network": "🌐", "empty": "➖"}
LABEL_CN = {"exists_ok": "存在", "discrepancy": "疑似错配",
            "defer": "待重试", "unresolved": "未收录", "network": "核验不可用",
            "empty": "无标题"}


# ────────────────────────────────── input parsing

def _ref_from_fields(fields):
    """['Title', 'Author', '2017', 'doi:10.…'] -> dict. Only the title is
    mandatory; identifiers are harvested from any field that carries a prefix
    so callers never have to remember column positions."""
    ref = {"title": "", "author": "", "year": None, "doi": "", "arxiv_id": "",
           "url": ""}
    if fields and fields[0].strip():
        ref["title"] = fields[0].strip()
    for raw in fields[1:]:
        f = raw.strip()
        low = f.lower()
        if not f:
            continue
        if low.startswith("doi:"):
            ref["doi"] = f[4:].strip()
        elif low.startswith(("arxiv:", "arxiv ")):
            ref["arxiv_id"] = f.split(":", 1)[1].strip() if ":" in f else f[6:].strip()
        elif low.startswith("http"):
            # cite_api only resolves DOIs / arXiv ids. An arXiv abs/PDF link
            # still yields an id; any other URL is kept as a hint for the
            # report author but never sent to the API as a bogus identifier.
            if "arxiv.org/" in low:
                tail = f.split("arxiv.org/", 1)[1]
                # .../abs/2004.12832v2, .../pdf/2004.12832v2.pdf and the
                # slash-less variants all reduce to the bare id
                for marker in ("/abs/", "/pdf/", "abs/", "pdf/"):
                    if marker in tail:
                        tail = tail.split(marker, 1)[1]
                        break
                tail = tail.split("?", 1)[0].strip("/ ").removesuffix(".pdf")
                ref["arxiv_id"] = ref["arxiv_id"] or tail
            else:
                ref["url"] = ref["url"] or f
        elif f.isdigit() and len(f) == 4 and ref["year"] is None:
            ref["year"] = int(f)
        elif not ref["author"]:
            ref["author"] = f
    return ref


def parse_refs(path: Path):
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith(("[", "{")):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("refs", [])
        refs = []
        for item in data:
            if not isinstance(item, dict):
                continue
            refs.append({
                "title": str(item.get("title", "")).strip(),
                "author": str(item.get("author", "")).strip(),
                "year": int(item["year"]) if str(item.get("year", "")).strip().isdigit() else None,
                "doi": str(item.get("doi", "")).strip(),
                "arxiv_id": str(item.get("arxiv_id", "")).strip(),
                "url": str(item.get("url", "")).strip(),
            })
        return refs
    refs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ref = _ref_from_fields(line.split("|"))
        if any(ref[k] for k in ("title", "doi", "arxiv_id", "url")):
            refs.append(ref)
    return refs


# ────────────────────────────────── gate classification

SIM_STRONG = 0.75  # cite_api's own "strong title candidate" threshold


def _evidence(result: dict, author: str):
    """The candidate the gate reasons about: the top title-level match, but
    preferring one that carries the claimed author. Live 2026-09-29: OpenAlex
    free-text ranking is noisy enough that the *lookalike* ("Is Attention All
    You Need?", 53 cites) surfaced first while the real Transformer record sat
    below it — judging on matches[0] alone would have mislabelled the most
    cited paper in ML as a misattribution."""
    cands = [m for m in (result.get("matches") or [])
             if (m.get("similarity") or 0) >= SIM_STRONG]
    if not cands:
        return None
    if author:
        for m in cands:
            if m.get("author_match"):
                return m
    return cands[0]


def classify(result: dict, author: str) -> str:
    """cite_api verdict -> gate label. The rules that matter:

    * identifier-present-but-incomplete is DEFER (the API says so in its notes)
      and an absent record is UNRESOLVED — never "does not exist";
    * the gate reads the CANDIDATE POOL, not the verdict level: a title-level
      match (sim >= 0.75) means the work exists. Only the absence of the
      claimed author from every such candidate is a misattribution signal
      (discrepancy) — that is what separates the real paper from a lookalike.
    * a year mismatch alone is NOT a signal — OpenAlex merges preprint and
      conference versions and re-indexes them under a later year (the real
      Transformer record reports 2025), so a year disagreement becomes a note
      on an exists_ok row instead of a false alarm."""
    if result.get("verdict") == "network_error":
        return "network"
    evidence = _evidence(result, author)
    if evidence is not None:
        if author and not evidence.get("author_match"):
            return "discrepancy"
        return "exists_ok"
    notes = " ".join(result.get("notes") or [])
    if "identifier given but lookup incomplete" in notes:
        return "defer"
    return "unresolved"


def gate_note(gate: str, result: dict, ref: dict) -> str:
    best = _evidence(result, ref.get("author", "")) or {}
    if gate == "discrepancy":
        lib = "、".join((best.get("authors") or [])[:2]) or "（库记录未列作者）"
        return (f"自报作者「{ref['author']}」未见于任何标题级候选（库中候选作者含 {lib}）"
                "——可能是标题形近的另一篇，或自报信息有误；报告须展示该分歧，不得静默采信任一方")
    if gate == "exists_ok":
        parts = []
        if ref.get("year") and not best.get("year_match"):
            if best.get("year"):
                parts.append(f"库中记录年份 {best['year']}（自报 {ref['year']}）"
                             "——预印本/会议合并记录与再索引年常见，核对即可")
            else:
                parts.append(f"库中该记录无年份（自报 {ref['year']}）")
        if result.get("verdict") not in ("exact", "probable") and not parts:
            parts.append("标题级匹配但置信未达 probable（作者/年份未给全时常见）；补 author/年份后重跑更稳")
        return "；".join(parts)
    if gate == "defer":
        return "已给标识符但解析不完整（OpenAlex 覆盖缺口 / S2 限流）——重试，不可判为不存在"
    if gate == "unresolved":
        return "OpenAlex/S2 未收录——**不等于不存在**（workshop/学位论文/非英文venue/新预印本覆盖有限）；换标识符重试或按【未核验】标注"
    if gate == "network":
        return ("网络/接口失败（连续 429 时批量会自动熔断）——窗口重置后对同一 "
                "--out 加 --resume 只补跑失败行；报告按【外部核验不可用】如实标注")
    if gate == "empty":
        return "该行无标题（或仅有无法检索的 URL），已跳过核验"
    return ""


# Consecutive 429 rows that trip the circuit-breaker. 2026-10-04 live case:
# OpenAlex answers every lookup with 429 once the one-time IP credits are
# spent (Retry-After hours ahead); without a stop the batch hammers all
# remaining rows into 🌐 and reads like a broken network rather than a spent
# quota. Three consecutive gives flaky-but-alive providers (intermittent S2
# 429s recover within one retry) a fair chance first.
QUOTA_HALT_AFTER = 3

_RETRY_AFTER_RE = re.compile(r"Retry-After (\d+)")


def _is_429(result: dict) -> bool:
    return any("429" in n for n in (result.get("notes") or []))


def _retry_after(result: dict):
    for n in result.get("notes") or []:
        m = _RETRY_AFTER_RE.search(n)
        if m:
            return int(m.group(1))
    return None


def _row_key(ref: dict):
    """Identity of a refs row inside a previous ledger, for --resume.
    The title is what parse_refs puts into entry["title"] when no queryable
    field exists; claimed author/year disambiguate duplicate titles."""
    return (ref.get("title", ""), ref.get("author", ""),
            str(ref.get("year") or ""))


def load_previous(out_path):
    """--resume: read the earlier ledger, return {row_key: entry} for rows
    whose verdict stands (anything but network/defer — those two say "we
    never got an answer", exactly what resume must retry)."""
    try:
        data = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    prev = {}
    for e in data.get("entries", []):
        if e.get("gate") in ("network", "defer"):
            continue
        c = e.get("claimed") or {}
        prev.setdefault((e.get("title", ""), c.get("author", ""),
                         str(c.get("year") or "")), e)
    return prev


def build_ledger(refs, delay: float = 0.6, max_n: int = 60, verify=None,
                 previous=None):
    verify = verify or cite_api.cite_verify
    previous = previous or {}
    entries, truncated, carried = [], 0, 0
    quota_halt = None
    streak = 0
    for ref in refs:
        key = _row_key(ref)
        if key in previous:                      # --resume: verdict stands
            entries.append(previous[key])
            carried += 1
            continue
        if len(entries) - carried >= max_n:
            truncated += 1
            continue
        title = ref.get("title", "")
        if quota_halt is not None:               # breaker tripped: don't query
            entries.append(_quota_entry(ref, quota_halt))
            continue
        if not (title or ref.get("doi") or ref.get("arxiv_id")):  # url-only row -> skipped, but visible in the ledger
            result = {"verdict": "not_found", "matches": [], "notes": ["empty query"]}
            gate = "empty"
        else:
            try:
                result = verify(query=title, author=ref.get("author", ""),
                                year=ref.get("year"), doi=ref.get("doi", ""),
                                arxiv_id=ref.get("arxiv_id", ""))
            except Exception as exc:  # never let one row kill the batch
                result = {"verdict": "network_error", "matches": [],
                          "notes": [f"{type(exc).__name__}: {exc}"]}
            gate = classify(result, ref.get("author", ""))
        # The gate reasons about _evidence()'s candidate (the author-matched
        # one); the ledger row — and the §6 table rendered from it — must show
        # THE SAME candidate, or an exists_ok verdict presents the lookalike's
        # venue/year/citations as the library record and invents a year note
        # about the wrong paper (live repro 2026-10-04: fake 53-cite record on
        # top of the real Transformer row). matches[0] only survives as the
        # fallback when NOTHING reaches SIM_STRONG: that row is unresolved
        # anyway, and "closest thing found" is still useful evidence there.
        best = _evidence(result, ref.get("author", "")) \
            or ((result.get("matches") or [None])[0] or {})
        entries.append({
            "title": title or ref.get("doi") or ref.get("arxiv_id")
                     or ref.get("url") or "(无标题)",
            "claimed": {"author": ref.get("author", ""), "year": ref.get("year"),
                        "doi": ref.get("doi", ""), "arxiv_id": ref.get("arxiv_id", ""),
                        "url": ref.get("url", "")},
            "gate": gate,
            "verdict": result.get("verdict", ""),
            # keys mirror cite_api._brief() exactly (plus the enrichment) —
            # a wrong key here silently empties the report table
            "match": {k: best.get(k) for k in
                      ("openalex_id", "title", "year", "venue", "doi",
                       "cited_by_count", "authors", "similarity",
                       "author_match", "year_match")} if best else None,
            "note": gate_note(gate, result, ref),
            "api_notes": result.get("notes", []),
        })
        # circuit-breaker bookkeeping: ONLY consecutive 429s count — one
        # transient S2 429 inside an otherwise healthy run must not halt a
        # 70-row batch.
        if gate == "network" and _is_429(result):
            streak += 1
            if streak >= QUOTA_HALT_AFTER and quota_halt is None:
                quota_halt = {"halted_at_entry": len(entries),
                              "retry_after_seconds": _retry_after(result)}
        else:
            streak = 0
        if delay and quota_halt is None and len(entries) < len(refs):
            time.sleep(delay)
    counts = {g: sum(1 for e in entries if e["gate"] == g) for g in GATES}
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": "OpenAlex (primary) + Semantic Scholar (fallback)",
        "counts": counts,
        "truncated": truncated,
        "carried": carried,
        "quota_halt": quota_halt,
        "entries": entries,
    }


def _quota_entry(ref, quota_halt):
    """A row the batch never queried because the providers proved they were
    spent. It is NOT a network flake — the note says so, and --resume is the
    documented way to finish the pass in a fresh quota window."""
    secs = quota_halt.get("retry_after_seconds")
    when = f"（上游声明约 {secs}s 后重置）" if secs else "（上游未声明重置时间）"
    return {
        "title": ref.get("title") or ref.get("doi") or ref.get("arxiv_id")
                 or ref.get("url") or "(无标题)",
        "claimed": {"author": ref.get("author", ""), "year": ref.get("year"),
                    "doi": ref.get("doi", ""), "arxiv_id": ref.get("arxiv_id", ""),
                    "url": ref.get("url", "")},
        "gate": "network",
        "verdict": "quota_halt",
        "match": None,
        "note": f"配额熔断：连续 {QUOTA_HALT_AFTER} 行 HTTP 429，此后未再发起查询"
                f"{when}。窗口重置后对同一 --out 加 --resume 补跑；"
                "报告侧按【外部核验不可用】如实标注，**不得写成“不存在”或“核验失败”**",
        "api_notes": ["quota-halt"],
    }


# ────────────────────────────────── output

def _record_cell(entry: dict) -> str:
    """One rendering of the library side, shared by the md table and the
    console summary so they can never drift apart."""
    m = entry.get("match") or {}
    if not m:
        return "—"
    bits = [b for b in (m.get("venue"), m.get("year")) if b]
    if m.get("cited_by_count") is not None:
        bits.append(f"被引 {m['cited_by_count']}")
    record = " · ".join(str(b) for b in bits) or "—"
    if entry["gate"] == "unresolved" and m.get("similarity") is not None:
        # a weak candidate is useful evidence ("closest thing is X"), but it
        # must never read as the record we were looking for
        record = f"（弱匹配 {m['similarity']:.2f}）{record}"
    return record


def _short_note(entry: dict) -> str:
    """The report-facing parenthetical. The long instructional note lives in
    the console readout and the ledger JSON — a report table cell must stay
    readable, and the legend right above it carries the caveats."""
    if entry["gate"] == "exists_ok":
        claimed = entry["claimed"]
        match = entry.get("match") or {}
        if claimed.get("year") and match and not match.get("year_match"):
            return f"库记录年份 {match.get('year') or '缺失'}"
        if entry.get("verdict") == "probable":
            return "probable 级匹配"
        return ""
    return {"discrepancy": "自报作者未见于库记录",
            "defer": "覆盖缺口/限流，待重试"}.get(entry["gate"], "")


def render_md(ledger: dict) -> str:
    lines = [
        "> 报告中点名的外部工作逐条经 `cite_verify` 存在性核验"
        "（OpenAlex 主 + Semantic Scholar 兜底）。核验是**存在性门**，不是"
        "\"伪造引用检测\"：❓ 未收录只说明外部库没覆盖（workshop/学位论文/非英文 venue/"
        "新预印本），**不得写成\"不存在\"**。",
        ">",
        "> ✅ 存在（自报作者/年份与库记录一致；年份不一致会就地注明原因）"
        " ｜ ⚠️ 疑似错配（自报作者未见于库记录——可能是标题形近的另一篇）"
        " ｜ ❓ 外部库未收录（不等于不存在）"
        " ｜ ⏸ 待重试（覆盖缺口/限流） ｜ 🌐 核验不可用",
        "",
        "| # | 工作（报告点名） | 论文自报 | 核验 | 外部库记录 |",
        "|---|-----------------|---------|------|-----------|",
    ]
    for i, e in enumerate(ledger["entries"], 1):
        claimed = ", ".join(str(x) for x in (e["claimed"]["author"], e["claimed"]["year"]) if x)
        icon = ICON.get(e["gate"], "")
        short = _short_note(e)
        conclusion = f"{icon} {LABEL_CN.get(e['gate'], e['gate'])}"
        if short:
            conclusion += f"（{short}）"
        title = (e["title"] or "").replace("|", "\\|")
        lines.append(f"| {i} | {title} | {claimed or '—'} | {conclusion} | "
                     f"{_record_cell(e)} |")
    if ledger.get("truncated"):
        lines.append("")
        lines.append(f"> ⚠️ 另有 {ledger['truncated']} 条超出本次上限未核验（--max 调整）。")
    return "\n".join(lines) + "\n"


def print_summary(ledger: dict, out_path: Path, md_path):
    c = ledger["counts"]
    order = [("exists_ok", "✅"), ("discrepancy", "⚠️"), ("unresolved", "❓"),
             ("defer", "⏸"), ("network", "🌐"), ("empty", "➖")]
    head = " | ".join(f"{icon} {LABEL_CN[g]} {c[g]}" for g, icon in order if c.get(g))
    print(f"\n核验 {len(ledger['entries'])} 条：{head or '（无条目）'}")
    for i, e in enumerate(ledger["entries"], 1):
        tail = f" → {_record_cell(e)}" if e.get("match") else ""
        print(f"  {ICON.get(e['gate'], '?')} [{i}] {e['title']}{tail}")
        if e.get("note"):
            print(f"        {e['note']}")
    if c.get("unresolved") or c.get("defer"):
        print("  ⚠️  “未收录/待重试”只表示外部库没覆盖或限流，**不得写成“不存在”**；"
              "按 mcp_server 的 DEFER 语义重试或降级标注。")
    if ledger.get("quota_halt"):
        q = ledger["quota_halt"]
        when = (f"上游声明约 {q['retry_after_seconds']}s 后重置"
                if q.get("retry_after_seconds") else "重置时间未知")
        print(f"  🛑 配额熔断于第 {q['halted_at_entry']} 行（{when}）："
              "OpenAlex 免费额度为每 IP 一次性 credit（约百次查询），大批量请分日跑；"
              "窗口重置后 `--resume` 只补跑失败行，已判定行原样保留。")
    if ledger.get("carried"):
        print(f"  ↩ --resume：{ledger['carried']} 条已有判定直接沿用，未消耗配额。")
    print(f"  ledger → {out_path}")
    if md_path:
        print(f"  md     → {md_path}")


# ────────────────────────────────── cli

def main(argv=None):
    force_utf8()
    ap = argparse.ArgumentParser(
        description="Batch existence-gate the external works a report names "
                    "(writes cite_ledger.json + an optional §6 markdown table).")
    ap.add_argument("--refs", required=True,
                    help="input file: 'Title | Author | Year | doi:…' lines, or JSON list")
    ap.add_argument("--out", default="", help="ledger JSON path (default: next to --refs)")
    ap.add_argument("--md", default="", help="also write a markdown table for 报告 §6")
    ap.add_argument("--delay", type=float, default=0.6,
                    help="seconds between lookups (default 0.6; be polite to S2)")
    ap.add_argument("--max", type=int, default=60,
                    help="cap on rows actually verified (default 60)")
    ap.add_argument("--resume", action="store_true",
                    help="merge with the existing --out ledger: rows already "
                         "judged (everything but network/defer) are carried "
                         "over without spending API quota; only failed/missing "
                         "rows are (re)verified — the way to finish a pass after "
                         "a quota-halt")
    args = ap.parse_args(argv)

    refs_path = Path(args.refs)
    if not refs_path.is_file():
        print(f"error: refs file not found: {refs_path}")
        return 1
    try:
        refs = parse_refs(refs_path)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        print(f"error: cannot parse {refs_path}: {exc}")
        return 1
    if not refs:
        print(f"error: no references parsed from {refs_path}")
        return 1

    out_path = Path(args.out) if args.out else refs_path.with_name("cite_ledger.json")
    previous = {}
    if args.resume:
        if not out_path.is_file():
            print(f"note: --resume 但 {out_path} 不存在——本次等同全量跑批")
        previous = load_previous(out_path)
    ledger = build_ledger(refs, delay=args.delay, max_n=args.max,
                          previous=previous)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    md_path = None
    if args.md:
        md_path = Path(args.md)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(render_md(ledger), encoding="utf-8")
    print_summary(ledger, out_path, md_path)
    partial = ledger["counts"].get("network", 0) + ledger["counts"].get("defer", 0)
    return 2 if partial else 0


if __name__ == "__main__":
    sys.exit(main())
