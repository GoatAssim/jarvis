"""Schedules: delete ONE finished job, and "Clear finished" (leftovers from the H.2 history patch).

  1. scheduler.delete_job(): removes exactly one FINISHED job (done, cancelled, error, over
     budget), refuses a job that can still fire (and says to cancel it first), an unknown id and
     a bad id, leaves every other job alone, and raises when the save fails (nothing removed).
  2. scheduler.clear_finished(): removes every finished job, keeps every active one, and now
     raises instead of reporting a removal that did not happen when the save fails.
  3. CLI: `jarvis sched-delete <id>` (ok / refused / bad id) and `jarvis sched-clear` still work.
  4. Static: the server route and action list, the reserved name, the /schedule verb's `covers`,
     the panel's calls, and -- the invariant -- no model-facing module reaches either function.

No network, no live model. Throwaway HOME. Written, not run, by the author: the owner runs it.

Run: python3 tests/test_sched_delete_clear.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-sched-delete-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS"):
    os.environ.pop(_var, None)
ROOT = Path(__file__).resolve().parent.parent
CLI_DIR = ROOT / "jarvis-cli"
sys.path.insert(0, str(CLI_DIR))

from jarvis import scheduler  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def fresh():
    try:
        scheduler.STORE_FILE.unlink()
    except (OSError, AttributeError):
        pass


def reminder(text="drink water", when="in 10 minutes"):
    return scheduler.create(kind="reminder", message=text, when=when, trusted=True)


def finish(job_id, status="done"):
    scheduler._mutate(job_id, lambda j: j.update(status=status, last_run="2026-10-01T09:00:00"))


def raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except scheduler.SchedulerError as e:
        return str(e)
    return None


def ids():
    return [j["id"] for j in scheduler.load_store()["jobs"]]


def cli(*args):
    p = subprocess.run([sys.executable, "-m", "jarvis", *args], cwd=str(CLI_DIR), env=dict(os.environ),
                       capture_output=True, text=True, timeout=120)
    try:
        out = json.loads(p.stdout)
    except ValueError:
        out = {}
    return p.returncode, out


def src(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


# --- 1. delete_job ----------------------------------------------------------------------------

def test_delete_job():
    fresh()
    jobs = {s: reminder(s) for s in ("done", "cancelled", "error", "over_budget", "live", "paused")}
    for s in ("done", "cancelled", "error", "over_budget"):
        finish(jobs[s]["id"], s)
    scheduler.pause(jobs["paused"]["id"])
    for s in ("done", "cancelled", "error", "over_budget"):
        before = ids()
        removed = scheduler.delete_job(jobs[s]["id"])
        check(f"a {s} job can be deleted, and the job is returned",
              removed["id"] == jobs[s]["id"] and jobs[s]["id"] not in ids() and len(ids()) == len(before) - 1)
    check("the active jobs are all still there", set(ids()) == {jobs["live"]["id"], jobs["paused"]["id"]})
    for s in ("live", "paused"):
        msg = raises(scheduler.delete_job, jobs[s]["id"])
        check(f"a {s} job is refused, with a pointer to cancel", msg and "cancel" in msg, msg)
        check(f"...and is still stored ({s})", jobs[s]["id"] in ids())
    pending_approval = scheduler.create(kind="task", title="t", when="in 1 hour",
                                        action={"type": "ask", "prompt": "say hi"}, trusted=False)
    check("a job waiting on approval is refused too", pending_approval["status"] == scheduler.STATUS_NEEDS_APPROVAL
          and raises(scheduler.delete_job, pending_approval["id"]) and pending_approval["id"] in ids())
    check("an unknown id is refused", "no scheduled job" in (raises(scheduler.delete_job, "deadbeefdeadbeef") or ""))
    check("an empty / None id is refused, never raises anything else",
          raises(scheduler.delete_job, "") is not None and raises(scheduler.delete_job, None) is not None)
    # the id is matched case-insensitively and trimmed, like every other per-job verb
    j = reminder("case")
    finish(j["id"])
    check("the id is matched trimmed and case-insensitively", scheduler.delete_job("  %s " % j["id"].upper())["id"] == j["id"])
    # a failed save removes nothing and says so
    j = reminder("save fails")
    finish(j["id"])
    real = scheduler.save_store
    scheduler.save_store = lambda store: False
    try:
        msg = raises(scheduler.delete_job, j["id"])
    finally:
        scheduler.save_store = real
    check("a failed save raises", msg and "nothing was removed" in msg, msg)
    check("...and the job is still in the file", j["id"] in ids())


# --- 2. clear_finished ------------------------------------------------------------------------

def test_clear_finished():
    fresh()
    live = [reminder("live one"), reminder("live two")]
    scheduler.pause(live[1]["id"])
    gone = [reminder("a"), reminder("b"), reminder("c"), reminder("d")]
    for job, s in zip(gone, ("done", "cancelled", "error", "over_budget")):
        finish(job["id"], s)
    n = scheduler.clear_finished()
    check("clear_finished returns how many it removed", n == 4, n)
    check("every finished job is gone and every active one kept", set(ids()) == {j["id"] for j in live})
    check("clearing again removes nothing", scheduler.clear_finished() == 0)
    j = reminder("x")
    finish(j["id"])
    real = scheduler.save_store
    scheduler.save_store = lambda store: False
    try:
        msg = raises(scheduler.clear_finished)
    finally:
        scheduler.save_store = real
    check("a failed save raises instead of reporting a removal", msg and "nothing was removed" in msg, msg)
    check("...and the finished job is still stored", j["id"] in ids())


# --- 3. CLI -----------------------------------------------------------------------------------

def test_cli():
    fresh()
    live, old = reminder("live"), reminder("old")
    finish(old["id"], "error")
    code, out = cli("sched-delete", live["id"])
    check("CLI: an active job is refused (exit 1, error says cancel)", code == 1 and "cancel" in out.get("error", ""), (code, out))
    check("CLI: ...and nothing was removed", live["id"] in ids() and old["id"] in ids())
    code, out = cli("sched-delete", "not-an-id")
    check("CLI: a malformed id gets the usage line", code == 1 and "usage" in out.get("error", ""), (code, out))
    code, out = cli("sched-delete")
    check("CLI: no id gets the usage line", code == 1 and "usage" in out.get("error", ""), (code, out))
    code, out = cli("sched-delete", old["id"])
    check("CLI: a finished job is removed", code == 0 and out.get("removed") == 1 and out.get("id") == old["id"], (code, out))
    check("CLI: ...really", old["id"] not in ids() and live["id"] in ids())
    finish(live["id"])
    code, out = cli("sched-clear")
    check("CLI: sched-clear still removes the finished ones", code == 0 and out.get("removed") == 1 and ids() == [], (code, out))


# --- 4. static wiring + the human-only invariant ----------------------------------------------

def test_wiring():
    server = src("web/server.js")
    m = re.search(r"const SCHED_ACTIONS = new Set\(\[([^\]]*)\]\)", server)
    check("server: 'delete' is an allowed per-job action", m and '"delete"' in m.group(1))
    check("server: the action list is still a fixed set", m and all(a in m.group(1) for a in ("cancel", "pause", "resume", "approve", "snooze", "budget", "edit")))
    route = re.search(r'app\.post\("/api/scheduled/clear",[^\n]*\n([^\n]*\n){0,3}', server)
    check("server: POST /api/scheduled/clear exists and runs sched-clear with no body-supplied argv",
          route and 'runJarvisOnce(["sched-clear"], 15000)' in route.group(0), route and route.group(0))
    check("server: the clear route is registered before /tick, neither can shadow the other",
          server.index('"/api/scheduled/clear"') < server.index('"/api/scheduled/tick"'))
    check("reserved_names has sched-delete", '"sched-delete"' in src("jarvis-cli/jarvis/reserved_names.py"))
    check("cli SCHEDULER_COMMANDS has sched-delete", '"sched-delete"' in src("jarvis-cli/jarvis/cli.py"))
    slash = src("web/public/slash-commands-data.js")
    check("slash registry: the /schedule verb covers sched-delete (once)", slash.count('"sched-delete"') == 1)
    panel = src("web/public/schedules.js")
    check("panel: Delete posts to the per-job delete route", "/delete`" in panel and "encodeURIComponent(job.id)" in panel)
    check("panel: Clear finished posts to /api/scheduled/clear", '"/api/scheduled/clear"' in panel)
    check("panel: both go through a confirm", panel.count("confirmText(") >= 2)
    check("panel: Delete is offered for finished jobs only", "if (isFinished(job)) buttons.push(deleteButton(job))" in panel)
    # the invariant: only the CLI reaches these (a model that could delete jobs could erase what ran unattended)
    hits = []
    for path in sorted((CLI_DIR / "jarvis").rglob("*.py")):
        # scheduler.py / cli.py are the two owners; doctor.py only NAMES the command in a hint;
        # tasks.py has its own, unrelated clear_finished() for the Tasks panel; reserved_names.py only
        # lists "sched-delete" / "sched-clear" as taken CLI words (this test asserts that just above).
        if path.name in ("scheduler.py", "cli.py", "doctor.py", "tasks.py", "reserved_names.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"\bdelete_job\b|\bclear_finished\b|sched-delete|sched-clear", text):
            hits.append(path.name)
    check("no other module (so no model tool) reaches delete_job / clear_finished", not hits, hits)
    doctor = src("jarvis-cli/jarvis/doctor.py")
    check("doctor's hint for a failed job points at sched-delete <id>, not sched-clear <id> (which takes no id)",
          "sched-delete <id>" in doctor and "sched-clear <id>" not in doctor)


test_delete_job()
test_clear_finished()
test_cli()
test_wiring()
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
