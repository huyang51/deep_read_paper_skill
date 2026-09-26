#!/usr/bin/env python3
"""render_report.py — turn a finished Markdown reading report into a polished,
standalone HTML reading view.

Design references (open-source, studied rather than copied wholesale):
- Tufte CSS (edwardtufte/tufte-css): serif body discipline, generous leading,
  booktabs-style tables (horizontal rules only — the signature academic look).
- VitePress default theme: sticky sidebar with active-section highlight,
  refined dual palette.
- pangu / 中文文案排版指北: automatic thin space between CJK and Latin/numerals.
- distill.pub: figure presentation (click-to-zoom, figure-first reading).
Plus the Chinese publication convention: sans headings + serif body.

Hard constraints (do not change casually):
- Output HTML sits NEXT TO the md (same dir, .html) so the report's relative
  image paths (../attachments/<short>/x.png) stay valid — zero asset copying.
- Deterministic conversion via Python-Markdown; the LLM never hand-writes HTML.
- All UI JavaScript is inline vanilla (active-section, progress bar, zoom,
  mobile TOC) — works fully offline. KaTeX via CDN is the ONLY external
  resource, and --offline drops it cleanly (formulas show as source).
- LaTeX stays as raw $...$/$$...$$ text for in-browser rendering.
- The md file is the single source of truth: re-render after ANY report edit.

Usage:
  python tools/render_report.py --md "<vault>/reports/ReT_解读报告.md"
  python tools/render_report.py --md <file.md> --out <file.html> --offline

Exit codes: 0 ok | 1 bad input / conversion error.
"""
import argparse
import html as html_mod
import re
import sys
from datetime import datetime
from pathlib import Path

import frontmatter
import markdown

MD_EXTENSIONS = ["tables", "fenced_code", "sane_lists", "attr_list"]
TS = chr(0x2009)  # thin space — built programmatically so no invisible
                  # literals ever enter this source file

KATEX_CDN = """<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"
  onload="renderMathInElement(document.body,{delimiters:[{left:'$$',right:'$$',display:true},{left:'$',right:'$',display:false}],throwOnError:false});"></script>"""

