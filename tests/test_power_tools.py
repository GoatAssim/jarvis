"""Tests for jarvis/actions/power_tools.py (master plan L.16, Q-L16b).

Run: python3 tests/test_power_tools.py

Nothing here ever shuts down, restarts, sleeps or locks a machine: every test
either calls build_command() (pure) or replaces power_tools._run. The one test
that goes through the scheduler never fires the job.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

# Belt and braces: even if a future edit forgets to patch _run, the handler
# only prints the command.
os.environ["JARVIS_POWER_DRY_RUN"] = "1"

from jarvis import notifier, scheduler, tool_router, tool_safety, tools  # noqa: E402
from jarvis.actions import power_tools as pt  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _live(fn):
    """Run fn with dry-run OFF and a recording fake _run; returns (result, argvs)."""
    calls, saved_run, saved_env = [], pt._run, os.environ.pop("JARVIS_POWER_DRY_RUN", None)
    saved_sys = pt._system

    def fake(argv):
        calls.append(argv)
        return fake.proc

    fake.proc = _Proc()
    pt._run, pt._system = fake, (lambda: "Windows")
    try:
        return fn(fake), calls
    finally:
        pt._run, pt._system = saved_run, saved_sys
        if saved_env is not None:
            os.environ["JARVIS_POWER_DRY_RUN"] = saved_env
        else:
            os.environ["JARVIS_POWER_DRY_RUN"] = "1"


def test_discovered_and_gated():
    check("power_action is a loaded tool", "power_action" in tools.TOOLS)
    check("it is in the system_control group", "power_action" in
          [s["name"] for s in tools.TOOL_SCHEMAS] and pt.TOOL_GROUP == "system_control")
    check("confirm is on by default (the interactive gate)",
          "power_action" in tool_safety.DEFAULT_CONFIRM_REQUIRED)
    check("the schema has no model-fillable 'confirm' argument",
          "confirm" not in pt.TOOL_SCHEMAS[0]["parameters"]["properties"])
    check("action is required and enumerated",
          pt.TOOL_SCHEMAS[0]["parameters"]["required"] == ["action"]
          and set(pt.TOOL_SCHEMAS[0]["parameters"]["properties"]["action"]["enum"])
          == set(pt.ACTIONS))


def test_routing():
    for phrase in ("shut down the pc", "turn off the computer", "shutdown the PC",
                   "restart the computer", "lock the screen", "put it to sleep"):
        r = tool_router.route(phrase)
        names = {m[1] for m in r.matches}
        check("routes: %r -> power_action" % phrase, "power_action" in names, names)


def test_windows_commands():
    b = lambda a, d=0, f=False: pt.build_command(a, d, f, "Windows")  # noqa: E731
    check("shutdown", b("shutdown", 15) == ["shutdown", "/s", "/t", "15"])
    check("restart now + force", b("restart", 0, True) == ["shutdown", "/r", "/t", "0", "/f"])
    check("cancel", b("cancel") == ["shutdown", "/a"])
    check("lock", b("lock") == ["rundll32.exe", "user32.dll,LockWorkStation"])
    sleep = b("sleep")
    check("sleep uses SetSuspendState via PowerShell, not rundll32 (which hibernates)",
          sleep[0] == "powershell" and "Suspend" in sleep[-1] and "rundll32" not in " ".join(sleep))
    check("unknown action has no command", b("explode") is None)


def test_other_platforms_are_strings_only():
    check("linux shutdown now", pt.build_command("shutdown", 0, False, "Linux") == ["systemctl", "poweroff"])
    check("linux restart now", pt.build_command("restart", 0, False, "Linux") == ["systemctl", "reboot"])
    check("linux delay rounds UP to whole minutes",
          pt.build_command("shutdown", 61, False, "Linux") == ["shutdown", "-P", "+2"])
    check("linux 5s delay is 1 minute, never 0",
          pt.build_command("restart", 5, False, "Linux") == ["shutdown", "-r", "+1"])
    check("linux lock", pt.build_command("lock", 0, False, "Linux") == ["loginctl", "lock-session"])
    check("mac sleep", pt.build_command("sleep", 0, False, "Darwin") == ["pmset", "sleepnow"])
    check("mac shutdown now", pt.build_command("shutdown", 0, False, "Darwin")[0] == "osascript")


def test_handler_defaults_and_clamping():
    def run(fake):
        return pt.tool_power_action({"action": "shutdown"})
    res, calls = _live(run)
    check("shutdown defaults to a 15s grace period",
          calls == [["shutdown", "/s", "/t", "15"]] and res["delay_seconds"] == 15 and res["ok"], (res, calls))
    check("the result tells the model how to cancel", "cancel" in res["note"])

    res, calls = _live(lambda f: pt.tool_power_action({"action": "restart", "delay_seconds": 0}))
    check("delay 0 is honoured, not replaced by the default", calls[0][3] == "0", calls)

    res, calls = _live(lambda f: pt.tool_power_action({"action": "shutdown", "delay_seconds": 999999}))
    check("delay is capped at an hour", calls[0][3] == str(pt.MAX_DELAY_SECONDS), calls)

    res, calls = _live(lambda f: pt.tool_power_action({"action": "shutdown", "delay_seconds": -5}))
    check("negative delay is rejected and runs nothing", not res["ok"] and calls == [], (res, calls))

    res, calls = _live(lambda f: pt.tool_power_action({"action": "shutdown", "delay_seconds": True}))
    check("a boolean is not a delay", not res["ok"] and calls == [], (res, calls))

    res, calls = _live(lambda f: pt.tool_power_action({"action": "lock", "delay_seconds": 30, "force": True}))
    check("lock ignores delay and force (and says delay 0)",
          calls == [["rundll32.exe", "user32.dll,LockWorkStation"]] and res["delay_seconds"] == 0, (res, calls))

    res, calls = _live(lambda f: pt.tool_power_action({"action": "shutdown", "force": True}))
    check("force is opt-in and reaches the command", calls[0][-1] == "/f" and res["force"] is True, calls)
    res, calls = _live(lambda f: pt.tool_power_action({"action": "shutdown"}))
    check("force is off by default", "/f" not in calls[0] and res["force"] is False, calls)


def test_aliases_and_bad_input():
    res, calls = _live(lambda f: pt.tool_power_action({"action": "Reboot"}))
    check("'Reboot' means restart", calls and calls[0][1] == "/r", calls)
    res, calls = _live(lambda f: pt.tool_power_action({"action": "power off"}))
    check("'power off' means shutdown", calls and calls[0][1] == "/s", calls)
    for bad in ({}, {"action": ""}, {"action": "hibernate"}, None):
        res, calls = _live(lambda f, b=bad: pt.tool_power_action(b))
        check("bad input %r runs nothing" % (bad,), not res["ok"] and calls == [], (res, calls))


def test_failures_are_reported_not_raised():
    def nonzero(fake):
        fake.proc = _Proc(5, "", "Access is denied.")
        return pt.tool_power_action({"action": "shutdown"})
    res, _ = _live(nonzero)
    check("non-zero exit -> ok false with the OS message",
          not res["ok"] and "Access is denied" in res["error"], res)

    def missing(fake):
        def boom(argv):
            raise FileNotFoundError()
        pt._run = boom
        return pt.tool_power_action({"action": "lock"})
    res, _ = _live(missing)
    check("missing binary -> ok false", not res["ok"] and "not found" in res["error"], res)

    def slow(fake):
        def boom(argv):
            raise subprocess.TimeoutExpired(argv, 1)
        pt._run = boom
        return pt.tool_power_action({"action": "sleep"})
    res, _ = _live(slow)
    check("timeout -> ok false, no exception", not res["ok"] and "didn't return" in res["error"], res)

    def nothing_pending(fake):
        fake.proc = _Proc(1116, "", "Unable to abort the system shutdown")
        return pt.tool_power_action({"action": "cancel"})
    res, _ = _live(nothing_pending)
    check("cancel with nothing pending (1116) is a calm ok, cancelled false",
          res["ok"] and res["cancelled"] is False, res)

    res, _ = _live(lambda f: pt.tool_power_action({"action": "cancel"}))
    check("a real cancel says cancelled true", res["ok"] and res["cancelled"] is True, res)


def test_dry_run_runs_nothing():
    os.environ["JARVIS_POWER_DRY_RUN"] = "1"
    saved = pt._run

    def must_not_run(argv):
        raise AssertionError("dry run executed %r" % (argv,))
    pt._run = must_not_run
    try:
        res = pt.tool_power_action({"action": "shutdown"})
    finally:
        pt._run = saved
    check("dry run returns the command and runs nothing",
          res["ok"] and res["dry_run"] and isinstance(res["command"], list)
          and res["command"][0] in ("shutdown", "systemctl", "osascript"), res)


def test_scheduler_gate_matches_owner_decision():
    """Q-L16b: confirm when the owner asks; a scheduled task needs no confirm
    AFTER the one-time approval at creation."""
    tmp = Path(tempfile.mkdtemp(prefix="jarvis_power_test_"))
    scheduler.JARVIS_DIR = tmp
    scheduler.STORE_FILE = tmp / "scheduled.json"
    scheduler.LOCK_FILE = tmp / "scheduled.lock"
    scheduler.ASK_LOG_FILE = tmp / "scheduler_ask_log.jsonl"
    notifier.JARVIS_DIR = tmp
    notifier.INBOX_FILE = tmp / "notifications.json"
    notifier.CONFIG_FILE = tmp / "notify_config.json"
    try:
        job = scheduler.create(kind="task", when="in 1 hour",
                               action={"type": "tool", "tool": "power_action",
                                       "args": {"action": "shutdown"}})
        check("a model-created shutdown job is parked for approval",
              job["status"] == scheduler.STATUS_NEEDS_APPROVAL, job["status"])
        scheduler.approve(job["id"])
        check("approving it makes it pending (and no further prompt exists)",
              scheduler.get(job["id"])["status"] == scheduler.STATUS_PENDING)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_scheduled_run_goes_straight_to_the_tool():
    """_do_tool -> execute_tool with no prompt. Dry run, so nothing happens."""
    res = tools.execute_tool("power_action", {"action": "shutdown", "delay_seconds": 30})
    check("execute_tool path works end to end (dry run)",
          res.get("ok") and res.get("dry_run") and res.get("delay_seconds") == 30, res)


for fn in [
    test_discovered_and_gated, test_routing, test_windows_commands,
    test_other_platforms_are_strings_only, test_handler_defaults_and_clamping,
    test_aliases_and_bad_input, test_failures_are_reported_not_raised,
    test_dry_run_runs_nothing, test_scheduler_gate_matches_owner_decision,
    test_scheduled_run_goes_straight_to_the_tool,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
