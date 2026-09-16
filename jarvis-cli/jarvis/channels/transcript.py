"""Durable per-thread conversation logs for chat channels.

WHY NOT REUSE conversations.py
------------------------------
conversations.py is Jarvis's *prompt* history: it exists to be replayed
back to a model, so it truncates aggressively (MAX_USER_CHARS=500), drops
exchanges with no reply, compresses older turns into a recap, and caps at
MAX_STORED_EXCHANGES=60. Every one of those is correct for building a
prompt and wrong for an audit log — "each conversation gets logged" means
the record should say what was actually said, including the messages that
were *denied*, which never become exchanges at all.

So this is a second, separate, append-only store. It answers "what happened
on Discord last Tuesday", not "what should the model see next turn".

The two are linked by `conv_id`: each chat thread gets a stable Jarvis
conversation id, so the model-facing history and this audit log can be
lined up after the fact without either one having to serve both jobs.

LAYOUT
------
    ~/.jarvis/channels/<platform>/<thread_id>.jsonl

One JSON object per line, append-only. JSONL rather than a single JSON
array because appending a line is atomic-enough under O_APPEND on both
platforms, while rewriting a whole array on every message would lose the
file to a crash mid-write and would get slower as the log grows.

Thread ids come from the platform and can contain characters that are not
legal in a filename (and, on Windows, names that are reserved outright), so
they are sanitized — see _safe_name.
"""

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from . import PLATFORMS

JARVIS_DIR = Path.home() / ".jarvis"
CHANNELS_DIR = JARVIS_DIR / "channels"
ENCODING = "utf-8"

# Rotate a thread's log once it passes this, so a busy channel can't grow a
# single unbounded file. The rotated copy is kept (this is an audit log —
# silently deleting it would defeat the point).
MAX_BYTES = 2_000_000

# Windows reserves these as device names regardless of extension, so a
# thread literally named "con" would produce an unopenable path.
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_name(thread_id):
    name = _UNSAFE.sub("_", str(thread_id or "unknown"))[:80] or "unknown"
    if name.lower() in _RESERVED or name.startswith("."):
        name = "_" + name
    return name


def thread_path(platform, thread_id):
    return CHANNELS_DIR / platform / f"{_safe_name(thread_id)}.jsonl"


def _rotate_if_needed(path):
    try:
        if path.exists() and path.stat().st_size > MAX_BYTES:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path.rename(path.with_suffix(f".{stamp}.jsonl"))
    except OSError:
        pass  # a failed rotate must not stop the append


def append(platform, thread_id, entry):
    """Append one record. Never raises — losing a log line must never take
    down the bot that was trying to write it."""
    try:
        path = thread_path(platform, thread_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        record = dict(entry or {})
        record.setdefault("at", datetime.now().replace(microsecond=0).isoformat())
        with path.open("a", encoding=ENCODING) as handle:
            handle.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
        return True
    except (OSError, TypeError, ValueError):
        return False


def log_inbound(platform, msg, decision, conv_id=None):
    """Record a received message and what the gate decided about it.

    Denied messages are logged too, with their stage — that is most of the
    value of this file. "Why didn't it answer me" is answerable in one
    `tail` instead of by re-reasoning about the config.
    """
    return append(platform, msg.thread_id, {
        "dir": "in",
        "context": msg.context,
        "user_id": msg.user_id,
        "user_handle": msg.user_handle,
        "text": msg.text,
        "mentioned": msg.mentioned,
        "guild_id": msg.guild_id,
        "channel_id": msg.channel_id,
        "message_id": msg.message_id,
        # `is not None`, NOT a truthiness test: Decision.__bool__ returns
        # .allowed, so a DENIED decision is falsy and `if decision` would
        # silently record it as "no decision at all" — dropping precisely
        # the denial data this log exists to capture. Caught by
        # tests/test_channels.py::test_transcript.
        "allowed": bool(decision.allowed) if decision is not None else None,
        "stage": decision.stage if decision is not None else None,
        "reason": decision.reason if decision is not None else None,
        "may_use_tools": (bool(decision.may_use_tools)
                          if decision is not None else None),
        "conv_id": conv_id,
    })


def log_outbound(platform, thread_id, text, conv_id=None, kind="reply",
                 ok=True, error="", provider=""):
    return append(platform, thread_id, {
        "dir": "out",
        "kind": kind,
        "text": text,
        "conv_id": conv_id,
        "ok": bool(ok),
        "error": error or "",
        "provider": provider or "",
    })


def read_thread(platform, thread_id, limit=200):
    """Most recent `limit` records for one thread, oldest first."""
    path = thread_path(platform, thread_id)
    try:
        lines = path.read_text(encoding=ENCODING).splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a torn final line from a crash — skip, don't fail
    return out


def list_threads(platform=None):
    """Every thread we have a log for, newest activity first."""
    platforms = [platform] if platform else list(PLATFORMS)
    out = []
    for name in platforms:
        folder = CHANNELS_DIR / name
        if not folder.is_dir():
            continue
        for path in folder.glob("*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            out.append({
                "platform": name,
                "thread_id": path.stem,
                "bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime)
                                    .replace(microsecond=0).isoformat(),
                "_mtime": stat.st_mtime,
            })
    out.sort(key=lambda item: item["_mtime"], reverse=True)
    for item in out:
        item.pop("_mtime", None)
    return out


# ---------------------------------------------------------------------------
# Thread -> conversation id mapping
# ---------------------------------------------------------------------------
# Each chat thread gets its own persistent Jarvis conversation, so a Discord
# channel keeps its context across messages the same way a browser tab does,
# and two different channels never bleed into each other. Stored in one
# small JSON file rather than derived from a hash of the thread id, because
# conversations.py assigns its own ids and we need to remember which one we
# were given.

MAP_FILE = CHANNELS_DIR / "threads.json"


def _load_map():
    try:
        data = json.loads(MAP_FILE.read_text(encoding=ENCODING))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {}


def conv_id_for(platform, thread_id, create=None):
    """Stable Jarvis conversation id for a chat thread.

    `create` is a zero-arg callable returning a fresh conversation id; it is
    only invoked when this thread has never been seen. Passed in rather than
    imported so this module stays free of a conversations.py dependency and
    stays trivially testable.
    """
    key = f"{platform}:{thread_id}"
    mapping = _load_map()
    existing = mapping.get(key)
    if existing:
        return existing
    if create is None:
        return None
    conv_id = create()
    if not conv_id:
        return None
    mapping[key] = conv_id
    try:
        MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = MAP_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(mapping, indent=2) + "\n", encoding=ENCODING)
        os.replace(str(tmp), str(MAP_FILE))
    except OSError:
        pass  # mapping is a cache; losing it costs context, not correctness
    return conv_id


# ---------------------------------------------------------------------------
# Cooldown bookkeeping
# ---------------------------------------------------------------------------
# permissions.decide() takes `last_seen_at` as a parameter rather than
# reading it, so the state lives here. In-process only: a gateway is a
# long-running process (unlike `jarvis ask`), so a dict is genuinely enough
# and a disk round trip per message would not buy anything.

_last_seen = {}


def note_accepted(platform, user_id, when=None):
    _last_seen[f"{platform}:{user_id}"] = time.time() if when is None else when


def last_accepted(platform, user_id):
    return _last_seen.get(f"{platform}:{user_id}")


def reset_cooldowns():
    """Test hook — module-level state would otherwise leak between cases."""
    _last_seen.clear()
