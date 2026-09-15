"""Tests for jarvis/scheduler.py + jarvis/notifier.py — the shared engine
behind scheduled tasks, notifications and reminders.

Run: python3 ../tests/test_scheduler.py   (from jarvis-cli/, like the others)

Every test repoints both modules' file paths at a fresh temp dir first, so
none of this touches the real ~/.jarvis. Nothing here spawns a subprocess:
the `ask`/`command` action types are covered by asserting they're gated at
creation, not by actually running a jarvis process.
"""

import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import notifier, scheduler, timespec  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


def fresh():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis_sched_test_"))
    scheduler.JARVIS_DIR = tmp
    scheduler.STORE_FILE = tmp / "scheduled.json"
    scheduler.LOCK_FILE = tmp / "scheduled.lock"
    notifier.JARVIS_DIR = tmp
    notifier.INBOX_FILE = tmp / "notifications.json"
    notifier.CONFIG_FILE = tmp / "notify_config.json"
    return tmp


def make_due(job_id, seconds_ago=5):
    """Backdate a job's next_run so the next tick picks it up."""
    store = scheduler.load_store()
    for job in store["jobs"]:
        if job["id"] == job_id:
            job["next_run"] = timespec.to_iso(datetime.now() - timedelta(seconds=seconds_ago))
    scheduler.save_store(store)


def notify_only(**kwargs):
    """A reminder delivered to the inbox only — keeps tests off the OS
    toast path, which would shell out to powershell/notify-send."""
    kwargs.setdefault("channels", ["inbox"])
    return scheduler.create(**kwargs)


