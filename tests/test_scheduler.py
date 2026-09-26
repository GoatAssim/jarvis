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

from jarvis import notifier, scheduler, timespec, conversations  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


def fresh():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis_sched_test_"))
    scheduler.JARVIS_DIR = tmp
    scheduler.STORE_FILE = tmp / "scheduled.json"
    scheduler.LOCK_FILE = tmp / "scheduled.lock"
    scheduler.ASK_LOG_FILE = tmp / "scheduler_ask_log.jsonl"
    notifier.JARVIS_DIR = tmp
    notifier.INBOX_FILE = tmp / "notifications.json"
    notifier.CONFIG_FILE = tmp / "notify_config.json"
    # D.6: _do_ask/_do_command/_do_tool now unconditionally open a fresh
    # conversations.py conversation per run (see _new_scheduled_conversation
    # in scheduler.py) — without repointing these too, every test below
    # would write straight into the real ~/.jarvis/conversations/.
    conversations.JARVIS_DIR = tmp
    conversations.CONV_DIR = tmp / "conversations"
    conversations.INDEX_FILE = conversations.CONV_DIR / "index.json"
    conversations.CURRENT_FILE = tmp / "current_conversation.json"
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


def test_scheduled_ask_logs_the_prompt_regardless_of_conv_id():
    # The gap: an ask job with no conv_id used to leave no record anywhere
    # of what was actually sent. _do_ask should log it unconditionally.
    tmp = fresh()
    orig_run = scheduler.subprocess.run

    class FakeCompleted:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout, self.stderr, self.returncode = stdout, stderr, returncode

    def fake_run(argv, **kwargs):
        return FakeCompleted(stdout="Jarvis: all done here.\n")

    scheduler.subprocess.run = fake_run
    try:
        job = {"id": "job1", "title": "My task", "kind": "task", "conv_id": None}
        action = {"type": "ask", "prompt": "check the thing and report back"}
        result = scheduler._do_ask(job, action)
        check("_do_ask still returns ok=True on a clean run", result["ok"] is True, result)

        entries = scheduler.read_ask_log()
        check("exactly one entry was logged", len(entries) == 1, entries)
        e = entries[0]
        check("the logged prompt matches what was sent", e["prompt"] == "check the thing and report back", e)
        check("the job id is recorded even with no conv_id", e["job_id"] == "job1", e)
        check("the reply is logged too", "all done here" in (e["reply"] or ""), e)
        check("ok=True is recorded", e["ok"] is True, e)
    finally:
        scheduler.subprocess.run = orig_run
        shutil.rmtree(tmp, ignore_errors=True)


def test_scheduled_ask_logs_failures_too():
    tmp = fresh()
    orig_run = scheduler.subprocess.run

    class FakeCompleted:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout, self.stderr, self.returncode = stdout, stderr, returncode

    def fake_run(argv, **kwargs):
        return FakeCompleted(stdout="", stderr="no providers configured", returncode=1)

    scheduler.subprocess.run = fake_run
    try:
        job = {"id": "job2", "title": "Another task", "kind": "task"}
        action = {"type": "ask", "prompt": "do the other thing"}
        result = scheduler._do_ask(job, action)
        check("_do_ask reports failure", result["ok"] is False, result)

        entries = scheduler.read_ask_log()
        check("the failure is logged", len(entries) == 1 and entries[0]["ok"] is False, entries)
        check("the error is captured", "no providers configured" in (entries[0]["error"] or ""), entries)
        check("the prompt is still captured on failure", entries[0]["prompt"] == "do the other thing", entries)
    finally:
        scheduler.subprocess.run = orig_run
        shutil.rmtree(tmp, ignore_errors=True)


def test_ask_log_filters_by_job_and_respects_limit():
    tmp = fresh()
    orig_run = scheduler.subprocess.run

    class FakeCompleted:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout, self.stderr, self.returncode = stdout, stderr, returncode

    scheduler.subprocess.run = lambda argv, **kw: FakeCompleted(stdout="ok\n")
    try:
        for i in range(3):
            scheduler._do_ask({"id": "jobA"}, {"type": "ask", "prompt": "p%d" % i})
        scheduler._do_ask({"id": "jobB"}, {"type": "ask", "prompt": "other"})

        all_entries = scheduler.read_ask_log()
        check("all four entries present", len(all_entries) == 4, all_entries)
        check("newest first", all_entries[0]["prompt"] == "other", all_entries)

        job_a_entries = scheduler.read_ask_log(job_id="jobA")
        check("job filter narrows to just jobA's three entries", len(job_a_entries) == 3, job_a_entries)

        limited = scheduler.read_ask_log(limit=2)
        check("limit caps the returned entries", len(limited) == 2, limited)
    finally:
        scheduler.subprocess.run = orig_run
        shutil.rmtree(tmp, ignore_errors=True)


