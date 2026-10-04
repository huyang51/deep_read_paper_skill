---
name: deep_read_paper_skill
description: |-
  深度阅读学术论文 PDF 并生成中文解读报告。当用户提及"读论文"、"文献阅读"、"论文解读"、"论文分析"、"深度阅读"、"paper reading"、"deep read"、提供论文PDF、想理解论文方法/实验/贡献、或要求对比多篇论文时，必须使用此 skill。也可用于建立文献记忆库、寻找论文间的关联和创新结合点。
---

# Deep Read Paper Skill

## 概述

本 skill 提供系统化的学术论文深度阅读工作流，产出两个核心交付物：
1. **中文文献解读报告** (Markdown 格式)
2. **结构化记忆条目** (持久化存储，支持跨论文对比)

适用于 AI/ML/CV/NLP 等领域的学术论文，也可通用于任何有方法、实验和公式的学术文献。

## 运行环境（前置，先满足再动手）

本 skill 的工具链跑在**它自己的 conda 环境**里（约定名 `paper-kb`），依赖清单的唯一事实源是仓库根的 `requirements.txt`：

```bash
conda create -n paper-kb python=3.10 -y        # 每台机器一次
conda activate paper-kb
PYTHONNOUSERSITE=1 python -m pip install -r requirements.txt
```

- **会话里敲 `python tools/xxx.py` 之前先 `conda activate paper-kb`**——命令命中的是 PATH 上的 python，不激活就会用系统 Python 跑出 `ModuleNotFoundError`。
- **MCP server 与两个 hook 不需要激活**：`deploy.py` 已把 `settings.json` 里 `python_cmd` 的绝对路径写进 user 作用域的 MCP 注册项与项目级 `.claude/settings.json`。
- **MCP server 用 user 作用域注册**（`python deploy.py --register`），不用项目级 `.mcp.json`：项目级 server 必须手动批准一次才启动，而仓库文件无法替自己批准（"a cloned repository can't approve its own servers"），未批准时工具一个都不注册且不报错。
- 默认嵌入模型 `paraphrase-multilingual-MiniLM-L12-v2` 需要 torch，**首次调用会从 HuggingFace 下载约 470MB**（国内网络先设 `HF_ENDPOINT=https://hf-mirror.com`，否则会卡住）。
- 环境不对时 `python deploy.py` 会打印 `[WARN]` 并列出缺哪个模块——照它给的命令建环境，不要靠降级模型绕过。

## 核心理念

- **追问"为什么"**：不止复述内容，更要解释作者每个选择背后的动机
- **追溯学术脉络**：厘清论文基于哪些前人工作，改进点在哪里
- **通俗化表达**：用平实的语言解释复杂方法和公式
- **持久记忆**：每篇论文的核心洞察被存储下来，为未来阅读提供上下文

## ⚠️ 最高原则 —— 零基础可理解性（"Explain like I'm 5"）

> **本 skill 的所有报告必须满足一个核心硬性要求：报告的每一位读者，**无论其专业背景如何**，读完报告后都能理解论文的核心思想、方法和结论。**

**这条原则意味着**：

1. **每个概念首次出现时必须解释**——包括但不限于：
   - 论文中提到的术语（如"晚期交互 late interaction"、"特征融合 feature fusion"）
   - 论文中引用但未解释的方法/模型（如"ColBERTv2"、"Block-Recurrent Transformer"）
   - 缩写（如"MLLM"、"KB-VQA"、"RAG"）

2. **每个变量/符号首次出现时必须定义**——包括：
   - 数学符号（$k$、$d$、$\bar{d}$、$L$、$B$ 等）
   - 张量形状（`[B, L, D]` 中的每个维度含义）
   - 论文自定义的变量（即使是论文中常用的 $E_l^T$、$h_l$ 也要解释）

3. **每个数字/指标首次出现时必须说明**——包括：
   - 表格中的评估指标（R@5、PR@5、nDCG 等）
   - 性能数字的"基准对比"含义（"81.8 比之前的 69.2 高 12.6 个点" → "12.6 个点意味着相对提升 18.2%"）
   - 训练资源数字（"80 GPU 小时" → "相当于 1 块 A100 跑 3.3 天"）

4. **每个引用前人工作时必须说清楚"它是什么"**——不能只说"FLMR"，必须说"FLMR（一种 2023 年提出的多模态细粒度检索方法，[Lin 等, NeurIPS 2023]）"。

5. **专业术语首次出现时标注"通俗解释"**——可用括号、`「」`、或脚注。

**自检清单**（生成报告后必须核对）：
- [ ] 报告中所有缩写首次出现时都有展开
- [ ] 报告中所有符号首次出现时都有定义
- [ ] 报告中所有指标首次出现时都有"是什么/为什么高/为什么低"的解释
- [ ] 报告中所有前人工作都有"一句话定位"（不是只给引用编号）
- [ ] 论文的方法/架构图已以**图片形式**嵌入报告（纯理论论文无图除外），且核心图速览每个解读块都写满了
- [ ] 每张嵌图的解读结论都来自**亲眼 Read 过的裁图**，不是凭正文转述（1.3 视觉核验闭环已执行）
- [ ] 报告中每个**关于其他论文**的事实断言都通过了 `cite_verify`（带核验出处标注），或已显式降级为【未核验——仅模型记忆】；§6 存在性台账已由 `tools/verify_refs.py` 产出且覆盖正文点名的全部外部工作（❓/⏸ 行未被写成"不存在"）
- [ ] §1.4 思维链每一环都有证据标注（原文明述/据数据推断/重构-原文未交代），无一处把推断冒充为作者说过的话
- [ ] 基础知识地图（≤8 概念：定义+接线+深度）已填；每个数据集有卡片和样本示例（或明写给不出）；每个基线有定位与公平性分析、缺席强基线已回答
- [ ] §2.8 方法深入讲解已完成（伪代码/复杂度/数值细节/边界情形）——报告不止通俗版；训练资源账本已填（原文交代值或带出处的区间估算）
- [ ] 类型路由已执行：按 Phase 2『类型路由』表判型，变化维度已按变体回答，不适用子项已写明原因（而非静默跳过）
- [ ] §3.7 统计严谨性台账已覆盖全部比较类数字断言；缺口行标 `[原文未交代]`，未据此推断"不显著/不可信/复现失败"（"论文没报"≠"没做"）
- [ ] 两张假设表已去重：§4.6.1（M 编号）与 §4.9.1（A 编号）条目互斥且完备，跨表互引编号有效，无一条假设被两处重复叙述
- [ ] §4.10 后验影响已由 `paper_citations` 数据填写（或标注外部核验不可用）
- [ ] 统一 QA 已执行且通过（或失败项已修复复审），未决项如实写入"✅ 验收记录"节；（超长档）维度间矛盾全部经仲裁或显式记录，无静默择一
- [ ] HTML 阅读视图已渲染（`reports/<短名>_解读报告.html`，与 md 同名同目录；须在 QA 通过后）
- [ ] 让一个**不熟悉该子领域**的研究生读完后，能在不看原文的情况下复述出方法的核心机制

