"""Offline importer for an Instagram "Download your information" messages export.

Meta documents group messaging as unsupported on its official API, so Jarvis cannot
receive an Instagram group chat live through the webhook. This module covers the other
half: it reads a data export the owner downloaded from Instagram and writes the group
chats in it into the channel transcripts, so the read side (`space_view`,
`channels-spaces`, `channels-space`) can show them like any other room.

WHAT IT DOES
------------
* Reads `message_*.json` files (any folder under the path you give it). One folder = one
  chat. Only chats with THREE OR MORE PEOPLE are imported; a two-person chat is a DM, and
  a DM is never imported (it is skipped and counted, not read into the logs).
* Writes transcript lines only, through `transcript.append` (rotation-aware):

      {dir: "in", context: "group", space: "group", imported: true, source: "export",
       at, ts_ms, user_id: "name:<sender name>", user_handle: "", author_name,
       text, mentioned: false, thread_name, ...}

  The export gives display names, not ids, so `user_id` is `name:<sender name>` (clipped
  to 40 characters, with a short hash when a name is longer). There is no gate decision on
  these lines, so none of `allowed` / `stage` / `reason` / `may_use_tools` / `conv_id`
  exists on them: nothing can mistake an imported line for an answered or a refused one.
* Thread id = `import-<8 hex of sha256(export folder name)>`, so the same export imports
  to the same room every time, and an imported room can never collide with a live one.
* Registers each imported room in `spaces.json` (`kind: "group"`, `imported: true`).
* Preview by default: counts per chat and the date range, nothing is written. Writing needs
  `confirm=True` (`--yes` on the command line). Idempotent: a line whose
  (thread, time, author, text hash) already exists is skipped, so a second run, or a run
  after a crash half-way, adds nothing twice.
* After a write, the thread's files get the modification time of their newest message
  (never later than now). Listings sort by modification time and some readers scan only
  the newest few dozen threads; a large import must not push real, recent DM threads out
  of those scans.

WHAT IT NEVER DOES (invariant: the importer grants nothing)
------------------------------------------------------------
No `people.json` record, no allowlist or scope change, no conversation, no conversation
id, no usage line, no model call, no network, no `spaces.note` (which is for live gating),
no message sent anywhere. It does not import `people`, `config` or `permissions` state;
it only reads the group-context constant. `tests/test_instagram_import.py` pins that the
only files that appear are the thread logs and `spaces.json`.

FORMAT RISK (VERIFY against a real export)
------------------------------------------
Written from Meta's documented layout, without a real export to check against:
`participants: [{name}]`, `messages: [{sender_name, timestamp_ms, content, type}]`,
`title`, newest message first, split over `message_1.json`, `message_2.json`, ... Meta's JSON
is known to store UTF-8 text as if it were Latin-1 (so "café" arrives as "cafÃ©");
`_fix_text` reverses that only when the text round-trips cleanly, otherwise the text is left
alone. Messages of type Call / Subscribe / Unsubscribe are system events and are skipped.
Timestamps become local naive ISO time, the same shape live lines use.
"""

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

from . import INSTAGRAM, permissions, space, spaces, transcript

MIN_PEOPLE = 3                  # a chat with fewer people is a DM and is not imported
MAX_THREADS = 2000              # same bound as spaces.MAX_SPACES
MAX_FILE_BYTES = 100_000_000    # one message_N.json; bigger is reported, not read
MAX_TEXT_CHARS = 10_000         # per message; Instagram's own limit is far lower
MAX_LISTED = 50                 # skipped chats / problems named in a result
SKIPPED_TYPES = ("Call", "Subscribe", "Unsubscribe")   # system events, not messages

