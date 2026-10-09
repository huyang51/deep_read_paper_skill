#!/usr/bin/env python3
"""lint_math.py — flag math written WITHOUT $…$ delimiters in a report.

Why this exists (field incident 2026-10-09)
-------------------------------------------
A report contained, as plain text:

    1. **编码**：查询 q=(qT,qV) 和文档 d=(dT,dV) 分别过……取出**每一层**的激活 E^m_l（m∈{T,V}）。

Markdown passes that through verbatim and so does the HTML renderer, so the
reader sees raw LaTeX-ish text — not the formula — in BOTH the .md and in
`html/*.html`.

The renderer is not at fault: `protect_math()` in render_report.py only
stashes *delimited* math (`$…$`, `$$…$$`, `\[…\]`, `\(…\)`, `\begin{…}`) and
deliberately has no $-pair auto-detection heuristic — it cannot tell
`q=(qT,qV)` from prose. Its `--offline` fallback (formulas shown as readable
LaTeX source) makes an undelimited formula indistinguishable from a correctly
rendered one that merely lost its KaTeX bundle.

So the only reliable place to catch this is a lint over the source .md.

What counts as a hit
--------------------
After stripping fenced code, inline code and delimited math, a line is flagged
if it still carries a high-precision math signal:

  caret        `E^V_l`, `h_{l+1}`            → `^`
  subscript    `z_x`, `h_l`                  → identifier immediately `_`
  backslash    `\alpha`, `\sum`              → `\command`
  combining    `d̄` (d + U+0304)             → a broken/partial TeX render
  paren-form   `q=(qT,qV)`, `L = (Lx+Ly)/2`  → `=` directly before `(`

Plain identifiers such as `k=32`, `id=3`, `P=2000`, `r=α=32` are NOT flagged —
they render fine as text and wrapping them is cosmetic. Nor is `C > γ` or
`Δ=0.2`: math-as-prose still reads correctly, it is not LaTeX source leaking
into the page. The rules above are deliberately limited to signals that mean
the source carries **LaTeX syntax the renderer will not interpret**.

Usage
-----
    python tools/lint_math.py reports/Foo_解读报告.md
    python tools/lint_math.py reports/               # every *.md under it
    python tools/lint_math.py --json reports/Foo.md

Exit codes: 0 clean | 1 bare math found | 2 bad arguments
"""
import argparse
import json
import re
import sys
from pathlib import Path

