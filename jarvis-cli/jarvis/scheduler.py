"""The scheduling engine — ONE store and ONE dispatch loop behind scheduled
tasks, notifications, and reminders.

WHY ONE ENGINE INSTEAD OF THREE
-------------------------------
"Run the backup at 2am", "tell me when the backup finishes", and "remind me
to call mum at 6" are the same mechanism wearing three hats: something
decides a moment has arrived, and something else happens. Splitting them
into three subsystems would mean three stores to keep consistent, three
places to fix a DST bug, and — the real killer — no way to chain them
("when the backup task is done, notify me" needs the notification to be
able to listen to the task).

So there is one record type, a JOB, with two independent halves:

    trigger  = WHEN it happens   (at / every / event / startup)
    action   = WHAT happens      (notify / ask / command / tool)

and a `kind` field (task / notify / reminder) that is *purely* labelling —
it steers defaults, CLI colouring, and which UI list a job shows up in, and
nothing else. A reminder IS a notify-action job with kind="reminder". This
is why reminders "use the notifying engine": there was never a second one.

THE HARD CONSTRAINT: THERE IS NO DAEMON
---------------------------------------
Jarvis is a brand-new OS process on every invocation (see the project
summary). Nothing is running between your commands, so nothing can "wake up
at 9am" on its own. Pretending otherwise would produce a scheduler that
silently never fires — the worst possible outcome for a reminder.

Instead this module is driven by an explicit TICK, and ships several
drivers, any of which is enough:

  * `jarvis sched-tick`         — run every due job once, print JSON.
  * web/server.js               — calls sched-tick on an interval while the
                                  web console is open (the usual driver),
                                  and once with --startup on boot.
  * `jarvis sched-daemon`       — an actual standing process (see
                                  sched_daemon.py) that loops sched-tick on
                                  an interval on its own, for anyone who
                                  wants firing to just work without leaving
                                  the web UI open or hand-configuring an OS
                                  scheduler. Still not a Jarvis-managed
                                  background service — you background it
                                  yourself (nohup/systemd/etc.) — but it's
                                  the first-class "make my reminders fire"
                                  answer instead of routing everyone to
                                  Task Scheduler/cron.
  * Windows Task Scheduler/cron — `jarvis sched-tick` every minute, for
                                  firing without running any Jarvis process
                                  continuously at all.
  * `jarvis ask`                — drains *notifications* only (cheap, no
                                  execution) so a terminal user still sees
                                  due reminders without any of the above.

tick() is therefore safe to call from several drivers at once: a lock file
(see _claim_lock) makes concurrent ticks a no-op rather than a double-fire,
because "your 9am reminder arrived twice" and "your backup ran twice" are
both real damage.

MISSED RUNS
-----------
If the machine was asleep for three days, a daily job is three runs
overdue. Firing three times in a row is never what anyone wants, so the
default (`catch_up=False`) is to fire ONCE and then advance to the next
future slot. `catch_up=True` opts into one make-up run per missed slot,
capped by MAX_CATCHUP_RUNS so a month offline can't spawn 30 processes.

SAFETY
------
A scheduled job runs with nobody watching, which is exactly the situation
tool_safety.py's confirm gate exists for. A job whose action would invoke a
confirm-gated tool is created with status="needs_approval" and will not run
until a human approves it out of band (`jarvis sched-approve <id>`, or the
web panel's Approve button) — the model cannot approve its own job, the
same way it can't fill in a `confirm: true` parameter for itself. See
_needs_approval().
"""

import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import timespec
from .timespec import TimeSpecError

JARVIS_DIR = Path.home() / ".jarvis"
STORE_FILE = JARVIS_DIR / "scheduled.json"
LOCK_FILE = JARVIS_DIR / "scheduled.lock"
ASK_LOG_FILE = JARVIS_DIR / "scheduler_ask_log.jsonl"
ENCODING = "utf-8"

KINDS = ("task", "notify", "reminder")
TRIGGER_TYPES = ("at", "every", "event", "startup")
ACTION_TYPES = ("notify", "ask", "command", "tool")

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"
STATUS_PAUSED = "paused"
STATUS_ERROR = "error"
STATUS_NEEDS_APPROVAL = "needs_approval"
ACTIVE_STATUSES = (STATUS_PENDING, STATUS_PAUSED, STATUS_NEEDS_APPROVAL)

# A tick that died mid-run (power cut, kill -9) would otherwise leave its
# lock behind and wedge the scheduler permanently. Any lock older than this
# is treated as abandoned and stolen. Set well above the slowest plausible
# single job (ASK_TIMEOUT below) so a legitimately slow tick is never
# mistaken for a dead one.
LOCK_STALE_SECONDS = 600

# Per-action ceilings. A scheduled job runs unattended, so "hangs forever"
# is indistinguishable from "did nothing" — every path gets a timeout.
ASK_TIMEOUT = 300
COMMAND_TIMEOUT = 300
TOOL_TIMEOUT = 120

MAX_CATCHUP_RUNS = 5
MAX_JOBS = 500            # a runaway loop creating jobs shouldn't fill the disk
MAX_RESULT_CHARS = 4000   # last_result is for humans/the model, not an archive
MAX_ASK_LOG_ENTRIES = 500  # same "don't fill the disk" ceiling as MAX_JOBS,
                           # applied to the ask-prompt log instead of the job
                           # store — see _log_scheduled_ask's docstring.
