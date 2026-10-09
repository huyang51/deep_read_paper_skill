#!/usr/bin/env python3
"""verify_math.py — does every formula actually DISPLAY, in both views?

A report is read in two places and they use different math engines:

    reports/*.md   Obsidian  → MathJax  (the vault is the primary reading view)
    html/*.html    browser   → KaTeX    (rendered by tools/render_report.py)

lint_math.py answers a different question ("is a formula missing its `$`
delimiters?"). This tool answers "given the delimiters are there, will the
reader SEE a formula?" — and those two questions have different failure modes,
which is how ``\\tag{1}`` survived a clean lint, a clean KaTeX run, and a
MathJax run with all packages enabled, while still being broken in Obsidian.

Checks
------
1. delimiter pairing (pure python, always on)
   Applies Obsidian's inline rule — opening `$` followed by a non-space,
   closing `$` preceded by a non-space and not followed by a digit. Any `$`
   that fails to pair is shown to the reader as a literal dollar.

2. Obsidian-hostile commands (pure python, always on)
   Obsidian's bundled MathJax does not enable AMS equation tags, so `\\tag`
   raises "Undefined control sequence"; `\\label`/`\\eqref` are reported to
   break the equation too. These are deny-listed rather than discovered,
   because discovering them costs a MathJax dependency — see check 3.

3. full MathJax render (optional; needs `mathjax-full`)
   The real oracle for the Obsidian view. Renders every formula with the
   DEFAULT package set (the pessimistic config): anything that needs an
   optional package fails here, which is the point. Point --mathjax-dir at a
   directory containing node_modules/mathjax-full, or `npm i mathjax-full`.

4. KaTeX render of html/*.html (optional; needs node + the fetched dist)
   The oracle for the HTML view, using the page's own options. Only runs when
   html/ sits next to reports/ (or --html-dir is given).

Usage
-----
    python tools/verify_math.py <vault>/reports/
    python tools/verify_math.py <vault>/reports/ --mathjax-dir /tmp/mj
    python tools/verify_math.py reports/ --html-dir html/ --json

Exit codes: 0 all clear | 1 a formula would not display | 2 bad arguments
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_FENCE_RE = re.compile(r"^([ \t]*)(```+|~~~+)[^\n]*\n.*?^\1\2[ \t]*$",
                       re.DOTALL | re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")

# Commands Obsidian's MathJax does not provide out of the box. Kept explicit
# and justified rather than pattern-guessed:
#   \tag/\label/\eqref/\ref — AMS equation numbering; MathJax needs
#     tex:{tags:'ams'} for \tag and the tagformat extension for \eqref, and
#     Obsidian does not enable them (forum.obsidian.md/t/3189, /t/4899).
#   \bm — needs the boldsymbol package; use \boldsymbol instead.
OBSIDIAN_HOSTILE = [
    (re.compile(r"\\tag\b"), r"\tag", r"use \qquad(N) and write 式(N) in prose"),
    (re.compile(r"\\label\b"), r"\label", r"drop it; cite the equation in prose"),
    (re.compile(r"\\eqref\b"), r"\eqref", r"cite the equation in prose"),
    (re.compile(r"(?<![A-Za-z])\\ref\b"), r"\ref", r"cite the equation in prose"),
    (re.compile(r"\\bm\s*\{"), r"\bm", r"use \boldsymbol{…}"),
]


def strip_nonmath_regions(text):
    """Blank code fences/inline code, preserving newlines for line numbers."""
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    text = _FENCE_RE.sub(blank, text)
    return _INLINE_CODE_RE.sub(blank, text)


def extract_formulas(text):
    """[(display, tex, offset)] using Obsidian's delimiter rules, plus
    [(offset, context)] for every `$` that would NOT pair."""
    s = strip_nonmath_regions(text)
    spans, orphans = [], []
    i, n = 0, len(s)
    while i < n:
        if s[i] != "$":
            i += 1
            continue
        if s.startswith("$$", i):
            j = s.find("$$", i + 2)
            if j == -1:
                orphans.append((i, s[i:i + 60].replace("\n", " ")))
                i += 2
                continue
            spans.append((True, s[i + 2:j], i))
            i = j + 2
            continue
        if i + 1 < n and not s[i + 1].isspace():
            j = i + 1
            while j < n and s[j] not in "$\n":
                j += 1
            if (j < n and s[j] == "$" and not s[j - 1].isspace()
                    and not (j + 1 < n and s[j + 1].isdigit())):
                spans.append((False, s[i + 1:j], i))
                i = j + 1
                continue
        orphans.append((i, s[max(0, i - 50):i + 50].replace("\n", " ")))
        i += 1
    return spans, orphans


def find_table_math_pipes(text):
    """A bare `|` inside `$…$` on a TABLE ROW splits the cell in Obsidian.

    `|` is the table column separator and Obsidian has no equivalent of the
    HTML renderer's protect_table_pipes(); the parser runs before MathJax, so
    the row is cut mid-formula and the table comes out misaligned. Known,
    long-standing bug — the documented workaround is `\\vert` (or
    `\\lvert`/`\\rvert`), which renders the identical glyph.
    An escaped `\\|` is fine: that is the standard table-cell escape (and in
    TeX it is the norm bar, which is usually what was meant anyway)."""
    hits = []
    for lineno, line in enumerate(strip_nonmath_regions(text).split("\n"), 1):
        if not line.strip().startswith("|"):
            continue
        for m in re.finditer(r"\$[^$\n]+?\$", line):
            if re.search(r"(?<!\\)\|", m.group(0)):
                hits.append({"line": lineno, "tex": m.group(0),
                             "fix": "把 | 换成 \\vert（或 \\lvert…\\rvert）"})
    return hits


def check_file(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    spans, orphans = extract_formulas(text)
    hostile = []
    for lineno, line in enumerate(strip_nonmath_regions(text).split("\n"), 1):
        for rx, name, fix in OBSIDIAN_HOSTILE:
            if rx.search(line):
                hostile.append({"line": lineno, "cmd": name, "fix": fix})
    return {
        # keep the display flag: a block formula validated as inline is a
        # different (and wrong) question — display-only constructs would be
        # reported as failures that never occur on the page.
        "formula_list": [{"tex": t, "display": d} for d, t, _ in spans],
        "formulas": [t for _, t, _ in spans],
        "orphan_dollars": [c for _, c in orphans],
        "hostile": hostile,
        "table_pipes": find_table_math_pipes(text),
    }


# --------------------------------------------------------------------------
# optional oracles
# --------------------------------------------------------------------------

_MJ_JS = r"""
const fs=require('fs');
const B=process.env.MJ_DIR;
const {mathjax}=require(B+'/mathjax-full/js/mathjax.js');
const {TeX}=require(B+'/mathjax-full/js/input/tex.js');
const {SVG}=require(B+'/mathjax-full/js/output/svg.js');
const {liteAdaptor}=require(B+'/mathjax-full/js/adaptors/liteAdaptor.js');
const {RegisterHTMLHandler}=require(B+'/mathjax-full/js/handlers/html.js');
const a=liteAdaptor();RegisterHTMLHandler(a);
// DEFAULT package set on purpose: the pessimistic config. Anything needing an
// optional package fails here, which is what we want to know.
const doc=mathjax.document('',{InputJax:new TeX({}),OutputJax:new SVG({fontCache:'none'})});
const data=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
let bad=0;
for(const [file,list] of Object.entries(data)){
  for(const item of list){
    try{
      const out=a.outerHTML(doc.convert(item.tex,{display:item.display,em:16,ex:8,containerWidth:80*16}));
      const e=out.match(/data-mjx-error="([^"]*)"/);
      if(e){bad++;console.log(JSON.stringify({file,tex:item.tex.slice(0,70),err:e[1]}));}
    }catch(e){bad++;console.log(JSON.stringify({file,tex:item.tex.slice(0,70),err:e.message.split('\n')[0]}));}
  }
}
process.exit(bad?1:0);
"""


def run_mathjax(payload, mathjax_dir):
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        js = Path(td) / "mj.js"
        data = Path(td) / "in.json"
        js.write_text(_MJ_JS, encoding="utf-8")
        data.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        env = dict(os.environ, MJ_DIR=mathjax_dir)
        p = subprocess.run(["node", str(js), str(data)], capture_output=True,
                           text=True, env=env)
        return p


def find_mathjax(explicit):
    """Return the directory that CONTAINS `mathjax-full` (i.e. node_modules).

    Accepts either a project dir (`…/proj` with node_modules/mathjax-full) or
    the node_modules dir itself, since users pass whichever they have."""
    cands = [explicit] if explicit else []
    cands += [os.environ.get("MATHJAX_DIR"), "/tmp/mjtest", str(Path.cwd()),
              str(Path(__file__).resolve().parent.parent)]
    for c in cands:
        if not c:
            continue
        if Path(c, "node_modules", "mathjax-full").is_dir():
            return str(Path(c, "node_modules"))
        if Path(c, "mathjax-full").is_dir():
            return c
    return None


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        description="Verify formulas will DISPLAY in Obsidian (MathJax) and HTML (KaTeX).")
    ap.add_argument("reports", help="reports directory or a single .md")
    ap.add_argument("--html-dir", default=None,
                    help="html/ directory for the KaTeX check (default: sibling of reports)")
    ap.add_argument("--mathjax-dir", default=None,
                    help="dir containing node_modules/mathjax-full for the full render check")
    ap.add_argument("--katex-dir", default=None, help="KaTeX dist (default: skill cache)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    target = Path(args.reports)
    files = sorted(target.rglob("*.md")) if target.is_dir() else [target]
    if not files:
        print("[ERROR] no .md matched", file=sys.stderr)
        return 2

    report, payload = {}, {}
    for f in files:
        r = check_file(f)
        report[str(f)] = r
        if r["formula_list"]:
            payload[str(f)] = r["formula_list"]

    orphan_total = sum(len(r["orphan_dollars"]) for r in report.values())
    hostile_total = sum(len(r["hostile"]) for r in report.values())
    pipe_total = sum(len(r["table_pipes"]) for r in report.values())
    mj_fails, katex_fails, notes = [], [], []

    n_formulas = sum(len(r["formulas"]) for r in report.values())

    # --- MathJax (Obsidian view) ---
    mj_dir = find_mathjax(args.mathjax_dir)
    if mj_dir and payload:
        p = run_mathjax(payload, mj_dir)
        for line in p.stdout.strip().split("\n"):
            if line.strip():
                try:
                    mj_fails.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        if p.returncode not in (0, 1):
            notes.append("MathJax 运行异常：" + (p.stderr.strip()[-160:] or "?"))
    else:
        notes.append("跳过 MathJax 全量渲染（未找到 mathjax-full）——"
                     "运行 `npm i mathjax-full` 后用 --mathjax-dir 指向该目录可启用")

    # --- KaTeX (HTML view) ---
    html_dir = Path(args.html_dir) if args.html_dir else (
        target.parent.parent / "html" if target.is_dir() else None)
    if html_dir and html_dir.is_dir():
        n_span = 0
        for h in sorted(html_dir.glob("*.html")):
            txt = h.read_text(encoding="utf-8", errors="replace")
            n_span += len(re.findall(r'class="math-(?:inline|block)"', txt))
            for rx, name in ((r"cdn\.jsdelivr\.net|unpkg\.com|cdn\.staticfile\.org|registry\.npmmirror\.com", "CDN"),
                             (r'<span class="math-(?:inline|block)">[^<]*\\(?:tag|label|eqref)\b', "Obsidian 敌对命令")):
                if re.search(rx, txt):
                    notes.append(f"{h.name} 命中 {name}")
        if n_span:
            notes.append(f"html/ 共 {n_span} 个 math span（KaTeX 校验见 render_report --embed-katex）")

    ok = not (orphan_total or hostile_total or pipe_total or mj_fails or katex_fails)

    if args.json:
        print(json.dumps({"ok": ok, "formulas": n_formulas,
                          "orphan_dollars": orphan_total, "hostile": hostile_total,
                          "table_pipes": pipe_total,
                          "mathjax_failures": mj_fails, "notes": notes},
                         ensure_ascii=False, indent=2))
    else:
        print(f"公式总数: {n_formulas}")
        if orphan_total:
            print(f"\n[FAIL] {orphan_total} 个 `$` 无法配对 —— Obsidian 里会显示成字面量")
            for f, r in report.items():
                for c in r["orphan_dollars"][:5]:
                    print(f"  {Path(f).name}: …{c}…")
        if hostile_total:
            print(f"\n[FAIL] {hostile_total} 处 Obsidian 不支持的编号类命令")
            for f, r in report.items():
                for h in r["hostile"][:5]:
                    print(f"  {Path(f).name} L{h['line']}: {h['cmd']} → {h['fix']}")
        if pipe_total:
            print(f"\n[FAIL] {pipe_total} 处表格单元格里的公式含裸 `|` —— "
                  f"Obsidian 会把它当列分隔符，撑破表格")
            for f, r in report.items():
                for h in r["table_pipes"][:5]:
                    print(f"  {Path(f).name} L{h['line']}: {h['tex']}  →  {h['fix']}")
        if mj_fails:
            print(f"\n[FAIL] MathJax（默认配置）渲染失败 {len(mj_fails)} 条")
            for e in mj_fails[:8]:
                print(f"  {Path(e['file']).name}: {e['tex']!r}\n    -> {e['err']}")
        for n in notes:
            print(f"[note] {n}")
        if ok:
            print("\n[OK] 两种阅读视图的公式均可正常显示")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
