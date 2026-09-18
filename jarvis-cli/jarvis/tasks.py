"""Long-running tasks that survive the process that started them.

THE PROBLEM THIS SOLVES
-----------------------
Everything in jarvis terminates inside one process lifecycle: `jarvis ask`
spins up, does at most MAX_TOOL_ROUNDS tool calls, prints, and dies. That's
the right shape for "what's my battery" and the wrong shape for "keep
retrying this build until it passes" or "research this and write it up".
dev_agent and schedule_watch are both working *around* that ceiling rather
than lifting it.

A Task lifts it by moving the loop **outside** any single process. The unit
of work is still one ordinary ask() — nothing here re-enters ai_client or
invents a second execution model. What changes is that the loop's state
lives on disk, checkpointed after every step, so the thing that advances it
can be a different process each time, minutes or hours apart.

    step N   -> spawn `jarvis ask`  -> checkpoint -> process exits
    (crash, reboot, provider outage, Ctrl+C, whatever)
    step N+1 -> spawn `jarvis ask`  -> checkpoint -> process exits

WHY ONE FILE PER TASK
---------------------
scheduler.py keeps every job in one store and rewrites the whole thing on
each mutation, which is fine at its write rate. A task checkpoints after
every step, and several tasks can be live at once, so a shared store would
turn every step into a read-modify-write race against every other task.
One file per task means two tasks advancing concurrently never touch the
same bytes. Writes go through atomic_io for the same reason conversations
do: this data is the point and cannot be regenerated.

LEASES, NOT LOCKS
-----------------
"Is this task running, or did the machine reboot while it was running?" is
undecidable from a status field alone — a hard kill (the web Stop button is
`taskkill /T /F` on Windows, see atomic_io's docstring) never gets to write
"crashed". So a task claims a **lease**: pid + a wall-clock expiry it
refreshes on each step. The supervisor treats a task whose lease has
expired, or whose pid is gone, as resumable. A lease is advisory — the
worst case is two workers advancing one task, which wastes a step but
cannot corrupt the file, because each checkpoint is a whole-file atomic
replace.

BUDGETS ARE NOT OPTIONAL
------------------------
A loop that can run for hours and spend real money needs a hard stop that
isn't "the user notices". Every task carries max_steps, max_seconds, and a
consecutive-failure ceiling, all checked *before* a step is dispatched.
Exhausting a budget is a terminal state with a reason, never a silent halt.

WHAT THIS MODULE DOES NOT DO
----------------------------
It doesn't call the model. `tasks.py` is the state machine and the store;
`task_runner.py` is what actually dispatches a step and interprets what
came back. Keeping them apart is what lets the state machine be tested with
no API key, no network, and no subprocess — see tests/test_tasks.py.
"""

import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from . import atomic_io, timespec

TASKS_DIR = Path.home() / ".jarvis" / "tasks"

# Lifecycle. "blocked" is deliberately distinct from "failed": blocked means
# the task stopped because it needs a human decision and can be resumed as-is,
# failed means the loop gave up. Only ACTIVE_STATUSES are ever dispatched.
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_BLOCKED = "blocked"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_BLOCKED,
            STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)
ACTIVE_STATUSES = (STATUS_PENDING, STATUS_RUNNING)
TERMINAL_STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)

# Defaults sized so that a runaway task costs an annoying amount, not a
# catastrophic one. 40 steps at ~5 tool rounds each is already a long
# session; 2 hours is longer than any single task should plausibly need.
DEFAULT_MAX_STEPS = 40
DEFAULT_MAX_SECONDS = 2 * 60 * 60
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3

# How long a worker's claim is good for. Longer than the longest plausible
# single step (a step is one ask, and ai_providers' own timeouts cap that),
# short enough that a crashed task is picked back up within a few minutes.
LEASE_SECONDS = 15 * 60

# Caps, so one task file can't grow without bound over a long run.
MAX_PLAN_STEPS = 60
MAX_HISTORY = 200
MAX_NOTES = 60
MAX_TEXT = 4000
MAX_TASKS_ON_DISK = 200