MAX_ASK_LOG_FIELD_CHARS = 4000  # mirrors MAX_RESULT_CHARS: a runaway prompt
                                 # or reply shouldn't be able to blow up the
                                 # log file or whatever renders sched-ask-log.

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_ID_RE = re.compile(r"^[a-f0-9]{8,32}$")
_EVENT_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")


class SchedulerError(Exception):
    """Raised for a caller mistake (bad trigger, unknown job). Never raised
    out of tick() — a failing job is recorded on the job, not propagated,
    so one broken job can't stop every other one from firing."""


# ---------------------------------------------------------------------------
# Store. Same best-effort JSON-file approach as skill_stickiness.py /
# route_stickiness.py, with one difference: writes go through a temp file +
# os.replace, because unlike a stickiness cache, losing this file loses
# things the user was promised would happen.
# ---------------------------------------------------------------------------


def _empty_store():
    return {"version": 1, "jobs": [], "events": {}, "last_tick": None, "last_startup_tick": None}


def load_store():
    try:
        data = json.loads(STORE_FILE.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return _empty_store()
    if not isinstance(data, dict):
        return _empty_store()
    base = _empty_store()
    base.update({k: v for k, v in data.items() if k in base})
    if not isinstance(base.get("jobs"), list):
        base["jobs"] = []
    if not isinstance(base.get("events"), dict):
        base["events"] = {}
    return base


def save_store(store):
    """Atomic-ish write. A half-written scheduled.json would read back as an
    empty store on the next load (the except above), silently dropping every
    pending reminder — so the new content lands in a temp file first and
    only replaces the real one once it's fully on disk."""
    try:
        JARVIS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STORE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(store, indent=2, default=str) + "\n", encoding=ENCODING)
        os.replace(str(tmp), str(STORE_FILE))
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Tick lock
# ---------------------------------------------------------------------------


def _claim_lock():
    """Best-effort mutual exclusion between concurrent ticks.

    O_CREAT|O_EXCL is atomic on every platform jarvis runs on, so two ticks
    racing here can't both win. Returns True if this process now owns the
    lock. A stale lock (see LOCK_STALE_SECONDS) is stolen rather than
    waited on — a wedged scheduler that never fires again is a much worse
    failure than a rare double-run after a crash.
    """
    try:
        JARVIS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return True  # can't lock, can't create the dir — don't block firing
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - LOCK_FILE.stat().st_mtime
        except OSError:
            return False
        if age < LOCK_STALE_SECONDS:
            return False
        try:
            LOCK_FILE.unlink()
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return False
    except OSError:
        return True
    try:
        os.write(fd, ("%d %s" % (os.getpid(), datetime.now().isoformat())).encode(ENCODING))
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return True


def _release_lock():
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Job construction
# ---------------------------------------------------------------------------


def _new_id():
    return secrets.token_hex(4)


def is_valid_id(job_id):
    return isinstance(job_id, str) and bool(_ID_RE.match(job_id.strip().lower()))


def normalize_trigger(when=None, trigger=None, now=None):
    """Turn whatever a caller supplied into a stored trigger dict.

    Accepts a plain-language/ISO string (`when`), an already-shaped dict
    (`trigger`), "on startup"/"next startup", or "when <event>". Raises
    SchedulerError with a usable message rather than defaulting — see
    timespec's docstring on why guessing a time is the worse failure.
    """
    now = now or datetime.now()

    if isinstance(trigger, dict) and trigger.get("type"):
        ttype = str(trigger["type"]).strip().lower()
        if ttype not in TRIGGER_TYPES:
            raise SchedulerError("unknown trigger type %r (expected one of %s)"
                                 % (ttype, ", ".join(TRIGGER_TYPES)))
        out = {"type": ttype}
        if ttype == "at":
            try:
                out["at"] = timespec.to_iso(timespec.from_iso(trigger.get("at")))
            except TimeSpecError as e:
                raise SchedulerError(str(e))
        elif ttype == "every":
            secs = int(trigger.get("every_seconds") or 0)
            if secs < 30:
                raise SchedulerError("every_seconds must be at least 30")
            out["every_seconds"] = secs
            anchor = trigger.get("at")
            out["at"] = timespec.to_iso(timespec.from_iso(anchor)) if anchor else \
                timespec.to_iso(now + timedelta(seconds=secs))
            if trigger.get("only_on") in ("weekday", "weekend"):
                out["only_on"] = trigger["only_on"]
            # Richer recurrence (timespec.matches_rule): "except holidays",
            # "first monday of the month", "every other tuesday". Preserved
            # verbatim — it's opaque data to this module, evaluated at fire
            # time by the one function that knows what the keys mean.
            if isinstance(trigger.get("rule"), dict) and trigger["rule"]:
                out["rule"] = trigger["rule"]
            # The anchor a fortnightly phase is counted from. Fixed at
            # creation so a skipped run can't shift the phase.
            if trigger.get("anchor"):
                out["anchor"] = trigger["anchor"]
            elif out.get("rule", {}).get("every_n_weeks"):
                out["anchor"] = out["at"]
        elif ttype == "event":
            event = normalize_event(trigger.get("event"))
            out["event"] = event
        return out

    text = (when or "").strip()
    if not text:
        raise SchedulerError("no trigger given — say when this should happen "
                             "('in 20 minutes', 'every day at 9am', 'on startup', "
                             "or 'when backup_done')")

    low = text.lower()
    if re.match(r"^(on |at |next )?(start ?up|boot|launch|login)\b", low) or low in ("startup", "boot"):
        return {"type": "startup"}

    event_match = re.match(r"^(?:when|after|on)\s+(.+?)(?:\s+(?:is|are)\s+done|\s+finishes|\s+completes)?$", low)
    if event_match and not _looks_like_time(low):
        candidate = event_match.group(1).strip()
        return {"type": "event", "event": normalize_event(candidate)}

    try:
        return timespec.parse_trigger(text, now)
    except TimeSpecError as e:
        raise SchedulerError(str(e))


