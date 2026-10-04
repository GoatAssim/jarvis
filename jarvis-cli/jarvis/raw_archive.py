"""Master plan L.38 -- the raw event log: what actually happened, stored as it
happened, so any view of a conversation can be rebuilt from it later.

THE PROBLEM THIS SOLVES
-----------------------
conversations.py and console_store.py store a *derived* record of a turn, not
the turn: the saved reply is the model's text after `_split_console_dump`
pulled lines out of it, thinking is clipped by `reasoning.clip_trace`, tool
results become small "extras" (a screenshot's filename, the first five
downloads), console lines are cut at 4,000 characters, and only the newest 60
exchanges are kept. Each of those was a sensible choice for the UI of the day,
and each one means a later UI -- or the same UI, asked to "show it again
correctly" -- can only show what that day's derivation kept.

THE MODEL
---------
One append-only JSON-Lines file per conversation,
``~/.jarvis/events/<conv_id>.jsonl``. It stores the RAW EVENTS at the moment
they occur, with no clipping, no capping, no splitting and no summarising.
Every other store -- the conversation file, the console store, the extras the
web UI replays -- is a VIEW derived from these events and can be rebuilt from
them (``rebuild_exchanges``, ``rebuild_console``). A new UI feature is a new
derivation over the same events; the storage never has to change again, which
is why event ``data`` is open-schema and each line carries a format version.

EVENTS (each line: ``v``, ``ts``, ``kind``, plus the fields below)
    user          text            exactly what was typed/sent, byte for byte
    model_reply   text, provider, cut, thinking, interim, trace
                                  the model's output BEFORE any splitting,
                                  the FULL thinking text (not clipped),
                                  every interim narration item
    console       seq, turn, surface, console_kind, tool, provider, stream,
                  text            every console line at full length
    tool_run      name, arguments, result, confirm
                                  the FULL result before shaping for the model
    reply         user (only if no `user` event came first), text, provider,
                  status, reason, extras
                                  what the conversation file was given: the
                                  saved exchange (status answered/interrupted)
    exchange_removed  reason, exchanges
                                  what a redo took out of the conversation

The conversation file and the console store keep serving the web UI exactly
as before (60-exchange cap, 4,000-char console clip, shaped model context),
so nothing in the UI can regress. What changes is that they are no longer the
only record.

REPLAY
------
``console_store.read(full=True)`` restores a clipped line's full text from
this log by ``seq`` (``console-read --full`` on the CLI). It is opt-in so the
replay the web UI gets today is byte-for-byte unchanged until the UI asks. ``conv-export --raw`` writes the whole event stream.
``rebuild_exchanges`` reproduces the conversation's exchanges -- all of them,
past the 60-cap -- from the events alone; the test suite checks that against
the live conversation file, which is the proof the log is sufficient.

LIFETIME AND SAFETY
-------------------
Deleting a conversation, or the web UI's Clear button, deletes its event log:
the owner asking for a conversation to go away must not leave a hidden copy.
Clearing the Logs viewer or the Live Feed never touches it. Every public
function swallows OSError and returns a harmless value -- logging must never
break an ask (same contract as logs.log and console_store.append).
``JARVIS_RAW_ARCHIVE=0`` turns writing off. The log is unbounded by design;
``stats()`` reports its size.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import conversations

JARVIS_DIR = Path.home() / ".jarvis"
ARCHIVE_DIR = JARVIS_DIR / "events"
ENCODING = "utf-8"
FORMAT_VERSION = 2

KINDS = ("user", "model_reply", "console", "tool_run", "reply", "exchange_removed")

_OFF_VALUES = {"0", "off", "false", "no"}


def enabled():
    """Writing is on unless JARVIS_RAW_ARCHIVE is 0/off/false/no."""
    return os.environ.get("JARVIS_RAW_ARCHIVE", "").strip().lower() not in _OFF_VALUES


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _path(conv_id):
    return ARCHIVE_DIR / f"{conv_id}.jsonl"


def record(conv_id, kind, data=None):
    """Append one event. Returns True if it was written. Never raises."""
    if not enabled() or not conversations.is_valid_id(conv_id):
        return False
    entry = {"v": FORMAT_VERSION, "ts": _now_iso(), "kind": kind}
    if data:
        entry.update(data)
    try:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        with _path(conv_id).open("a", encoding=ENCODING) as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return True
    except (OSError, TypeError, ValueError) as e:
        print(f"Warning: couldn't write raw event log for {conv_id}: {e}", file=sys.stderr)
        return False


def read(conv_id, kinds=None):
    """All events, oldest first, optionally only some kinds. A half-written
    last line (crash mid-append) is skipped, not fatal."""
    if not conversations.is_valid_id(conv_id):
        return []
    path = _path(conv_id)
    if not path.exists():
        return []
    wanted = set(kinds) if kinds else None
    out = []
    try:
        with path.open("r", encoding=ENCODING) as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(entry, dict):
                    continue
                if wanted is not None and entry.get("kind") not in wanted:
                    continue
                out.append(entry)
    except OSError:
        return []
    return out


def stats(conv_id):
    """{"entries": int, "bytes": int} -- zeros when there is no log."""
    if not conversations.is_valid_id(conv_id):
        return {"entries": 0, "bytes": 0}
    try:
        size = _path(conv_id).stat().st_size
    except OSError:
        return {"entries": 0, "bytes": 0}
    return {"entries": len(read(conv_id)), "bytes": size}


def delete(conv_id):
    """Remove a conversation's event log. True if there is none left."""
    if not conversations.is_valid_id(conv_id):
        return False
    try:
        _path(conv_id).unlink(missing_ok=True)
        return True
    except OSError:
        return False