def test_one_engine_three_kinds():
    tmp = fresh()
    try:
        reminder = notify_only(kind="reminder", when="in 1 hour", message="Call mum")
        note = notify_only(kind="notify", when="in 1 hour", message="Heads up")
        task = notify_only(kind="task", when="in 1 hour",
                           action={"type": "notify", "message": "Ran"})
        # The whole "same backend engine" claim in one assertion: three
        # different kinds, one action type, one store.
        check("all three kinds land in one store", len(scheduler.list_jobs()) == 3)
        check("a reminder is a notify-action job",
              reminder["action"]["type"] == "notify")
        check("kind is preserved separately from action",
              {reminder["kind"], note["kind"], task["kind"]} == {"reminder", "notify", "task"})
        check("a plain reminder needs no approval", reminder["status"] == scheduler.STATUS_PENDING)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_time_trigger_fires_once():
    tmp = fresh()
    try:
        job = notify_only(kind="reminder", when="in 10 minutes", message="Stand up")
        check("nothing is due yet", scheduler.due_jobs() == [])
        make_due(job["id"])
        check("backdated job is now due", len(scheduler.due_jobs()) == 1)

        result = scheduler.tick()
        check("the tick ran it", [r["id"] for r in result["ran"]] == [job["id"]])
        check("it delivered a notification", len(result["notifications"]) == 1)
        check("a one-shot job is done afterwards",
              scheduler.get(job["id"])["status"] == scheduler.STATUS_DONE)
        # The re-fire guard: a second tick must not deliver it again.
        check("a second tick does not re-fire it", scheduler.tick()["ran"] == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_recurring_reschedules():
    tmp = fresh()
    try:
        job = notify_only(kind="task", when="every 30 minutes",
                          action={"type": "notify", "message": "tick"})
        make_due(job["id"])
        scheduler.tick()
        after = scheduler.get(job["id"])
        check("a recurring job stays pending", after["status"] == scheduler.STATUS_PENDING)
        check("it counted the run", after["run_count"] == 1)
        check("its next run is in the future",
              timespec.from_iso(after["next_run"]) > datetime.now())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_missed_runs_fire_once_not_many():
    tmp = fresh()
    try:
        job = notify_only(kind="task", when="every 30 minutes",
                          action={"type": "notify", "message": "tick"})
        # Pretend the machine was off for three days: ~144 missed slots.
        make_due(job["id"], seconds_ago=3 * 86400)
        result = scheduler.tick()
        check("three days offline fires ONCE, not 144 times",
              len(result["notifications"]) == 1, str(len(result["notifications"])))
        check("and it catches up to a future slot",
              timespec.from_iso(scheduler.get(job["id"])["next_run"]) > datetime.now())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_catch_up_is_capped():
    tmp = fresh()
    try:
        job = notify_only(kind="task", when="every 30 minutes", catch_up=True,
                          action={"type": "notify", "message": "tick"})
        make_due(job["id"], seconds_ago=3 * 86400)
        result = scheduler.tick()
        check("catch_up makes up missed runs",
              len(result["notifications"]) > 1, str(len(result["notifications"])))
        check("but never more than MAX_CATCHUP_RUNS",
              len(result["notifications"]) <= scheduler.MAX_CATCHUP_RUNS,
              str(len(result["notifications"])))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_event_trigger():
    tmp = fresh()
    try:
        job = notify_only(kind="notify", when="when backup_done", message="Backup finished")
        check("an event job has no next_run", job["trigger"]["type"] == "event")
        check("an event job is not time-due", scheduler.due_jobs() == [])

        # Normalization is the join between the two halves — if 'Backup Done'
        # and 'backup_done' didn't collapse to the same key, chaining would
        # silently never fire.
        result = scheduler.signal("Backup Done!")
        check("a loosely-spelled signal still fires it", result["fired"] == [job["id"]])
        check("signalling ran the job", len(result["ran"]) == 1)
        check("an event job re-arms for next time",
              scheduler.get(job["id"])["status"] == scheduler.STATUS_PENDING)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_event_chaining():
    tmp = fresh()
    try:
        # Task A announces an event when it finishes; job B waits on it.
        first = notify_only(kind="task", when="in 10 minutes", emit_on_done="stage_one",
                            action={"type": "notify", "message": "stage one done"})
        second = notify_only(kind="notify", when="when stage_one", message="on to stage two")
        make_due(first["id"])
        result = scheduler.tick()
        ran = [r["id"] for r in result["ran"]]
        check("the first job ran", first["id"] in ran)
        check("finishing it chained into the second, in the same tick",
              second["id"] in ran, str(ran))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_startup_trigger():
    tmp = fresh()
    try:
        job = notify_only(kind="task", when="on startup",
                          action={"type": "notify", "message": "welcome back"})
        check("'on startup' parses to a startup trigger", job["trigger"]["type"] == "startup")
        check("an ordinary tick does NOT fire it", scheduler.tick()["ran"] == [])
        result = scheduler.tick(startup=True)
        check("a startup tick fires it", [r["id"] for r in result["ran"]] == [job["id"]])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_approval_gate():
    tmp = fresh()
    try:
        # A model-created job that would RUN something must not run unattended.
        gated = scheduler.create(kind="task", when="in 1 hour",
                                 action={"type": "command", "command": "backup"})
        check("a command job from the model is parked",
              gated["status"] == scheduler.STATUS_NEEDS_APPROVAL)
        make_due(gated["id"])
        check("a parked job is not due", scheduler.due_jobs() == [])
        check("and a tick will not run it", scheduler.tick()["ran"] == [])

        scheduler.approve(gated["id"])
        check("approving clears the gate",
              scheduler.get(gated["id"])["status"] == scheduler.STATUS_PENDING)

        # The human path (CLI / web panel) skips the gate outright.
        trusted = scheduler.create(kind="task", when="in 1 hour", trusted=True,
                                   action={"type": "command", "command": "backup"})
        check("a human-created job is not gated",
              trusted["status"] == scheduler.STATUS_PENDING)
        # A plain notification never needed a gate in the first place.
        plain = notify_only(kind="reminder", when="in 1 hour", message="hi")
        check("a notify-only job is never gated", plain["status"] == scheduler.STATUS_PENDING)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_lifecycle_actions():
    tmp = fresh()
    try:
        job = notify_only(kind="reminder", when="in 10 minutes", message="x")
        scheduler.pause(job["id"])
        check("pausing sets the status", scheduler.get(job["id"])["status"] == scheduler.STATUS_PAUSED)
        make_due(job["id"])
        check("a paused job never fires", scheduler.tick()["ran"] == [])

        scheduler.resume(job["id"])
        check("resuming restores pending",
              scheduler.get(job["id"])["status"] == scheduler.STATUS_PENDING)

        scheduler.snooze(job["id"], "30 minutes")
        snoozed = timespec.from_iso(scheduler.get(job["id"])["next_run"])
        check("snoozing pushes the run into the future", snoozed > datetime.now())
        check("snoozing by roughly the right amount",
              1500 < (snoozed - datetime.now()).total_seconds() < 1900)

        scheduler.cancel(job["id"])
        check("cancelling clears next_run", scheduler.get(job["id"])["next_run"] is None)
        check("a cancelled job is hidden from the active list", scheduler.list_jobs() == [])
        check("but still findable with include_finished",
              len(scheduler.list_jobs(include_finished=True)) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_tick_lock_prevents_double_fire():
    tmp = fresh()
    try:
        job = notify_only(kind="reminder", when="in 10 minutes", message="once")
        make_due(job["id"])
        # Simulate another tick already in flight by taking the lock first.
        scheduler._claim_lock()
        result = scheduler.tick()
        check("a tick skips when another holds the lock", result.get("skipped") is not None)
        check("and fires nothing", result["ran"] == [])
        scheduler._release_lock()
        check("once released, the job still fires", len(scheduler.tick()["ran"]) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_bad_job_does_not_stop_the_others():
    tmp = fresh()
    try:
        broken = notify_only(kind="task", when="in 10 minutes",
                             action={"type": "tool", "tool": "no_such_tool_at_all"})
        scheduler.approve(broken["id"])
        good = notify_only(kind="reminder", when="in 10 minutes", message="still fires")
        make_due(broken["id"])
        make_due(good["id"])
        result = scheduler.tick()
        ran = {r["id"]: r for r in result["ran"]}
        check("both jobs were attempted", len(ran) == 2, str(list(ran)))
        check("the broken one is recorded as failed", ran[broken["id"]]["ok"] is False)
        check("the healthy one still fired", ran[good["id"]]["ok"] is True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_bad_triggers_are_rejected():
    tmp = fresh()
    try:
        for bad in ("", "sometime", "every purple"):
            try:
                notify_only(kind="reminder", when=bad, message="x")
                check(f"trigger {bad!r} is rejected", False, "created anyway")
            except scheduler.SchedulerError:
                check(f"trigger {bad!r} is rejected with a usable error", True)
        check("nothing was created", scheduler.list_jobs() == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_notifier_inbox_is_per_consumer():
    tmp = fresh()
    try:
        notifier.notify(title="R", message="m", channels=["inbox"], kind="reminder")
        check("web sees it", len(notifier.pending("web")) == 1)
        check("cli sees it too", len(notifier.pending("cli")) == 1)

        drained = notifier.drain_for_cli()
        check("draining for cli returns it", len(drained) == 1)
        check("cli has now seen it", notifier.pending("cli") == [])
        # The point of per-consumer acks: a browser and a terminal are
        # different places a person might be looking.
        check("but web still has it waiting", len(notifier.pending("web")) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_notifier_always_writes_the_inbox():
    tmp = fresh()
    try:
        # Even asked for a live-only channel, the durable copy must exist —
        # otherwise a reminder raised while nothing is open vanishes.
        record = notifier.notify(title="T", message="m", channels=["stream"], kind="notify")
        check("inbox is forced into the channel list", "inbox" in record["delivered"])
        check("the notification is actually waiting", len(notifier.pending("web")) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_summaries_are_display_ready():
    tmp = fresh()
    try:
        job = notify_only(kind="reminder", when="every day at 9am", message="standup")
        summary = scheduler.summarize(job)
        check("summarize gives a human 'when'", "every" in summary["when"], summary["when"])
        check("summarize gives an eta", summary["in"] is not None)
        overview = scheduler.overview()
        check("overview counts by kind", overview["counts"]["reminders"] == 1)
        check("overview carries the jobs", len(overview["jobs"]) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


for fn in [
    test_one_engine_three_kinds, test_time_trigger_fires_once,
    test_recurring_reschedules, test_missed_runs_fire_once_not_many,
    test_catch_up_is_capped, test_event_trigger, test_event_chaining,
    test_startup_trigger, test_approval_gate, test_lifecycle_actions,
    test_tick_lock_prevents_double_fire, test_bad_job_does_not_stop_the_others,
    test_bad_triggers_are_rejected, test_notifier_inbox_is_per_consumer,
    test_notifier_always_writes_the_inbox, test_summaries_are_display_ready,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