def _looks_like_time(text):
    """Does this phrase contain anything timespec could resolve? Used to keep
    "when the clock hits 5pm" out of the event branch — it starts with
    "when", but it's plainly a time, and turning it into an event named
    "the clock hits 5pm" would create a job that can never fire."""
    try:
        timespec.parse_trigger(re.sub(r"^(when|after|on)\s+", "", text), datetime.now())
        return True
    except TimeSpecError:
        return False


def normalize_event(name):
    """Event names are the join between 'X is done' and 'when X is done', so
    they have to normalize identically from both sides — 'Backup Done!' and
    'backup done' must be the same event or chaining silently never fires."""
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    if not slug or not _EVENT_RE.match(slug):
        raise SchedulerError("event name must be plain letters/numbers, e.g. 'backup_done'")
    return slug[:64]


def _needs_approval(action):
    """True if this action would run something tool_safety.py gates behind a
    human confirmation. A scheduled job has no human in the loop at fire
    time, so the check moves to *creation* time instead: the job is parked
    at status=needs_approval and a person approves it out of band.

    tool_safety is imported lazily — scheduler.py is imported (via
    actions/scheduler_tools.py) partway through tools.py's own
    initialization, and anything reaching back into that module graph at
    import time is the circular-import trap actions/_template.py documents.
    """
    atype = (action or {}).get("type")
    if atype == "notify":
        return False
    try:
        from . import tool_safety
    except ImportError:
        return True  # can't verify -> assume it needs a human. Fail closed.
    if atype == "tool":
        return bool(tool_safety.requires_confirmation((action.get("tool") or "").strip()))
    if atype in ("command", "ask"):
        # A saved command runs arbitrary shell; an ask can reach any tool the
        # model chooses. Neither can be cleared by inspecting a tool name, so
        # both always need a human's sign-off once.
        return True
    return True


def create(kind="reminder", title="", when=None, trigger=None, action=None,
           message="", channels=None, conv_id=None, emit_on_done=None,
           max_runs=None, catch_up=False, trusted=False, now=None):
    """Create one job and persist it. Returns the stored job dict.

    `trusted=True` skips the approval gate — passed by the CLI and the web
    panel (a human is literally typing the command), never by a model-facing
    tool handler.
    """
    now = now or datetime.now()
    kind = (kind or "reminder").strip().lower()
    if kind not in KINDS:
        raise SchedulerError("kind must be one of %s" % ", ".join(KINDS))

    action = dict(action or {})
    if not action.get("type"):
        # A reminder/notify with no explicit action is a notification of its
        # own message — the overwhelmingly common case, so it's the default
        # rather than something every caller has to spell out.
        action = {"type": "notify", "message": message or title}
    atype = str(action.get("type")).strip().lower()
    if atype not in ACTION_TYPES:
        raise SchedulerError("action type must be one of %s" % ", ".join(ACTION_TYPES))
    action["type"] = atype
    if atype == "notify" and not (action.get("message") or "").strip():
        action["message"] = message or title
    if atype == "ask" and not (action.get("prompt") or "").strip():
        raise SchedulerError("an 'ask' action needs a prompt")
    if atype == "command" and not (action.get("command") or "").strip():
        raise SchedulerError("a 'command' action needs a command name")
    if atype == "tool" and not (action.get("tool") or "").strip():
        raise SchedulerError("a 'tool' action needs a tool name")

    trig = normalize_trigger(when=when, trigger=trigger, now=now)

    store = load_store()
    if len([j for j in store["jobs"] if j.get("status") in ACTIVE_STATUSES]) >= MAX_JOBS:
        raise SchedulerError("too many active scheduled jobs (%d) — cancel some first" % MAX_JOBS)

    status = STATUS_PENDING
    if not trusted and _needs_approval(action):
        status = STATUS_NEEDS_APPROVAL

    job = {
        "id": _new_id(),
        "kind": kind,
        "title": (title or message or action.get("prompt") or action.get("command")
                  or action.get("tool") or "scheduled job").strip()[:200],
        "trigger": trig,
        "action": action,
        "status": status,
        "channels": list(channels) if channels else None,
        "conv_id": conv_id or None,
        "emit_on_done": normalize_event(emit_on_done) if emit_on_done else None,
        "max_runs": int(max_runs) if max_runs else None,
        "catch_up": bool(catch_up),
        "created_at": timespec.to_iso(now),
        "next_run": trig.get("at"),
        "last_run": None,
        "run_count": 0,
        "last_result": None,
        "last_error": None,
    }
    store["jobs"].append(job)
    save_store(store)
    return job


# ---------------------------------------------------------------------------
# Query / mutate
# ---------------------------------------------------------------------------