_CSS = """
:root{color-scheme:light dark;
  --fg:#26282d;--fg-dim:#5d6570;--bg:#f7f6f2;--panel:#ffffff;--accent:#33517d;
  --rule:#e0dcd2;--rule-soft:#eae7de;--code-bg:#f0eee7;--badge-bg:#e7edf6;
  --badge-fg:#33517d;--quote-bg:#fbfaf6;--warn:#b26a00;--ok:#2e7d32;
  --tw-sh:rgba(70,60,40,.06)}
@media (prefers-color-scheme: dark){:root{
  --fg:#d9dce2;--fg-dim:#98a1ad;--bg:#171a20;--panel:#1e222a;--accent:#8fb1de;
  --rule:#323743;--rule-soft:#2a2f39;--code-bg:#232733;--badge-bg:#2a3a52;
  --badge-fg:#a8c6ea;--quote-bg:#1c2027;--warn:#d29a4a;--ok:#7cc47f;
  --tw-sh:rgba(0,0,0,.35)}}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--fg);
  font:17px/1.85 Georgia,"Times New Roman","Source Han Serif SC","Noto Serif SC",
  "Songti SC","SimSun",serif}
#progress{position:fixed;top:0;left:0;height:3px;width:0;background:var(--accent);
  z-index:50;transition:width .1s linear}
.layout{display:flex;max-width:1320px;margin:0 auto}
nav.toc{width:275px;flex:0 0 275px;position:sticky;top:0;max-height:100vh;
  overflow:auto;padding:30px 6px 40px 18px;font-size:13.5px;
  font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  border-right:1px solid var(--rule-soft)}
nav.toc .toc-title{font-weight:700;padding:4px 10px 12px;letter-spacing:.12em;
  color:var(--fg-dim);font-size:12px}
nav.toc a{display:block;color:var(--fg-dim);text-decoration:none;
  padding:3px 10px;border-left:2px solid transparent;border-radius:0 6px 6px 0}
nav.toc a:hover{color:var(--accent);background:var(--panel)}
nav.toc a.active{color:var(--accent);border-left-color:var(--accent);
  background:var(--panel);font-weight:600}
nav.toc a.l3{padding-left:24px;font-size:12.5px}
main{flex:1;min-width:0;padding:40px 52px 100px}
article{max-width:46em}
header.masthead{max-width:46em;border-bottom:1px solid var(--rule);
  padding-bottom:22px;margin-bottom:30px}
header.masthead .kicker{font-family:-apple-system,"Segoe UI","PingFang SC",
  "Microsoft YaHei",sans-serif;font-size:12px;letter-spacing:.25em;
  color:var(--fg-dim);text-transform:uppercase}
header.masthead h1{font-family:-apple-system,"Segoe UI","PingFang SC",
  "Microsoft YaHei",sans-serif;font-size:31px;line-height:1.4;
  margin:10px 0 6px;font-weight:700;text-wrap:balance;letter-spacing:.01em}
.badges{margin:10px 0 2px}
.badge{display:inline-block;background:var(--badge-bg);color:var(--badge-fg);
  border-radius:999px;padding:2px 12px;font-size:12px;margin:2px 6px 2px 0;
  font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.meta-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));
  gap:3px 24px;font-size:13.5px;color:var(--fg-dim);margin-top:12px}
.meta-grid b{color:var(--fg);font-weight:600}
h2{font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  font-size:23px;font-weight:700;margin:52px 0 16px;padding-bottom:8px;
  border-bottom:1px solid var(--rule);letter-spacing:.02em;text-wrap:balance}
h3{font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  font-size:18px;font-weight:700;margin:34px 0 12px;color:var(--accent);
  text-wrap:balance}
h2 .secmark{opacity:.4;font-weight:400;margin-right:9px;font-size:.8em}
h2.sec-warn{border-bottom-color:var(--warn);color:var(--warn)}
h2.sec-ok{border-bottom-color:var(--ok);color:var(--ok)}
a.para{opacity:0;margin-left:8px;color:var(--accent);text-decoration:none;
  font-size:.75em}
h2:hover a.para,h3:hover a.para{opacity:.55}
p{margin:.9em 0}
ul,ol{padding-left:1.5em;margin:.8em 0}
li{margin:.3em 0}
blockquote{margin:1.1em 0;padding:8px 18px;background:var(--quote-bg);
  color:var(--fg-dim);border-left:3px solid var(--accent);border-radius:0 8px 8px 0}
blockquote p{margin:.4em 0}
code{font:.85em ui-monospace,SFMono-Regular,"Cascadia Code","JetBrains Mono",
  Consolas,monospace;background:var(--code-bg);padding:1.5px 5px;border-radius:5px}
pre{background:var(--code-bg);padding:14px 16px;border-radius:10px;
  overflow:auto;line-height:1.55;border:1px solid var(--rule-soft)}
pre code{background:none;padding:0;font-size:13.5px}
/* booktabs-style tables (Tufte CSS): horizontals only, no vertical rules */
.tw{overflow-x:auto;margin:1.3em 0;border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:14px;
  font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  line-height:1.55}
th,td{border:none;border-bottom:1px solid var(--rule-soft);padding:8px 11px;
  text-align:left;vertical-align:top}
thead th{border-top:2px solid var(--fg);border-bottom:1px solid var(--fg);
  font-weight:700;white-space:nowrap}
tbody tr:hover td{background:var(--quote-bg)}
td code{font-size:12.5px}
figure{margin:1.6em 0;text-align:center}
figure img{margin:0 auto 8px}
figcaption{font-size:13px;color:var(--fg-dim);text-align:left;
  font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  padding-left:4px}
img{max-width:100%;height:auto;display:block;border:1px solid var(--rule);
  border-radius:8px;background:#fff;cursor:zoom-in;
  box-shadow:0 2px 10px var(--tw-sh)}
@media (prefers-color-scheme: dark){img{background:#23252b;filter:brightness(.94)}}
.math-block{overflow-x:auto;overflow-y:hidden;padding:6px 0;text-align:center}
.wikilink{border-bottom:1px dashed var(--accent);color:var(--accent);
  font-style:italic}
#zoom{position:fixed;inset:0;background:rgba(10,10,12,.86);display:none;
  z-index:80;align-items:center;justify-content:center;cursor:zoom-out}
#zoom img{max-width:96vw;max-height:94vh;margin:0;border:none;
  box-shadow:0 8px 40px rgba(0,0,0,.6);cursor:zoom-out;background:#fff}
#toc-fab{display:none;position:fixed;right:18px;bottom:18px;z-index:60;
  background:var(--accent);color:#fff;border:none;border-radius:999px;
  width:46px;height:46px;font-size:20px;box-shadow:0 4px 14px rgba(0,0,0,.3);
  cursor:pointer}
footer.build{max-width:46em;margin-top:70px;padding-top:14px;
  border-top:1px solid var(--rule);font-size:12.5px;color:var(--fg-dim)}
@media (max-width:1120px){
  nav.toc{position:fixed;left:0;top:0;bottom:0;z-index:70;background:var(--bg);
    transform:translateX(-105%);transition:transform .25s ease;
    box-shadow:4px 0 20px rgba(0,0,0,.15);width:290px}
  nav.toc.open{transform:translateX(0)}
  #toc-fab{display:block}
  main{padding:30px 22px 80px}
  body{font-size:16px}
}
@media print{#progress,nav.toc,#toc-fab,footer.build,#zoom{display:none}
  body{background:#fff;color:#111}main{padding:0}article{max-width:100%}
  img,table,blockquote,pre{break-inside:avoid}h2{break-after:avoid}}
"""

