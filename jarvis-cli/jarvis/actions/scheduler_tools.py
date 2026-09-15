"""scheduling — the model's side of scheduled tasks, notifications and
reminders.

Six tools over ONE engine (jarvis/scheduler.py). The split between them is
about what the model is trying to express, not about three separate
mechanisms underneath:

    remind_me        "remind me to call mum at 6"        kind=reminder
    notify_me        "tell me when the download's done"  kind=notify
    schedule_task    "run the backup every night at 2"   kind=task
    list_scheduled   "what have I got set up?"
    cancel_scheduled cancel / pause / resume / snooze
    signal_event     "the backup finished"  -> fires anything waiting on it

Every one of them lands in the same store, with the same trigger types and
the same tick loop. remind_me and notify_me are, mechanically, schedule_task
with action={"type": "notify"} — which is exactly what "reminders use the
notifying engine" means in practice.

WHY THREE TOOLS INSTEAD OF ONE WITH A `kind` PARAMETER
------------------------------------------------------
A single schedule(kind=...) tool would be smaller in the catalog but much
worse to route: tool_router.route() matches keywords to *tool names*, and
"remind me" / "notify me" / "schedule" are three genuinely different
phrasings a user reaches for. Three narrow tools each get their own keyword
block and their own worked examples in the description, which is what
actually steers correct selection. The shared engine keeps the cost of that
split near zero.

IMPORT DISCIPLINE (see actions/_template.py)
--------------------------------------------
`scheduler` is safe to import at module level — it only pulls in stdlib and
jarvis.timespec, neither of which reaches back into jarvis.tools. Everything
that DOES (tools, tool_safety, notifier's playnite/voice paths) is imported
lazily inside scheduler.py's own function bodies, so this file never
participates in the partially-initialized-jarvis.tools cycle that silently
drops an action from the catalog.
"""

from .. import scheduler
from ..scheduler import SchedulerError

# Channel names accepted on any of the three creating tools. Kept in one
# place so a typo'd channel produces a clear error listing the real ones
# rather than being silently dropped at delivery time.
_CHANNEL_HELP = (
    "Where to deliver it: 'inbox' (always on — the durable queue the web "
    "console and terminal both drain), 'stream' (live in an open web chat), "
    "'toast' (native OS notification), 'voice' (spoken aloud), 'playnite'. "
    "Omit to use the user's configured defaults, which is almost always right."
)

_WHEN_HELP = (
    "When it should happen, in the user's own words — 'in 20 minutes', "
    "'tomorrow at 9am', 'every weekday at 08:30', 'every 2 hours', an exact "
    "'2026-09-16 14:30', 'on startup' (fires the next time Jarvis starts), or "
    "'when <event_name>' to wait for signal_event. Pass the phrasing through "
    "rather than converting it to a timestamp yourself — the parser resolves "
    "it against the real clock, including date rollovers you'd have to guess at."
)


def _ok(job, note=None):
    """One shared success shape. Every creating tool returns the same keys so
    the model doesn't have to learn three result formats, and so a follow-up
    "cancel that" always has an `id` to work with."""
    summary = scheduler.summarize(job)
    out = {
        "ok": True,
        "id": job["id"],
        "kind": job["kind"],
        "title": job["title"],
        "when": summary["when"],
        "next_run": summary["next_run"],
        "in": summary["in"],
        "status": job["status"],
    }
    if job["status"] == scheduler.STATUS_NEEDS_APPROVAL:
        out["needs_approval"] = True
        out["note"] = (
            "Created, but parked pending the user's approval because it runs "
            "something (a command, a gated tool, or a full ask) unattended. "
            "Tell them to approve it in the web console's Scheduled panel or "
            "with `jarvis sched-approve %s` — you cannot approve it yourself."
            % job["id"]
        )
    if note:
        out["note"] = note
    return out


def _err(message, **extra):
    out = {"error": message}
    out.update(extra)
    return out


def _channels(args):
    raw = args.get("channels")
    if not raw:
        return None
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    channels = [c for c in raw if c in scheduler_channels()]
    return channels or None


