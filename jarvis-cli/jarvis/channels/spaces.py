"""A registry of the rooms (server channels, threads, groups) Jarvis has been
addressed in: what each one is and what it is called.

`~/.jarvis/channels/spaces.json`:

    {platform: {thread_id: {kind, title, guild_id, guild_name, channel_id,
                            channel_name, thread_name, first_seen, last_seen,
                            speakers: [user ids, at most 50]}}}

PRIVACY RULES (the reason this file is small and boring)
--------------------------------------------------------
* NAMES AND LABELS ONLY. Never a message, never text a person wrote.
* ONLY ADDRESSED MESSAGES. `note()` is called from `base.handle_message`
  after the gate, for a message that was actually addressed to Jarvis and only
  while conversations are being saved. It is NEVER called from a gateway's
  pre-gate hook (unlike `servers.note_channel`, which only learns channel
  names): a message that did not mention Jarvis was never seen, and that
  includes registering its room or its author.
* DMs are not registered. A DM is one person; its label lives in people.json.
* IT GRANTS NOTHING. Nothing reads this file to decide who may talk to Jarvis.
* IT IS A CACHE. `rebuild()` reproduces it from the transcripts; losing or
  corrupting the file costs labels, never behaviour (same contract as
  servers.json). A corrupt file reads as empty.
"""

import json
import os
import threading
import time
from datetime import datetime

from . import PLATFORMS, permissions, space, transcript

ENCODING = "utf-8"
MAX_SPEAKERS = 50                 # ids kept per room: a bound, not a policy
MAX_SPACES = 2000                 # per platform
REFRESH_SECONDS = 300             # how stale last_seen may get before a write
_LOCK = threading.Lock()
_cache = {}                       # (platform, thread_id) -> (fingerprint, wrote_at)

_NAME_FIELDS = ("guild_name", "channel_name", "thread_name")


def registry_file():
    """Resolved on each call: tests redirect HOME before import, and a long
    gateway should follow transcript's directory, never a stale copy."""
    return transcript.CHANNELS_DIR / "spaces.json"


def _now():
    return datetime.now().replace(microsecond=0).isoformat()


def _load():
    try:
        data = json.loads(registry_file().read_text(encoding=ENCODING))
        return data if isinstance(data, dict) else {}
    except (OSError, UnicodeDecodeError, ValueError):
        return {}


def _save(data):
    path = registry_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                       encoding=ENCODING)
        os.replace(str(tmp), str(path))
        return True
    except OSError:
        return False


def _fingerprint(rec):
    return (rec.get("kind"), rec.get("title"), rec.get("guild_id"),
            rec.get("channel_id"), *(rec.get(f) for f in _NAME_FIELDS),
            tuple(rec.get("speakers") or ()))


def _block(data, platform):
    block = data.get(platform)
    return block if isinstance(block, dict) else {}


def read(platform):
    """{thread_id: record} for one platform; {} when there is none."""
    return dict(_block(_load(), platform))


def get(platform, thread_id):
    """One room's record, or None. Accepts the id as logged or as it appears
    in a transcript file name (which is sanitised)."""
    block = _block(_load(), platform)
    tid = str(thread_id or "")
    rec = block.get(tid)
    if isinstance(rec, dict):
        return rec
    safe = transcript._safe_name(tid)
    for key, value in block.items():
        if isinstance(value, dict) and transcript._safe_name(key) == safe:
            return value
    return None


