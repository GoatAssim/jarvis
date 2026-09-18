"""Dispatch one task step, interpret the result, checkpoint, exit.

SPLIT FROM tasks.py ON PURPOSE
------------------------------
`tasks.py` is the state machine and can be exercised with no API key, no
network and no subprocess. This module is everything that touches the
outside world. The split is what makes the loop testable: tests/test_tasks.py
drives the state machine directly, and the one function here that needs a
model (`run_step`) takes an injectable `ask` callable so a fake can stand in.

ONE STEP PER PROCESS
--------------------
`_spawn_ask()` shells out to a fresh `jarvis ask` exactly the way
scheduler._do_ask() does, and for exactly the same reason: a tick can be
running inside the web server's spawned CLI, inside another tool's handler,
or standalone, and ask() builds a full prompt, mutates conversation history
and can call tools. Re-entering it from inside a tool call is the unbounded
loop this design is trying to avoid. One process per step is also already
jarvis's runtime model (see history.py's docstring), so nothing new is
being invented here.

The cost is a process spawn per step. That is the right trade: it buys a
hard memory reset between steps, a crash boundary, and the ability for the
*next* step to run on a different machine state entirely (after a reboot,
under a different provider) with no in-memory assumptions carried over.

WHY A MARKER LINE AND NOT A TOOL
--------------------------------
The step's verdict ("done / failed / the whole task is complete") arrives as
a trailing `JARVIS_TASK {json}` line rather than a function call, because a
tool call would have to round-trip through the provider's tool loop and
could be filtered out by the router before the model ever saw it. A trailing
line always survives, costs nothing, and degrades cleanly: no line at all
means "step done, keep going", which is the safe default.
"""

import os
import subprocess
import sys

from . import tasks

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Same protocol shape as cli.py's JARVIS_CONFIRM_REQUEST / JARVIS_USAGE.
CONTROL_MARKER = "JARVIS_TASK"

# A single step is one ask. ai_providers already caps a round, and
# MAX_TOOL_ROUNDS caps the rounds, so this is a backstop against a hung
# subprocess rather than the primary limit.
STEP_TIMEOUT = 900


def _jarvis_argv():
    """How to invoke this same jarvis. Mirrors scheduler._jarvis_argv()."""
    exe = os.environ.get("JARVIS_EXE")
    if exe:
        return [exe]
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "jarvis"]


def _spawn_ask(task, prompt):
    """Run one prompt through a fresh CLI process. Returns (ok, text, error)."""
    argv = _jarvis_argv() + [prompt]

    env = dict(os.environ)
    env["JARVIS_UI"] = env.get("JARVIS_UI", "cli")
    # Lets any tool notice there is no human watching this particular ask,
    # the same signal scheduler._do_ask sets.
    env["JARVIS_SCHEDULED"] = "1"
    env["JARVIS_TASK_ID"] = task.get("id") or ""
    env["JARVIS_LOG_SOURCE"] = "task"
    if task.get("conv_id"):
        env["JARVIS_CONVERSATION_ID"] = task["conv_id"]
    # Per-task overrides ride the same env vars the web UI already uses, so
    # there is one mechanism for "this ask, this provider, this thinking
    # level" rather than three.
    if task.get("provider"):
        provider = task["provider"]
        env["JARVIS_PROVIDER_OVERRIDE"] = (
            ",".join(provider) if isinstance(provider, (list, tuple)) else str(provider))
    if task.get("think"):
        env["JARVIS_THINK_OVERRIDE"] = str(task["think"])
    if task.get("allowed_tools"):
        # Same env var the web UI's tool picker uses — one mechanism for
        # "restrict this ask's tools", not a second parallel one.
        env["JARVIS_ALLOWED_TOOLS"] = ",".join(str(t) for t in task["allowed_tools"])

    # A subagent step spends that role's OWN keys. If the role has no pool,
    # key_env() returns {} and we refuse to dispatch rather than letting the
    # child inherit the ambient config — which is the main Jarvis key. That
    # refusal is the whole isolation guarantee at the call site; see
    # subagents.providers_from_env for the enforcement inside the child.
    if task.get("agent"):
        from . import subagents
        keys = subagents.key_env(task["agent"])
        if not keys:
            return False, "", ("subagent %r has no API key pool — subagents never "
                               "use the main Jarvis key" % task["agent"])
        env.update(keys)

    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=STEP_TIMEOUT,
            env=env, creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False, "", "step timed out after %ds" % STEP_TIMEOUT
    except (OSError, ValueError) as exc:
        return False, "", "couldn't run jarvis: %s" % exc

    text = (proc.stdout or "").strip()
    if proc.returncode != 0 and not text:
        return False, "", (proc.stderr or "ask failed").strip()[:500]
    return True, text, None


