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
            # utf-8-sig matches mcp_server/config.py: an editor that saved the
            # file as "UTF-8 with BOM" (Notepad's default) used to make this
            # json.load raise, silently downgrading to the hardcoded fallback.
            with open(config_file, "r", encoding="utf-8-sig") as f:
                config = json.load(f)
            cn = config.get("trigger_keywords_cn", ["论文", "文献"])
            en = config.get("trigger_keywords_en", ["paper", "literature"])
            # dict.fromkeys, not plain `+`: with the same word in both tables
            # (settings.example ships "paper" twice) the prompt echoed
            # 「检测到…关键词 (论文, paper, paper)」.
            return list(dict.fromkeys(list(cn) + list(en)))
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
    """Simple word-frequency search over vault papers.

    No cache on purpose: Claude Code runs this hook as a fresh process for
    every user prompt, so a module-level cache could never be hit twice — it
    only ever added bookkeeping.
    """
    results = []
    if not PAPERS_DIR.exists():
        return results

    # Tokenize prompt once (skip short words and punctuation)
    prompt_lower = prompt.lower()
    words = tuple(
        w.strip(",.?!()[]{}\"'") for w in prompt_lower.split() if len(w.strip(",.?!()[]{}\"'")) >= 2
    )

    for paper_file in PAPERS_DIR.glob("*.md"):
        try:
            with open(paper_file, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            continue

        score = 0
        content_lower = content.lower()
        for word in words:
            score += content_lower.count(word)

        if score > 0:
            title = _extract_title_from_yaml(content, paper_file.stem)
            paper_id = _extract_id_from_yaml(content, "?")

            results.append({
                "paper_id": paper_id,
                "title": title,
                "score": score,
            })

    results.sort(key=lambda r: -r["score"])
    results = results[:5]
    return results


def main():
    try:
        input_data = json.load(sys.stdin)
    except Exception:
        print(json.dumps({}))
        return

    # .get() is what raises when stdin holds valid JSON that is not an object
    # (`"hi"`, `[1,2]`), and it sits outside the try above — the hook then died
    # with a traceback instead of emitting the empty result that means "no
    # opinion", which is how a hook is supposed to decline.
    if not isinstance(input_data, dict):
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
        # Plain string, so the old text shipped a literal `{{id}}` into every
        # session transcript — the intended placeholder never existed here.
        lines.append("> 使用 `paper_get(paper_id=<编号>)` 查看详情，或 `paper_search(\"查询\")` 进行语义检索。")
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
