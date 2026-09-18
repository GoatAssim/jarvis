"""Auto-discovered tools: daemons, raw log search, and the backlog.

Three subsystems in one file because they share a single audience — this
is the "what is my machine doing, and what am I meant to be doing" group —
and splitting them would mean three router groups whose keywords fight
each other for the router's two-group budget.

Each tool is a thin wrapper. The real logic, the invariants and the
reasoning live in daemons.py, log_files.py and backlog.py; nothing here
does anything a caller could not do from the CLI, which is the property
that keeps the tool layer honest.

ONE DELIBERATE OMISSION
-----------------------
There is no `daemon_add` tool. Registering a daemon means storing an argv
that Jarvis will later run unattended, and the model is the component most
exposed to text written by other people (a Discord guest, a fetched web
page, a file it was asked to read). "Add a daemon that runs this command"
is the single highest-value sentence an injection could get executed, and
no amount of confirmation prompting makes handing that capability to the
model a good trade. Creating a daemon is a human action: `jarvis
daemon-add`, or the Daemons panel. Jarvis can start, stop and inspect what
a human already registered.
"""


def _ok(payload):
    out = {"ok": True}
    out.update(payload)
    return out


# ---------------------------------------------------------------------------
# daemons
# ---------------------------------------------------------------------------

def tool_list_daemons(args=None):
    from .. import daemons
    args = args or {}
    entries = daemons.list_daemons()
    if args.get("running_only"):
        entries = [e for e in entries if e.get("running")]
    # Trimmed on purpose: the full registry entry carries env dicts and
    # absolute paths that cost tokens and tell the model nothing it can act
    # on. Anything missing here is one `daemon_console` call away.
    return _ok({
        "count": len(entries),
        "daemons": [{
            "id": e["id"],
            "name": e.get("name") or e["id"],
            "status": e.get("status"),
            "running": e.get("running"),
            "pid": e.get("pid"),
            "builtin": e.get("builtin"),
            "enabled": e.get("enabled", True),
            "next_start": e.get("next_start") or None,
            "last_error": e.get("last_error") or "",
        } for e in entries],
    })


def tool_daemon_status(args):
    from .. import daemons
    did = (args or {}).get("id") or ""
    entry = daemons.get(did)
    if not entry:
        return {"ok": False, "error": f"no daemon '{did}'",
                "hint": "Call list_daemons to see the registered ids."}
    return _ok(daemons.describe(entry))


def tool_daemon_start(args):
    from .. import daemons
    did = (args or {}).get("id") or ""
    ok, message = daemons.start(did)
    if not ok:
        return {"ok": False, "error": message}
    return _ok({"id": did, "message": message,
                "note": "It takes a moment to come up — check daemon_status "
                        "or daemon_console before reporting success."})


def tool_daemon_stop(args):
    from .. import daemons
    did = (args or {}).get("id") or ""
    ok, message = daemons.stop(did)
    return _ok({"id": did, "message": message}) if ok else {
        "ok": False, "error": message}


def tool_daemon_restart(args):
    from .. import daemons
    did = (args or {}).get("id") or ""
    ok, message = daemons.restart(did)
    return _ok({"id": did, "message": message}) if ok else {
        "ok": False, "error": message}


def tool_daemon_console(args):
    from .. import daemons
    args = args or {}
    did = args.get("id") or ""
    if not daemons.get(did):
        return {"ok": False, "error": f"no daemon '{did}'"}
    try:
        lines = max(1, min(int(args.get("lines") or 60), 300))
    except (TypeError, ValueError):
        lines = 60
    output = daemons.read_console(did, lines=lines)
    return _ok({"id": did, "lines": output, "count": len(output),
                "backups": daemons.console_backups(did)})


def tool_daemon_input(args):
    from .. import daemons
    args = args or {}
    did = args.get("id") or ""
    text = args.get("text") or ""
    ok, message = daemons.send_input(did, text)
    return _ok({"id": did, "message": message}) if ok else {
        "ok": False, "error": message}


def tool_daemon_schedule(args):
    from .. import daemons
    args = args or {}
    did = args.get("id") or ""
    ok, result = daemons.schedule(did, args.get("when") or "")
    if not ok:
        return {"ok": False, "error": result}
    return _ok({"id": did, "next_start": result if result != "cleared" else None,
                "message": f"'{did}' will start at {result}"
                           if result != "cleared" else "schedule cleared"})


# ---------------------------------------------------------------------------
# raw log search
# ---------------------------------------------------------------------------

def tool_search_log_files(args):
    from .. import log_files
    args = args or {}
    query = (args.get("query") or "").strip()
    if not query:
        return {"needs_clarification": True,
                "message": "What should I look for in the logs?"}
    sets = args.get("sets")
    if isinstance(sets, str):
        sets = [s.strip() for s in sets.split(",") if s.strip()]
    paths = args.get("paths")
    if isinstance(paths, str):
        paths = [paths]
    result = log_files.search(
        query,
        mode=args.get("mode") or "words",
        limit=args.get("limit") or 40,
        sets=sets,
        paths=paths,
        context=args.get("context") or 0,
    )
    if not result.get("ok"):
        return result
    # The full result carries absolute paths for every hit; the model only
    # needs the short label and the line.
    return _ok({
        "query": query,
        "matches": [{"file": r["file"], "line": r["line"], "text": r["text"]}
                    for r in result["results"]],
        "count": len(result["results"]),
        "truncated": result.get("truncated"),
        "files_scanned": result.get("files_scanned"),
    })


