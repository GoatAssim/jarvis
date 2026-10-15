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

import glob
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


def _room_fields(msg):
    """The optional room-context fields of an inbound line, each present ONLY
    when set, so every older line and every DM line keeps exactly its old
    shape: `space` (the kind, rooms only -- a DM is already `context: dm`),
    `author_name`, `thread_name`, `reply_to` and `attachments` (a count).
    Display data only: nothing in the gate reads any of it. Never raises: a
    field that cannot be built is left out, the line is still written."""
    out = {}
    try:
        from . import space
        kind = space.kind_of(msg)
        if kind != space.KIND_DM:
            out["space"] = kind
        author = space._clean(getattr(msg, "author_name", ""), space.MAX_NAME_CHARS)
        if author:
            out["author_name"] = author
        thread_name = space._clean(getattr(msg, "thread_name", ""), 100)
        if thread_name:
            out["thread_name"] = thread_name
        reply = getattr(msg, "reply_to", None)
        if isinstance(reply, dict):
            clipped = {
                "message_id": space._clean(reply.get("message_id"), space.MAX_ID_CHARS),
                "user_id": space._clean(reply.get("user_id"), space.MAX_ID_CHARS),
                "user_name": space._clean(reply.get("user_name"), space.MAX_NAME_CHARS),
                "snippet": space._clean(reply.get("snippet"), 120),
            }
            if any(clipped.values()):
                out["reply_to"] = clipped
        count = len(getattr(msg, "attachments", None) or ())
        if count:
            out["attachments"] = count
    except Exception:  # noqa: BLE001
        pass
    return out


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
        **({"guild_name": msg.guild_name} if getattr(msg, "guild_name", "") else {}),
        **({"channel_name": msg.channel_name} if getattr(msg, "channel_name", "") else {}),
        **_room_fields(msg),
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
                 ok=True, error="", provider="", to_user=None, tool_calls=None):
    """Record one reply. `to_user` is the stable id of the person it answers.

    It exists because a group thread mixes people and an outbound line used to
    say nothing about who it was for, so the per-person Conversation view
    (L.36-P1) could attribute replies in a DM thread but never in a group.
    Written only when given, so older lines and callers are unchanged."""
    record = {
        "dir": "out",
        "kind": kind,
        "text": text,
        "conv_id": conv_id,
        "ok": bool(ok),
        "error": error or "",
        "provider": provider or "",
    }
    if to_user:
        record["to_user"] = str(to_user)
    calls = clean_tool_calls(tool_calls)
    if calls:
        record["tool_calls"] = calls
    return append(platform, thread_id, record)


# ---------------------------------------------------------------------------
# Tool calls shown in the Conversation view
# ---------------------------------------------------------------------------
# A reply may have been produced by running tools. The owner wants to see which,
# with what, and how it went. This is the AUDIT log (it already holds every
# message verbatim), so it may hold a clipped view of a call -- unlike
# channels/usage.py, which is written even with log_conversations off and
# therefore holds counts and tool names only. Rules for what lands here:
#   * only when log_conversations is on (base.handle_message decides);
#   * arguments and results are SCRUBBED of secret-looking keys, then clipped
#     hard -- this is a glance, not an archive (raw_archive.py keeps the full
#     run per conversation);
#   * the record rides on the reply it produced (`tool_calls` on a dir="out"
#     line), so it is attributed to the same person as that reply.

MAX_TOOL_CALLS = 25            # per reply
MAX_TOOL_ARG_CHARS = 300
MAX_TOOL_RESULT_CHARS = 500
_SECRET_KEY = re.compile(
    r"(token|secret|passw(or)?d|passwd|api[_-]?key|authorization|cookie|credential|private[_-]?key)",
    re.IGNORECASE)


def _scrub(value, depth=0):
    """Copy of `value` with the contents of secret-looking keys hidden."""
    if depth > 4:
        return "..."
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:40]:
            out[str(key)] = ("(hidden)" if _SECRET_KEY.search(str(key))
                             else _scrub(item, depth + 1))
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub(item, depth + 1) for item in list(value)[:20]]
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000]
    return value


