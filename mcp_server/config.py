"""Configuration for paper_kb_mcp. Vault path can be set via:
1. Environment variable PAPER_KB_VAULT_DIR (highest priority)
2. settings.json in the skill directory
3. Default fallback (cwd/knowledge-base)
"""
import os
import json
from pathlib import Path

# Skill base directory (this file is in <skill>/mcp_server/config.py)
SKILL_DIR = Path(__file__).resolve().parent.parent

def _load_vault_dir() -> Path:
    # 1. Environment variable
    env_val = os.environ.get("PAPER_KB_VAULT_DIR")
    if env_val:
        return Path(env_val)

    # 2. settings.json in skill directory
    config_file = SKILL_DIR / "settings.json"
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
            vault_path = config.get("vault_dir")
            if vault_path:
                return Path(vault_path)
        except Exception:
            pass

    # 3. Default: knowledge-base/ in current working directory
    return Path.cwd() / "knowledge-base"

def _load_config_val(key: str, default: str) -> str:
    config_file = SKILL_DIR / "settings.json"
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
            return config.get(key, default)
        except Exception:
            pass
    return default

VAULT_DIR = _load_vault_dir()
PAPERS_DIR = VAULT_DIR / "papers"
REPORTS_DIR = VAULT_DIR / "reports"
INSIGHTS_DIR = VAULT_DIR / "insights"
CHROMA_DIR = VAULT_DIR / ".chromadb"
EMBEDDING_MODEL = _load_config_val("embedding_model", "all-MiniLM-L6-v2")
COLLECTION_NAME = "paper_memories"