# ---------------------------------------------------------------------------
# backlog
# ---------------------------------------------------------------------------

def tool_backlog_add(args):
    from .. import backlog
    args = args or {}
    item, err = backlog.add(
        args.get("title") or "",
        project=args.get("project") or "",
        state=args.get("state") or backlog.TODO,
        priority=args.get("priority") or "normal",
        note=args.get("note") or "",
        tags=args.get("tags") or [],
    )
    if item is None:
        return {"needs_clarification": True, "message": err}
    return _ok({"id": item["id"], "title": item["title"],
                "state": item["state"], "project": item["project"]})


def tool_backlog_list(args=None):
    from .. import backlog
    args = args or {}
    if args.get("board"):
        grouped = backlog.board()
        return _ok({"board": {
            state: [{"id": i["id"], "title": i["title"],
                     "project": i.get("project") or "",
                     "blocked_on": i.get("blocked_on") or ""}
                    for i in items]
            for state, items in grouped.items() if items}})
    found = backlog.items(
        state=args.get("state"),
        project=args.get("project"),
        tag=args.get("tag"),
        include_done=bool(args.get("include_done")),
        limit=args.get("limit") or 50,
    )
    return _ok({
        "count": len(found),
        "items": [{"id": i["id"], "title": i["title"], "state": i["state"],
                   "project": i.get("project") or "",
                   "priority": i.get("priority"),
                   "blocked_on": i.get("blocked_on") or ""} for i in found],
    })


def tool_backlog_update(args):
    from .. import backlog
    args = args or {}
    query = args.get("item") or args.get("id") or args.get("title") or ""
    if not query:
        return {"needs_clarification": True,
                "message": "Which item? Pass its id or part of its title."}
    item, err = backlog.update(
        query,
        state=args.get("state"),
        priority=args.get("priority"),
        title=args.get("new_title"),
        project=args.get("project"),
        note=args.get("note"),
        blocked_on=args.get("blocked_on"),
        tags=args.get("tags"),
    )
    if item is None:
        return {"ok": False, "error": err}
    return _ok({"id": item["id"], "title": item["title"],
                "state": item["state"],
                "blocked_on": item.get("blocked_on") or ""})


def tool_backlog_summary(args=None):
    from .. import backlog
    return _ok(backlog.summary())


