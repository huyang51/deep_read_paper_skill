#!/usr/bin/env python3
"""render_report.py — turn a finished Markdown reading report into a polished,
standalone HTML reading view.

Design references (open-source, studied rather than copied wholesale):
- Tufte CSS (edwardtufte/tufte-css): serif body discipline, generous leading,
  booktabs-style tables (horizontal rules only — the signature academic look).
- VitePress default theme: sticky sidebar with active-section highlight, refined
  dual palette, code-block copy affordance.
- GitHub markdown-css: horizontally scrollable wide tables with a per-column
  floor width, task lists, and `> [!NOTE]`-style alerts.
- pangu / 中文文案排版指北: automatic thin space between CJK and Latin/numerals.
- pymdownx.arithmatex + KaTeX docs: math is *protected before* Markdown and
  emitted as explicit `data`-carrying elements, rendered by an explicit
  `katex.render()` call per element. No `$`-delimiter auto-render heuristics —
  that is what makes currency, `|` in table cells and `$ .. $` spacing break.
- KaTeX contrib: mhchem (chemistry) is loaded only when the report uses it.
- distill.pub: figure presentation (click-to-zoom, figure-first reading).
Plus the Chinese publication convention: sans headings + serif body.

Hard constraints (do not change casually):
- Output HTML sits NEXT TO the md (same dir, .html) so the report's relative
  image paths (../attachments/<short>/x.png) stay valid — zero asset copying.
- Deterministic conversion via Python-Markdown; the LLM never hand-writes HTML.
- All UI JavaScript is inline vanilla (active-section, progress bar, zoom,
  mobile TOC, copy buttons) — works fully offline.
- The md file is the single source of truth: re-render after ANY report edit.

Math delivery (`--katex`):
  cdn    (default) four-mirror fallback chain — jsDelivr, npmmirror（国内）,
         staticfile（国内）, unpkg. A blocked mirror degrades to the next one.
  embed  inline KaTeX (JS + CSS + woff2 fonts as data URIs) into the HTML from a
         local dist dir — self-contained file, no network at read time.
         Populate it once with:  --fetch-katex
  off    no KaTeX at all (`--offline`): formulas stay readable LaTeX source.
When every mirror is unreachable the page falls back to readable LaTeX source
instead of showing raw `$…$`, and says so in a one-line notice.

Usage:
  python tools/render_report.py --md "<vault>/reports/ReT_解读报告.md"
  python tools/render_report.py --md <file.md> --out <file.html> --offline
  python tools/render_report.py --md <file.md> --fetch-katex   # 一次性下载
  python tools/render_report.py --md <file.md> --embed-katex   # 自包含单文件

Exit codes: 0 ok | 1 bad input / conversion error.
"""
import argparse
import base64
import html as html_mod
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import frontmatter
import markdown

MD_EXTENSIONS = ["tables", "fenced_code", "sane_lists", "attr_list", "footnotes",
                 "md_in_html"]
TS = chr(0x2009)  # thin space — built programmatically so no invisible
                  # literals ever enter this source file

KATEX_VERSION = "0.16.11"
# mirror order: global CDN first, then the two China-friendly ones, then unpkg.
KATEX_BASES = (
    "https://cdn.jsdelivr.net/npm/katex@%s/dist" % KATEX_VERSION,
    "https://registry.npmmirror.com/katex/%s/files/dist" % KATEX_VERSION,
    "https://cdn.staticfile.org/KaTeX/%s" % KATEX_VERSION,
    "https://unpkg.com/katex@%s/dist" % KATEX_VERSION,
)
# LLM-written LaTeX uses these shorthands a lot; KaTeX has no such macros.
KATEX_MACROS = {
    "\\R": "\\mathbb{R}", "\\N": "\\mathbb{N}", "\\Z": "\\mathbb{Z}",
    "\\Q": "\\mathbb{Q}", "\\C": "\\mathbb{C}", "\\E": "\\mathbb{E}",
    "\\Var": "\\operatorname{Var}", "\\Cov": "\\operatorname{Cov}",
    "\\softmax": "\\operatorname{softmax}", "\\relu": "\\operatorname{ReLU}",
    "\\argmax": "\\operatorname*{arg\\,max}",
    "\\argmin": "\\operatorname*{arg\\,min}",
}


# --------------------------------------------------------------------------
# math: protect -> markdown -> restore as explicit elements
# --------------------------------------------------------------------------
_ENV_NAMES = (r"align\*?|aligned|alignat\*?|equation\*?|gather\*?|gathered|"
              r"multline\*?|cases|dcases|rcases|array|split|"
              r"[bpBvV]?matrix|smallmatrix|subarray")
_BLOCK_DOLLAR = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_ENV_BLOCK = re.compile(r"(\\begin\{(%s)\}.*?\\end\{\2\})" % _ENV_NAMES,
                        re.DOTALL)
