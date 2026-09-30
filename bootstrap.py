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
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = SKILL_DIR / "settings.json"
SETTINGS_EXAMPLE = SKILL_DIR / "settings.example.json"
REQUIREMENTS = SKILL_DIR / "requirements.txt"
VAULT_TEMPLATE = SKILL_DIR / "vault-template"

DEFAULT_ENV_NAME = "paper-kb"
# Marks the re-exec into the conda env, so a failed install there reports the
# problem instead of spawning itself forever.
REEXEC_FLAG = "PAPER_KB_BOOTSTRAP_REEXEC"

# What the MCP server imports at startup, plus the embedder for the default
# (multilingual) model. Keep in sync with deploy.SERVER_IMPORTS.
SERVER_IMPORTS = ("chromadb", "frontmatter", "watchfiles", "pydantic")
MODEL_IMPORTS = ("sentence_transformers",)

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


def launchable(argv: list) -> list:
    """argv that CreateProcess can actually start.

    On Windows `conda` resolves to `condabin/conda.bat`, and a .bat/.cmd is not
    an executable — CreateProcess fails with WinError 193 and the error reads
    like conda is broken rather than like it needs a shell in front.
    """
    exe = shutil.which(argv[0])
    if exe and os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", exe, *argv[1:]]
    return [exe or argv[0], *argv[1:]]


def conda_base() -> Path:
    """The conda installation root, or None."""
    if not shutil.which("conda"):
        return None
    try:
        proc = subprocess.run(launchable(["conda", "info", "--base"]),
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
    single most common way a hand-written settings.json fails.
    """
    return str(Path(path).resolve()).replace("\\", "/")


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
    parser.add_argument("--force", action="store_true",
                        help="覆盖已存在的 settings.json（先备份为 settings.json.bak）")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="不再询问，直接采用默认值")
    return parser.parse_args(argv)


def install_into_env(name: str, base: Path):
    """Create the conda env if needed and install requirements into it."""
    target = env_python(base, name)

    if not target.exists():
        log(f"  [1/2] 创建 conda 环境 {name} (python={ENV_PYTHON}) ……")
        proc = subprocess.run(launchable(["conda", "create", "-n", name,
                                          f"python={ENV_PYTHON}", "-y"]))
        if proc.returncode != 0 or not target.exists():
            die(f"conda create 失败。手动执行：conda create -n {name} python={ENV_PYTHON} -y")
    else:
        log(f"  [1/2] 复用已存在的 conda 环境 {name}")

    log(f"  [2/2] 安装依赖到 {name}（首次约 280 MB 下载，几分钟）……")
    env = dict(os.environ)
    # pip otherwise counts packages in the user site as already satisfied and
    # skips them, leaving an env that breaks the moment user site is disabled.
    env["PYTHONNOUSERSITE"] = "1"
    proc = subprocess.run([str(target), "-m", "pip", "install",
                           "-r", str(REQUIREMENTS)], env=env)
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
    forwarded = []          # what to pass back to ourselves on re-exec
    for flag, value in (("--vault", args.vault), ("--project", args.project),
                        ("--env-name", args.env_name)):
        if value:
            forwarded += [flag, value]
    for flag, on in (("--register", args.register), ("--no-env", args.no_env),
                     ("--no-vault-template", args.no_vault_template),
                     ("--force", args.force), ("--yes", args.yes)):
        if on:
            forwarded.append(flag)

    log("=" * 55)
    log("  deep_read_paper_skill -- bootstrap")
    log("=" * 55)
    log()

    if sys.version_info < MIN_PYTHON:
        die(f"需要 Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+，"
            f"当前是 {sys.version.split()[0]}")

    broken = missing_imports()
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
        target = install_into_env(args.env_name, base)
        reap_exec(target, forwarded)
        return  # unreachable

    python_cmd = interpreter_path(sys.executable)
    log(f"  解释器      : {python_cmd}")
    log("  [OK] 依赖齐全")

    settings = existing_settings()
    vault_default = settings.get("vault_dir") or str(Path.cwd() / "knowledge-base")
    project_default = settings.get("project_dir") or str(Path.cwd())
    log()
    vault = Path(prompt_path("知识库目录 (vault_dir)", vault_default, args.yes))
    project = Path(prompt_path("项目根目录 (project_dir)", project_default, args.yes))

    # An existing settings.json keeps every key it had (embedding_model, the
    # trigger keywords, openalex_mailto); a fresh one starts from the example.
    payload = dict(settings)
    if not payload and SETTINGS_EXAMPLE.exists():
        example = json.loads(SETTINGS_EXAMPLE.read_text(encoding="utf-8"))
        payload = {k: v for k, v in example.items() if not k.startswith("_")}
    payload["vault_dir"] = interpreter_path(vault)
    payload["project_dir"] = interpreter_path(project)
    payload["python_cmd"] = python_cmd

    state = write_settings(payload, force=args.force)
    log()
    if state == "kept":
        log(f"  [注意] {SETTINGS_FILE.name} 已存在，保持原样。")
        log(f"         要按上面的路径重写：python bootstrap.py --force（先备份）")
    else:
        log(f"  [OK] 写入 {SETTINGS_FILE}"
            + ("（原文件备份为 settings.json.bak）" if state == "overwritten" else ""))

    if not args.no_vault_template:
        state = seed_vault(vault)
        if state == "seeded":
            log(f"  [OK] 知识库已按 vault-template/ 初始化：{vault}")
        elif state == "exists":
            log(f"  [OK] 知识库已存在，未改动：{vault}")

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