_ID_RE = re.compile(r"^t_[a-z0-9]{8}$")


class TaskError(Exception):
    """Bad input from a caller — surfaced to the user, not a bug."""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def _now():
    return datetime.now()


def _iso(dt=None):
    return timespec.to_iso(dt or _now())


def _from_iso(text):
    """timespec.from_iso, but None instead of TimeSpecError on junk.

    Every caller here is deciding whether a lease expired or a budget ran
    out — questions where an unparseable timestamp must mean "treat it as
    unset" rather than take down the supervisor loop.
    """
    if not text:
        return None
    try:
        return timespec.from_iso(text)
    except Exception:
        return None


def _new_id():
    return "t_" + uuid.uuid4().hex[:8]


def is_valid_id(task_id):
    return bool(task_id) and bool(_ID_RE.match(str(task_id)))


def _path(task_id):
    if not is_valid_id(task_id):
        raise TaskError("bad task id %r" % (task_id,))
    return TASKS_DIR / ("%s.json" % task_id)


def _clip(text, limit=MAX_TEXT):
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit] + "\u2026"


def load(task_id):
    """One task, or None. Never raises on a corrupt/absent file."""
    try:
        path = _path(task_id)
    except TaskError:
        return None
    if not path.exists():
        return None
    try:
        return atomic_io.read_json(path, default=None)
    except Exception:
        return None


def save(task):
    """Checkpoint. Called after every state change, so it must be cheap and
    must never raise — a failed save means we lose this step's progress, not
    that the worker dies mid-task."""
    task["updated_at"] = _iso()
    try:
        return atomic_io.write_json(_path(task["id"]), task)
    except Exception:
        return False


def all_tasks(status=None, include_terminal=False):
    """Every task on disk, newest first. Unreadable files are skipped rather
    than raising — one corrupt task must not hide the other nineteen."""
    out = []
    if not TASKS_DIR.exists():
        return out
    for path in TASKS_DIR.glob("t_*.json"):
        try:
            task = atomic_io.read_json(path, default=None)
        except Exception:
            continue
        if not isinstance(task, dict) or not task.get("id"):
            continue
        if status and task.get("status") != status:
            continue
        if not include_terminal and not status and task.get("status") in TERMINAL_STATUSES:
            continue
        out.append(task)
    out.sort(key=lambda t: str(t.get("created_at") or ""), reverse=True)
    return out


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def create(goal, plan=None, conv_id=None, provider=None, think=None,
           notes=None, max_steps=None, max_seconds=None, title=None,
           parent_id=None, agent=None):
    """Create and persist one task.

    `plan` is optional: a task with no plan starts in "pending" and its
    first step is a planning step (task_runner asks the model to produce
    one). That's deliberately the common path — making the user write the
    plan defeats the point.
    """
    goal = (goal or "").strip()
    if not goal:
        raise TaskError("a task needs a goal")
    if len(all_tasks(include_terminal=True)) >= MAX_TASKS_ON_DISK:
        raise TaskError("too many tasks on disk (%d) — clear finished ones first"
                        % MAX_TASKS_ON_DISK)

    task = {
        "id": _new_id(),
        "title": _clip(title or goal, 200),
        "goal": _clip(goal),
        "status": STATUS_PENDING,
        "plan": _normalize_plan(plan),
        "cursor": 0,
        # Working memory is what survives between steps and gets replayed
        # into the next step's prompt. Kept as structured notes rather than
        # a transcript so it stays bounded — a transcript grows without
        # limit and is exactly what conversations.py already does better.
        "working_memory": {"notes": [], "facts": {}},
        "budget": {
            "max_steps": int(max_steps or DEFAULT_MAX_STEPS),
            "max_seconds": int(max_seconds or DEFAULT_MAX_SECONDS),
            "max_consecutive_failures": DEFAULT_MAX_CONSECUTIVE_FAILURES,
            "steps_used": 0,
            "consecutive_failures": 0,
            "started_at": None,
            "tokens_in": 0,
            "tokens_out": 0,
        },
        # Per-task provider/thinking overrides, same shapes ai_client.ask()
        # already accepts — a research task can be pinned to a cheap model
        # and a debugging task to a thinking one.
        "provider": provider or None,
        "think": think or None,
        "notes": _clip(notes, 1000) if notes else None,
        "conv_id": conv_id or None,
        # Set when this task was spawned by another task (subagent work).
        "parent_id": parent_id or None,
        "agent": agent or None,
        "history": [],
        "result": None,
        "last_error": None,
        "lease": None,
        "created_at": _iso(),
        "updated_at": _iso(),
    }
    save(task)
    return task