_PAREN_BLOCK = re.compile(r"\\\[(.+?)\\\]", re.DOTALL)
_PAREN_INLINE = re.compile(r"\\\((.+?)\\\)", re.DOTALL)
_INLINE_DOLLAR = re.compile(r"(?<!\\)\$(?!\s)([^$\n]+?)(?<!\s)\$(?!\d)")
# `$ x_i $` with air inside — LLMs write this; currency never looks like it
_INLINE_DOLLAR_SP = re.compile(r"(?<!\\)\$\s+([^$\n]+?)\s+\$(?!\d)")
_STRONG_MATH = re.compile(r"[\\^_]|[A-Za-z]\s*[=<>]")
_ESCAPED_DOLLAR = re.compile(r"\\\$")
_SENTINEL = "\x00M%04d%s\x00"
_SENTINEL_RE = re.compile(r"\x00M\d{4}[bi]\x00")
_CJK_RE = re.compile(r"[\u2e80-\u312f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# code regions — math inside them is literal source, never a formula
_FENCE_RE = re.compile(r"^([ \t]*)(```+|~~~+)[^\n]*\n.*?^\1\2[ \t]*$",
                       re.DOTALL | re.MULTILINE)
_INLINE_CODE = re.compile(r"(``.+?``|`[^`\n]*`)")   # group: re.split keeps it

# LaTeX that KaTeX rejects outright, or that is pure typography.
_TEX_STRIP = re.compile(
    r"\\(?:label|nonumber|notag|hfill|noindent|vspace|hspace)\*?"
    r"\s*(?:\{[^{}]*\})?")
_TEX_BM = re.compile(r"\\bm\b\s*\{")
_LONE_EOL_BACKSLASH = re.compile(r"(?<!\\)\\(?=[ \t]*\r?\n)")
# KaTeX renders the AMS display environments only in display mode, and then
# numbers every row itself — `(1)`, `(2)`, restarting in each block. A reading
# view has no numbering of its own (the prose cites the *paper's* Eq. numbers),
# so the inner, unnumbered forms are what we want. They also render inline.
_AMS_ENV = {"align": "aligned", "alignat": "alignedat", "equation": "aligned",
            "gather": "gathered", "multline": "gathered",
            # mathtools spellings KaTeX has no environment for at all
            "dcases": "cases", "rcases": "cases"}
_TEX_ENV = re.compile(r"\\(begin|end)\{(%s)\*?\}" % "|".join(_AMS_ENV))
_TEX_TAG = re.compile(r"\\tag\*?\s*\{([^{}]*)\}")


def _sanitize_tex(tex, display=False):
    """Drop constructs KaTeX rejects, map common shorthands. Conservative:
    only what is provably fatal (\\label, \\bm) or pure spacing.

    An explicit `\\tag{3}` is the author quoting the paper's own equation
    number — that one is kept, as plain parenthesised text, because `\\tag`
    itself is rejected outside a display equation.

    In display math a lone backslash at end of line is TeX's "control space"
    — it silently glues rows together. Authors (and LLMs) who wrote `\\` in
    Markdown and lost one backslash on the way here meant a row break, so
    that single case is repaired."""
    tex = _TEX_STRIP.sub("", tex)
    tex = _TEX_BM.sub(r"\\boldsymbol{", tex)
    tex = _TEX_ENV.sub(lambda m: "\\%s{%s}"
                       % (m.group(1), _AMS_ENV[m.group(2)]), tex)
    tex = _TEX_TAG.sub(lambda m: "\\qquad(%s)" % m.group(1).strip()
                       if m.group(1).strip() else "", tex)
    if display:
        tex = _LONE_EOL_BACKSLASH.sub("\\\\\\\\", tex)
    return tex.strip()


def _looks_mathy(body):
    """A `$…$` pair is math only if it smells like TeX. Blocks prose that
    happens to sit between two currency amounts, e.g. `$5 与 $10`."""
    if any(c in body for c in "\\^_=+{}"):
        return True
    if _CJK_RE.search(body) and not re.search(r"[A-Za-z]", body):
        return False
    return bool(re.search(r"[A-Za-z0-9]", body)) and len(body) <= 120


def protect_math(text):
    """Replace every math span with an opaque sentinel so Python-Markdown can
    never touch it (`_`, `*`, `|`, `&`, `<` all survive untouched).

    Fenced blocks and inline code are stepped over: `$x$` there is literal
    source, not a formula."""
    store = {}

    def stash(kind, body):
        key = _SENTINEL % (len(store), kind)
        store[key] = (kind, body)
        return key

    def math_pass(chunk):
        chunk = _ESCAPED_DOLLAR.sub("\x01DOLLAR\x01", chunk)   # \$ → literal $
        # $$…$$ first (it swallows any environment inside it whole), then the
        # inline $…$ forms. `$\begin{bmatrix}…\end{bmatrix}$` must be stashed
        # by the *dollar* rule, or the environment rule would stash the inside
        # first and leave a sentinel nested inside another formula's TeX.
        chunk = _BLOCK_DOLLAR.sub(lambda m: stash("b", m.group(1)), chunk)
        chunk = _INLINE_DOLLAR.sub(lambda m: _inline(m, _looks_mathy), chunk)
        # `$ x_i $`: the air is itself the tell, so demand a strong signal
        chunk = _INLINE_DOLLAR_SP.sub(lambda m: _inline(m, _STRONG_MATH.search),
                                      chunk)
        # delimiting forms that are not tied to a $ pair
        chunk = _ENV_BLOCK.sub(lambda m: stash("b", m.group(1)), chunk)
        chunk = _PAREN_BLOCK.sub(lambda m: stash("b", m.group(1)), chunk)
        return _PAREN_INLINE.sub(lambda m: stash("i", m.group(1)), chunk)

    def _inline(m, gate):
        body = m.group(1)
        if not gate(body):
            return m.group(0)                     # prose between two prices
        return stash("i", body.strip())

    def code_pass(chunk):
        return "".join(p if i % 2 else math_pass(p)
                       for i, p in enumerate(_INLINE_CODE.split(chunk)))

    out, pos = [], 0
    for fence in _FENCE_RE.finditer(text):
        out.append(code_pass(text[pos:fence.start()]))
        out.append(fence.group(0))
        pos = fence.end()
    out.append(code_pass(text[pos:]))
    return "".join(out), store


_RAW_BLOCK_OPEN = re.compile(r"<(div|details|section|figure|aside)\b([^>]*)>",
                             re.I)
_MD_ATTR = re.compile(r"\bmarkdown\s*=", re.I)


def enable_md_in_html(text):
    """Reports wrap their header table in `<div align="center">`. Raw HTML
    blocks are passed through verbatim, so the markdown table inside one
    stayed a run-on line of pipes. `md_in_html` parses that content — but
    only when the tag says `markdown="1"`, which is what is added here.

    Fenced blocks and inline code are stepped over: a snippet that *shows*
    `<div class="x">` must keep showing it."""
    def rep(m):
        if _MD_ATTR.search(m.group(2)):
            return m.group(0)
        return '<%s%s markdown="1">' % (m.group(1), m.group(2))

    def code_pass(chunk):
        return "".join(p if i % 2 else _RAW_BLOCK_OPEN.sub(rep, p)
                       for i, p in enumerate(_INLINE_CODE.split(chunk)))

    out, pos = [], 0
    for fence in _FENCE_RE.finditer(text):
        out.append(code_pass(text[pos:fence.start()]))
        out.append(fence.group(0))
        pos = fence.end()
    out.append(code_pass(text[pos:]))
    return "".join(out)


def restore_math(html_text, store):
    """Emit one element per formula, with the TeX as its text content.

    Text content (not an attribute) keeps the no-JS/no-network fallback
    readable — the reader sees `\\alpha = \\beta`, never `$\\alpha = \\beta$`.
    The renderer reads el.textContent and hands it to katex.render().

    A stash can land inside another stash's body (a `\\begin{cases}` in a
    `$…$`, a `\\[…\\]` around a bare `\\begin{align}`). Those inner TeX
    strings are spliced back into the outer formula: one element, one
    formula, no sentinel left showing as `M0007b` in the prose."""
    def resolve(body, depth=0):
        if depth >= 8 or "\x00" not in body:
            return body
        return _SENTINEL_RE.sub(
            lambda m: resolve(store[m.group(0)][1], depth + 1)
            if m.group(0) in store else m.group(0), body)

    for key, (kind, body) in store.items():
        # inside math a literal dollar must stay escaped for KaTeX
        body = resolve(body).replace("\x01DOLLAR\x01", "\\$")
        tex = html_mod.escape(_sanitize_tex(body, display=(kind == "b")))
        cls = "math-block" if kind == "b" else "math-inline"
        html_text = html_text.replace(key, '<span class="%s">%s</span>'
                                      % (cls, tex))
    # a display formula that is the only thing in its paragraph: drop the <p>
    # wrapper (span is valid inside <p>, but the extra margins look wrong)
    html_text = re.sub(r"<p>\s*(<span class=\"math-block\">.*?</span>)\s*</p>",
                       r"\1", html_text, flags=re.DOTALL)
    return html_text


# --------------------------------------------------------------------------
# CJK <-> Latin spacing (pangu)
# --------------------------------------------------------------------------
_CODEBLOCK = re.compile(r"<pre>.*?</pre>", re.DOTALL)
_MATH_EL = r"<span class=\"math-(?:block|inline)\">.*?</span>"
_HEADING = re.compile(r"<h([23])>(.*?)</h\1>", re.DOTALL)
_IMG_P = re.compile(r"<p>\s*<img\b([^>]*)>\s*</p>")
_ATTR_SRC = re.compile(r'src="([^"]+)"')
_ATTR_ALT = re.compile(r'alt="([^"]*)"')
# pangu char classes — ASCII escapes only, never literal glyphs here.
# The CJK side EXCLUDES the punctuation rows (U+3000-3002 ideographic space/、/。
# and U+3008-3011 〈〉《》【】): a thin space after a 顿号 or before a 句号 is no
# typesetting convention in any script. Full-width rows (U+FF00+) were never in.
_CJK = "\u2e80-\u2fff\u3003-\u3007\u3012-\u312f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_LAT = "A-Za-z0-9\u03b1-\u03c9\u2032\u2019$%\u2208\u22c8\u2248\u2265\u2264\u00d7\u00f7\u2211"
_P2A = re.compile("([" + _CJK + "])(?=[" + _LAT + "])")
_P2B = re.compile("(?<=[" + _LAT + "])([" + _CJK + "])")


def _pangu(html_text):
    """Thin-space CJK<->Latin boundaries in text nodes; skip code/pre, tags,
    math bodies. Math sentinels (still un-restored here) count as math tokens
    so a formula never glues onto a Chinese glyph."""
    out = []
    pos = 0
    for code in _CODEBLOCK.finditer(html_text):
        out.append(_pangu_text(html_text[pos:code.start()]))
        out.append(code.group(0))
        pos = code.end()
    out.append(_pangu_text(html_text[pos:]))
    return "".join(out)


def _pangu_text(chunk):
    parts = re.split(
        r"(<code>.*?</code>|<pre>.*?</pre>|" + _MATH_EL + r"|<[^>]+>|"
        r"\x00M\d{4}[bi]\x00|\$[^$\n]+\$)",
        chunk, flags=re.DOTALL)
    for i, p in enumerate(parts):
        if not p:
            continue
        if p.startswith("<"):                       # tag or whole code span
            continue
        is_inline_math = p.startswith("$") or (len(p) == 8 and p[6] == "i"
                                               and p[0] == "\x00")
        if is_inline_math:                          # never space the body
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


# --------------------------------------------------------------------------
# figures
# --------------------------------------------------------------------------
def figures_to_figures(html_text):
    """Wrap converted <p><img></p> standalone images into <figure> with a
    figcaption taken from alt (distill.pub style). Runs AFTER markdown."""
    def rep(m):
        attrs = m.group(1)
        alt_m, src_m = _ATTR_ALT.search(attrs), _ATTR_SRC.search(attrs)
        alt = alt_m.group(1) if alt_m else ""
        if not src_m:
            return m.group(0)
        img = '<img alt="%s" src="%s" loading="lazy" decoding="async">' % (
            alt, src_m.group(1))
        cap = "<figcaption>%s</figcaption>" % alt if alt else ""
        return "<figure>%s%s</figure>" % (img, cap)
    return _IMG_P.sub(rep, html_text)


# --------------------------------------------------------------------------
# tables: booktabs look + numeric alignment + honest markup
# --------------------------------------------------------------------------
_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.DOTALL)
_THEAD_RE = re.compile(r"<thead\b[^>]*>(.*?)</thead>", re.DOTALL)
_TBODY_RE = re.compile(r"<tbody\b[^>]*>(.*?)</tbody>", re.DOTALL)
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.DOTALL)
_CELL_RE = re.compile(r"<(t[dh])\b([^>]*)>(.*?)</\1>", re.DOTALL)
_ALIGN_RE = re.compile(r"text-align\s*:\s*(left|right|center)", re.I)
_TAGS = re.compile(r"<[^>]+>")
_ALIGN_CLS = {"right": "num", "center": "ctr", "left": ""}