def list_jobs(kind=None, status=None, include_finished=False):
    """Every job, soonest-first. Finished/cancelled jobs are hidden by
    default — a list that grows forever stops being readable, and the
    history lives in last_run/run_count on the recurring jobs anyway."""
    store = load_store()
    out = []
    for job in store["jobs"]:
        if kind and job.get("kind") != kind:
            continue
        if status and job.get("status") != status:
            continue
        if not include_finished and not status and job.get("status") not in ACTIVE_STATUSES:
            continue
        out.append(job)

    def sort_key(j):
        nxt = j.get("next_run")
        if not nxt:
            return (1, j.get("created_at") or "")
        return (0, nxt)

    return sorted(out, key=sort_key)


def get(job_id):
    job_id = (job_id or "").strip().lower()
    for job in load_store()["jobs"]:
        if job.get("id") == job_id:
            return job
    return None


def _mutate(job_id, fn):
    store = load_store()
    for job in store["jobs"]:
        if job.get("id") == (job_id or "").strip().lower():
            result = fn(job)
            save_store(store)
            return result if result is not None else job
    raise SchedulerError("no scheduled job with id %r" % job_id)


def cancel(job_id):
    return _mutate(job_id, lambda j: j.update({"status": STATUS_CANCELLED, "next_run": None}))


def pause(job_id):
    return _mutate(job_id, lambda j: j.update({"status": STATUS_PAUSED}))


def resume(job_id):
    def _resume(job):
        if job.get("status") not in (STATUS_PAUSED, STATUS_ERROR):
            return
        job["status"] = STATUS_PENDING
        # A paused recurring job's next_run is in the past by the time it's
        # resumed; rolling it forward here (rather than letting tick() see it
        # as overdue) is what stops "resume" from meaning "fire immediately".
        if job["trigger"].get("type") == "every":
            job["next_run"] = timespec.to_iso(_advance(job, datetime.now()))
    return _mutate(job_id, _resume)


def approve(job_id):
    """Clear the needs_approval gate. Only ever called from the CLI or the
    web panel — deliberately not exposed as a model-callable tool, which is
    the entire point of the gate."""
    def _approve(job):
        if job.get("status") != STATUS_NEEDS_APPROVAL:
            return
        job["status"] = STATUS_PENDING
        job["approved_at"] = timespec.to_iso(datetime.now())
    return _mutate(job_id, _approve)


def snooze(job_id, delay_text="10 minutes"):
    """Push a job's next run back. The one mutation a reminder actually needs
    at fire time, and the reason notifications carry their job id through to
    the UI."""
    seconds = timespec.parse_delay(delay_text)
    if not seconds:
        raise SchedulerError("couldn't read %r as a delay" % delay_text)

    def _snooze(job):
        target = datetime.now() + timedelta(seconds=seconds)
        job["next_run"] = timespec.to_iso(target)
        job["status"] = STATUS_PENDING
        if job["trigger"].get("type") == "at":
            job["trigger"]["at"] = job["next_run"]
    return _mutate(job_id, _snooze)


def clear_finished():
    store = load_store()
    before = len(store["jobs"])
    store["jobs"] = [j for j in store["jobs"] if j.get("status") in ACTIVE_STATUSES]
    save_store(store)
    return before - len(store["jobs"])


# ---------------------------------------------------------------------------
# Events — the "when something is done" half
# ---------------------------------------------------------------------------


def signal(event, payload=None, run=True):
    """Announce that something happened, firing every job waiting on it.

    Returns {"event", "fired": [ids], "results": [...]}. `run=False` only
    marks the jobs due and leaves them for the next tick — used by tick()
    itself when a finishing job emits its own done-event, so a chain of
    jobs resolves inside one tick without re-entering it recursively.
    """
    event = normalize_event(event)
    store = load_store()
    store["events"][event] = timespec.to_iso(datetime.now())
    fired = []
    for job in store["jobs"]:
        if job.get("status") != STATUS_PENDING:
            continue
        trig = job.get("trigger") or {}
        if trig.get("type") == "event" and trig.get("event") == event:
            job["next_run"] = timespec.to_iso(datetime.now())
            if payload:
                job["last_payload"] = str(payload)[:MAX_RESULT_CHARS]
            fired.append(job["id"])
    save_store(store)
    if fired and run:
        result = tick()
        return {"event": event, "fired": fired, "ran": result.get("ran", [])}
    return {"event": event, "fired": fired, "ran": []}


def recent_events(limit=20):
    store = load_store()
    items = sorted(store["events"].items(), key=lambda kv: kv[1] or "", reverse=True)
    return [{"event": k, "at": v} for k, v in items[:limit]]


# ---------------------------------------------------------------------------
# Due evaluation + tick
# ---------------------------------------------------------------------------


def _weekday_ok(trigger, when):
    """Apply the weekday/weekend filter parse_every() couldn't express as an
    interval (Mon-Fri isn't a constant gap, so it's stored as daily and
    filtered here instead)."""
    only = trigger.get("only_on")
    if only == "weekday" and when.weekday() > 4:
        return False
    if only == "weekend" and when.weekday() < 5:
        return False
    # Richer rules layered on top of the original two. Both are ANDed: a job
    # that is "every weekday except holidays" has to pass the weekday filter
    # AND the holiday one, which is exactly what the phrasing says.
    rule = trigger.get("rule")
    if rule:
        return timespec.matches_rule(when, rule, anchor=trigger.get("anchor"))
    return True


