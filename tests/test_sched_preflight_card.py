"""The L.16 capability preflight on the APPROVAL card (leftover from the preflight patch).

The preflight used to fire only when a scheduled ask ran. `scheduler.preflight_of(job)` now asks
the same question about the same text at approval time, and `jarvis sched-show` /
`jarvis sched-approve` / the Schedules panel say so.

  1. scheduler.preflight_of: None for jobs the preflight does not check (notify / command / tool,
     empty prompt); ok / blockers / lines for an ask; sentences capitalised and ended; fails open
     (an error is None, never a made-up warning); never changes the job; it is advice, so a
     blocker never changes what approve() does.
  2. CLI `sched-show`: carries `preflight` only for an ask job that is waiting on approval.
     `sched-approve`: still approves, and warns on stderr when the check says it would not start.
  3. summarize()/overview() do NOT carry it (they run per row on every refresh and feed the
     model's list_scheduled).
  4. Static: the panel shows the block and asks before approving a job it knows will not start.

No network, no live model, no Tesseract needed (the check is stubbed where a blocker is wanted).
Written, not run, by the author: the owner runs it.

Run: python3 tests/test_sched_preflight_card.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-pfcard-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
for _var in ("JARVIS_SCHEDULED", "JARVIS_CONTEXT", "JARVIS_ALLOWED_TOOLS"):
    os.environ.pop(_var, None)
ROOT = Path(__file__).resolve().parent.parent
CLI_DIR = ROOT / "jarvis-cli"
sys.path.insert(0, str(CLI_DIR))

from jarvis import preflight, scheduler  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


def fresh():
    try:
        scheduler.STORE_FILE.unlink()
    except (OSError, AttributeError):
        pass


def ask_job(prompt="say hello", trusted=False):
    return scheduler.create(kind="task", title="t", when="in 1 hour",
                            action={"type": "ask", "prompt": prompt}, trusted=trusted)


BLOCKED = {"ok": False, "needs": ["see_screen"],
           "blockers": [{"need": "see_screen", "why": "it has to read the screen, but there is no OCR and no vision"}]}


class Stub:
    """Swap preflight.check for the duration of a block."""
    def __init__(self, result=None, exc=None):
        self.result, self.exc = result, exc

    def __enter__(self):
        self.real = preflight.check

        def fake(prompt):
            if self.exc:
                raise self.exc
            return self.result
        preflight.check = fake

    def __exit__(self, *a):
        preflight.check = self.real


def test_preflight_of():
    fresh()
    job = ask_job("check the screen then type continue")
    with Stub(BLOCKED):
        pf = scheduler.preflight_of(job)
    check("a blocked ask is not ok, with the blockers and the needs", pf and pf["ok"] is False and pf["needs"] == ["see_screen"]
          and len(pf["blockers"]) == 1, pf)
    check("the sentence starts with a capital and ends with a full stop",
          pf["lines"] == ["It has to read the screen, but there is no OCR and no vision."], pf["lines"])
    with Stub({"ok": True, "needs": [], "blockers": []}):
        pf = scheduler.preflight_of(job)
    check("an ask with nothing missing is ok with no lines", pf == {"ok": True, "needs": [], "blockers": [], "lines": []}, pf)
    with Stub({"ok": False, "needs": ["power"], "blockers": [{"need": "power", "why": "it has to shut down. Already ends."}]}):
        check("a sentence that already ends with a full stop is not given a second",
              scheduler.preflight_of(job)["lines"] == ["It has to shut down. Already ends."])
    with Stub(None, exc=RuntimeError("boom")):
        check("an error fails OPEN: None, not a warning", scheduler.preflight_of(job) is None)
    with Stub({"ok": False, "blockers": [{"need": "x"}, "junk", {"need": "y", "why": "   "}]}):
        pf = scheduler.preflight_of(job)
        check("blockers with no usable text produce no lines and junk entries are dropped",
              pf["lines"] == [] and all(isinstance(b, dict) for b in pf["blockers"]), pf)
    called = []
    real = preflight.check
    preflight.check = lambda p: called.append(p) or BLOCKED
    try:
        notify = scheduler.create(kind="reminder", message="hi", when="in 1 hour", trusted=True)
        tool = scheduler.create(kind="task", title="t", when="in 1 hour", action={"type": "tool", "tool": "get_battery"}, trusted=True)
        empty = {"action": {"type": "ask", "prompt": "   "}}
        results = (scheduler.preflight_of(notify), scheduler.preflight_of(tool), scheduler.preflight_of(empty),
                   scheduler.preflight_of(None), scheduler.preflight_of({}))
        spy = list(called)
        scheduler.preflight_of(job)
    finally:
        preflight.check = real
    check("notify / tool / empty-prompt / missing jobs are not checked at all", results == (None,) * 5 and spy == [], (results, spy))
    check("the check is asked about the job's prompt, exactly as the run passes it", called == ["check the screen then type continue"], called)
    before = json.dumps(job, sort_keys=True)
    with Stub(BLOCKED):
        scheduler.preflight_of(job)
    check("it does not change the job", json.dumps(job, sort_keys=True) == before)
    # advice, never a gate
    j = ask_job("check the screen")
    with Stub(BLOCKED):
        scheduler.approve(j["id"])
    check("a blocker does not stop approve()", scheduler.get(j["id"])["status"] == scheduler.STATUS_PENDING)
    check("summarize() does not carry it (it runs per row, and feeds the model's list_scheduled)",
          "preflight" not in scheduler.summarize(scheduler.get(j["id"])))
    check("overview() rows do not carry it either", all("preflight" not in r for r in scheduler.overview()["jobs"]))


WRAPPER = r"""
import json, sys
sys.path.insert(0, %(cli)r)
from jarvis import preflight
preflight.check = lambda prompt: %(result)r
sys.argv = ["jarvis"] + json.loads(%(args)r)
from jarvis import cli
sys.exit(cli.main())
"""


def cli(*args, blocked=None):
    if blocked is None:
        p = subprocess.run([sys.executable, "-m", "jarvis", *args], cwd=str(CLI_DIR), env=dict(os.environ),
                           capture_output=True, text=True, timeout=120)
    else:
        code = WRAPPER % {"cli": str(CLI_DIR), "result": blocked, "args": json.dumps(list(args))}
        p = subprocess.run([sys.executable, "-c", code], cwd=str(CLI_DIR), env=dict(os.environ),
                           capture_output=True, text=True, timeout=120)
    try:
        out = json.loads(p.stdout)
    except ValueError:
        out = {}
    return p.returncode, out, p.stderr


def test_cli():
    fresh()
    waiting = ask_job("check the screen")
    pending = ask_job("say hi", trusted=True)
    tool_wait = scheduler.create(kind="task", title="tw", when="in 1 hour",
                                 action={"type": "tool", "tool": "run_shell", "args": {"command": "echo"}}, trusted=False)
    code, out, err = cli("sched-show", waiting["id"], blocked=BLOCKED)
    check("sched-show: a waiting ask carries `preflight`", code == 0 and out.get("preflight", {}).get("ok") is False
          and out["preflight"]["lines"], (code, out, err[-200:]))
    check("sched-show: the usual fields are all still there", out.get("risk") is not None and out.get("id") == waiting["id"]
          and out.get("status") == "needs_approval")
    code, out, err = cli("sched-show", pending["id"], blocked=BLOCKED)
    check("sched-show: a job that is not waiting on approval has no `preflight` key", code == 0 and "preflight" not in out, out)
    code, out, err = cli("sched-show", tool_wait["id"], blocked=BLOCKED)
    check("sched-show: a waiting TOOL job has none (the preflight only checks asks)", code == 0 and "preflight" not in out, out)
    code, out, err = cli("sched-show", waiting["id"])
    check("sched-show: with the real check it still answers (ok or not, never an error)", code == 0 and "error" not in out, (code, out, err[-200:]))
    code, out, err = cli("sched-approve", waiting["id"], blocked=BLOCKED)
    check("sched-approve: still approves", code == 0 and out.get("status") == "pending", (code, out, err[-300:]))
    check("sched-approve: warns on stderr that it would not start", "would not start right now" in err and "read the screen" in err, err)
    other = ask_job("check the screen again")
    code, out, err = cli("sched-approve", other["id"], blocked={"ok": True, "needs": [], "blockers": []})
    check("sched-approve: says nothing extra when the check is fine", code == 0 and "would not start" not in err, err)
    check("sched-approve: stdout stays one JSON object", out.get("id") == other["id"])


def test_static():
    panel = (ROOT / "web" / "public" / "schedules.js").read_text(encoding="utf-8")
    check("panel: the detail pane draws a preflight block from job.preflight", "sch-detail__preflight" in panel and "preflightLines(job)" in panel)
    check("panel: approving a job that will not start asks first, even with nothing risky in it",
          "if (!lines.length && !warn.length) return true;" in panel)
    check("panel: only a job still waiting on approval shows it", 'job.status !== "needs_approval"' in panel)
    check("panel: the warning says approving is still allowed", "You can still approve it" in panel)
    css = (ROOT / "web" / "public" / "schedules.css").read_text(encoding="utf-8")
    check("css: the block has a style", ".sch-detail__preflight" in css)
    tools = (CLI_DIR / "jarvis" / "actions" / "scheduler_tools.py").read_text(encoding="utf-8")
    check("the model-facing schedule_task result is NOT changed by this (no tool / checklist change)", "preflight" not in tools)


test_preflight_of()
test_cli()
test_static()
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
