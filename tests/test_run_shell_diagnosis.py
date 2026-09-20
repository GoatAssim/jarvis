"""Tests for F.4's "diagnosis text" follow-up (master plan Part F):
`run_shell`'s failing WinError 2 used to always get the generic ffmpeg/
git/tesseract advice from tool_diagnosis.py, which is wrong when the real
cause is "the model tried to exec a cmd.exe builtin (dir, type, echo, ...)
directly" rather than "an external program is missing".

Two pieces, both covered here:

1. `_run_shell_impl` (jarvis/actions/code_agent.py) now carries a
   `command` field on every outcome (success, timeout, OSError) — added
   because `tool_diagnosis.diagnose()` only ever sees a tool's name and
   result dict, never the arguments that produced it, so there was no way
   to recover the failing command line otherwise.
2. `tool_diagnosis.diagnose()` special-cases `run_shell` + a WinError-2-
   shaped error: if the command's first word is a known cmd.exe builtin
   (the same `_CMD_BUILTINS` set `_run_shell_impl` itself routes through
   `cmd /c`), it returns a specific "'X' is a cmd.exe builtin" cause/fix
   instead of falling through to the generic missing-external-program
   advice. Every other tool, and every other WinError 2 shape, is
   unaffected — this only ever narrows one specific false-positive.
3. `code_agent`'s own inner loop (`_execute_step`'s `run_shell` branch)
   never goes through `ai_client`'s tool executor — that's where
   `tool_diagnosis.annotate()` normally gets attached — so it never saw
   any of the above. `_run_shell_outcome()` (the short line that becomes
   this step's `log` entry, read by the outer model on both a normal
   recap and F.7's failover transcript) now calls `diagnose()` directly
   for the same builtin case, so the hint reaches the inner loop's own
   failures too, not just the standalone `run_shell` tool's.

Run: python3 tests/test_run_shell_diagnosis.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis.actions import code_agent  # noqa: E402
from jarvis import tool_diagnosis as td  # noqa: E402


# --- `command` field present on every _run_shell_impl outcome --------------

def test_command_field_on_success():
    result, err = code_agent._run_shell_impl("echo hi", cwd=Path("."))
    assert err is None
    assert result["command"] == "echo hi"


def test_command_field_on_oserror():
    # A command that can never exist as a program triggers the OSError
    # branch on any platform (no cmd.exe routing kicks in on non-Windows).
    result, err = code_agent._run_shell_impl(
        "this-program-does-not-exist-xyz --flag", cwd=Path("."))
    assert err is None
    assert result["exit_code"] is None
    assert result["command"] == "this-program-does-not-exist-xyz --flag"


def test_command_field_on_timeout():
    orig_run = code_agent.subprocess.run

    def fake_run(argv, **kwargs):
        import subprocess
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    code_agent.subprocess.run = fake_run
    try:
        result, err = code_agent._run_shell_impl("sleep 999", cwd=Path("."))
    finally:
        code_agent.subprocess.run = orig_run
    assert err is None
    assert result["exit_code"] is None
    assert "timed out" in result["stderr_tail"]
    assert result["command"] == "sleep 999"


def test_command_field_is_the_original_line_not_the_cmd_wrapped_argv():
    # On the routed (builtin) path, `command` must stay the ORIGINAL
    # string the model wrote, not "cmd /c <command>" — diagnosis needs to
    # inspect the real first word, and a caller re-displaying `command`
    # shouldn't see Jarvis's own routing mechanics.
    orig_platform = sys.platform
    orig_split = code_agent._windows_argv_split
    orig_run = code_agent.subprocess.run
    sys.platform = "win32"
    code_agent._windows_argv_split = lambda cmd: cmd.split()

    def fake_run(argv, **kwargs):
        raise FileNotFoundError("stand-in: no cmd.exe on this box")

    code_agent.subprocess.run = fake_run
    try:
        result, err = code_agent._run_shell_impl('dir /a "some dir"', cwd=Path("."))
    finally:
        sys.platform = orig_platform
        code_agent._windows_argv_split = orig_split
        code_agent.subprocess.run = orig_run
    assert err is None
    assert result["command"] == 'dir /a "some dir"'


# --- tool_diagnosis.py's builtin-aware special case -------------------------

def test_diagnose_names_the_builtin_for_run_shell_winerror2():
    fake_result = {
        "exit_code": None, "stdout_tail": "",
        "stderr_tail": "[WinError 2] The system cannot find the file specified",
        "command": 'dir /a "some dir"',
    }
    d = td.diagnose("run_shell", fake_result)
    assert d is not None
    assert "cmd.exe builtin" in d["cause"]
    assert d["fix"] == 'Run it as: cmd /c dir /a "some dir"'


def test_diagnose_is_case_insensitive_on_the_builtin_name():
    fake_result = {
        "stderr_tail": "[WinError 2] The system cannot find the file specified",
        "command": "TYPE notes.txt",
    }
    d = td.diagnose("run_shell", fake_result)
    assert d is not None
    assert "'type' is a cmd.exe builtin" in d["cause"]


def test_diagnose_falls_through_to_generic_advice_for_a_real_missing_program():
    fake_result = {
        "exit_code": None, "stdout_tail": "",
        "stderr_tail": "[WinError 2] The system cannot find the file specified",
        "command": "ffmpeg -version",
    }
    d = td.diagnose("run_shell", fake_result)
    assert d is not None
    assert "cmd.exe builtin" not in d["cause"]
    assert "ffmpeg" in d["fix"] or "install" in d["fix"].lower()


def test_diagnose_falls_through_when_command_field_is_missing():
    # A result shaped like run_shell's but with no `command` key (e.g. an
    # older caller, or a bug) must not crash and must not falsely claim a
    # builtin — it has nothing to check the first word against.
    fake_result = {
        "stderr_tail": "[WinError 2] The system cannot find the file specified",
    }
    d = td.diagnose("run_shell", fake_result)
    assert d is not None
    assert "cmd.exe builtin" not in d["cause"]


def test_diagnose_special_case_never_fires_for_other_tools():
    # Guards against a coincidental "command" key on some other tool's
    # result ever being misread as this shape.
    fake_result = {
        "error": "[WinError 2] The system cannot find the file specified",
        "command": "dir",
    }
    d = td.diagnose("some_other_tool", fake_result)
    assert d is not None
    assert "cmd.exe builtin" not in d["cause"]


def test_diagnose_unaffected_for_non_winerror2_run_shell_failures():
    # A completely unrelated run_shell failure (e.g. a 429-shaped message
    # some command happened to print) must still go through the normal
    # pattern table untouched.
    fake_result = {
        "stderr_tail": "connection refused",
        "command": "curl http://localhost:9999",
    }
    d = td.diagnose("run_shell", fake_result)
    assert d is not None
    assert "Nothing is listening" in d["cause"]


# --- inner dev-agent loop: _run_shell_outcome (the log line, not the tool
# result) also gets the builtin-aware hint -------------------------------

def test_inner_loop_outcome_names_the_builtin():
    result = {
        "exit_code": None, "stdout_tail": "",
        "stderr_tail": "[WinError 2] The system cannot find the file specified",
        "command": "dir /a",
    }
    outcome = code_agent._run_shell_outcome(result)
    assert "cmd.exe builtin" in outcome
    assert "'dir'" in outcome


def test_inner_loop_outcome_falls_through_for_real_missing_program():
    result = {
        "exit_code": None, "stdout_tail": "",
        "stderr_tail": "[WinError 2] The system cannot find the file specified",
        "command": "ffmpeg -version",
    }
    outcome = code_agent._run_shell_outcome(result)
    assert "cmd.exe builtin" not in outcome
    assert "WinError 2" in outcome


def test_inner_loop_outcome_unaffected_for_normal_exit_codes():
    # Guard against the diagnosis hook changing anything on the paths that
    # were already correct before this change.
    assert code_agent._run_shell_outcome(
        {"exit_code": 0, "stdout_tail": "ok", "stderr_tail": ""}) == "exit 0"
    assert code_agent._run_shell_outcome(
        {"exit_code": 1, "stdout_tail": "", "stderr_tail": "boom"}) == "exit 1: boom"


def test_inner_loop_outcome_missing_command_field_does_not_crash():
    # Defensive: an older-shaped result with no `command` key must still
    # produce a sane outcome line, not raise.
    result = {"exit_code": None, "stdout_tail": "",
              "stderr_tail": "[WinError 2] The system cannot find the file specified"}
    outcome = code_agent._run_shell_outcome(result)
    assert "cmd.exe builtin" not in outcome
    assert "WinError 2" in outcome


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