def scheduler_channels():
    from .. import notifier
    return notifier.CHANNELS


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def tool_remind_me(args, context=None):
    args = args or {}
    message = (args.get("message") or "").strip()
    when = (args.get("when") or "").strip()
    if not message:
        return {"needs_clarification": True, "message": "What should the reminder say?"}
    if not when:
        return {"needs_clarification": True, "message": "When should I remind you?"}
    try:
        job = scheduler.create(
            kind="reminder",
            title=message,
            when=when,
            message=message,
            channels=_channels(args),
            conv_id=getattr(context, "conv_id", None),
            max_runs=args.get("times"),
        )
    except SchedulerError as e:
        return {"needs_clarification": True, "message": str(e)}
    return _ok(job)


def tool_notify_me(args, context=None):
    args = args or {}
    message = (args.get("message") or "").strip()
    if not message:
        return {"needs_clarification": True, "message": "What should the notification say?"}
    when = (args.get("when") or "").strip()

    # No `when` means right now. Going straight to notifier (rather than
    # creating a job due immediately and waiting for a tick) is what makes
    # "tell the user X" feel instant instead of arriving up to a tick
    # interval late.
    if not when:
        from .. import notifier
        record = notifier.notify(
            title=(args.get("title") or "Jarvis").strip(),
            message=message,
            channels=_channels(args),
            kind="notify",
            conv_id=getattr(context, "conv_id", None),
        )
        return {"ok": True, "sent": True, "id": record["id"],
                "delivered": record["delivered"],
                "failed_channels": record["failed_channels"]}

    try:
        job = scheduler.create(
            kind="notify",
            title=(args.get("title") or message).strip(),
            when=when,
            message=message,
            channels=_channels(args),
            conv_id=getattr(context, "conv_id", None),
            max_runs=args.get("times"),
        )
    except SchedulerError as e:
        return {"needs_clarification": True, "message": str(e)}
    return _ok(job)


def tool_schedule_task(args, context=None):
    args = args or {}
    when = (args.get("when") or "").strip()
    if not when:
        return {"needs_clarification": True, "message": "When should this run?"}

    action_type = (args.get("do") or "").strip().lower()
    prompt = (args.get("prompt") or "").strip()
    command = (args.get("command") or "").strip()
    tool_name = (args.get("tool") or "").strip()

    # Infer the action from whichever field was filled, so a model that gives
    # a prompt but forgets `do` still gets what it obviously meant. Explicit
    # `do` always wins.
    if not action_type:
        if prompt:
            action_type = "ask"
        elif command:
            action_type = "command"
        elif tool_name:
            action_type = "tool"
        else:
            return {"needs_clarification": True,
                    "message": "What should the task do? Give a prompt, a saved "
                               "command name, or a tool name."}

    if action_type == "ask":
        if not prompt:
            return {"needs_clarification": True, "message": "What should I do at that time?"}
        action = {"type": "ask", "prompt": prompt}
    elif action_type == "command":
        if not command:
            return {"needs_clarification": True, "message": "Which saved command should run?"}
        action = {"type": "command", "command": command, "args": args.get("args") or {}}
    elif action_type == "tool":
        if not tool_name:
            return {"needs_clarification": True, "message": "Which tool should run?"}
        action = {"type": "tool", "tool": tool_name, "args": args.get("args") or {}}
    else:
        return _err("do must be one of: ask, command, tool")

    action["report"] = bool(args.get("report", True))

    try:
        job = scheduler.create(
            kind="task",
            title=(args.get("title") or prompt or command or tool_name).strip(),
            when=when,
            action=action,
            channels=_channels(args),
            conv_id=getattr(context, "conv_id", None),
            emit_on_done=args.get("emit_on_done"),
            max_runs=args.get("times"),
            catch_up=bool(args.get("catch_up", False)),
        )
    except SchedulerError as e:
        return {"needs_clarification": True, "message": str(e)}
    return _ok(job)


def tool_list_scheduled(args, context=None):
    args = args or {}
    kind = (args.get("kind") or "").strip().lower() or None
    if kind and kind not in scheduler.KINDS:
        return _err("kind must be one of: %s" % ", ".join(scheduler.KINDS))
    jobs = scheduler.list_jobs(kind=kind, include_finished=bool(args.get("include_finished")))
    return {
        "count": len(jobs),
        "jobs": [scheduler.summarize(j) for j in jobs],
        "events": scheduler.recent_events(5),
    }


