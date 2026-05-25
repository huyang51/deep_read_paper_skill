"""UserPromptSubmit hook: detect paper-related keywords and inject search hints.

Reads vault directory from PAPER_KB_VAULT_DIR env var, or settings.json,
or defaults to <cwd>/knowledge-base.
"""
import sys
import json
import re
from pathlib import Path

# Ensure UTF-8 encoding on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))

from mcp_server.config import PAPERS_DIR


def load_keywords() -> list[str]:
    """Load trigger keywords from settings.json. Returns unified list."""
    config_file = SKILL_DIR / "settings.json"
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
            cn = config.get("trigger_keywords_cn", ["论文", "文献"])
            en = config.get("trigger_keywords_en", ["paper", "literature"])
            return cn + en
        except Exception:
            pass
    return ["论文", "文献", "paper", "literature"]


def match_keywords(prompt: str, keywords: list[str]) -> list[str]:
    """Check if prompt contains any trigger keywords."""
    prompt_lower = prompt.lower()
    return [w for w in keywords if w.lower() in prompt_lower]


def _extract_title_from_yaml(content: str, fallback: str) -> str:
    """Extract title from YAML frontmatter with fallback. Tries quoted, unquoted,
    and folded scalar YAML syntax."""
    # Try double-quoted string (with optional escaped quotes inside)
    m = re.search(r'title:\s*"((?:[^"\\]|\\.)*)"', content)
    if m:
        return m.group(1)
    # Try single-quoted string
    m = re.search(r"title:\s*'([^']*)'", content)
    if m:
        return m.group(1)
    # Try unquoted scalar (until newline or comment)
    m = re.search(r'title:\s*([^\n#]+?)\s*\n', content)
    if m:
        return m.group(1).strip()
    return fallback


def _extract_id_from_yaml(content: str, fallback: str) -> str:
    """Extract id from YAML frontmatter. Handles int, string, and quoted forms."""
    m = re.search(r'id:\s*(\d+)', content)
    if m:
        return m.group(1)
    return fallback


def quick_keyword_search(prompt: str) -> list[dict]:
    """Simple word-frequency search over vault papers."""
    results = []
    if not PAPERS_DIR.exists():
        return results

    prompt_lower = prompt.lower()
    for paper_file in PAPERS_DIR.glob("*.md"):
        try:
            with open(paper_file, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            continue

        score = 0
        for word in prompt_lower.split():
            word = word.strip(",.?!()[]{}\"'")
            if len(word) < 2:
                continue
            score += content.lower().count(word)

        if score > 0:
            title = _extract_title_from_yaml(content, paper_file.stem)
            paper_id = _extract_id_from_yaml(content, "?")

            results.append({
                "paper_id": paper_id,
                "title": title,
                "score": score,
            })

    results.sort(key=lambda r: -r["score"])
    return results[:5]


def main():
    try:
        input_data = json.load(sys.stdin)
    except Exception:
        print(json.dumps({}))
        return

    prompt = input_data.get("prompt", "")
    keywords = load_keywords()
    matched = match_keywords(prompt, keywords)

    if not matched:
        print(json.dumps({}))
        return

    results = quick_keyword_search(prompt)

    if not results:
        context = f"\n\n> 检测到论文相关关键词 ({', '.join(matched[:5])})。知识库中暂无匹配论文。使用 `paper_search` MCP tool 进行语义检索。"
    else:
        lines = [
            f"\n\n> 检测到论文相关关键词 ({', '.join(matched[:5])})。知识库中可能相关论文:",
        ]
        for r in results:
            lines.append(f"> - [{r['paper_id']}] {r['title']} (匹配度: {r['score']})")
        lines.append("> 使用 `paper_get({{id}})` 查看详情，或 `paper_search(\"查询\")` 进行语义检索。")
        context = "\n".join(lines)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context
        }
    }

    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
