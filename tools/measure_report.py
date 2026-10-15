#!/usr/bin/env python3
"""篇幅预算检查器：算出解读报告的「主线 / 深度附录」体量，并对照模板预算。

用法:
    python tools/measure_report.py <报告.md|目录> [...]

规则来源: references/report_template.md『✍️ 篇幅预算』与『〔📎 附录〕标记约定』

- 深度附录 = 标题带 〔📎 附录〕 的 H3，**或**属于附录的 H2（§6 / 附：跨论文对比 / ✅ 验收记录）
  下的全部子节（H3 继承父 H2 的附录身份）
- 主线 = 其余部分；预算 ≤450 行（约 1 小时一次性读完）
- 单节预算：主线小节 ≤35 行、附录小节 ≤70 行

退出码: 0 = 全部达标 | 1 = 有超预算项
"""
import os
import re
import sys

MAIN_SEC_BUDGET = 35
APP_SEC_BUDGET = 70
MAIN_TOTAL_BUDGET = 450
CHARS_PER_MIN = 400  # 中文阅读速度（含标点）

APPENDIX_H2_PREFIXES = ("## ━━━ 6.", "## ━━━ 附：", "## ━━━ ✅")
BADGE = "〔📎 附录〕"


def count_prose(lines):
    """单节预算只数**散文行**——表格行与围栏代码块不计入（模板『篇幅预算』明文）。"""
    n = 0
    in_fence = False
    for l in lines:
        if l.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or l.lstrip().startswith("|"):
            continue
        n += 1
    return n


def measure(path):
    lines = open(path, encoding="utf-8").read().split("\n")
    marks = [(i, l) for i, l in enumerate(lines) if re.match(r"^#{2,3} ", l)]
    parent_app = False
    main = app = 0
    sections = []  # (title, n_lines, prose_lines, is_appendix)
    for k, (i, l) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(lines)
        n = end - i
        if l.startswith("## "):
            parent_app = (BADGE in l) or l.startswith(APPENDIX_H2_PREFIXES)
        is_app = parent_app or (BADGE in l)
        sections.append((l.lstrip("# ").strip(), n, count_prose(lines[i:end]), is_app))
        if is_app:
            app += n
        else:
            main += n
    text = open(path, encoding="utf-8").read()
    minutes = round(len(re.findall(r"[^\s]", text)) / CHARS_PER_MIN * main / max(len(lines), 1))
    return main, app, minutes, sections


def main(argv):
    targets = []
    for a in argv:
        if os.path.isdir(a):
            targets += [os.path.join(a, f) for f in sorted(os.listdir(a))
                        if f.endswith("_解读报告.md")]
        else:
            targets.append(a)
    if not targets:
        print(__doc__)
        return 2

    bad = 0
    print(f"{'报告':<24}{'主线行':>7}{'≈分钟':>7}{'附录行':>7}   判定")
    for t in targets:
        mainl, appl, mins, secs = measure(t)
        ok_total = mainl <= MAIN_TOTAL_BUDGET
        over_sec = [(s, n) for s, _raw, n, a in secs if n > (APP_SEC_BUDGET if a else MAIN_SEC_BUDGET)]
        if not ok_total or over_sec:
            bad += 1
        flag = "✅" if (ok_total and not over_sec) else "❌"
        note = []
        if not ok_total:
            note.append(f"主线超 {mainl - MAIN_TOTAL_BUDGET} 行 / 超 {mins - 60} 分钟")
        for s, n in over_sec:
            note.append(f"单节超预算: {s[:26]} = {n} 行散文")
        print(f"{os.path.basename(t)[:-3]:<24}{mainl:>7}{mins:>7}{appl:>7}   {flag} {'; '.join(note)}")
    print(f"\n预算: 主线 ≤{MAIN_TOTAL_BUDGET} 行 | 主线小节 ≤{MAIN_SEC_BUDGET} 行 | 附录小节 ≤{APP_SEC_BUDGET} 行")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
