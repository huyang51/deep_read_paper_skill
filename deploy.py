"""deep_read_paper_skill — config generator & deployer (formerly setup.py).

This script ONLY generates/deploys configuration. Package installation and
dependency management live in `pyproject.toml` (`pip install -e .`).

What it does:
  1. Reads `settings.json` from the skill directory (copy `settings.example.json`
     to get started).
  2. Renders `templates/.claude-settings.json` with actual SKILL_DIR /
     PYTHON_CMD paths into `output/`.
  3. If `project_dir` is set in settings.json, copies the hooks
     (`output/.claude-settings.json`) to `<project_dir>/.claude/settings.json`.
  4. Prints the `claude mcp add --scope user` command that registers the MCP
     server, and with `--register` runs it.
  5. Checks that the repo sits in a skills discovery path
     (~/.claude/skills/ or <project_dir>/.claude/skills/) — anywhere else and
     SKILL.md never triggers, no matter how correct the rest of the install is.

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
import re
import shutil
import subprocess
import sys
from pathlib import Path

from mcp_server import SERVER_IMPORTS
from mcp_server import config
from mcp_server.console import force_utf8, spawnable
from mcp_server.hf_offline import prefer_cached_model

# Shared with bootstrap.py, the hooks and the tools/ so the encoding policy
# cannot drift per file (three of the copies used to omit errors="replace").
force_utf8()

SKILL_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SKILL_DIR / "output"
TEMPLATES_DIR = SKILL_DIR / "templates"
SETTINGS_FILE = SKILL_DIR / "settings.json"
SETTINGS_EXAMPLE = SKILL_DIR / "settings.example.json"

# The placeholder vault_dir in settings.example.json, verbatim.
SETTINGS_EXAMPLE_VAULT = "D:/my-papers/knowledge-base"

# The registered server's name. Used to come out of templates/.mcp.json, which
# this script stopped deploying along with project-scope registration; warn_if_
# shadowed() still looks for that file on disk, because a leftover from an older
# version of this installer outranks the user-scope registration.
MCP_SERVER_NAME = "paper_kb_mcp"

# ChromaDB's built-in ONNX embedder needs none of torch, so
# sentence_transformers is only required for other models — see probe_python().
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
        print("  This file is git-ignored, so a fresh clone does not contain it.")
        print("  Create it first:")
        print(f"      cp {example} settings.json")
        print("  Then edit the 3 required fields: vault_dir, project_dir, python_cmd,")
        print("  and re-run this command.")
        sys.exit(1)

    # utf-8-sig, matching mcp_server/config.py and bootstrap.py: Notepad's
    # "UTF-8" save adds a BOM, the server read that file fine while deploy
    # died with a raw JSONDecodeError traceback on the very same settings.
    with open(SETTINGS_FILE, "r", encoding="utf-8-sig") as f:
        raw = json.load(f)

    settings = {k: v for k, v in raw.items() if not k.startswith("_")}

    if not settings.get("vault_dir"):
        print("[ERROR] settings.json: vault_dir is required and cannot be empty.")
        print("  vault_dir: Absolute path to your Obsidian vault / knowledge base directory.")
        print("  Example: \"D:/Paper_read/knowledge-base\"")
        sys.exit(1)

    # The example file ships literal placeholders. Left unedited, deploy would
    # create D:/my-papers/.claude on Windows — or, on Linux where that string is
    # a *relative* path, a directory literally named "D:" inside the cwd. Nobody
    # wants either, and nothing else in the flow would have said a word.
    if settings.get("vault_dir") == SETTINGS_EXAMPLE_VAULT:
        print(f"[ERROR] settings.json 还是模板里的示例路径（{SETTINGS_EXAMPLE_VAULT}）。")
        print("        请把 vault_dir / project_dir / python_cmd 改成你自己的路径后重跑。")
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
        # Every placeholder lives INSIDE a JSON string (see templates/), so the
        # value must be inserted as JSON string CONTENT. settings.json legally
        # spells "python_cmd": "D:\\Anaconda\\…"; raw str(val) injection wrote
        # a single backslash into the rendered file and the output was invalid
        # JSON — the deploy self-check at json.loads(...) then crashed on our
        # own artifact. (config.py tells users to use forward slashes; that is
        # advice, not a validation deploy may assume.)
        content = content.replace("{{" + key + "}}",
                                  json.dumps(str(val))[1:-1])

    unreplaced = set(re.findall(r"\{\{(\w+)\}\}", content))
    if unreplaced:
        print(f"  [WARN] Unreplaced placeholders in {template_path.name}: {unreplaced}")

    return content


def generate_config(register: bool = False, hf_endpoint: str = ""):
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

    # The model the *server* will actually load, not a guess from this file.
    # deploy.py used to fall back to the ONNX model when settings.json omitted
    # embedding_model, while mcp_server/config.py falls back to the multilingual
    # one — so the preflight probed the wrong model and printed [OK] on an
    # interpreter with no torch, which then died at startup. This is the very
    # failure the preflight exists to catch, so it reads the server's own value.
    model = config.EMBEDDING_MODEL or ONNX_EMBEDDER
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
        print("  [注意] 嵌入模型尚未缓存：首次索引调用需联网下载。")
        print("         若网络到不了 huggingface.co，注册时加：")
        print("           --hf-endpoint https://hf-mirror.com")
        print("         （模型由 server 进程下载，外层 shell 里 export HF_ENDPOINT")
        print("         到不了它，必须注册时注入。）")
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

    name = MCP_SERVER_NAME

    claude_template = TEMPLATES_DIR / ".claude-settings.json"
    if claude_template.exists():
        rendered = render_template(claude_template, variables)
        output_path = OUTPUT_DIR / ".claude-settings.json"
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(rendered)
        print("  [OK] output/.claude-settings.json")

    print()

    # Hooks stay project-scoped on purpose: SessionStart injects the knowledge
    # base summary, which is only wanted in the project where papers are read.
    # The MCP server is the opposite — it is a personal, machine-wide tool, so
    # it is registered at user scope instead. See register_mcp_server().
    if project_dir:
        project_path = Path(project_dir)
        if not project_path.is_dir():
            # Not created on purpose. A typo'd or not-yet-existing project_dir
            # would otherwise get a .claude/ tree planted under it, pointing at
            # hooks for a project that does not exist.
            print(f"  [注意] project_dir 不存在，跳过 hooks 部署：{project_path}")
            print("         改对路径后重跑（MCP 注册与它无关，不受影响）。")
        else:
            claude_dir = project_path / ".claude"
            claude_dir.mkdir(parents=True, exist_ok=True)
            ours = json.loads((OUTPUT_DIR / ".claude-settings.json")
                              .read_text(encoding="utf-8"))
            deploy_project_settings(claude_dir / "settings.json", ours)
            warn_if_shadowed(project_path, name)
    else:
        print("  Next:")
        print("  cp output/.claude-settings.json <project>/.claude/settings.json")

    print()
    register_mcp_server(name, skill_dir, python_cmd, register, hf_endpoint)
    print()
    check_skill_discovery(project_dir)
    print("=" * 55)


def _hook_scripts(entries) -> set:
    """Hook script filenames referenced by one event's entries.

    Identity is the script name, not the whole command line: the interpreter
    path in front of it is a user's to change, and a re-run must recognize its
    own hook through an edited command rather than installing a second copy.
    """
    found = set()
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        for hook in (entry.get("hooks") or []):
            if isinstance(hook, dict):
                found.update(re.findall(r"([\w.-]+\.py)", str(hook.get("command", ""))))
    return found


def merge_hook_config(existing: dict, ours: dict):
    """Fold our hooks into a settings dict. Returns ``(merged, added_events)``.

    Everything not named `hooks` is carried through untouched, and per event an
    entry is only appended when its script is not already referenced — so a
    second deploy is a no-op instead of a duplicate.
    """
    merged = dict(existing)
    hooks = dict(merged.get("hooks") or {})
    added = []
    for event, entries in (ours.get("hooks") or {}).items():
        current = list(hooks.get(event) or [])
        present = _hook_scripts(current)
        for entry in entries:
            if _hook_scripts([entry]) & present:
                continue
            current.append(entry)
            added.append(event)
        hooks[event] = current
    merged["hooks"] = hooks
    return merged, added


def deploy_project_settings(dest: Path, ours: dict):
    """Install the hooks into a project's settings.json without destroying it.

    The old code was a bare shutil.copy over the destination, which silently
    replaced whatever the project already had — permissions, model, env, its own
    hooks. Users are told to point project_dir at a real project root, and real
    project roots already have this file, so the documented path ran straight
    into the one irreversible mistake in the whole flow.
    """
    if not dest.exists():
        dest.write_text(json.dumps(ours, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        print(f"  [OK] Hooks deployed to {dest}")
        return

    try:
        existing = json.loads(dest.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        print(f"  [WARN] {dest} 已存在但无法解析（{type(e).__name__}: {e}），没有动它。")
        print("         请手动把 output/.claude-settings.json 的 hooks 合并进去。")
        return
    if not isinstance(existing, dict):
        print(f"  [WARN] {dest} 的顶层不是 JSON 对象，没有动它。")
        return

    merged, added = merge_hook_config(existing, ours)
    if not added and merged == existing:
        print(f"  [OK] Hooks 已在 {dest} 中，无需改动")
        return

    backup = dest.with_name(dest.name + ".bak")
    shutil.copy(dest, backup)
    dest.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    print(f"  [OK] Hooks 已合并进 {dest}（原有配置保留，备份 {backup.name}）")


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


def registration_command(name: str, skill_dir: str, python_cmd: str,
                         hf_endpoint: str = ""):
    """The `claude mcp add` argv that registers the server at user scope.

    No `cwd`: `claude mcp add` has none, and the server does not need one —
    `config.py` resolves settings.json from `__file__`, and PYTHONPATH is what
    makes `-m mcp_server` importable from any directory.

    hf_endpoint is baked in as an env of the registration itself, because the
    *server process* is the one that downloads the embedding model: exporting
    HF_ENDPOINT in the shell that runs deploy.py does not reach it. (The lazy
    init reads the variable again inside the worker, so a re-registration is
    all it takes — no settings.json change.)
    """
    argv = ["claude", "mcp", "add", "--scope", "user", name,
            "-e", f"PYTHONPATH={skill_dir}"]
    if hf_endpoint:
        argv += ["-e", f"HF_ENDPOINT={hf_endpoint}"]
    return argv + ["--", python_cmd, "-m", "mcp_server"]


def register_mcp_server(name: str, skill_dir: str, python_cmd: str, run_it: bool,
                        hf_endpoint: str = ""):
    """Tell the user how to register at user scope — or do it, with --register.

    Why user scope: a server delivered by a project `.mcp.json` is gated behind
    a one-time approval, and that gate is not something a repository may lift
    for itself — "A cloned repository can't approve its own servers", so
    approval keys committed to `.claude/settings.json` are ignored until the
    folder is trusted. User scope is not gated: `claude mcp add` is the user's
    own action, so there is nothing left to approve.
    """
    argv = registration_command(name, skill_dir, python_cmd, hf_endpoint)
    printable = " ".join(f'"{a}"' if " " in a else a for a in argv)

    if not run_it:
        print("  MCP server（user 作用域，注册一次全局可用，不需要 /mcp 批准）:")
        print(f"    {printable}")
        print("  或让本脚本代跑:  python deploy.py --register")
        return

    resolved = spawnable(argv)
    if resolved is None:
        print("  [WARN] PATH 上找不到 claude 命令，请手动执行：")
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


def skill_installed_where() -> Path:
    """The skills directory Claude Code should discover this skill from.

    Personal skills live in ~/.claude/skills/<name>/ (SKILL.md is the marker);
    project skills in <project>/.claude/skills/<name>/. Personal wins as the
    recommendation: the skill reads papers wherever the user points it, not
    just inside one project.
    """
    return Path.home() / ".claude" / "skills" / "deep_read_paper_skill"


def check_skill_discovery(project_dir):
    """Warn when the repo is somewhere Claude Code will never look for skills.

    Every other deployment step (env, settings.json, MCP registration, hooks)
    only makes the *tools* work. The skill itself — the thing that makes "读这
    篇论文" trigger the whole workflow — is discovered in exactly two places,
    and a repo cloned to ~/code/deep_read_paper_skill is in neither. Without
    this check the user finishes a 7-step install into a skill that can never
    fire, with no error anywhere.
    """
    try:
        repo = SKILL_DIR.resolve()
    except OSError:
        return
    bases = [Path.home() / ".claude" / "skills"]
    if project_dir:
        bases.append(Path(project_dir) / ".claude" / "skills")
    for base in bases:
        try:
            repo.relative_to(base.resolve())
            print(f"  [OK] Skill 位于发现路径内：{base}")
            return
        except (ValueError, OSError):
            continue

    target = skill_installed_where()
    print("  [注意] 本仓库不在 Claude Code 的 skills 发现路径里 —— 上面所有步骤装完，")
    print("         skill 也永远不会触发（触发词就找不到 SKILL.md）。")
    print("         把它放进 skills 目录（个人级，所有项目可用）：")
    print(f'           git clone <本仓库URL> "{target}"')
    print("         （或直接移动现有克隆；移动后必须在新位置重跑：")
    print("            python deploy.py --register  —— settings.json 与 MCP 注册都")
    print("            记着旧路径，不重跑 MCP server 会指向已被删掉/过期的目录。）")
    print("         项目级（仅当前项目可用）：")
    if project_dir:
        print(f'           移动到 "{Path(project_dir) / ".claude" / "skills" / "deep_read_paper_skill"}"')
    else:
        print("           <project>/.claude/skills/deep_read_paper_skill")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Generate and deploy the deep_read_paper_skill configuration.")
    parser.add_argument(
        "--register", action="store_true",
        help="也执行 claude mcp add --scope user，把 MCP server 注册进你的用户级配置"
             "（默认只打印命令，不动你的配置）")
    parser.add_argument(
        "--hf-endpoint", default="",
        help="把 HF_ENDPOINT 写进 MCP 注册项（如 https://hf-mirror.com）。"
             "嵌入模型由 server 进程下载，只在外层 shell export 到不了它——"
             "必须注册时注入。对已有注册：先 claude mcp remove --scope user 再重跑。")
    args = parser.parse_args()
    generate_config(register=args.register, hf_endpoint=args.hf_endpoint)


if __name__ == "__main__":
    main()