def run_step(task, ask=None):
    """Advance one task by exactly one step. Returns the updated task.

    `ask` is an injectable `(task, prompt) -> (ok, text, error)` so tests can
    drive the whole loop without a model. Defaults to a real subprocess.

    Every exit path checkpoints. That is the invariant the whole design
    rests on: if this process dies immediately after this function returns,
    the next supervisor tick must be able to pick up exactly where it left
    off.
    """
    ask = ask or _spawn_ask

    exhausted, reason = tasks.budget_exhausted(task)
    if exhausted:
        return tasks.finish(task, tasks.STATUS_FAILED, error=reason)

    tasks.start(task)
    planning = tasks.needs_plan(task)
    prompt = (tasks.plan_prompt(task, CONTROL_MARKER) if planning
              else tasks.step_prompt(task, CONTROL_MARKER))

    ok, text, error = ask(task, prompt)
    if not ok:
        # A transport failure is a failed step, not a failed task — the
        # consecutive-failure ceiling in tasks.budget_exhausted is what
        # eventually gives up, so a provider outage costs retries, not the
        # whole run. This is the "real teeth" the degraded-reply handling
        # never had: nothing is synthesized, the step just gets retried on
        # a later tick.
        tasks.record_step(task, ok=False, summary="step could not run", error=error)
        exhausted, reason = tasks.budget_exhausted(task)
        if exhausted:
            return tasks.finish(task, tasks.STATUS_FAILED, error=reason)
        tasks.release(task)
        task["status"] = tasks.STATUS_PENDING
        tasks.save(task)
        return task

    control = tasks.parse_control(text, CONTROL_MARKER)
    body = tasks.strip_control(text, CONTROL_MARKER)

    if planning:
        return _apply_plan(task, control, body)
    return _apply_step(task, control, body)


def _apply_plan(task, control, body):
    """Interpret a planning step's reply."""
    plan = control.get("plan")
    if isinstance(plan, str):
        plan = [plan]
    plan = [str(p).strip() for p in (plan or []) if str(p).strip()]

    if not plan:
        # No usable plan came back. Rather than fail, fall through to a
        # one-step plan that is just the goal — a task with a clear goal and
        # no plan is still runnable, and this keeps a weak model from
        # bricking the whole feature.
        plan = [task.get("goal") or "complete the task"]
        tasks.add_note(task, "planner returned no plan; running the goal as a single step")

    tasks.set_plan(task, plan)
    tasks.record_step(task, ok=True,
                      summary="planned %d step%s" % (len(plan), "" if len(plan) == 1 else "s"))
    # The planning step consumed a cursor slot it shouldn't have — planning
    # happens *before* step 1, so rewind so the first real step is next.
    task["cursor"] = 0
    tasks.release(task)
    task["status"] = tasks.STATUS_PENDING
    tasks.save(task)
    return task


def _apply_step(task, control, body):
    """Interpret an execution step's reply and decide what happens next."""
    verdict = str(control.get("step") or "done").strip().lower()
    task_verdict = str(control.get("task") or "continue").strip().lower()
    summary = str(control.get("summary") or "").strip()
    if not summary:
        # Fall back to the reply's first non-empty line, so history is never
        # a wall of blanks just because the model skipped the summary field.
        summary = next((ln.strip() for ln in body.splitlines() if ln.strip()), "(no summary)")

    step_ok = verdict not in ("failed", "fail", "error")
    tasks.record_step(task, ok=step_ok, summary=summary,
                      error=None if step_ok else summary)

    note = str(control.get("note") or "").strip()
    if note:
        tasks.add_note(task, note)

    if task_verdict in ("complete", "completed", "done", "finished"):
        return tasks.finish(task, tasks.STATUS_DONE, result=body or summary)
    if task_verdict in ("blocked", "block") or verdict == "blocked":
        return tasks.finish(task, tasks.STATUS_BLOCKED,
                            result=body or summary,
                            error=summary or "needs a human decision")

    exhausted, reason = tasks.budget_exhausted(task)
    if exhausted:
        return tasks.finish(task, tasks.STATUS_FAILED, result=body, error=reason)

    if tasks.current_step(task) is None:
        # Ran off the end of the plan without the model saying "complete".
        # Treat that as done rather than looping: the plan was the contract,
        # and every step in it has now been attempted.
        return tasks.finish(task, tasks.STATUS_DONE,
                            result=body or "all planned steps attempted")

    tasks.release(task)
    task["status"] = tasks.STATUS_PENDING
    tasks.save(task)
    return task


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


def tick(limit=1, ask=None):
    """Advance runnable tasks. Called by sched_daemon on its normal interval.

    `limit` is deliberately 1 by default: each step is a full ask with real
    token cost, and the daemon ticks often. Advancing every runnable task on
    every tick would turn three parked tasks into three concurrent model
    calls every interval. One step per tick means tasks make steady progress
    and interleave fairly.
    """
    advanced = []
    for task in tasks.all_tasks():
        if len(advanced) >= limit:
            break
        if not tasks.is_runnable(task):
            continue
        if not tasks.claim(task):
            continue  # someone else got there first
        try:
            updated = run_step(task, ask=ask)
            advanced.append({
                "id": updated.get("id"),
                "status": updated.get("status"),
                "progress": tasks.progress_line(updated),
            })
        except Exception as exc:  # noqa: BLE001 — a supervisor must not die
            # An unexpected error here is a bug, but taking down the daemon
            # takes down the scheduler too. Record it on the task, drop the
            # lease so it can be retried, and carry on.
            tasks.record_step(task, ok=False, summary="runner error",
                              error="%s: %s" % (type(exc).__name__, exc))
            tasks.release(task)
            task["status"] = tasks.STATUS_PENDING
            tasks.save(task)
    return {"advanced": advanced, "count": len(advanced)}


def recover_stale():
    """Re-arm tasks whose worker died mid-step. Run once at daemon startup.

    A task left in "running" with a dead or expired lease is exactly the
    crash case this whole module exists for: the step it was on never
    checkpointed a result, so it simply runs again.
    """
    recovered = []
    for task in tasks.all_tasks(status=tasks.STATUS_RUNNING):
        if tasks.lease_is_live(task):
            continue
        tasks.release(task)
        task["status"] = tasks.STATUS_PENDING
        tasks.add_note(task, "resumed after the previous worker stopped unexpectedly")
        tasks.save(task)
        recovered.append(task.get("id"))
    return recovered