def _is_num_cell(txt):
    """Digits without prose: a number, a range, a metric, `Eq. (1)`."""
    t = html_mod.unescape(_TAGS.sub("", txt)).strip()
    if not t or len(t) > 24:
        return False
    if _CJK_RE.search(t):                      # CJK → prose cell
        return False
    return any(c.isdigit() for c in t)


def _plain_cell(txt):
    return html_mod.unescape(_TAGS.sub("", txt)).strip()


def _rows_of(fragment):
    return [_CELL_RE.findall(r) for r in _ROW_RE.findall(fragment)]


def _column_aligns(header, body, ncols):
    """Explicit md alignment (`:---:`) wins; otherwise numeric columns get
    right-alignment with tabular figures (booktabs convention)."""
    aligns = [""] * ncols
    for row in ([header] if header else []) + body:
        for i, (_tag, attrs, _inner) in enumerate(row):
            if i >= ncols:
                break
            m = _ALIGN_RE.search(attrs)
            if m and not aligns[i]:
                aligns[i] = _ALIGN_CLS.get(m.group(1).lower(), "")
    for c in range(ncols):
        if aligns[c]:
            continue
        hits = tot = 0
        for row in body:
            if c < len(row):
                tot += 1
                if _is_num_cell(row[c][2]):
                    hits += 1
        if tot >= 2 and hits * 1.0 / tot >= 0.6:
            aligns[c] = "num"
    return aligns


def _label_column(body, aligns):
    """True when column 0 reads as a row label (short, non-numeric, ≥2 rows)."""
    if len(body) < 2:
        return False
    cells = [_plain_cell(r[0][2]) for r in body if r]
    if len(cells) < 2:
        return False
    if aligns and aligns[0] == "num":
        return False
    return all(len(c) <= 14 for c in cells) and not any(
        _is_num_cell(c) for c in cells)


def _render_row(cells, aligns, labels, kind, is_label_col):
    out = []
    for i, (tag, _attrs, inner) in enumerate(cells):
        cls = []
        if i < len(aligns) and aligns[i]:
            cls.append(aligns[i])
        if i == 0 and is_label_col:
            cls.append("k")
        attr = ' class="%s"' % " ".join(cls) if cls else ""
        lab = ""
        if kind == "td" and i < len(labels) and labels[i]:
            lab = ' data-label="%s"' % html_mod.escape(labels[i], quote=True)
        # the wrapper is inert in a table; on a narrow screen the cell becomes
        # a grid and one wrapped item keeps `$h_l$ [k, d]` on one line instead
        # of splitting the math span onto a row of its own
        out.append("<%s%s%s><span class=\"cv\">%s</span></%s>"
                   % (tag, attr, lab, inner, tag))
    return "<tr>%s</tr>" % "".join(out)


def rewrite_tables(html_text):
    """Rebuild every Markdown table: header-less tables lose the phantom
    <thead>, numeric columns line up, wide tables get a per-column floor width
    and scroll instead of being crushed, and each cell carries its column name
    for the narrow-screen card layout."""
    def fix_table(tm):
        tbl = tm.group(0)
        head_m = _THEAD_RE.search(tbl)
        body_m = _TBODY_RE.search(tbl)
        header_rows = _rows_of(head_m.group(1)) if head_m else []
        body = _rows_of(body_m.group(1)) if body_m else _rows_of(
            _THEAD_RE.sub("", tbl))
        if not body and not header_rows:
            return tbl
        header = header_rows[0] if header_rows else []
        ncols = max([len(r) for r in ([header] if header else []) + body] or [0])
        if ncols == 0:
            return tbl
        # `| | |` placeholder header (report meta block) → not a real header
        if header and all(not _plain_cell(c[2]) for c in header):
            header = []
        labels = [_plain_cell(c[2]) for c in header] if header else [""] * ncols
        aligns = _column_aligns(header, body, ncols)
        is_label_col = _label_column(body, aligns)
        parts = []
        if header:
            parts.append("<thead>%s</thead>" % _render_row(
                header, aligns, labels, "th", False))
        if body:
            parts.append("<tbody>%s</tbody>" % "".join(
                _render_row(r, aligns, labels, "td", is_label_col)
                for r in body))
        tcls = "kv" if (not header and ncols == 2) else ""
        cards = ' data-cards="1"' if (ncols >= 3 and len(body) >= 2) else ""
        return ('<div class="tw" data-cols="%d"%s><table%s>%s</table></div>'
                % (ncols, cards, ' class="%s"' % tcls if tcls else "",
                   "".join(parts)))
    return _TABLE_RE.sub(fix_table, html_text)


# --------------------------------------------------------------------------
# ascii summary box -> styled card
# --------------------------------------------------------------------------
_GLANCE_BOX = re.compile(r"<pre><code[^>]*>(.*?)</code></pre>", re.DOTALL)
_LABELLED = re.compile(r"^\s*[║┃|]?\s*([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z /·\-]{1,9})"
                       r"\s*[:：]\s*(.*)$")
_BOX_CHARS = "╔╗╚╝╠╣║┃═─·"
_BOX_INDENT = re.compile(r"^[║┃]([ \t]*)")


def glance_cards(html_text):
    """Convert the legacy ASCII ╔═╗ summary box (kept that way in md for
    Obsidian) into a styled 30-second card — box art misaligns badly in
    proportional fonts. Continuation lines (label-less, indented) belong to
    the previous field and must not be dropped."""
    def rep(m):
        raw = html_mod.unescape(m.group(1))
        if "╔" not in raw and "╠" not in raw:
            return m.group(0)
        title, pairs = "30 秒速览", []
        for raw_line in raw.splitlines():
            indent = _BOX_INDENT.match(raw_line)
            wrapped = bool(indent) and len(indent.group(1)) >= 4
            line = raw_line.strip(_BOX_CHARS).strip()
            if not line or set(line) <= set(_BOX_CHARS + " "):
                continue
            if "📌" in line:
                title = line.replace("📌", "").strip() or title
                continue
            lm = None if wrapped else _LABELLED.match(line)
            if lm:
                pairs.append([lm.group(1).strip(), lm.group(2).strip()])
            elif pairs:                      # wrapped continuation of a value
                pairs[-1][1] = (pairs[-1][1] + " " + line).strip()
        if not pairs:
            return m.group(0)
        dls = "".join("<dt>%s</dt><dd>%s</dd>"
                      % (_pangu_text(html_mod.escape(k)),
                         _pangu_text(html_mod.escape(v)))
                      for k, v in pairs)
        return ('<div class="glance"><div class="glance-title">%s</div>'
                '<dl>%s</dl></div>'
                % (_pangu_text(html_mod.escape(title)), dls))
    return _GLANCE_BOX.sub(rep, html_text)


# --------------------------------------------------------------------------
# small fidelity upgrades borrowed from GitHub / VitePress
# --------------------------------------------------------------------------
_TASK = re.compile(r"<li>\s*\[([ xX])\]\s*", re.MULTILINE)
_ALERT_MARK = re.compile(r"<p>\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*",
                         re.I)
_ALERT = re.compile(r"<blockquote>\s*" + _ALERT_MARK.pattern, re.I)
_BLOCKQUOTE = re.compile(r"<blockquote>(.*?)</blockquote>", re.DOTALL)
_LINK = re.compile(r'<a href="(https?://[^"]+)"')
_EMPTY_DIV = re.compile(r"<div\b[^>]*>\s*</div>")
_ALERT_LABEL = {"note": "说明", "tip": "提示", "important": "重要",
                "warning": "警告", "caution": "注意"}


def task_lists(html_text):
    def rep(m):
        done = m.group(1).lower() == "x"
        return '<li class="task%s"><span class="cb">%s</span> ' % (
            " done" if done else "", "✓" if done else "")
    out = _TASK.sub(rep, html_text)
    # only the lists that actually contain tasks get the marker class
    return re.sub(r"<ul>((?:(?!</ul>).)*?<li class=\"task)", r'<ul class="tasklist">\1',
                  out, flags=re.DOTALL)


