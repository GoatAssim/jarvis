"""Daemon commands are split the way the OS will see them (L.27 follow-up).

On Windows `daemons.add()` / the editor used `shlex.split(cmd, posix=False)`,
which leaves the quote characters INSIDE the tokens. A command such as

    "C:\\Program Files\\Python\\python.exe" -c "import time; time.sleep(600)"

became an argv[0] that literally started with a `"`, and Popen failed with
[WinError 2] -- the failure seen in test_l26 / test_l27 on a Windows box.
`daemons._split_command` fixes that (CommandLineToArgvW on Windows, a
quote-stripping shlex fallback if the WinAPI call is unavailable, plain POSIX
shlex elsewhere).

The Windows branches are exercised here through the `windows=` override, so
they run on any OS: on Linux `ctypes.windll` does not exist and the FALLBACK
is what is tested; on a real Windows box the same asserts go through the real
CommandLineToArgvW. The end-to-end checks (add -> registry -> argv) run on
whatever platform this is.

    python3 tests/test_daemon_command_split.py
"""

import os
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "jarvis-cli"
sys.path.insert(0, str(PKG))

HOME = tempfile.mkdtemp(prefix="jarvis_split_")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["JARVIS_HOME"] = os.path.join(HOME, ".jarvis")

from jarvis import daemons  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


EXE = r"C:\Program Files\Python\python.exe"
CMD = f'"{EXE}" -c "import time; time.sleep(600)"'


def test_windows_quoted_exe_path_loses_its_quotes():
    argv = daemons._split_command(CMD, windows=True)
    check("windows: argv[0] is the bare path, no literal quotes", argv[0] == EXE, argv)
    check("windows: quoted -c payload is ONE token without quotes",
          argv[1:] == ["-c", "import time; time.sleep(600)"], argv)


def test_windows_plain_command_is_unchanged():
    argv = daemons._split_command("node server.js --port 3000", windows=True)
    check("windows: unquoted command splits on spaces", argv == ["node", "server.js", "--port", "3000"], argv)


def test_windows_empty_command_is_empty_not_this_executable():
    # CommandLineToArgvW("") returns the CURRENT exe's path; that must never
    # leak into a daemon's argv.
    check("windows: '' -> []", daemons._split_command("", windows=True) == [])
    check("windows: whitespace -> []", daemons._split_command("   ", windows=True) == [])


def test_posix_split_is_unchanged():
    argv = daemons._split_command('python -c "print(1)" \'a b\'', windows=False)
    check("posix: quotes consumed, spaces kept", argv == ["python", "-c", "print(1)", "a b"], argv)


def test_add_stores_an_argv_with_no_literal_quotes():
    ok, err = daemons.add("split-quoted", CMD if os.name == "nt" else
                          f'"{sys.executable}" -c "import time; time.sleep(600)"')
    check("add() accepts a quoted command", ok, err)
    argv = daemons.get("split-quoted")["argv"]
    check("stored argv[0] has no literal quote characters", '"' not in argv[0], argv)
    check("stored -c payload is a single unquoted token",
          len(argv) == 3 and argv[1] == "-c" and '"' not in argv[2], argv)
    daemons.remove("split-quoted")


def test_editing_argv_uses_the_same_split():
    daemons.add("split-edit", f'"{sys.executable}" -c "pass"')
    ok, err = daemons.edit("split-edit", argv=f'"{sys.executable}" -c "import sys; sys.exit(3)"')
    check("editing argv from a string works", ok, err)
    argv = daemons.get("split-edit")["argv"]
    check("edited argv has no literal quote characters", all('"' not in a for a in argv), argv)
    daemons.remove("split-edit")


if __name__ == "__main__":
    for fn in [v for k, v in list(globals().items()) if k.startswith("test_")]:
        fn()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
