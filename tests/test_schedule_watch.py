"""Tests for jarvis/actions/scheduler_tools.py's schedule_watch — the
guided front end onto schedule_task for "read the screen, check
conditions in order, act on the first match" prompts, so a caller doesn't
have to hand-compose that shape of prose every time (see
_build_watch_prompt's and tool_schedule_watch's docstrings).

Runnable directly: python3 tests/test_schedule_watch.py
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import notifier, scheduler  # noqa: E402
from jarvis.actions import scheduler_tools  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


def fresh():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis_schedule_watch_test_"))
    scheduler.JARVIS_DIR = tmp
    scheduler.STORE_FILE = tmp / "scheduled.json"
    scheduler.LOCK_FILE = tmp / "scheduled.lock"
    scheduler.ASK_LOG_FILE = tmp / "scheduler_ask_log.jsonl"
    notifier.JARVIS_DIR = tmp
    notifier.INBOX_FILE = tmp / "notifications.json"
    notifier.CONFIG_FILE = tmp / "notify_config.json"
    return tmp


CHECKS = [
    {"if_screen_shows": "tokens are exhausted", "then": "Do nothing."},
    {"if_screen_shows": "everything is done",
     "then": "Tell it to do one of: (1) save scheduler prompts to logs, "
             "(2) fix the API provider issue."},
    {"if_screen_shows": "it didn't finish everything", "then": "Tell it to continue."},
]


def test_generates_an_ordered_prompt_and_creates_a_task_job():
    tmp = fresh()
    try:
        result = scheduler_tools.tool_schedule_watch({
            "when": "at 2:15 pm",
            "context": "You're monitoring another Claude session running in a terminal.",
            "checks": CHECKS,
            "otherwise": "Leave it be and do nothing — it's still running.",
        })
        check("reports ok", result.get("ok") is True, result)
        check("returns a job id", bool(result.get("id")), result)
        check("job kind is task", result.get("kind") == "task", result)

        prompt = result["generated_prompt"]
        check("prompt tells the model to use read_screen, not take_screenshot",
              "read_screen" in prompt and "NOT take_screenshot" in prompt, prompt)
        check("prompt preserves check order (exhausted before done before unfinished)",
              prompt.index("tokens are exhausted") < prompt.index("everything is done")
              < prompt.index("didn't finish everything"), prompt)
        check("each check's action text made it into the prompt",
              "Do nothing." in prompt and "Tell it to continue." in prompt
              and "fix the API provider issue" in prompt, prompt)
        check("the context framing is included",
              "monitoring another Claude session" in prompt, prompt)
        check("the otherwise branch is included",
              "still running" in prompt, prompt)
        check("only-one-action guidance is present",
              "at most" in prompt.lower() or "only take one" in prompt.lower() or "ONE action" in prompt,
              prompt)

        # And it actually landed as a real ask-type task job.
        job = scheduler.get(result["id"])
        check("the created job is an ask action", job["action"]["type"] == "ask", job)
        check("the job's prompt is exactly what was reported back",
              job["action"]["prompt"] == prompt, job["action"]["prompt"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_defaults_title_from_first_check_when_not_given():
    tmp = fresh()
    try:
        result = scheduler_tools.tool_schedule_watch({"when": "in 10 minutes", "checks": CHECKS})
        check("a sensible default title is generated",
              "tokens are exhausted" in result.get("title", ""), result)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_requires_when():
    tmp = fresh()
    try:
        result = scheduler_tools.tool_schedule_watch({"checks": CHECKS})
        check("missing when asks for clarification", result.get("needs_clarification") is True, result)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_requires_at_least_one_check():
    tmp = fresh()
    try:
        result = scheduler_tools.tool_schedule_watch({"when": "in 10 minutes", "checks": []})
        check("empty checks asks for clarification", result.get("needs_clarification") is True, result)
        result2 = scheduler_tools.tool_schedule_watch({"when": "in 10 minutes"})
        check("missing checks entirely asks for clarification", result2.get("needs_clarification") is True, result2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_rejects_a_malformed_check():
    tmp = fresh()
    try:
        result = scheduler_tools.tool_schedule_watch({
            "when": "in 10 minutes",
            "checks": [{"if_screen_shows": "done"}],  # missing "then"
        })
        check("a check missing 'then' is a clear error, not a crash",
              "error" in result and "checks[0]" in result["error"], result)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_defaults_otherwise_to_do_nothing():
    tmp = fresh()
    try:
        result = scheduler_tools.tool_schedule_watch({"when": "in 10 minutes", "checks": CHECKS})
        check("a default 'do nothing' otherwise branch is used when omitted",
              "Do nothing." in result["generated_prompt"].splitlines()[-1], result["generated_prompt"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_registered_in_tools_and_schemas():
    check("schedule_watch is in TOOLS", "schedule_watch" in scheduler_tools.TOOLS)
    check("schedule_watch has a schema entry",
          any(s["name"] == "schedule_watch" for s in scheduler_tools.TOOL_SCHEMAS))
    schema = next(s for s in scheduler_tools.TOOL_SCHEMAS if s["name"] == "schedule_watch")
    check("checks and when are required", set(schema["parameters"]["required"]) == {"when", "checks"}, schema)


for fn in [
    test_generates_an_ordered_prompt_and_creates_a_task_job,
    test_defaults_title_from_first_check_when_not_given,
    test_requires_when,
    test_requires_at_least_one_check,
    test_rejects_a_malformed_check,
    test_defaults_otherwise_to_do_nothing,
    test_registered_in_tools_and_schemas,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