> **违反这条原则 = 报告失败**。技术深度不等于术语堆砌——能用大白话讲清楚才是真功夫。

---

## Phase 0: 分诊档位（开读之前，2-3 分钟）

> Keshav 三遍法的第一遍本质是**筛选决策**：不是每篇论文都值得 11 维精读，也不都有同样的质量预算。分诊用最少的读取决定档位，把力气花在刀刃上。

**分诊只读**（禁止在此阶段做深度分析——分诊的价值是快）：PDF 第 1-2 页（标题/摘要/贡献列表）+ 最后 1 页（结论）+ 各页开头扫章节标题与图表 caption 计数 + 总页数。**判型**（方法/理论/综述/基准/系统/报告——识别信号见 Phase 2『类型路由』表）用同样这几页即可完成，不额外读页。

**档位决策表**（自上而下取先命中者；**用户指令优先于一切**）：

| 档位 | 触发条件 | 执行路径 |
|------|---------|---------|
| `quick` 速览 | 用户说"先看看/这篇重要吗/速览"；或 5C 初判与用户研究线明显弱相关（`paper_find_related` 无命中 + 主题偏离） | 只做 `references/quickcard_template.md` 速览卡 → Phase 4（read_mode: quick）→ 结束，不走 Phase 1-3 全量流程 |
| `standard` 标准（**默认档**） | 除另两档外的**一切论文**（含常见的 10-40 页论文）——主会话在**单上下文一遍通读**（当前模型 1M 级窗口对整篇论文+关键裁图绰绰有余，无需代理分读） | Phase 1 → Phase 2 全 11 维（单遍串行）→ Phase 3 报告 + **统一 QA 门禁** → Phase 4-5（read_mode: standard） |
| `ultra` 超长档 | 总页数 >60（技术报告/综述/学位论文），或用户**点名**"深读/编排模式" | Phase 1 → Phase 2 **编排模式**（`references/orchestration_prompts.md` 三组任务卡并行 + 分页硬契约）→ Phase 3 装配+矛盾检测 → 统一 QA → Phase 4-5（read_mode 值沿用 `deep`，兼容存量） |

> 设计依据（2026-09-25 实测校准）：多代理分维对普通论文是仪式不是功能——独立视角的价值一个"局外人 QA 代理"即可提供；超长文档才需要分组防止后段失焦。

