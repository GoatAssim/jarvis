"""Who changed whose access, and when -- the permission change log (L.36-P15).

WHY THIS EXISTS
---------------
Every switch in the Channels panel, every quick setup, every bulk edit and
every `jarvis channels-allow / -deny / -follow / -user ...` ends in one of a
few small writers. Each of them leaves only the CURRENT state behind. After a
bulk edit has touched twenty people there is no record of what the state was,
who changed it, or when. This file is that record.

    ~/.jarvis/channels/changes.jsonl     append-only, one JSON object per change

WHERE THE LINES COME FROM -- THE LOWEST LAYER, NOT THE PANEL
------------------------------------------------------------
The lines are written by the writers themselves, so a change is recorded
whichever way it was made:

    config.add_to_set / remove_from_set   dm / reply / tool lists ("list")
    config.set_value                       `owner` and `allow_tools` only
    user_perms.set_tools / set_can_dm      tool scope, "Jarvis may DM them"
    people.set_follow                      approved / blocked / unknown
    user_admin.set_handle / forget_person  a handle edit, a wipe
    user_admin.grant_tools_for             "tools until <time>" (kind timed);
                                           the list flip itself is the usual
                                           `list` line, and the one written
                                           when the time runs out carries
                                           why="time limit ended"

A line is written only when something ACTUALLY changed (re-adding someone who
is already listed writes nothing), and only after the change itself was saved,
so the log never claims a flip that did not happen.

WHAT A LINE HOLDS -- AND WHAT IT DOES NOT
-----------------------------------------
When, which platform, what kind of change, whose id or handle, the new state,
the old state where it is cheap, tool NAMES (at most MAX_TOOLS_SHOWN of them,
plus a count), where it came from (`via`), and an optional `why`. Never a
message, a note, a token, a config value other than the two named above, or a
tool's arguments. It can be shown, kept or deleted without being a copy of
anything else.

`via` is "panel" when the change came through the web panel (server.js sets
JARVIS_CHANGE_SOURCE=panel for every `channels-*` command it runs) and
"terminal" otherwise. It records the route, not a login: Jarvis has one owner
and no accounts. `why` is set by `reason(...)` for batch routes -- a quick
setup or a bulk edit -- so twenty lines from one click read as one action.

NEVER RAISES, NEVER BLOCKS
--------------------------
Every function that writes returns True/False and swallows its errors. Losing
a log line must never be the reason a permission change fails -- the change is
what protects the owner; the log only explains it afterwards.

HISTORY BEGINS WHEN THIS FILE DOES
----------------------------------
Changes made before this module existed were never recorded and are not
guessed at. The view says when the log starts.

ROTATION
--------
Rotated like usage.py and transcript.py: renamed, never deleted.
"""

import json
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime

from .directory import CHANNELS_DIR

ENCODING = "utf-8"
LOG = CHANNELS_DIR / "changes.jsonl"
MAX_BYTES = 1_000_000
_ROTATED = re.compile(r"^changes\.\d{8}-\d{6}\.jsonl$")
SOURCE_ENV = "JARVIS_CHANGE_SOURCE"
MAX_TOOLS_SHOWN = 30
MAX_LIMIT = 500

# Kinds of line. A short closed set, so the view can word every one of them.
K_LIST = "list"          # in / out of the dm, reply or tool list
K_TOOLS = "tools"        # which tools one person may use
K_SEND_DM = "send_dm"    # may Jarvis DM them
K_FOLLOW = "follow"      # approved / blocked / unknown
K_OWNER = "owner"        # became / stopped being the platform's owner
K_PLATFORM = "platform"  # a platform-wide switch (allow_tools)
K_HANDLE = "handle"      # the owner edited a hand-added person's handle
K_FORGOT = "forgot"      # "Forget this person" ran
K_TIMED = "timed"        # tool use switched on until a deadline (L.36-P6)

_LIST_LABEL = {"dm_allowlist": "direct messages", "reply_allowlist": "replies",
               "tool_allowlist": "tool use", "image_allowlist": "pictures",
               "allowed_guilds": "the allowed-servers filter",
               "allowed_channels": "the allowed-channels filter"}

_state = threading.local()


def _now():
    return datetime.now().replace(microsecond=0)


def source():
    return "panel" if os.environ.get(SOURCE_ENV, "").strip().lower() == "panel" else "terminal"


@contextmanager
def reason(text):
    """Tag every line written inside the block with `why` (a quick setup, a
    bulk edit). Nests: the outermost reason wins, so a bulk edit that applies
    a quick setup reads as the bulk edit."""
    previous = getattr(_state, "why", "")
    if not previous:
        _state.why = str(text or "")[:120]
    try:
        yield
    finally:
        _state.why = previous


def _rotate_if_needed():
    try:
        if LOG.exists() and LOG.stat().st_size > MAX_BYTES:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            LOG.rename(LOG.with_name(f"changes.{stamp}.jsonl"))
    except OSError:
        pass  # a failed rotate must not stop the append


def _norm(ident):
    text = str(ident or "").strip()
    return (text[1:] if text.startswith("@") else text).lower()


