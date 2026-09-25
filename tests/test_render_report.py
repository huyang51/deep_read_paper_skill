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
        self.assertIn("$x^2$", restored)


if __name__ == "__main__":
    unittest.main(verbosity=2)
