---
created: YYYY-MM-DD
tags: [index]
---
# 论文知识库索引

## 概览

- **总论文数**: `$= dv.pages('"papers"').length`
- **最近更新**: `$= dv.pages('"papers"').map(p => p.date_read).sort().last()`

---

## 全部论文

```dataview
TABLE title, year, venue, method_category, date_read
FROM "papers"
SORT date_read DESC
```

---

## 按方法类别

```dataview
TABLE title, year, problem_domain
FROM "papers"
SORT method_category ASC, year DESC
```

---

## 按问题领域

```dataview
TABLE title, year, method_category
FROM "papers"
SORT problem_domain ASC, year DESC
```

---

## 创新见解

```dataview
TABLE source_papers, tags
FROM "insights"
SORT date DESC
```

---

## 最近阅读

```dataview
TABLE title, core_contribution, date_read
FROM "papers"
SORT date_read DESC
LIMIT 5
```