def _normalize_plan(plan):
    """Accept a list of strings or a list of dicts; always store dicts."""
    out = []
    for i, entry in enumerate(plan or []):
        if isinstance(entry, dict):
            text = str(entry.get("text") or entry.get("step") or "").strip()
            status = entry.get("status") or "pending"
        else:
            text = str(entry).strip()
            status = "pending"
        if not text:
            continue
        out.append({
            "n": i + 1,
            "text": _clip(text, 500),
            "status": status if status in ("pending", "done", "failed", "skipped") else "pending",
            "result": entry.get("result") if isinstance(entry, dict) else None,
            "at": entry.get("at") if isinstance(entry, dict) else None,
        })
        if len(out) >= MAX_PLAN_STEPS:
            break
    return out


def set_plan(task, plan):
    """Replace the plan. Used by the planning step, and by a mid-run revision
    when the model decides the remaining steps are wrong.

    Completed steps are preserved by position: re-planning after step 3 must
    not resurrect steps 1-3 as pending, or the task loops forever redoing
    work. Only the tail from `cursor` onward is replaced.
    """
    done_head = task.get("plan", [])[:task.get("cursor", 0)]
    tail = _normalize_plan(plan)
    for i, step in enumerate(tail):
        step["n"] = len(done_head) + i + 1
    task["plan"] = (done_head + tail)[:MAX_PLAN_STEPS]
    return task


# ---------------------------------------------------------------------------
# Leases
# ---------------------------------------------------------------------------


