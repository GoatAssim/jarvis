"""The tools that make subagents.py reachable from an ordinary ask.

subagents.py is a complete library — spawn(), aggregate(), key isolation —
but nothing before this module could call it except Python code. This is
the steering wheel: five tools the model can call, thin wrappers that do no
policy of their own. Every actual decision (which roles exist, when to
spawn two agents instead of one, how to read their disagreement) belongs in
a SKILL.md, not here — see skills.py's ensure_builtin_skills() for the
`consult` skill that drives these. Keeping the wrappers dumb is what makes
"not hardcoded" true: change the debate strategy by editing markdown, no
code change and no patch required.

SYNCHRONOUS BY DEFAULT
-----------------------
subagents.spawn() persists a runnable Task and returns immediately — by
design, so a fan-out survives the parent process dying (see subagents.py's
own docstring). But the common case from inside an interactive ask is "spawn
two subagents and wait for their answer before replying", and making that
depend on sched_daemon being installed and running would make /consult
silently hang for anyone who hasn't set up the scheduler. So `run_subagents`
below drives task_runner.run_step() itself, in-process, in a loop — the
exact same function the daemon calls, just called synchronously here
instead of once per daemon tick. A subagent task advanced this way is
indistinguishable on disk from one the daemon advanced; if this process
dies mid-loop, the daemon (if running) picks up the lease-expired task
exactly as it would after any other crash.

PARENT LINKAGE
--------------
spawn_subagent defaults `parent_id` to the CURRENT task, read from
JARVIS_TASK_ID — the same env var task_runner._spawn_ask() sets on every
step's subprocess. A subagent spawned from inside an ordinary ask (no
JARVIS_TASK_ID set) simply has no parent; get_subagent_results still works
by id, it just isn't grouped under anything.
"""

import os

from . import subagents, tasks, task_runner

# Ceiling on how many step-rounds run_subagents will drive per call. Each
# round is one ask per still-active subagent, so this bounds the worst case
# (every subagent stuck retrying) to a fixed number of model calls per tool
# invocation, on top of each task's own max_steps.
MAX_ROUNDS = 25


def tool_spawn_subagent(args):
    """Create a subagent task. Returns immediately; the task runs when
    something advances it — call run_subagents to drive it to completion
    now, or leave it for the scheduler daemon to pick up later."""
    args = args or {}
    role = str(args.get("role") or "").strip()
    goal = str(args.get("goal") or "").strip()
    if not role or not goal:
        return {"needs_clarification": True,
                "message": "Need both a role and a goal to spawn a subagent."}

    parent_id = args.get("parent_id") or os.environ.get("JARVIS_TASK_ID") or None
    try:
        task = subagents.spawn(
            role, goal,
            parent_id=parent_id,
            notes=args.get("notes"),
            max_steps=args.get("max_steps"),
        )
    except subagents.SubagentError as exc:
        return {"error": str(exc)}
    return {
        "task_id": task["id"],
        "role": role,
        "goal": goal,
        "parent_id": parent_id,
        "status": task["status"],
        "note": "call run_subagents with this task_id (or its parent_id) to "
                "drive it to completion.",
    }


