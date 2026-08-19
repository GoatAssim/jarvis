"""Allowlisted git commands for Jarvis — not a general shell."""

import os
import re
import shutil
import subprocess
from pathlib import Path

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Read / everyday write. Destructive extras need confirm=true.
_SAFE = frozenset({
    "status", "log", "diff", "show", "branch", "tag", "remote", "fetch",
    "pull", "push", "add", "commit", "stash", "switch", "checkout",
    "restore", "merge", "rebase", "clone", "blame", "shortlog",
    "rev-parse", "describe", "ls-files", "ls-remote",
})
_NEEDS_CONFIRM = frozenset({"reset", "clean", "push"})  # push only if force

_ARG_OK = re.compile(r"^[A-Za-z0-9_./@%+=:,~^!? \-]{1,400}$")
_FORCE = re.compile(r"^(--force|-f|--hard|--clean|-D|-d)$")


def _git_bin():
    return shutil.which("git")


def _need_confirm(args, why):
    confirm = (args or {}).get("confirm")
    if confirm is True:
        return None
    return {
        "needs_confirmation": True,
        "message": f"Ask the user to confirm this git action ({why}), then call git_run again with confirm=true.",
    }


def _validate_args(extra):
    if extra is None:
        return [], None
    if isinstance(extra, str):
        extra = extra.split()
    if not isinstance(extra, list):
        return None, {"error": "args must be a list of strings."}
    out = []
    for a in extra:
        if not isinstance(a, str) or not a.strip():
            return None, {"error": "Empty git argument."}
        a = a.strip()
        if any(c in a for c in ";|&`$()<>\n\r"):
            return None, {"error": f"Rejected git argument: {a!r}"}
        if a.startswith("-c") or a.startswith("--exec") or a == "--upload-pack":
            return None, {"error": "That git flag isn't allowed."}
        if not _ARG_OK.match(a):
            return None, {"error": f"Rejected git argument: {a!r}"}
        out.append(a)
    if len(out) > 24:
        return None, {"error": "Too many git arguments."}
    return out, None


def _cwd(path):
    raw = (path or "").strip() or os.getcwd()
    p = Path(raw).expanduser()
    try:
        p = p.resolve()
    except OSError:
        return None, {"error": f"Bad path: {raw}"}
    if not p.exists() or not p.is_dir():
        return None, {"error": f"Not a directory: {p}"}
    return str(p), None


def tool_git_run(args):
    args = args or {}
    if not _git_bin():
        return {"error": "git isn't installed or not on PATH."}

    cmd = (args.get("command") or args.get("subcommand") or "").strip().lstrip("-")
    if cmd not in _SAFE and cmd not in _NEEDS_CONFIRM:
        return {
            "needs_clarification": True,
            "message": f"Git subcommand '{cmd}' isn't allowed.",
            "allowed": sorted(_SAFE | _NEEDS_CONFIRM),
        }

    extra, err = _validate_args(args.get("args"))
    if err:
        return err

    force = any(_FORCE.match(a) for a in extra) or cmd in ("reset", "clean")
    if cmd == "push" and not force:
        force = False
    if cmd in ("reset", "clean") or (cmd == "push" and any(a in ("--force", "-f", "--force-with-lease") for a in extra)):
        blocked = _need_confirm(args, f"git {cmd} {' '.join(extra)}".strip())
        if blocked:
            return blocked
    if cmd == "clone":
        blocked = _need_confirm(args, "git clone")
        if blocked:
            return blocked

    cwd, err = _cwd(args.get("cwd"))
    if err:
        return err
    if cmd != "clone":
        probe = subprocess.run(
            [_git_bin(), "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        if probe.returncode != 0:
            return {"error": f"Not a git repo: {cwd}"}

    argv = [_git_bin(), cmd, *extra]
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            timeout=120,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"error": "git timed out after 120s", "cwd": cwd}
    except OSError as e:
        return {"error": str(e)}

    stdout = (result.stdout or b"").decode("utf-8", errors="replace")
    stderr = (result.stderr or b"").decode("utf-8", errors="replace")

    def cap(s, n=4000):
        s = (s or "").strip()
        return s if len(s) <= n else s[:n] + "\n…(truncated)"

    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "cwd": cwd,
        "command": " ".join(argv[1:]),
        "stdout": cap(stdout),
        "stderr": cap(stderr, 1500),
    }


GIT_TOOL_SCHEMAS = [
    {
        "name": "git_run",
        "description": (
            "Run an allowlisted git subcommand in a repo directory. "
            "Safe: status, log, diff, branch, add, commit, pull, push, fetch, stash, switch, checkout, "
            "merge, remote, show, … cwd defaults to the current folder. "
            "reset/clean/force-push/clone need confirm=true. Not a general shell."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Git subcommand, e.g. status, log, pull, commit."},
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Extra args, e.g. ['-n','10','--oneline'] or ['-m','fix wifi tool'].",
                },
                "cwd": {"type": "string", "description": "Repo directory. Default: current working directory."},
                "confirm": {"type": "boolean", "description": "Required true for reset, clean, force push, clone."},
            },
            "required": ["command"],
        },
    },
]

GIT_TOOLS = {
    "git_run": tool_git_run,
}