def _advance(job, now):
    """Next future slot for a recurring job, honouring catch_up.

    The loop is what implements "asleep for three days fires once, not
    three times": it walks the schedule forward until it's ahead of `now`,
    rather than adding a single interval to a next_run that's already days
    stale.
    """
    trig = job["trigger"]
    interval = timedelta(seconds=int(trig.get("every_seconds") or 3600))
    try:
        nxt = timespec.from_iso(job.get("next_run") or trig.get("at"))
    except (TimeSpecError, AttributeError):
        nxt = now
    guard = 0
    while nxt <= now or not _weekday_ok(trig, nxt):
        nxt += interval
        guard += 1
        if guard > 10000:  # a corrupt tiny interval shouldn't spin forever
            return now + interval
    return nxt


def due_jobs(now=None, startup=False):
    """Every job that should fire right now. Pure — no side effects — so the
    CLI/web can preview what a tick *would* do without doing it."""
    now = now or datetime.now()
    out = []
    for job in load_store()["jobs"]:
        if not isinstance(job, dict):
            continue
        if job.get("status") != STATUS_PENDING:
            continue
        trig = job.get("trigger")
        # `job.get("trigger") or {}` only defends against a MISSING trigger,
        # not a wrong-typed one. A job whose trigger is a bare string (an
        # older schema, a hand-edit, a half-written store) hit .get() on a
        # str and raised AttributeError out of due_jobs — which runs before
        # tick()'s per-job loop, so the guard there never saw it. One bad
        # job stopped the entire scan, permanently, and every unrelated
        # reminder with it.
        if not isinstance(trig, dict):
            trig = {}
        ttype = trig.get("type")
        if ttype == "startup":
            if startup:
                out.append(job)
            continue
        nxt = job.get("next_run")
        if not nxt:
            continue
        try:
            when = timespec.from_iso(nxt)
        except TimeSpecError:
            continue
        if when <= now and (ttype != "every" or _weekday_ok(trig, when)):
            out.append(job)
    return out


def tick(now=None, startup=False, limit=25):
    """Fire everything due, once. The single entry point every driver calls.

    Never raises: a job that blows up is recorded on itself (status=error,
    last_error) and the loop continues, because one malformed job must not
    stop an unrelated reminder from arriving. Returns a summary dict the
    CLI prints and server.js broadcasts.
    """
    now = now or datetime.now()
    if not _claim_lock():
        return {"ok": True, "skipped": "another tick is already running", "ran": []}

    ran, notifications = [], []
    try:
        pending = due_jobs(now=now, startup=startup)[:limit]
        follow_up_events = []

        for stub in pending:
            store = load_store()
            job = next((j for j in store["jobs"] if j["id"] == stub["id"]), None)
            if job is None or job.get("status") != STATUS_PENDING:
                continue  # cancelled by someone else between the scan and now

            # Everything from here to ran.append() is guarded. _run_action()
            # already catches its own handler errors, but the code around it
            # does not: `job["trigger"]`, `job["kind"]` and `job["title"]`
            # are unchecked subscripts, and a job missing any of them (a
            # hand-edited store, a half-written one, an older schema) raises
            # KeyError right here. That exception propagates out of tick(),
            # and sched_daemon calls tick() unguarded, so the daemon dies and
            # every future reminder silently stops.
            #
            # Worse, the crash can land AFTER the action already fired but
            # BEFORE save_store(), leaving the job status=pending — so the
            # next tick re-fires the notification and crashes again. One
            # malformed job became a duplicate-notification crash loop.
            #
            # This is the behavior the docstring above always claimed: record
            # the failure on the offending job, keep going, let unrelated
            # reminders arrive.
            try:
                runs_this_pass = 1
                trigger = job.get("trigger")
                if job.get("catch_up") and isinstance(trigger, dict) and trigger.get("type") == "every":
                    runs_this_pass = min(_missed_runs(job, now), MAX_CATCHUP_RUNS)

                outcome = None
                for _ in range(max(1, runs_this_pass)):
                    outcome = _run_action(job)
                    job["run_count"] = int(job.get("run_count") or 0) + 1
                    if outcome.get("notification"):
                        notifications.append(outcome["notification"])
                    if not outcome.get("ok"):
                        break

                job["last_run"] = timespec.to_iso(datetime.now())
                job["last_result"] = _trim(outcome.get("summary") if outcome else "")
                job["last_error"] = outcome.get("error") if outcome else None

                _apply_next_state(job, outcome, now)

                if outcome and outcome.get("ok") and job.get("emit_on_done"):
                    follow_up_events.append(job["emit_on_done"])
                # Every completed job also announces itself under a stable name,
                # so "when <that job> is done" can be written before the listener
                # job even exists — the chaining case that made events worth
                # having at all.
                if outcome and outcome.get("ok"):
                    follow_up_events.append("job_%s_done" % job["id"])

                store["last_tick"] = timespec.to_iso(datetime.now())
                if startup:
                    store["last_startup_tick"] = store["last_tick"]
                save_store(store)

                ran.append({
                    "id": job["id"], "kind": job.get("kind"), "title": job.get("title"),
                    "ok": bool(outcome and outcome.get("ok")),
                    "summary": job["last_result"], "error": job["last_error"],
                    "status": job["status"], "next_run": job.get("next_run"),
                })
            except Exception as exc:  # noqa: BLE001 — see the block comment
                # STATUS_ERROR (not STATUS_PENDING) is what stops the crash
                # loop: due_jobs() only returns pending jobs, so parking the
                # broken one here means the next tick skips it entirely
                # instead of re-firing it.
                detail = "%s: %s" % (type(exc).__name__, exc)
                try:
                    job["status"] = STATUS_ERROR
                    job["last_error"] = _trim(detail)
                    job["last_run"] = timespec.to_iso(datetime.now())
                    save_store(store)
                except Exception:  # noqa: BLE001
                    pass  # a store we can't write is not worth dying over either
                ran.append({
                    "id": job.get("id"), "kind": job.get("kind"),
                    "title": job.get("title"), "ok": False, "summary": "",
                    "error": detail, "status": STATUS_ERROR,
                    "next_run": job.get("next_run"),
                })

        if not pending:
            store = load_store()
            store["last_tick"] = timespec.to_iso(datetime.now())
            if startup:
                store["last_startup_tick"] = store["last_tick"]
            save_store(store)
    finally:
        # Released before the follow-up signal below, so a chained job can
        # tick inside the same call without deadlocking on our own lock.
        _release_lock()

    chained = []
    for event in dict.fromkeys(follow_up_events):
        try:
            result = signal(event, run=False)
            if result["fired"]:
                chained.extend(result["fired"])
        except SchedulerError:
            continue
    if chained:
        nested = tick(limit=limit)
        ran.extend(nested.get("ran", []))
        notifications.extend(nested.get("notifications", []))

    # --- riders on the tick ------------------------------------------------
    # Two things that need a regular heartbeat and don't deserve a daemon of
    # their own: daemons whose scheduled start time has arrived, and the
    # ambient monitor's observations. Both run AFTER the lock is released
    # and both are individually wrapped — neither is allowed to be the
    # reason a reminder fails to fire, which is this function's actual job.
    started_daemons = []
    try:
        from . import daemons as _daemons
        started_daemons = _daemons.tick(now=None)
    except Exception:  # noqa: BLE001
        started_daemons = []

    noticed = {}
    try:
        from . import ambient as _ambient
        noticed = _ambient.tick()
    except Exception:  # noqa: BLE001
        noticed = {}

    return {"ok": True, "ran": ran, "notifications": notifications,
            "daemons_started": started_daemons,
            "noticed": noticed.get("new") or [],
            "at": timespec.to_iso(datetime.now())}


