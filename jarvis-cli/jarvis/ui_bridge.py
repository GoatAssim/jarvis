"""UI bridge — let any tool put something on the user's screen, and get an answer back.

WHY
---
Until now a tool could return a dict and that was it. Everything richer was
hardcoded: screenshots had their own JARVIS_MEDIA line, downloads had
another, present_file a third, dev_agent a fourth, and the confirm gate had
its own separate JARVIS_CONFIRM_REQUEST protocol. Each one meant an edit in
tools.py, a branch in app.js, and CSS. Fine for five built-ins; hopeless as
the answer to "my custom tool wants to warn the user about something".

So this generalizes the pattern the codebase already proved works, into one
protocol any tool — built-in, auto-discovered, or a user's own custom
Python — can call:

    from jarvis import ui_bridge as ui

    ui.toast("Backup finished", level="success")
    ui.bubble("Disk report", body=table, level="info")
    ui.dialog("Careful", "This will overwrite 12 files.", level="warn")
    if ui.confirm("Delete 12 files?", body=file_list):
        ...
    choice = ui.choose("Which environment?", ["staging", "production"])
    name   = ui.prompt("What should I call the backup?")

TWO DIRECTIONS, TWO CHANNELS
----------------------------
**Fire-and-forget** (toast/bubble/dialog/progress) goes out as
`JARVIS_MEDIA\\tui\\t<json>` on **stderr**, exactly like present_tools.py
and dev_agent_events.py. The web server already forwards the child's stderr
line-by-line, so these appear the instant they're printed — no new transport,
no new server route.

**Blocking prompts** (confirm/choose/prompt/form) need an answer, so they
reuse the shape cli.confirm_tool_call already proved: print a
`JARVIS_UI_REQUEST {...}` line on **stdout**, then block reading one line
from stdin. server.js relays it to the browser and writes the answer back.
Same mechanism, one generalized message type instead of a confirm-only one.

THREE SURFACES, ONE CALL
------------------------
The same call has to work in three places, and the caller must not have to
care which it's in:

    web UI       -> a real popup / in-chat bubble
    plain CLI    -> ANSI-styled terminal output, input() for prompts
    headless     -> (scheduler tick, discord daemon) no one is watching, so
                    a prompt returns its default instead of hanging forever

That last one is the important one. A tool that blocks on input() inside a
scheduled job at 3am hangs the whole tick, silently, forever. Every blocking
call here therefore has a mandatory default and a timeout, and in a context
with nobody attached it returns the default immediately rather than waiting.
A tool that needs an answer nobody can give does not get to stop the world.

SAFETY
------
This module can show things and ask things. It cannot *do* things — there is
no "run this" payload, no HTML injection (the browser side renders every
field as text, never as markup), and `confirm()` here is NOT a substitute for
tool_safety.py's confirm_required gate. A tool calling ui.confirm() is asking
a question; a tool flagged confirm_required is *being gated*, out of band,
by a config file the model can't reach. Those are different mechanisms on
purpose and this one never replaces the other.
"""

import json
import os
import sys
import time

# Levels a surface can style differently. Ordered by severity so a renderer
# can map them onto whatever it has (colors, icons, sounds).
LEVELS = ("info", "success", "warn", "error")
DEFAULT_LEVEL = "info"

KINDS = ("toast", "bubble", "dialog", "confirm", "choose", "prompt", "form",
         "progress", "dismiss")

# Anything longer than this is a log, not a popup. Truncated rather than
# rejected: a tool that accidentally passes 4MB of stdout should produce a
# clipped popup, not a failed one.
MAX_TITLE = 120
MAX_BODY = 4000
MAX_OPTIONS = 12

# How long a blocking prompt waits before giving up and returning its
# default. Long enough that a distracted person can still answer, short
# enough that a forgotten dialog can't wedge a scheduled job until reboot.
DEFAULT_TIMEOUT = 120

_seq = [0]
_cli_hook = None


def set_hook(fn):
    """Register a callback that sees every emitted event.

    Same seam as dev_agent_events.set_hook: in plain-CLI mode there is no
    subprocess boundary for anything to intercept, so cli.py registers a
    hook to pretty-print events itself. Never raises on a bad callback.
    """
    global _cli_hook
    _cli_hook = fn


def _next_id(prefix="ui"):
    _seq[0] += 1
    return "%s_%d_%d" % (prefix, os.getpid(), _seq[0])