def github_alerts(html_text):
    def rep(m):
        kind = m.group(1).lower()
        cls = "callout" + ("" if kind in ("note", "tip") else " warn")
        return ('<blockquote class="%s"><p class="callout-title">%s</p><p>'
                % (cls, _ALERT_LABEL[kind]))

    def split_merged(m):
        """Two alerts written back to back are a single blockquote to
        Markdown; the second has to be cut out of it before either can
        become a callout of its own."""
        body = m.group(1)
        marks = list(_ALERT_MARK.finditer(body))
        if len(marks) < 2:
            return m.group(0)
        parts, pos = [], 0
        for i, mk in enumerate(marks):
            parts.append(body[pos:mk.start()])
            parts.append("</blockquote><blockquote>" if i else "")
            pos = mk.start()
        parts.append(body[pos:])
        return "<blockquote>" + "".join(parts) + "</blockquote>"

    return _ALERT.sub(rep, _BLOCKQUOTE.sub(split_merged, html_text))


# The report template also writes light-weight callouts as a bare blockquote
# opening with 💡 or ⚠️ (no [!TIP] marker). They must get the same classes, or
# they render as plain quotes and the warn border never shows.
# Two traps found by the 2026-10-04 audit, both absent from the hand-joined
# unit-test string this once passed on:
#  1. the Markdown library emits "<blockquote>\n<p>…" — `\s*` before `<p>`
#     (mirrors _ALERT) is required or nothing in a real report ever matched;
#  2. adjacent `>` blocks merge into ONE blockquote even across blank lines,
#     so an emoji callout below a plain quote is not at the block's head —
#     split_merged cuts it out first, same remedy github_alerts uses.
_EMOJI = "\U0001f4a1|⚠️|⚠"
# The canonical template form is "> **💡 核心洞察**：…" — the emoji can hide
# inside the paragraph's opening <strong>/<em> (real vault reports confirmed
# the bold form is what actually gets written), so both shapes must match.
_EMOJI_LEAD = r"(?:<strong>|<em>)?\s*"
_EMOJI_CALLOUT = re.compile(r"<blockquote>\s*<p>(" + _EMOJI_LEAD + ")(" + _EMOJI + ")")
_EMOJI_MARK = re.compile(r"<p>\s*" + _EMOJI_LEAD + "(?:" + _EMOJI + ")")


def emoji_callouts(html_text):
    def rep(m):
        warn = m.group(2).startswith("⚠")
        return ('<blockquote class="callout%s"><p>%s%s'
                % (" warn" if warn else "", m.group(1), m.group(2)))

    def split_merged(m):
        body = m.group(1)
        marks = list(_EMOJI_MARK.finditer(body))
        if not marks:
            return m.group(0)
        parts, pos = [], 0
        for mk in marks:
            if mk.start() > pos:
                parts.append(body[pos:mk.start()])
                parts.append("</blockquote><blockquote>")
            pos = mk.start()
        parts.append(body[pos:])
        return "<blockquote>" + "".join(parts) + "</blockquote>"

    return _EMOJI_CALLOUT.sub(rep, _BLOCKQUOTE.sub(split_merged, html_text))


def external_links(html_text):
    return _LINK.sub(r'<a target="_blank" rel="noopener noreferrer" href="\1"',
                     html_text)


# A URL written bare in the prose or in a table cell (`| 代码 | https://… |`)
# is a link the reader expects to be able to click.
_BARE_URL = re.compile(r'(?<!["\'=])\bhttps?://[^\s<>"\']+')
_URL_SKIP = re.compile(r"(<pre\b.*?</pre>|<code\b.*?</code>|<a\b.*?</a>|"
                       r"<span class=\"math-(?:block|inline)\">.*?</span>)",
                       re.DOTALL)
_URL_TAIL = ".,;:!?、，。；：）)】"


def linkify_bare_urls(html_text):
    def rep(m):
        url = m.group(0)
        tail = ""
        while url and url[-1] in _URL_TAIL:      # `见 https://x.org/a.` → drop 。
            tail, url = url[-1] + tail, url[:-1]
        return ('<a target="_blank" rel="noopener noreferrer" href="%s">%s</a>%s'
                % (url, url, tail)) if url else m.group(0)

    parts = _URL_SKIP.split(html_text)
    for i in range(0, len(parts), 2):            # odd parts are the skipped ones
        parts[i] = _BARE_URL.sub(rep, parts[i])
    return "".join(parts)


def strip_empty_divs(html_text):
    return _EMPTY_DIV.sub("", html_text)


def wikilinks_to_spans(text):
    return _WIKILINK.sub(
        lambda m: '<span class="wikilink">'
        + html_mod.escape(m.group(2) or m.group(1)) + "</span>",
        text)


_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


# --------------------------------------------------------------------------
# table pipes inside code spans (`a|b`) would otherwise split a row
# --------------------------------------------------------------------------
_PIPE_SENT = "\x01PIPE\x01"
_TABLE_LINE = re.compile(r"^[ \t]*\|.*$", re.MULTILINE)
_CODE_SPAN = re.compile(r"`[^`\n]*`")


def protect_table_pipes(text):
    def fix_line(m):
        return _CODE_SPAN.sub(lambda c: c.group(0).replace("|", _PIPE_SENT),
                              m.group(0))
    return _TABLE_LINE.sub(fix_line, text)


# --------------------------------------------------------------------------
# headings / TOC
# --------------------------------------------------------------------------
def build_toc_and_ids(body_html):
    entries = []
    counter = {"n": 0}

    def repl(m):
        level, raw_inner = m.group(1), m.group(2)
        # strip decorative ━━━ rails from the DISPLAYED heading too
        inner = re.sub(r"[━]+", "", raw_inner).strip()
        counter["n"] += 1
        sid = "s%d" % counter["n"]
        plain = re.sub(r"<[^>]+>", "", inner).strip()
        clean = plain.strip(" ·")
        cls = ""
        if "⚠" in plain:
            cls = ' class="sec-warn"'
        elif "✅" in plain or "验收" in plain:
            cls = ' class="sec-ok"'
        entries.append((int(level), sid, clean))
        para = '<a class="para" href="#%s" aria-label="锚点">¶</a>' % sid
        return ('<h%s id="%s"%s><span class="secmark" aria-hidden="true">§</span>'
                '%s%s</h%s>' % (level, sid, cls, inner, para, level))

    new_body = _HEADING.sub(repl, body_html)
    parts = ['<div class="toc-title">目录 CONTENTS</div>']
    for level, sid, t in entries:
        parts.append('<a class="l%d" href="#%s">%s</a>' % (level, sid, html_mod.escape(t)))
    return "".join(parts), new_body


def reading_minutes(md_text):
    cjk = len(re.findall("[" + _CJK + "]", md_text))
    words = len(re.findall(r"[A-Za-z]+", md_text))
    return max(1, round(cjk / 400 + words / 260))


_KV_ROW = re.compile(r"^\|\s*\*\*(.+?)\*\*\s*\|\s*(.+?)\s*\|\s*$")


def promote_body_masthead(body_md: str, metadata: dict):
    """Lift the template's in-body masthead into the page header.

    The report template never specified frontmatter: the title is the body's
    first h1 and the metadata a key-value table right under it. meta_header
    reads frontmatter, so every template-following report rendered an empty
    meta-grid plus two h1s (masthead + page title) — a broken first screen on
    every report, silently. When frontmatter has no title, promote the body
    masthead instead: the h1 becomes the page title (removed from the body),
    the ``| **label** | value |`` rows become meta-grid rows. Only a masthead
    position is promoted — the h1 must precede any ``## `` heading — so a
    document that merely reuses # later is never hijacked. When a second h1
    appears before the table, the *last* such h1 is the masthead and the
    earlier placeholder heading is dropped, but its surrounding content (the
    old template's ``# 文献解读报告`` line + glance box) otherwise stays in the
    body — promoting from the first h1 used to swallow the box and the title.

    Returns (body_md, fallback_title_or_None, [(label, value), ...]).
    """
    if (metadata or {}).get("title"):
        return body_md, None, []

    h1 = re.search(r"^# (?!#)(.+?)\s*$", body_md, re.M)
    if not h1 or re.search(r"^## ", body_md[:h1.start()], re.M):
        return body_md, None, []

    lines = body_md[h1.start():].splitlines(keepends=True)
    rows = []
    last_row = None  # index into lines of the final key-value row
    h1_at = 0        # index into lines of the CURRENT masthead candidate
    for idx in range(1, len(lines)):
        line = lines[idx]
        if re.match(r"^## ", line):
            break
        if re.match(r"^# (?!#)", line):
            # A second h1 before any ##: an old-template report carries a
            # placeholder h1 (`# 文献解读报告`) + glance box ahead of the real
            # `# 标题` + table. The table belongs to the h1 directly above it,
            # so restart the candidate there — promoting the first h1 instead
            # swallowed the glance box and the real title whole. The
            # placeholder heading line itself is dropped (the promoted title
            # supersedes it); everything else before the masthead survives.
            h1_at = idx
            rows = []
            last_row = None
            continue
        row = _KV_ROW.match(line.strip())
        if row:
            rows.append((row.group(1), row.group(2)))
            last_row = idx
    if not rows:
        return body_md, None, []  # an h1 alone is content, not a masthead

    title = lines[h1_at].strip()[2:].strip()
    remainder = "".join(lines[1:h1_at]) + "".join(lines[last_row + 1:])
    return body_md[:h1.start()] + remainder, title, rows


