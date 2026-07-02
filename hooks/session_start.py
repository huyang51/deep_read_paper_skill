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

        # Dynamically load tool list from mcp_server (avoids drift if tools
        # are added/removed in server.py).
        try:
            from mcp_server.server import TOOLS
            lines.extend([
                "",
                "**可用 MCP tools**:",
            ])
            for t in TOOLS:
                # First sentence of description (skip the "humanized" prefix)
                desc = t["description"]
                short = desc.split("。")[0] if "。" in desc else desc
                lines.append(f"- `{t['name']}` — {short}")
        except Exception:
            # Fallback if mcp_server can't be imported
            lines.extend([
                "",
                "**可用 MCP tools**: (run `python -m mcp_server` to see the full list)",
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