def _compact(value, limit):
    """(text, clipped) -- one-line JSON (or plain text) of `value`, clipped."""
    if value is None:
        return "", False
    try:
        text = value if isinstance(value, str) else json.dumps(
            _scrub(value), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[:limit].rstrip() + "\u2026", True
    return text, False


def tool_call_record(name, arguments=None, result=None, confirm=None):
    """One tool run as the Conversation view shows it. Pure; never raises.

    `ok` is None when no result was captured (the call is known, its outcome
    is not)."""
    try:
        args_text, args_clipped = _compact(arguments, MAX_TOOL_ARG_CHARS)
        ok, res_text, res_clipped = None, "", False
        if isinstance(result, dict):
            ok = result.get("ok") is not False and not result.get("cancelled")
            shown = result.get("error") if not ok and result.get("error") else result
            res_text, res_clipped = _compact(shown, MAX_TOOL_RESULT_CHARS)
        elif result is not None:
            ok = True
            res_text, res_clipped = _compact(result, MAX_TOOL_RESULT_CHARS)
        record = {"name": str(name or "?")[:64], "args": args_text,
                  "ok": ok, "result": res_text,
                  "clipped": bool(args_clipped or res_clipped)}
        if isinstance(confirm, dict):
            record["confirm"] = "approved" if confirm.get("approved") else "declined"
        return record
    except Exception:  # noqa: BLE001 -- a log line must never break a reply
        return {"name": str(name or "?")[:64], "args": "", "ok": None,
                "result": "", "clipped": False}


def clean_tool_calls(calls):
    """Whatever is stored or passed in -> a bounded list of well-formed
    records. Applied on write AND on read, so a hand-edited or older line can
    never hand the panel a wrong type or an unbounded string."""
    out = []
    if not isinstance(calls, (list, tuple)):
        return out
    for call in list(calls)[:MAX_TOOL_CALLS]:
        if not isinstance(call, dict):
            continue
        ok = call.get("ok")
        record = {
            "name": str(call.get("name") or "?")[:64],
            "args": str(call.get("args") or "")[:MAX_TOOL_ARG_CHARS + 1],
            "ok": ok if isinstance(ok, bool) else None,
            "result": str(call.get("result") or "")[:MAX_TOOL_RESULT_CHARS + 1],
            "clipped": bool(call.get("clipped")),
        }
        if call.get("confirm") in ("approved", "declined"):
            record["confirm"] = call["confirm"]
        out.append(record)
    return out


def _parse_lines(lines):
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn final line from a crash — skip, don't fail
        if isinstance(record, dict):
            out.append(record)
    return out


def _thread_files(platform, thread_id):
    """[rotated copies oldest first, live file last] for ONE thread, only
    the ones that exist. Same grouping rule as _thread_groups, but without
    listing the whole platform folder."""
    safe = _safe_name(thread_id)
    folder = CHANNELS_DIR / platform
    rotated = []
    try:
        for path in folder.glob(f"{glob.escape(safe)}.*.jsonl"):
            match = _ROTATED.match(path.stem)
            if match and match.group("base") == safe:
                rotated.append((match.group("stamp"), path))
    except OSError:
        pass
    files = [path for _, path in sorted(rotated, key=lambda it: it[0])]
    live = folder / f"{safe}.jsonl"
    if live.exists():
        files.append(live)
    return files


def read_thread(platform, thread_id, limit=200):
    """Most recent `limit` records for one thread, oldest first.

    Rotation-aware: a busy room rolls its log over at MAX_BYTES, and the
    rotated copy is still part of the same thread. The live file is read
    first; only when it holds fewer than `limit` records are the newest
    rotated files used to top it up."""
    files = _thread_files(platform, thread_id)
    out = []
    for path in reversed(files):
        need = limit - len(out)
        if need <= 0:
            break
        try:
            lines = path.read_text(encoding=ENCODING).splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        out = _parse_lines(lines)[-need:] + out
    return out


def list_threads(platform=None, details=False):
    """Every thread we have a log for, newest activity first.

    One entry per thread: a rotated copy (`name.20260101-120000.jsonl`) is
    part of `name`, not a thread of its own. `bytes` is the whole thread,
    `modified` its newest file.

    `details=True` adds `space` (the kind: dm / server_channel /
    server_thread / group, "" when the thread has no inbound line) and
    `title` (a room's name, "" for a DM) to every entry. It is opt-in so the
    callers that only need the four keys above (recent_dms, denied,
    send_dm, channels-log) keep their exact shape and do no extra reading."""
    platforms = [platform] if platform else list(PLATFORMS)
    out = []
    registry = {}
    if details:
        for name in platforms:
            registry[name] = _registry_by_file_name(name)
    for name in platforms:
        for base, paths in _thread_groups(name).items():
            size, newest = 0, None
            for path in paths:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                size += stat.st_size
                if newest is None or stat.st_mtime > newest:
                    newest = stat.st_mtime
            if newest is None:
                continue
            item = {
                "platform": name,
                "thread_id": base,
                "bytes": size,
                "modified": datetime.fromtimestamp(newest)
                                    .replace(microsecond=0).isoformat(),
                "_mtime": newest,
            }
            if details:
                item["space"], item["title"] = _room_info(
                    name, base, registry.get(name, {}).get(_safe_name(base)), paths)
            out.append(item)
    out.sort(key=lambda item: item["_mtime"], reverse=True)
    for item in out:
        item.pop("_mtime", None)
    return out


# ---------------------------------------------------------------------------
# Rooms: what a thread is called and who is in it  (channel-spaces S3.2 / S3.3)
# ---------------------------------------------------------------------------
# Read-only. A room's kind and title come from spaces.json when the room is
# registered there, else from the newest inbound line of its own log, so a
# lost or never-written registry costs nothing here (it is a cache).

_TAIL_BYTES = 65536


def _registry_by_file_name(platform):
    """{sanitised thread name: spaces.json record} for one platform. Keyed the
    way the transcript files are named, because that is what a listing has.
    {} when there is no registry. Never raises."""
    try:
        from . import spaces
        return {_safe_name(key): rec for key, rec in spaces.read(platform).items()
                if isinstance(rec, dict)}
    except Exception:  # noqa: BLE001 -- labels are a cache
        return {}


def _newest_inbound(paths):
    """The newest inbound record of a thread, read from the END of its newest
    file(s) only (a busy room's log can be 2 MB). None when there is none."""
    for path in reversed(paths):
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - _TAIL_BYTES))
                chunk = handle.read().decode(ENCODING, "ignore")
        except OSError:
            continue
        lines = chunk.splitlines()
        if size > _TAIL_BYTES:
            lines = lines[1:]          # the first line is probably cut in half
        for record in reversed(_parse_lines(lines)):
            if record.get("dir") == "in":
                return record
    return None


