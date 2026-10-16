#!/usr/bin/env python3
"""Build, install and launch Jarvis.

This is what script.bat used to do in batch, now in Python. script.bat is
only a launcher for this file (so it can sit in the startup apps).

    python install.py                full build: install the CLI, copy the exe
                                     to Program Files, npm install, start the
                                     web server in its own window, open the UI
    python install.py --serve-only   skip the build, just start the web server
                                     (this is the one for startup apps)

Options:
    --serve-only        no build; resolve the existing install and start the server
    --no-browser        don't open the web UI in the browser
    --with-deps         also pull jarvis-cli's dependencies (pyproject.toml changed).
                        Done automatically the first time, when nothing is installed yet.
    --no-venv           ignore any .venv and use plain Python
    --venv PATH         use this venv (its folder, or its python) instead of looking for one
    --install-dir DIR   where the exe gets copied (default: Program Files (x86)\\Utils\\bin).
                        Pass --install-dir "" to skip that copy.

WHICH PYTHON IT BUILDS WITH (same order script.bat used):
    1. --venv PATH / --no-venv, if given
    2. jarvis-cli/.venv, then a root-level .venv, if either exists
    3. otherwise the python running this script (which is itself a venv's
       python if one is activated, or plain Python if not)
Every pip / build_tools call goes through that interpreter, so a venv build
lands in the venv and a plain build lands in your normal Python, exactly as before.

THE STEPS ARE THE SAME AS script.bat's, in the same order:
    [1]   persona -> CLI command name (build_tools/sync_entry_point.py)
    [1.5] bump the build number, only if the source changed
    [2]   pip install . --force-reinstall --no-deps
    [3]   find the freshly built exe
    [4]   copy it to Program Files (one UAC prompt, for that copy only)
    [4.5] verify PATH's command matches this build (non-fatal)
    [5]   npm install, then start the web server

The config directory (~/.jarvis) is never touched or renamed here; see
cli_name.txt below for the one file this script keeps in it.

Stdlib only, Python 3.8+, and ASCII-only output so a cp1252 console can't
choke on it. Kept in one file on purpose: it has to run BEFORE anything is
installed.
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
JARVIS_CLI = ROOT / "jarvis-cli"
WEB_DIR = ROOT / "web"
BUILD_TOOLS = JARVIS_CLI / "build_tools"

IS_WIN = os.name == "nt"
EXE_SUFFIX = ".exe" if IS_WIN else ""

# NOTE: the config directory (~/.jarvis) is NEVER renamed, on purpose -- it
# stays fixed for backwards compatibility no matter what persona/CLI name is
# active. CLI_NAME_STATE just remembers the last-installed exe name so a
# persona switch can clean up the old one; it has nothing to do with where
# settings/history/etc. actually live.
CLI_NAME_STATE = Path.home() / ".jarvis" / "cli_name.txt"

# What the persona sanitizer can produce (alnum only) -- used to refuse a
# hand-edited / corrupted cli_name.txt turning into a path we'd delete.
_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9_.\-]+")


class InstallError(Exception):
    """A fatal step failed. The message is shown as-is."""


def say(msg=""):
    print(msg, flush=True)


def _fix_console():
    # Never let an odd character in a path crash a print on a cp1252 console.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Windows plumbing: admin check, de-elevation, elevation, own-console check
# ---------------------------------------------------------------------------


def is_admin():
    if not IS_WIN:
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _own_console():
    """True when this process is the only one attached to its console, i.e. it
    was double-clicked or opened in its own window (the de-elevated relaunch,
    the elevated copy). Launched from cmd/script.bat/a terminal, the shell is
    attached too. Used to keep an error on screen instead of letting the
    window vanish."""
    if not IS_WIN:
        return False
    try:
        import ctypes

        buf = (ctypes.c_uint * 4)()
        return ctypes.windll.kernel32.GetConsoleProcessList(buf, 4) <= 1
    except Exception:
        return False


def _pause_if_own_window():
    if _own_console():
        try:
            input("\nPress Enter to close...")
        except (EOFError, KeyboardInterrupt):
            pass


def _powershell(script):
    """Run a PowerShell snippet. -EncodedCommand sidesteps every quoting
    problem a snippet with quotes in it would otherwise have (the same
    class of cmd.exe landmine find_cli_exe.py's docstring describes)."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    ).returncode


def _ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def deelevate_self(argv):
    """The whole script was 'Run as administrator': relaunch as the normal
    user so pip / npm / node / jarvis never run with an admin token (and
    ~/.jarvis resolves to YOUR profile, not an admin account's). Going
    through Shell.Application.ShellExecute is the standard way to start a
    process un-elevated from an elevated one."""
    say()
    say("NOTE: Elevated window detected. Re-launching as your normal user...")
    say()
    args = subprocess.list2cmdline([str(Path(__file__).resolve())] + list(argv))
    script = (
        "$sh = New-Object -ComObject Shell.Application; "
        "$sh.ShellExecute(%s, %s, %s, '', 1)"
        % (_ps_quote(sys.executable), _ps_quote(args), _ps_quote(ROOT))
    )
    if _powershell(script) != 0:
        raise InstallError(
            "Couldn't drop admin rights to relaunch as your normal user. "
            "Run this from a normal (non-administrator) window."
        )


def run_elevated(argv):
    """Run this script again with admin rights (one UAC prompt) and return
    its exit code. A cancelled UAC prompt or a failed launch is non-zero."""
    args = subprocess.list2cmdline([str(Path(__file__).resolve())] + list(argv))
    script = (
        "try { "
        "$p = Start-Process -FilePath %s -Verb RunAs -ArgumentList %s -Wait -PassThru; "
        "exit $p.ExitCode "
        "} catch { exit 1 }" % (_ps_quote(sys.executable), _ps_quote(args))
    )
    return _powershell(script)


# ---------------------------------------------------------------------------
# Which python
# ---------------------------------------------------------------------------


def venv_python(venv_dir):
    venv_dir = Path(venv_dir)
    if IS_WIN:
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _plain_python():
    """The interpreter for --no-venv. If this script is itself running from a
    venv (e.g. script.bat picked a .venv python before seeing the flag, or one
    is activated), step out to the base interpreter it was made from."""
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        base = getattr(sys, "_base_executable", None)
        if base and Path(base).exists() and Path(base).resolve() != Path(sys.executable).resolve():
            return Path(base)
        fallback = (
            Path(sys.base_prefix) / "python.exe" if IS_WIN else Path(sys.base_prefix) / "bin" / "python3"
        )
        if fallback.exists():
            return fallback
        say("WARNING: --no-venv was given but the base Python couldn't be located; "
            "using the current (venv) interpreter.")
    return Path(sys.executable)


def resolve_python(venv_arg=None, no_venv=False):
    """Returns (python_path, human_label)."""
    if venv_arg:
        p = Path(venv_arg).expanduser()
        py = venv_python(p) if p.is_dir() else p
        if not py.exists():
            raise InstallError("--venv %s: no python found there (looked for %s)." % (venv_arg, py))
        return py, "--venv"
    if no_venv:
        return _plain_python(), "plain Python (--no-venv)"
    for d in (JARVIS_CLI / ".venv", ROOT / ".venv"):
        py = venv_python(d)
        if py.exists():
            try:
                shown = d.relative_to(ROOT)
            except ValueError:
                shown = d
            return py, "project venv (%s)" % shown
    return Path(sys.executable), "current interpreter"


_INSPECT = (
    "import json, sys, sysconfig;"
    "print(json.dumps({'in_venv': sys.prefix != getattr(sys, 'base_prefix', sys.prefix),"
    " 'scripts': sysconfig.get_path('scripts'), 'version': sys.version.split()[0]}))"
)


def inspect_python(py):
    try:
        out = subprocess.run(
            [str(py), "-c", _INSPECT], capture_output=True, text=True, timeout=30
        )
        if out.returncode != 0:
            raise ValueError(out.stderr.strip() or "exit %s" % out.returncode)
        return json.loads(out.stdout.strip().splitlines()[-1])
    except Exception as e:
        raise InstallError("Can't run Python at %s (%s)." % (py, e))


def _child_env():
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_tool(py, script, *args):
    """Run one of build_tools/*.py with the target python, from jarvis-cli/.
    Their stdout contracts ('<number> <hash>', a bare name, a bare path) are
    unchanged; they're just read here instead of by a batch `for /f`."""
    return subprocess.run(
        [str(py), str(BUILD_TOOLS / script)] + [str(a) for a in args],
        cwd=str(JARVIS_CLI),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_child_env(),
    )


# ---------------------------------------------------------------------------
# Build steps
# ---------------------------------------------------------------------------


def step_persona(py):
    """[1/5] Resolve the persona's CLI name AND rewrite pyproject.toml's
    [project.scripts] entry so the upcoming `pip install .` bakes the right
    exe name in."""
    out = run_tool(py, "sync_entry_point.py")
    name = (out.stdout or "").strip()
    if out.returncode != 0 or not name:
        if out.stderr:
            say(out.stderr.strip())
        raise InstallError(
            "Could not resolve/sync the persona's CLI name. "
            "Check that Python can import jarvis.ai_config."
        )
    return name


def resolve_cli_name_readonly(py):
    """Same answer as step_persona, without rewriting pyproject.toml. Used by
    --serve-only, which must not touch the build inputs."""
    code = (
        "import sys; sys.path.insert(0, r'%s'); "
        "from jarvis.persona_name import current_cli_name; print(current_cli_name())" % JARVIS_CLI
    )
    out = subprocess.run(
        [str(py), "-c", code], capture_output=True, text=True, errors="replace", env=_child_env()
    )
    lines = (out.stdout or "").strip().splitlines()
    return lines[-1].strip() if out.returncode == 0 and lines else "jarvis"


def step_bump(py):
    """[1.5/5] Non-fatal. The number only changes when jarvis-cli/jarvis/
    source itself changed since the last build; rebuilding with no edits
    reports the SAME number/hash on purpose."""
    out = run_tool(py, "bump_build_version.py")
    if out.stderr:
        say(out.stderr.strip())
    parts = (out.stdout or "").split()
    if out.returncode != 0 or len(parts) < 2:
        say("WARNING: Could not bump build number (non-fatal) -- version display may be stale.")
        return
    say("This build: #%s (src %s)" % (parts[0], parts[1][:12]))


def _is_installed(py):
    r = subprocess.run(
        [str(py), "-c", "import importlib.metadata as m; m.version('jarvis-cli')"],
        capture_output=True,
    )
    return r.returncode == 0


def step_pip(py, with_deps):
    """[2/5] --force-reinstall is required: plain `pip install .` only checks
    name==version against what's installed and silently no-ops when that
    matches -- it does NOT diff source contents. This project's version
    string doesn't bump on every change, so without it every run after the
    first would install NOTHING while every step still reported success.
    --no-deps keeps it fast (the dependency tree is unchanged).

    When dependencies might have changed (--with-deps) or nothing is
    installed yet, a plain `pip install .` runs first to pull them in."""
    first_time = not _is_installed(py)
    if first_time:
        say("First install into this Python -- pulling dependencies too.")
    if first_time or with_deps:
        if subprocess.run([str(py), "-m", "pip", "install", "."], cwd=str(JARVIS_CLI)).returncode != 0:
            raise InstallError("pip install failed.")
    rc = subprocess.run(
        [str(py), "-m", "pip", "install", ".", "--force-reinstall", "--no-deps"],
        cwd=str(JARVIS_CLI),
    ).returncode
    if rc != 0:
        raise InstallError("pip install failed.")


def _same_path(a, b):
    try:
        return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))
    except Exception:
        return False