def meta_header(post, minutes, fallback_title=None, extra_rows=None):
    d = post.metadata or {}
    title = str(d.get("title") or fallback_title or post.get("title")
                or "论文解读报告")
    badges = []
    if d.get("read_mode"):
        badges.append('<span class="badge">档位 %s</span>'
                      % html_mod.escape(str(d["read_mode"])))
    if d.get("novelty_level"):
        badges.append('<span class="badge">新颖性 %s</span>'
                      % html_mod.escape(str(d["novelty_level"])))
    badges.append('<span class="badge">阅读约 %d 分钟</span>' % minutes)
    rows = []
    seen_labels = set()
    for label, val in (extra_rows or []):
        seen_labels.add(label)
        rows.append("<div><b>%s：</b>%s</div>" % (html_mod.escape(label),
                                                  html_mod.escape(str(val))))
    labels = {"authors": "作者", "venue": "发表于", "year": "年份",
              "core_contribution": "核心贡献", "date_read": "阅读日期",
              "method_category": "方法类别", "problem_domain": "问题领域"}
    for key, label in labels.items():
        if label in seen_labels:
            continue  # the masthead table already carried this line
        val = d.get(key)
        if not val:
            continue
        if isinstance(val, list):
            val = "、".join(str(v) for v in val[:6]) + ("…" if len(val) > 6 else "")
        rows.append("<div><b>%s：</b>%s</div>" % (label, html_mod.escape(str(val))))
    meta = '<div class="meta-grid">%s</div>' % "".join(rows) if rows else ""
    return '<div class="badges">%s</div>' % "".join(badges), meta, title


# --------------------------------------------------------------------------
# KaTeX delivery
# --------------------------------------------------------------------------
def _cache_root():
    env = os.environ.get("DEEP_READ_CACHE")
    if env:
        return Path(env)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "deep-read-paper"


def local_katex_dir(explicit=None):
    """A usable KaTeX dist on disk: --katex-dir, else the fetched cache."""
    cand = Path(explicit) if explicit else _cache_root() / "katex" / KATEX_VERSION
    if (cand / "katex.min.js").is_file() and (cand / "katex.min.css").is_file():
        return cand
    return None


def _download(url, timeout=30):
    req = Request(url, headers={"User-Agent": "render_report.py"})
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_katex(dest=None, quiet=False):
    """One-time download of the KaTeX dist (+ mhchem + woff2 fonts) so later
    renders can embed it and need no network at read time."""
    dest = Path(dest) if dest else _cache_root() / "katex" / KATEX_VERSION
    dest.mkdir(parents=True, exist_ok=True)
    css = js = None
    last_err = None
    for base in KATEX_BASES:
        try:
            css = _download(base + "/katex.min.css").decode("utf-8")
            js = _download(base + "/katex.min.js")
            break
        except (URLError, OSError, UnicodeDecodeError) as exc:
            last_err = exc
    if css is None or js is None:
        raise RuntimeError("所有 KaTeX 镜像都不可达：%s" % last_err)
    (dest / "katex.min.css").write_text(css, encoding="utf-8")
    (dest / "katex.min.js").write_bytes(js)
    fonts = sorted(set(re.findall(r"url\((fonts/[^)]+?\.woff2)\)", css)))
    (dest / "fonts").mkdir(exist_ok=True)
    for rel in fonts:
        (dest / rel).write_bytes(_download(
            base.rstrip("/") + "/" + rel))
    try:
        (dest / "mhchem.min.js").write_bytes(
            _download(base + "/contrib/mhchem.min.js"))
    except (URLError, OSError):
        pass                                  # chemistry is optional
    if not quiet:
        size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
        print("[OK] KaTeX %s -> %s (%.1f MB, %d fonts)"
              % (KATEX_VERSION, dest, size / 1e6, len(fonts)))
    return dest


def _embed_katex(dist, needs_chem=False):
    """Inline KaTeX (CSS + woff2 fonts as data URIs + JS) into the page."""
    css = (dist / "katex.min.css").read_text(encoding="utf-8")
    fonts = {}
    for rel in set(re.findall(r"url\((fonts/[^)]+?\.woff2)\)", css)):
        f = dist / rel
        if f.is_file():
            fonts[rel] = base64.b64encode(f.read_bytes()).decode("ascii")
    # keep woff2 only — the other formats are dead weight once we inline
    css = re.sub(r",\s*url\(fonts/[^)]+?\.(?:woff|ttf)\)[^,;]*", "", css)

    def sub_font(m):
        rel = m.group(1)
        if rel not in fonts:
            return m.group(0)
        return "url(data:font/woff2;base64,%s)" % fonts[rel]
    css = re.sub(r"url\((fonts/[^)]+?\.woff2)\)", sub_font, css)
    js = (dist / "katex.min.js").read_text(encoding="utf-8")
    chem = ""
    if needs_chem and (dist / "mhchem.min.js").is_file():
        chem = "<script>%s</script>\n" % _inline_safe(
            (dist / "mhchem.min.js").read_text(encoding="utf-8"))
    head = "<style>%s</style>" % css.replace("</style", "<\\/style")
    return head, "<script>%s</script>\n%s" % (_inline_safe(js), chem)


def _inline_safe(js):
    """`</script>` inside an inlined script would close the tag early."""
    return js.replace("</script", "<\\/script")


def _katex_assets(mode, dist=None, needs_chem=False):
    """-> (head_html, body_scripts_html, note). mode: cdn | embed | off.
    Both returned fragments are ready-to-inject HTML (script tags included)."""
    macros = json.dumps(KATEX_MACROS)
    render_js = _MATH_RENDER_JS.replace("__MACROS__", macros).replace(
        "__ERRCOLOR__", '"#c0392b"')
    if mode == "off":
        return "", "", ("本页以离线方式渲染：公式保留 LaTeX 源码，"
                        "联网打开或改用 --embed-katex 可得到排版后的公式。")
    if mode == "embed" and dist:
        head, scripts = _embed_katex(dist, needs_chem)
        return head, scripts + "<script>%s</script>" % render_js, ""
    if mode == "embed":
        print("[WARN] 未找到本地 KaTeX（先运行 --fetch-katex），"
              "本次改用 CDN 模式", file=sys.stderr)
    loader = _MATH_LOADER_JS.replace("__BASES__", json.dumps(list(KATEX_BASES))) \
        .replace("__NEEDS_CHEM__", "true" if needs_chem else "false") \
        .replace("__RENDER__", render_js)
    return "", "<script>%s</script>" % loader, ""