def _room_info(platform, base, registered, paths, newest=None):
    """(kind, title) of one thread: ("dm", "") for a DM, ("", "") when the
    thread has no inbound line and no registry entry. `registered` is the
    thread's spaces.json record, or None."""
    try:
        from . import permissions, space
        rec = registered
        if isinstance(rec, dict) and rec.get("kind") in space.KINDS:
            return rec["kind"], str(rec.get("title") or "")
        newest = newest if newest is not None else _newest_inbound(paths)
        if newest is None:
            return "", ""
        if newest.get("context") == permissions.CTX_DM:
            return space.KIND_DM, ""
        stand_in = permissions.IncomingMessage(
            platform=platform,
            context=newest.get("context") or permissions.CTX_GROUP,
            user_id=newest.get("user_id") or "",
            user_handle=newest.get("user_handle") or "",
            guild_id=newest.get("guild_id") or "",
            channel_id=newest.get("channel_id") or "",
            thread_id=base,
            guild_name=newest.get("guild_name") or "",
            channel_name=newest.get("channel_name") or "",
            thread_name=newest.get("thread_name") or "")
        logged = newest.get("space")
        kind = logged if logged in space.KINDS else space.kind_of(stand_in)
        return kind, space.title_for(stand_in) if kind != space.KIND_DM else ""
    except Exception:  # noqa: BLE001
        return "", ""


MAX_PARTICIPANTS = 100


def _reply_to_view(value):
    """A stored `reply_to` as a viewer gets it: only the four known fields,
    each clipped. None when it is absent or not a dict."""
    if not isinstance(value, dict):
        return None
    out = {"message_id": str(value.get("message_id") or "")[:40],
           "user_id": str(value.get("user_id") or "")[:40],
           "user_name": str(value.get("user_name") or "")[:40],
           "snippet": str(value.get("snippet") or "")[:120]}
    return out if any(out.values()) else None