def _path_dirs():
    return [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]


def find_exe(py, info, cli_name, install_dir):
    """[3/5] Locate the exe `pip install .` just built.

    1. The venv's own Scripts dir, when the target python is a venv. Checked
       FIRST, because find_cli_exe.py also looks in the per-user scripts dir
       and newest-mtime-wins could pick a stale exe from there over the venv's.
    2. Otherwise ask the SAME python that ran pip where IT puts console
       scripts (base + per-user dir, newest wins) -- build_tools/find_cli_exe.py.
    3. Last resort: PATH, but NEVER a match inside install_dir. That's the
       copy DESTINATION; it's about to be deleted and re-copied FROM, so
       treating it as a 'freshly built' source would delete the only copy.
    """
    name = cli_name + EXE_SUFFIX
    if info.get("in_venv"):
        cand = Path(info["scripts"]) / name
        if cand.is_file():
            return cand
    out = run_tool(py, "find_cli_exe.py", cli_name)
    found = (out.stdout or "").strip()
    if out.returncode == 0 and found and Path(found).is_file():
        return Path(found)
    for d in _path_dirs():
        if install_dir is not None and _same_path(d, install_dir):
            continue
        cand = Path(d) / name
        if cand.is_file():
            return cand
    raise InstallError(
        "%s not found.\n"
        "Tried the venv scripts dir, the base + user Python scripts dirs, and PATH.\n"
        "(Skipped any PATH match inside %s -- that's the old install target, not a new build.)\n"
        "Run pip install from jarvis-cli first, or check that %s is the python you expect."
        % (name, install_dir, py)
    )


