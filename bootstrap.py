#!/usr/bin/env python3
"""One-command setup for a fresh clone.

    conda create -n paper-kb python=3.10 -y && conda activate paper-kb
    PYTHONNOUSERSITE=1 python -m pip install -r requirements.txt
    python bootstrap.py --vault D:/papers/knowledge-base --register

Why this exists
---------------
The manual path has one step that is easy to get wrong and impossible to notice
afterwards: `python_cmd` in settings.json must be the *absolute* path of the
interpreter that has the dependencies. Pointing it at system Python produces a
config that renders fine, prints no error, and dies at MCP startup with a
traceback nobody sees — the symptom is "those tools do not exist". But the
person running this script is already inside the right interpreter, so the
answer is simply `sys.executable`; they should not have to be told to run
`which python` and paste the result into JSON.

What it does, in order:

  1. checks the *running* interpreter for the server's imports;
  2. if they are missing, creates/uses the skill's own conda env and re-runs
     itself there (--no-env to refuse instead);
  3. writes settings.json (never over an existing one without --force) with
     python_cmd derived from that interpreter;
  4. seeds the vault from vault-template/ — only when the vault does not exist;
  5. hands off to deploy.py, which renders the config and probes the result.

It is deliberately not named setup.py: that name belongs to setuptools, and a
`pip install .` in this directory would try to execute it.
"""
import argparse
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILL_DIR))

# mcp_server/__init__.py and mcp_server/console.py are stdlib-only on purpose:
# this script runs before any dependency is installed, and it must set the
# console encoding before its first Chinese progress line (a GBK console used to
# crash on vault_dir / python_cmd / claude output echoes here).
from mcp_server import MODEL_IMPORTS, SERVER_IMPORTS  # noqa: E402
from mcp_server.console import force_utf8, spawnable  # noqa: E402

force_utf8()
SETTINGS_FILE = SKILL_DIR / "settings.json"
SETTINGS_EXAMPLE = SKILL_DIR / "settings.example.json"
REQUIREMENTS = SKILL_DIR / "requirements.txt"
VAULT_TEMPLATE = SKILL_DIR / "vault-template"

DEFAULT_ENV_NAME = "paper-kb"
# Marks the re-exec into the conda env, so a failed install there reports the
# problem instead of spawning itself forever.
REEXEC_FLAG = "PAPER_KB_BOOTSTRAP_REEXEC"

MIN_PYTHON = (3, 9)          # what the code actually needs (no PEP 604 syntax)
ENV_PYTHON = "3.10"          # what a freshly created env gets


def log(message: str = ""):
    print(message)


def die(message: str, code: int = 1):
    log(f"[ERROR] {message}")
    sys.exit(code)


def missing_imports(modules=SERVER_IMPORTS + MODEL_IMPORTS) -> list:
    """Modules the *current* interpreter cannot import, as "name: error".

    Imported rather than looked up with find_spec: a package that is present
    but broken (a missing native library, a half-finished upgrade) is exactly
    the state this has to catch, and find_spec reports it as present.
    """
    broken = []
    for name in modules:
        try:
            importlib.import_module(name)
        except BaseException as e:  # noqa: BLE001 — a broken wheel can raise anything
            broken.append(f"{name}: {type(e).__name__}: {e}")
    return broken