def tool_run_subagents(args):
    """Advance one or more subagent tasks to completion, synchronously, and
    return the aggregated result.

    Runs task_runner.run_step() in a loop across whichever tasks are still
    active, round-robin, until every named task is terminal or MAX_ROUNDS is
    hit. This is the same step function the scheduler daemon calls — nothing
    here re-implements the task loop, it just drives it inline so a /consult
    doesn't have to wait for a background tick.
    """
    args = args or {}
    task_ids = args.get("task_ids")
    parent_id = args.get("parent_id") or os.environ.get("JARVIS_TASK_ID") or None

    if task_ids:
        ids = [str(t) for t in task_ids]
    elif parent_id:
        ids = [t["id"] for t in subagents.children(parent_id)]
    else:
        return {"error": "pass task_ids or parent_id — nothing to run"}

    if not ids:
        return {"error": "no matching subagent tasks found"}

    rounds = 0
    # BUGFIX: `int(args.get("max_rounds") or MAX_ROUNDS)` treated an
    # explicit `max_rounds: 0` the same as "not provided" (0 is falsy in
    # Python), silently running up to MAX_ROUNDS instead of the zero rounds
    # actually requested. `is None` is the only case that should mean
    # "use the default".
    raw_max_rounds = args.get("max_rounds")
    max_rounds = MAX_ROUNDS if raw_max_rounds is None else int(raw_max_rounds)
    max_rounds = max(0, min(max_rounds, MAX_ROUNDS))
    while rounds < max_rounds:
        pending = []
        for tid in ids:
            task = tasks.load(tid)
            if task and task.get("status") in tasks.ACTIVE_STATUSES:
                pending.append(task)
        if not pending:
            break
        for task in pending:
            if not tasks.claim(task):
                continue  # another worker (e.g. the daemon) has it right now
            task_runner.run_step(task)
        rounds += 1

    results = [tasks.load(tid) for tid in ids]
    results = [t for t in results if t]

    # BUGFIX: every id in `ids` failing to resolve (a typo, or a task
    # already garbage-collected) used to fall straight through to the
    # summary code below with an empty `results`, which reports
    # `all_finished: true` — indistinguishable from "ran fine, nothing left
    # to do" when what actually happened is "none of these ids exist".
    # subagent_status already gets this right for a single id; do the same
    # here rather than claiming false success.
    if not results:
        return {"error": "no matching subagent tasks found for the given id(s)"}

    if parent_id:
        summary = subagents.summary_for_parent(parent_id)
        agg = subagents.aggregate(parent_id)
    else:
        # No parent to group under — build the same shape from just the
        # tasks that were actually asked for, so the tool's return contract
        # doesn't depend on whether the caller used parent_id or task_ids.
        agg = {
            "total": len(results),
            "done": [r for r in results if r.get("status") == tasks.STATUS_DONE],
            "blocked": [r for r in results if r.get("status") == tasks.STATUS_BLOCKED],
            "failed": [r for r in results if r.get("status") in
                      (tasks.STATUS_FAILED, tasks.STATUS_CANCELLED)],
            "pending": [r for r in results if r.get("status") in tasks.ACTIVE_STATUSES],
        }
        agg["all_finished"] = not agg["pending"]
        lines = ["Subagent results (%d total):" % agg["total"]]
        for r in results:
            lines.append("  [%s] %s (%s): %s" % (
                r.get("status"), r.get("agent"), r.get("id"),
                (r.get("result") or r.get("last_error") or "")[:400]))
        summary = "\n".join(lines)

    return {
        "rounds_run": rounds,
        "all_finished": agg.get("all_finished", agg.get("pending") == []),
        "summary": summary,
        "results": [
            {"id": t.get("id"), "role": t.get("agent"), "status": t.get("status"),
             "result": t.get("result"), "error": t.get("last_error")}
            for t in results
        ],
    }


def tool_subagent_status(args):
    """Check one subagent's current status without advancing it."""
    args = args or {}
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return {"needs_clarification": True, "message": "Which task_id?"}
    task = tasks.load(task_id)
    if not task:
        return {"error": "no such task: %s" % task_id}
    return {
        "id": task["id"], "role": task.get("agent"), "goal": task.get("goal"),
        "status": task["status"], "progress": tasks.progress_line(task),
        "result": task.get("result"), "error": task.get("last_error"),
    }


def tool_list_subagent_roles(args=None):
    """What roles exist to spawn, and whether each has a usable key pool.
    A role with pool_configured=false will refuse to spawn — say so plainly
    rather than trying it and surfacing a raw error."""
    cfg = subagents.load_config()
    roles = subagents.agents(cfg)
    return {
        "roles": [
            {"name": name, "description": spec.get("description"),
             "builtin": spec.get("builtin", False),
             "pool_configured": subagents.key_pool(name, cfg) is not None}
            for name, spec in sorted(roles.items())
        ],
    }


SUBAGENT_TOOL_SCHEMAS = [
    {
        "name": "spawn_subagent",
        "description": (
            "Create a subagent task with its own API key, its own tool "
            "allowance, and its own budget — for delegating a scoped piece "
            "of a larger job (a coding fix, a research question, a stance "
            "in a debate). Call list_subagent_roles first if you don't "
            "already know which roles exist. Returns immediately; call "
            "run_subagents to actually drive it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "Which subagent role to use."},
                "goal": {"type": "string", "description": "The scoped task for this subagent."},
                "notes": {"type": "string", "description": "Extra instructions appended to the role's prompt."},
                "max_steps": {"type": "integer", "description": "Override the role's default step budget."},
                "parent_id": {"type": "string", "description": "Group under this parent task id. Defaults to the current task, if any."},
            },
            "required": ["role", "goal"],
        },
    },
    {
        "name": "run_subagents",
        "description": (
            "Drive one or more spawned subagents to completion right now, "
            "synchronously, and return their combined results. Pass either "
            "task_ids (specific subagents) or parent_id (everything spawned "
            "under that parent). Use this instead of waiting — it does not "
            "return until every named subagent is done, blocked, failed, "
            "or the round cap is hit."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_ids": {"type": "array", "items": {"type": "string"},
                            "description": "Specific subagent task ids to run."},
                "parent_id": {"type": "string",
                             "description": "Run every subagent spawned under this parent."},
                "max_rounds": {"type": "integer",
                              "description": "Cap on step-rounds to run (default %d)." % MAX_ROUNDS},
            },
            "required": [],
        },
    },
    {
        "name": "subagent_status",
        "description": "Check a subagent's current status without advancing it.",
        "parameters": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "list_subagent_roles",
        "description": "List available subagent roles and whether each has a configured API key pool.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]

SUBAGENT_TOOLS = {
    "spawn_subagent": tool_spawn_subagent,
    "run_subagents": tool_run_subagents,
    "subagent_status": tool_subagent_status,
    "list_subagent_roles": tool_list_subagent_roles,
}