# --------------------------------------------------------------------------
# page chrome
# --------------------------------------------------------------------------
_CSS = """
:root{color-scheme:light dark;
  --fg:#24262b;--fg-dim:#5d6570;--bg:#f7f6f2;--panel:#ffffff;--accent:#33517d;
  --rule:#e0dcd2;--rule-soft:#eae7de;--code-bg:#f0eee7;--badge-bg:#e7edf6;
  --badge-fg:#33517d;--quote-bg:#fbfaf6;--warn:#b26a00;--ok:#2e7d32;
  --tw-sh:rgba(70,60,40,.06);--tbl-hd:rgba(70,60,40,.035);--mnote:#8a6d3b;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  --mono:ui-monospace,SFMono-Regular,"Cascadia Code","JetBrains Mono","Fira Code",Consolas,monospace}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  color-scheme:dark;
  --fg:#d9dce2;--fg-dim:#98a1ad;--bg:#171a20;--panel:#1e222a;--accent:#8fb1de;
  --rule:#323743;--rule-soft:#2a2f39;--code-bg:#232733;--badge-bg:#2a3a52;
  --badge-fg:#a8c6ea;--quote-bg:#1c2027;--warn:#d29a4a;--ok:#7cc47f;
  --tw-sh:rgba(0,0,0,.35);--tbl-hd:rgba(255,255,255,.04);--mnote:#d3b273}}
:root[data-theme="dark"]{color-scheme:dark;
  --fg:#d9dce2;--fg-dim:#98a1ad;--bg:#171a20;--panel:#1e222a;--accent:#8fb1de;
  --rule:#323743;--rule-soft:#2a2f39;--code-bg:#232733;--badge-bg:#2a3a52;
  --badge-fg:#a8c6ea;--quote-bg:#1c2027;--warn:#d29a4a;--ok:#7cc47f;
  --tw-sh:rgba(0,0,0,.35);--tbl-hd:rgba(255,255,255,.04);--mnote:#d3b273}
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
  font-family:var(--sans);border-right:1px solid var(--rule-soft)}
nav.toc .toc-title{font-weight:700;padding:4px 10px 12px;letter-spacing:.12em;
  color:var(--fg-dim);font-size:12px}
nav.toc a{display:block;color:var(--fg-dim);text-decoration:none;
  padding:3px 10px;border-left:2px solid transparent;border-radius:0 6px 6px 0}
nav.toc a:hover{color:var(--accent);background:var(--panel)}
nav.toc a.active{color:var(--accent);border-left-color:var(--accent);
  background:var(--panel);font-weight:600}
nav.toc a.l3{padding-left:24px;font-size:12.5px}
main{flex:1;min-width:0;padding:40px 52px 100px}
/* prose holds a reading measure; a table may use the width beside it, up to
   the point where a row would turn into a desert of whitespace */
article>*{max-width:46em}
article>.tw{max-width:none}
header.masthead{max-width:46em;border-bottom:1px solid var(--rule);
  padding-bottom:22px;margin-bottom:30px}
header.masthead .kicker{font-family:var(--sans);font-size:12px;letter-spacing:.25em;
  color:var(--fg-dim);text-transform:uppercase}
header.masthead h1{font-family:var(--sans);font-size:31px;line-height:1.4;
  margin:10px 0 6px;font-weight:700;text-wrap:balance;letter-spacing:.01em}
.badges{margin:10px 0 2px}
.badge{display:inline-block;background:var(--badge-bg);color:var(--badge-fg);
  border-radius:999px;padding:2px 12px;font-size:12px;margin:2px 6px 2px 0;
  font-family:var(--sans)}
.meta-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));
  gap:3px 24px;font-size:13.5px;color:var(--fg-dim);margin-top:12px}
.meta-grid b{color:var(--fg);font-weight:600}
/* reports often open with `# 文献解读报告` + the paper title as two more H1s.
   They are section headings here, not a second masthead — keep them one step
   above h2 instead of at display size. */
article h1{font-family:var(--sans);font-size:25px;font-weight:700;
  margin:46px 0 14px;padding-bottom:8px;border-bottom:1px solid var(--rule);
  letter-spacing:.02em;text-wrap:balance}
h2{font-family:var(--sans);font-size:23px;font-weight:700;margin:52px 0 16px;
  padding-bottom:8px;border-bottom:1px solid var(--rule);letter-spacing:.02em;
  text-wrap:balance}
h3{font-family:var(--sans);font-size:18px;font-weight:700;margin:34px 0 12px;
  color:var(--accent);text-wrap:balance}
h4{font-family:var(--sans);font-size:15.5px;font-weight:700;margin:26px 0 10px}
h2 .secmark,h3 .secmark{opacity:.4;font-weight:400;margin-right:9px;font-size:.8em}
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
blockquote.callout{border-left-color:var(--accent)}
blockquote.callout.warn{border-left-color:var(--warn)}
blockquote .callout-title{font-family:var(--sans);font-weight:700;font-size:13px;
  letter-spacing:.06em;color:var(--accent);margin-bottom:2px}
blockquote.callout.warn .callout-title{color:var(--warn)}
code{font-family:var(--mono);font-size:.85em;background:var(--code-bg);
  padding:1.5px 5px;border-radius:5px}
pre{position:relative;background:var(--code-bg);padding:14px 16px;border-radius:10px;
  overflow:auto;line-height:1.55;border:1px solid var(--rule-soft)}
pre code{background:none;padding:0;font-size:13.5px}
.copybtn{position:absolute;top:8px;right:8px;font-family:var(--sans);font-size:11px;
  padding:2px 9px;border:1px solid var(--rule);border-radius:6px;
  background:var(--panel);color:var(--fg-dim);cursor:pointer;opacity:0;
  transition:opacity .15s}
pre:hover .copybtn,.copybtn:focus{opacity:1}
/* booktabs tables (Tufte CSS): horizontals only, no vertical rules */
.tw{overflow-x:auto;overflow-y:hidden;margin:1.3em 0;border-radius:8px;
  -webkit-overflow-scrolling:touch}
.tw>table{border-collapse:collapse;width:100%;max-width:64em;font-size:14px;
  min-width:calc(var(--cols,2) * 7.6em);
  font-family:var(--sans);line-height:1.55}
th,td{border:none;border-bottom:1px solid var(--rule-soft);padding:8px 12px;
  text-align:left;vertical-align:top;overflow-wrap:break-word}
thead th{border-top:2px solid var(--fg);border-bottom:1px solid var(--fg);
  font-weight:700;white-space:nowrap;background:var(--tbl-hd)}
tbody tr:last-child td{border-bottom:2px solid var(--fg)}
tbody tr:hover td{background:var(--quote-bg)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;
  font-feature-settings:"tnum" 1}
td.ctr,th.ctr{text-align:center}
td.k{font-weight:600;white-space:nowrap}
table.kv{max-width:46em}   /* meta tables keep the prose edge */
table.kv td{vertical-align:top}
table.kv td:first-child{white-space:nowrap;color:var(--fg-dim);font-weight:600;
  width:1%}
td code{font-size:12.5px}
td>p{margin:0}
td ul,td ol{margin:.2em 0;padding-left:1.2em}
/* math — KaTeX owns the look once rendered; before that the TeX source is
   shown in a quiet monospace style instead of raw $…$ noise */
.math-inline,.math-block{font-family:var(--mono);font-size:.86em;
  color:var(--fg-dim);overflow-wrap:break-word}
.math-block{display:block;position:relative;margin:1.15em 0;padding:.1em 0;
  overflow-x:auto;overflow-y:hidden;text-align:center;white-space:pre-wrap}
.math-block:not(.katex-ok){text-align:left}
.math-inline.katex-ok,.math-block.katex-ok{font-family:inherit;font-size:1em;
  color:inherit}
.math-inline .katex{font-size:1.04em}
.math-block.katex-ok>.katex-display{width:max-content;max-width:100%;margin:.25em auto}
.math-block .katex-display .katex{font-size:1.1em}
.katex-err{color:var(--warn)}
.texcopy{position:absolute;top:0;right:0;font-family:var(--sans);font-size:11px;
  padding:1px 8px;border:1px solid var(--rule);border-radius:6px;
  background:var(--panel);color:var(--fg-dim);cursor:pointer;opacity:0;
  transition:opacity .15s}
.math-block:hover .texcopy{opacity:.9}
#math-note{max-width:46em;margin:0 0 1.4em;font-family:var(--sans);font-size:12.5px;
  color:var(--mnote);border:1px dashed var(--rule);border-radius:8px;
  padding:7px 12px}
figure{margin:1.6em 0;text-align:center}
figure img{margin:0 auto 8px}
figcaption{font-size:13px;color:var(--fg-dim);text-align:left;font-family:var(--sans);
  padding-left:4px}
img{max-width:100%;height:auto;display:block;border:1px solid var(--rule);
  border-radius:8px;background:#fff;cursor:zoom-in;
  box-shadow:0 2px 10px var(--tw-sh)}
.wikilink{border-bottom:1px dashed var(--accent);color:var(--accent);
  font-style:italic}
.footnote{font-size:.9em;color:var(--fg-dim);margin-top:2.4em}
.footnote hr{border:none;border-top:1px solid var(--rule);width:32%;margin:1.6em 0 1em}
.footnote ol{padding-left:1.4em}
sup.footnote-ref{font-size:.72em;line-height:0}
sup.footnote-ref a{color:var(--accent);text-decoration:none;font-weight:600}
a.footnote-backref{color:var(--fg-dim);text-decoration:none;margin-left:.3em}
ul.tasklist{padding-left:.15em;list-style:none}
li.task{display:flex;gap:.5em;align-items:flex-start}
li.task>.cb{flex:0 0 auto;width:1.02em;height:1.02em;margin-top:.36em;
  border:1.5px solid var(--rule);border-radius:4px;display:inline-flex;
  align-items:center;justify-content:center;font-size:.72em;line-height:1;
  color:transparent;font-family:var(--sans)}
li.task.done>.cb{background:var(--ok);border-color:var(--ok);color:#fff}
.glance{max-width:46em;border:1px solid var(--rule);border-left:4px solid var(--accent);
  border-radius:0 12px 12px 0;background:var(--panel);padding:14px 22px 16px;
  margin:1.5em 0;box-shadow:0 2px 12px var(--tw-sh)}
.glance-title{font-family:var(--sans);font-weight:700;font-size:13px;
  letter-spacing:.14em;color:var(--accent);margin-bottom:10px}
.glance dl{display:grid;grid-template-columns:5.5em 1fr;gap:5px 16px;margin:0;
  font-size:14.5px}
.glance dt{font-weight:700;color:var(--fg-dim);font-family:var(--sans)}
.glance dd{margin:0}
#zoom{position:fixed;inset:0;background:rgba(10,10,12,.86);display:none;
  z-index:80;align-items:center;justify-content:center;cursor:zoom-out}
#zoom figure{margin:0;max-width:96vw}
#zoom img{max-width:96vw;max-height:82vh;margin:0;border:none;
  box-shadow:0 8px 40px rgba(0,0,0,.6);cursor:zoom-out;background:#fff}
#zoom figcaption{color:#d9dce2;text-align:center;margin-top:12px;font-size:13.5px;
  padding:0 6px}
#toc-overlay{display:none;position:fixed;inset:0;z-index:65;
  background:rgba(0,0,0,.38)}
#toc-overlay.show{display:block}
#toc-fab{display:none;position:fixed;right:18px;bottom:18px;z-index:60;
  background:var(--accent);color:#fff;border:none;border-radius:999px;
  width:46px;height:46px;font-size:20px;box-shadow:0 4px 14px rgba(0,0,0,.3);
  cursor:pointer}
#theme-btn{position:fixed;right:18px;top:18px;z-index:60;width:38px;height:38px;
  border:1px solid var(--rule);border-radius:999px;background:var(--panel);
  color:var(--fg-dim);font-size:17px;line-height:1;cursor:pointer;
  box-shadow:0 2px 8px var(--tw-sh)}
#theme-btn:hover{color:var(--accent);border-color:var(--accent)}
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
/* manual theme override — data-theme wins over prefers-color-scheme in both
   directions; "auto" removes the attribute and the media query applies */
:root[data-theme="light"]{color-scheme:light}
:root[data-theme="dark"]{color-scheme:dark}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){img{background:#23252b;filter:brightness(.94)}}
}
:root[data-theme="dark"] img{background:#23252b;filter:brightness(.94)}
/* narrow screens: ≥3-column tables become one card per row */
@media (max-width:620px){
  .tw{--cols:1}
  .tw[data-cards]>table{min-width:0;font-size:13.5px}
  .tw[data-cards] thead{display:none}
  .tw[data-cards] tbody tr{display:block;border-top:1px solid var(--rule);
    padding:7px 2px 9px}
  .tw[data-cards] tbody tr:last-child td{border-bottom:none}
  .tw[data-cards] tbody tr:hover td{background:none}
  .tw[data-cards] td{display:grid;grid-template-columns:minmax(5.2em,auto) 1fr;
    gap:3px 12px;border:none;padding:3px 4px;text-align:left}
  .tw[data-cards] td>.cv{min-width:0}
  .tw[data-cards] td::before{content:attr(data-label);color:var(--fg-dim);
    font-weight:600;font-size:12.5px;line-height:1.6}
  .tw[data-cards] td:not([data-label]){display:block;font-weight:700;
    padding:2px 4px 5px;white-space:normal}
  .tw[data-cards] td:not([data-label])::before{display:none}
  .tw[data-cards] td.num{text-align:left}
}
@media print{#progress,nav.toc,#toc-fab,footer.build,#zoom,#theme-btn,.copybtn,.texcopy{display:none}
  body{background:#fff;color:#111}main{padding:0}article>*{max-width:100%}
  img,blockquote,pre{break-inside:avoid}h2{break-after:avoid}
  .math-block,figure{break-inside:avoid}
  table{break-inside:auto}thead{display:table-header-group}
  tr,td,th{break-inside:avoid}
  .tw{overflow:visible}.tw>table{min-width:0}}
"""