def conda_base() -> Path:
    """The conda installation root, or None."""
    if not shutil.which("conda"):
        return None
    cmd = ["conda", "info", "--base"]
    try:
        # `or cmd`: conda was just found on PATH; if it vanished in between,
        # running the bare name lets the OSError report that, rather than
        # quietly returning None from a helper the caller cannot use.
        proc = subprocess.run(spawnable(cmd) or cmd,
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    base = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0 or not base:
        return None

    path = Path(base[-1].strip())
    return path if path.is_dir() else None


def env_python(base: Path, name: str) -> Path:
    """The interpreter inside conda env `name` (POSIX and Windows layouts)."""
    if os.name == "nt":
        return base / "envs" / name / "python.exe"
    return base / "envs" / name / "bin" / "python"


def interpreter_path(path) -> str:
    """A path string settings.json can hold: forward slashes, absolute.

    Forward slashes because a Windows path in JSON needs them — backslashes are
    escapes, and `"D:\\Anaconda3\\envs\\..."` is a parse error, which is the
    single most common way a hand-written settings.json fails. expanduser()
    because no shell expands `~` inside an argument's *value* on either
    platform: `--vault ~/papers/kb` would otherwise resolve against the cwd and
    grow a literal `~` directory tree.
    """
    return str(Path(path).expanduser().resolve()).replace("\\", "/")


def seed_vault(vault: Path) -> str:
    """Copy vault-template/ into a vault that does not exist yet.

    Only when the target is absent. An existing directory is somebody's real
    Obsidian vault, and this template carries `.obsidian/app.json` and
    `graph.json` — copying over those would reconfigure their whole vault.
    """
    if vault.exists():
        return "exists"
    if not VAULT_TEMPLATE.is_dir():
        return "no-template"
    shutil.copytree(VAULT_TEMPLATE, vault)
    return "seeded"


def seed_obsidian(vault: Path) -> list:
    """Merge the template's Obsidian files into an EXISTING vault — additions
    only. Returns the list of files written.

    The template only reaches a brand-new vault today, so the real vault
    (created before the template grew these files) runs without graph.json and
    the Dataview index: direction arrows degrade to undirected lines and
    index.md's dynamic table shows nothing. What may be added is exactly what
    is missing; app.json and any file the vault already has are never touched —
    overwriting either would reconfigure somebody's working vault.
    """
    if not VAULT_TEMPLATE.is_dir() or not vault.is_dir():
        return []
    written = []
    for src in sorted(VAULT_TEMPLATE.rglob("*")):
        if not src.is_file():
            continue
        dest = vault / src.relative_to(VAULT_TEMPLATE)
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dest)
        written.append(str(dest.relative_to(vault)))
    return written


def write_settings(payload: dict, force: bool = False) -> str:
    """Write settings.json. Returns "written", "kept" or "overwritten"."""
    if SETTINGS_FILE.exists() and not force:
        return "kept"
    result = "written"
    if SETTINGS_FILE.exists():
        shutil.copy(SETTINGS_FILE, SETTINGS_FILE.with_name("settings.json.bak"))
        result = "overwritten"
    SETTINGS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    return result


def existing_settings() -> dict:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        # utf-8-sig, same as the server: a BOM is otherwise a parse error.
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def prompt_path(label: str, default: str, assume_yes: bool) -> str:
    if assume_yes or not sys.stdin.isatty():
        return default
    answer = input(f"  {label} [{default}]: ").strip()
    return answer or default


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Set up a fresh clone of deep_read_paper_skill.")
    parser.add_argument("--vault", help="知识库目录（vault_dir）；默认 <当前目录>/knowledge-base")
    parser.add_argument("--project", help="Claude Code 项目根目录（hooks 装到这里）；默认当前目录")
    parser.add_argument("--register", action="store_true",
                        help="同时执行 claude mcp add --scope user（会写你的用户级配置）")
    parser.add_argument("--env-name", default=DEFAULT_ENV_NAME,
                        help=f"缺少依赖时使用的 conda 环境名（默认 {DEFAULT_ENV_NAME}）")
    parser.add_argument("--no-env", action="store_true",
                        help="缺少依赖时不建 conda 环境，直接报错退出")
    parser.add_argument("--no-vault-template", action="store_true",
                        help="不把 vault-template/ 复制进新知识库")
    parser.add_argument("--seed-obsidian", action="store_true",
                        help="对已存在的知识库补齐缺失的 Obsidian 文件"
                             "（graph.json、index.md 等；只增不改，已存在的一律不动）")
    parser.add_argument("--force", action="store_true",
                        help="覆盖已存在的 settings.json（先备份为 settings.json.bak）")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="不再询问，直接采用默认值")
    parser.add_argument("--light", action="store_true",
                        help="轻量安装：跳过 sentence-transformers/torch，嵌入走 ChromaDB"
                             " 自带 ONNX 模型（embedding_model 设为 all-MiniLM-L6-v2，"
                             "英文为主、中文较弱；换模型等于换向量空间，已有 .chromadb 需重建）")
    return parser.parse_args(argv)