def _pid_alive(pid):
    """Mirrors sched_daemon._pid_alive — signal 0 on POSIX, OpenProcess on
    Windows. A pid we can't check is assumed alive, so a permissions quirk
    never causes two workers to fight over one task."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def lease_is_live(task):
    """True if some process currently holds a usable claim on this task."""
    lease = task.get("lease")
    if not isinstance(lease, dict):
        return False
    until = _from_iso(lease.get("until"))
    if until is None:
        # A lease with no readable expiry is not a claim. Falling through to
        # the pid check here would let one corrupt timestamp pin a task as
        # "held" for as long as that pid happens to be alive — and pids get
        # reused, so that can outlive the worker by a long way. Treating it
        # as dead costs at most one duplicated step.
        return False
    if until < _now():
        return False  # expired, regardless of whether the pid is still around
    return _pid_alive(lease.get("pid"))


def claim(task, pid=None):
    """Take the lease. Returns True if we got it, False if someone live has it.

    Racy by construction (check-then-write, no OS lock), and that is
    accepted: see the module docstring. The window is small and the
    consequence is a duplicated step, not corruption.
    """
    if lease_is_live(task):
        return False
    task["lease"] = {
        "pid": int(pid or os.getpid()),
        "until": _iso(_now() + timedelta(seconds=LEASE_SECONDS)),
        "since": _iso(),
    }
    save(task)
    return True


def renew(task):
    if isinstance(task.get("lease"), dict):
        task["lease"]["until"] = _iso(_now() + timedelta(seconds=LEASE_SECONDS))
    return task


def release(task):
    task["lease"] = None
    return task


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------


def budget_exhausted(task):
    """(True, reason) when this task must not dispatch another step."""
    budget = task.get("budget") or {}
    used = int(budget.get("steps_used") or 0)
    max_steps = int(budget.get("max_steps") or DEFAULT_MAX_STEPS)
    if used >= max_steps:
        return True, "step budget exhausted (%d steps)" % max_steps

    started = _from_iso(budget.get("started_at"))
    max_seconds = int(budget.get("max_seconds") or DEFAULT_MAX_SECONDS)
    if started and (_now() - started).total_seconds() >= max_seconds:
        return True, "time budget exhausted (%s)" % timespec.human_duration(max_seconds)

    fails = int(budget.get("consecutive_failures") or 0)
    max_fails = int(budget.get("max_consecutive_failures") or DEFAULT_MAX_CONSECUTIVE_FAILURES)
    if fails >= max_fails:
        return True, "%d steps failed in a row" % fails

    return False, None


def is_runnable(task):
    """Can a supervisor pick this up right now?"""
    if not isinstance(task, dict):
        return False
    if task.get("status") not in ACTIVE_STATUSES:
        return False
    if budget_exhausted(task)[0]:
        return False
    return not lease_is_live(task)


def current_step(task):
    """The plan entry the cursor points at, or None when the plan is done
    (or hasn't been made yet)."""
    plan = task.get("plan") or []
    cursor = int(task.get("cursor") or 0)
    if 0 <= cursor < len(plan):
        return plan[cursor]
    return None


def needs_plan(task):
    return not (task.get("plan") or [])


def start(task):
    """Mark the clock as started. Idempotent — resuming doesn't restart it,
    which matters because max_seconds is wall-clock from first dispatch, so
    a task that sat unattended overnight doesn't get a fresh budget."""
    budget = task.setdefault("budget", {})
    if not budget.get("started_at"):
        budget["started_at"] = _iso()
    task["status"] = STATUS_RUNNING
    return task


def record_step(task, ok, summary, step_index=None, tokens=None, error=None):
    """Checkpoint one completed step. This is THE function the runner calls
    after every dispatch, and it is where the on-disk state advances."""
    budget = task.setdefault("budget", {})
    budget["steps_used"] = int(budget.get("steps_used") or 0) + 1
    if tokens:
        budget["tokens_in"] = int(budget.get("tokens_in") or 0) + int(tokens.get("in") or 0)
        budget["tokens_out"] = int(budget.get("tokens_out") or 0) + int(tokens.get("out") or 0)

    if ok:
        budget["consecutive_failures"] = 0
        task["last_error"] = None
    else:
        budget["consecutive_failures"] = int(budget.get("consecutive_failures") or 0) + 1
        task["last_error"] = _clip(error or summary, 1000)

    entry = {
        "step": step_index if step_index is not None else task.get("cursor"),
        "ok": bool(ok),
        "summary": _clip(summary, 1500),
        "at": _iso(),
    }
    if error:
        entry["error"] = _clip(error, 600)
    history = task.setdefault("history", [])
    history.append(entry)
    # Trim from the front: the oldest steps are the least useful for
    # deciding what to do next, and history is a debugging aid, not state.
    if len(history) > MAX_HISTORY:
        del history[:len(history) - MAX_HISTORY]

    plan = task.get("plan") or []
    cursor = int(task.get("cursor") or 0)
    if 0 <= cursor < len(plan):
        plan[cursor]["status"] = "done" if ok else "failed"
        plan[cursor]["result"] = _clip(summary, 600)
        plan[cursor]["at"] = _iso()
        # A failed step still advances the cursor. Retrying the same step
        # forever is the failure mode this whole module exists to avoid;
        # the consecutive-failure ceiling is what stops a doomed task, and
        # the model can re-add the step via a plan revision if it matters.
        task["cursor"] = cursor + 1

    renew(task)
    save(task)
    return task


def add_note(task, note):
    """Append to working memory — what the next step gets replayed."""
    note = _clip(note, 800)
    if not note:
        return task
    wm = task.setdefault("working_memory", {"notes": [], "facts": {}})
    notes = wm.setdefault("notes", [])
    notes.append({"text": note, "at": _iso()})
    if len(notes) > MAX_NOTES:
        del notes[:len(notes) - MAX_NOTES]
    return task


def set_fact(task, key, value):
    wm = task.setdefault("working_memory", {"notes": [], "facts": {}})
    facts = wm.setdefault("facts", {})
    facts[str(key)[:80]] = _clip(value, 500)
    # Bounded like notes — a model that invents a new key every step
    # shouldn't be able to grow the file forever.
    if len(facts) > 40:
        for extra in list(facts)[:len(facts) - 40]:
            facts.pop(extra, None)
    return task


def finish(task, status, result=None, error=None):
    if status not in TERMINAL_STATUSES + (STATUS_BLOCKED,):
        raise TaskError("not a finishing status: %r" % status)
    task["status"] = status
    if result is not None:
        task["result"] = _clip(result, 6000)
    if error:
        task["last_error"] = _clip(error, 1000)
    release(task)
    save(task)
    return task


def cancel(task_id):
    task = load(task_id)
    if not task:
        raise TaskError("no task %s" % task_id)
    if task.get("status") in TERMINAL_STATUSES:
        return task
    return finish(task, STATUS_CANCELLED, error="cancelled by user")


def resume(task_id):
    """Un-block a blocked task, or re-arm one that hit a budget ceiling.

    Resuming after a budget stop grants a fresh allowance rather than
    raising the ceiling permanently, so "let it keep going" doesn't quietly
    become "no limit at all".
    """
    task = load(task_id)
    if not task:
        raise TaskError("no task %s" % task_id)
    if task.get("status") in (STATUS_DONE, STATUS_CANCELLED):
        raise TaskError("task %s already finished (%s)" % (task_id, task["status"]))

    budget = task.setdefault("budget", {})
    exhausted, _reason = budget_exhausted(task)
    if exhausted:
        budget["max_steps"] = int(budget.get("steps_used") or 0) + DEFAULT_MAX_STEPS
        budget["started_at"] = _iso()
        budget["consecutive_failures"] = 0
    task["status"] = STATUS_PENDING
    task["last_error"] = None
    release(task)
    save(task)
    return task


def clear_finished():
    """Delete terminal tasks. Returns how many went."""
    removed = 0
    for task in all_tasks(include_terminal=True):
        if task.get("status") not in TERMINAL_STATUSES:
            continue
        try:
            _path(task["id"]).unlink()
            removed += 1
        except OSError:
            continue
    return removed


# ---------------------------------------------------------------------------
# Prompt assembly — what a step actually sees
# ---------------------------------------------------------------------------


def progress_line(task):
    plan = task.get("plan") or []
    done = len([s for s in plan if s.get("status") in ("done", "failed", "skipped")])
    return "%d/%d" % (done, len(plan)) if plan else "no plan yet"


def describe(task):
    """One-line summary for listings."""
    return "%s [%s] %s (%s, %d steps used)" % (
        task.get("id"), task.get("status"), task.get("title"),
        progress_line(task), (task.get("budget") or {}).get("steps_used") or 0,
    )


def context_block(task, max_chars=3000):
    """The working-memory digest replayed into the next step's prompt.

    Deliberately a *digest* and not a transcript: bounded, newest-last, and
    it summarizes outcomes rather than quoting them. An unbounded replay is
    how a long task ends up sending its entire history every step, which is
    the exact cost problem the token-optimization work exists to fix.
    """
    parts = []
    facts = ((task.get("working_memory") or {}).get("facts") or {})
    if facts:
        parts.append("Known so far:\n" + "\n".join(
            "  %s: %s" % (k, v) for k, v in list(facts.items())[-15:]))

    notes = ((task.get("working_memory") or {}).get("notes") or [])
    if notes:
        parts.append("Notes:\n" + "\n".join(
            "  - %s" % n.get("text", "") for n in notes[-12:]))

    history = task.get("history") or []
    if history:
        lines = []
        for entry in history[-8:]:
            mark = "ok" if entry.get("ok") else "FAILED"
            lines.append("  step %s [%s]: %s" % (entry.get("step"), mark,
                                                 entry.get("summary") or ""))
        parts.append("Recent steps:\n" + "\n".join(lines))

    block = "\n\n".join(parts)
    if len(block) > max_chars:
        # Trim from the FRONT — the most recent steps are the ones the next
        # step actually needs.
        block = "\u2026\n" + block[-max_chars:]
    return block


def step_prompt(task, control_marker):
    """Build the full prompt for the next step.

    The prompt ends with an explicit control-line contract. Parsing a
    trailing marker line is the same protocol shape cli.py already uses for
    JARVIS_CONFIRM_REQUEST/JARVIS_USAGE, and it degrades the right way: a
    model that ignores the instruction produces a step with no control
    line, which task_runner treats as "step done, continue" rather than an
    error.
    """
    step = current_step(task)
    lines = [
        "You are executing one step of a long-running task. This is step %d."
        % (int((task.get("budget") or {}).get("steps_used") or 0) + 1),
        "",
        "TASK GOAL: %s" % task.get("goal"),
    ]
    if task.get("notes"):
        lines.append("EXTRA INSTRUCTIONS: %s" % task["notes"])

    plan = task.get("plan") or []
    if plan:
        lines.append("")
        lines.append("PLAN:")
        for entry in plan:
            mark = {"done": "x", "failed": "!", "skipped": "-"}.get(entry.get("status"), " ")
            pointer = "  <- you are here" if entry is step else ""
            lines.append("  [%s] %d. %s%s" % (mark, entry.get("n"), entry.get("text"), pointer))

    context = context_block(task)
    if context:
        lines.extend(["", context])

    lines.extend([
        "",
        "Do ONLY this step: %s" % (step.get("text") if step else "decide what to do next"),
        "Use your tools as needed. Do not try to finish the whole task in one go.",
        "",
        "When you're done, end your reply with exactly one line:",
        '%s {"step": "done"|"failed"|"blocked", "summary": "<one sentence>", '
        '"note": "<anything the next step needs to know, optional>", '
        '"task": "continue"|"complete"|"blocked" }' % control_marker,
        "Use task=complete only when the WHOLE goal is achieved, and task=blocked "
        "only when you need a human decision to go further.",
    ])
    return "\n".join(lines)


def plan_prompt(task, control_marker):
    """First-step prompt for a task created without a plan."""
    lines = [
        "Plan a task. Do not execute any of it yet.",
        "",
        "GOAL: %s" % task.get("goal"),
    ]
    if task.get("notes"):
        lines.append("EXTRA INSTRUCTIONS: %s" % task["notes"])
    lines.extend([
        "",
        "Break this into between 2 and 12 concrete steps, each one a single "
        "action or investigation that one round of tool use could reasonably "
        "finish. Prefer fewer, larger steps over many tiny ones.",
        "",
        "Reply with a short sentence about your approach, then exactly one line:",
        '%s {"plan": ["step one", "step two", ...]}' % control_marker,
    ])
    return "\n".join(lines)


def parse_control(text, control_marker):
    """Pull the trailing control line out of a reply. Returns {} when there
    isn't one — an absent control line is normal, not an error.

    Scans from the END because the marker is specified as the last line, and
    a model echoing the instruction back mid-reply shouldn't win over its
    real answer at the bottom.
    """
    if not text:
        return {}
    for line in reversed(str(text).splitlines()):
        line = line.strip()
        if not line.startswith(control_marker):
            continue
        payload = line[len(control_marker):].strip()
        # Some models wrap it in backticks despite being told not to.
        payload = payload.strip("`").strip()
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        return data if isinstance(data, dict) else {}
    return {}


def strip_control(text, control_marker):
    """The reply with the control line removed, for showing a human."""
    if not text:
        return ""
    kept = [ln for ln in str(text).splitlines() if not ln.strip().startswith(control_marker)]
    return "\n".join(kept).strip()