def tool_cancel_scheduled(args, context=None):
    args = args or {}
    job_id = (args.get("id") or "").strip().lower()
    action = (args.get("action") or "cancel").strip().lower()
    if not job_id:
        return {"needs_clarification": True,
                "message": "Which one? Call list_scheduled first to get its id."}
    if not scheduler.is_valid_id(job_id):
        return _err("%r isn't a scheduled-job id" % job_id)
    try:
        if action == "cancel":
            job = scheduler.cancel(job_id)
        elif action == "pause":
            job = scheduler.pause(job_id)
        elif action == "resume":
            job = scheduler.resume(job_id)
        elif action == "snooze":
            job = scheduler.snooze(job_id, args.get("delay") or "10 minutes")
        else:
            return _err("action must be one of: cancel, pause, resume, snooze")
    except SchedulerError as e:
        return _err(str(e))
    return {"ok": True, "action": action, **scheduler.summarize(job)}


def tool_signal_event(args, context=None):
    args = args or {}
    event = (args.get("event") or "").strip()
    if not event:
        return {"needs_clarification": True,
                "message": "What happened? Give it a short name like 'backup_done'."}
    try:
        result = scheduler.signal(event, payload=args.get("detail"))
    except SchedulerError as e:
        return {"needs_clarification": True, "message": str(e)}
    return {
        "ok": True,
        "event": result["event"],
        "fired": len(result["fired"]),
        "ran": result.get("ran", []),
        "note": ("Nothing was waiting on that event — it's recorded, so a job "
                 "created later with when='when %s' still won't fire "
                 "retroactively." % result["event"]) if not result["fired"] else None,
    }


