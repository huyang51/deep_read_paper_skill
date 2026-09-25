#!/usr/bin/env python3
"""render_report.py — turn a finished Markdown reading report into a polished,
self-contained HTML reading view.

Design decisions (do not casually change):
- Output HTML is written NEXT TO the md file (same directory, .html suffix),
  so the report's relative image paths (../attachments/<short>/x.png) stay
  valid without copying any asset.
- Deterministic conversion (Python-Markdown) — the LLM never hand-writes HTML.
- LaTeX stays as raw $...$/$$...$$ text in the HTML and is rendered in the
  browser by KaTeX (CDN). Pass --offline to skip CDN entirely (formulas then
  display as source text; everything else still works).
- YAML frontmatter becomes a metadata card in the header.
- [[wikilinks]] render as styled plain spans (no broken links in a
  standalone HTML file).
- The md file is the single source of truth: re-run this tool after ANY
  report edit; the HTML is a build artifact, never hand-edit it.

Usage:
  python tools/render_report.py --md "<vault>/reports/ReT_解读报告.md"
  python tools/render_report.py --md <file.md> --out <file.html> --offline

Exit codes: 0 ok | 1 bad input / conversion error.
"""
import argparse
import html as html_mod
import re
import sys
from pathlib import Path

import frontmatter
import markdown

MD_EXTENSIONS = ["tables", "fenced_code", "sane_lists", "attr_list"]

KATEX_CDN = """<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"
  onload="renderMathInElement(document.body,{delimiters:[{left:'$$',right:'$$',display:true},{left:'$',right:'$',display:false}]});"></script>"""

CSS = """
:root{--fg:#1f2430;--fg-dim:#5b6472;--bg:#f7f6f3;--panel:#fffdf9;--accent:#35507a;
--rule:#e3ded4;--code-bg:#efede7;--badge-bg:#e8eef7;--badge-fg:#35507a;
--warn-bg:#fdf3e7;--warn-edge:#e0b26a;}
@media (prefers-color-scheme: dark){:root{--fg:#d8dbe2;--fg-dim:#9aa1ac;--bg:#191b20;
--panel:#20232a;--accent:#8fb0dd;--rule:#333742;--code-bg:#262a32;
--badge-bg:#2b3a52;--badge-fg:#a9c4e8;--warn-bg:#332a1c;--warn-edge:#8a6a32;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.75 "Georgia","Times New Roman","Source Han Serif SC","Noto Serif CJK SC","SimSun",serif;}
.layout{display:flex;max-width:1280px;margin:0 auto;}
nav.toc{width:270px;flex:0 0 270px;position:sticky;top:0;max-height:100vh;overflow:auto;
padding:28px 8px 40px 20px;font-size:13px;border-right:1px solid var(--rule);}
nav.toc a{display:block;color:var(--fg-dim);text-decoration:none;padding:3px 8px;border-radius:6px;}
nav.toc a:hover{background:var(--panel);color:var(--accent);}
nav.toc a.l3{padding-left:22px;font-size:12px;}
main{flex:1;min-width:0;padding:36px 48px 90px;}
article{max-width:880px;}
header.masthead{border-bottom:3px solid var(--accent);padding-bottom:18px;margin-bottom:26px;}
header.masthead h1{font-size:30px;line-height:1.35;margin:6px 0 4px;}
.meta-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:4px 22px;
font-size:13.5px;color:var(--fg-dim);margin-top:10px;}
.meta-grid b{color:var(--fg);font-weight:600;}
.badge{display:inline-block;background:var(--badge-bg);color:var(--badge-fg);
border-radius:999px;padding:1px 11px;font-size:12px;margin-right:6px;}
h2{font-size:23px;margin:44px 0 14px;padding-bottom:7px;border-bottom:2px solid var(--rule);}
h3{font-size:18px;margin:30px 0 10px;color:var(--accent);}
h2 .secmark,h3 .secmark{opacity:.45;font-weight:400;margin-right:8px;}
p{margin:.85em 0;}
blockquote{margin:1em 0;padding:2px 16px;border-left:4px solid var(--accent);
background:var(--panel);color:var(--fg-dim);border-radius:0 8px 8px 0;}
table{border-collapse:collapse;width:100%;font-size:14px;margin:1.2em 0;
font-family:"Helvetica Neue",Arial,"PingFang SC","Microsoft YaHei",sans-serif;}
th,td{border:1px solid var(--rule);padding:7px 10px;text-align:left;vertical-align:top;}
th{background:var(--panel);}
tr:nth-child(even) td{background:rgba(0,0,0,.018);}
@media (prefers-color-scheme: dark){tr:nth-child(even) td{background:rgba(255,255,255,.03);}}
code{background:var(--code-bg);padding:1px 5px;border-radius:5px;font-size:87%;}
pre{background:var(--code-bg);padding:14px 16px;border-radius:10px;overflow:auto;line-height:1.55;}
pre code{background:none;padding:0;}
img{max-width:100%;height:auto;display:block;margin:1.2em auto;
border:1px solid var(--rule);border-radius:8px;background:#fff;}
.wikilink{border-bottom:1px dashed var(--accent);color:var(--accent);font-style:italic;}
.math-block{overflow-x:auto;overflow-y:hidden;padding:4px 0;}
footer.build{margin-top:60px;padding-top:14px;border-top:1px solid var(--rule);
font-size:12.5px;color:var(--fg-dim);}
@media (max-width:1080px){nav.toc{display:none;}main{padding:26px 20px 70px;}}
@media print{nav.toc,footer.build{display:none;}body{background:#fff;}
img,table,blockquote,pre{break-inside:avoid;}}
"""

TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
{katex}
</head>
<body>
<div class="layout">
<nav class="toc" aria-label="目录">{toc}</nav>
<main>
<header class="masthead">
{badges}
<h1>{title}</h1>
{meta}
</header>
<article>{body}</article>
<footer class="build">本页面由 <code>tools/render_report.py</code> 从 Markdown 报告生成 ——
源文件 <code>{source}</code> · 生成时间 {stamp} · 数学公式渲染需联网加载 KaTeX（离线用 --offline 重新生成）</footer>
</main>
</div>
</body>
</html>
"""

_BLOCK_MATH = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_INLINE_MATH = re.compile(r"(?<!\\)\$(?!\s)([^$\n]+?)(?<!\s)\$")
_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_HEADING = re.compile(r"<h([23])>(.*?)</h\1>", re.DOTALL)


def protect_math(text):
    """Replace math spans with sentinels so the Markdown parser can't mangle
    them; returns (text, mapping)."""
    store = {}

    def _stash(kind, body, i):
        key = f"\x00MATH{i:04d}{kind}\x00"
        store[key] = (kind, body)
        return key

    def block_sub(m):
        i = len(store)
        return _stash("b", m.group(1), i)

    text = _BLOCK_MATH.sub(block_sub, text)

    def inline_sub(m):
        i = len(store)
        return _stash("i", m.group(1), i)

    return _INLINE_MATH.sub(inline_sub, text), store


def restore_math(html_text, store):
    for key, (kind, body) in store.items():
        raw = html_mod.escape(body)
        if kind == "b":
            repl = f'<div class="math-block">$${raw}$$</div>'
        else:
            repl = f"${raw}$"
        html_text = html_text.replace(key, repl)
    return html_text


def wikilinks_to_spans(text):
    return _WIKILINK.sub(
        lambda m: f'<span class="wikilink">{html_mod.escape(m.group(2) or m.group(1))}</span>',
        text)


def build_toc_and_ids(body_html):
    """Insert stable ids on h2/h3 and return (sidebar_html, new_body_html)."""
    entries = []
    counter = {"n": 0}

    def repl(m):
        level = m.group(1)
        inner = m.group(2)
        counter["n"] += 1
        sid = f"s{counter['n']}"
        plain = re.sub(r"<[^>]+>", "", inner).strip()
        entries.append((int(level), sid, plain))
        return f'<h{level} id="{sid}"><span class="secmark">§</span>{inner}</h{level}>'

    new_body = _HEADING.sub(repl, body_html)
    parts = []
    for i, (level, sid, title) in enumerate(entries):
        # section-divider h2s like "━━━ 1. xxx ━━━" stay but drop the ornaments
        clean = re.sub(r"[━]+", "", title).strip(" ·")
        parts.append(f'<a class="l{level}" href="#{sid}">{html_mod.escape(clean)}</a>')
    nav = ('<div style="font-weight:700;padding:4px 8px 10px">目录</div>'
           if parts else "") + "".join(parts)
    return nav, new_body


def meta_header(post):
    """Return (badges_html, meta_grid_html, page_title) from frontmatter."""
    d = post.metadata or {}
    title = str(d.get("title") or post.get("title") or "论文解读报告")
    short = str(d.get("short_name") or "")
    badges = []
    if d.get("read_mode"):
        badges.append(f'<span class="badge">档位 {html_mod.escape(str(d["read_mode"]))}</span>')
    if d.get("novelty_level"):
        badges.append(f'<span class="badge">新颖性 {html_mod.escape(str(d["novelty_level"]))}</span>')
    rows = []
    labels = {"authors": "作者", "venue": "发表于", "year": "年份",
              "core_contribution": "核心贡献", "date_read": "阅读日期",
              "method_category": "方法类别", "problem_domain": "问题领域"}
    for key, label in labels.items():
        val = d.get(key)
        if not val:
            continue
        if isinstance(val, list):
            val = "、".join(str(v) for v in val[:6]) + ("…" if len(val) > 6 else "")
        rows.append(f"<div><b>{label}：</b>{html_mod.escape(str(val))}</div>")
    meta = f'<div class="meta-grid">{"".join(rows)}</div>' if rows else ""
    badges_html = ("<div>" + "".join(badges) + "</div>") if badges else ""
    _ = short  # reserved for future breadcrumb
    return badges_html, meta, title


def render_report(md_path, out_path=None, offline=False):
    from datetime import datetime
    md_path = Path(md_path)
    post = frontmatter.load(str(md_path))
    body_md = post.content

    guarded, store = protect_math(body_md)
    guarded = wikilinks_to_spans(guarded)
    body_html = markdown.markdown(guarded, extensions=MD_EXTENSIONS,
                                  output_format="html")
    # keep sentinels untouched by markdown: \x00 survives, so restore after
    body_html = restore_math(body_html, store)
    toc, body_html = build_toc_and_ids(body_html)
    badges, meta, title = meta_header(post)

    out_path = Path(out_path) if out_path else md_path.with_suffix(".html")
    page = TEMPLATE.format(
        title=html_mod.escape(title),
        css=CSS,
        katex=("" if offline else KATEX_CDN),
        toc=toc,
        badges=badges,
        meta=meta,
        body=body_html,
        source=html_mod.escape(md_path.name),
        stamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    out_path.write_text(page, encoding="utf-8")
    return out_path


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Render a Markdown reading report "
                                             "to a standalone HTML view.")
    ap.add_argument("--md", required=True, help="report .md file (frontmatter ok)")
    ap.add_argument("--out", default=None, help="output .html (default: alongside md)")
    ap.add_argument("--offline", action="store_true",
                    help="omit KaTeX CDN (formulas stay as source text)")
    args = ap.parse_args(argv)
    if not Path(args.md).is_file():
        print(f"[ERROR] file not found: {args.md}")
        return 1
    try:
        out = render_report(args.md, args.out, offline=args.offline)
    except Exception as exc:
        print(f"[ERROR] {type(exc).__name__}: {exc}")
        return 1
    print(f"[OK] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
