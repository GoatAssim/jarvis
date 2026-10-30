#!/usr/bin/env python3
"""Download Hub, N23 step one: what do the package managers print when they list what is out of date?

The sibling of `steam-checks.py` and `tests/store_checks.py`.  The update detection Jarvis will need
(winget, choco, scoop, npm -g, pip, pipx) must be written from the real output on the owner's PC, not
from memory of how those tools print.  This script RECORDS that output, raw: the exact command, its exit
code, how long it took, what it printed on stdout and on stderr.  It does NOT parse any of it, does not
turn it into versions and does not decide what is "outdated".

It is read-only.  Every command comes from the fixed list below (`COMMANDS`), is checked against a list of
verbs that change things (`CHANGING`) before it runs, and is run without a shell.  Nothing is installed,
upgraded, removed or configured.  The only file it may write is the one you name with --out.
Two honest side effects of the tools themselves:
  * `choco outdated`, `npm outdated -g`, `pip list --outdated` and `pip index versions` ask their servers
    (that is the question).
  * `scoop status` runs `git fetch` in scoop's bucket folders to compare with the catalogue (it refreshes
    the catalogue copies, never an installed app).  Skip it with --skip scoop if you'd rather not.

Usage (Windows, Python 3):

    python tests/package_update_checks.py --out package-report.txt --redact
    python tests/package_update_checks.py --only winget npm --timeout 180
    python tests/package_update_checks.py --skip scoop
    python tests/package_update_checks.py --admin --only choco --out admin-report.txt --redact

--admin (round 10, still read-only) also records whether THIS window runs as an administrator, where Chocolatey
lives, and who may write in its folders (`whoami /groups`, `icacls <folder>`: they only list).  Run it once from a
normal window and send the report: Jarvis decides "needs an administrator window" from exactly these facts.

Send back the report (--redact replaces your Windows user name with <user>).
"""

import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

MAX_STDOUT = 8000
MAX_STDERR = 2000
PIPX_PACKAGES = 5         # how many pipx packages get a `pip index versions` look

# manager -> [(label, argv)].  The executable name is looked up on PATH; "python" means this interpreter.
COMMANDS = {
    "winget": [("version", ["winget", "--version"]),
               ("upgradable", ["winget", "list", "--upgrade-available"])],
    "choco": [("version", ["choco", "--version"]),
              ("outdated", ["choco", "outdated"])],
    "scoop": [("version", ["scoop", "--version"]),
              ("status", ["scoop", "status"])],
    "npm": [("version", ["npm", "--version"]),
            ("outdated -g", ["npm", "outdated", "-g"]),
            ("outdated -g --json", ["npm", "outdated", "-g", "--json"]),
            ("installed -g", ["npm", "ls", "-g", "--depth=0"])],
    "pip": [("version", ["python", "-m", "pip", "--version"]),
            ("outdated", ["python", "-m", "pip", "list", "--outdated"]),
            ("outdated json", ["python", "-m", "pip", "list", "--outdated", "--format=json"])],
    "pipx": [("version", ["pipx", "--version"]),
             ("installed", ["pipx", "list", "--short"])],
}
# verbs that change something: a command containing any of them never runs.  `upgrade` is here on purpose:
# `winget list --upgrade-available` is the read-only way to ask winget, and "--upgrade-available" is not the verb.
CHANGING = frozenset(("install", "uninstall", "remove", "upgrade", "update", "add", "delete", "clean", "cleanup", "cache",
                      "config", "set", "reset", "repair", "import", "export", "pin", "unpin", "uninject", "inject",
                      "reinstall", "upgrade-all", "run", "ensurepath", "prune", "dedupe", "audit", "fund", "publish",
                      "login", "logout", "link", "unlink", "rebuild", "restart"))


def is_read_only(argv):
    """True when no word of the command is one that changes things (a flag like --upgrade-available is fine)."""
    words = [str(w).lower() for w in argv]
    return bool(words) and not any(w in CHANGING for w in words)


# --admin: facts about administrator rights.  Every command only LISTS (whoami, icacls with a folder and nothing else).
ADMIN_COMMANDS = [("who am I and at what level does this window run", ["whoami", "/groups"]),
                  ("privileges of this window", ["whoami", "/priv"])]


def elevation_state():
    """'yes' | 'no' | 'unknown (...)': does THIS process run as an administrator."""
    if os.name != "nt":
        return "unknown (not Windows)"
    try:
        import ctypes
        return "yes" if ctypes.windll.shell32.IsUserAnAdmin() else "no"
    except Exception as exc:  # noqa: BLE001
        return "unknown (%s)" % exc


