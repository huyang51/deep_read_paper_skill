"""deep_read_paper_skill — config generator & deployer (formerly setup.py).

This script ONLY generates/deploys configuration. Package installation and
dependency management live in `pyproject.toml` (`pip install -e .`).

What it does:
  1. Reads `settings.json` from the skill directory (copy `settings.example.json`
     to get started).
  2. Renders `templates/.mcp.json` and `templates/.claude-settings.json`
     with actual SKILL_DIR / PYTHON_CMD paths into `output/`.
  3. If `project_dir` is set in settings.json, copies the hooks
     (`output/.claude-settings.json`) to `<project_dir>/.claude/settings.json`.
  4. Prints the `claude mcp add --scope user` command that registers the MCP
     server, and with `--register` runs it.

Why the MCP server is no longer deployed as `<project_dir>/.mcp.json`: a
project-scoped server is gated behind a one-time approval that only the user
can give — a repository cannot approve its own servers, so nothing this script
writes can lift it. User scope is not gated, and it also makes the server
available from every directory (the skill repo included), which project scope
never did. Hooks stay project-scoped, since the SessionStart summary is only
wanted where papers are read.

Usage:
  paper-kb-deploy              # after `pip install -e .`
  python deploy.py             # or directly from the repo, no install needed
  python deploy.py --register  # also register the MCP server at user scope
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from mcp_server.hf_offline import prefer_cached_model

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


def generate_config(register: bool = False):
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
        print("         修法：给本 skill 建一个专用 conda 环境（一次建好，长期复用），")
        print("         把该环境 python 的绝对路径填进 settings.json 的 python_cmd：")
        print("           conda create -n paper-kb python=3.10 -y")
        print("           conda activate paper-kb")
        print("           PYTHONNOUSERSITE=1 python -m pip install -r requirements.txt")
        print("         （只想跑英文库、不想装 torch：把 embedding_model 设为 "
              f"{ONNX_EMBEDDER}，")
        print("         它走 ChromaDB 自带 ONNX 嵌入；代价是中文语义检索变弱。）")
        print("         注意换 embedding 模型等于换向量空间，已有 .chromadb 需重建。")
    else:
        print(f"  [OK] python 启动自检通过（{len(report)} 个模块可导入）")

    # Startup cost, not correctness: an uncached model means the first start
    # downloads it, and a blocked huggingface.co means it waits out a timeout
    # first. Worth saying here because the MCP client's own startup timeout
    # (Claude Code: MCP_TIMEOUT, 30s) turns that into "the tools never appear".
    decision = prefer_cached_model(model)
    if not decision.cached:
        print("  [注意] 嵌入模型尚未缓存：首次启动需联网下载，")
        print("         若网络到不了 huggingface.co，会先卡到超时再回退。")
    elif decision.offline:
        print("  [OK] 嵌入模型已在本地缓存 —— 启动跳过 Hub 联网检查")
    else:
        print(f"  [注意] {decision.reason}")
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

    name = mcp_server_name()

    claude_template = TEMPLATES_DIR / ".claude-settings.json"
    if claude_template.exists():
        rendered = render_template(claude_template, variables)
        output_path = OUTPUT_DIR / ".claude-settings.json"
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(rendered)
        print(f"  [OK] output/.claude-settings.json")

    print()

    # Hooks stay project-scoped on purpose: SessionStart injects the knowledge
    # base summary, which is only wanted in the project where papers are read.
    # The MCP server is the opposite — it is a personal, machine-wide tool, so
    # it is registered at user scope instead. See register_mcp_server().
    if project_dir:
        project_path = Path(project_dir)
        claude_dir = project_path / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)

        shutil.copy(OUTPUT_DIR / ".claude-settings.json", claude_dir / "settings.json")
        print(f"  [OK] Hooks deployed to {claude_dir / 'settings.json'}")
        warn_if_shadowed(project_path, name)
    else:
        print("  Next:")
        print(f"  cp output/.claude-settings.json <project>/.claude/settings.json")

    print()
    register_mcp_server(name, skill_dir, python_cmd, register)
    print()
    print("=" * 55)


def mcp_server_name() -> str:
    """The MCP server name, read from the render so the template stays the source."""
    try:
        data = json.loads((OUTPUT_DIR / ".mcp.json").read_text(encoding="utf-8"))
        return next(iter(data["mcpServers"]))
    except (OSError, ValueError, KeyError, StopIteration):
        return "paper_kb_mcp"


def warn_if_shadowed(project_dir: Path, name: str):
    """Flag a project `.mcp.json` that would outrank the user-scope registration.

    Scope precedence is Local > Project > User, so a leftover `.mcp.json` from
    an earlier version of this skill keeps winning — and it is the one that
    sits at "Pending approval" until clicked. Left in place, it silently
    cancels the switch to user scope, which is a confusing way to fail.
    """
    mcp_json = project_dir / ".mcp.json"
    if not mcp_json.exists():
        return
    try:
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    other = [k for k in (data.get("mcpServers") or {}) if k != name]
    if name not in (data.get("mcpServers") or {}):
        return

    print()
    print(f"  [注意] {mcp_json} 里还有一个项目级 '{name}'，它比 user 作用域优先，")
    print("         会继续遮蔽新注册、继续要求 /mcp 批准。")
    if other:
        print(f"         该文件还含 {other} —— 请只删掉 '{name}' 这一项。")
    else:
        print(f"         删掉它：  rm \"{mcp_json}\"")


def registration_command(name: str, skill_dir: str, python_cmd: str):
    """The `claude mcp add` argv that registers the server at user scope.

    No `cwd`: `claude mcp add` has none, and the server does not need one —
    `config.py` resolves settings.json from `__file__`, and PYTHONPATH is what
    makes `-m mcp_server` importable from any directory.
    """
    return ["claude", "mcp", "add", "--scope", "user", name,
            "-e", f"PYTHONPATH={skill_dir}", "--", python_cmd, "-m", "mcp_server"]


def runnable(argv):
    """`argv` with argv[0] resolved enough for this platform to actually launch.

    On Windows, npm installs Claude Code as a `claude.cmd` shim, and
    CreateProcess cannot start a .cmd directly — it needs cmd.exe. A bare list
    would work on a machine with claude.exe and fail on one with the shim.
    Returns None when the command is nowhere on PATH, so the caller can fall
    back to printing it.
    """
    exe = shutil.which(argv[0])
    if exe is None:
        return None
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", exe] + list(argv[1:])
    return [exe] + list(argv[1:])


def register_mcp_server(name: str, skill_dir: str, python_cmd: str, run_it: bool):
    """Tell the user how to register at user scope — or do it, with --register.

    Why user scope: a server delivered by a project `.mcp.json` is gated behind
    a one-time approval, and that gate is not something a repository may lift
    for itself — "A cloned repository can't approve its own servers", so
    approval keys committed to `.claude/settings.json` are ignored until the
    folder is trusted. User scope is not gated: `claude mcp add` is the user's
    own action, so there is nothing left to approve.
    """
    argv = registration_command(name, skill_dir, python_cmd)
    printable = " ".join(f'"{a}"' if " " in a else a for a in argv)

    if not run_it:
        print("  MCP server（user 作用域，注册一次全局可用，不需要 /mcp 批准）:")
        print(f"    {printable}")
        print("  或让本脚本代跑:  python deploy.py --register")
        return

    resolved = runnable(argv)
    if resolved is None:
        print(f"  [WARN] PATH 上找不到 claude 命令，请手动执行：")
        print(f"    {printable}")
        return

    print(f"  $ {printable}")
    try:
        proc = subprocess.run(resolved, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  [WARN] 注册失败: {type(e).__name__}: {e}")
        print("         可手动执行上面这条命令。")
        return
    out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if proc.returncode == 0:
        print(f"  [OK] 已注册到 user 作用域 {out}".rstrip())
    else:
        print(f"  [WARN] claude mcp add 返回 {proc.returncode}: {out}")
        print("         若提示已存在，先 claude mcp remove --scope user " + name)


def main():
    parser = argparse.ArgumentParser(
        description="Generate and deploy the deep_read_paper_skill configuration.")
    parser.add_argument(
        "--register", action="store_true",
        help="也执行 claude mcp add --scope user，把 MCP server 注册进你的用户级配置"
             "（默认只打印命令，不动你的配置）")
    args = parser.parse_args()
    generate_config(register=args.register)


if __name__ == "__main__":
    main()
