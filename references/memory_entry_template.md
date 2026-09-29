# 记忆条目模板

每篇深度阅读的论文在 `knowledge-base/papers/` 下生成一个结构化摘要文件，用于 Obsidian Vault 长期存储和跨论文对比。

> **模板同步**:此模板与 `references/memory_entry_template.md` 内容保持一致(单一来源是 `references/memory_entry_template.md`,vault 端模板是它的副本)。修改时请同步更新两个文件。

> **⚠️ 零基础可理解性原则**：记忆条目中所有方法名、术语、缩写首次出现时必须有一句话定位（例如"FLMR（一种 2023 年提出的多模态细粒度检索方法）"），便于未来回顾时无需翻原文也能理解。

---

## YAML Frontmatter（文件开头，必须）

```yaml
---
id: N
title: "论文标题"
short_name: "模型简称"
year: YYYY
venue: "会议/期刊"
authors: ["作者1", "作者2"]
method_category: "方法类别"
problem_domain: "问题领域"
keywords: ["关键词1", "关键词2"]
core_contribution: "一句话核心贡献"
novelty_level: incremental | substantial | breakthrough
relations:
  - target: <对方论文ID>
    type: method_similar | problem_related | complementary | evolutionary
    direction: predecessor | successor | peer   # 相对本文：对方是本文的前身 / 后续 / 同期
    note: "一句话关联依据（可留空，但正文 ## 与前人工作的关系 里必须有依据）"
related_papers: []   # relations 的自动投影（双方 ID 自动互加），请勿手写
date_read: YYYY-MM-DD
read_mode: quick | standard | deep   # Phase 0 分诊档位：quick 速览卡；standard 默认档（单上下文一遍通读+统一QA）；deep=超长档（>60页，三组编排+矛盾检测+统一QA）
aliases: ["别名1", "别名2"]
tags: [tag1, tag2]
---
```

## 跨论文关系与图谱箭头约定

- **`relations` 是唯一事实源**：`target`（对方 ID）/ `type`（method_similar | problem_related | complementary | evolutionary）/ `direction`（相对本文：predecessor | successor | peer）/ `note`。规则见 SKILL.md §4.5
- **箭头方向**：旧论文 → 新论文（学术影响流向）。由 `direction` 推导——边写在被声明为 `successor` 的一方
- **`related_papers` 与 `## 后续引用` 都是投影**：由脚本在写入时自动同步（互指条目 + 图谱边），**不要手写**
- **引用 vault 内已有论文**：统一使用**加粗文本**（如 `**FLMR**`），不使用 wikilink——手写 wikilink 会造成重复边或方向错误的边
- **未入库的论文或方法**：同样使用加粗文本（如 `**CLIP**`），避免在图谱中产生幽灵节点

---

# [论文标题]

## 核心问题与动机

> 1段概括：论文要解决什么问题？为什么之前的方法解决不了？作者怎么分析发现的？

## 方法概述与创新点

> 2-3段概括：
> - 方法的核心思想
> - 方法的来源（原创/基于什么改进）
> - 关键创新点（1-3条）
> - 与最相关的前人工作的区别（统一用加粗文本；图谱边由 `relations` 驱动、脚本写入旧论文的 `## 后续引用`）

- **新颖性定位**: [incremental / substantial / breakthrough] — [一句话理由，引用 4.5.1 的判定]

## 主要实验结论

> 1段概括：实验证明了什么？最重要的发现是什么？

## 局限性

1. [局限1]
2. [局限2]
3. [局限3]

## 失败场景

> 引用报告 4.6.2，列出 2-3 个**作者未在文中展示**的推断失效场景。

1. [场景1：触发条件 → 预期表现]
2. [场景2]
3. [场景3]

## 与前人工作的关系

- **基座方法**: **基座论文名**（加粗文本；本文 frontmatter 的 `relations` 里对它有 `direction: predecessor` 声明，图谱边由此生成） — [关系说明]
- **竞争方法**: **竞争论文名**（加粗文本） — [关系说明]
- **继承自**: [核心思想/技术的来源]

## 关键词标签

[标签1], [标签2], [标签3], ...
