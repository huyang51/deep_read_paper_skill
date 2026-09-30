"""Configuration for paper_kb_mcp. Vault path can be set via:
1. Environment variable PAPER_KB_VAULT_DIR (highest priority)
2. settings.json in the skill directory
3. Default fallback (cwd/knowledge-base)
"""
import os
import json
import logging
from pathlib import Path

# Skill base directory (this file is in <skill>/mcp_server/config.py)
SKILL_DIR = Path(__file__).resolve().parent.parent
SETTINGS_FILE = SKILL_DIR / "settings.json"

# Set when settings.json exists but cannot be used. The process still starts —
# see the note in _read_settings — and server.py turns this into a message that
# reaches the user instead of a silent wrong-vault.
CONFIG_ERROR = None


def _read_settings() -> dict:
    """Parsed settings.json, or {} when the file is absent.

    A *missing* file is a documented state: a fresh clone has none, deploy.py
    explains how to create it, and the env var / cwd fallbacks below are meant
    to cover it.

    A *present but unusable* file is the opposite, and used to be swallowed by a
    bare `except Exception: pass`. That silently redirected the entire vault to
    `<cwd>/knowledge-base`: semantic search answered "no papers", and newly
    indexed papers were written into a directory nobody had configured — with no
    error anywhere to explain it. Since a user-scope MCP registration has no cwd
    to speak of, that path was not even stable between sessions.

    So the failure is recorded instead of discarded. It is not raised here
    because importing this module must not kill the server before the handshake
    — a process that dies pre-`initialize` registers zero tools and shows the
    user nothing. server.py reads CONFIG_ERROR and reports it through the tools,
    which is where a user can actually see it.
    """
    global CONFIG_ERROR
    if not SETTINGS_FILE.exists():
        return {}
    try:
        # utf-8-sig: a BOM (Notepad, some editors) is otherwise a JSON syntax
        # error, which is a confusing way to be told your paths are unsupported.
        with open(SETTINGS_FILE, "r", encoding="utf-8-sig") as f:
            config = json.load(f)
    except (OSError, ValueError) as e:
        CONFIG_ERROR = (
            f"{SETTINGS_FILE} 读取失败：{type(e).__name__}: {e}\n"
            f"提示：Windows 路径在 JSON 里要用正斜杠（\"D:/papers/...\"），"
            f"反斜杠是转义字符，写成 \"D:\\papers\" 会解析失败。"
        )
        return {}
    if not isinstance(config, dict):
        CONFIG_ERROR = f"{SETTINGS_FILE} 的顶层必须是一个 JSON 对象（{{...}}）。"
        return {}
    return config


def _load_vault_dir() -> Path:
    # 1. Environment variable
    env_val = os.environ.get("PAPER_KB_VAULT_DIR")
    if env_val:
        return Path(env_val)

    # 2. settings.json in skill directory
    config = _read_settings()
    vault_path = config.get("vault_dir")
    if vault_path:
        return Path(vault_path)

    # 3. Default: knowledge-base/ in current working directory
    fallback = Path.cwd() / "knowledge-base"
    if not CONFIG_ERROR:
        logging.getLogger("paper_kb_mcp").warning(
            f"settings.json 未配置 vault_dir，暂用 {fallback} —— "
            f"论文会索引到那里。跑一次 `python deploy.py` 生成配置。"
        )
    return fallback


def _load_config_val(key: str, default: str) -> str:
    return _read_settings().get(key, default)


VAULT_DIR = _load_vault_dir()
PAPERS_DIR = VAULT_DIR / "papers"
REPORTS_DIR = VAULT_DIR / "reports"
INSIGHTS_DIR = VAULT_DIR / "insights"
CHROMA_DIR = VAULT_DIR / ".chromadb"
EMBEDDING_MODEL = _load_config_val("embedding_model", "paraphrase-multilingual-MiniLM-L12-v2")
COLLECTION_NAME = "paper_memories"