# --- regions where a `$` / `^` / `_` means something else -------------------
_FENCE_RE = re.compile(r"^([ \t]*)(```+|~~~+)[^\n]*\n.*?^\1\2[ \t]*$",
                       re.DOTALL | re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
# COUPLING: these two dollar patterns mirror render_report.protect_math()'s
# acceptors. They must stay identical to it, or the lint strips math the
# renderer will NOT treat as math and the leftover-`$` rule below goes blind.
# That is exactly the `$k > $` bug: the renderer's `(?<!\s)` before the closing
# dollar rejects a `$… $` pair, so the text leaked through with literal `$`.
_BLOCK_DOLLAR = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_INLINE_DOLLAR = re.compile(r"(?<!\\)\$(?!\s)([^$\n]+?)(?<!\s)\$(?!\d)")
_INLINE_DOLLAR_SP = re.compile(r"(?<!\\)\$\s+([^$\n]+?)\s+\$(?!\d)")
_STRONG_MATH = re.compile(r"[\\^_]|[A-Za-z]\s*[=<>]")
_MATH_RE = re.compile(
    r"\\\[.+?\\\]"                      # \[ … \]
    r"|\\\(.+?\\\)"                     # \( … \)
    r"|\\begin\{(?:equation|align|gather|eqnarray|array|matrix|bmatrix|"
    r"pmatrix|cases|split)\*?\}.+?\\end\{\w+\*?\}",
    re.DOTALL)
_INLINE_HTML_RE = re.compile(r"</?[A-Za-z][^>]*>")
_ESCAPED_DOLLAR_RE = re.compile(r"\\\$")
_CJK_RE = re.compile(r"[⺀-ㄯ㐀-䶿一-鿿豈-﫿]")

RULES = [
    ("caret", re.compile(r"\^"),
     "caret outside math — write `$…$`"),
    ("subscript", re.compile(r"(?<![A-Za-z0-9_\\])[A-Za-z]_"),
     "identifier subscript outside math — write `$…$`"),
    ("backslash", re.compile(r"\\[A-Za-z]{2,}"),
     "LaTeX command outside math — write `$…$`"),
    ("combining", re.compile(r"[̀-ͯ]"),
     "combining mark (e.g. d̄ = d+U+0304) — write `$\\bar{d}$`"),
    ("paren-form", re.compile(r"[A-Za-z]\s*=\s*\("),
     "`x=(…)` looks like a flattened formula — write `$…$`"),
    ("dollar", re.compile(r"\$"),
     "stray/unbalanced `$` — the renderer did NOT accept this pair, so the "
     "dollar shows up as literal text (e.g. `$k > $ x` → write `$k$ > x`)"),
]


def _looks_mathy(body):
    """Mirror of render_report._looks_mathy — keeps `$5 与 $10` style prose
    from being mistaken for a formula."""
    if any(c in body for c in "\\^_=+{}"):
        return True
    if _CJK_RE.search(body) and not re.search(r"[A-Za-z]", body):
        return False
    return bool(re.search(r"[A-Za-z0-9]", body)) and len(body) <= 120


def strip_nonmath(text):
    """Text with code fences, inline code, inline HTML and — crucially — only
    the math the RENDERER would accept, replaced by blanks (newlines preserved
    so line numbers stay valid).

    A `$…$` pair the renderer rejects is deliberately left in place: it will
    surface as a literal `$` and be caught by the `dollar` rule."""
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))

    def _inline(m, gate):
        return blank(m) if gate(m.group(1)) else m.group(0)

    text = _ESCAPED_DOLLAR_RE.sub(blank, text)     # \$ is a literal dollar
    text = _FENCE_RE.sub(blank, text)
    text = _INLINE_CODE_RE.sub(blank, text)
    text = _BLOCK_DOLLAR.sub(blank, text)
    text = _INLINE_DOLLAR.sub(lambda m: _inline(m, _looks_mathy), text)
    text = _INLINE_DOLLAR_SP.sub(lambda m: _inline(m, _STRONG_MATH.search), text)
    text = _MATH_RE.sub(blank, text)
    text = _INLINE_HTML_RE.sub(blank, text)
    return text


def lint_text(text):
    """[{line, rule, hint, context}] for every bare-math signal in `text`."""
    hits = []
    for lineno, line in enumerate(strip_nonmath(text).split("\n"), 1):
        for rule, rx, hint in RULES:
            m = rx.search(line)
            if not m:
                continue
            lo = max(0, m.start() - 40)
            hits.append({
                "line": lineno,
                "rule": rule,
                "hint": hint,
                "context": line[lo:m.end() + 40].strip(),
            })
            break          # one report per line is enough to act on
    return hits


def lint_file(path):
    return lint_text(Path(path).read_text(encoding="utf-8"))


def iter_targets(target):
    p = Path(target)
    if p.is_dir():
        return sorted(p.rglob("*.md"))
    return [p]


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        description="Flag math written without $…$ delimiters in report .md files.")
    ap.add_argument("targets", nargs="+", help="report .md file(s) or a directory")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    files = []
    for t in args.targets:
        files.extend(iter_targets(t))
    files = [f for f in files if f.is_file()]
    if not files:
        print("[ERROR] no .md file matched", file=sys.stderr)
        return 2

    report, total = {}, 0
    for f in files:
        hits = lint_file(f)
        if hits:
            report[str(f)] = hits
            total += len(hits)

    if args.json:
        print(json.dumps({"files": report, "total": total}, ensure_ascii=False,
                         indent=2))
    elif total:
        print(f"[FAIL] {total} bare-math hit(s) in {len(report)} file(s) — "
              f"these render as raw text in BOTH the .md and the .html\n")
        for f, hits in report.items():
            print(f"{f}")
            for h in hits:
                print(f"  L{h['line']:>4} [{h['rule']:11s}] {h['context']}")
                print(f"        → {h['hint']}")
    else:
        print(f"[OK] no bare math in {len(files)} file(s)")

    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