def test_ask_log_is_capped_and_clearable():
    tmp = fresh()
    orig_max = scheduler.MAX_ASK_LOG_ENTRIES
    orig_run = scheduler.subprocess.run

    class FakeCompleted:
        def __init__(self, stdout=""):
            self.stdout, self.stderr, self.returncode = stdout, "", 0

    scheduler.subprocess.run = lambda argv, **kw: FakeCompleted(stdout="ok\n")
    scheduler.MAX_ASK_LOG_ENTRIES = 3
    try:
        for i in range(5):
            scheduler._do_ask({"id": "j%d" % i}, {"type": "ask", "prompt": "p%d" % i})
        entries = scheduler.read_ask_log()
        check("log is capped at MAX_ASK_LOG_ENTRIES", len(entries) == 3, entries)
        check("the newest entries survive the cap", entries[0]["prompt"] == "p4", entries)

        cleared = scheduler.clear_ask_log()
        check("clear_ask_log reports success", cleared is True)
        check("the log is empty after clearing", scheduler.read_ask_log() == [])
    finally:
        scheduler.subprocess.run = orig_run
        scheduler.MAX_ASK_LOG_ENTRIES = orig_max
        shutil.rmtree(tmp, ignore_errors=True)


def test_L4_display_matches_next_run_after_resume():
    # Root cause (see timespec.describe()'s docstring): trigger["at"] is
    # written once at creation and never touched again, while next_run —
    # the value due_jobs()/tick() actually fire against — DOES advance on
    # resume(). Before the fix, describe() (and therefore summarize()'s
    # "when", which is what the web panel shows) read trigger["at"] only,
    # so it stayed frozen at the original time forever while the job kept
    # firing correctly against the real, advanced next_run.
    tmp = fresh()
    try:
        now = datetime(2026, 9, 26, 0, 0, 0)
        job = scheduler.create(
            kind="task", title="continue",
            trigger={"type": "every", "every_seconds": 86400,
                     "at": timespec.to_iso(now.replace(hour=3))},
            action={"type": "notify", "message": "continue"},
            trusted=True, now=now,
        )
        before = scheduler.summarize(job)
        check("displayed time matches next_run before any advance",
              timespec._friendly(job["next_run"]) in before["when"], before)

        scheduler.pause(job["id"])
        # ...time passes; the original 3:00 slot is now overdue while paused...
        job = scheduler.resume(job["id"])
        check("resume rolled the persisted next_run forward, past the original slot",
              job["next_run"] != job["trigger"]["at"], job)

        after = scheduler.summarize(job)
        check("persisted next_run is a real future time", after["next_run"] == job["next_run"], after)
        check("displayed 'when' text agrees with the ADVANCED next_run, not the frozen creation-time trigger.at",
              timespec._friendly(job["next_run"]) in after["when"], after)
        check("displayed 'when' text is NOT the stale original time",
              timespec._friendly(job["trigger"]["at"]) not in after["when"] or
              job["trigger"]["at"] == job["next_run"], after)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_L4_owner_reported_repro_3am_continue_task():
    # L.4.0's concrete repro, as a fixture: a task scheduled to fire at
    # 3:00 AM whose actual next fire time later became 6:00 AM (the
    # ~3-hour discrepancy the owner reported) must be DISPLAYED as 6:00 AM,
    # not still shown as 3:00 AM.
    tmp = fresh()
    try:
        now = datetime(2026, 9, 26, 0, 0, 0)
        job = scheduler.create(
            kind="task", title="continue",
            trigger={"type": "every", "every_seconds": 3 * 3600,
                     "at": timespec.to_iso(now.replace(hour=3))},
            action={"type": "notify", "message": "continue"},
            trusted=True, now=now,
        )
        check("job displays the correct 3:00 AM run time at creation",
              "03:00" in scheduler.summarize(job)["when"], job)

        # Simulate the job actually firing late, at 6:00 AM, the way a
        # real tick() advances next_run once the action has run (see
        # _apply_next_state) — next_run moves on, trigger["at"] does not.
        job["next_run"] = timespec.to_iso(now.replace(hour=6))

        summary = scheduler.summarize(job)
        check("next_run itself correctly reflects the actual (late) fire time",
              summary["next_run"] == job["next_run"], summary)
        check("the UI-facing 'when' text now shows 6:00 AM, matching next_run, "
              "instead of staying frozen on the originally-displayed 3:00 AM",
              "06:00" in summary["when"] and "03:00" not in summary["when"], summary)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_D6_scheduled_ask_opens_a_brand_new_scheduler_conversation():
    # Before this, an ask job's env["JARVIS_CONVERSATION_ID"] was whatever
    # conversation created the job (or, with none, ai_client.ask() fell
    # back to "current" on disk) — a scheduled run could append into a
    # conversation a person is live in. Every run must get its own fresh,
    # origin="scheduler" conversation instead.
    tmp = fresh()
    orig_run = scheduler.subprocess.run
    seen_env = {}

    class FakeCompleted:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout, self.stderr, self.returncode = stdout, stderr, returncode

    def fake_run(argv, **kwargs):
        seen_env.update(kwargs.get("env") or {})
        return FakeCompleted(stdout="Jarvis: done.\n")

    scheduler.subprocess.run = fake_run
    try:
        creating_conv = conversations.new_conversation(title="live chat", make_current=True)
        job = {"id": "job1", "title": "nightly continue", "kind": "task",
               "conv_id": creating_conv}
        action = {"type": "ask", "prompt": "continue"}
        result = scheduler._do_ask(job, action)

        run_conv_id = result.get("conv_id")
        check("_do_ask returns the id of the conversation it opened",
              conversations.is_valid_id(run_conv_id), result)
        check("the run's conversation is NOT the one that created the job",
              run_conv_id != creating_conv, (run_conv_id, creating_conv))
        check("the spawned subprocess was pointed at the new run conversation",
              seen_env.get("JARVIS_CONVERSATION_ID") == run_conv_id, seen_env)

        record = conversations._load_conv(run_conv_id)
        check("the new conversation is tagged origin=scheduler",
              record is not None and record.get("origin") == "scheduler", record)

        entries = scheduler.read_ask_log(job_id="job1")
        check("the ask log's conv_id points at the new run conversation, not the creating one",
              entries and entries[0]["conv_id"] == run_conv_id, entries)
    finally:
        scheduler.subprocess.run = orig_run
        shutil.rmtree(tmp, ignore_errors=True)