def record(platform, kind, ident="", **fields):
    """Append one change. Returns True if written; never raises."""
    try:
        entry = {"at": _now().isoformat(), "platform": str(platform),
                 "kind": str(kind), "ident": _norm(ident),
                 "via": source()}
        why = getattr(_state, "why", "")
        if why:
            entry["why"] = why
        for name, value in fields.items():
            if value is None:
                continue
            if name == "tools":
                names = [str(n)[:64] for n in value]
                entry["tool_count"] = len(names)
                entry["tools"] = names[:MAX_TOOLS_SHOWN]
            elif isinstance(value, bool):
                entry[name] = value
            else:
                entry[name] = str(value)[:120]
        LOG.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed()
        with LOG.open("a", encoding=ENCODING) as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except (OSError, TypeError, ValueError):
        return False


def _files():
    """Rotated copies oldest first, live file last."""
    folder = LOG.parent
    if not folder.is_dir():
        return []
    rotated = sorted(p for p in folder.glob("changes.*.jsonl")
                     if _ROTATED.match(p.name))
    return rotated + ([LOG] if LOG.exists() else [])


def read_all():
    """Every readable line, oldest first. A torn or hand-edited line is
    skipped, never fatal."""
    out = []
    for path in _files():
        try:
            text = path.read_text(encoding=ENCODING)
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and entry.get("at") and entry.get("kind"):
                out.append(entry)
    return out


def started():
    """When the oldest readable line was written, or ''."""
    for entry in read_all():
        return str(entry.get("at") or "")
    return ""


def _earlier_names(entries, idents):
    """Handles a person had BEFORE an edit: a `handle` line names the new
    ident and keeps the old one, so history survives the edit. Followed to a
    fixed point (an edit of an edit), which is a handful of passes at most."""
    known = set(idents)
    changed = True
    while changed:
        changed = False
        for e in entries:
            if e.get("kind") == K_HANDLE and e.get("ident") in known:
                old = _norm(e.get("old"))
                if old and old not in known:
                    known.add(old)
                    changed = True
    return known


def _is_everyone(entry):
    """A line that changed things for everyone on the platform: a `*` entry
    in a list, or a platform-wide switch."""
    return entry.get("kind") == K_PLATFORM or (
        entry.get("kind") == K_LIST and entry.get("ident") == "*")


def describe(entry):
    """One plain sentence for a line. Everything interpolated is a name from
    the log, shown by the panel as text, never as markup."""
    kind = entry.get("kind")
    on = entry.get("on")
    if kind == K_LIST:
        label = _LIST_LABEL.get(entry.get("list"), str(entry.get("list") or "a list"))
        if entry.get("ident") == "*":
            return f"Everyone (*) {'added to' if on else 'removed from'} {label}"
        return f"{label.capitalize()} switched {'on' if on else 'off'}"
    if kind == K_TOOLS:
        if entry.get("mode") == "custom":
            n = int(entry.get("tool_count") or 0)
            names = ", ".join(entry.get("tools") or [])
            more = n - len(entry.get("tools") or [])
            tail = f" (+{more} more)" if more > 0 else ""
            return (f"Tools limited to {n}: {names}{tail}" if n
                    else "Tools limited to none")
        return "Tools no longer limited to a list (the platform's setting applies)"
    if kind == K_SEND_DM:
        return f"Jarvis {'may' if on else 'may not'} DM them for you"
    if kind == K_FOLLOW:
        state = entry.get("state") or "?"
        before = entry.get("old")
        return (f"Follow status {before} → {state}" if before
                else f"Follow status set to {state}")
    if kind == K_OWNER:
        return "Became the owner" if on else "Stopped being the owner"
    if kind == K_PLATFORM:
        key = entry.get("key") or "a platform switch"
        if key == "allow_tools":
            return ("Tool use for everyone the lists allow switched "
                    + ("on" if on else "off"))
        return f"{key} switched {'on' if on else 'off'}"
    if kind == K_HANDLE:
        if entry.get("old"):
            return f"Handle changed from @{entry['old']} to @{entry.get('ident') or '?'}"
        return f"Handle set to @{entry.get('ident') or '?'}"
    if kind == K_FORGOT:
        return "Forgotten (access, limits and record wiped)"
    if kind == K_TIMED:
        until = str(entry.get("until") or "").replace("T", " ")
        return (f"Tool use switched on until {until}" if until
                else "Tool use switched on for a limited time")
    return str(kind)


def for_person(platform, idents, limit=100):
    """Newest `limit` lines about this person (by any of their ids or
    handles, past ones included) plus the lines that touched everyone on the
    platform, oldest first. Returns (lines, total_matching)."""
    entries = [e for e in read_all() if e.get("platform") == platform]
    names = _earlier_names(entries, {_norm(i) for i in idents if i})
    mine = []
    for e in entries:
        if e.get("ident") in names or _is_everyone(e):
            mine.append(e)
    try:
        limit = max(1, min(MAX_LIMIT, int(limit)))
    except (TypeError, ValueError):
        limit = 100
    total = len(mine)
    shown = []
    for e in mine[-limit:]:
        row = dict(e)
        row["text"] = describe(e)
        row["everyone"] = _is_everyone(e)
        shown.append(row)
    return shown, total