def _surface():
    """Where are we rendering? Decided per call, never cached.

    JARVIS_UI is set by the web server on the processes it spawns; a tty on
    stdin means a human is at a terminal; anything else (a scheduler tick, a
    daemon, a test) is headless.
    """
    if (os.environ.get("JARVIS_UI") or "").strip().lower() == "web":
        return "web"
    try:
        if sys.stdin.isatty():
            return "cli"
    except Exception:  # noqa: BLE001 — a detached stdin can raise on some platforms
        pass
    return "headless"


def _clip(text, limit):
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "\u2026"


def _normalize_level(level):
    level = (str(level or DEFAULT_LEVEL)).strip().lower()
    aliases = {"warning": "warn", "danger": "error", "fail": "error",
               "failure": "error", "ok": "success", "done": "success",
               "note": "info", "notice": "info"}
    level = aliases.get(level, level)
    return level if level in LEVELS else DEFAULT_LEVEL


def emit(kind, **fields):
    """Print one fire-and-forget UI event. Returns the event dict.

    Never raises: a popup failing to serialize must not take down the tool
    that tried to show it — the same contract dev_agent_events.emit() has,
    and for the same reason.
    """
    event = {"kind": kind, "id": fields.pop("id", None) or _next_id(), **fields}
    try:
        line = "JARVIS_MEDIA\tui\t" + json.dumps(event, default=str, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        event = {"kind": "toast", "id": event["id"], "level": "error",
                 "message": "a tool tried to show something unserializable: %s" % exc}
        line = "JARVIS_MEDIA\tui\t" + json.dumps(event)
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass
    if _cli_hook is not None:
        try:
            _cli_hook(event)
        except Exception:  # noqa: BLE001 — a broken hook is not the tool's problem
            pass
    return event


# ---------------------------------------------------------------------------
# Fire-and-forget
# ---------------------------------------------------------------------------


def toast(message, level="info", timeout=None, title=""):
    """A transient corner message. The lightest possible "FYI".

    This is the generic form of the error popup the web UI already shows for
    its own failures — same widget, now reachable by any tool.
    """
    level = _normalize_level(level)
    event = emit("toast", message=_clip(message, MAX_BODY), level=level,
                 title=_clip(title, MAX_TITLE),
                 timeout=int(timeout) if timeout else None)
    if _surface() == "cli":
        _print_cli(level, title or level.upper(), message)
    return {"ok": True, "shown": "toast", "id": event["id"]}


def bubble(title, body="", level="info", data=None):
    """A card in the chat thread itself.

    For output the user will want to scroll back to — a table, a diff, a
    summary. Unlike a toast it persists in the conversation, and unlike a
    tool's return value it's shown as its own styled block rather than being
    swallowed into the model's prose.
    """
    level = _normalize_level(level)
    event = emit("bubble", title=_clip(title, MAX_TITLE),
                 body=_clip(body, MAX_BODY), level=level, data=data or {})
    if _surface() == "cli":
        _print_cli(level, title, body)
    return {"ok": True, "shown": "bubble", "id": event["id"]}


def dialog(title, body="", level="info", actions=None, blocking=False):
    """A real modal, outside the chat.

    For something that should interrupt: a misconfiguration, a destructive
    result, a "this needs your attention before anything else". `actions` is
    a list of {label, value} — purely informational here (nothing runs when
    clicked); use confirm()/choose() when you need the answer.
    """
    level = _normalize_level(level)
    event = emit("dialog", title=_clip(title, MAX_TITLE),
                 body=_clip(body, MAX_BODY), level=level,
                 actions=_normalize_actions(actions), blocking=bool(blocking))
    if _surface() == "cli":
        _print_cli(level, title, body)
    return {"ok": True, "shown": "dialog", "id": event["id"]}


def progress(label, value=None, total=None, job_id=None, done=False):
    """An updatable progress row. Call repeatedly with the same job_id.

    Returns the job_id so the first call can be `pid = ui.progress("...")`
    and later ones `ui.progress("...", 3, 10, job_id=pid)`.
    """
    job_id = job_id or _next_id("prog")
    emit("progress", id=job_id, label=_clip(label, MAX_TITLE),
         value=value, total=total, done=bool(done))
    return job_id


def dismiss(event_id):
    """Remove something previously shown (a dialog, a progress row)."""
    emit("dismiss", id=event_id)
    return {"ok": True}


def _normalize_actions(actions):
    out = []
    for action in (actions or [])[:6]:
        if isinstance(action, str):
            out.append({"label": _clip(action, 40), "value": action})
        elif isinstance(action, dict):
            label = _clip(action.get("label") or action.get("value") or "", 40)
            if label:
                out.append({"label": label,
                            "value": str(action.get("value") or label),
                            "level": _normalize_level(action.get("level", "info"))})
    return out


# ---------------------------------------------------------------------------
# Blocking prompts
# ---------------------------------------------------------------------------


def _ask(kind, payload, default, timeout=None):
    """Shared round-trip for every blocking prompt.

    Web  -> JARVIS_UI_REQUEST on stdout, one line back on stdin.
    CLI  -> rendered prompt, input().
    None -> return `default` immediately.

    The headless branch is the one that matters most; see the module
    docstring. It is checked FIRST, before anything is printed, so a
    scheduled job never even emits a prompt nobody could answer.
    """
    surface = _surface()
    if surface == "headless":
        return default, "no-ui"

    payload = dict(payload)
    payload["kind"] = kind
    payload["id"] = payload.get("id") or _next_id(kind)
    payload["timeout"] = int(timeout or DEFAULT_TIMEOUT)

    if surface == "cli":
        return _ask_cli(kind, payload, default)

    try:
        print("JARVIS_UI_REQUEST " + json.dumps(payload, default=str), flush=True)
    except Exception:  # noqa: BLE001
        return default, "emit-failed"

    deadline = time.time() + payload["timeout"]
    try:
        line = sys.stdin.readline()
    except Exception:  # noqa: BLE001
        return default, "read-failed"
    if not line:
        # EOF: the parent closed our stdin (a Stop press, a killed tab).
        return default, "closed"
    if time.time() > deadline:
        return default, "timeout"

    raw = line.strip()
    if not raw:
        return default, "empty"
    # The answer is JSON when the surface has structure to send back (a form's
    # fields), and a bare string for the simple cases. Accepting both keeps
    # the wire format readable by hand during debugging.
    if raw.startswith("{"):
        try:
            return json.loads(raw).get("value", default), "answered"
        except (ValueError, TypeError):
            return default, "bad-response"
    return raw, "answered"


def confirm(question, body="", level="warn", confirm_label="Yes",
            cancel_label="No", default=False, timeout=None):
    """Blocking yes/no. Returns a bool.

    NOT a security gate — see the module docstring. This is for a tool that
    wants to check an intention ("this looks like a production database, are
    you sure?"), not for gating a dangerous tool, which tool_safety.py does
    out of band where the model can't reach it.
    """
    value, why = _ask("confirm", {
        "title": _clip(question, MAX_TITLE),
        "body": _clip(body, MAX_BODY),
        "level": _normalize_level(level),
        "confirmLabel": _clip(confirm_label, 30),
        "cancelLabel": _clip(cancel_label, 30),
        "default": bool(default),
    }, "y" if default else "n", timeout)
    if why != "answered":
        return bool(default)
    return str(value).strip().lower() in ("y", "yes", "true", "1", "ok", "confirm")


def choose(question, options, body="", default=None, level="info", timeout=None):
    """Blocking pick-one. Returns the chosen option string, or `default`."""
    opts = []
    for option in (options or [])[:MAX_OPTIONS]:
        if isinstance(option, dict):
            opts.append({"label": _clip(option.get("label") or option.get("value"), 60),
                         "value": str(option.get("value") or option.get("label"))})
        else:
            opts.append({"label": _clip(option, 60), "value": str(option)})
    if not opts:
        return default
    fallback = default if default is not None else opts[0]["value"]
    value, why = _ask("choose", {
        "title": _clip(question, MAX_TITLE),
        "body": _clip(body, MAX_BODY),
        "options": opts,
        "level": _normalize_level(level),
        "default": fallback,
    }, fallback, timeout)
    if why != "answered":
        return fallback
    valid = {o["value"] for o in opts}
    return value if value in valid else fallback


def prompt(question, body="", default="", placeholder="", secret=False,
           level="info", timeout=None):
    """Blocking free-text input. Returns the typed string, or `default`.

    `secret=True` masks the field. It does NOT make the value secret
    anywhere else — it still travels back over the same channel and is still
    visible to whatever the tool does with it, so this is shoulder-surfing
    protection, not secret management.
    """
    value, why = _ask("prompt", {
        "title": _clip(question, MAX_TITLE),
        "body": _clip(body, MAX_BODY),
        "placeholder": _clip(placeholder, 80),
        "secret": bool(secret),
        "level": _normalize_level(level),
        "default": str(default or ""),
    }, str(default or ""), timeout)
    return value if why == "answered" else str(default or "")


def form(title, fields, body="", level="info", timeout=None):
    """Blocking multi-field input. Returns {field_name: value}.

    `fields` is a list of {name, label, type, default, placeholder,
    options, required}. type is text | number | password | select |
    checkbox | textarea. Anything unrecognized renders as text, so an
    unknown type degrades to a usable field rather than a broken dialog.
    """
    normalized, defaults = [], {}
    for field in (fields or [])[:12]:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or "").strip()
        if not name:
            continue
        ftype = str(field.get("type") or "text").strip().lower()
        if ftype not in ("text", "number", "password", "select", "checkbox", "textarea"):
            ftype = "text"
        entry = {
            "name": name,
            "label": _clip(field.get("label") or name, 60),
            "type": ftype,
            "default": field.get("default", "" if ftype != "checkbox" else False),
            "placeholder": _clip(field.get("placeholder") or "", 80),
            "required": bool(field.get("required")),
        }
        if ftype == "select":
            entry["options"] = [str(o) for o in (field.get("options") or [])][:MAX_OPTIONS]
        normalized.append(entry)
        defaults[name] = entry["default"]
    if not normalized:
        return {}

    value, why = _ask("form", {
        "title": _clip(title, MAX_TITLE),
        "body": _clip(body, MAX_BODY),
        "fields": normalized,
        "level": _normalize_level(level),
        "default": defaults,
    }, defaults, timeout)
    if why != "answered":
        return defaults
    if isinstance(value, dict):
        return {k: value.get(k, defaults.get(k)) for k in defaults}
    return defaults