def _read_prev_name():
    try:
        text = CLI_NAME_STATE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.strip().splitlines()
    prev = lines[0].strip() if lines else ""
    return prev if _SAFE_NAME_RE.fullmatch(prev) else ""


def _write_state(cli_name):
    CLI_NAME_STATE.parent.mkdir(parents=True, exist_ok=True)
    CLI_NAME_STATE.write_text(cli_name + "\n", encoding="utf-8")


def copy_exe(src, install_dir, cli_name, prev_name=""):
    """Delete a previously-installed exe under a DIFFERENT persona name (so
    Program Files\\...\\bin doesn't accumulate one exe per persona you've ever
    equipped), then replace the current one. Refuses when src IS the
    destination: delete-then-copy-from-itself would destroy the only copy."""
    src = Path(src)
    install_dir = Path(install_dir)
    dest = install_dir / (cli_name + EXE_SUFFIX)
    if _same_path(src, dest):
        raise InstallError(
            "Source and destination are the same file:\n  %s\n"
            "Refusing to delete-then-copy from itself. This means the exe lookup "
            "couldn't find a real freshly-built exe outside %s -- re-run the "
            "build without deleting the old install first." % (src, install_dir)
        )
    install_dir.mkdir(parents=True, exist_ok=True)
    if prev_name and prev_name != cli_name and _SAFE_NAME_RE.fullmatch(prev_name):
        old = install_dir / (prev_name + EXE_SUFFIX)
        if old.exists():
            say("Removing previous persona's command: %s" % old.name)
            try:
                old.unlink()
            except OSError:
                pass
    try:
        dest.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass  # the copy below reports it properly if it's really stuck
    try:
        shutil.copy2(str(src), str(dest))
    except OSError as e:
        raise InstallError("Failed to copy %s: %s" % (dest.name, e))