_MESSAGE_FILE = re.compile(r"^message_(\d+)\.json$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------

def _fix_text(value):
    """Undo Meta's double encoding: UTF-8 bytes that were written out as Latin-1
    characters. Only when the text round-trips cleanly (every character is Latin-1 AND the
    bytes are valid UTF-8); a correctly-encoded "café", or anything with a character above
    U+00FF, is returned untouched."""
    text = value if isinstance(value, str) else ""
    if not text or text.isascii():
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def thread_id_for(folder_name):
    """`import-<8 hex>` for an export chat folder name."""
    digest = hashlib.sha256(str(folder_name).encode("utf-8", "replace")).hexdigest()
    return "import-" + digest[:8]


def user_id_for(name):
    """`name:<sender name>`, at most `space.MAX_ID_CHARS` long. A longer name keeps its
    start and gets a short hash of the full name, so two long names stay two people."""
    name = space._clean(name, 200)
    uid = "name:" + name
    if len(uid) <= space.MAX_ID_CHARS:
        return uid
    tail = hashlib.sha1(name.encode("utf-8", "replace")).hexdigest()[:8]
    return uid[:space.MAX_ID_CHARS - 9] + "~" + tail


def _text_hash(text):
    return hashlib.sha1(str(text).encode("utf-8", "replace")).hexdigest()[:16]


def _key(record):
    """What makes two lines the same message: time, author, text."""
    return (record.get("ts_ms") or record.get("at"), record.get("user_id"),
            _text_hash(record.get("text")))


def _iso(ts_ms):
    """Local naive ISO time (what live lines use), or None for a timestamp that is not one."""
    try:
        ms = int(ts_ms)
        if ms <= 0:
            return None
        return datetime.fromtimestamp(ms / 1000).replace(microsecond=0).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


# ---------------------------------------------------------------------------
# reading the export
# ---------------------------------------------------------------------------

def find_chats(path):
    """({folder path: [message files, in order]}, [problems]) for a path that is an export
    root, any folder inside it, one chat folder, or one message_N.json file. Symlinked
    folders are not followed. Never raises."""
    chats, problems = {}, []
    try:
        root = Path(os.path.expanduser(str(path))).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        return {}, [f"cannot open that path: {exc}"]
    if root.is_file():
        if _MESSAGE_FILE.match(root.name):
            return {root.parent: [root]}, []
        return {}, ["that file is not a message_N.json file (unzip the export first)"]
    if not root.is_dir():
        return {}, ["that path does not exist"]
    for folder, dirs, files in os.walk(root):
        dirs.sort()
        hits = sorted((f for f in files if _MESSAGE_FILE.match(f)),
                      key=lambda f: int(_MESSAGE_FILE.match(f).group(1)))
        if hits:
            chats[Path(folder)] = [Path(folder) / f for f in hits]
        if len(chats) > MAX_THREADS:
            problems.append(f"more than {MAX_THREADS} chats; only the first {MAX_THREADS} are read")
            break
    return dict(list(chats.items())[:MAX_THREADS]), problems


def _load_chat(files):
    """{title, names (set), messages [dict]} from one chat's files, plus a problems list.
    A file that is too big, not JSON or the wrong shape is reported and skipped."""
    title, names, messages, problems = "", set(), [], []
    for file in files:
        try:
            if file.stat().st_size > MAX_FILE_BYTES:
                problems.append(f"{file.name}: larger than {MAX_FILE_BYTES // 1_000_000} MB, skipped")
                continue
            data = json.loads(file.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            problems.append(f"{file.name}: not readable JSON ({type(exc).__name__})")
            continue
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            problems.append(f"{file.name}: no messages list")
            continue
        if not title and isinstance(data.get("title"), str):
            title = _fix_text(data["title"])
        for person in data.get("participants") or ():
            if isinstance(person, dict) and isinstance(person.get("name"), str):
                names.add(space._clean(_fix_text(person["name"]), space.MAX_NAME_CHARS))
        messages.extend(m for m in data["messages"] if isinstance(m, dict))
    return {"title": title, "names": {n for n in names if n}, "messages": messages}, problems


def _records(chat, title):
    """(records oldest first, skipped {reason: count}) for one loaded chat."""
    skipped = {"no_text": 0, "system": 0, "bad_time": 0}
    out = []
    for msg in chat["messages"]:
        if msg.get("type") in SKIPPED_TYPES:
            skipped["system"] += 1
            continue
        text = _fix_text(msg.get("content")).strip()
        raw_sender = _fix_text(msg.get("sender_name"))
        sender = space._clean(raw_sender, space.MAX_NAME_CHARS)
        if not text or not sender:
            skipped["no_text"] += 1
            continue
        at = _iso(msg.get("timestamp_ms"))
        if at is None:
            skipped["bad_time"] += 1
            continue
        record = {
            "dir": "in", "context": permissions.CTX_GROUP, "space": space.KIND_GROUP,
            "user_id": user_id_for(raw_sender), "user_handle": "", "author_name": sender,
            "text": text[:MAX_TEXT_CHARS], "mentioned": False,
            "guild_id": "", "channel_id": "", "message_id": "",
            "imported": True, "source": "export",
            "at": at, "ts_ms": int(msg["timestamp_ms"]),
        }
        if title:
            record["thread_name"] = title
        out.append(record)
    out.sort(key=lambda r: r["ts_ms"])      # the export lists newest first
    return out, skipped


def _existing_keys(thread_id):
    """Keys of the imported lines already saved for this thread (every rotated file)."""
    paths = transcript._thread_files(INSTAGRAM, thread_id)
    return {_key(r) for r in transcript._read_records(paths) if r.get("imported")}


# ---------------------------------------------------------------------------
# the entry point
# ---------------------------------------------------------------------------

def run(path, confirm=False):
    """Preview (default) or import the group chats found under `path`.

    Returns `{ok, mode: "preview"|"import", path, chats: [per chat], totals,
    skipped_chats, problems}`; `ok` is False only when nothing could be read at all.
    Each entry of `chats` has: folder, thread_id, title, people, messages (importable),
    new, duplicates, first_at, last_at, skipped {no_text, system, bad_time}, and in import
    mode `written` (lines appended) and `registered`. A preview writes NOTHING, not even
    a folder. Never raises."""
    result = {"ok": True, "mode": "import" if confirm else "preview", "path": str(path),
              "chats": [], "totals": {"chats": 0, "messages": 0, "new": 0, "duplicates": 0,
                                      "written": 0, "skipped_chats": 0},
              "skipped_chats": [], "problems": []}
    try:
        return _run(path, confirm, result)
    except Exception as exc:  # noqa: BLE001 -- a CLI must print a result, not a traceback
        result["ok"] = False
        result["problems"].append(f"unexpected {type(exc).__name__}: {exc}")
        return result


def _run(path, confirm, result):
    chats, problems = find_chats(path)
    result["problems"].extend(problems)
    if not chats:
        result["ok"] = False
        if not problems:
            result["problems"].append("no message_N.json files found under that path")
        return result
    totals = result["totals"]
    for folder, files in chats.items():
        chat, bad = _load_chat(files)
        result["problems"].extend(f"{folder.name}: {p}" for p in bad)
        senders = {space._clean(_fix_text(m.get("sender_name")), space.MAX_NAME_CHARS)
                   for m in chat["messages"]} - {""}
        people = len(chat["names"] | senders)
        if people < MIN_PEOPLE:
            totals["skipped_chats"] += 1
            if len(result["skipped_chats"]) < MAX_LISTED:
                result["skipped_chats"].append({
                    "folder": folder.name,
                    "reason": f"{people} {'person' if people == 1 else 'people'}: not a group chat"})
            continue
        thread_id = thread_id_for(folder.name)
        title = space._clean(chat["title"], 60)
        records, skipped = _records(chat, title)
        have = _existing_keys(thread_id)
        fresh, seen = [], set(have)
        for rec in records:
            key = _key(rec)
            if key not in seen:
                seen.add(key)
                fresh.append(rec)
        entry = {"folder": folder.name, "thread_id": thread_id, "title": title,
                 "people": people, "messages": len(records), "new": len(fresh),
                 "duplicates": len(records) - len(fresh),
                 "first_at": records[0]["at"] if records else "",
                 "last_at": records[-1]["at"] if records else "", "skipped": skipped}
        if confirm:
            entry["written"] = _write(thread_id, fresh, entry["last_at"]) if fresh else 0
            totals["written"] += entry["written"]
        if confirm and records:
            entry["registered"] = _register(thread_id, title, records)
        result["chats"].append(entry)
        totals["chats"] += 1
        totals["messages"] += len(records)
        totals["new"] += len(fresh)
        totals["duplicates"] += entry["duplicates"]
    if not result["chats"]:
        result["problems"].append(
            f"no chat with {MIN_PEOPLE} or more people was found; nothing to import")
    return result


def _write(thread_id, records, newest_at):
    """Append `records` (oldest first). Returns how many were written. The thread's files
    then take the time of the chat's newest line (`newest_at`, which may be older than
    the lines just added on a re-import that filled a gap) so listings stay in real-time
    order."""
    written = 0
    for rec in records:
        if transcript.append(INSTAGRAM, thread_id, rec):
            written += 1
    if written:
        try:
            stamp = min(datetime.fromisoformat(newest_at).timestamp(),
                        datetime.now().timestamp())
            for file in transcript._thread_files(INSTAGRAM, thread_id):
                os.utime(file, (stamp, stamp))
        except (OSError, ValueError, OverflowError):
            pass            # an unset time costs list order, never the import
    return written


def _register(thread_id, title, records):
    speakers = []
    for rec in records:
        if rec["user_id"] not in speakers:
            speakers.append(rec["user_id"])
    return spaces.register_imported(
        INSTAGRAM, thread_id, title, records[0]["at"], records[-1]["at"], speakers)
