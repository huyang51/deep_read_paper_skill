"""Docs-structure tripwires for the SKILL.md / references split.

Phase 2's detail moved to ``references/dimensions.md`` (2026-10, SKILL.md was
794 lines). Nothing mechanically keeps the two in sync — these checks fail
loudly if the single-source contract drifts: a dimension renamed or dropped
on one side, the pointer removed, or the orchestration doc still pasting from
the wrong file.
"""
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _read(*parts):
    return (REPO.joinpath(*parts)).read_text(encoding="utf-8")


class DimensionsSplitTest(unittest.TestCase):
    def test_skill_points_at_dimensions_md(self):
        skill = _read("SKILL.md")
        self.assertIn("references/dimensions.md", skill)

    def test_dimensions_md_carries_all_eleven_dimensions(self):
        dims = _read("references", "dimensions.md")
        for i in range(1, 12):
            with self.subTest(dimension=i):
                self.assertRegex(dims, rf"(?m)^### 2\.{i} ")

    def test_dimensions_md_keeps_each_dimension_mandated_output(self):
        """Truncation tripwire: 11 section HEADERS existing proves little —
        losing the tensor-table out of 2.3 or the silence-checklist out of
        2.5 keeps the heading and guts the dimension. Keywords are the
        artifacts SKILL/模板/QA gates reference by name."""
        dims = _read("references", "dimensions.md")
        per_dimension = {
            "2.1": "思维链",               # chain-of-thought evidence rule
            "2.2": "改进脉络对比表",        # lineage-comparison table
            "2.3": "张量符号",              # tensor symbol table + walk-through
            "2.5": "沉默对照",              # silent-comparison checklist
            "2.7": "失效场景",              # failure scenarios (M-numbered)
            "2.8": "rebuttal",              # rejection risk + rebuttal plans
            "2.9": "反事实",                # counterfactual conditions
            "2.10": "隐含假设",             # implicit assumptions (A-numbered)
            "2.11": "行动地图",             # action map
        }
        for dim, kw in per_dimension.items():
            with self.subTest(dim=dim):
                self.assertIn(kw, dims)

    def test_dimensions_md_carries_routing_and_hard_checks(self):
        dims = _read("references", "dimensions.md")
        self.assertIn("### 类型路由", dims)
        self.assertIn("🌐 外部断言核验", dims)
        self.assertIn("📊 统计严谨性硬检查", dims)
        # the per-type table rows the orchestrator pastes verbatim
        for t in ("理论", "综述", "基准", "系统", "报告"):
            self.assertIn(f"| **{t}**", dims)

    def test_skill_no_longer_duplicates_dimension_details(self):
        skill = _read("SKILL.md")
        self.assertNotIn("### 2.1 问题溯源分析", skill)
        # the pointer may name the table; the table itself must not be here
        self.assertNotIn("| 类型 | 识别信号（分诊可判） | 维度调整 |", skill)

    def test_orchestrator_sources_from_dimensions_md(self):
        orch = _read("references", "orchestration_prompts.md")
        self.assertIn("references/dimensions.md", orch)
        self.assertNotIn("SKILL.md` Phase 2 原文", orch)

    def test_readme_has_what_goes_where_table(self):
        for name in ("README.md", "README_CN.md"):
            with self.subTest(readme=name):
                readme = _read(name)
                self.assertIn("dimensions.md", readme)
                self.assertIn("render_report.py", readme)


if __name__ == "__main__":
    unittest.main()