def step_program_files(exe, cli_name, install_dir):
    """[4/5] Copy the exe next to the other Utils\\bin tools. Writing under
    Program Files needs admin, so ask for it for THIS copy only (the rest of
    the build stays un-elevated)."""
    if install_dir is None:
        say("Program Files copy skipped.")
        return
    prev = _read_prev_name()
    if is_admin() or not IS_WIN:  # POSIX has no UAC step: write straight to the dir given
        copy_exe(exe, install_dir, cli_name, prev)
        _write_state(cli_name)
        say("Program Files copy updated.")
        return
    say("Requesting admin for Program Files copy only...")
    rc = run_elevated([
        "--admin-copy", "--cli-name", cli_name, "--src", str(exe),
        "--install-dir", str(install_dir), "--prev-name", prev,
    ])
    if rc != 0:
        say("WARNING: Admin copy skipped - using user-local %s." % (cli_name + EXE_SUFFIX))
        return
    # The state file is written HERE, un-elevated: an elevated process may
    # belong to a different account whose ~ isn't yours.
    _write_state(cli_name)
    say("Program Files copy updated.")


def admin_copy_main(args):
    """The elevated half of step 4. Only ever copies; takes everything it
    needs on the command line so it can't drift from what the parent decided."""
    try:
        copy_exe(args.src, args.install_dir, args.cli_name, args.prev_name or "")
        say("[admin] Done.")
        return 0
    except InstallError as e:
        say("ERROR: %s" % e)
        _pause_if_own_window()
        return 1


