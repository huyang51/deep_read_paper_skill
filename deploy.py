"""deep_read_paper_skill — config generator & deployer (formerly setup.py).

This script ONLY generates/deploys configuration. Package installation and
dependency management live in `pyproject.toml` (`pip install -e .`).

What it does:
  1. Reads `settings.json` from the skill directory (copy `settings.example.json`
     to get started).
  2. Renders `templates/.mcp.json` and `templates/.claude-settings.json`
     with actual SKILL_DIR / PYTHON_CMD paths into `output/`.
  3. If `project_dir` is set in settings.json, also copies the rendered files
     to `<project_dir>/.mcp.json` and `<project_dir>/.claude/settings.json`.

Usage:
  paper-kb-deploy          # after `pip install -e .`
  python deploy.py         # or directly from the repo, no install needed
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SKILL_DIR / "output"
TEMPLATES_DIR = SKILL_DIR / "templates"
SETTINGS_FILE = SKILL_DIR / "settings.json"
SETTINGS_EXAMPLE = SKILL_DIR / "settings.example.json"

# What the MCP server imports at startup. ChromaDB's built-in ONNX embedder
# (all-MiniLM-L6-v2) needs none of torch, so sentence_transformers is only
# required for other models — see probe_python().
SERVER_IMPORTS = ("chromadb", "frontmatter", "watchfiles", "pydantic")
ONNX_EMBEDDER = "all-MiniLM-L6-v2"


def probe_python(python_cmd: str, embedding_model: str = "") -> dict:
    """Return ``{module: "ok" | "<ErrorType>: <msg>"}`` for the server's imports.

    A wrong interpreter is the failure that looks like nothing: the config
    renders fine, and the MCP server plus both hooks die at startup with a
    traceback the user never sees. Probing here turns that into a deploy-time
    warning.
    """
    mods = list(SERVER_IMPORTS)
    if embedding_model and embedding_model != ONNX_EMBEDDER:
        mods.append("sentence_transformers")

    code = (
        "import json\n"
        "out = {}\n"
        f"for m in {mods!r}:\n"
        "    try:\n"
        "        __import__(m)\n"
        "        out[m] = 'ok'\n"
        "    except Exception as e:\n"
        "        out[m] = f'{type(e).__name__}: {e}'\n"
        "print(json.dumps(out))\n"
    )
    try:
        proc = subprocess.run([python_cmd, "-c", code],
                              capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as e:
        return {m: f"{type(e).__name__}: {e}" for m in mods}

    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    detail = (proc.stderr or proc.stdout or "no output").strip().splitlines()
    return {m: (detail[-1][:100] if detail else "no output") for m in mods}


def load_settings() -> dict:
    if not SETTINGS_FILE.exists():
        example = SETTINGS_EXAMPLE.name if SETTINGS_EXAMPLE.exists() else "settings.example.json"
        print(f"[ERROR] Cannot find {SETTINGS_FILE}")
        print()
        print(f"  This file is git-ignored, so a fresh clone does not contain it.")
        print(f"  Create it first:")
        print(f"      cp {example} settings.json")
        print(f"  Then edit the 3 required fields: vault_dir, project_dir, python_cmd,")
        print(f"  and re-run this command.")
        sys.exit(1)

    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)

    settings = {k: v for k, v in raw.items() if not k.startswith("_")}

    if not settings.get("vault_dir"):
        print("[ERROR] settings.json: vault_dir is required and cannot be empty.")
        print("  vault_dir: Absolute path to your Obsidian vault / knowledge base directory.")
        print("  Example: \"D:/Paper_read/knowledge-base\"")
        sys.exit(1)

    python_cmd = settings.get("python_cmd", "python")
    if shutil.which(python_cmd) is None:
        print(f"[WARN] python_cmd '{python_cmd}' is not on PATH;")
        print("       hooks/templates will use this exact string at deploy time.")
        print("       Verify it works in the target environment before deploying.")

    return settings


def render_template(template_path: Path, variables: dict) -> str:
    with open(template_path, "r", encoding="utf-8") as f:
        content = f.read()

    for key, val in sorted(variables.items(), key=lambda kv: -len(kv[0])):
        content = content.replace("{{" + key + "}}", str(val))

    unreplaced = set(re.findall(r"\{\{(\w+)\}\}", content))
    if unreplaced:
        print(f"  [WARN] Unreplaced placeholders in {template_path.name}: {unreplaced}")

    return content


def generate_config():
    print("=" * 55)
    print("  deep_read_paper_skill -- config generator")
    print("=" * 55)
    print()

    if not TEMPLATES_DIR.exists():
        print(f"[ERROR] Templates not found at {TEMPLATES_DIR}")
        print("  You are likely running the console script from a NON-editable install")
        print("  (site-packages copy). Use `pip install -e .`, or run")
        print("  `python deploy.py` from a clone of the skill repository.")
        sys.exit(1)

    settings = load_settings()

    vault_dir = settings.get("vault_dir", "")
    project_dir = settings.get("project_dir")
    python_cmd = settings.get("python_cmd", "python")
    skill_dir = str(SKILL_DIR).replace("\\", "/")

    print(f"  Skill dir   : {skill_dir}")
    print(f"  Vault dir   : {vault_dir}")
    print(f"  Python      : {python_cmd}")
    print(f"  Project dir : {project_dir or '(not set — manual deploy)'}")

    model = settings.get("embedding_model", ONNX_EMBEDDER)
    report = probe_python(python_cmd, model)
    broken = {m: d for m, d in report.items() if d != "ok"}
    if broken:
        print("  [WARN] 该 python 跑不起 MCP server —— 配置写出来也是死的：")
        for m, d in broken.items():
            print(f"         - {m}: {d}")
        print("         修法：换成装齐依赖的解释器，或装上缺的包；只想跑英文库可把")
        print(f"         embedding_model 设为 {ONNX_EMBEDDER}"
              "（ChromaDB 自带 ONNX 嵌入，不需要 torch）。")
        print("         注意换 embedding 模型等于换向量空间，已有 .chromadb 需重建。")
    else:
        print(f"  [OK] python 启动自检通过（{len(report)} 个模块可导入）")
    print()

    variables = {
        "SKILL_DIR": skill_dir,
        "PYTHON_CMD": python_cmd,
    }

    OUTPUT_DIR.mkdir(exist_ok=True)

    mcp_template = TEMPLATES_DIR / ".mcp.json"
    if mcp_template.exists():
        rendered = render_template(mcp_template, variables)
        output_path = OUTPUT_DIR / ".mcp.json"
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(rendered)
        print(f"  [OK] output/.mcp.json")

    claude_template = TEMPLATES_DIR / ".claude-settings.json"
    if claude_template.exists():
        rendered = render_template(claude_template, variables)
        output_path = OUTPUT_DIR / ".claude-settings.json"
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(rendered)
        print(f"  [OK] output/.claude-settings.json")

    print()

    if project_dir:
        project_path = Path(project_dir)
        claude_dir = project_path / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)

        shutil.copy(OUTPUT_DIR / ".mcp.json", project_path / ".mcp.json")
        shutil.copy(OUTPUT_DIR / ".claude-settings.json", claude_dir / "settings.json")
        print(f"  [OK] Deployed to {project_dir}")
    else:
        print("  Next:")
        print(f"  cp output/.mcp.json <project>/.mcp.json")
        print(f"  cp output/.claude-settings.json <project>/.claude/settings.json")
    print()
    print("=" * 55)


def main():
    generate_config()


if __name__ == "__main__":
    main()
