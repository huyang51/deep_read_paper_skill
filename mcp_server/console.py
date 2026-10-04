"""Console plumbing shared by every entry point (stdlib only — importable
before any dependency is installed).

Two things used to be copy-pasted around here, and the copies drifted:

* the UTF-8 stream preamble, which ten files carried. Three of them forgot
  ``errors="replace"``, so ``tools/index_paper.py`` and both hooks still died
  with a UnicodeEncodeError on a GBK console when a paper title contained a
  character cp936 cannot encode — exactly the crash the preamble exists to
  prevent, in the same line of code that was supposed to fix it.
* the Windows ``.cmd``/``.bat`` shim, which existed twice under two names
  (``deploy.runnable`` and ``bootstrap.launchable``) with two different
  not-found contracts.

Nothing in this module may import a third-party module, and nothing here may
print: ``bootstrap.py`` imports it on a machine where chromadb does not exist
yet, before it has said a single word to the user.
"""
import os
import shutil
import sys


def force_utf8(stdin: bool = False) -> None:
    """Put the output streams (and optionally stdin) on UTF-8, replacing what
    cannot encode instead of raising.

    ``errors="replace"`` is the point: the strings printed by these scripts are
    partly user-controlled (vault paths, paper titles, ``claude`` output, the
    MCP payload echoed back), and an emoji in a path must not kill an installer
    that is halfway through writing settings.json.
    """
    names = ["stdout", "stderr"] + (["stdin"] if stdin else [])
    for name in names:
        stream = getattr(sys, name, None)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def spawnable(argv):
    """``argv`` with argv[0] resolved enough for this platform to launch, or
    None when the command is nowhere on PATH (so the caller can fall back to
    printing the command instead of running it).

    On Windows npm installs Claude Code as ``claude.cmd`` and conda resolves to
    ``condabin/conda.bat``; CreateProcess cannot start a .cmd/.bat directly and
    fails with WinError 193, which reads like the tool is broken rather than
    like it needs cmd.exe in front of it.
    """
    exe = shutil.which(argv[0])
    if exe is None:
        return None
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", exe, *argv[1:]]
    return [exe, *argv[1:]]