def install_into_env(name: str, base: Path, light: bool = False):
    """Create the conda env if needed and install requirements into it.

    light=True installs the requirements WITHOUT the sentence-transformers
    line: the ONNX embedder that replaces it runs on chromadb alone, and the
    torch chain it pulls in is by far the heaviest part of the install. The
    filter is derived from requirements.txt itself — the file stays the single
    source of truth for what exists, light just skips one optional line.
    """
    target = env_python(base, name)

    if not target.exists():
        log(f"  [1/2] 创建 conda 环境 {name} (python={ENV_PYTHON}) ……")
        cmd = ["conda", "create", "-n", name, f"python={ENV_PYTHON}", "-y"]
        proc = subprocess.run(spawnable(cmd) or cmd)
        if proc.returncode != 0 or not target.exists():
            die(f"conda create 失败。手动执行：conda create -n {name} python={ENV_PYTHON} -y")
    else:
        log(f"  [1/2] 复用已存在的 conda 环境 {name}")

    requirements = REQUIREMENTS
    if light:
        kept = [ln for ln in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
                if not ln.strip().startswith("sentence-transformers")]
        requirements = Path(tempfile.gettempdir()) / "requirements-paper-kb-light.txt"
        requirements.write_text("\n".join(kept) + "\n", encoding="utf-8")
        log("  （轻量方案：跳过 sentence-transformers/torch，嵌入走 ChromaDB 自带 ONNX）")

    log(f"  [2/2] 安装依赖到 {name}（首次约 280 MB 下载，几分钟）……")
    env = dict(os.environ)
    # pip otherwise counts packages in the user site as already satisfied and
    # skips them, leaving an env that breaks the moment user site is disabled.
    env["PYTHONNOUSERSITE"] = "1"
    proc = subprocess.run([str(target), "-m", "pip", "install",
                           "-r", str(requirements)], env=env)
    if proc.returncode != 0:
        die("依赖安装失败，请看上面的 pip 输出。")
    return target


def reap_exec(target: Path, argv):
    """Re-run this script with the env's interpreter."""
    if os.environ.get(REEXEC_FLAG):
        die(f"{target} 里仍缺少依赖，已停下以免反复重试。\n"
            f"        手动确认：\"{target}\" -m pip install -r requirements.txt")
    log(f"\n  依赖已就绪，切换到 {target} 继续\n")
    env = dict(os.environ)
    env[REEXEC_FLAG] = "1"
    proc = subprocess.run([str(target), str(Path(__file__).resolve()), *argv],
                          env=env)
    sys.exit(proc.returncode)