# ---------------------------------------------------------------------------
# Terminal rendering
# ---------------------------------------------------------------------------

_ANSI = {
    "info": "\033[36m", "success": "\033[32m",
    "warn": "\033[33m", "error": "\033[31m",
}
_RESET = "\033[0m"
_DIM = "\033[2m"


def _color(level):
    if os.environ.get("NO_COLOR"):
        return "", ""
    return _ANSI.get(level, ""), _RESET


def _print_cli(level, title, body):
    start, end = _color(level)
    mark = {"info": "\u2139", "success": "\u2713", "warn": "\u26a0", "error": "\u2717"}.get(level, "\u2022")
    try:
        print("\n%s%s %s%s" % (start, mark, title or level.upper(), end), file=sys.stderr)
        for line in str(body or "").splitlines():
            print("  " + line, file=sys.stderr)
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


def _ask_cli(kind, payload, default):
    """Terminal versions of the blocking prompts. Plain input(), no curses —
    this has to work in a Windows cmd window piped through a batch file."""
    level = payload.get("level", "info")
    _print_cli(level, payload.get("title"), payload.get("body"))
    start, end = _color(level)
    try:
        if kind == "confirm":
            suffix = "[Y/n]" if payload.get("default") else "[y/N]"
            answer = input("%s%s %s: %s" % (start, payload.get("confirmLabel", "Proceed?"),
                                            suffix, end))
            return (answer.strip() or ("y" if payload.get("default") else "n")), "answered"

        if kind == "choose":
            options = payload.get("options") or []
            for i, option in enumerate(options, start=1):
                print("  %s%d%s) %s" % (_DIM, i, _RESET, option["label"]), file=sys.stderr)
            answer = input("%sPick 1-%d [%s]: %s" % (start, len(options),
                                                     payload.get("default"), end)).strip()
            if not answer:
                return payload.get("default"), "answered"
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return options[int(answer) - 1]["value"], "answered"
            return answer, "answered"

        if kind == "prompt":
            shown = payload.get("default") or payload.get("placeholder") or ""
            hint = (" [%s]" % shown) if shown else ""
            if payload.get("secret"):
                import getpass
                answer = getpass.getpass("%s>%s " % (start, end))
            else:
                answer = input("%s>%s%s " % (start, hint, end))
            return (answer.strip() or payload.get("default") or ""), "answered"

        if kind == "form":
            out = {}
            for field in payload.get("fields") or []:
                label = field["label"]
                fdefault = field.get("default")
                hint = (" [%s]" % fdefault) if fdefault not in (None, "", False) else ""
                answer = input("%s%s%s:%s " % (start, label, hint, end)).strip()
                if not answer:
                    out[field["name"]] = fdefault
                elif field["type"] == "checkbox":
                    out[field["name"]] = answer.lower() in ("y", "yes", "true", "1")
                elif field["type"] == "number":
                    try:
                        out[field["name"]] = float(answer)
                    except ValueError:
                        out[field["name"]] = fdefault
                else:
                    out[field["name"]] = answer
            return out, "answered"
    except (EOFError, KeyboardInterrupt):
        # Ctrl-C/Ctrl-D at a prompt means "no", not "crash the tool".
        return default, "cancelled"
    except Exception:  # noqa: BLE001
        return default, "failed"
    return default, "unsupported"