_JS = """
(function(){var p=document.getElementById('progress'),h=document.documentElement;
function up(){var d=h.scrollHeight-h.clientHeight;
p.style.width=(d>0?(h.scrollTop||document.body.scrollTop)/d*100:0)+'%';}
addEventListener('scroll',up,{passive:true});up();
var toc=document.querySelector('nav.toc');
var fab=document.getElementById('toc-fab');
if(fab)fab.onclick=function(){toc.classList.toggle('open')};
var links=[].slice.call(toc.querySelectorAll('a[href^="#"]')),secs=[];
links.forEach(function(a){var el=document.getElementById(a.href.split('#')[1]);
if(el)secs.push([el,a]);});
if('IntersectionObserver' in window){
 var io=new IntersectionObserver(function(es){es.forEach(function(e){
  if(e.isIntersecting){links.forEach(function(l){l.classList.remove('active')});
   var s=secs.find(function(x){return x[0]===e.target});
   if(s)s[1].classList.add('active');}});},{rootMargin:'-10% 0px -75% 0px'});
 secs.forEach(function(x){io.observe(x[0]);});}
var z=document.getElementById('zoom');
document.querySelectorAll('article img').forEach(function(im){
 im.onclick=function(){z.firstElementChild.src=im.src;z.style.display='flex';};});
z.onclick=function(){z.style.display='none';};
addEventListener('keydown',function(e){if(e.key==='Escape')z.style.display='none';});
})();
"""