# ---- derived views ----------------------------------------------------------

def console_full_text(conv_id):
    """{seq: full text} for every console line in the log. Used to restore
    clipped lines when the console store is replayed."""
    return {e["seq"]: e.get("text", "")
            for e in read(conv_id, kinds=("console",)) if e.get("seq") is not None}


def rebuild_console(conv_id):
    """Every console line ever written, full text, in order -- including the
    lines the console store's size cap rotated out of its own file. Lines are
    in the console store's own shape (``kind`` is the console kind)."""
    lines = []
    for e in read(conv_id, kinds=("console",)):
        lines.append({
            "seq": e.get("seq"), "ts": e.get("console_ts") or e.get("ts"),
            "turn": e.get("turn"), "surface": e.get("surface"),
            "kind": e.get("console_kind"), "tool": e.get("tool"),
            "provider": e.get("provider"), "stream": e.get("stream"),
            "text": e.get("text", ""),
        })
    return lines


def rebuild_exchanges(conv_id):
    """The conversation's exchanges, rebuilt from events alone -- every one,
    not just the newest 60. Same shape as the conversation file's exchanges
    (``ts``, ``user``, ``jarvis``, ``provider``, optional ``extras`` /
    ``interrupted``). A redo removes the exchanges the live file removed.
    """
    exchanges = []
    open_user = None   # (ts, text) of a `user` event with no `reply` yet
    for e in read(conv_id, kinds=("user", "reply", "exchange_removed")):
        kind = e["kind"]
        if kind == "user":
            if open_user is not None:
                # the previous turn never resolved (hard kill): the live file
                # downgrades it to `interrupted` on the next begin -- same here
                exchanges.append({"ts": open_user[0], "user": open_user[1], "jarvis": "",
                                  "provider": None, "interrupted": "interrupted"})
            open_user = (e.get("ts"), e.get("text", ""))
        elif kind == "reply":
            if "user" in e:
                user_text = e.get("user", "")
            elif open_user is not None:
                user_text = open_user[1]
            else:
                user_text = ""
            open_user = None
            ex = {"ts": e.get("ts"), "user": user_text, "jarvis": e.get("text", ""),
                  "provider": e.get("provider")}
            if e.get("status") == "interrupted":
                ex["interrupted"] = e.get("reason") or "interrupted"
            if e.get("extras"):
                ex["extras"] = e["extras"]
            exchanges.append(ex)
        elif kind == "exchange_removed":
            n = len(e.get("exchanges") or [])
            if n:
                exchanges = exchanges[:-n] if n <= len(exchanges) else []
    if open_user is not None:
        # a turn that began but never resolved: keep what the person said
        exchanges.append({"ts": open_user[0], "user": open_user[1], "jarvis": "",
                          "provider": None, "pending": True})
    return exchanges


def export_records(conv_id):
    """The whole event stream, oldest first, preceded by one ``meta`` line
    that carries per-kind counts. This is what ``conv-export --raw`` writes."""
    record_ = conversations.get_conversation(conv_id) or {}
    events = read(conv_id)
    counts = {}
    for e in events:
        counts[e.get("kind")] = counts.get(e.get("kind"), 0) + 1
    meta = {"v": FORMAT_VERSION, "ts": _now_iso(), "kind": "meta",
            "conversation": conv_id, "title": record_.get("title"),
            "created_at": record_.get("created_at"), "counts": counts}
    return [meta] + events