def _room_entry(record, thread_id):
    text, clipped = _clip(record.get("text"))
    entry = {
        "at": record.get("at") or "",
        "dir": record.get("dir"),
        "thread_id": thread_id,
        "text": text,
        "clipped": clipped,
    }
    if record.get("dir") == "in":
        entry.update({
            "context": record.get("context") or "",
            "user_id": str(record.get("user_id") or ""),
            "user_handle": str(record.get("user_handle") or "")[:60],
            "author_name": str(record.get("author_name") or "")[:60],
            "mentioned": bool(record.get("mentioned")),
            "allowed": record.get("allowed"),
            "stage": record.get("stage"),
            "reason": record.get("reason"),
            "may_use_tools": record.get("may_use_tools"),
            "message_id": str(record.get("message_id") or ""),
            "attachments": int(record.get("attachments") or 0)
                           if str(record.get("attachments") or "0").isdigit() else 0,
        })
        reply = _reply_to_view(record.get("reply_to"))
        if reply:
            entry["reply_to"] = reply
    else:
        entry.update({
            "kind": record.get("kind") or "reply",
            "ok": record.get("ok") is not False,
            "error": record.get("error") or "",
            "provider": record.get("provider") or "",
            "to_user": str(record.get("to_user") or ""),
            "tool_calls": clean_tool_calls(record.get("tool_calls")),
        })
    return entry


def read_space(platform, thread_id, limit=100):
    """One room's log, shaped for a viewer. Read-only: nothing here writes,
    rotates, creates a file or calls a model.

    Rotation-aware (the rotated copies are part of the room), oldest first,
    the newest `limit` messages (1..500). Returns:

      entries               inbound AND outbound lines, each with its tool calls
                            (clean_tool_calls) on a reply
      total / shown         how many lines exist / how many are in `entries`
      kind / title          from spaces.json, else the newest inbound line
      participants          [{user_id, handle, name, messages, last_at}], most
                            recent first. Built from the WHOLE history, not
                            only the shown part. A message the gate turned
                            away is not a participant message (and a person
                            with only denied messages is not listed): it is
                            counted in `denied` and still shown in `entries`
                            with allowed=false so "why no answer" can be seen
      denied                how many inbound lines were turned away
      unattributed_replies  outbound lines with no `to_user`. In a room they
                            are NOT guessed to belong to anyone (the rule
                            person_records applies to group threads too)
    """
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100
    thread = str(thread_id or "")
    result = {"platform": platform, "thread_id": thread, "kind": "", "title": "",
              "entries": [], "total": 0, "shown": 0, "participants": [],
              "denied": 0, "unattributed_replies": 0}
    if platform not in PLATFORMS or not thread:
        return result
    paths = _thread_files(platform, thread)
    records = [r for r in _read_records(paths) if r.get("dir") in ("in", "out")]
    if not records:
        return result
    inbound = [r for r in records if r.get("dir") == "in"]
    try:
        from . import spaces
        registered = spaces.get(platform, thread)
    except Exception:  # noqa: BLE001
        registered = None
    result["kind"], result["title"] = _room_info(
        platform, thread, registered, paths, newest=inbound[-1] if inbound else None)

    seen = {}
    for record in inbound:
        if record.get("allowed") is False:
            result["denied"] += 1
            continue
        uid = str(record.get("user_id") or "")
        if not uid:
            continue
        who = seen.setdefault(uid, {"user_id": uid, "handle": "", "name": "",
                                    "messages": 0, "last_at": ""})
        who["messages"] += 1
        if record.get("user_handle"):
            who["handle"] = str(record["user_handle"])[:60]
        if record.get("author_name"):
            who["name"] = str(record["author_name"])[:60]
        who["last_at"] = max(who["last_at"], str(record.get("at") or ""))
    result["participants"] = sorted(
        seen.values(), key=lambda p: p["last_at"], reverse=True)[:MAX_PARTICIPANTS]
    result["unattributed_replies"] = sum(
        1 for r in records if r.get("dir") == "out" and not r.get("to_user"))
    shown = records[-limit:]
    result["entries"] = [_room_entry(r, thread) for r in shown]
    result["total"] = len(records)
    result["shown"] = len(shown)
    return result


