"""Unit tests for tools/render_report.py (offline, no browser needed).

Run from repo root:  python -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import render_report as rr  # noqa: E402

SAMPLE = """---
title: "SayPlan: Grounding LLMs using 3D Scene Graphs"
short_name: "SayPlan"
year: 2023
venue: "CoRL"
authors: ["石川-一人", "Driess R."]
read_mode: deep
novelty_level: substantial
core_contribution: "用 3D 场景图作为 LLM 的可查询世界模型完成长程任务规划"
date_read: 2026-09-25
---

# 报告正文

## ━━━ 1. 问题背景与分析 ━━━

### 1.1 研究问题

注意力公式 $\\alpha = \\mathrm{softmax}(QK^T/\\sqrt{d_k})$ 中的缩放因子。

$$
L_{base} = \\sum_{i} w_i \\cdot l_i
$$

> 引用块中的 $E=mc^2$ 内联数学。

| 方法 | R@5 | 说明 |
|------|-----|------|
| Baseline | 44.2 | 对比 |
| **本文** | **55.3** | 提升 [[PreFLMR]] 十倍 |

![图3：架构总览](../attachments/sayplan/sayplan_p03_fig3.png)

```python
def plan(goal):
    return sg.query(goal)  # 代码块
```

## ━━━ 2. 方法详解 ━━━