def step_verify(py, cli_name):
    """[4.5/5] The ACTUAL check, not the version display: resolve the command
    through PATH exactly like a plain invocation would, run its `version`,
    and compare the source hash it reports to jarvis-cli/jarvis/ on disk now.
    Non-fatal: a mismatch means 'you're not about to run what you just
    built' -- worth a loud warning, not worth blocking the server."""
    out = run_tool(py, "verify_exe.py", cli_name)
    if out.stdout:
        say(out.stdout.strip())
    if out.stderr:
        say(out.stderr.strip())


def step_npm(force):
    npm = shutil.which("npm")
    if not npm:
        raise InstallError("npm not found on PATH. Install Node.js (https://nodejs.org), then re-run.")
    if not force and (WEB_DIR / "node_modules").is_dir():
        return
    if subprocess.run([npm, "install"], cwd=str(WEB_DIR)).returncode != 0:
        raise InstallError("npm install failed.")


# ---------------------------------------------------------------------------
# The web server
# ---------------------------------------------------------------------------


def _port():
    try:
        return int(os.environ.get("PORT") or 4173)
    except ValueError:
        return 4173


def serve_window_main():
    """What runs inside the server's own console window (replaces
    web/start-server.bat): show which jarvis the server will use, re-run the
    PATH-vs-source-hash check on every server start (not just at build time --
    PATH order can change underneath you), then run node. If node exits with
    an error the window stays open so the error is readable."""
    _fix_console()
    if IS_WIN:
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleTitleW("Jarvis web server")
        except Exception:
            pass
    jarvis_bin = os.environ.get("JARVIS_BIN")
    if jarvis_bin:
        version = "version unavailable"
        try:
            r = subprocess.run(
                [jarvis_bin, "version"], capture_output=True, text=True, errors="replace", timeout=20
            )
            lines = (r.stdout or "").strip().splitlines()
            if lines:
                version = lines[0]
        except Exception:
            pass
        say("Using jarvis: %s - %s" % (jarvis_bin, version))
    else:
        say("JARVIS_BIN not set - server will auto-detect jarvis on PATH.")

    cli_name = os.environ.get("JARVIS_CLI_NAME")
    if cli_name:
        step_verify(Path(sys.executable), cli_name)
        say()

    say()
    say("Jarvis web UI: http://127.0.0.1:%d" % _port())
    say("Close this window to stop the server.")
    say()

    node = shutil.which("node")
    if not node:
        say("ERROR: node not found on PATH. Install Node.js (https://nodejs.org).")
        _pause_if_own_window()
        return 1
    try:
        rc = subprocess.call([node, "server.js"], cwd=str(WEB_DIR))
    except KeyboardInterrupt:
        return 0
    if rc != 0:
        say("\nnode server.js exited with code %s." % rc)
        _pause_if_own_window()
    return rc