# ---------------------------------------------------------------------------
# One person's slice of the logs  (L.36-P1 Conversation view, P2 usage counts)
# ---------------------------------------------------------------------------
# The files are keyed by THREAD, not by person, so a person's conversation is
# assembled by reading the thread files and keeping what is provably theirs:
#
#   inbound    user_id == theirs.                                    (direct)
#   outbound   `to_user` == theirs (written since L.36-P1).          (direct)
#   outbound   no `to_user` (older lines): theirs ONLY when every inbound line
#              in that thread is from them and every one was a DM. A DM thread
#              has exactly one other party, so the reply can only be to them.
#                                                                    (thread)
#   anything else is left out and counted as `unattributed_replies`, because
#   guessing which of several people in a group a reply was for would put
#   one person's conversation in another person's view.
#
# Read-only. Nothing here writes, rotates or creates a file.

MAX_TEXT_CHARS = 4000          # per message handed to a viewer
_ROTATED = re.compile(r"^(?P<base>.+)\.(?P<stamp>\d{8}-\d{6})$")


def _thread_groups(platform):
    """{thread name: [paths oldest first, live file last]} for one platform.
    A rotated copy (`name.20260101-120000.jsonl`) belongs to the same thread
    as `name.jsonl`; reading only the live file would drop its history."""
    folder = CHANNELS_DIR / platform
    groups = {}
    if not folder.is_dir():
        return groups
    for path in folder.glob("*.jsonl"):
        match = _ROTATED.match(path.stem)
        base = match.group("base") if match else path.stem
        stamp = match.group("stamp") if match else "99999999-999999"
        groups.setdefault(base, []).append((stamp, path))
    return {base: [p for _, p in sorted(items, key=lambda it: it[0])]
            for base, items in groups.items()}


def _read_records(paths):
    out = []
    for path in paths:
        try:
            text = path.read_text(encoding=ENCODING)
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn line from a crash
            if isinstance(record, dict):
                out.append(record)
    return out


def person_records(platform, user_id):
    """Everything attributable to one person, oldest first.

    Returns (matched, unattributed): `matched` is a list of
    (record, via, thread) where `via` is "direct" or "thread" (see above), and
    `unattributed` counts older group-chat replies that could not be matched
    to anyone."""
    uid = str(user_id or "").strip()
    unattributed = 0
    matched = []
    if not uid or platform not in PLATFORMS:
        return matched, unattributed
    for thread, paths in _thread_groups(platform).items():
        records = _read_records(paths)
        inbound = [r for r in records if r.get("dir") == "in"]
        senders = {str(r.get("user_id") or "") for r in inbound}
        if uid not in senders and not any(
                str(r.get("to_user") or "") == uid for r in records):
            continue
        solo = (senders == {uid}
                and all(r.get("context") == "dm" for r in inbound))
        for record in records:
            if record.get("dir") == "in":
                if str(record.get("user_id") or "") == uid:
                    matched.append((record, "direct", thread))
            elif record.get("dir") == "out":
                to = str(record.get("to_user") or "")
                if to:
                    if to == uid:
                        matched.append((record, "direct", thread))
                elif solo:
                    matched.append((record, "thread", thread))
                elif uid in senders:
                    unattributed += 1
    matched.sort(key=lambda item: str(item[0].get("at") or ""))
    return matched, unattributed


def _history_split(platform, user_id):
    """Sort one person's logged history into what is theirs alone and what is
    shared. Returns (solo, in_groups):

      solo       {thread: (paths, conv_ids, records)} -- threads where they are
                 the ONLY sender and every inbound line was a DM. Same test
                 person_records uses to attribute an old reply with no
                 `to_user`: a DM thread has exactly one other party.
      in_groups  how many of their messages (and replies addressed to them)
                 sit in threads other people also wrote in.

    Read-only."""
    uid = str(user_id or "").strip()
    solo, in_groups = {}, 0
    if not uid or platform not in PLATFORMS:
        return solo, in_groups
    for thread, paths in _thread_groups(platform).items():
        records = _read_records(paths)
        inbound = [r for r in records if r.get("dir") == "in"]
        senders = {str(r.get("user_id") or "") for r in inbound}
        theirs = uid in senders or any(
            str(r.get("to_user") or "") == uid for r in records)
        if not theirs:
            continue
        if senders == {uid} and all(r.get("context") == "dm" for r in inbound):
            conv_ids = {str(r["conv_id"]) for r in records if r.get("conv_id")}
            solo[thread] = (paths, conv_ids, records)
        else:
            in_groups += sum(
                1 for r in records
                if (r.get("dir") == "in" and str(r.get("user_id") or "") == uid)
                or (r.get("dir") == "out" and str(r.get("to_user") or "") == uid))
    return solo, in_groups


