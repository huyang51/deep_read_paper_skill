"""SessionStart hook: inject recent paper summaries into Claude Code context.

Reads vault directory from PAPER_KB_VAULT_DIR env var, or settings.json,
or defaults to <cwd>/knowledge-base.
"""
import sys
import json
from pathlib import Path

# Ensure UTF-8 encoding on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))

from mcp_server.config import PAPERS_DIR
from mcp_server.markdown_parser import get_all_papers


def main():
    papers = get_all_papers()
    papers.sort(key=lambda p: str(p.get("date_read", "")), reverse=True)
    recent = papers[:3]

    if not recent:
        context = (
            "## 知识库状态\n"
            "- 总论文数: 0\n"
            "- 知识库中暂无论文。使用 deep_read_paper_skill 阅读新论文，"
            "或使用 paper_search MCP tool 检索相关知识。"
        )
    else:
        total = len(papers)
        lines = [
            "## 知识库状态",
            f"- 总论文数: {total}",
            "- 最近阅读:",
        ]
        for p in recent:
            title = p.get("title", "Unknown")
            date_read = p.get("date_read", "N/A")
            contrib = p.get("core_contribution", "")
            pid = p.get("id", "?")
            lines.append(f"  - [{pid}] **{title}** ({date_read}): {contrib}")

        lines.extend([
            "",
            "**可用 MCP tools**:",
            "- `paper_search` — 语义搜索论文",
            "- `paper_get` — 获取论文详情",
            "- `paper_find_related` — 查找关联论文",
            "- `paper_search_by_method` — 按方法类别搜索",
            "- `paper_index` — 创建/更新论文条目",
            "- `paper_remove` — 删除论文条目",
            "- `paper_index_stats` — 知识库统计",
        ])
        context = "\n".join(lines)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context
        }
    }

    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
