"""H.2 -- scheduler: editing an existing job, and keeping finished jobs as
history in their own category.

  * update_job(): changes only the fields given, validates all of them before
    changing any, refuses unknown fields / fields the action type doesn't have /
    finished (done, cancelled) jobs, and can't be used to dodge approval.
  * finished jobs: done / cancelled / error / over_budget stay in the store, get
    a finished_at, show up in list_finished() and overview()["finished"], and
    are pruned oldest-first past MAX_FINISHED_JOBS -- never the active ones.
  * the `sched-edit` argument parser (JSON, or --field value pairs).

Run: python3 tests/test_h2_sched_edit_history.py
Uses a throwaway HOME; touches no real ~/.jarvis.
"""
import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-h2-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

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


def raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except scheduler.SchedulerError as e:
        return str(e)
    return None


def reminder(text="drink water", when="in 10 minutes", **kw):
    return scheduler.create(kind="reminder", message=text, when=when, trusted=True, **kw)


def finish(job_id, status="done"):
    scheduler._mutate(job_id, lambda j: j.update(status=status, last_run="2026-10-01T09:00:00"))


# --- finished history ---------------------------------------------------------
fresh()
a, b, c, d = reminder("one"), reminder("two"), reminder("three"), reminder("four")
finish(a["id"], "done")
scheduler.cancel(b["id"])
finish(c["id"], "error")

check("FINISHED_STATUSES is the complement of ACTIVE_STATUSES",
      set(scheduler.FINISHED_STATUSES) | set(scheduler.ACTIVE_STATUSES) == {
          scheduler.STATUS_PENDING, scheduler.STATUS_PAUSED, scheduler.STATUS_NEEDS_APPROVAL,
          scheduler.STATUS_DONE, scheduler.STATUS_CANCELLED, scheduler.STATUS_ERROR, scheduler.STATUS_OVER_BUDGET}
      and not set(scheduler.FINISHED_STATUSES) & set(scheduler.ACTIVE_STATUSES))
check("list_jobs() still means 'still scheduled' (only the fourth)",
      [j["id"] for j in scheduler.list_jobs()] == [d["id"]])
fin = scheduler.list_finished()
check("list_finished() has the three finished jobs", {j["id"] for j in fin} == {a["id"], b["id"], c["id"]}, fin)
check("every finished job got a finished_at", all(j.get("finished_at") for j in fin), fin)
check("a finished_at is not set on a live job", not scheduler.get(d["id"]).get("finished_at"))
check("list_finished(status=) narrows",
      [j["id"] for j in scheduler.list_finished(status="error")] == [c["id"]])

ov = scheduler.overview()
check("overview() keeps `jobs` active-only", [j["id"] for j in ov["jobs"]] == [d["id"]], ov["jobs"])
check("overview() carries the finished list", {j["id"] for j in ov["finished"]} == {a["id"], b["id"], c["id"]})
check("overview() counts finished / done / failed / cancelled",
      (ov["counts"]["finished"], ov["counts"]["done"], ov["counts"]["failed"], ov["counts"]["cancelled"]) == (3, 1, 1, 1),
      ov["counts"])
row = [j for j in ov["finished"] if j["id"] == a["id"]][0]
check("a finished summary row says when it finished", bool(row.get("finished_at")), row)
check("a live summary row carries no finished_at / last_result",
      ov["jobs"][0].get("finished_at") is None and ov["jobs"][0].get("last_result") is None)
scheduler._mutate(a["id"], lambda j: j.update(last_result="x" * 400))
check("a finished row's last_result is trimmed",
      len(scheduler.summarize(scheduler.get(a["id"]))["last_result"]) <= scheduler.SUMMARY_RESULT_CHARS + 1)

resumed = scheduler.resume(c["id"])
check("resuming a failed job takes it back out of the finished list",
      c["id"] not in [j["id"] for j in scheduler.list_finished()] and not resumed.get("finished_at"), resumed)