def _missed_runs(job, now):
    trig = job["trigger"]
    interval = max(30, int(trig.get("every_seconds") or 3600))
    try:
        nxt = timespec.from_iso(job.get("next_run") or trig.get("at"))
    except TimeSpecError:
        return 1
    missed = int((now - nxt).total_seconds() // interval) + 1
    return max(1, missed)


def _apply_next_state(job, outcome, now):
    """Decide where a job goes after a run: rescheduled, finished, or dead."""
    trig = job.get("trigger") or {}
    ttype = trig.get("type")
    failed = not (outcome and outcome.get("ok"))

    if failed and ttype != "every":
        job["status"] = STATUS_ERROR
        job["next_run"] = None
        return

    if ttype == "every":
        max_runs = job.get("max_runs")
        if max_runs and int(job.get("run_count") or 0) >= int(max_runs):
            job["status"] = STATUS_DONE
            job["next_run"] = None
            return
        job["next_run"] = timespec.to_iso(_advance(job, datetime.now()))
        # A recurring job that failed stays pending on purpose: the usual
        # cause is transient (network down at 9am), and disabling someone's
        # daily job over one bad morning is worse than a second attempt.
        job["status"] = STATUS_PENDING
        return

    if ttype == "event":
        # Event jobs re-arm by default — "notify me when the build finishes"
        # should keep working for the next build too. max_runs=1 makes it
        # one-shot for callers that want that.
        max_runs = job.get("max_runs")
        if max_runs and int(job.get("run_count") or 0) >= int(max_runs):
            job["status"] = STATUS_DONE
            job["next_run"] = None
        else:
            job["status"] = STATUS_PENDING
            job["next_run"] = None
        return

    job["status"] = STATUS_DONE
    job["next_run"] = None


def _trim(text):
    text = "" if text is None else str(text)
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + "\u2026"


# ---------------------------------------------------------------------------
# Action dispatch — the WHAT half
# ---------------------------------------------------------------------------


def _run_action(job):
    """Execute one job's action. Returns {ok, summary, error, notification}.

    Every branch is wrapped: an action raising out of here would abort the
    whole tick and strand every later job in the queue.
    """
    action = job.get("action") or {}
    atype = action.get("type")
    try:
        if atype == "notify":
            return _do_notify(job, action)
        if atype == "ask":
            return _do_ask(job, action)
        if atype == "command":
            return _do_command(job, action)
        if atype == "tool":
            return _do_tool(job, action)
        return {"ok": False, "error": "unknown action type %r" % atype, "summary": ""}
    except Exception as e:  # noqa: BLE001 — see docstring; never re-raise
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "summary": ""}


def _do_notify(job, action):
    from . import notifier  # lazy: notifier reaches into tools for playnite/voice
    message = (action.get("message") or job.get("title") or "").strip()
    if job.get("last_payload"):
        message = "%s\n%s" % (message, job["last_payload"])
    note = notifier.notify(
        title=action.get("title") or _default_title(job),
        message=message,
        channels=job.get("channels"),
        kind=job.get("kind"),
        job_id=job.get("id"),
        conv_id=job.get("conv_id"),
    )
    return {"ok": True, "summary": message, "notification": note, "error": None}


