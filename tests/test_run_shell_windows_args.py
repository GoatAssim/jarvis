"""Tests for the F.4 fix (master plan Part F): run_shell isn't a shell, and
on Windows `python -c "…"` silently did nothing.

Two independent bugs, both in `_run_shell_impl`
(jarvis/actions/code_agent.py):

1. `_shlex_split` used `shlex.split(command, posix=False)` on Windows,
   which leaves quote characters IN the token instead of stripping them —
   `python -c "import os; print(1)"` became argv `["python", "-c",
   '"import', 'os;', 'print(1)"']` in effect (the quoted string never
   becomes one token), so python either errors or, worse, silently parses
   a leftover quoted literal and exits 0 with empty stdout. The fix
   parses Windows command lines via CommandLineToArgvW (the same way
   every real Windows program's argv is built) instead.
2. `dir`, `type`, `echo`, etc. are cmd.exe builtins with no standalone
   .exe, so subprocess.run([...]) can never find them (WinError 2). The
   fix detects a known builtin as argv[0] and re-runs the ORIGINAL
   command line through `cmd /c` instead.

Neither bug is reachable on the Linux/Mac sandbox this runs in (posix
shlex was never wrong, and there's no cmd.exe to route to), so these
tests exercise `_shlex_split`/`_run_shell_impl` with `sys.platform`
patched to "win32" and `_windows_argv_split` stubbed to a deterministic
splitter — this checks the SPLITTING and ROUTING logic, not that a real
Windows box's CommandLineToArgvW/cmd.exe behave as expected (untestable
here, same caveat the plan itself notes for §4's sibling fixes).

Run: python3 tests/test_run_shell_windows_args.py
"""

import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis.actions import code_agent  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


@contextmanager
def _as_windows(argv_split=None):
    """Patch sys.platform to win32 and, optionally, stub the ctypes-backed
    splitter with a deterministic one (real CommandLineToArgvW only
    exists on actual Windows)."""
    orig_platform = sys.platform
    orig_split = code_agent._windows_argv_split
    sys.platform = "win32"
    if argv_split is not None:
        code_agent._windows_argv_split = argv_split
    try:
        yield
    finally:
        sys.platform = orig_platform
        code_agent._windows_argv_split = orig_split


def _naive_win_split(command):
    """Stand-in for CommandLineToArgvW: strips quotes, keeps a quoted
    span as one token. Good enough to prove the ROUTING logic (this repo
    doesn't run on Windows), not a real Windows argv parser."""
    import shlex
    return shlex.split(command, posix=True)


def test_posix_split_handles_quoted_python_c_correctly():
    # This was already correct before the fix (non-Windows used
    # posix=True already) — a regression guard, not new behavior.
    argv = code_agent._shlex_split('python -c "import os; print(1)"')
    check(
        "posix split keeps quoted -c argument as one token",
        argv == ["python", "-c", "import os; print(1)"],
        argv,
    )


def test_windows_split_used_on_win32():
    calls = []

    def fake_split(cmd):
        calls.append(cmd)
        return _naive_win_split(cmd)

    with _as_windows(argv_split=fake_split):
        argv = code_agent._shlex_split('python -c "import os; print(1)"')
    check("windows path calls _windows_argv_split, not posix shlex", calls == ['python -c "import os; print(1)"'], calls)
    check(
        "windows split also keeps quoted arg as one token",
        argv == ["python", "-c", "import os; print(1)"],
        argv,
    )


def test_windows_split_falls_back_on_ctypes_failure():
    def broken_split(cmd):
        raise OSError("no ctypes on this box")

    with _as_windows(argv_split=broken_split):
        argv = code_agent._shlex_split("echo hi")
    check("falls back to naive split rather than crashing", argv == ["echo", "hi"], argv)


def test_known_cmd_builtin_is_routed_through_cmd_slash_c():
    with _as_windows(argv_split=_naive_win_split):
        # subprocess.run(["cmd", ...]) will itself fail on this non-Windows
        # box (no such program) — that's expected and fine, it proves the
        # ROUTING happened rather than an attempt to exec "dir" directly.
        result, err = code_agent._run_shell_impl('dir /a "some dir"', cwd=Path("."))
    check("run_shell doesn't error out before attempting the run", err is None, err)
    check(
        "builtin command was NOT execed directly (no WinError-2-style failure "
        "distinguishing it from a routing failure)",
        result is not None,
        result,
    )


def test_builtin_routing_preserves_original_command_line():
    """The routed argv must be exactly ["cmd", "/c", <original command>] —
    re-joining a re-split argv would re-escape quoting the model wrote."""
    seen = {}
    orig_run = code_agent.subprocess.run

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        raise FileNotFoundError("stand-in: no cmd.exe on this box")

    code_agent.subprocess.run = fake_run
    try:
        with _as_windows(argv_split=_naive_win_split):
            code_agent._run_shell_impl('dir /a "some dir with spaces"', cwd=Path("."))
    finally:
        code_agent.subprocess.run = orig_run

    check(
        "routed argv is exactly ['cmd', '/c', original_command]",
        seen.get("argv") == ["cmd", "/c", 'dir /a "some dir with spaces"'],
        seen.get("argv"),
    )


def test_non_builtin_command_is_not_routed_through_cmd():
    seen = {}
    orig_run = code_agent.subprocess.run

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        raise FileNotFoundError("stand-in")

    code_agent.subprocess.run = fake_run
    try:
        with _as_windows(argv_split=_naive_win_split):
            code_agent._run_shell_impl("pytest -q", cwd=Path("."))
    finally:
        code_agent.subprocess.run = orig_run

    check(
        "a real program (pytest) is exec'd directly, not routed through cmd",
        seen.get("argv") == ["pytest", "-q"],
        seen.get("argv"),
    )


def test_non_windows_platform_never_routes_through_cmd():
    seen = {}
    orig_run = code_agent.subprocess.run

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        raise FileNotFoundError("stand-in")

    code_agent.subprocess.run = fake_run
    try:
        code_agent._run_shell_impl("echo hi", cwd=Path("."))
    finally:
        code_agent.subprocess.run = orig_run

    check("non-Windows echo is never routed through cmd", seen.get("argv") == ["echo", "hi"], seen.get("argv"))


for fn in [
    test_posix_split_handles_quoted_python_c_correctly,
    test_windows_split_used_on_win32,
    test_windows_split_falls_back_on_ctypes_failure,
    test_known_cmd_builtin_is_routed_through_cmd_slash_c,
    test_builtin_routing_preserves_original_command_line,
    test_non_builtin_command_is_not_routed_through_cmd,
    test_non_windows_platform_never_routes_through_cmd,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