# UI chrome: progress, active TOC section, mobile drawer, image zoom,
# code copy buttons, click-to-copy for display formulas.
_JS = """
(function(){
var p=document.getElementById('progress'),h=document.documentElement;
function up(){var d=h.scrollHeight-h.clientHeight;
p.style.width=(d>0?(h.scrollTop||document.body.scrollTop)/d*100:0)+'%';}
addEventListener('scroll',up,{passive:true});up();
var toc=document.querySelector('nav.toc'),fab=document.getElementById('toc-fab');
var ovl=document.getElementById('toc-overlay');
function closeToc(){toc.classList.remove('open');
 if(ovl)ovl.classList.remove('show');}
function fabClick(){var open=toc.classList.toggle('open');
 if(ovl)ovl.classList.toggle('show',open);}
if(fab)fab.onclick=fabClick;
if(ovl)ovl.onclick=closeToc;
var links=[].slice.call(toc.querySelectorAll('a[href^="#"]')),secs=[];
links.forEach(function(a){var el=document.getElementById(a.href.split('#')[1]);
if(el)secs.push([el,a]);
a.addEventListener('click',closeToc);});   /* mobile drawer: follow the link, close */
if('IntersectionObserver' in window){
 var io=new IntersectionObserver(function(es){es.forEach(function(e){
  if(e.isIntersecting){links.forEach(function(l){l.classList.remove('active')});
   var s=secs.find(function(x){return x[0]===e.target});
   if(s)s[1].classList.add('active');}});},{rootMargin:'-10% 0px -75% 0px'});
 secs.forEach(function(x){io.observe(x[0]);});}
var z=document.getElementById('zoom'),zc=document.getElementById('zoom-cap');
document.querySelectorAll('article img').forEach(function(im){
 im.onclick=function(){z.querySelector('img').src=im.src;
  var fig=im.closest('figure'),cap=fig&&fig.querySelector('figcaption');
  zc.textContent=cap?cap.textContent:'';
  zc.style.display=cap?'block':'none';z.style.display='flex';};});
z.onclick=function(){z.style.display='none';};
addEventListener('keydown',function(e){if(e.key==='Escape'){z.style.display='none';closeToc();}});
/* manual theme: auto (follow system) -> light -> dark, remembered per browser */
var tbtn=document.getElementById('theme-btn');
var TORDER=['auto','light','dark'],
    TICON={auto:'◐',light:'☀',dark:'☾'},
    TLABEL={auto:'跟随系统',light:'亮色',dark:'暗色'};
function setTheme(t){
 if(t==='auto')document.documentElement.removeAttribute('data-theme');
 else document.documentElement.setAttribute('data-theme',t);
 try{localStorage.setItem('dr-theme',t);}catch(e){}
 if(tbtn){tbtn.textContent=TICON[t];
  tbtn.setAttribute('aria-label','配色：'+TLABEL[t]);}}
if(tbtn){
 var saved='auto';
 try{saved=localStorage.getItem('dr-theme')||'auto';}catch(e){}
 if(TORDER.indexOf(saved)<0)saved='auto';
 setTheme(saved);
 tbtn.onclick=function(){
  var cur=document.documentElement.getAttribute('data-theme')||'auto';
  setTheme(TORDER[(TORDER.indexOf(cur)+1)%3]);};}
function copy(text,btn,label){
 var done=function(){var o=btn.textContent;btn.textContent=label;
  setTimeout(function(){btn.textContent=o;},1400);};
 if(navigator.clipboard&&navigator.clipboard.writeText){
  navigator.clipboard.writeText(text).then(done,function(){});return;}
 var ta=document.createElement('textarea');ta.value=text;ta.style.position='fixed';
 ta.style.opacity='0';document.body.appendChild(ta);ta.select();
 try{document.execCommand('copy');done();}catch(e){}
 document.body.removeChild(ta);}
document.querySelectorAll('article pre').forEach(function(pre){
 var code=pre.querySelector('code');if(!code)return;
 var b=document.createElement('button');b.className='copybtn';b.type='button';
 b.textContent='复制';
 b.onclick=function(){copy(code.innerText,b,'已复制');};
 pre.appendChild(b);});
})();
"""