_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
{katex}
</head>
<body>
<div id="progress" aria-hidden="true"></div>
<div class="layout">
<nav class="toc" aria-label="目录">{toc}</nav>
<main>
<header class="masthead">
<div class="kicker">论文精读报告 · DEEP PAPER READING REPORT</div>
{badges}
<h1>{title}</h1>
{meta}
</header>
<article>{body}</article>
<footer class="build">本页由 <code>tools/render_report.py</code> 从 <code>{source}</code> 确定性生成 · {stamp} · md 为唯一事实源，修订后请重新渲染；公式渲染需联网加载 KaTeX</footer>
</main>
</div>
<div id="zoom"><img alt=""></div>
<button id="toc-fab" aria-label="目录">☰</button>
<script>{js}</script>
</body>
</html>
"""

_BLOCK_MATH = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_INLINE_MATH = re.compile(r"(?<!\\)\$(?!\s)([^$\n]+?)(?<!\s)\$")
_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_HEADING = re.compile(r"<h([23])>(.*?)</h\1>", re.DOTALL)
_CODEBLOCK = re.compile(r"<pre>.*?</pre>", re.DOTALL)
_IMG_P = re.compile(r'<p><img alt="([^"]*)" src="([^"]+)"></p>')
# pangu char classes — ASCII escapes only, never literal glyphs here.
_CJK = "\u2e80-\u312f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_LAT = "A-Za-z0-9\u03b1-\u03c9\u2032\u2019$%\u2208\u22c8\u2248\u2265\u2264\u00d7\u00f7\u2211"
_P2A = re.compile("([" + _CJK + "])(?=[" + _LAT + "])")
_P2B = re.compile("(?<=[" + _LAT + "])([" + _CJK + "])")


def _pangu(html_text):
    """Thin-space CJK<->Latin boundaries in text nodes; skip code/pre,
    tags, math bodies. Math tokens get boundary space so formulas never
    glue onto Chinese glyphs."""
    out = []
    pos = 0
    for code in _CODEBLOCK.finditer(html_text):
        out.append(_pangu_text(html_text[pos:code.start()]))
        out.append(code.group(0))
        pos = code.end()
    out.append(_pangu_text(html_text[pos:]))
    return "".join(out)


def _pangu_text(chunk):
    parts = re.split(r"(<code>.*?</code>|<pre>.*?</pre>|<[^>]+>|\$[^$\n]+\$)",
                     chunk, flags=re.DOTALL)
    for i, p in enumerate(parts):
        if not p:
            continue
        if p.startswith("<"):                       # tag or whole code span
            continue
        if p.startswith("$"):                       # inline math token
            prev = parts[i - 1] if i else ""
            nxt = parts[i + 1] if i + 1 < len(parts) else ""
            if prev and re.search("[" + _CJK + "]$", prev):
                p = TS + p
            if nxt and re.match("[" + _CJK + "]", nxt):
                p = p + TS
            parts[i] = p
            continue
        p = _P2A.sub(lambda m: m.group(1) + TS, p)
        parts[i] = _P2B.sub(lambda m: TS + m.group(1), p)
    return "".join(parts)


def wrap_tables(html_text):
    return (html_text.replace("<table>", '<div class="tw"><table>')
            .replace("</table>", "</table></div>"))


def figures_to_figures(html_text):
    """Wrap converted <p><img></p> standalone images into <figure> with a
    figcaption taken from alt (distill.pub style). Runs AFTER markdown."""
    def rep(m):
        alt, src = m.group(1), m.group(2)
        cap = f"<figcaption>{alt}</figcaption>" if alt else ""
        return f'<figure><img alt="{alt}" src="{src}">{cap}</figure>'
    html_text = _IMG_P.sub(rep, html_text)
    # block math restored inside a <p>: unwrap for valid nesting
    html_text = html_text.replace('<p><div class="math-block">',
                                  '<div class="math-block">')
    html_text = html_text.replace('</div></p>', '</div>')
    return html_text


def protect_math(text):
    store = {}

    def _stash(kind, body, i):
        key = "\x00MATH%04d%s\x00" % (i, kind)
        store[key] = (kind, body)
        return key

    text = _BLOCK_MATH.sub(lambda m: _stash("b", m.group(1), len(store)), text)
    return _INLINE_MATH.sub(lambda m: _stash("i", m.group(1), len(store)), text), store


def restore_math(html_text, store):
    for key, (kind, body) in store.items():
        raw = html_mod.escape(body)
        repl = ('<div class="math-block">$$' + raw + '$$</div>'
                if kind == "b" else "$" + raw + "$")
        html_text = html_text.replace(key, repl)
    return html_text


def wikilinks_to_spans(text):
    return _WIKILINK.sub(
        lambda m: '<span class="wikilink">'
        + html_mod.escape(m.group(2) or m.group(1)) + "</span>",
        text)


def build_toc_and_ids(body_html):
    entries = []
    counter = {"n": 0}

    def repl(m):
        level, inner = m.group(1), m.group(2)
        counter["n"] += 1
        sid = "s%d" % counter["n"]
        plain = re.sub(r"<[^>]+>", "", inner).strip()
        clean = re.sub(r"[━]+", "", plain).strip(" ·")
        cls = ""
        if "⚠" in plain:
            cls = ' class="sec-warn"'
        elif "✅" in plain or "验收" in plain:
            cls = ' class="sec-ok"'
        entries.append((int(level), sid, clean))
        para = '<a class="para" href="#%s" aria-label="锚点">¶</a>' % sid
        return '<h%s id="%s"%s><span class="secmark">§</span>%s%s</h%s>' % (
            level, sid, cls, inner, para, level)

    new_body = _HEADING.sub(repl, body_html)
    parts = ['<div class="toc-title">目录 CONTENTS</div>']
    for level, sid, t in entries:
        parts.append('<a class="l%d" href="#%s">%s</a>' % (level, sid, html_mod.escape(t)))
    return "".join(parts), new_body


def reading_minutes(md_text):
    cjk = len(re.findall("[" + _CJK + "]", md_text))
    words = len(re.findall(r"[A-Za-z]+", md_text))
    return max(1, round(cjk / 400 + words / 260))


def meta_header(post, minutes):
    d = post.metadata or {}
    title = str(d.get("title") or post.get("title") or "论文解读报告")
    badges = []
    if d.get("read_mode"):
        badges.append('<span class="badge">档位 %s</span>'
                      % html_mod.escape(str(d["read_mode"])))
    if d.get("novelty_level"):
        badges.append('<span class="badge">新颖性 %s</span>'
                      % html_mod.escape(str(d["novelty_level"])))
    badges.append('<span class="badge">阅读约 %d 分钟</span>' % minutes)
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
        rows.append("<div><b>%s：</b>%s</div>" % (label, html_mod.escape(str(val))))
    meta = '<div class="meta-grid">%s</div>' % "".join(rows) if rows else ""
    return '<div class="badges">%s</div>' % "".join(badges), meta, title


def render_report(md_path, out_path=None, offline=False):
    md_path = Path(md_path)
    post = frontmatter.load(str(md_path))
    body_md = post.content

    guarded, store = protect_math(body_md)
    guarded = wikilinks_to_spans(guarded)
    body_html = markdown.markdown(guarded, extensions=MD_EXTENSIONS,
                                  output_format="html")
    body_html = restore_math(body_html, store)
    body_html = figures_to_figures(body_html)
    body_html = wrap_tables(body_html)
    body_html = _pangu(body_html)
    toc, body_html = build_toc_and_ids(body_html)
    badges, meta, title = meta_header(post, reading_minutes(body_md))

    out_path = Path(out_path) if out_path else md_path.with_suffix(".html")
    page = _TEMPLATE.format(
        title=html_mod.escape(title),
        css=_CSS,
        js=_JS,
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
                                             "to a standalone HTML reading view.")
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