# ---------------------------------------------------------------------------
# tool_loader contract
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "remind_me",
        "description": (
            "Set a reminder that notifies the user at a time. Use this whenever "
            "they say 'remind me', 'don't let me forget', or 'ping me' about "
            "something — a message delivered back to them later, not work that "
            "runs. Handles one-offs ('in 20 minutes', 'tomorrow at 9') and "
            "repeats ('every weekday at 08:30'). Fires even if the chat is "
            "closed. For work that should actually run (a command, a lookup, a "
            "tool call), use schedule_task instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "What to remind them about, phrased as it should "
                                   "appear: 'Call mum', 'Stand up and stretch'.",
                },
                "when": {"type": "string", "description": _WHEN_HELP},
                "channels": {
                    "type": "array", "items": {"type": "string"},
                    "description": _CHANNEL_HELP,
                },
                "times": {
                    "type": "integer",
                    "description": "For a repeating reminder, stop after this many "
                                   "firings. Omit for unlimited.",
                },
            },
            "required": ["message", "when"],
        },
    },
    {
        "name": "notify_me",
        "description": (
            "Send the user a notification — immediately if `when` is omitted, or "
            "at/on a trigger if given. Use the immediate form to surface "
            "something they should see outside the chat transcript (a long job "
            "you just finished, something you noticed). Use the triggered form "
            "for 'tell me when X is done' — pass when='when x_done' and the "
            "notification waits until signal_event fires that name. Prefer "
            "remind_me for anything the user themselves has to act on."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The notification body."},
                "title": {"type": "string", "description": "Short headline. Defaults to 'Jarvis'."},
                "when": {
                    "type": "string",
                    "description": "Omit to send it right now. Otherwise: " + _WHEN_HELP,
                },
                "channels": {
                    "type": "array", "items": {"type": "string"},
                    "description": _CHANNEL_HELP,
                },
                "times": {
                    "type": "integer",
                    "description": "For a repeating/event notification, stop after "
                                   "this many firings.",
                },
            },
            "required": ["message"],
        },
    },
    {
        "name": "schedule_task",
        "description": (
            "Schedule real work to run later, unattended: a prompt for Jarvis to "
            "answer ('do'='ask'), a saved command ('do'='command'), or a single "
            "tool call ('do'='tool'). Use for 'every morning summarize my "
            "calendar', 'run the backup at 2am', 'check that site every hour', "
            "'on startup, open my dev setup'. The result is notified back to the "
            "user by default. Because it runs with nobody watching, anything "
            "beyond a plain notification is created pending their approval — say "
            "so in your reply. For a message-only reminder, use remind_me."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "when": {"type": "string", "description": _WHEN_HELP},
                "do": {
                    "type": "string",
                    "enum": ["ask", "command", "tool"],
                    "description": "What kind of work. 'ask' runs a prompt through "
                                   "Jarvis in a fresh session; 'command' runs a saved "
                                   "command; 'tool' calls one tool directly. Inferred "
                                   "from whichever of prompt/command/tool you fill in.",
                },
                "prompt": {
                    "type": "string",
                    "description": "For do='ask': the instruction to run at that time, "
                                   "written standalone — it runs in a fresh session with "
                                   "no memory of this conversation, so 'summarize today's "
                                   "calendar', not 'do that thing we discussed'.",
                },
                "command": {"type": "string", "description": "For do='command': the saved command name."},
                "tool": {"type": "string", "description": "For do='tool': the tool name."},
                "args": {
                    "type": "object",
                    "description": "Arguments for do='command' (flags) or do='tool' "
                                   "(the tool's own parameters).",
                },
                "title": {"type": "string", "description": "Short label shown in lists and notifications."},
                "report": {
                    "type": "boolean",
                    "description": "Notify the user with the result when it finishes. "
                                   "Default true; set false for genuinely silent background work.",
                },
                "emit_on_done": {
                    "type": "string",
                    "description": "Announce this event name when the task finishes "
                                   "successfully, so another job created with "
                                   "when='when <name>' runs next. How you chain jobs.",
                },
                "catch_up": {
                    "type": "boolean",
                    "description": "If the machine was off through scheduled runs, make "
                                   "them up on the next tick (capped at 5). Default false: "
                                   "fire once and move on.",
                },
                "times": {"type": "integer", "description": "Stop after this many runs."},
                "channels": {"type": "array", "items": {"type": "string"}, "description": _CHANNEL_HELP},
            },
            "required": ["when"],
        },
    },
    {
        "name": "list_scheduled",
        "description": (
            "List the user's scheduled tasks, reminders and pending notifications, "
            "with their next run time and id. Call this before cancelling or "
            "changing anything — ids are what cancel_scheduled takes — and "
            "whenever they ask what's set up, what's coming, or why something "
            "didn't fire (a job showing needs_approval never ran)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["task", "reminder", "notify"],
                    "description": "Only show one kind. Omit for everything.",
                },
                "include_finished": {
                    "type": "boolean",
                    "description": "Also show completed/cancelled jobs. Default false.",
                },
            },
        },
    },
    {
        "name": "cancel_scheduled",
        "description": (
            "Cancel, pause, resume or snooze one scheduled job by id. Cancel is "
            "permanent; pause keeps it for later; snooze pushes the next run back "
            "by a delay ('10 minutes'), which is what 'not now' about a reminder "
            "that just fired means. Get the id from list_scheduled first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The job id from list_scheduled."},
                "action": {
                    "type": "string",
                    "enum": ["cancel", "pause", "resume", "snooze"],
                    "description": "Default 'cancel'.",
                },
                "delay": {
                    "type": "string",
                    "description": "For action='snooze': how long to push it back. "
                                   "Default '10 minutes'.",
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": "signal_event",
        "description": (
            "Announce that something finished, firing every job waiting on that "
            "event. This is the 'when something is done' half of scheduling: a "
            "job created with when='when backup_done' sits idle until "
            "signal_event('backup_done') runs. Call it when you complete a "
            "multi-step piece of work the user asked to be told about, or when "
            "they tell you something is done. Event names are short and "
            "snake_case; they're matched loosely, so 'Backup done' and "
            "'backup_done' are the same event."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "event": {
                    "type": "string",
                    "description": "Short event name, e.g. 'backup_done', 'build_finished'.",
                },
                "detail": {
                    "type": "string",
                    "description": "Optional extra text appended to any notification "
                                   "this fires — the outcome, a count, an error.",
                },
            },
            "required": ["event"],
        },
    },
]

