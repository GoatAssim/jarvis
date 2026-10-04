"""Master plan L.38 (a) -- the raw archive: nothing the other stores cap, trim
or drop is ever lost.

WHY THIS EXISTS
---------------
Three stores each throw something away on purpose, for good reasons (a
readable UI, a bounded prompt, a bounded file):

    conversations.py   keeps the newest MAX_STORED_EXCHANGES (60) exchanges;
                       older ones, and ones removed by a redo, are dropped.
    console_store.py   clips every line to MAX_LINE_CHARS (4000) and trims the
                       oldest lines once the file passes MAX_FILE_BYTES.
    ai_client.py       gives the MODEL a shaped copy of each tool result
                       (tool_result_shaping), not the full one.

The owner asked for conversation and console saving to keep "everything -- the
raw input and all" instead of a reduced version. This module is the answer to
(a) of Q-L38: a COMPLETE stored record. It deliberately does NOT change what
the model is shown (that is (b) in Q-L38, which multiplies the tokens of every
ask) and it does not change what the web UI replays: the capped stores stay as
they were, and this file holds what they would otherwise lose.

WHAT IS IN IT
-------------
One append-only JSON-Lines file per conversation,
``~/.jarvis/archive/<conv_id>.jsonl``. No size cap, no rotation, no per-line
clip. Every line carries ``v`` (format version), ``ts`` (UTC) and ``kind``:

    exchange_overflow  exchanges conversations.py dropped to stay at 60
                       (``exchanges``: the full dicts, extras included)
    exchange_dropped   exchanges removed by a redo (`drop_from_user`)
    console_full       the FULL text of a console line that was clipped
                       (``seq`` ties it to the clipped line in the console store)
    console_trimmed    a console line rotated out by the size cap (``line``)
    tool_run           one tool call: name, arguments, the FULL pre-shaping
                       result, and the confirmation record if there was one

Nothing is duplicated: a console line that was not clipped or trimmed lives
only in the console store, an exchange that is still inside the 60 lives only
in the conversation file. ``export_records()`` stitches the three back
together into one chronological list -- that is what ``conv-export --raw``
writes.

LIFETIME
--------
Deleting a conversation, or the web UI's Clear button, deletes its archive
too: those are the owner asking for the conversation to go away, and a hidden
copy that survives them would be the wrong kind of "complete". Clearing the
Logs viewer or the Live Feed never touches it.

SAFETY
------
Every public function swallows OSError and returns a harmless value -- an
archive failure must never break an ask (same contract as logs.log and
console_store.append). Set ``JARVIS_RAW_ARCHIVE=0`` to switch writing off;
reading, exporting and deleting keep working. The archive grows without bound
by design; ``stats()`` reports its size.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import conversations

JARVIS_DIR = Path.home() / ".jarvis"
ARCHIVE_DIR = JARVIS_DIR / "archive"
ENCODING = "utf-8"
FORMAT_VERSION = 1

KINDS = (
    "exchange_overflow", "exchange_dropped", "console_full",
    "console_trimmed", "tool_run",
)

_OFF_VALUES = {"0", "off", "false", "no"}


def enabled():
    """Writing is on unless JARVIS_RAW_ARCHIVE is 0/off/false/no."""
    return os.environ.get("JARVIS_RAW_ARCHIVE", "").strip().lower() not in _OFF_VALUES


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _path(conv_id):
    return ARCHIVE_DIR / f"{conv_id}.jsonl"


def record(conv_id, kind, data=None):
    """Append one entry. Returns True if it was written. Never raises."""
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
        print(f"Warning: couldn't write raw archive for {conv_id}: {e}", file=sys.stderr)
        return False


def read(conv_id, kinds=None):
    """All entries, oldest first, optionally restricted to some kinds. A
    half-written last line (crash mid-append) is skipped, not fatal."""
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
    """{"entries": int, "bytes": int} -- zeros when there is no archive."""
    if not conversations.is_valid_id(conv_id):
        return {"entries": 0, "bytes": 0}
    path = _path(conv_id)
    try:
        size = path.stat().st_size
    except OSError:
        return {"entries": 0, "bytes": 0}
    return {"entries": len(read(conv_id)), "bytes": size}


def delete(conv_id):
    """Remove a conversation's archive. True if there is none left."""
    if not conversations.is_valid_id(conv_id):
        return False
    try:
        _path(conv_id).unlink(missing_ok=True)
        return True
    except OSError:
        return False


# ---- export -----------------------------------------------------------------

def _console_records(conv_id):
    """The console store's lines with every clipped one restored to its full
    text, plus the lines the size cap rotated out (from the archive)."""
    from . import console_store
    full_by_seq = {}
    trimmed = []
    for entry in read(conv_id, kinds=("console_full", "console_trimmed")):
        if entry["kind"] == "console_full" and entry.get("seq") is not None:
            full_by_seq[entry["seq"]] = entry.get("text", "")
        elif entry["kind"] == "console_trimmed" and isinstance(entry.get("line"), dict):
            trimmed.append(entry["line"])
    lines = []
    seen = set()
    for line in trimmed + console_store._read_raw_lines(conv_id):
        seq = line.get("seq")
        if seq in seen and seq is not None:
            continue
        seen.add(seq)
        line = dict(line)
        if seq in full_by_seq:
            line["text"] = full_by_seq[seq]
            line["restored_from_archive"] = True
        lines.append(line)
    return lines


def export_records(conv_id):
    """One chronological list of every record this conversation has: the
    conversation's exchanges (archived overflow first), every console line at
    full length, and every tool run. Each item is ``{"ts", "kind", ...}``.
    ``kind`` here is one of ``meta``, ``exchange``, ``console``, ``tool_run``.
    """
    record_ = conversations.get_conversation(conv_id) or {}
    items = []

    for entry in read(conv_id, kinds=("exchange_overflow", "exchange_dropped")):
        for ex in entry.get("exchanges") or []:
            if isinstance(ex, dict):
                items.append({"ts": ex.get("ts") or entry.get("ts"), "kind": "exchange",
                              "source": entry["kind"], "reason": entry.get("reason"),
                              "exchange": ex})
    for ex in record_.get("exchanges") or []:
        items.append({"ts": ex.get("ts"), "kind": "exchange", "source": "conversation",
                      "exchange": ex})
    for line in _console_records(conv_id):
        items.append({"ts": line.get("ts"), "kind": "console", "line": line})
    for entry in read(conv_id, kinds=("tool_run",)):
        items.append({"ts": entry.get("ts"), "kind": "tool_run",
                      "name": entry.get("name"), "arguments": entry.get("arguments"),
                      "result": entry.get("result"), "confirm": entry.get("confirm")})

    # Stable sort: equal timestamps keep their insertion order. Entries with
    # no timestamp sort first rather than being lost.
    items.sort(key=lambda it: str(it.get("ts") or ""))
    meta = {
        "ts": _now_iso(), "kind": "meta", "conversation": conv_id,
        "title": record_.get("title"), "created_at": record_.get("created_at"),
        "format": FORMAT_VERSION, "counts": {},
    }
    for it in items:
        meta["counts"][it["kind"]] = meta["counts"].get(it["kind"], 0) + 1
    return [meta] + items