# --- retention ----------------------------------------------------------------
fresh()
old_cap = scheduler.MAX_FINISHED_JOBS
scheduler.MAX_FINISHED_JOBS = 3
try:
    live = reminder("stays")
    ids = []
    for n in range(5):
        j = reminder("old %d" % n)
        scheduler._mutate(j["id"], lambda x, n=n: x.update(status="done", finished_at="2026-09-0%dT10:00:00" % (n + 1)))
        ids.append(j["id"])
    reminder("trigger a prune")  # create() prunes
    left = {j["id"] for j in scheduler.list_finished()}
    check("history is capped at MAX_FINISHED_JOBS", len(left) == 3, left)
    check("the OLDEST finished jobs fell off", left == set(ids[2:]), left)
    check("an active job is never pruned", scheduler.get(live["id"]) is not None)
finally:
    scheduler.MAX_FINISHED_JOBS = old_cap

# --- update_job: basics --------------------------------------------------------
fresh()
j = reminder("drink water")
check("title follows an edited message when it was just the old message",
      scheduler.update_job(j["id"], {"message": "stretch"}, trusted=True)["title"] == "stretch")
j = scheduler.update_job(j["id"], {"title": "Hydrate", "level": 3, "max_runs": 2, "catch_up": True,
                                   "channels": ["Inbox", "inbox"], "emit_on_done": "water-done"}, trusted=True)
check("title/level/max_runs/catch_up/channels/emit_on_done are set",
      (j["title"], j["level"], j["max_runs"], j["catch_up"], j["channels"]) == ("Hydrate", 3, 2, True, ["inbox"]), j)
check("a chosen title is NOT overwritten by a later message edit",
      scheduler.update_job(j["id"], {"message": "walk"}, trusted=True)["title"] == "Hydrate")
before_next = scheduler.get(j["id"])["next_run"]
j = scheduler.update_job(j["id"], {"when": "in 3 hours"}, trusted=True)
check("`when` replaces the trigger and moves next_run", j["next_run"] != before_next and j["status"] == "pending", j)
j = scheduler.update_job(j["id"], {"level": None, "max_runs": None, "channels": None}, trusted=True)
check("None clears level / max_runs / channels", (j["level"], j["max_runs"], j["channels"]) == (None, None, None), j)
check("the job keeps its id and creation time", j["id"] and j.get("created_at"))
check("an edit stamps updated_at", bool(j.get("updated_at")))

# --- update_job: refusals, all-or-nothing -----------------------------------------
fresh()
j = reminder("original", when="in 10 minutes")
snapshot = scheduler.get(j["id"])
check("no changes is an error", raises(scheduler.update_job, j["id"], {}) is not None)
check("an unknown field is named in the error", "colour" in (raises(scheduler.update_job, j["id"], {"colour": "red"}) or ""))
check("action_type can't be edited (a different job, not an edit)",
      raises(scheduler.update_job, j["id"], {"action_type": "command"}) is not None)
check("a prompt can't be set on a notification", raises(scheduler.update_job, j["id"], {"prompt": "x"}) is not None)
check("an empty message is refused", raises(scheduler.update_job, j["id"], {"message": "  "}) is not None)
check("level out of range is refused", raises(scheduler.update_job, j["id"], {"level": 9}) is not None)
check("a bad max_runs is refused", raises(scheduler.update_job, j["id"], {"max_runs": "lots"}) is not None)
check("an unknown kind is refused", raises(scheduler.update_job, j["id"], {"kind": "chore"}) is not None)
check("an unparseable `when` is refused", raises(scheduler.update_job, j["id"], {"when": "whenever, man"}) is not None)
check("a missing job is a SchedulerError", raises(scheduler.update_job, "nope0000", {"title": "x"}) is not None)
err = raises(scheduler.update_job, j["id"], {"title": "changed", "level": 99}, trusted=True)
check("one bad field leaves EVERY field unchanged (all-or-nothing)",
      err is not None and scheduler.get(j["id"]) == snapshot, scheduler.get(j["id"]))