**执行细则**：
1. 分诊结论（页数/类型/5C 初判/**推荐档位+一句话理由**）用 3-4 行告知用户后**直接按推荐档位执行，不等待确认**——用户改档只需事后说一句"用超长档重读 / 这篇只要速览"
2. 改档重跑时，Phase 1 已产出的逐页文本/裁图资产直接复用，只重排后续任务
3. 同篇从速览升级精读后，更新其记忆条目 `read_mode`（速览卡可保留或删除，以完整报告为准）
4. 分诊阶段的外部判断（"这是 XX 团队的工作"）允许凭记忆，但速览卡按模板要求显式声明"未经核验"

---

## Phase 1: 论文阅读与信息提取

### 1.1 阅读 PDF

**关键警告**：Windows 下 Python 的 `sys.stdout` 默认使用 GBK 编码，**严禁使用 `print()` 直接输出 PDF 文本**（会导致 UnicodeEncodeError 使提取中断，部分页面丢失）。必须写入文件：

```python
import fitz, os, sys
# Windows GBK 防护同样适用于辅助脚本自身（2026-09-25 实战中两次因缺此行打印含
# 特殊字符文本而中断）：任何 print 前必须加
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
pdf_path = r"<pdf_path>"
out_dir = r"<vault_dir>/.extract_tmp"  # 隐藏子目录，避免污染 vault 根
os.makedirs(out_dir, exist_ok=True)
doc = fitz.open(pdf_path)
print(f"Total pages: {len(doc)}")  # 这行打印不会出错（无特殊字符）
for i, page in enumerate(doc):
    text = page.get_text()
    out_path = f"{out_dir}/.pdf_page_{i+1}.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text if text.strip() else "[NO TEXT - image-based page]")
doc.close()
print("Done - all pages extracted")  # 确认完成
```

**逐页全部读取，严禁跳过任何一页。**

> ⚠️ **硬性约束（无法绕过的自检步骤）**：
> 1. 提取 PDF，记录总页数 N
> 2. 分批并行读取全部 N 页
> 3. **读完所有批次后，显式输出「已读取 X/N 页」并核对 X == N**——不一致则立即补读缺失页
> 4. **逐页临时文件（`.extract_tmp/.pdf_page_i.txt`）必须保留到 Phase 3 报告生成完毕后才允许清理**（清理=删除 `.extract_tmp/` 整个目录）——§2.8 数值细节、§3.2 样本转写、§4.x 回查、自检都在后续阶段依赖回看原文；核对通过（第 3 步）前更是禁止清理
>
> **禁止**根据主观判断跳过任何页面（如"这应该是参考文献"、"中间都是附录Figure"）——许多论文将关键实验表、消融研究、训练细节放在参考文献之后的 Appendix 中。上下文压力不是跳页的理由：宁可多读无用的附录页，也不能遗漏正文内容。

先通读全文把握整体结构，再精读关键部分：

- **重点精读**：摘要、引言末尾（贡献列表）、**相关工作**（理解方法定位和对比基准，是方法溯源分析的基础）、方法章节、实验章节（含完整的实验结果表和**消融实验**——消融实验是理解"方法为什么有效"的关键窗口，不可轻视）、结论、**附录中的额外实验与训练细节**
- **快速浏览**：补充材料、参考文献
- **关注标记**：公式编号、图表编号及其所在页码、算法伪代码

### 1.2 记录图表位置

阅读过程中，记录论文中每张图（Figure）和表（Table）的编号及其所在页面，与 1.3 节提取产物的 `manifest.json` 对照确认无遗漏。**表格**用文字引用（"见表 2"）+ 将关键数据转写为 Markdown 表格嵌入报告正文（文字层无损）；**图**必须嵌原图（见 1.3），仅文字转述不可替代看图。

### 1.3 视觉通道：图片提取与嵌图素材（硬性）

**双通道阅读**：文字通道（1.1）负责全文覆盖与数字溯源；视觉通道负责"真的看见图"。架构图/pipeline 图/损失曲线/attention map 的关键信息常常不在正文复述——**没看图，方法解读就是凭文字碎片脑补**。

逐页文本提取完成后，运行图片提取工具（基于 PDF 对象坐标 + caption 锚定的几何裁剪，非像素猜测）：

```bash
python "<skill_dir>/tools/extract_figures.py" \
  --pdf "<PDF 路径>" \
  --outdir "<vault_dir>/attachments/<short_name>/" \
  --prefix <short_name> --dpi 300
```

产出 `{short_name}_p<页码>_fig<编号>.png` 与 `manifest.json`（每张图：页码、caption 原文、裁剪坐标、像素尺寸、类型）。退出码：0 成功 | 2 一张图都没检出（需走规则 5）| 1 参数错误。

**硬性规则**：

1. **表格不嵌图**；**核心图嵌 3-6 张**：方法/架构图 1-2 张 + 最能支撑（或暴露）结论的关键证据图（结果曲线、定性对比、动机示例）。
2. **每张要用的图必须先 Read 亲眼看**——检查完整性（坐标轴/图例/子图/标注有无被截断）、caption 匹配。裁剪不合格时**手动重裁**（坐标取自 manifest 该图的 `rect_pt` 微调）：
   ```bash
   python "<skill_dir>/tools/extract_figures.py" --pdf ... --outdir ... \
     --page <N> --rect <x0,y0,x1,y1> --name fix_fig<K>
   ```
   报告中的任何"图 N 解读"结论必须来自亲眼读到的图，**严禁**凭正文转述或想象写图。
3. 嵌图语法（报告位于 vault 根下的 `reports/`）：
   `![图3：一句话说清这张图在讲什么](../attachments/<short_name>/<文件名>.png)`
4. 会话模型无视觉能力时：嵌图照常（给读者看），图解读开头显式标注"**仅基于原文文字转述，未经视觉核验**"。
5. 扫描件或提取结果为空（退出码 2）：加 `--include-uncaptioned` 重跑导出全部候选簇供人工甄别，仍不行则退回整页 `--page --rect` 手动裁。

### 1.4 处理公式

论文中的公式需要被正确理解。对于每个关键公式：
- 识别公式在文中的编号（如 Eq. (3)）
- 理解每个符号的含义（对照论文中的符号表或上下文）
- 理解公式的物理/数学含义，而不仅是符号层面

---

## Phase 2: 深度分析（11 维度框架）

> **档位分支**：standard 档（默认）由主会话单上下文一遍通读全文与关键图后，按 `references/dimensions.md` 原文串行完成 11 维。**仅超长档（>60 页/点名）**把该文件作为任务卡的"分析要求"唯一来源——按 `references/orchestration_prompts.md` **三组**子代理并行（分页硬契约防重复读），维度卡回收后进 Phase 3 装配。两档共享同一套维度标准：**编排不降低任何一个维度的完成度要求**。

完成阅读后，按 **11 维度框架**进行深度分析。**重要**:本节中所有"报告 §X.Y"引用都对应 `references/report_template.md` 中的实际章节编号,**不是**这里的 §2.X 编号。`report_template.md` 的章节组织是按内容类型(问题/方法/实验/局限/总结)分大节,11 个维度**分散在 5 个大节里**。

**11 维度 → 报告章节映射**:

| 维度编号 | 维度名称 | 对应报告章节 |
|----------|---------|-------------|
| 2.1 | 问题溯源分析 | 报告 §1.1-1.4 |
| 2.2 | 方法溯源分析 | 报告 §2.3 |
| 2.3 | 方法通俗解读 | 报告 §2.1-2.2, §2.4-2.8（§2.3 方法溯源由维度 2.2 产出）, 基础知识地图 |
| 2.4 | 实验设计分析 | 报告 §3.1-3.7（含 §3.2 数据集深析/基线深析块、§2.7 资源账本、§3.7 统计严谨性台账） |
| 2.5 | 局限性分析 | 报告 §4.1-4.4（§4.3 含沉默对照清单） |
| 2.6 | 新颖性审计 (审稿人) | 报告 §4.5 |
| 2.7 | 失败案例 (审稿人) | 报告 §4.6 |
| 2.8 | 拒稿风险 (审稿人) | 报告 §4.7 |
| 2.9 | 反事实检验 (深度理解) | 报告 §4.8 |
| 2.10 | 隐含假设 (深度理解) | 报告 §4.9 |
| 2.11 | 综合判断 (深度理解) | 报告 §4.10-4.11、§5 总结与启示（§4.10 后验影响按"🌐 外部断言核验"规则（dimensions.md）由 `paper_citations` 产出；§5 为全维度结论的收束） |

每个维度下面我会标注它对应报告的哪个章节(→ 报告 §X.Y),方便写作时定位。报告在阅读准备之后、§1 之前另设两个专项节："**📚 基础知识地图**"（维度 2.3 产出：前置概念≤8 个，定义+接线+深度标注）与"**🖼️ 核心图速览**"（1.3 提取的原图 + 维度 2.3/2.4 的读图结论）。

### 维度细则 → `references/dimensions.md`（单一来源）

11 个维度**全部照跑**（类型只改各维"问什么、拿什么当证据"，不减少维度数）。每个维度的完整要求——提问清单、输出格式（改进脉络对比表、张量符号约定表、端到端走通、审稿人三件套、反事实/隐含假设的检验法等）——统一在 `references/dimensions.md`，此处不再重复。执行时按该文件原文；standard 档串行逐维完成，超长档把对应维度原文整段粘进编排任务卡。

### 类型路由（Phase 0 判型，先判型再跑 11 维）

> **类型词表**（与速览卡 Category 一致）：**方法 / 理论 / 综述 / 基准 / 系统 / 报告**。判型依据 = 贡献列表第一条（主贡献）+ 章节骨架 + 图表类型，全在 Phase 0 分诊只读范围内完成。

三条硬规则与**类型 × 维度调整表**（每类型改哪些维度的问法与节名、哪些子项"本类型不适用"）见 `references/dimensions.md`『类型路由』节。要点：类型只改"问什么"不减少维度；混合型按主贡献判型；逐页全读与最高原则不受类型影响。

### 🌐 / 📊 两道跨维度硬检查（出口门，细则见 dimensions.md）

- **🌐 外部断言核验**：凡引用**其他论文**作为事实（方法溯源/新颖性/反事实），写入报告前必须过 `cite_verify`；`uncertain/not_found` 禁止作为事实写入；满 1 年的论文用 `paper_citations` 填 §4.10 后验影响；报告定稿前用 `tools/verify_refs.py` 对点名外部工作批量核验（三态判定，"未收录 ≠ 不存在"）。
- **📊 统计严谨性硬检查**：每条**比较类数字断言**进 §3.7 台账，四问（重复性/显著性/幅度 vs 波动/选择空间），论文没写就标 `[原文未交代]`——是披露缺口标注，不是扣分项；禁止把披露缺口推断成"未做检验"或编造反向结论。

---

## Phase 3: 生成解读报告

报告严格按照 `references/report_template.md` 的结构生成。核心要求：

### 3.1 报告格式

- 使用 Markdown 格式
- 中文撰写（专业术语可保留英文，但首次出现需标注中文含义）
- 公式使用 LaTeX 语法（`$...$` 用于行内，`$$...$$` 用于块级）

### 3.2 报告质量要求

- **深度而非长度**：每个分析点都要有实质内容，避免空泛的概述
- **逻辑连贯**：章节之间应有逻辑递进，读者能自然跟随作者思路
- **举证充分**：每个观点都应有论文中的具体内容作为支撑
- **图为实、文为证**：核心图必须按 1.3 规范嵌入原图裁剪（经视觉核验），表格转写 Markdown 并文字引用编号；禁止用大段文字转述替代本应嵌入的图
- **面向读者**：假设读者有一定基础但不熟悉该具体领域，兼顾专业性和可读性

### 3.3 装配与矛盾检测（仅超长档）

三组维度卡回收后，主会话通读并**按模板装配成报告**（装配不是拼接：过渡与衔接由主会话完成）。装配时**逐项核对 `orchestration_prompts.md` 的六对张力表**；分歧三选一：仲裁卡裁决 / 写入"⚠️ 矛盾与仲裁记录"节 / 确认某卡有误（修正留痕）。**禁止静默择一。** standard 档跳过本节（单上下文无维度卡），主会话自查张力后直接定稿。

### 3.4 统一 QA 门禁（standard / 超长档收尾各执行一次，不可跳过）

**QA 派发前**先跑 Phase 2"外部断言核验"第 4 条的存在性门（`tools/verify_refs.py`），产出台账并入报告 §6——QA 事实面第 3 项要对照它，台账缺席 = 该项直接 REWORK。

报告定稿前派发**一个**全新上下文的审计代理（任务卡见 `orchestration_prompts.md`"统一 QA 卡"）——事实面（数字/锚点抽查 15 处、三态覆盖、外部核验覆盖、统计台账覆盖、假设清单 M/A 去重、图解读来源）、可读面（陌生研究生五问、术语抽查 6 处）与类型面（按『类型路由』核对：变化维度是否已按类型回答、不适用子项是否写明原因）**合并一趟完成**。REWORK 项修复后**只复审失败项一次**；仍有未决 → 如实写入"✅ 验收记录"节。**QA 未通过不得进入 Phase 4。**

> 为什么是一个而不是两个：独立视角的价值在于"局外人"存在，与其数量无关（2026-09-25 实测校准）。

### 3.5 报告保存

报告保存到 vault 的 `reports/` 目录下，文件名为 `{short_name}_解读报告.md`（如 `ReT_解读报告.md`）。如 vault 不可用，保存到用户指定的位置。

### 3.6 HTML 阅读视图渲染（standard/deep 完成后执行）

md 报告落盘后（必须在**统一 QA 通过、定稿之后**）渲染配套 HTML：

```bash
python "<skill_dir>/tools/render_report.py" --md "<vault>/reports/{short_name}_解读报告.md"
```

- 输出与 md **同名同目录**的 `.html`：图片相对路径（`../attachments/...`）原样有效，零拷贝
- 自带：frontmatter 元信息卡、侧边目录（h2/h3 自动生成）、KaTeX 公式渲染、表格/引用块/代码样式、暗色模式与打印样式、`[[wikilink]]` 转样式化文本
- **公式**：`$…$` / `$$…$$` / `\[…\]` / 裸 `\begin{align}` 都识别；TeX 作为元素文本内容存放，每个公式单独 `katex.render()`（不依赖 `$` 配对启发式）。KaTeX 未加载或语法错误时**降级为可读的 LaTeX 源码**，不显示 `$` 噪声
- **表格**：booktabs 横线风格、数值列右对齐（tabular-nums）、无表头表转键值表、窄屏（≤620px）≥3 列自动转卡片；单元格内公式与代码中的 `|` 均不受影响
- **公式引擎三档**（默认第一档）：
  1. CDN 多镜像（jsDelivr → npmmirror → staticfile → unpkg，逐个回退）
  2. `--fetch-katex` 一次性下载到本地缓存后，用 `--embed-katex` 内嵌 JS/CSS/字体（单文件 ~700KB，完全离线可开）
  3. `--offline` 完全不加载 KaTeX（公式显示为源码，其余不受影响）
- 渲染失败（退出码 1）不阻塞完成流程：修复 md 后重试，或如实告知用户 HTML 未生成
- quick 档速览卡默认不渲染

---

## Phase 4: 记忆系统

每次深度阅读完成后，将论文的核心信息存入 Obsidian Vault 知识库。

### 4.1 记忆存储位置

知识库存储在 `settings.json` 中配置的 Vault 目录下。标准结构如下：

```
<vault_dir>/
├── index.md                  # Dataview 动态索引
├── papers/                   # 各论文的结构化摘要（YAML frontmatter + wikilinks）
│   └── <short_name>.md       # 如 ReT.md, PreFLMR.md
├── reports/                  # 完整解读报告
│   └── <short_name>_解读报告.md
└── insights/                 # 跨论文创新见解
    └── <见解标题>.md
```

### 4.2 创建 Vault Paper 文件

> 🚨 **本节会写入跨论文关系与图谱边**——`relations` 声明什么方向，图谱边就落在哪篇论文里（非时间顺序阅读不再需要手动修正）。索引完成后**必须**运行 `python tools/verify_graph_arrows.py` 复核。详见 §4.5。

**仅使用以下两种方式之一，严禁同时使用两种方式（会导致同一 ID 生成两个文件）**。

**方式 A（推荐）：通过 CLI 脚本调用 paper_index**

使用 Bash 工具执行 index_paper.py 脚本，该脚本通过文件传递数据（避免 Windows 下 stdin/stdout 管道 GBK 编码问题）：

```bash
python "<skill_dir>/tools/index_paper.py" \
  --title "论文标题" \
  --short_name "ReT" \
  --year 2025 \
  --venue "CVPR" \
  --authors "作者1,作者2" \
  --method_category "方法类别" \
  --problem_domain "问题领域" \
  --keywords "kw1,kw2,kw3" \
  --core_contribution "一句话核心贡献" \
  --relations '[{"target":1,"type":"evolutionary","direction":"predecessor","note":"本文沿用它的双塔结构"},{"target":3,"type":"method_similar","direction":"peer","note":"同期并行工作"}]' \
  --date_read "2026-05-15" \
  --read_mode "deep" \
  --aliases "别名1,别名2" \
  --tags "tag1,tag2" \
  --body_file "<tmp_body_file>.md"
```

脚本自动完成 ID 分配、文件创建、ChromaDB 索引，并把 `--relations` 声明的关系落地成三件事：**对方论文里的互指条目**（`direction` 自动取反）、**双方 `related_papers` 投影**、**按声明方向放置的 `## 后续引用` 图谱边**（写在被声明为 `successor` 的一方）。输出 JSON 格式结果到 stdout；`relations_synced.unresolved_targets` 非空表示指向的论文尚未入库——索引对方论文后重跑一次即可补齐。

> **`--relations` 是唯一的关系入口**：`related_papers` 是它的投影，写不出来也不再接受输入。关系较多时 `--relations` 接受一个 JSON 文件路径（避免命令行转义地狱）。
>
> 返回里 `vector_index: "failed: …"` 表示向量索引没建起来（常见原因：嵌入模型未安装/未下载），**论文文件与关系已经正常落盘**——按 warning 里的提示修好环境后重跑一次索引即可，不要因为这一项重写论文文件。

**方式 B（备选）：手动创建 Markdown 文件**

仅当 CLI 脚本不可用时使用。直接在 `papers/` 目录下创建 Markdown 文件。文件监控会自动触发增量索引，但**不会创建额外文件**（watcher 仅更新向量索引，不写磁盘）。

创建前务必确认：
- papers/ 目录下没有同 ID 的现有文件（使用 `ls papers/` 检查）
- ID 使用 `<vault_dir>/papers/` 下的最大 ID + 1

**YAML frontmatter**（位于文件开头，`---` 包裹）：
```yaml
---
id: <自增ID>
title: "论文标题"
short_name: "模型简称"
year: <年份>
venue: "会议/期刊"
authors: ["作者1", "作者2"]
method_category: "方法类别"
problem_domain: "问题领域"
keywords: ["关键词1", "关键词2"]
core_contribution: "一句话核心贡献"
relations:
  - target: <对方论文ID>
    type: method_similar | problem_related | complementary | evolutionary
    direction: predecessor | successor | peer   # 相对本文：对方是本文的前身 / 后续 / 同期
    note: "一句话关联依据（可留空，但正文 ## 与前人工作的关系 里必须有依据）"
related_papers: [<关联论文ID列表——有 relations 时由脚本自动投影，请勿手写>]
date_read: <YYYY-MM-DD>
read_mode: quick | standard | deep
aliases: ["别名1", "别名2"]
tags: [tag1, tag2]
---
```

**Body 内容**：与原有结构化摘要相同，但需注意：

- **正文里引用 vault 中已有的论文**：统一用**加粗文本**（如 `**FLMR**`）——wikilink 边由 `relations` 驱动、由脚本写入 `## 后续引用` 小节，正文再手写一个只会造成重复边或反向边
- **`## 后续引用` 小节由脚本维护**：哪篇论文带这段、里面有哪些 `[[wikilink]]`，全部由双方 `relations` 的 `direction` 推导（旧论文 → 新论文）。**不要手写或手工搬移这个段落**
- **对尚未阅读、vault 中不存在的论文或方法**，同样使用加粗文本（如 `**CLIP**`），避免在图谱中产生幽灵节点
- body 内容保持轻量，只存核心理解和关系信息，不存公式细节和实验数据（详见报告）

### 4.3 保存解读报告

完整解读报告使用 Write 工具保存到 vault 的 `reports/<short_name>_解读报告.md`（如 `reports/ReT_解读报告.md`）。

### 4.4 更新 index.md

在 vault 的 `index.md` 中无需手动更新——Dataview 插件会自动从 YAML frontmatter 中读取并动态生成索引。确认新 paper 的 YAML frontmatter 字段完整即可。

### 4.5 跨论文关联

> 🚨 **必读 — 三大硬性规则**（按重要性排序，违反任一 = 知识库污染）
>
> 1. **相关性优先**：声明 `relations` 前**必须**调用 MCP 工具 `paper_find_related` 找候选，人工核验后才写
> 2. **`relations` 是唯一事实源**：`related_papers` 与 `## 后续引用` 图谱边都是它的投影，**禁止手改**（手改会在下次同步被覆盖）
> 3. **方向是数据，不是阅读顺序**：`direction` 由你按两篇论文的实际关系声明，脚本据此把图谱边放到正确的一侧

#### 4.5.1 规则 1：相关性优先（最重要的规则）

**声明 `relations` 之前必须先用 `paper_find_related` 工具找候选**。该工具按可信度分两档返回，每个候选带 `source` 字段：
- `declared` — 已有 `relations` 声明（最高）
- `inferred` — 共享关键词推断：method_category / problem_domain / keywords 的重叠数（需 ≥ 2 个才视为相关）

`related_papers` 不是候选来源：它只是 `relations` 的投影，读它等于读已经声明过的东西。

工具返回的每个候选都包含：
- `relation_type`：`method_similar` / `problem_related` / `complementary` / `evolutionary`
- `direction`：`predecessor` / `successor` / `peer`（相对被查询的论文）
- `note`：声明里写的关联依据（若有）
- `shared_keywords`：具体共享了哪些关键词

**禁止** 仅凭 Agent 直觉声明关系——必须以工具返回的候选为基础。

**完整工作流**：
1. 写完论文 body，**先不填** `relations`
2. **临时索引**论文（拿到 ID）
3. 调用 `paper_find_related(new_paper_id)` 找出所有候选
4. **人工核验**每个候选：
   - `relation_type` 是否合理（method_similar / problem_related / complementary / evolutionary）
   - `shared_keywords` 是否真的重叠（不只是字符串匹配）
   - 是否真的有学术联系（不是同名巧合）
5. 核验通过的候选写成 `relations` 条目（`target` / `type` / `direction` / `note`）——**`direction` 是相对本文的**：对方是本文的前身写 `predecessor`，对方是本文的后续工作写 `successor`，同期并行写 `peer`
6. 重新索引（`index_paper.py --relations` 或 MCP 工具 `paper_index`）——互指条目、`related_papers` 投影与图谱边自动同步

> ⚠️ **不能直接照搬工具结果**——工具是基于字符串匹配启发式，可能误判。人工核验是最后一道防线。

#### 4.5.2 规则 2：方向与图谱边（由声明驱动）

**图谱箭头方向：旧论文 → 新论文**（学术影响流向）。这条约定没变，变的是**谁来保证它**：

| 事项 | 旧流程（迁移前） | 现在 |
| --- | --- | --- |
| 方向判定 | 比较年份，人工判断谁旧谁新 | 你声明 `direction`，脚本按声明落边 |
| 图谱边 | 脚本假定"被索引的论文是最新的"，非时间顺序阅读时**手工搬移** `## 后续引用` | 边写在**被声明为 `successor` 的一方**（即更早那篇）的 body 里，与阅读顺序无关 |
| 互指条目 | 手工在双方 `related_papers` 互加 ID | 声明一侧即自动写入对方（`direction` 自动取反） |
| `peer` 关系 | 无对应概念，只能当"无关联" | 明确声明 `peer`：双向互指、**不产生图谱边**（并行工作没有影响方向） |

四类关系的判定口径（写 `type` 时对照）：
- **方法相似** `method_similar`：两篇论文使用或改进相似的方法
- **问题相通** `problem_related`：两篇论文解决的是同一类问题
- **互补关系** `complementary`：论文A的方法可用于改进论文B的某个模块
- **发展关系** `evolutionary`：论文A是论文B的基座/前身，或反过来

例：先读 2024 的 ReT，再读 2023 的 UniIR。索引 UniIR 时声明 `{"target": <ReT 的 id>, "type": "evolutionary", "direction": "successor"}`——脚本把 `[[ReT]]` 写进 **UniIR** 的 `## 后续引用`（而不是像旧脚本那样误写进 ReT），并在 ReT 里写入 `direction: predecessor` 的互指条目。

> ⚠️ **声明与年份冲突**（preprint、v1/v2、合并记录等）：报 `year_conflict` **提示**，不阻断。以声明为准，必要时在 `note` 里写明原因。
>
> ⚠️ **正文里手写的 wikilink 仍是错误来源**：脚本写入的边不会出错，但你在正文随手写一个 `[[ReT]]` 就会造出重复边或方向错误的边。引用 vault 内论文一律用**加粗文本**。

**核查**：关联完成后运行
```bash
python tools/verify_graph_arrows.py
```
脚本按可信度递减做三层检查：① `relations` 完整性（互指对称、类型一致、目标存在、方向与年份、依据是否齐备，以及 `related_papers` 是否仍等于投影——不等就是手改或旧 vault 未迁移，报 ERROR）② 箭头方向（此时报错基本意味着**手写链接**，不是同步失败）③ 每条关系是否有正文依据。有问题时 exit code 1。

**关联方式**：
1. 通过 `index_paper.py --relations` / MCP 工具 `paper_index` 的 `relations` 参数声明；脚本同步互指条目与图谱边
2. **新论文** body 中引用旧论文用**加粗文本**（如 `**PreFLMR**`），**不要**用 wikilink
3. 依据仍然写在正文 `## 与前人工作的关系` / 方法概述里（`verify_graph_arrows.py` 第 ③ 层检查它）；`note` 只是索引卡上的一句话
4. 如有有价值的跨论文创新见解，在 vault 的 `insights/` 下创建 insight 文件，frontmatter 中 `source_papers` 列表（与 `.obsidian/templates/insight-template.md` 一致，index.md 的 Dataview 也按它取列）按年份从早到晚排列；正文引用论文同样用**加粗文本**，不用 wikilink

#### 4.5.3 旧 vault 迁移

已有 vault 若只有 `related_papers` 和手写 `## 后续引用`（没有 `relations`），**必须先迁移再索引**：运行时只读 `relations`，`related_papers` 不再作为输入被读取，未迁移的笔记一旦被写入就会按投影重建（那些没有声明覆盖的 ID 随之丢弃）。`verify_graph_arrows.py` 与 `migrate_relations.py --check` 会以 **ERROR**（exit 1）报 `unmigrated`，不会让你错过这一步。迁移工具**默认只出计划，不写盘**：

```bash
python tools/migrate_relations.py            # 扫描并打印迁移计划（安全，不写盘）
python tools/migrate_relations.py --apply    # 写入 relations + 互指条目 + 图谱边
python tools/migrate_relations.py --check    # 只做关系完整性校验
python tools/migrate_relations.py --set-type 1=complementary --apply   # 类别推断不对时纠正
```

迁移口径：`related_papers` 行按年份推断方向，计划里标注"（按年份推断）"；`## 后续引用` 里的链接方向**本来就已被约定写明**（链接意味着"对方在我之后"），因此恢复为 `direction: successor` 而不猜；`related_papers` 里指向未入库论文的 ID 报为幽灵节点，**不静默丢弃**。exit code：0 干净 / 2 有待迁移项或提示 / 1 有错误。

**权威顺序**：已声明的 `relations` > `## 后续引用` 链接 > 年份推断。`related_papers` 只是投影（只有 ID，没有类别），所以**已声明的类别不会被重新推断覆盖**——重跑迁移不会把 `complementary` 改回 `method_similar`，声明为 `peer` 的关系也不会被遗留的链接重新拉出一条箭头。推断出的类别是猜的（`method_category` + `problem_domain` 相等即判方法相似），若正文写的是互补/发展，用 `--set-type <对方论文ID>=<类别>` 在**计划阶段**钉住：它同时钉住指向该论文的行和该论文自己声明的行，两侧不会各写一个类别（同一对给两个类别会被拒绝，exit 1）。计划末尾会逐行标注来源（沿用已声明值 / 方向由链接恢复 / 按年份推断），apply 前照此核对。

### 4.6 Obsidian 图谱视图

图谱默认配置中 `showArrow: true`，连线带箭头以显示引用方向。为保持视图整洁，`reports/`、`insights/`、`.obsidian/templates/` 目录通过 `search` 过滤串隐藏（用户可在图谱设置的搜索框中手动切换），论文节点经 `colorGroups` 以主色高亮、一眼可辨：

```json
{
  "search": "-path:reports/ -path:insights/ -path:.obsidian/templates/",
  "colorGroups": [
    { "query": "path:papers/", "color": { "a": 1, "rgb": 3900150 } }
  ]
}
```

注意：Obsidian 的 graph 配置**没有 `filters` 键**——隐藏靠 `search` 过滤串、着色靠 `colorGroups`，不要写不存在的键（会被整体忽略，视图行为与配置看似不符）。

papers 目录下的文件以 `short_name` 命名（如 `ReT.md`），在图谱中显示为干净的模型名节点。各论文的裁图存于 vault 的 `attachments/<short_name>/`（PNG + manifest.json，见 1.3），`graph.json` 的 `showAttachments: false` 使其不进入图谱视图，仅供报告相对路径引用。

图谱中论文节点之间的**边只来自 `## 后续引用` 小节**（由 `relations` 驱动、脚本维护，见 4.5.2）——正文里的加粗文本不产生边，手写的 wikilink 会产生不受控的边。

---

## Phase 5: 跨论文对比与创新建议

如果知识库中已有至少一篇论文，读完新论文后执行跨论文对比分析，并在报告中追加一个小节：

### 5.1 关联论文识别

扫描 vault `papers/` 目录中所有论文的 YAML frontmatter（keywords、method_category、problem_domain），找出与当前论文高度相关的论文。也可使用 MCP tool `paper_find_related` 辅助检索。

### 5.2 对比维度

对比分析从以下维度展开：
- **方法维度**：方法结构、核心公式、训练策略的异同
- **问题维度**：解决的问题是否相同/相似/互补
- **实验维度**：使用的数据集、评估指标是否可比较
- **改进路径维度**：改进思路的异同（是否从同一基座出发、是否借鉴了相同的中间工作）

### 5.3 创新结合建议

基于对比分析，提供 1-3 个可能的创新结合方向：
- 论文A的方法A + 论文B的模块B → 可能的改进方案C
- 论文A的损失函数 + 论文B的架构 → 新的混合模型
- 论文A在数据集X上的发现 + 论文B在任务Y上的方法 → 跨领域应用

建议需有具体的技术可行性说明，而非空洞的"A+B"。

**必须**创建 insight 文件到 `insights/` 目录（参照 `vault-template/.obsidian/templates/insight-template.md`），包含核心洞察、创新方案、待验证假设。

### 5.4 关联更新

- 把 5.1-5.3 识别出的关联写成 `relations` 条目（**必须先执行 4.5.1 的候选核验**），再重新索引该论文：互指条目、`related_papers` 投影与 `## 后续引用` 图谱边由脚本按 `direction` 放置
- **禁止**手改 `related_papers` 或 `## 后续引用`：它们是 `relations` 的投影，手改会在下次同步时被覆盖，且 `verify_graph_arrows.py` 会以 **ERROR** 报 `projection_drift`（列出没有声明覆盖的条目）
- 方向由声明决定，与阅读顺序无关；`peer` 关系双向互指但不产生图谱边

---

## 注意事项

1. **PDF 质量**：如果 PDF 是扫描版（PyMuPDF 无法提取文字），告知用户需要使用 OCR 工具；此时视觉通道按 1.3 规则 5 降级处理（整页读图/手动裁剪），图仍要嵌入报告
2. **非常规论文**：Phase 0 判型（方法/理论/综述/基准/系统/报告）后按 Phase 2『类型路由』表调整各维度问法与报告节名——类型**不减少维度数**、不放松逐页全读；表中未列的类型（position paper / tutorial / 元分析等）按"方法"骨架跑全 11 维，并在报告元信息类型行标注"（存疑）"
3. **数学密集型论文**：直觉先行不变（§2.1-2.2 先建立理解），但关键公式的**推导链必须完整**（§2.6 推导链行）——原文跳步处依据原文 Appendix 补全或报告补证，逐条标注来源；**禁止**以"数学直觉"为由省略核心公式的中间代数步骤。确实非核心的公式可以不选入 §2.6，但须在节首说明取舍
4. **记忆维护**：阅读 5 篇以上论文后，回顾早期论文的 `relations` 是否仍然准确（新论文可能改变"发展关系"的判定），改完用 `python tools/migrate_relations.py --check` 或 `tools/verify_graph_arrows.py` 复核；**不要**手工编辑 `related_papers` / `## 后续引用`（它们是投影，会被同步覆盖）
5. **完成报告**：全部阶段完成后，仅回复"完成"，不附加任何过程检查项（如"无 Read 调用、无编码错误、无重复文件"等）。**standard/超长档的"完成"以统一 QA 通过为前提**；若有未决项，回复"完成（有未决项，见验收记录）"并给一行摘要
6. **档位纪律**：分诊结论告知后直接执行不等待确认；**默认走 standard 单上下文通读，不要主动升超长档**；超长档三组任务卡必须同一条消息内并行派发（勿串行），派发前完成 `orchestration_prompts.md`"派发前必做"六条（绝对路径 / 视觉分工 / 台账补验 / 页码契约 / **分页硬契约**——分组读页清单列死、并集覆盖全页、清单外禁读 / **类型透传**——论文类型与该类型的维度调整行注入每张卡）；`.dimcards/` 保留不删
7. **HTML 是构建产物**：`reports/*.html` 由 `render_report.py` 生成，**禁止手工编辑**；md 报告任何修改后必须重跑渲染同步

## 参考资源

- 报告模板：`references/report_template.md`
- 维度细则（Phase 2 单一来源）：`references/dimensions.md`（11 维提问清单/输出格式 + 类型路由调整表 + 🌐/📊 两道硬检查）
- 记忆条目模板：`references/memory_entry_template.md`
- 图片提取工具：`tools/extract_figures.py`（几何裁剪 + caption 锚定，用法与硬性规则见 1.3；单元测试：`tests/test_extract_figures.py`）
- 外部引用核验：MCP 工具 `cite_verify` / `paper_citations`（OpenAlex + Semantic Scholar，实现于 `mcp_server/cite_api.py`，离线测试 `tests/test_cite_api.py`；使用规则见 `references/dimensions.md`"🌐 外部断言核验"）；批量存在性门 `tools/verify_refs.py`（点名外部工作 → §6 台账，规则见同节第 4 条；测试 `tests/test_verify_refs.py`）
- HTML 阅读视图渲染器：`tools/render_report.py`（md 定稿 → 同名 .html，KaTeX/目录/嵌图；测试 `tests/test_render_report.py`；用法见 Phase 3.6）
- 跨论文关系：结构化 `relations` frontmatter（唯一事实源，规则见 4.5；实现 `mcp_server/relations.py`）；体检 `tools/verify_graph_arrows.py`、旧 vault 迁移 `tools/migrate_relations.py`（离线测试 `tests/test_relations.py`）
- 分诊速览卡模板：`references/quickcard_template.md`（quick 档唯一产出）
- 超长档编排任务卡：`references/orchestration_prompts.md`（三组维度卡 + 分页硬契约 + 装配矛盾检测 + 统一 QA 卡 + 仲裁卡；仅 >60 页或点名编排时加载，普通论文用 standard 档不需本文件）