正文里出现美元金额 $5 和 $100 不应被当公式（后随空格规则），
但 $x^2$ 应该保留。
"""


class RenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.md = Path(cls.tmp.name) / "SayPlan_解读报告.md"
        cls.md.write_text(SAMPLE, encoding="utf-8")
        cls.html = rr.render_report(cls.md)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _h(self):
        return self.html.read_text(encoding="utf-8")

    def test_output_next_to_source(self):
        self.assertEqual(self.html.name, "SayPlan_解读报告.html")
        self.assertTrue(self.html.is_file())

    def test_frontmatter_meta_card(self):
        h = self._h()
        self.assertIn("<title>SayPlan: Grounding LLMs", h)
        self.assertIn("CoRL", h)
        self.assertIn("档位 deep", h)
        self.assertIn("新颖性 substantial", h)

    def test_tables_rendered(self):
        h = self._h()
        self.assertIn("<table>", h)
        self.assertIn("55.3", h)

    def test_math_survives(self):
        h = self._h()
        self.assertIn("softmax", h)          # inline math content restored
        self.assertIn("math-block", h)       # block math wrapped
        self.assertIn("L_{base}", h)         # block body intact, not eaten

    def test_wikilink_as_span(self):
        h = self._h()
        self.assertIn('<span class="wikilink">PreFLMR</span>', h)
        self.assertNotIn("[[PreFLMR]]", h)

    def test_image_relative_path_untouched(self):
        self.assertIn('src="../attachments/sayplan/sayplan_p03_fig3.png"',
                      self._h())

    def test_toc_entries_with_ids(self):
        h = self._h()
        self.assertIn('<nav class="toc"', h)
        self.assertIn('href="#s1"', h)
        self.assertIn("问题背景与分析", h)

    def test_katex_cdn_and_offline(self):
        self.assertIn("katex@0.16.11", self._h())
        out2 = rr.render_report(self.md, offline=True)
        self.assertNotIn("katex@0.16.11", out2.read_text(encoding="utf-8"))

    def test_code_block_rendered(self):
        self.assertIn("<pre><code", self._h())

    def test_cli_exit_codes(self):
        rc = rr.main(["--md", str(self.md), "--out",
                      str(Path(self.tmp.name) / "x.html")])
        self.assertEqual(rc, 0)
        self.assertEqual(rr.main(["--md", str(self.md.parent / "nope.md")]), 1)

    # ---------- v2 design chrome (Tufte/VitePress/pangu) ----------

    def test_booktabs_wrapping_and_chrome(self):
        h = self._h()
        self.assertIn('<div class="tw" data-cols="3" data-cards="1"><table>', h)
        self.assertIn('class="para"', h)                 # ¶ heading anchors
        self.assertIn("阅读约", h)                        # reading-time badge
        self.assertIn("<figure>", h)                     # figure + figcaption
        self.assertIn('id="progress"', h)                # reading progress bar
        self.assertIn('id="toc-fab"', h)                 # mobile TOC drawer
        self.assertIn('id="zoom"', h)                    # click-to-zoom overlay
        self.assertIn("sec-warn", rr.build_toc_and_ids(
            "<h2>⚠️ 矛盾与仲裁记录</h2>")[1])            # warn-tinted section

    def test_no_table_tag_corruption(self):
        """regression (2026-09-26): a cell regex that captured only the `t` of
        `<td>` re-emitted `<d>`, collapsing every table into run-on text."""
        import re as _re
        h = self._h()
        self.assertEqual(_re.findall(r"</?[dh]>", h), [])
        self.assertIn("<td", h)
        self.assertIn("<th", h)

    def test_cell_attributes_preserved(self):
        """`class`/`data-label` must survive the rewrite, not be replaced."""
        import re as _re
        h = self._h()
        row = _re.search(r'<tr><td[^>]*><span class="cv">Baseline.*?</tr>', h).group(0)
        self.assertIn('class="k"', row)                  # label column
        self.assertIn('data-label="方法"', row)           # mobile card labels
        self.assertIn('data-label="R@5"', row)
        self.assertIn('class="num"', row)                # numeric alignment

    def test_pangu_spacing_and_exemptions(self):
        TS = chr(0x2009)
        self.assertEqual(rr._pangu_text("汉字x测试"),
                         f"汉字{TS}x{TS}测试")
        self.assertEqual(rr._pangu_text("约80词"), f"约{TS}80{TS}词")
        # CJK punctuation never takes pangu space: 顿号/句号/书名号 are not
        # letters, and a thin gap after them reads as a rendering bug.
        self.assertEqual(rr._pangu_text("方法、工具"), "方法、工具")
        self.assertEqual(rr._pangu_text("结束。Next"), "结束。Next")
        self.assertEqual(rr._pangu_text("详见《Deep RL》一书"), "详见《Deep RL》一书")
        out = rr._pangu_text("在<code>code内x字</code>中end")
        self.assertIn("<code>code内x字</code>", out)      # span untouched…
        self.assertIn(f"中{TS}end", out)                  # …text around still spaced
        out = rr._pangu_text("公式$2\\pi r$旁")
        self.assertIn("$2\\pi r$", out)                    # math body pristine
        self.assertIn(TS, out)
        # regression (2026-09-26 review): the bare-tag branch must not eat
        # <code> before the span alternative gets a chance
        out = rr._pangu_text("<code>a内b</code>")
        self.assertEqual(out, "<code>a内b</code>")

    def test_emoji_callouts(self):
        """💡/⚠️ blockquotes (the template's light callout form) must get the
        same classes as the [!TIP]/[!WARNING] form, or the warn border never
        shows and the two spellings of the same intent render differently."""
        self.assertEqual(
            rr.emoji_callouts("<blockquote><p>💡 提示内容</p>"),
            '<blockquote class="callout"><p>💡 提示内容</p>')
        self.assertEqual(
            rr.emoji_callouts("<blockquote><p>⚠️ 注意内容</p>"),
            '<blockquote class="callout warn"><p>⚠️ 注意内容</p>')
        self.assertEqual(
            rr.emoji_callouts("<blockquote><p>普通引用</p>"),
            "<blockquote><p>普通引用</p>")

    def test_chrome_fixes_present(self):
        """The small-chrome batch: h3 secmark (aria-hidden), mobile drawer
        overlay + link-closes, zoom keeps its caption, manual theme toggle."""
        h = self._h()
        out = rr.build_toc_and_ids("<h2>A</h2><h3>B</h3>")[1]
        self.assertIn('<span class="secmark" aria-hidden="true">§</span>', out)
        self.assertIn('id="toc-overlay"', h)             # drawer backdrop
        self.assertIn('id="zoom-cap"', h)                # zoom keeps the caption
        self.assertIn('id="theme-btn"', h)               # manual theme toggle
        self.assertIn(":root[data-theme=\"dark\"]", h)   # three-state palette
        self.assertIn(":root:not([data-theme=\"light\"])", h)
        self.assertIn("closeToc", h)                     # drawer link click closes
        self.assertIn("dr-theme", h)                     # theme persisted


class ChromeV21Test(unittest.TestCase):
    """glance card / numeric-column alignment / heading-rail cleanup /
    \\(..\\) \\[..\\] delimiter normalization."""

    MD = """---
title: "Chrome Test"
read_mode: standard
---

```
╔══════╗
║ 📌 30秒速览
║ 问题: RAG是单轮检索，学不会中途查询
║ 方法: πθ(y|x;R) 把引擎写进RL目标
╚══════╝
```

| 方法 | NQ | Avg |
|------|----|-----|
| RAG | 0.349 | 0.304 |
| SR1 | 0.480 | 0.431 |

## ━━━ 1. 问题背景 ━━━

奖励 $r\\in\\{0,1\\}$ 与块级
\\[r_\\phi(x,y)=\\mathrm{EM}(a_{pred},a_{gold})\\]
收尾。

## ━━━ ⚠️ 矛盾记录 ━━━

无。
"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        md = Path(cls.tmp.name) / "c.md"
        md.write_text(cls.MD, encoding="utf-8")
        cls.h = rr.render_report(md).read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_glance_card(self):
        self.assertIn('<div class="glance">', self.h)
        self.assertIn("<dt>问题</dt>", self.h)
        self.assertNotIn("╔", self.h)

    def test_numeric_columns(self):
        self.assertIn('class="num"', self.h)
        # first column (方法名) must NOT be right-aligned
        import re as _re
        first_cells = _re.findall(r'<td([^>]*)><span class="cv">RAG', self.h)
        self.assertEqual(len(first_cells), 1)
        self.assertNotIn("num", first_cells[0])

    def test_heading_rails_stripped_and_tinted(self):
        import re as _re
        h2s = _re.findall(r"<h2[^>]*>.*?</h2>", self.h, _re.DOTALL)
        self.assertTrue(all("━" not in x for x in h2s))
        self.assertTrue(any("sec-warn" in x for x in h2s))

    def test_delimiter_normalization(self):
        art = self.h.split("<article>")[1].split("</article>")[0]
        self.assertNotIn("\\[", art)                 # \[…\] became an element
        self.assertNotIn("$$", art)                  # and so did $$…$$
        # the TeX is the element's text content: no $ delimiters to mis-read
        self.assertIn('<span class="math-block">r_\\phi(x,y)=', art)
        self.assertIn('<span class="math-inline">r\\in\\{0,1\\}</span>', art)

    # ---------- math pipeline ----------

    def test_math_is_text_content_not_attribute(self):
        """No-JS fallback: the reader sees the TeX itself, never `$…$`."""
        import re as _re
        art = self.h.split("<article>")[1].split("</article>")[0]
        spans = _re.findall(r'<span class="math-(?:inline|block)">(.*?)</span>',
                            art, _re.DOTALL)
        self.assertTrue(spans)
        for s in spans:
            self.assertFalse(s.startswith("$") or s.endswith("$"))
            self.assertNotIn("$$", s)
        self.assertIn("r_\\phi(x,y)=", " ".join(spans))

    def test_nested_stash_is_spliced_not_leaked(self):
        """`$\\begin{bmatrix}…\\end{bmatrix}$` — the env rule used to stash the
        inside of the dollar span first, leaving sentinel `M0005b` in the
        prose."""
        md = ("矩阵 $" + chr(92) + "begin{bmatrix} a & b " + chr(92) * 2 +
              " c & d " + chr(92) + "end{bmatrix}$ 收。\n")
        guarded, store = rr.protect_math(md)
        out = rr.restore_math(guarded, store)
        self.assertNotIn("\x00", out)
        self.assertEqual(out.count('<span class="math-inline">'), 1)
        self.assertIn("\\begin{bmatrix}", out)

    def test_literal_dollar_inside_math_stays_escaped(self):
        guarded, store = rr.protect_math("价格 $\\text{\\$5}$ 与文字\n")
        out = rr.restore_math(guarded, store)
        self.assertIn("\\text{\\$5}", out)           # KaTeX needs the backslash
        self.assertNotIn("\x01", out)


class TexSanitizeTest(unittest.TestCase):
    """LaTeX that LLMs emit and KaTeX rejects, plus the numbering trap."""

    def test_label_and_bm_mapped(self):
        self.assertEqual(rr._sanitize_tex("\\bm{W} x"), "\\boldsymbol{W} x")
        self.assertNotIn("\\label", rr._sanitize_tex("x \\label{eq:1} y"))
        self.assertNotIn("\\nonumber", rr._sanitize_tex("x \\nonumber y"))
        self.assertNotIn("eq:1", rr._sanitize_tex("x \\label{eq:1} y"))

    def test_ams_envs_lose_their_auto_numbers(self):
        """KaTeX numbers every row of `align`/`equation` itself — `(1)`, `(2)`,
        restarting in each block — while the prose cites the paper's numbers."""
        for src, name in (("align", "aligned"), ("align*", "aligned"),
                          ("equation", "aligned"), ("equation*", "aligned"),
                          ("gather", "gathered"), ("multline", "gathered"),
                          ("alignat", "alignedat")):
            B, E = chr(92), chr(92)
            got = rr._sanitize_tex("%sbegin{%s}a%send{%s}" % (B, src, E, src))
            self.assertEqual(got, "%sbegin{%s}a%send{%s}" % (B, name, E, name))
        # an already-inner environment is left exactly as it was
        got = rr._sanitize_tex("\\begin{aligned}a\\end{aligned}")
        self.assertEqual(got, "\\begin{aligned}a\\end{aligned}")

    def test_explicit_tag_kept_as_text(self):
        """`\\tag{3}` is the author quoting the paper's number; KaTeX refuses
        `\\tag` outside a display equation, so it becomes plain parentheses."""
        self.assertEqual(rr._sanitize_tex("x \\tag{3}", display=True),
                         "x \\qquad(3)")
        self.assertEqual(rr._sanitize_tex("\\tag*{A.1}"), "\\qquad(A.1)")
        self.assertEqual(rr._sanitize_tex("x \\tag{} y"), "x  y")

    def test_lone_eol_backslash_repaired_in_display_only(self):
        tex = "a = 1 " + chr(92) + "\nb = 2"
        self.assertIn(chr(92) * 2, rr._sanitize_tex(tex, display=True))
        self.assertNotIn(chr(92) * 2, rr._sanitize_tex(tex, display=False))


class RawHtmlBlockTest(unittest.TestCase):
    """`<div align="center">` around a table is the shape real reports use."""

    MD = """---
title: "Raw HTML"
read_mode: standard
---

<div align="center">

| | |
|---|---|
| **原文** | Some Paper Title |
| **代码** | https://github.com/aimagelab/ReT |

</div>

正文里的裸链接 https://example.org/a 与 `https://code.example` 保持原样。
"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        md = Path(cls.tmp.name) / "raw.md"
        md.write_text(cls.MD, encoding="utf-8")
        cls.h = rr.render_report(md).read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_table_inside_div_becomes_a_table(self):
        self.assertIn('<table class="kv">', self.h)
        self.assertNotIn("|---|---|", self.h)          # no run-on pipe line
        self.assertNotIn("| **原文** |", self.h)

    def test_md_in_html_attribute_added(self):
        # added on the way in; `md_in_html` drops it from the output, which is
        # why the end-to-end assertion above is on the table itself
        got = rr.enable_md_in_html('<div align="center">\n\n| | |\n')
        self.assertIn('<div align="center" markdown="1">', got)
        # an author who already wrote it is not given a second one
        once = rr.enable_md_in_html('<div markdown="1">\n')
        self.assertEqual(once.count("markdown="), 1)

    def test_code_spans_keep_their_html_literal(self):
        md = "看 `" + '<div class="x">' + "` 这个标签。\n"
        self.assertNotIn('markdown="1"', rr.enable_md_in_html(md))

    def test_bare_url_linkified_but_not_in_code(self):
        self.assertIn('href="https://github.com/aimagelab/ReT"', self.h)
        self.assertIn('href="https://example.org/a"', self.h)
        self.assertNotIn('href="https://code.example"', self.h)


class InlineChromeTest(unittest.TestCase):
    """checklists, GitHub alerts, table pipes in code — the small blocks."""

    MD = """---
title: "Chrome"
read_mode: quick
---

- [ ] 待办项 A
- [x] 已完成项 B

> [!WARNING]
> 消融口径是推理期。

> [!NOTE]
> 普通提示。

| 名称 | 备注 |
|------|------|
| `top-1|top-3` | 代码里的竖线 |

| 名称 | 备注 |
|------|------|
| 无表头命中 | $a \\mid b$ |
"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        md = Path(cls.tmp.name) / "c.md"
        md.write_text(cls.MD, encoding="utf-8")
        cls.h = rr.render_report(md).read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_task_list(self):
        self.assertIn('<ul class="tasklist">', self.h)
        self.assertIn('<li class="task done">', self.h)
        self.assertIn('<span class="cb">✓</span>', self.h)

    def test_alerts(self):
        self.assertIn('<blockquote class="callout warn">', self.h)
        self.assertIn('<blockquote class="callout">', self.h)
        self.assertIn("警告", self.h)
        self.assertNotIn("[!WARNING]", self.h)

    def test_pipe_in_code_span_survives(self):
        self.assertIn("<code>top-1|top-3</code>", self.h)

    def test_pipe_inside_math_cell_survives(self):
        self.assertIn("\\mid", self.h)
        self.assertIn('class="math-inline"', self.h)


class FakeCurrencyTest(unittest.TestCase):
    def test_dollar_amounts_not_greedy(self):
        # "$5 和 $100" — inline math regex requires non-space right after $
        # and forbids newline/inner $: "5 和 " spans Chinese but ends at
        # the next $; verify the *pairing* stays sane with real math present.
        # Real behaviour (by design): a math span must open with non-space
        # right after $ AND close with non-space right before $ — "$5 和 " /
        # "$100，公式 " pairs both end on whitespace → only "$x^2$" is math.
        text = "价格 $5 和 $100，公式 $x^2$ 结束"
        guarded, store = rr.protect_math(text)
        self.assertEqual(len(store), 1)
        restored = rr.restore_math(guarded, store)
        self.assertIn("价格 $5 和 $100", restored)
        self.assertIn('<span class="math-inline">x^2</span>', restored)

    def test_space_padded_math_needs_a_strong_signal(self):
        # `$ x_i $` is math; `$ 100 美元 $` is prose between two prices
        self.assertEqual(len(rr.protect_math("记 $ x_i $ 为输入")[1]), 1)
        self.assertEqual(len(rr.protect_math("价格 $ 100 元 $ 左右")[1]), 0)

    def test_escaped_dollar_is_literal(self):
        guarded, store = rr.protect_math("价格 \\$5 不参与配对 $x$ 结束")
        self.assertEqual(len(store), 1)
        self.assertIn("$x$", rr.restore_math(guarded, store)
                      .replace('<span class="math-inline">', "$")
                      .replace("</span>", "$"))


class TemplateMastheadTest(unittest.TestCase):
    """The template writes the masthead IN the body: first h1 = title, then a
    key-value table. It never specified frontmatter, so meta_header (which
    reads frontmatter) rendered an empty meta-grid and the page carried two
    h1s — a broken first screen on every template-following report."""

    TEMPLATE_LIKE = """# 视觉语言导航的层次化规划

<div align="center">

| | |
|---|---|
| **原文** | Hierarchical Planning for Vision-Language Navigation |
| **作者** | 张三、李四 |
| **发表** | CoRL, 2024 |
| **阅读** | 2026-10-03 |

</div>

---

## 1. 问题背景

正文开始。

## 2. 方法

第二节正文。
"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.md = Path(self.tmp.name) / "masthead_report.md"
        self.md.write_text(self.TEMPLATE_LIKE, encoding="utf-8")
        self.html_path = rr.render_report(self.md)
        self.html = self.html_path.read_text(encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_title_promoted_no_double_h1(self):
        self.assertIn("<title>视觉语言导航的层次化规划</title>", self.html)
        self.assertEqual(self.html.count("<h1"), 1)

    def test_kv_table_becomes_meta_grid(self):
        self.assertIn("原文", self.html)
        self.assertIn("张三、李四", self.html)
        self.assertIn("meta-grid", self.html)

    def test_masthead_rows_removed_from_body_flow(self):
        self.assertNotIn("<div align=\"center\">", self.html)

    def test_body_sections_survive(self):
        self.assertIn("正文开始。", self.html)
        self.assertIn("第二节正文。", self.html)

    def test_plain_h1_without_kv_table_is_not_hijacked(self):
        """An h1 with no masthead table under it is content — leave it in the
        body and keep the default page title."""
        md = Path(self.tmp.name) / "plain.md"
        md.write_text("# 一篇普通文档\n\n## 1. 背景\n\n正文。\n", encoding="utf-8")
        rr.render_report(md)
        h = (md.with_suffix(".html")).read_text(encoding="utf-8")
        self.assertIn("<title>论文解读报告</title>", h)
        self.assertIn("<h1>一篇普通文档</h1>", h)


class OldTemplateMastheadTest(unittest.TestCase):
    """Regression on the two existing vault reports: the old template began
    with a placeholder h1 (`# 文献解读报告`) + the 30-second glance box, and
    only then the real `# title` + kv table. promote_body_masthead took the
    FIRST h1 as the masthead and deleted everything from it through the last
    table row — the glance box and the real title vanished from the page,
    and <title> read as the placeholder. The table must promote from the
    h1 directly above it; content before that h1 stays in the body."""

    OLD_TEMPLATE_LIKE = """# 文献解读报告

```
╔══════════════════════════════╗
║  📌 30秒速览                  ║
║  问题: 单模态查询瓶颈          ║
╚══════════════════════════════╝
```

---

# Recurrence-Enhanced Vision-Language Retrieval

<div align="center">

| | |
|---|---|
| **原文** | Recurrence-Enhanced Vision-Language Retrieval |
| **作者** | Davide Caffagni 等 |
| **发表** | CVPR, 2025 |

</div>

---

## 1. 问题背景

正文开始。
"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.md = Path(self.tmp.name) / "old_report.md"
        self.md.write_text(self.OLD_TEMPLATE_LIKE, encoding="utf-8")
        rr.render_report(self.md)
        self.html = self.md.with_suffix(".html").read_text(encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_title_promoted_not_placeholder(self):
        self.assertIn("<title>Recurrence-Enhanced Vision-Language Retrieval</title>",
                      self.html)

    def test_glance_box_survives(self):
        # rendered as the styled glance card; pangu spaces "30秒" -> "30 秒"
        self.assertIn('class="glance"', self.html)
        self.assertIn("单模态查询瓶颈", self.html)

    def test_placeholder_heading_superseded_not_promoted(self):
        """The placeholder h1 line itself is dropped — the promoted real title
        supersedes it — while the glance box that followed it survives."""
        self.assertNotIn("<title>文献解读报告</title>", self.html)
        self.assertNotIn("<h1>文献解读报告</h1>", self.html)
        self.assertEqual(self.html.count("<h1"), 1)  # only the page header

    def test_kv_table_promoted_and_body_sections_survive(self):
        self.assertIn("meta-grid", self.html)
        self.assertIn("Davide Caffagni 等", self.html)
        self.assertIn("正文开始。", self.html)
        self.assertNotIn('<div align="center">', self.html)  # masthead gone


class EmbedFallbackHonestyTest(unittest.TestCase):
    """The footer must state what the page ACTUALLY loads. When --embed-katex
    found no local dist, _katex_assets warned on stderr and degraded to CDN —
    while the page footer still promised 「（内嵌）」, i.e. the artifact
    claimed self-containment exactly when its formulas needed network."""

    def test_missing_local_dist_is_reported_as_cdn_in_the_footer(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = Path(tmp) / "r.md"
            md.write_text("---\ntitle: T\nyear: 2024\n---\n\n"
                          "公式 $x^2$ 与\n\n| a | b |\n|---|---|\n| 1 | 2 |\n",
                          encoding="utf-8")
            out = Path(tmp) / "r.html"
            rr.render_report(md, out_path=out, katex="embed",
                             katex_dir=str(Path(tmp) / "empty"))
            html = out.read_text(encoding="utf-8")
        self.assertIn("并非自包含", html)
        self.assertNotIn("（内嵌）", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
