"""What KIND of place a conversation happens in -- pure, no IO.

A **space** is a place a conversation happens. `space_id` is the thread id the
channels already use (Discord channel id, or a Discord thread's own id;
Instagram thread key), so nothing is re-keyed: existing transcript files,
`threads.json` keys and conversation ids stay valid.

Kinds
-----
    dm              a 1:1 conversation
    server_channel  a Discord server channel
    server_thread   a Discord thread / forum post inside a server channel
    group           anything else that is not a DM (an Instagram group thread)

Everything here is a pure function of an `IncomingMessage` (or of a record
read back from a log), so it can be tested without a gateway, a disk or a
model. Nothing here is read by `permissions.decide()`: the gate matches ids,
never names and never kinds.

SPEAKER LABELS
--------------
In a shared room the saved history must say who wrote each line, or one
person's words read as another's. `speaker_record` builds the small clipped
dict stored on an exchange; `speaker_label` turns it into the text prefix the
model sees (`Alice`, `Alice (owner)`, `@alice`, `user...1234`).
"""

import re

from . import DISCORD, permissions

KIND_DM = "dm"
KIND_CHANNEL = "server_channel"
KIND_THREAD = "server_thread"
KIND_GROUP = "group"
KINDS = (KIND_DM, KIND_CHANNEL, KIND_THREAD, KIND_GROUP)

# instagram_gateway.MENTION_THREAD_PREFIX. Duplicated on purpose: that module
# imports an HTTP server and this one must stay import-light. A test pins that
# the two never drift apart.
MENTION_THREAD_PREFIX = "mention-"

MAX_LABEL_CHARS = 32           # the whole label, owner suffix included
OWNER_SUFFIX = " (owner)"
MAX_NAME_CHARS = 40            # stored on an exchange: clipped, never raw
MAX_HANDLE_CHARS = 40
MAX_ID_CHARS = 40
MAX_TITLE_CHARS = 60           # what a conversation title can hold

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _clean(value, limit):
    """One line, no control characters, no square brackets (they delimit the
    label in the prompt), single spaces, clipped."""
    text = _CONTROL.sub(" ", str(value or ""))
    text = text.replace("[", "(").replace("]", ")")
    return " ".join(text.split())[:limit]


def _tail(value, count):
    text = str(value or "").strip()
    return text[-count:] if text else ""


def kind_of(msg):
    """The kind of space this message was written in."""
    if getattr(msg, "context", "") == permissions.CTX_DM:
        return KIND_DM
    thread_id = str(getattr(msg, "thread_id", "") or "")
    channel_id = str(getattr(msg, "channel_id", "") or "")
    if getattr(msg, "platform", "") == DISCORD:
        # A Discord thread has its own id; its parent channel is channel_id.
        if thread_id and channel_id and thread_id != channel_id:
            return KIND_THREAD
        if getattr(msg, "guild_id", ""):
            return KIND_CHANNEL
    return KIND_GROUP


def is_room(msg):
    """True for anything several people can write in (everything but a DM)."""
    return kind_of(msg) != KIND_DM


def _channel_part(msg):
    name = _clean(getattr(msg, "channel_name", ""), 60)
    if name:
        return "#" + name.lstrip("#")
    cid = _tail(getattr(msg, "channel_id", ""), 6)
    return f"Channel {cid}" if cid else ""


def title_for(msg):
    """A human title for a room, e.g. `#general · My Server`,
    `Thread 'deploy talk' · #general · My Server`, `Group 123456`.

    Empty for a DM: the caller keeps its existing title there, so a DM's
    conversation is named exactly as before."""
    kind = kind_of(msg)
    if kind == KIND_DM:
        return ""
    guild = _clean(getattr(msg, "guild_name", ""), 60)
    thread_id = str(getattr(msg, "thread_id", "") or "")
    if kind == KIND_THREAD:
        name = _clean(getattr(msg, "thread_name", ""), 60)
        parts = [f"Thread '{name}'" if name else f"Thread {_tail(thread_id, 6)}".strip(),
                 _channel_part(msg), guild]
    elif kind == KIND_CHANNEL:
        parts = [_channel_part(msg), guild]
    else:
        if thread_id.startswith(MENTION_THREAD_PREFIX):
            who = _clean(getattr(msg, "user_handle", ""), 40).lstrip("@")
            return _clean(f"Mentions · @{who}" if who else "Mentions", MAX_TITLE_CHARS)
        name = _clean(getattr(msg, "thread_name", "") or getattr(msg, "channel_name", ""), 60)
        if name:
            parts = [f"Group '{name}'"]
        else:
            tail = _tail(thread_id, 6)
            parts = [f"Group {tail}" if tail else "Group chat"]
    return _clean(" · ".join(p for p in parts if p), MAX_TITLE_CHARS)


def speaker_record(entry, msg, is_owner):
    """The clipped dict stored on an exchange written in a room:
    `{id, name, handle, owner}`.

    `name` is a SNAPSHOT of what the person was called when they wrote it
    (the name Jarvis knows them by, else the platform display name), so an old
    line keeps its label even if they are renamed later. Every field is
    clipped; nothing here is a message."""
    entry = entry if isinstance(entry, dict) else {}
    try:
        # Imported lazily: people.py is heavy and this module is pure.
        from . import people
        name = people.effective_name(entry)
    except Exception:  # noqa: BLE001 -- a label is never worth a failure
        name = entry.get("name") or ""
    name = name or getattr(msg, "author_name", "")
    handle = entry.get("handle") or getattr(msg, "user_handle", "")
    uid = entry.get("user_id") or getattr(msg, "user_id", "")
    return {
        "id": _clean(uid, MAX_ID_CHARS),
        "name": _clean(name, MAX_NAME_CHARS),
        "handle": _clean(str(handle or "").lstrip("@"), MAX_HANDLE_CHARS),
        "owner": bool(is_owner),
    }


def speaker_label(speaker, is_owner=None):
    """The label the model reads: stored name, else `@handle`, else
    `user...<last4>`; ` (owner)` appended for the owner. Never empty.

    `speaker` is the stored dict (or None for an exchange with no speaker).
    The label is capped at MAX_LABEL_CHARS INCLUDING the owner suffix, so the
    caller can truncate the message text first and prefix the label last and
    the label is never cut off."""
    speaker = speaker if isinstance(speaker, dict) else {}
    owner = bool(speaker.get("owner")) if is_owner is None else bool(is_owner)
    name = _clean(speaker.get("name"), MAX_LABEL_CHARS)
    handle = _clean(str(speaker.get("handle") or "").lstrip("@"), MAX_LABEL_CHARS)
    uid = str(speaker.get("id") or "").strip()
    if name:
        base = name
    elif handle:
        base = "@" + handle
    elif uid:
        base = "user…" + uid[-4:]
    else:
        base = "someone"
    suffix = OWNER_SUFFIX if owner else ""
    return base[:MAX_LABEL_CHARS - len(suffix)].rstrip() + suffix
