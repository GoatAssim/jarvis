"""Handle -> id directory, learned passively from real messages.

WHY THIS EXISTS, AND WHAT IT CANNOT DO
---------------------------------------
Neither Discord nor Instagram lets you look up an arbitrary stranger's
stable id from their @handle. Discord's username search needs the
privileged Server Members intent plus a shared guild; Instagram has no
lookup at all — an IGSID only comes into existence once that person has
messaged your account, and even then Meta's webhook payload usually omits
their username entirely (see instagram_gateway.py's enrichment step).

So this is not a lookup service. It is a notebook: every time a real
message arrives from someone, whichever gateway handled it writes down
"this handle currently belongs to this id" here. From then on, anywhere in
config.py's allowlists you'd otherwise have to paste a raw id, you can
write `@their_handle` instead and it resolves through this file.

That ordering — record on inbound, resolve on command — is the whole
design. It also means resolution only works for people who have already
messaged the bot at least once, which is a limitation of the platforms,
not of this file. `channels-directory` exists so you can see exactly who
Jarvis currently knows.

STORAGE
-------
    ~/.jarvis/channels/directory.json

One file, all platforms, keyed as "<platform>:<lowercased-handle>". Ids
never expire (a Discord snowflake or an IGSID doesn't get reassigned), but
a handle can be renamed and reused by someone else, so RESOLUTION ALWAYS
PREFERS THE MOST RECENTLY SEEN MAPPING for a given handle — record()
overwrites rather than merges, and last_seen is what "most recent" means.
"""

import re
from pathlib import Path

from . import PLATFORMS
from .. import atomic_io

CHANNELS_DIR = Path.home() / ".jarvis" / "channels"
DIRECTORY_FILE = CHANNELS_DIR / "directory.json"


def _normalize_handle(handle):
    text = str(handle or "").strip().lstrip("@").lower()
    return text


def _key(platform, handle):
    return f"{platform}:{_normalize_handle(handle)}"


def _load():
    return atomic_io.read_json(DIRECTORY_FILE, default={}, expect=dict)


def _save(data):
    return atomic_io.write_json(DIRECTORY_FILE, data)


def record(platform, handle, user_id, when=None):
    """Note that `handle` currently belongs to `user_id` on `platform`.

    Safe to call on every inbound message — it's a cheap dict update, and
    idempotent for someone messaging repeatedly. Does nothing if either
    side is empty, since a message with no handle teaches us nothing new
    and shouldn't overwrite something we already knew.
    """
    handle = _normalize_handle(handle)
    user_id = str(user_id or "").strip()
    if not handle or not user_id or platform not in PLATFORMS:
        return
    import time
    data = _load()
    data[_key(platform, handle)] = {
        "id": user_id,
        "handle": handle,
        "platform": platform,
        "last_seen": when if when is not None else time.time(),
    }
    _save(data)


def resolve(platform, entry):
    """If `entry` looks like a handle we've seen, return its id. Otherwise
    return `entry` unchanged.

    Deliberately permissive about what counts as "looks like a handle":
    anything given as `@name` is treated as a handle lookup even if it
    isn't found, because typing `@` is the user declaring intent — silently
    falling through to storing the literal string "@name" as if it were an
    id would fail in a much more confusing way later, at message-matching
    time instead of at config-edit time.

    A bare numeric string, or "*", is returned as-is without a lookup:
    those are already ids/wildcards, not handles.
    """
    raw = str(entry or "").strip()
    if raw == "*" or not raw:
        return entry, None
    if not raw.startswith("@") and raw.isdigit():
        return entry, None
    if not raw.startswith("@"):
        # Bare non-numeric text (e.g. a Discord username typed without @).
        # Still worth trying the directory before giving up on it.
        pass
    data = _load()
    hit = data.get(_key(platform, raw))
    if hit:
        return hit["id"], None
    return None, (
        f"'{raw}' is not a known handle for {platform} yet. Jarvis only "
        f"learns a handle once that person has messaged the bot — have "
        f"them send a DM first, then try again. Run `jarvis "
        f"channels-directory {platform}` to see who Jarvis currently knows."
    )


def all_entries(platform=None):
    """Every known handle->id mapping, newest first. For `channels-directory`."""
    data = _load()
    items = [v for k, v in data.items()
            if platform is None or k.startswith(f"{platform}:")]
    items.sort(key=lambda it: it.get("last_seen") or 0, reverse=True)
    return items
