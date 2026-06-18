# 记忆条目模板

每篇深度阅读的论文在 `knowledge-base/papers/` 下生成一个结构化摘要文件，用于 Obsidian Vault 长期存储和跨论文对比。

> **模板同步**:此模板与 `vault-template/.obsidian/templates/paper-memory-template.md` 内容保持一致(单一来源是 `references/memory_entry_template.md`,vault 端模板是它的副本)。修改时请同步更新两个文件。

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
related_papers: []
date_read: YYYY-MM-DD
aliases: ["别名1", "别名2"]
tags: [tag1, tag2]
---
```

## 图谱箭头约定

- **箭头方向**：旧论文 → 新论文（学术影响流向）
- **新论文 body**：引用旧论文时统一使用**加粗文本**（如 `**FLMR**`），不使用 wikilink。这避免产生新→旧的反向图谱边
- **旧论文 body**：系统在创建新论文时自动在旧论文中追加 `## 后续引用` 小节，包含指向新论文的 `[[wikilink]]`，形成旧→新的图谱箭头
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
> - 与最相关的前人工作的区别（统一用加粗文本，系统自动在旧论文中添加回链 wikilink 来生成图谱箭头）

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

- **基座方法**: **基座论文名**（加粗文本；系统根据 `related_papers` 自动在旧论文中创建回链 wikilink） — [关系说明]
- **竞争方法**: **竞争论文名**（加粗文本） — [关系说明]
- **继承自**: [核心思想/技术的来源]

## 关键词标签

[标签1], [标签2], [标签3], ...