def note(platform, msg, is_owner=False):
    """Register or refresh the room `msg` was written in. Never raises.

    Call it ONLY for a message that was addressed to Jarvis (see the module
    docstring). `is_owner` is accepted for call-site symmetry and is not
    stored: the registry records rooms, not who is who.

    Returns None when nothing applies (a DM, an unknown platform, no thread
    id), else `{"written": bool, "renamed": bool, "title": str}`: `written`
    is True when the file changed on disk, `renamed` is True when a name we
    already had changed (the caller refreshes the conversation's label
    then). A name that arrives EMPTY never overwrites a stored one: a
    gateway that did not resolve a name this time has not renamed the room."""
    try:
        if platform not in PLATFORMS:
            return None
        kind = space.kind_of(msg)
        thread_id = str(getattr(msg, "thread_id", "") or "").strip()[:120]
        if kind == space.KIND_DM or not thread_id:
            return None
        now = _now()
        with _LOCK:
            data = _load()
            block = data.get(platform)
            if not isinstance(block, dict):
                block = data[platform] = {}
            existed = isinstance(block.get(thread_id), dict)
            if not existed and len(block) >= MAX_SPACES:
                return None
            rec = block.get(thread_id) if existed else {
                "kind": kind, "title": "", "guild_id": "", "guild_name": "",
                "channel_id": "", "channel_name": "", "thread_name": "",
                "first_seen": now, "last_seen": now, "speakers": []}
            before = _fingerprint(rec)
            renamed = False
            rec["kind"] = kind
            for field in ("guild_id", "channel_id"):
                value = str(getattr(msg, field, "") or "").strip()
                if value:
                    rec[field] = value
            for field in _NAME_FIELDS:
                value = space._clean(getattr(msg, field, ""), 100)
                if value:
                    if existed and rec.get(field) and rec[field] != value:
                        renamed = True
                    rec[field] = value
            # Titled from the MERGED names, so a message that arrived without
            # a name cannot downgrade a good title to an id fallback.
            title = space.title_for(permissions.IncomingMessage(
                platform=platform, context=msg.context, user_id=msg.user_id,
                user_handle=msg.user_handle, guild_id=rec.get("guild_id"),
                channel_id=rec.get("channel_id"), thread_id=thread_id,
                guild_name=rec.get("guild_name"), channel_name=rec.get("channel_name"),
                thread_name=rec.get("thread_name")))
            if title:
                rec["title"] = title
            uid = str(getattr(msg, "user_id", "") or "").strip()[:space.MAX_ID_CHARS]
            speakers = [s for s in (rec.get("speakers") or []) if isinstance(s, str)]
            if uid and uid not in speakers:
                speakers.append(uid)
            rec["speakers"] = speakers[-MAX_SPEAKERS:]

            key = (platform, thread_id)
            after = _fingerprint(rec)
            cached = _cache.get(key)
            fresh = cached is not None and (time.time() - cached[1]) < REFRESH_SECONDS
            if existed and after == before and fresh:
                # Nothing changed and last_seen is recent: a busy room must
                # not rewrite this file on every message.
                return {"written": False, "renamed": False, "title": rec.get("title", "")}
            rec["last_seen"] = now
            block[thread_id] = rec
            written = _save(data)
            if written:
                _cache[key] = (after, time.time())
            return {"written": written, "renamed": renamed,
                    "title": rec.get("title", "")}
    except Exception:  # noqa: BLE001 -- a label cache must never break a reply
        return None


def _latest(records, field):
    for rec in reversed(records):
        value = rec.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def rebuild(platform):
    """Recreate one platform's registry from its transcripts and write it.
    Returns the new `{thread_id: record}`.

    A transcript only ever holds addressed messages, so rebuilding cannot
    register anything note() would not have. Threads that are DMs (any inbound
    line in DM context) are skipped. Entries marked `imported` (written by an
    importer, which has no live transcript to rebuild from) are kept as they
    are. Never raises."""
    try:
        if platform not in PLATFORMS:
            return {}
        fresh = {}
        for base, paths in transcript._thread_groups(platform).items():
            recs = [r for r in transcript._read_records(paths) if r.get("dir") == "in"]
            if not recs or any(r.get("context") == permissions.CTX_DM for r in recs):
                continue
            newest = recs[-1]
            stand_in = permissions.IncomingMessage(
                platform=platform,
                context=newest.get("context") or permissions.CTX_GROUP,
                user_id=newest.get("user_id") or "",
                user_handle=newest.get("user_handle") or "",
                guild_id=newest.get("guild_id") or "",
                channel_id=newest.get("channel_id") or "",
                thread_id=base,
                guild_name=_latest(recs, "guild_name"),
                channel_name=_latest(recs, "channel_name"),
                thread_name=_latest(recs, "thread_name"))
            logged_kind = newest.get("space")
            kind = logged_kind if logged_kind in space.KINDS else space.kind_of(stand_in)
            if kind == space.KIND_DM:
                continue
            speakers = []
            for rec in recs:
                uid = str(rec.get("user_id") or "").strip()[:space.MAX_ID_CHARS]
                if uid and uid not in speakers:
                    speakers.append(uid)
            fresh[base] = {
                "kind": kind,
                "title": space.title_for(stand_in),
                "guild_id": stand_in.guild_id,
                "guild_name": stand_in.guild_name,
                "channel_id": stand_in.channel_id,
                "channel_name": stand_in.channel_name,
                "thread_name": stand_in.thread_name,
                "first_seen": str(recs[0].get("at") or ""),
                "last_seen": str(newest.get("at") or ""),
                "speakers": speakers[-MAX_SPEAKERS:],
            }
        with _LOCK:
            data = _load()
            old = _block(data, platform)
            for key, rec in old.items():
                if isinstance(rec, dict) and rec.get("imported") and key not in fresh:
                    fresh[key] = rec
            data[platform] = fresh
            _save(data)
            for key in [k for k in _cache if k[0] == platform]:
                _cache.pop(key, None)
        return dict(fresh)
    except Exception:  # noqa: BLE001
        return {}
