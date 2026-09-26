"""Tests for the F.6 fix (master plan Part F): code_agent's inner loop had
no idea what its own round budget was, so it spent rounds probing (list_dir,
read_file, then three more shell probes) instead of editing once the fix was
obvious, and hit the ceiling with edit_file never called. Its result then
told the outer model nothing about what those wasted rounds had actually
found ({"ok": false, "steps": [...]} where every step was just a bare tool
name) — see F.7 for the consequence (the outer model re-does the work).

The fix, in three parts:
1. `_code_agent_system_prompt(round_limit)` bakes the REAL per-attempt round
   limit into the prompt as a number, with explicit "edit now if the fix is
   evident" / "your last request must be tool-less" guidance.
2. `_step_outcome` gives every completed step a short, tool-specific outcome
   line (e.g. "31 lines", "exit 1: ModuleNotFoundError...") instead of
   nothing; `tool_code_agent`'s result gains a compact `log` list built from
   these, alongside the existing raw `steps` telemetry.
3. `TOOL_RESULT_SPECS["code_agent"]` drops the bulky `steps` list at "low"
   verbosity (mirroring dev_agent's own treatment) and caps `last_error`,
   since `log` is what a tight recap should read instead.

Note on scope: whether a REAL model actually economizes its calls given the
new prompt can't be checked here (no live model/network access in this
sandbox — same caveat as F.4's "not verified against a real Windows box").
What's covered instead is everything that doesn't require a live model: the
prompt text itself, the outcome/log mechanics end to end through
`tool_code_agent`'s public interface (via a fake `_run_agent_loop` that
drives the real `executor`, the way a model would), and the result shaping.

Run: python3 tests/test_code_agent_budget_and_outcomes.py
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

# jarvis.tools scans a ~/.jarvis/tools/ directory for auto-discovered action
# modules at import time — redirect HOME before importing it (AGENTS.md).
_TMP_HOME = tempfile.mkdtemp(prefix="jarvis-codeagent-shaping-test-")
os.environ["HOME"] = _TMP_HOME
os.environ["USERPROFILE"] = _TMP_HOME
Path(_TMP_HOME, ".jarvis").mkdir(parents=True, exist_ok=True)

from jarvis.actions import code_agent  # noqa: E402
from jarvis import tool_result_shaping  # noqa: E402
from jarvis import tools as _tools  # noqa: E402,F401 — importing this is what actually
# runs tools.py's best-effort merge of every action module's own
# TOOL_RESULT_SPECS into tool_result_shaping.TOOL_RESULT_SPECS (see
# tools.py's "Best-effort merges" block). Without this import,
# code_agent.py's own TOOL_RESULT_SPECS never reaches the global dict and
# shape_result("code_agent", ...) below would silently no-op.

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


def _project(files):
    d = Path(tempfile.mkdtemp(prefix="ca-"))
    for name, content in files.items():
        path = d / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return d


# ---------------------------------------------------------------------------
# 1. The budget prompt actually states the real number, and the guidance
#    F.6 asked for is present.
# ---------------------------------------------------------------------------

def test_prompt_states_the_real_round_limit():
    p5 = code_agent._code_agent_system_prompt(5)
    p1 = code_agent._code_agent_system_prompt(1)
    check("plural budget wording for 5", "at most 5 tool calls for this" in p5, p5)
    check("singular budget wording for 1", "at most 1 tool call for this" in p1, p1)
    check("no accidental plural for 1", "1 tool calls" not in p1, p1)
    check("changing round_limit changes the prompt", p5 != p1)


def test_prompt_tells_the_model_to_edit_promptly_and_reserve_the_summary():
    p = code_agent._code_agent_system_prompt(5)
    check("tells it to edit now once the fix is evident", "edit now" in p, p)
    check("tells it the last request must be tool-less", "tool-less text summary" in p, p)


def test_prompt_still_carries_the_f5_dotenv_guidance():
    """Regression: rewriting the constant into a function must not lose the
    F.5-era guidance about .env/hidden files."""
    p = code_agent._code_agent_system_prompt(5)
    check("still mentions hidden dotfiles", "hidden" in p, p)
    check("still mentions masked .env values", "•••• (N chars)" in p, p)


# ---------------------------------------------------------------------------
# 2. _step_outcome / _run_shell_outcome — one short line per tool, matching
#    the master plan's own examples (list_dir -> "1 entry", etc).
# ---------------------------------------------------------------------------

def test_step_outcome_list_dir():
    check("empty dir", code_agent._step_outcome("list_dir", {"entry_count": 0}, None) == "0 entries")
    check("singular entry", code_agent._step_outcome("list_dir", {"entry_count": 1}, None) == "1 entry")
    out = code_agent._step_outcome("list_dir", {"entry_count": 3, "hidden": [".env"]}, None)
    check("plural + hidden note", out == "3 entries (+1 hidden)", out)


def test_step_outcome_read_file():
    out = code_agent._step_outcome("read_file", {"total_lines": 31}, None)
    check("matches master plan's own example", out == "31 lines", out)
    masked = code_agent._step_outcome("read_file", {"total_lines": 5, "values_masked": True}, None)
    check("notes when values were masked", masked == "5 lines (masked)", masked)


def test_step_outcome_search_code():
    out = code_agent._step_outcome("search_code", {"matches": [{}, {}, {}], "files_scanned": 10}, None)
    check("plural matches", out == "3 matches in 10 files", out)
    out1 = code_agent._step_outcome("search_code", {"matches": [{}], "files_scanned": 2}, None)
    check("singular match", out1 == "1 match in 2 files", out1)
    truncated = code_agent._step_outcome("search_code", {"matches": [{}], "files_scanned": 2, "truncated": True}, None)
    check("notes truncation", truncated.endswith("(truncated)"), truncated)


def test_step_outcome_edit_and_write_file():
    e = code_agent._step_outcome("edit_file", {"bytes_written": 120}, None)
    check("edit_file outcome", e == "edited, 120 bytes written", e)
    w = code_agent._step_outcome("write_file", {"bytes_written": 45}, None)
    check("write_file outcome", w == "created, 45 bytes written", w)


def test_step_outcome_run_shell_success_and_failure():
    ok = code_agent._step_outcome("run_shell", {"exit_code": 0, "stdout_tail": "hi", "stderr_tail": ""}, None)
    check("clean exit", ok == "exit 0", ok)

    failed = code_agent._step_outcome(
        "run_shell",
        {"exit_code": 1, "stdout_tail": "", "stderr_tail": "ModuleNotFoundError: No module named 'foo'"},
        None,
    )
    check("nonzero exit surfaces the real stderr line", failed == "exit 1: ModuleNotFoundError: No module named 'foo'", failed)

    winerr = code_agent._step_outcome(
        "run_shell",
        {"exit_code": None, "stdout_tail": "", "stderr_tail": "[WinError 2] The system cannot find the file specified"},
        None,
    )
    check("a process that never started is distinguished from exit 0",
          winerr == "did not run — [WinError 2] The system cannot find the file specified", winerr)


def test_step_outcome_error_path_and_unknown_tool():
    err_out = code_agent._step_outcome("edit_file", None, "old_str not found — re-read the file")
    check("error path returns the error text, not a crash", err_out == "old_str not found — re-read the file", err_out)

    long_err = code_agent._step_outcome("edit_file", None, "x" * 500)
    check("long errors are still capped to one short line", len(long_err) <= 150, len(long_err))

    unknown = code_agent._step_outcome("some_future_tool", {"whatever": 1}, None)
    check("unrecognized tool name never raises, falls back cleanly", unknown == "done", unknown)


def test_step_outcome_never_raises_on_garbage_input():
    for name in ("list_dir", "read_file", "search_code", "edit_file", "write_file", "run_shell"):
        try:
            out = code_agent._step_outcome(name, "not a dict", None)
            ok = isinstance(out, str)
        except Exception as e:  # noqa: BLE001
            ok = False
            out = e
        check(f"{name} tolerates a non-dict result", ok, out)


# ---------------------------------------------------------------------------
# 3. End to end through tool_code_agent(): a fake _run_agent_loop drives the
#    REAL executor (the way an actual model's tool calls would), so this
#    exercises the real steps/log/outcome wiring without needing a live
#    provider. Mirrors F.6's own "Tests" note (replay Case 1, edit_file
#    reached in <=4 calls) as closely as is possible without a live model.
# ---------------------------------------------------------------------------

def test_end_to_end_success_reaches_edit_within_budget_and_logs_it():
    original = "import os\nTOKEN = None\nprint(TOKEN)\n"
    new_str = "TOKEN = os.getenv(\"TOKEN\")"
    edited = original.replace("TOKEN = None", new_str, 1)
    expected_bytes = len(edited.encode("utf-8"))  # what _edit_file_impl will
    # actually report — computed from first principles, not guessed, since
    # bytes_written is the length of the WHOLE new file, not just the diff.

    d = _project({"main.py": original})
    orig = code_agent._run_agent_loop

    def fake_loop(task, schemas, executor, round_limit):
        executor("list_dir", {"path": "."})
        executor("read_file", {"path": "main.py"})
        executor("edit_file", {"path": "main.py", "old_str": "TOKEN = None", "new_str": new_str})
        return "Read main.py, replaced the placeholder with os.getenv(\"TOKEN\").", None, None

    code_agent._run_agent_loop = fake_loop
    try:
        result = code_agent.tool_code_agent({"task": "fix the token", "root": str(d)})
    finally:
        code_agent._run_agent_loop = orig
        shutil.rmtree(d, ignore_errors=True)

    check("ok", result.get("ok") is True, result)
    check("edit_file reached within F.6's <=4 call target", result.get("tool_calls", 99) <= 4, result.get("tool_calls"))
    check("log has one line per call", result.get("log") == [
        "list_dir → 1 entry",
        "read_file → 3 lines",
        f"edit_file → edited, {expected_bytes} bytes written",
    ], result.get("log"))
    ok_tool_steps = [s for s in result["steps"] if s.get("status") == "ok" and s.get("phase") == "step"]
    check("every per-tool ok step carries an outcome field", len(ok_tool_steps) == 3 and all("outcome" in s for s in ok_tool_steps), ok_tool_steps)


def test_end_to_end_failure_still_reports_last_error_and_a_useful_log():
    d = _project({"main.py": "import sys\nprint('hi')\n"})
    orig = code_agent._run_agent_loop

    def fake_loop(task, schemas, executor, round_limit):
        # Simulates a run that probes instead of editing and gives up —
        # the F.6 evidence shape — but with a REAL run_shell call so the
        # outcome logic is exercised against real subprocess output rather
        # than a synthetic dict.
        executor("list_dir", {"path": "."})
        executor("read_file", {"path": "main.py"})
        executor("run_shell", {"command": "python3 -c \"print(1)\""})
        executor("run_shell", {"command": "python3 -c \"import sys; sys.exit(3)\""})
        return None, "gemini: gave up after 5 rounds of tool calls with no final answer", None

    code_agent._run_agent_loop = fake_loop
    try:
        result = code_agent.tool_code_agent({"task": "fix something", "root": str(d)})
    finally:
        code_agent._run_agent_loop = orig
        shutil.rmtree(d, ignore_errors=True)

    check("ok is False", result.get("ok") is False, result)
    check("reason is agent_failed", result.get("reason") == "agent_failed", result.get("reason"))
    check("last_error is the model's real give-up message",
          result.get("last_error") == "gemini: gave up after 5 rounds of tool calls with no final answer",
          result.get("last_error"))
    log = result.get("log") or []
    check("log has 4 entries, one per call", len(log) == 4, log)
    check("log shows the successful shell call", log[2] == "run_shell → exit 0", log)
    check("log shows the failing shell call's real exit code", log[3] == "run_shell → exit 3", log)
    check("no last_words key when the loop didn't supply one",
          "last_words" not in result, result)


def test_end_to_end_failure_surfaces_last_words_separately_from_last_error():
    # F.6/K.3.5: when ai_providers did capture the model's own narration
    # from the give-up round (ai_providers.AIResult.last_words), it must
    # reach the caller as its own field — distinct from `last_error`, the
    # harness's diagnostic string — not get folded into or replace it.
    d = _project({"main.py": "import sys\nprint('hi')\n"})
    orig = code_agent._run_agent_loop

    def fake_loop(task, schemas, executor, round_limit):
        executor("list_dir", {"path": "."})
        return (
            None,
            "gemini: gave up after 5 rounds of tool calls with no final answer",
            "I found the bug in main.py's import order but ran out of calls before fixing it.",
        )

    code_agent._run_agent_loop = fake_loop
    try:
        result = code_agent.tool_code_agent({"task": "fix something", "root": str(d)})
    finally:
        code_agent._run_agent_loop = orig
        shutil.rmtree(d, ignore_errors=True)

    check("ok is False", result.get("ok") is False, result)
    check("last_error is still the harness's diagnostic",
          result.get("last_error") == "gemini: gave up after 5 rounds of tool calls with no final answer",
          result.get("last_error"))
    check("last_words carries the model's own narration, unchanged",
          result.get("last_words") == "I found the bug in main.py's import order but ran out of calls before fixing it.",
          result.get("last_words"))
    check("last_words is not last_error", result.get("last_words") != result.get("last_error"))


# ---------------------------------------------------------------------------
# 4. TOOL_RESULT_SPECS shaping — steps dropped at low, kept (minus
#    arguments) at medium, log always present, last_error capped.
# ---------------------------------------------------------------------------

def _sample_failed_result():
    return {
        "ok": False,
        "job_id": "abc123def456",
        "root": "/tmp/ca-xxxxxxxx",
        "steps": [
            {"job_id": "abc123def456", "seq": 0, "phase": "plan", "status": "start", "task": "fix it", "root": "/tmp/ca-xxxxxxxx"},
            {"job_id": "abc123def456", "seq": 1, "phase": "step", "status": "start", "tool": "list_dir", "arguments": {"path": "."}},
            {"job_id": "abc123def456", "seq": 2, "phase": "step", "status": "ok", "tool": "list_dir", "outcome": "1 entry"},
            {"job_id": "abc123def456", "seq": 3, "phase": "step", "status": "start", "tool": "read_file", "arguments": {"path": "main.py"}},
            {"job_id": "abc123def456", "seq": 4, "phase": "step", "status": "ok", "tool": "read_file", "outcome": "31 lines"},
            {"job_id": "abc123def456", "seq": 5, "phase": "done", "status": "fail", "error": "gave up", "tool_calls": 2},
        ],
        "tool_calls": 2,
        "reason": "agent_failed",
        "last_error": "gemini: gave up after 5 rounds of tool calls with no final answer",
        "log": ["list_dir → 1 entry", "read_file → 31 lines"],
    }


def test_shaping_drops_steps_at_low_but_keeps_log():
    shaped = tool_result_shaping.shape_result("code_agent", _sample_failed_result(), "low")
    check("steps dropped at low", "steps" not in shaped, shaped.keys())
    check("log survives at low", shaped.get("log") == ["list_dir → 1 entry", "read_file → 31 lines"], shaped.get("log"))
    check("last_error still present (just capped)", "last_error" in shaped)
    size = len(json.dumps(shaped))
    check(f"shaped result is compact (~600 char target, got {size})", size < 600, size)


def test_shaping_keeps_steps_at_medium_but_drops_arguments():
    shaped = tool_result_shaping.shape_result("code_agent", _sample_failed_result(), "medium")
    check("steps kept at medium", "steps" in shaped)
    check("no step item still carries arguments at medium",
          all("arguments" not in s for s in shaped["steps"]), shaped["steps"])
    check("outcome survives on the ok steps at medium",
          any(s.get("outcome") == "31 lines" for s in shaped["steps"]), shaped["steps"])


def test_shaping_caps_a_long_last_error():
    huge = dict(_sample_failed_result())
    huge["last_error"] = "provider A failed: " + ("boom " * 200)
    low = tool_result_shaping.shape_result("code_agent", huge, "low")
    medium = tool_result_shaping.shape_result("code_agent", huge, "medium")
    suffix_len = len(tool_result_shaping._TRIM_SUFFIX)
    check("last_error capped at low (150 chars + suffix)", len(low["last_error"]) <= 150 + suffix_len, len(low["last_error"]))
    check("last_error capped at medium (300 chars + suffix)", len(medium["last_error"]) <= 300 + suffix_len, len(medium["last_error"]))
    check("low cap is tighter than medium cap", len(low["last_error"]) < len(medium["last_error"]))


def test_shaping_caps_a_long_last_words_independently_of_last_error():
    huge = dict(_sample_failed_result())
    huge["last_words"] = "well, I think the fix is " + ("probably in there somewhere " * 100)
    low = tool_result_shaping.shape_result("code_agent", huge, "low")
    medium = tool_result_shaping.shape_result("code_agent", huge, "medium")
    suffix_len = len(tool_result_shaping._TRIM_SUFFIX)
    check("last_words capped at low (150 chars + suffix)", len(low["last_words"]) <= 150 + suffix_len, len(low["last_words"]))
    check("last_words capped at medium (300 chars + suffix)", len(medium["last_words"]) <= 300 + suffix_len, len(medium["last_words"]))
    check("last_error is untouched by last_words' own cap", low["last_error"] == huge["last_error"], low["last_error"])


def test_shaping_is_a_no_op_at_full_verbosity():
    sample = _sample_failed_result()
    shaped = tool_result_shaping.shape_result("code_agent", sample, "full")
    check("full verbosity returns the exact same object untouched", shaped is sample)


for fn in [
    test_prompt_states_the_real_round_limit,
    test_prompt_tells_the_model_to_edit_promptly_and_reserve_the_summary,
    test_prompt_still_carries_the_f5_dotenv_guidance,
    test_step_outcome_list_dir,
    test_step_outcome_read_file,
    test_step_outcome_search_code,
    test_step_outcome_edit_and_write_file,
    test_step_outcome_run_shell_success_and_failure,
    test_step_outcome_error_path_and_unknown_tool,
    test_step_outcome_never_raises_on_garbage_input,
    test_end_to_end_success_reaches_edit_within_budget_and_logs_it,
    test_end_to_end_failure_still_reports_last_error_and_a_useful_log,
    test_end_to_end_failure_surfaces_last_words_separately_from_last_error,
    test_shaping_drops_steps_at_low_but_keeps_log,
    test_shaping_keeps_steps_at_medium_but_drops_arguments,
    test_shaping_caps_a_long_last_error,
    test_shaping_caps_a_long_last_words_independently_of_last_error,
    test_shaping_is_a_no_op_at_full_verbosity,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
