"""Conversation-scoped logs of the raw traffic between the model and the
Jarvis backend: every HTTP request Jarvis sends to a provider, every
response it gets back, and every tool call Jarvis runs in between.

Storage mirrors conversations.py on purpose — one append-only file per
conversation, under ``~/.jarvis/logs/<conv_id>.jsonl`` — so logs persist
across restarts and are naturally "conversation based": deleting a
conversation's log never touches another conversation's, and there's
nothing to migrate if conversations.py's own format changes.

JSON Lines (one compact JSON object per line) rather than one big JSON
array, so appending never requires reading/rewriting the whole file and a
crash mid-write only ever corrupts the last, incomplete line.
"""

import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import conversations

JARVIS_DIR = Path.home() / ".jarvis"
LOG_DIR = JARVIS_DIR / "logs"
ENCODING = "utf-8"

MAX_LINE_CHARS = 20000  # a single runaway payload (huge tool result, etc.)
                        # shouldn't be able to blow up a log file or the UI
                        # rendering it; truncate defensively, note that we did.

_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _log_path(conv_id):
    return LOG_DIR / f"{conv_id}.jsonl"


def _safe_json(obj):
    try:
        text = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = json.dumps(str(obj), ensure_ascii=False)
    if len(text) > MAX_LINE_CHARS:
        text = text[:MAX_LINE_CHARS] + "...(truncated)"
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"truncated": True, "preview": text}
    return obj


def log(conv_id, direction, data, provider=None, round_num=None):
    """Append one entry to conv_id's log. Never raises — a logging failure
    must never break an actual ask.

    direction: one of "request" | "response" | "tool_call" | "tool_result"
               | "error" | "info" — free-form label shown in the UI.
    data: any JSON-serializable payload (the raw request body, the raw
          response body, tool name+arguments, etc).
    """
    if not conversations.is_valid_id(conv_id):
        return
    entry = {
        "ts": _now(),
        "direction": direction,
        "provider": provider,
        "round": round_num,
        "data": _safe_json(data),
    }
    line = json.dumps(entry, ensure_ascii=False)
    try:
        with _lock:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with _log_path(conv_id).open("a", encoding=ENCODING) as f:
                f.write(line + "\n")
    except OSError as e:
        print(f"Warning: couldn't write log for {conv_id}: {e}", file=sys.stderr)


def has_log(conv_id):
    return _log_path(conv_id).exists()


def list_logged_conversations():
    """Every conversation that has at least one log entry, newest first,
    enriched with title/updated_at from conversations.py's own index so the
    UI can show something meaningful instead of a bare id. Includes
    conversations no longer in the index (e.g. deleted) under their raw id,
    so their logs are still reachable rather than orphaned."""
    if not LOG_DIR.exists():
        return []
    index_by_id = {it["id"]: it for it in conversations.list_conversations()}
    items = []
    for path in LOG_DIR.glob("*.jsonl"):
        conv_id = path.stem
        if not conversations.is_valid_id(conv_id):
            continue
        meta = index_by_id.get(conv_id)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0
        items.append({
            "id": conv_id,
            "title": (meta or {}).get("title") or "(deleted conversation)",
            "updated_at": (meta or {}).get("updated_at"),
            "exists": meta is not None,
            "_mtime": mtime,
        })
    items.sort(key=lambda it: it["_mtime"], reverse=True)
    for it in items:
        it.pop("_mtime", None)
    return items


def read_entries(conv_id, limit=None):
    """All log entries for conv_id, oldest first. limit, if given, returns
    only the last `limit` entries (still oldest-first order)."""
    path = _log_path(conv_id)
    if not path.exists():
        return []
    entries = []
    try:
        with path.open("r", encoding=ENCODING) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    if limit:
        entries = entries[-limit:]
    return entries


def clear(conv_id):
    if not conversations.is_valid_id(conv_id):
        return False
    try:
        _log_path(conv_id).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True