finish(j["id"], "done")
check("a done job is history and can't be edited", "finished history" in (raises(scheduler.update_job, j["id"], {"title": "x"}) or ""))
k = reminder("cancel me")
scheduler.cancel(k["id"])
check("a cancelled job can't be edited either", raises(scheduler.update_job, k["id"], {"title": "x"}) is not None)

# --- update_job: failed job is editable but NOT resumed ------------------------------
fresh()
j = reminder("flaky")
finish(j["id"], "error")
e = scheduler.update_job(j["id"], {"message": "flaky v2"}, trusted=True)
check("editing a failed job doesn't resume it", e["status"] == "error" and e.get("finished_at"), e)

# --- update_job: approval can't be dodged ----------------------------------------
fresh()
ask = scheduler.create(kind="task", title="check mail", when="in 1 hour", trusted=True,
                       action={"type": "ask", "prompt": "check my mail"})
approved = scheduler._mutate(ask["id"], lambda x: x.update(status="pending", approved_at="2026-10-01T09:00:00",
                                                           approved_kinds=["reads files"]))
orig_assess = scheduler.job_risk.assess
try:
    scheduler.job_risk.assess = lambda action: {"kinds": ["reads files"], "level": "low"}
    t = scheduler.update_job(ask["id"], {"prompt": "check my mail and archive it"}, trusted=True)
    check("a trusted edit within the approved kinds keeps the job runnable", t["status"] == "pending", t)
    check("the action text actually changed", t["action"]["prompt"] == "check my mail and archive it")

    scheduler.job_risk.assess = lambda action: {"kinds": ["reads files", "controls the desktop"], "level": "high"}
    t = scheduler.update_job(ask["id"], {"prompt": "check my mail, then close all my windows"}, trusted=True)
    check("a trusted edit that adds an UNAPPROVED kind goes back to needs_approval",
          t["status"] == "needs_approval" and "approved_kinds" not in t and "approved_at" not in t, t)

    scheduler._mutate(ask["id"], lambda x: x.update(status="pending", approved_at="2026-10-01T09:00:00",
                                                   approved_kinds=["reads files"]))
    scheduler.job_risk.assess = lambda action: {"kinds": ["reads files"], "level": "low"}
    t = scheduler.update_job(ask["id"], {"prompt": "something new"}, trusted=False)
    check("an UNTRUSTED edit of an ask always needs approval again, even within the kinds",
          t["status"] == "needs_approval" and "approved_kinds" not in t, t)

    t = scheduler.update_job(ask["id"], {"title": "renamed"}, trusted=False)
    check("an untrusted edit that doesn't touch the action doesn't change status",
          t["status"] == "needs_approval" and t["title"] == "renamed")
finally:
    scheduler.job_risk.assess = orig_assess

# --- argument parser for `jarvis sched-edit` ---------------------------------------
from jarvis import cli  # noqa: E402

p = cli.parse_sched_edit_args
check("one JSON object is accepted", p(['{"title": "x", "level": 2}']) == ({"title": "x", "level": 2}, None))
check("JSON split across argv tokens is rejoined", p(['{"title":', '"a b"}'])[0] == {"title": "a b"})
check("a JSON array is refused", p(["[1]"])[0] is None)
check("broken JSON is refused with a reason", p(["{nope"])[1] is not None)
changes, err = p(["--title", "New name", "--when", "tomorrow at 9am", "--level", "none",
                  "--max-runs", "3", "--channels", "inbox, discord", "--no-catch-up", "--report"])
check("--field value pairs parse", err is None and changes == {
    "title": "New name", "when": "tomorrow at 9am", "level": None, "max_runs": "3",
    "channels": ["inbox", "discord"], "catch_up": False, "report": True}, (changes, err))
check("--args takes a JSON object", p(["--args", '{"k": "v"}'])[0] == {"args": {"k": "v"}})
check("a flag with no value is refused", p(["--title"])[1] is not None)
check("a bare word is refused", p(["title"])[1] is not None)
check("nothing at all is refused", p([])[0] is None)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