def launch_server(py, jarvis_exe, cli_name, open_browser):
    env = os.environ.copy()
    env["JARVIS_BIN"] = str(jarvis_exe)
    env["JARVIS_CLI_NAME"] = cli_name

    if not IS_WIN:
        # No second console to open here: run the server in this one.
        os.environ.update({"JARVIS_BIN": env["JARVIS_BIN"], "JARVIS_CLI_NAME": cli_name})
        if open_browser:
            try:
                webbrowser.open("http://127.0.0.1:%d" % _port())
            except Exception:
                pass
        return serve_window_main()

    subprocess.Popen(
        [str(py), str(Path(__file__).resolve()), "--serve-window"],
        cwd=str(WEB_DIR),
        env=env,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    say()
    say("Server window opened. JARVIS_BIN=%s (command: %s)" % (jarvis_exe, cli_name))
    if open_browser:
        time.sleep(2)
        try:
            webbrowser.open("http://127.0.0.1:%d" % _port())
        except Exception:
            pass
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="install.py",
        description="Build, install and launch Jarvis. See the top of this file for details.",
    )
    p.add_argument("--serve-only", action="store_true", help="skip the build; just start the web server")
    p.add_argument("--no-browser", action="store_true", help="don't open the web UI in the browser")
    p.add_argument("--with-deps", action="store_true", help="also install jarvis-cli's dependencies")
    p.add_argument("--no-venv", action="store_true", help="ignore any .venv; use plain Python")
    p.add_argument("--venv", metavar="PATH", help="use this venv (folder or python) instead of looking for one")
    p.add_argument(
        "--install-dir", default=None, metavar="DIR",
        help="where the exe is copied (default: Program Files (x86)\\Utils\\bin; empty to skip)",
    )
    # Internal: the elevated copy and the server window re-enter this script.
    p.add_argument("--admin-copy", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--serve-window", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--cli-name", help=argparse.SUPPRESS)
    p.add_argument("--src", help=argparse.SUPPRESS)
    p.add_argument("--prev-name", help=argparse.SUPPRESS)
    return p.parse_args(argv)


def resolve_install_dir(raw):
    if raw is None:
        if not IS_WIN:
            return None
        base = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
        return Path(base) / "Utils" / "bin"
    return Path(raw) if raw.strip() else None


def run(argv):
    args = parse_args(argv)

    if args.admin_copy:
        if not (args.cli_name and args.src and args.install_dir):
            say("ERROR: --admin-copy needs --cli-name, --src and --install-dir.")
            return 2
        return admin_copy_main(args)
    if args.serve_window:
        return serve_window_main()

    # Drop elevation if the whole script was "Run as administrator".
    if IS_WIN and is_admin():
        deelevate_self(argv)
        return 0

    install_dir = resolve_install_dir(args.install_dir)
    py, how = resolve_python(args.venv, args.no_venv)
    info = inspect_python(py)

    say()
    say("==========================================")
    say("         JARVIS BUILD SCRIPT" if not args.serve_only else "         JARVIS WEB SERVER")
    say("==========================================")
    say()
    say("Python: %s  [%s, %s]" % (py, how, info.get("version", "?")))
    say()

    if args.serve_only:
        cli_name = resolve_cli_name_readonly(py)
        try:
            exe = find_exe(py, info, cli_name, install_dir)
        except InstallError as e:
            raise InstallError("%s\nNothing is installed yet? Run once without --serve-only." % e)
        say("Using: %s" % exe)
        say()
        step_npm(force=False)
        return launch_server(py, exe, cli_name, not args.no_browser)

    say("[1/5] Reading equipped persona...")
    cli_name = step_persona(py)
    say("Persona's CLI command will be: %s" % cli_name)
    say("(config still lives in ~/.jarvis regardless of the above)")
    say()

    say("[1.5/5] Bumping build number...")
    step_bump(py)
    say()

    say('[2/5] Installing CLI as "%s"...' % cli_name)
    step_pip(py, args.with_deps)
    say()

    say("[3/5] Locating %s%s..." % (cli_name, EXE_SUFFIX))
    exe = find_exe(py, info, cli_name, install_dir)
    say("Found: %s" % exe)
    say()

    say("[4/5] Updating Program Files copy...")
    step_program_files(exe, cli_name, install_dir)
    say()

    say("[4.5/5] Verifying PATH's %s matches this build..." % cli_name)
    step_verify(py, cli_name)
    say()

    say("[5/5] Starting web server...")
    step_npm(force=True)
    return launch_server(py, exe, cli_name, not args.no_browser)


def main(argv=None):
    _fix_console()
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        return run(argv)
    except InstallError as e:
        say()
        say("ERROR: %s" % e)
        _pause_if_own_window()
        return 1
    except KeyboardInterrupt:
        say("\nCancelled.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