def main(argv=None):
    args = parse_args(argv)

    log("=" * 55)
    log("  deep_read_paper_skill -- bootstrap")
    log("=" * 55)
    log()

    if sys.version_info < MIN_PYTHON:
        die(f"需要 Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+，"
            f"当前是 {sys.version.split()[0]}")

    light = args.light
    if not light and not args.yes and sys.stdin.isatty():
        # torch is the heaviest thing this skill installs and the multilingual
        # model is its only user. An English-only vault on a small disk does
        # not need either — ask, once, instead of forcing 2GB on everyone.
        log("  嵌入方案：")
        log("    [1] 多语言 SentenceTransformer（默认，中文检索好，含 torch 约 +2 GB）")
        log("    [2] ONNX 轻量（免 torch，仅 ChromaDB 自带模型，英文为主、中文较弱）")
        answer = input("  选择 [1]: ").strip()
        light = answer == "2"
        log()

    # Build the re-exec forwarding AFTER the interactive choice, and from the
    # resolved `light`, not argv: the old table saw only `--light` on the
    # command line, so a hand-picked "[2]" never reached the child — it
    # judged the freshly-installed light env "missing torch", reinstalled the
    # full 2 GB stack against the user's explicit choice, and then died in
    # reap_exec with a FALSE 「仍缺少依赖」 (REEXEC_FLAG was already set).
    # --seed-obsidian was missing from the table altogether: silently dropped
    # on the second pass whenever dependencies had to be installed first.
    forwarded = []          # what to pass back to ourselves on re-exec
    for flag, value in (("--vault", args.vault), ("--project", args.project),
                        ("--env-name", args.env_name)):
        if value:
            forwarded += [flag, value]
    for flag, on in (("--register", args.register), ("--no-env", args.no_env),
                     ("--no-vault-template", args.no_vault_template),
                     ("--seed-obsidian", args.seed_obsidian),
                     ("--force", args.force), ("--yes", args.yes),
                     ("--light", light)):
        if on:
            forwarded.append(flag)

    needed = SERVER_IMPORTS if light else SERVER_IMPORTS + MODEL_IMPORTS
    broken = missing_imports(needed)
    if broken:
        log("  当前解释器缺少依赖：")
        for item in broken:
            log(f"    - {item}")
        log()
        if args.no_env:
            die("用 --no-env 跳过建环境，但依赖必须自己装好：\n"
                f"        {interpreter_path(sys.executable)} -m pip install -r requirements.txt")
        base = conda_base()
        if base is None:
            die("没找到 conda。装好依赖后重跑，或手动执行：\n"
                f"        {interpreter_path(sys.executable)} -m pip install -r requirements.txt\n"
                "        （建议先用 conda 建独立环境，别装进系统 Python）")
        target = install_into_env(args.env_name, base, light=light)
        reap_exec(target, forwarded)
        return  # unreachable

    python_cmd = interpreter_path(sys.executable)
    log(f"  解释器      : {python_cmd}")
    log("  [OK] 依赖齐全")

    settings = existing_settings()
    vault_default = settings.get("vault_dir") or str(Path.cwd() / "knowledge-base")
    project_default = settings.get("project_dir") or str(Path.cwd())
    log()
    # expanduser() here, not just in interpreter_path(): seed_vault() tests this
    # Path for existence before copying the template into it, and a literal `~/x`
    # is never an existing directory — it is a `~` folder waiting to be created.
    vault = Path(prompt_path("知识库目录 (vault_dir)", vault_default, args.yes)).expanduser()
    project = Path(prompt_path("项目根目录 (project_dir)", project_default, args.yes)).expanduser()

    # An existing settings.json keeps every key it had (embedding_model, the
    # trigger keywords, openalex_mailto); a fresh one starts from the example.
    payload = dict(settings)
    if not payload and SETTINGS_EXAMPLE.exists():
        example = json.loads(SETTINGS_EXAMPLE.read_text(encoding="utf-8"))
        payload = {k: v for k, v in example.items() if not k.startswith("_")}
    payload["vault_dir"] = interpreter_path(vault)
    payload["project_dir"] = interpreter_path(project)
    payload["python_cmd"] = python_cmd
    if light:
        # The install just skipped torch; the settings must follow, or the
        # server would still ask SentenceTransformer for a model it cannot load.
        if payload.get("embedding_model") not in (None, "", "all-MiniLM-L6-v2"):
            log(f"  [注意] --light 覆盖已有的 embedding_model="
                f"{payload.get('embedding_model')} → all-MiniLM-L6-v2")
        payload["embedding_model"] = "all-MiniLM-L6-v2"

    state = write_settings(payload, force=args.force)
    log()
    if state == "kept":
        log(f"  [注意] {SETTINGS_FILE.name} 已存在，保持原样。")
        log("         要按上面的路径重写：python bootstrap.py --force（先备份）")
    else:
        log(f"  [OK] 写入 {SETTINGS_FILE}"
            + ("（原文件备份为 settings.json.bak）" if state == "overwritten" else ""))

    if not args.no_vault_template:
        state = seed_vault(vault)
        if state == "seeded":
            log(f"  [OK] 知识库已按 vault-template/ 初始化：{vault}")
        elif state == "exists":
            log(f"  [OK] 知识库已存在，未改动：{vault}")
            if args.seed_obsidian:
                added = seed_obsidian(vault)
                if added:
                    log("  [OK] 已补齐缺失的 Obsidian 文件（只增不改）：")
                    for item in added:
                        log(f"       + {item}")
                else:
                    log("  [OK] Obsidian 模板文件已齐全，无需补齐")

    log()
    log("-" * 55)
    log()
    # deploy.py owns the rest: it renders output/, probes the interpreter it was
    # given, and prints the exact registration command.
    sys.path.insert(0, str(SKILL_DIR))
    import deploy  # noqa: E402 — after the dependency check, on purpose
    deploy.generate_config(register=args.register)

    if not args.register:
        log("  MCP 服务器尚未注册（默认不动你的用户级配置）。注册：")
        log("      python bootstrap.py --register")
        log()


if __name__ == "__main__":
    main()