# renders every formula element; loaded once KaTeX itself is available.
_MATH_RENDER_JS = """
(function(){
var MACROS=__MACROS__, ERR=__ERRCOLOR__;
function clean(t){return t.replace(/\\\\(label|nonumber|notag|hfill)\\\\s*(\\\\{[^{}]*\\\\})?/g,'')
 .replace(/\\\\bm\\\\s*\\\\{/g,'\\\\boldsymbol{').replace(/^\\$+|\\$+$/g,'').trim();}
function opts(display,cb){return {displayMode:display,throwOnError:false,strict:false,
 trust:false,macros:MACROS,errorColor:ERR,errorCallback:cb};}
function paint(el){
 var display=el.classList.contains('math-block');
 var tex=el.__tex;
 var settled=false;
 var o=opts(display,function(){ if(settled)return; settled=true;
  var fixed=clean(tex);
  if(fixed!==tex){ try{ katex.render(fixed,el,opts(display,function(){
     el.classList.add('katex-err');})); el.classList.add('katex-ok','katex-fixed');
     return; }catch(e){} }
  el.classList.add('katex-err'); el.title=tex;});
 try{ katex.render(tex,el,o); }catch(e){ if(!settled){settled=true;
  el.classList.add('katex-err');el.title=tex;return;} }
 if(!settled){settled=true;el.classList.add('katex-ok');}
}
document.querySelectorAll('.math-inline,.math-block').forEach(function(el){
 if(el.__tex!==undefined)return;
 el.__tex=el.textContent;
 paint(el);
 if(el.classList.contains('math-block')){
  var b=document.createElement('button');b.className='texcopy';b.type='button';
  b.textContent='TeX';b.title='复制 LaTeX 源码';
  b.onclick=function(){var ta=document.createElement('textarea');
   ta.value=el.__tex;ta.style.position='fixed';ta.style.opacity='0';
   document.body.appendChild(ta);ta.select();
   try{document.execCommand('copy');b.textContent='✓';
    setTimeout(function(){b.textContent='TeX';},1400);}catch(e){}
   document.body.removeChild(ta);};
  el.appendChild(b);}
});
document.documentElement.classList.add('katex-on');
})();
"""

# loads KaTeX from the mirror chain, then calls the renderer.
_MATH_LOADER_JS = """
(function(){
var BASES=__BASES__, NEEDS_CHEM=__NEEDS_CHEM__, i=0;
var els=document.querySelectorAll('.math-inline,.math-block');
if(!els.length)return;
function fail(){
 var note=document.createElement('div');note.id='math-note';
 note.textContent='公式未能排版：KaTeX 资源不可达（本机所有 CDN 镜像都失败）。'+
  '下方按 LaTeX 源码显示。可运行 --fetch-katex 后加 --embed-katex 生成自包含版本。';
 var art=document.querySelector('article');
 if(art&&art.firstChild)art.insertBefore(note,art.firstChild);
}
function boot(base){ if(NEEDS_CHEM){ var c=document.createElement('script');
  c.src=base+'/contrib/mhchem.min.js';
  c.onload=function(){__RENDER__};c.onerror=function(){__RENDER__};
  document.head.appendChild(c);} else {__RENDER__} }
function tryNext(){
 if(i>=BASES.length){fail();return;}
 var base=BASES[i++];
 var link=document.createElement('link');link.rel='stylesheet';
 link.href=base+'/katex.min.css';document.head.appendChild(link);
 var s=document.createElement('script');s.src=base+'/katex.min.js';
 s.onload=function(){ if(window.katex){boot(base);} else {tryNext();} };
 s.onerror=function(){ if(link.parentNode)link.parentNode.removeChild(link);
  if(s.parentNode)s.parentNode.removeChild(s); tryNext(); };
 document.head.appendChild(s);
}
tryNext();
})();
"""

_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
{katex_head}
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
{math_note}
<article>{body}</article>
<footer class="build">本页由 <code>tools/render_report.py</code> 从 <code>{source}</code> 确定性生成 · {stamp} · md 为唯一事实源，修订后请重新渲染 · 公式引擎 KaTeX {kmode}</footer>
</main>
</div>
<div id="zoom"><figure><img alt=""><figcaption id="zoom-cap"></figcaption></figure></div>
<div id="toc-overlay" aria-hidden="true"></div>
<button id="toc-fab" aria-label="目录">☰</button>
<button id="theme-btn" type="button" aria-label="配色：跟随系统">◐</button>
<script>{js}</script>
{katex_scripts}
</body>
</html>
"""


def render_report(md_path, out_path=None, offline=False, katex=None,
                  katex_dir=None):
    md_path = Path(md_path)
    post = frontmatter.load(str(md_path))
    body_md, fallback_title, masthead_rows = promote_body_masthead(
        post.content, post.metadata)
    post = frontmatter.Post(body_md, **(post.metadata or {}))

    mode = "off" if offline else (katex or "cdn")
    needs_chem = bool(re.search(r"\\ce\{|\\pu\{", body_md))

    body_md = enable_md_in_html(body_md)
    guarded, store = protect_math(body_md)
    guarded = protect_table_pipes(guarded)
    guarded = guarded.replace("\x01DOLLAR\x01", "$")
    guarded = wikilinks_to_spans(guarded)
    body_html = markdown.markdown(guarded, extensions=MD_EXTENSIONS,
                                  output_format="html")
    body_html = body_html.replace(_PIPE_SENT, "|")
    body_html = _pangu(body_html)              # sentinels still atomic here
    body_html = restore_math(body_html, store)
    body_html = figures_to_figures(body_html)
    body_html = rewrite_tables(body_html)
    body_html = glance_cards(body_html)
    body_html = task_lists(body_html)
    body_html = github_alerts(body_html)
    body_html = emoji_callouts(body_html)
    body_html = external_links(body_html)
    body_html = linkify_bare_urls(body_html)
    body_html = strip_empty_divs(body_html)
    toc, body_html = build_toc_and_ids(body_html)
    badges, meta, title = meta_header(post, reading_minutes(body_md),
                                      fallback_title=fallback_title,
                                      extra_rows=masthead_rows)

    dist = local_katex_dir(katex_dir) if mode == "embed" else None
    katex_head, katex_scripts, note = _katex_assets(mode, dist, needs_chem)
    # The footer must report what the PAGE ACTUALLY IS: on the embed→CDN
    # fallback _katex_assets warns on stderr but keeps going — while kmode
    # still promised 「KaTeX …（内嵌）」, i.e. the artifact claimed to be
    # self-contained while its formulas in fact need network.
    if mode == "embed" and dist is None:
        kmode = KATEX_VERSION + "（CDN 多镜像——本地 KaTeX 缺失，页面并非自包含）"
    else:
        kmode = {"off": "未加载（离线）",
                 "embed": KATEX_VERSION + "（内嵌）"}.get(
            mode, KATEX_VERSION + "（CDN 多镜像）")

    out_path = Path(out_path) if out_path else md_path.with_suffix(".html")
    page = _TEMPLATE.format(
        title=html_mod.escape(title),
        css=_CSS,
        js=_JS,
        katex_head=katex_head,
        katex_scripts=katex_scripts,
        kmode=kmode,
        math_note=('<div id="math-note">%s</div>' % html_mod.escape(note)
                   if note else ""),
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
    ap.add_argument("--md", default=None, help="report .md file (frontmatter ok)")
    ap.add_argument("--out", default=None, help="output .html (default: alongside md)")
    ap.add_argument("--offline", action="store_true",
                    help="no KaTeX at all (formulas stay as readable LaTeX source)")
    ap.add_argument("--embed-katex", action="store_true",
                    help="inline KaTeX into the HTML (self-contained; needs a "
                         "local dist, see --fetch-katex)")
    ap.add_argument("--katex-dir", default=None,
                    help="path to a local KaTeX dist directory")
    ap.add_argument("--fetch-katex", action="store_true",
                    help="download the KaTeX dist into the cache and exit "
                         "(one-time; enables --embed-katex)")
    args = ap.parse_args(argv)

    if args.fetch_katex:
        try:
            fetch_katex(args.katex_dir)
        except Exception as exc:
            print(f"[ERROR] {type(exc).__name__}: {exc}")
            return 1
        if not args.md:
            return 0
    if not args.md:
        print("[ERROR] --md is required")
        return 1
    if not Path(args.md).is_file():
        print(f"[ERROR] file not found: {args.md}")
        return 1
    try:
        out = render_report(args.md, args.out, offline=args.offline,
                            katex="embed" if args.embed_katex else None,
                            katex_dir=args.katex_dir)
    except Exception as exc:
        print(f"[ERROR] {type(exc).__name__}: {exc}")
        return 1
    print(f"[OK] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