def test_D6_scheduled_command_and_tool_also_get_fresh_conversations():
    tmp = fresh()
    orig_run = scheduler.subprocess.run

    class FakeCompleted:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout, self.stderr, self.returncode = stdout, stderr, returncode

    scheduler.subprocess.run = lambda argv, **kw: FakeCompleted(stdout="backup ok\n")
    try:
        job = {"id": "jobC", "title": "nightly backup", "kind": "task", "conv_id": None}
        result = scheduler._do_command(job, {"type": "command", "command": "backup", "args": {}})
        conv_id = result.get("conv_id")
        check("a command action also opens a fresh conversation",
              conversations.is_valid_id(conv_id), result)
        record = conversations._load_conv(conv_id)
        check("the command's conversation is tagged origin=scheduler and has an exchange logged",
              record is not None and record.get("origin") == "scheduler"
              and len(record.get("exchanges") or []) == 1, record)
    finally:
        scheduler.subprocess.run = orig_run
        shutil.rmtree(tmp, ignore_errors=True)


for fn in [
    test_one_engine_three_kinds, test_time_trigger_fires_once,
    test_recurring_reschedules, test_missed_runs_fire_once_not_many,
    test_catch_up_is_capped, test_event_trigger, test_event_chaining,
    test_startup_trigger, test_approval_gate, test_lifecycle_actions,
    test_tick_lock_prevents_double_fire, test_bad_job_does_not_stop_the_others,
    test_bad_triggers_are_rejected, test_notifier_inbox_is_per_consumer,
    test_notifier_always_writes_the_inbox, test_summaries_are_display_ready,
    test_scheduled_ask_logs_the_prompt_regardless_of_conv_id,
    test_scheduled_ask_logs_failures_too,
    test_ask_log_filters_by_job_and_respects_limit,
    test_ask_log_is_capped_and_clearable,
    test_L4_display_matches_next_run_after_resume,
    test_L4_owner_reported_repro_3am_continue_task,
    test_D6_scheduled_ask_opens_a_brand_new_scheduler_conversation,
    test_D6_scheduled_command_and_tool_also_get_fresh_conversations,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