def history_summary(platform, user_id):
    """What forgetting this person's history would delete, without deleting
    it: {threads, messages, conversations, left_in_groups}."""
    solo, in_groups = _history_split(platform, user_id)
    return {
        "threads": len(solo),
        "messages": sum(len(v[2]) for v in solo.values()),
        "conversations": len({c for v in solo.values() for c in v[1]}),
        "left_in_groups": in_groups,
    }


def forget_conv_ids(conv_ids):
    """Drop thread -> conversation mappings that point at any of `conv_ids`
    (the conversations themselves are deleted by the caller). Returns how many
    mappings went. Best effort: the map is a cache."""
    wanted = {str(c) for c in conv_ids or []}
    if not wanted:
        return 0
    mapping = _load_map()
    drop = [k for k, v in mapping.items() if str(v) in wanted]
    if not drop:
        return 0
    for k in drop:
        del mapping[k]
    try:
        MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = MAP_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(mapping, indent=2) + "\n", encoding=ENCODING)
        os.replace(str(tmp), str(MAP_FILE))
    except OSError:
        return 0
    return len(drop)


def purge_person(platform, user_id):
    """Delete the logs of every thread that is only this person's DMs (live
    file and rotated copies). Returns {threads, files, conv_ids, errors,
    left_in_groups}; `conv_ids` is for the caller to delete the model-facing
    conversations with. Never raises.

    A thread other people also wrote in is NOT touched: removing a line from a
    shared file means rewriting it while a gateway may be appending to it, and
    deleting the file would erase other people's messages. `left_in_groups`
    says how many of their lines stay behind, so the caller can tell the
    owner. The usage ledger is counts-only and is left alone too."""
    solo, in_groups = _history_split(platform, user_id)
    files, errors, conv_ids = 0, [], set()
    for thread, (paths, convs, _records) in solo.items():
        conv_ids |= convs
        for path in paths:
            try:
                path.unlink()
                files += 1
            except FileNotFoundError:
                pass
            except OSError as exc:
                errors.append(f"{path.name}: {exc}")
    return {"threads": len(solo), "files": files,
            "conv_ids": sorted(conv_ids), "errors": errors,
            "left_in_groups": in_groups}


# ---------------------------------------------------------------------------
# One person's conversations, cut into episodes  (L.21 P4)
# ---------------------------------------------------------------------------
# An EPISODE is a run of one person's messages in one thread with no gap longer
# than `gap_seconds`. channels/episodes.py summarizes the finished ones. This
# is the same attribution rule as `person_records` above (their inbound lines;
# replies with their `to_user`; a reply with no `to_user` only in a thread that
# is theirs alone) -- restated here because it also has to remember WHERE each
# line sits in its file, which `person_records` does not. A parity test in
# tests/test_episodes.py keeps the two from drifting.
#
# Two kinds of line are left out: a message the gate turned away (nobody
# answered it, and the person was not accepted), and a reply that failed to
# send. Read-only: nothing here writes, rotates or creates a file.

EPISODE_GAP_SECONDS = 6 * 3600


def _epoch(at):
    try:
        return datetime.fromisoformat(str(at)).timestamp()
    except (TypeError, ValueError):
        return None