TOOL_SCHEMAS = [
    {
        "name": "list_daemons",
        "description": (
            "List every background service Jarvis knows about (scheduler, "
            "Discord gateway, Instagram webhook, plus any the user added) "
            "with whether each is running."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "running_only": {"type": "boolean",
                                 "description": "Only show ones currently up."},
            },
            "required": [],
        },
    },
    {
        "name": "daemon_status",
        "description": "Detailed state of one daemon: running, pid, uptime, last error.",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "Daemon id, e.g. 'discord'."}},
            "required": ["id"],
        },
    },
    {
        "name": "daemon_start",
        "description": "Start a background service that is registered but not running.",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "daemon_stop",
        "description": "Stop a running background service.",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "daemon_restart",
        "description": "Stop then start a background service — use after a config change.",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "daemon_console",
        "description": (
            "Read a daemon's recent console output. Use this to find out WHY "
            "one crashed or isn't behaving, before guessing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "lines": {"type": "integer", "description": "How many lines from the end (default 60)."},
            },
            "required": ["id"],
        },
    },
    {
        "name": "daemon_input",
        "description": (
            "Type a line into a running daemon's console, for services that "
            "read stdin. Fails cleanly if that daemon doesn't accept input."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "text": {"type": "string", "description": "The line to send."},
            },
            "required": ["id", "text"],
        },
    },
    {
        "name": "daemon_schedule",
        "description": (
            "Set when a daemon should next start, e.g. 'in 2 hours' or "
            "'tomorrow 9am'. Empty clears it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "when": {"type": "string", "description": "A time expression, or empty to clear."},
            },
            "required": ["id"],
        },
    },
    {
        "name": "search_log_files",
        "description": (
            "Search the raw log FILES on disk line by line — conversation "
            "logs, daemon consoles, the scheduler log — and get back file, "
            "line number and the matching line. Use this for crashes, "
            "stack traces and anything a daemon printed. This is different "
            "from search_conversations, which searches what was SAID; this "
            "searches what was written to the log files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words to find. All must appear on one line."},
                "mode": {"type": "string", "enum": ["words", "phrase", "regex"]},
                "sets": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Which log sets: conversations, daemons, scheduler, notifications.",
                },
                "paths": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Specific files or globs to search instead.",
                },
                "context": {"type": "integer", "description": "Lines of context either side (0-10)."},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "backlog_add",
        "description": (
            "Add something to the user's backlog — open-ended work with no "
            "due date. Use for 'add X to my backlog', 'remind me to look at "
            "Y sometime', 'I should Z at some point'. For anything with an "
            "actual time, use remind_me or schedule_task instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "project": {"type": "string", "description": "Optional grouping, e.g. 'website'."},
                "state": {"type": "string", "enum": ["idea", "todo", "doing", "blocked", "done"]},
                "priority": {"type": "string", "enum": ["low", "normal", "high"]},
                "note": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title"],
        },
    },
    {
        "name": "backlog_list",
        "description": (
            "List backlog items, optionally filtered by state or project. "
            "Pass board=true for everything grouped by state."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "state": {"type": "string", "enum": ["idea", "todo", "doing", "blocked", "done"]},
                "project": {"type": "string"},
                "tag": {"type": "string"},
                "board": {"type": "boolean"},
                "include_done": {"type": "boolean"},
                "limit": {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "backlog_update",
        "description": (
            "Change a backlog item — mark it done, start it, block it. The "
            "item can be named by id or by part of its title."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "item": {"type": "string", "description": "Item id, or part of its title."},
                "state": {"type": "string", "enum": ["idea", "todo", "doing", "blocked", "done"]},
                "priority": {"type": "string", "enum": ["low", "normal", "high"]},
                "blocked_on": {"type": "string", "description": "What it's waiting for. Sets state to blocked."},
                "new_title": {"type": "string"},
                "project": {"type": "string"},
                "note": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["item"],
        },
    },
    {
        "name": "backlog_summary",
        "description": (
            "What's in progress, what's blocked and on what, and what has "
            "gone stale. Use for 'what am I working on', 'what's blocked'."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]

TOOLS = {
    "list_daemons": tool_list_daemons,
    "daemon_status": tool_daemon_status,
    "daemon_start": tool_daemon_start,
    "daemon_stop": tool_daemon_stop,
    "daemon_restart": tool_daemon_restart,
    "daemon_console": tool_daemon_console,
    "daemon_input": tool_daemon_input,
    "daemon_schedule": tool_daemon_schedule,
    "search_log_files": tool_search_log_files,
    "backlog_add": tool_backlog_add,
    "backlog_list": tool_backlog_list,
    "backlog_update": tool_backlog_update,
    "backlog_summary": tool_backlog_summary,
}

TOOL_GROUP = "workspace"

TOOL_KEYWORDS = {
    "list_daemons": {
        "daemon": 10, "daemons": 10, "background service": 9,
        # Phrased several ways on purpose: the router matches substrings,
        # so "is the bot running" alone misses "is the discord bot
        # running" — the single most likely way this gets asked.
        "bot running": 10, "bot up": 9, "discord bot": 8,
        "instagram bot": 8, "gateway running": 9, "scheduler running": 10,
        "services running": 8, "what's running": 7, "still running": 8,
    },
    "daemon_start": {
        "start the": 6, "start daemon": 10, "bring up": 7,
        "start the bot": 10, "start the scheduler": 10,
    },
    "daemon_stop": {"stop daemon": 10, "stop the bot": 10, "shut down the": 7},
    "daemon_restart": {"restart the": 8, "restart daemon": 10},
    "daemon_console": {
        "daemon log": 10, "console output": 9, "why did it crash": 9,
        "daemon output": 10,
    },
    "daemon_input": {"send to console": 9, "type into": 7},
    "daemon_schedule": {"start it later": 8, "schedule the daemon": 10},
    "search_log_files": {
        "search the logs": 10, "in the logs": 9, "log file": 9,
        "grep the logs": 10, "find in logs": 10, "traceback": 8,
        "stack trace": 8, "error log": 9, "what went wrong": 6,
    },
    "backlog_add": {
        "backlog": 10, "to my list": 7, "at some point": 6,
        "i should": 5, "add a todo": 9, "note that i need to": 8,
    },
    "backlog_list": {
        "my backlog": 10, "my todos": 9, "what's on my list": 9,
        "kanban": 9, "my board": 8,
    },
    "backlog_update": {
        "mark it done": 9, "mark as done": 9, "i finished": 7,
        "blocked on": 9, "i'm stuck on": 8,
    },
    "backlog_summary": {
        "what am i working on": 10, "what's blocked": 10,
        "what am i in the middle of": 10, "where was i": 7,
    },
}

TOOL_PACK_INSTRUCTION = (
    "Daemons are long-running background services. Before saying one is "
    "broken, read daemon_console — the reason is almost always in it. You "
    "can start/stop/restart a registered daemon, but you cannot create one; "
    "tell the user to run `jarvis daemon-add` if they want a new one. "
    "search_log_files greps the raw log files (use it for crashes and "
    "tracebacks); search_conversations searches what was said. The backlog "
    "is untimed work — anything with a real time goes to remind_me or "
    "schedule_task instead."
)

# Stopping a gateway mid-conversation or restarting the scheduler is
# disruptive and easy to trigger from an ambiguous sentence, so both ask
# first. Starting something is safe: the worst case is a service that was
# already meant to be up.
TOOL_CONFIRM_REQUIRED = {"daemon_stop", "daemon_restart"}
TOOL_AI_REVIEW = set()
