"""deep_read_paper_skill config generator.

Usage:
  1. Edit settings.json (the ONLY config file you need to touch)
  2. Set project_dir to your Claude Code project path
  3. Run: python setup.py
     - If project_dir is set: configs auto-deployed to your project
     - If project_dir is null: configs generated to output/ (copy manually)
"""
import json
import re
import shutil
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SKILL_DIR / "output"
TEMPLATES_DIR = SKILL_DIR / "templates"


def load_settings():
    settings_file = SKILL_DIR / "settings.json"
    if not settings_file.exists():
        print(f"[ERROR] Cannot find {settings_file}")
        sys.exit(1)

    with open(settings_file, "r", encoding="utf-8") as f:
        raw = json.load(f)

    settings = {}
    for k, v in raw.items():
        if not k.startswith("_"):
            settings[k] = v

    # Validate required settings
    if not settings.get("vault_dir"):
        print("[ERROR] settings.json: vault_dir is required and cannot be empty.")
        print("  vault_dir: Absolute path to your Obsidian vault / knowledge base directory.")
        print("  Example: \"D:/Paper_read/knowledge-base\"")
        sys.exit(1)

    # Validate python_cmd is executable (best effort — skip on Windows where
    # the configured command may not be on the PATH used during setup)
    import shutil
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

    # Warn about unreplaced placeholders
    unreplaced = set(re.findall(r'\{\{(\w+)\}\}', content))
    if unreplaced:
        print(f"  [WARN] Unreplaced placeholders in {template_path.name}: {unreplaced}")

    return content


def main():
    print("=" * 55)
    print("  deep_read_paper_skill -- config generator")
    print("=" * 55)
    print()

    settings = load_settings()

    vault_dir = settings.get("vault_dir", "")
    project_dir = settings.get("project_dir")
    python_cmd = settings.get("python_cmd", "python")
    skill_dir = str(SKILL_DIR).replace("\\", "/")

    print(f"  Skill dir   : {skill_dir}")
    print(f"  Vault dir   : {vault_dir}")
    print(f"  Python      : {python_cmd}")
    print(f"  Project dir : {project_dir or '(not set — manual deploy)'}")
    print()

    variables = {
        "SKILL_DIR": skill_dir,
        "PYTHON_CMD": python_cmd,
    }

    OUTPUT_DIR.mkdir(exist_ok=True)

    # Generate .mcp.json
    mcp_template = TEMPLATES_DIR / ".mcp.json"
    if mcp_template.exists():
        rendered = render_template(mcp_template, variables)
        output_path = OUTPUT_DIR / ".mcp.json"
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(rendered)
        print(f"  [OK] output/.mcp.json")

    # Generate .claude-settings.json
    claude_template = TEMPLATES_DIR / ".claude-settings.json"
    if claude_template.exists():
        rendered = render_template(claude_template, variables)
        output_path = OUTPUT_DIR / ".claude-settings.json"
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(rendered)
        print(f"  [OK] output/.claude-settings.json")

    print()

    # Auto-deploy to project if project_dir is configured
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


if __name__ == "__main__":
    main()