def person_episodes(platform, user_id, gap_seconds=EPISODE_GAP_SECONDS):
    """Every episode this person has, oldest first.

    Each is {thread, conv_id, file, line_range, from, to, from_ts, to_ts,
    inbound, outbound, messages}. `messages` is [(who, text)] with who
    "person" or "jarvis", oldest first. `from`/`to` are the log's own
    timestamps (the stable pointer); `file` and `line_range` locate the
    segment's lines in the first file it touches (a rotated log can split one
    segment across two files, in which case `line_range` stops at the end of
    the first)."""
    uid = str(user_id or "").strip()
    out = []
    if not uid or platform not in PLATFORMS:
        return out
    for thread, paths in _thread_groups(platform).items():
        rows = []                  # (record, file name, line number)
        for path in paths:
            try:
                text = path.read_text(encoding=ENCODING)
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    rows.append((record, path.name, number))
        inbound = [r for r, _f, _n in rows if r.get("dir") == "in"]
        senders = {str(r.get("user_id") or "") for r in inbound}
        if uid not in senders and not any(
                str(r.get("to_user") or "") == uid for r, _f, _n in rows):
            continue
        solo = (senders == {uid}
                and all(r.get("context") == "dm" for r in inbound))
        mine = []
        for record, fname, number in rows:
            text = str(record.get("text") or "").strip()
            when = _epoch(record.get("at"))
            if not text or when is None:
                continue
            if record.get("dir") == "in":
                if (str(record.get("user_id") or "") == uid
                        and record.get("allowed") is not False):
                    mine.append(("person", record, fname, number, when))
            elif record.get("dir") == "out" and record.get("ok") is not False:
                to = str(record.get("to_user") or "")
                if (to and to == uid) or (not to and solo):
                    mine.append(("jarvis", record, fname, number, when))
        mine.sort(key=lambda item: item[4])
        segment = []
        for item in mine:
            if segment and item[4] - segment[-1][4] > gap_seconds:
                out.append(_episode(thread, segment))
                segment = []
            segment.append(item)
        if segment:
            out.append(_episode(thread, segment))
    out.sort(key=lambda ep: ep["from_ts"])
    return out


def _episode(thread, segment):
    first = segment[0]
    same_file = [n for _w, _r, f, n, _t in segment if f == first[2]]
    conv = next((str(r["conv_id"]) for _w, r, _f, _n, _t in segment
                 if r.get("conv_id")), "")
    return {
        "thread": thread,
        "conv_id": conv,
        "file": first[2],
        "line_range": [min(same_file), max(same_file)],
        "from": str(first[1].get("at") or ""),
        "to": str(segment[-1][1].get("at") or ""),
        "from_ts": first[4],
        "to_ts": segment[-1][4],
        "inbound": sum(1 for w, *_ in segment if w == "person"),
        "outbound": sum(1 for w, *_ in segment if w == "jarvis"),
        "messages": [(w, str(r.get("text") or "").strip())
                     for w, r, _f, _n, _t in segment],
    }


def _clip(text):
    text = "" if text is None else str(text)
    if len(text) > MAX_TEXT_CHARS:
        return text[:MAX_TEXT_CHARS], True
    return text, False


def read_person(platform, user_id, limit=100):
    """The newest `limit` messages to/from one person, oldest first, shaped
    for a viewer. `total` is how many exist, `unattributed_replies` how many
    older group-chat replies could not be matched to anyone (see above)."""
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100
    matched, unattributed = person_records(platform, user_id)
    entries = []
    for record, via, thread in matched[-limit:]:
        text, clipped = _clip(record.get("text"))
        entry = {
            "at": record.get("at") or "",
            "dir": record.get("dir"),
            "thread_id": thread,
            "text": text,
            "clipped": clipped,
            "via": via,
        }
        if record.get("dir") == "in":
            entry.update({
                "context": record.get("context") or "",
                "mentioned": bool(record.get("mentioned")),
                "allowed": record.get("allowed"),
                "stage": record.get("stage"),
                "reason": record.get("reason"),
                "may_use_tools": record.get("may_use_tools"),
                "guild_id": str(record.get("guild_id") or ""),
                "channel_id": str(record.get("channel_id") or ""),
                "guild_name": str(record.get("guild_name") or "")[:100],
                "channel_name": str(record.get("channel_name") or "")[:100],
            })
        else:
            entry.update({
                "kind": record.get("kind") or "reply",
                "ok": record.get("ok") is not False,
                "error": record.get("error") or "",
                "provider": record.get("provider") or "",
                "tool_calls": clean_tool_calls(record.get("tool_calls")),
            })
        entries.append(entry)
    return {
        "entries": entries,
        "total": len(matched),
        "shown": len(entries),
        "threads": len({thread for _, _, thread in matched}),
        "unattributed_replies": unattributed,
    }


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