def _default_title(job):
    return {"reminder": "Reminder", "notify": "Jarvis", "task": "Scheduled task"}.get(
        job.get("kind"), "Jarvis")


def _truncate_field(text, limit=MAX_ASK_LOG_FIELD_CHARS):
    text = text or ""
    if len(text) > limit:
        return text[:limit] + "...(truncated)"
    return text


def _log_scheduled_ask(job, prompt, ok, reply=None, error=None):
    """Append one line to ~/.jarvis/scheduler_ask_log.jsonl for every
    prompt the scheduler sends to `jarvis ask`.

    Before this, a scheduled ask's prompt was only ever visible if the job
    happened to carry a conv_id AND that conv_id was still a valid,
    existing conversation — logs.log() silently no-ops otherwise (see its
    is_valid_id() guard), so an ask job created directly via schedule_task
    (no linked conversation) left no trace anywhere of what was actually
    sent once it fired, success or failure. That made a scheduled prompt
    that misbehaved essentially undebuggable after the fact — nothing to
    look at except "it/didn't run".

    This log is deliberately independent of conversations.py/logs.py: it
    always fires, regardless of whether the job has a conv_id, and it
    survives even if that conversation is later deleted. One JSONL file,
    same append-only shape as logs.py's own conversation logs, capped at
    MAX_ASK_LOG_ENTRIES lines the same way MAX_JOBS caps the job store —
    a misbehaving recurring job firing every minute forever shouldn't be
    able to grow this file without bound.
    """
    entry = {
        "ts": datetime.now().isoformat(),
        "job_id": job.get("id"),
        "title": job.get("title") or _default_title(job),
        "kind": job.get("kind"),
        "conv_id": job.get("conv_id"),
        "prompt": _truncate_field(prompt),
        "ok": bool(ok),
        "reply": _truncate_field(reply) if reply else None,
        "error": _truncate_field(error) if error else None,
    }
    try:
        JARVIS_DIR.mkdir(parents=True, exist_ok=True)
        lines = []
        if ASK_LOG_FILE.exists():
            try:
                lines = ASK_LOG_FILE.read_text(encoding=ENCODING).splitlines()
            except OSError:
                lines = []
        lines.append(json.dumps(entry, ensure_ascii=False))
        if len(lines) > MAX_ASK_LOG_ENTRIES:
            lines = lines[-MAX_ASK_LOG_ENTRIES:]
        ASK_LOG_FILE.write_text("\n".join(lines) + "\n", encoding=ENCODING)
    except OSError:
        # Never let a logging failure take down the actual scheduled
        # action — same "never raise" spirit as _run_action's own docstring.
        pass


def read_ask_log(limit=None, job_id=None):
    """Every logged scheduled-ask prompt, newest first. `job_id` filters to
    one job's history; `limit` caps how many entries come back."""
    if not ASK_LOG_FILE.exists():
        return []
    entries = []
    try:
        for line in ASK_LOG_FILE.read_text(encoding=ENCODING).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    entries.reverse()
    if job_id:
        entries = [e for e in entries if e.get("job_id") == job_id]
    if limit:
        entries = entries[:limit]
    return entries


def clear_ask_log():
    try:
        ASK_LOG_FILE.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _do_ask(job, action):
    """Run a scheduled prompt through a *fresh* `jarvis ask` subprocess.

    Deliberately a subprocess rather than an in-process ai_client.ask()
    call: a tick can be running inside the web server's spawned CLI, inside
    another tool's handler, or on its own, and ask() builds a full prompt,
    mutates conversation history, and can call tools — re-entering it from
    inside a tool call is exactly the kind of loop that's hard to bound.
    One process per ask is also, precisely, jarvis's existing runtime model.
    """
    prompt = (action.get("prompt") or "").strip()
    argv = _jarvis_argv() + ["ask", prompt]
    env = dict(os.environ)
    env["JARVIS_UI"] = env.get("JARVIS_UI", "cli")
    env["JARVIS_SCHEDULED"] = "1"  # lets any tool notice it has no human
    if job.get("conv_id"):
        env["JARVIS_CONVERSATION_ID"] = job["conv_id"]
    # Label every log entry this run produces as scheduler-driven, so the
    # Logs viewer can tell a job's ask apart from one the user typed — they
    # land in the same conversation and are otherwise identical on disk.
    env["JARVIS_LOG_SOURCE"] = "scheduler"
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=ASK_TIMEOUT,
            env=env, creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        error = "ask timed out after %ds" % ASK_TIMEOUT
        _log_scheduled_ask(job, prompt, ok=False, error=error)
        return {"ok": False, "error": error, "summary": ""}
    except (OSError, ValueError) as e:
        error = "couldn't run jarvis ask: %s" % e
        _log_scheduled_ask(job, prompt, ok=False, error=error)
        return {"ok": False, "error": error, "summary": ""}

    reply = (proc.stdout or "").strip()
    if proc.returncode != 0 and not reply:
        error = (proc.stderr or "ask failed").strip()[:500]
        _log_scheduled_ask(job, prompt, ok=False, error=error)
        return {"ok": False, "error": error, "summary": ""}

    _log_scheduled_ask(job, prompt, ok=True, reply=reply)

    note = None
    if _should_report(job):
        from . import notifier
        note = notifier.notify(
            title=job.get("title") or "Scheduled task",
            message=reply or "(no output)",
            channels=job.get("channels"),
            kind="task", job_id=job.get("id"), conv_id=job.get("conv_id"),
        )
    return {"ok": True, "summary": reply, "notification": note, "error": None}