def admin_probe(timeout, emit):
    emit("[admin] this window runs as an administrator: %s" % elevation_state())
    for var in ("ChocolateyInstall", "ProgramData"):
        emit("[admin] %%%s%% = %s" % (var, os.environ.get(var) or "(not set)"))
    emit("[admin] choco is at: %s" % (which("choco") or "(not on PATH)"))
    for label, argv in ADMIN_COMMANDS:
        record("admin", label, argv, timeout, emit)
    root = os.environ.get("ChocolateyInstall")
    if root:
        record("admin", "who may write in Chocolatey's folder", ["icacls", root], timeout, emit)
        record("admin", "who may write in Chocolatey's lib folder", ["icacls", os.path.join(root, "lib")], timeout, emit)


def now_text():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def which(name):
    return shutil.which(name)


def run_command(argv, timeout):
    """-> (exit code or None, stdout, stderr, seconds, note).  No shell; stdin closed; never raises."""
    start = time.monotonic()
    env = dict(os.environ, NO_COLOR="1", NPM_CONFIG_UPDATE_NOTIFIER="false", PIP_DISABLE_PIP_VERSION_CHECK="1",
               PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, env=env, shell=False)
        return (proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace"),
                time.monotonic() - start, "")
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return None, out, "", time.monotonic() - start, "TIMED OUT after %s s (killed)" % timeout
    except (OSError, ValueError) as exc:
        return None, "", "", time.monotonic() - start, "could not run: %s" % exc


RUN = run_command


def mask(text, user=None):
    if user:
        text = re.sub(re.escape(user), "<user>", text, flags=re.I)
    return text


def clip(text, limit):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text if len(text) <= limit else text[:limit] + "\n... (cut: %d more characters)" % (len(text) - limit)


def resolve(argv):
    """Replace the program name by what is on PATH; `python` is this interpreter.  None when it is not installed."""
    head = argv[0]
    if head == "python":
        return [sys.executable] + list(argv[1:])
    found = which(head)
    return [found] + list(argv[1:]) if found else None


def record(manager, label, argv, timeout, emit):
    if not is_read_only(argv):                       # a bug here must never run a command that changes something
        emit("[%s] %s: REFUSED, the command contains a verb that changes things: %s" % (manager, label, " ".join(argv)))
        return None
    real = resolve(argv)
    if real is None:
        emit("[%s] %s: not installed (no %s on PATH)" % (manager, label, argv[0]))
        return None
    code, out, err, seconds, note = RUN(real, timeout)
    emit("[%s] %s: $ %s" % (manager, label, " ".join(argv)))
    emit("    exit code: %s   took: %.1f s   %s" % (code, seconds, note))
    emit("    --- stdout ---")
    emit(clip(out, MAX_STDOUT).rstrip() or "    (empty)")
    emit("    --- stderr ---")
    emit(clip(err, MAX_STDERR).rstrip() or "    (empty)")
    return out


def pipx_index_lookups(listing, timeout, emit):
    """For the first few pipx packages: what `pip index versions` prints.  Read-only; the package names come from
    `pipx list --short` and are used as plain words after a fixed command."""
    names = []
    for line in (listing or "").splitlines():
        word = line.split(" ")[0].strip()
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", word) and word.lower() not in CHANGING and word not in names:
            names.append(word)
    for name in names[:PIPX_PACKAGES]:
        record("pipx", "latest on the index for %s" % name, ["python", "-m", "pip", "index", "versions", name], timeout, emit)


def main(argv=None, out=None):
    parser = argparse.ArgumentParser(description="Record what the package managers print about updates (read-only).")
    parser.add_argument("--only", nargs="+", choices=sorted(COMMANDS), help="only these managers")
    parser.add_argument("--skip", nargs="+", choices=sorted(COMMANDS), default=[], help="leave these out")
    parser.add_argument("--timeout", type=float, default=120.0, help="seconds each command may take (default 120)")
    parser.add_argument("--out", help="also write the report to this file")
    parser.add_argument("--redact", action="store_true", help="replace your user name with <user>")
    parser.add_argument("--admin", action="store_true", help="also record administrator-rights facts (read-only)")
    args = parser.parse_args(argv)
    user = (os.environ.get("USERNAME") or os.environ.get("USER")) if args.redact else None
    report = []

    def emit(text):
        text = mask(text, user)
        report.append(text)
        print(text, file=out or sys.stdout, flush=True)

    emit("package_update_checks %s: read-only; raw output only, nothing is parsed, nothing is installed or changed." % now_text())
    emit("platform: %s   python: %s" % (sys.platform, sys.version.split()[0]))
    for manager in COMMANDS:
        if (args.only and manager not in args.only) or manager in args.skip:
            emit("[%s] skipped" % manager)
            continue
        listing = None
        for label, cmd in COMMANDS[manager]:
            got = record(manager, label, cmd, args.timeout, emit)
            if manager == "pipx" and label == "installed":
                listing = got
        if manager == "pipx" and listing:
            pipx_index_lookups(listing, args.timeout, emit)
    if args.admin:
        admin_probe(args.timeout, emit)
    emit("done.")
    if args.out:
        Path(args.out).write_text("\n".join(report) + "\n", encoding="utf-8")
        print("report written to %s" % args.out, file=out or sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
