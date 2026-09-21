"""D6 (F.12 "confirm churn"): a small, fixed allow-list of genuinely
read-only run_shell commands (dir, type, where, echo) skips the
confirm/ai_review gate entirely, so the same read-only `dir` doesn't cost
a prompt AND a risk-review call every single time (F.12 evidence: one such
confirm cost the user 32 seconds). See tool_safety.is_allowlisted_read_only_shell
and its call site in ai_client._make_tool_executor.

Run: python3 tests/test_readonly_shell_confirm.py
"""
import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_client, tool_safety, tools as system_tools  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


# ---------------------------------------------------------------------------
# Unit tests: the classifier itself, in both directions.
# ---------------------------------------------------------------------------

def test_allowlisted_exact_commands():
    for cmd in ("dir", "dir /a", "DIR", "  dir  ", "type file.txt",
                "where python", "echo hi", "Echo Hello There"):
        check(f"allowlisted: {cmd!r}", tool_safety.is_allowlisted_read_only_shell(cmd), cmd)


def test_not_allowlisted_by_default():
    for cmd in ("del file.txt", "rmdir /s /q C:\\", "python -c \"import os\"",
                "directory_scan", "dirty_hack.exe", "", None, 123):
        check(f"NOT allowlisted: {cmd!r}", not tool_safety.is_allowlisted_read_only_shell(cmd), cmd)


def test_metacharacters_disqualify_even_with_safe_prefix():
    # The whole point: nothing can ride along after a safe-looking prefix.
    for cmd in ("dir & del *", "dir; rm -rf /", "echo hi > startup.cmd",
                "dir | more", "type file.txt `whoami`", "echo $(rm -rf /)",
                "dir\ndel *"):
        check(f"metachar disqualifies: {cmd!r}", not tool_safety.is_allowlisted_read_only_shell(cmd), cmd)


# ---------------------------------------------------------------------------
# Integration: through the real confirm gate in _make_tool_executor.
# ---------------------------------------------------------------------------

def _stub_run_shell(monkeypatched_result):
    """Stubs system_tools.execute_tool so run_shell never actually spawns a
    process — matches the pattern test_enhancements.py already uses for
    click_on_text."""
    original = system_tools.execute_tool

    def fake(name, arguments=None, verbosity=None, context=None):
        if name == "run_shell":
            return dict(monkeypatched_result)
        return original(name, arguments, verbosity, context=context)

    return fake, original


def test_allowlisted_run_shell_skips_confirm_entirely():
    fake, original = _stub_run_shell({"ok": True, "stdout": "a.txt\nb.txt", "returncode": 0})
    system_tools.execute_tool = fake
    confirm_calls = []
    try:
        executor = ai_client._make_tool_executor(
            on_tool_call=None, schemas=None,
            on_confirm_request=lambda *a, **k: confirm_calls.append((a, k)) or True,
        )
        result = executor("run_shell", {"command": "dir /a"})
        check("allowlisted run_shell actually ran (not cancelled)",
              bool(result.get("ok")) and not result.get("cancelled"), result)
        check("on_confirm_request was never called for an allowlisted command",
              confirm_calls == [], confirm_calls)
    finally:
        system_tools.execute_tool = original


def test_non_allowlisted_run_shell_still_gated():
    fake, original = _stub_run_shell({"ok": True, "stdout": "", "returncode": 0})
    system_tools.execute_tool = fake
    confirm_calls = []
    try:
        executor = ai_client._make_tool_executor(
            on_tool_call=None, schemas=None,
            on_confirm_request=lambda *a, **k: confirm_calls.append((a, k)) or True,
        )
        result = executor("run_shell", {"command": "del file.txt"})
        check("non-allowlisted run_shell still ran (approved)",
              bool(result.get("ok")) and not result.get("cancelled"), result)
        check("on_confirm_request WAS called for a non-allowlisted command",
              len(confirm_calls) == 1, confirm_calls)
    finally:
        system_tools.execute_tool = original


def test_non_allowlisted_run_shell_declined_still_cancels():
    fake, original = _stub_run_shell({"ok": True, "stdout": "", "returncode": 0})
    system_tools.execute_tool = fake
    try:
        executor = ai_client._make_tool_executor(
            on_tool_call=None, schemas=None,
            on_confirm_request=lambda *a, **k: False,
        )
        result = executor("run_shell", {"command": "del file.txt"})
        check("declined non-allowlisted run_shell is cancelled, not run",
              result.get("cancelled") is True, result)
    finally:
        system_tools.execute_tool = original


def test_allowlist_bypass_is_specific_to_run_shell():
    # A different confirm-gated tool with an argument that happens to look
    # like an allowlisted shell command must NOT be affected — this is a
    # run_shell-only carve-out, not a generic "command"-argument bypass.
    original = system_tools.execute_tool
    system_tools.execute_tool = lambda name, arguments=None, verbosity=None, context=None: (
        {"ok": True} if name == "write_file" else original(name, arguments, verbosity, context=context)
    )
    confirm_calls = []
    try:
        executor = ai_client._make_tool_executor(
            on_tool_call=None, schemas=None,
            on_confirm_request=lambda *a, **k: confirm_calls.append((a, k)) or True,
        )
        executor("write_file", {"path": "dir", "content": "x"})
        check("write_file is unaffected by the run_shell-only allow-list",
              len(confirm_calls) == 1, confirm_calls)
    finally:
        system_tools.execute_tool = original


for fn in (test_allowlisted_exact_commands, test_not_allowlisted_by_default,
           test_metacharacters_disqualify_even_with_safe_prefix,
           test_allowlisted_run_shell_skips_confirm_entirely,
           test_non_allowlisted_run_shell_still_gated,
           test_non_allowlisted_run_shell_declined_still_cancels,
           test_allowlist_bypass_is_specific_to_run_shell):
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    sys.exit(1)