def _do_command(job, action):
    """Run a saved command (`jarvis <name> --flag value`) — the same argv the
    user would type, built the same way web/server.js builds it."""
    name = (action.get("command") or "").strip()
    argv = _jarvis_argv() + [name]
    for key, value in (action.get("args") or {}).items():
        argv += ["--%s" % str(key), str(value)]
    env = dict(os.environ)
    env["JARVIS_SCHEDULED"] = "1"
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=COMMAND_TIMEOUT,
            env=env, creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "command timed out after %ds" % COMMAND_TIMEOUT, "summary": ""}
    except (OSError, ValueError) as e:
        return {"ok": False, "error": "couldn't run command: %s" % e, "summary": ""}

    output = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
    ok = proc.returncode == 0
    note = None
    if _should_report(job):
        from . import notifier
        note = notifier.notify(
            title=job.get("title") or ("Command: %s" % name),
            message=(output or ("finished" if ok else "failed"))[:1000],
            channels=job.get("channels"), kind="task",
            job_id=job.get("id"), conv_id=job.get("conv_id"), failed=not ok,
        )
    return {"ok": ok, "summary": output,
            "error": None if ok else "exit code %s" % proc.returncode,
            "notification": note}


def _do_tool(job, action):
    """Call one AI tool directly, in-process.

    Safe to do here (unlike ask) because execute_tool is a single function
    call with no prompt building and no recursion back into the scheduler —
    and it's already been through the approval gate at creation time.
    """
    from . import tools as tools_mod  # lazy: import cycle, see _needs_approval
    name = (action.get("tool") or "").strip()
    args = action.get("args") or {}
    if name not in getattr(tools_mod, "TOOLS", {}):
        return {"ok": False, "error": "no such tool: %s" % name, "summary": ""}
    result = tools_mod.execute_tool(name, args)
    failed = isinstance(result, dict) and bool(result.get("error"))
    summary = json.dumps(result, default=str)[:MAX_RESULT_CHARS]
    note = None
    if _should_report(job):
        from . import notifier
        note = notifier.notify(
            title=job.get("title") or ("Tool: %s" % name),
            message=summary, channels=job.get("channels"), kind="task",
            job_id=job.get("id"), conv_id=job.get("conv_id"), failed=failed,
        )
    return {"ok": not failed, "summary": summary,
            "error": result.get("error") if failed else None, "notification": note}


def _should_report(job):
    """Whether a non-notify action also announces its result.

    Defaults to True: a task that runs silently at 3am and leaves its output
    in a JSON file nobody opens may as well not have run. Set
    action.report=False for genuinely background work.
    """
    return bool((job.get("action") or {}).get("report", True))


def _jarvis_argv():
    """How to invoke jarvis from inside jarvis.

    JARVIS_BIN is what web/server.js already uses to locate the CLI, so
    honouring it here means a scheduled job in a packaged/renamed build
    (see build_tools/sync_entry_point.py) spawns the *same* exe the user is
    running, not whatever unrelated 'jarvis' happens to be on PATH. The
    sys.executable -m fallback covers a source checkout where nothing is
    installed on PATH at all.
    """
    override = (os.environ.get("JARVIS_BIN") or "").strip().strip('"')
    if override:
        return [override]
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "jarvis"]


# ---------------------------------------------------------------------------
# Presentation helpers shared by the CLI, the tools, and server.js's payload
# ---------------------------------------------------------------------------


def summarize(job):
    """One flat, display-ready dict per job. Built here rather than in each
    of the three surfaces so `jarvis sched-list`, the web panel, and the
    model's list_scheduled can never drift into describing the same job
    differently."""
    now = datetime.now()
    when_text = timespec.describe(job.get("trigger") or {})
    eta = None
    if job.get("next_run"):
        try:
            eta = int((timespec.from_iso(job["next_run"]) - now).total_seconds())
        except TimeSpecError:
            eta = None
    return {
        "id": job.get("id"),
        "kind": job.get("kind"),
        "title": job.get("title"),
        "status": job.get("status"),
        "when": when_text,
        "next_run": job.get("next_run"),
        "in": timespec.human_duration(eta) if eta is not None else None,
        "action": (job.get("action") or {}).get("type"),
        "run_count": job.get("run_count", 0),
        "last_run": job.get("last_run"),
        "last_error": job.get("last_error"),
        "needs_approval": job.get("status") == STATUS_NEEDS_APPROVAL,
    }


def overview():
    """Everything a dashboard needs in one read."""
    jobs = list_jobs()
    return {
        "jobs": [summarize(j) for j in jobs],
        "counts": {
            "total": len(jobs),
            "tasks": len([j for j in jobs if j.get("kind") == "task"]),
            "reminders": len([j for j in jobs if j.get("kind") == "reminder"]),
            "notifications": len([j for j in jobs if j.get("kind") == "notify"]),
            "needs_approval": len([j for j in jobs if j.get("status") == STATUS_NEEDS_APPROVAL]),
        },
        "events": recent_events(10),
        "last_tick": load_store().get("last_tick"),
    }