TOOLS = {
    "remind_me": tool_remind_me,
    "notify_me": tool_notify_me,
    "schedule_task": tool_schedule_task,
    "list_scheduled": tool_list_scheduled,
    "cancel_scheduled": tool_cancel_scheduled,
    "signal_event": tool_signal_event,
}

TOOL_GROUP = "scheduling"

# A brand-new group, so these keywords are the ONLY thing that makes the
# group routable by tool_router.route() (see tool_loader.py's docstring) —
# without them these six tools would be reachable only through search_tools'
# low-confidence fallback. Weights are >= tool_router.MIN_SCORE (5) to count
# as real signal; phrases are matched on word boundaries, not substrings.
#
# Deliberately NOT included: bare "set", "every", "later", "when", "after".
# Each one is common enough in unrelated requests ("set the volume", "every
# game in my library", "when did I install this") that including it would
# pull ordinary messages into this group and push out the tools they
# actually needed.
TOOL_KEYWORDS = {
    "remind_me": {
        "remind me": 10, "reminder": 10, "remind": 9, "don't let me forget": 10,
        "dont let me forget": 10, "nudge me": 8, "ping me": 7, "wake me": 7,
        "alarm": 7, "set a reminder": 10,
    },
    "notify_me": {
        "notify me": 10, "notification": 9, "let me know": 9, "tell me when": 10,
        "alert me": 9, "message me": 7, "notify": 8,
    },
    "schedule_task": {
        "schedule": 10, "scheduled": 9, "every morning": 9, "every night": 9,
        "every day at": 9, "each morning": 8, "on startup": 9, "at startup": 9,
        "next startup": 9, "recurring": 8, "automatically run": 9, "cron": 8,
        "in the background later": 7, "every hour": 8, "daily": 7, "hourly": 7,
    },
    "list_scheduled": {
        "what's scheduled": 10, "whats scheduled": 10, "my reminders": 10,
        "scheduled tasks": 10, "upcoming reminders": 9, "list reminders": 10,
    },
    "cancel_scheduled": {
        "cancel the reminder": 10, "cancel that reminder": 10, "snooze": 9,
        "unschedule": 10, "stop reminding me": 10, "delete the reminder": 10,
    },
    "signal_event": {
        "is done": 6, "has finished": 6, "just finished": 6, "mark as done": 8,
    },
}

TOOL_PACK_INSTRUCTION = (
    "Scheduling, notifications and reminders share one engine: a job is a TRIGGER "
    "(a time, a repeat, an event, or next startup) plus an ACTION (notify / ask / "
    "command / tool). Pass the user's own time wording straight through to `when` "
    "— don't convert it to a timestamp yourself. remind_me and notify_me for "
    "messages back to the user; schedule_task for work that runs. Chain jobs with "
    "schedule_task(emit_on_done='x') + a second job whose when is 'when x'. Always "
    "list_scheduled before cancelling, and tell the user when a job comes back "
    "needs_approval, since it will not run until they approve it."
)

# Creating a scheduled job is itself harmless — nothing runs at creation
# time, and the job's own action is separately gated at creation by
# scheduler._needs_approval(), which parks anything that would run a
# command, a tool requiring confirmation, or a full ask at
# status=needs_approval until a human clears it out of band. Putting a
# confirm prompt on these six tools as well would mean two prompts for one
# decision and a confirm dialog on a plain "remind me in 10 minutes", which
# is the kind of friction that trains people to click through prompts
# without reading them.
TOOL_CONFIRM_REQUIRED = set()
TOOL_AI_REVIEW = set()

# list_scheduled is the only one whose result can get long (every job, each
# with a full summary dict). The others return a single job. At "medium"
# (the default `compact` capacity mode) the per-job fields a model rarely
# needs to reason about are dropped; "low" (ultra) trims to the bone.
TOOL_RESULT_SPECS = {
    "list_scheduled": {
        "drop_fields": {
            "medium": ["events"],
            "low": ["events"],
        },
        "list_item_drop": {
            "jobs": {
                "medium": ["last_run", "run_count", "action"],
                "low": ["last_run", "run_count", "action", "last_error",
                        "next_run", "needs_approval"],
            },
        },
    },
}
