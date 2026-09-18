"""Who Jarvis is actually talking to on a chat platform.

WHY THIS ISN'T directory.py
---------------------------
`directory.py` is a lookup table: handle -> id, so an allowlist can say
`@someone` instead of a raw snowflake. It answers "which account is this
name?".

This file answers a completely different question: "who is this person,
and what is my relationship with them?". Name they asked to be called,
when they first showed up, how many times they've written, whether the
owner has approved them. Those are facts about a *person*, keyed by the
one thing that never changes (the platform id), whereas directory.py is
keyed by the thing that does change (the handle) precisely because its job
is to resolve renames.

Keeping them apart means a handle rename updates directory.py and leaves
this file's history intact, which is the correct behaviour for both.

THE PROBLEM THIS EXISTS TO FIX
------------------------------
Until now `base._ask_jarvis()` handed the model nothing but the message
text. Over Discord or Instagram that is actively misleading: the system
prompt says "you are a private AI assistant running locally for one user
on their own computer" and addresses whoever is typing as the owner. So a
stranger DMing the bot got answered as though they *were* the owner —
same form of address, same assumed history, same long-term memory about
someone else's life.

So every accepted message now carries a short identity block naming who
sent it and whether they're the owner. It's a handful of tokens and it is
the difference between "your usual, sir" and "I don't think we've met".

OWNER NOTIFICATION
------------------
First contact from a non-owner also pings the owner once — never on every
message, which is what `notified_at` is for. The owner decides whether to
"follow" that person (`follow`), and that decision is recorded here so the
question is asked exactly once per person.

`follow` is deliberately NOT a permission. Approving someone here writes
them into the real `reply_allowlist` via channels/config.py (see
`cli`-level channels-follow); this field only records that the question
was answered, so permissions.py stays the single source of truth for who
may be answered. Two systems that both decide access is how you end up
with a bot that answers someone every allowlist says it shouldn't.

STORAGE
-------
    ~/.jarvis/channels/people.json

Keyed "<platform>:<user_id>". Written through atomic_io like every other
store here — this accumulates slowly over months and is not regenerable
from anything.
"""

import time

from . import PLATFORMS
from .. import atomic_io
from .directory import CHANNELS_DIR

PEOPLE_FILE = CHANNELS_DIR / "people.json"

# Follow states. "unknown" is the starting point for anyone who hasn't
# been asked about yet; "pending" means the owner has been told and hasn't
# answered. Both are non-permissions — see the module docstring.
FOLLOW_UNKNOWN = "unknown"
FOLLOW_PENDING = "pending"
FOLLOW_APPROVED = "approved"
FOLLOW_BLOCKED = "blocked"
FOLLOW_STATES = (FOLLOW_UNKNOWN, FOLLOW_PENDING, FOLLOW_APPROVED, FOLLOW_BLOCKED)

# A name is shown to the model and typed by a stranger, so it is capped and
# stripped of newlines — a "name" containing its own instructions on its
# own line is the obvious injection to try against a block that sits in the
# system prompt.
MAX_NAME_LEN = 48
MAX_NOTE_LEN = 160
MAX_NOTES = 6


def _load():
    return atomic_io.read_json(PEOPLE_FILE, default={}, expect=dict)


def _save(data):
    return atomic_io.write_json(PEOPLE_FILE, data)


def key(platform, user_id):
    return f"{platform}:{str(user_id or '').strip()}"


def _clean_text(value, limit):
    """One line, length-capped. Control characters and newlines are dropped
    rather than escaped: this text is interpolated into the system prompt,
    and a value that can introduce a line break can introduce a line that
    reads like an instruction."""
    text = " ".join(str(value or "").split())
    return text[:limit].strip()


def _blank(platform, user_id, handle=""):
    now = time.time()
    return {
        "platform": platform,
        "user_id": str(user_id or ""),
        "handle": _clean_text(handle, MAX_NAME_LEN).lstrip("@").lower(),
        "name": "",
        "notes": [],
        "first_seen": now,
        "last_seen": now,
        "messages": 0,
        "follow": FOLLOW_UNKNOWN,
        "is_owner": False,
        "notified_at": None,
    }


def get(platform, user_id):
    """The stored record, or None. Never creates."""
    if not str(user_id or "").strip():
        return None
    entry = _load().get(key(platform, user_id))
    return entry if isinstance(entry, dict) else None


def touch(platform, user_id, handle="", is_owner=False):
    """Record that this person just sent an accepted message.

    Creates the record on first contact, bumps last_seen/messages after
    that, and refreshes the handle (people rename themselves). Returns the
    record, which is what the caller renders into the prompt block — so
    the common path is one read, one write, no extra lookup.

    `is_owner` is stored rather than recomputed at read time because the
    config's `owner` field can be edited later and it's genuinely useful
    to see that someone *was* being treated as the owner during a past
    exchange.
    """
    if platform not in PLATFORMS or not str(user_id or "").strip():
        return _blank(platform, user_id, handle)
    data = _load()
    k = key(platform, user_id)
    entry = data.get(k)
    if not isinstance(entry, dict):
        entry = _blank(platform, user_id, handle)
    if handle:
        entry["handle"] = _clean_text(handle, MAX_NAME_LEN).lstrip("@").lower()
    entry["is_owner"] = bool(is_owner)
    entry["last_seen"] = time.time()
    entry["messages"] = int(entry.get("messages") or 0) + 1
    # The owner is never a follow question — they already own the thing.
    if is_owner:
        entry["follow"] = FOLLOW_APPROVED
    data[k] = entry
    _save(data)
    return entry


def _update(platform, user_id, **fields):
    data = _load()
    k = key(platform, user_id)
    entry = data.get(k)
    if not isinstance(entry, dict):
        entry = _blank(platform, user_id)
    entry.update(fields)
    data[k] = entry
    _save(data)
    return entry


def set_name(platform, user_id, name):
    """What this person asked to be called. Cleared by an empty name."""
    return _update(platform, user_id, name=_clean_text(name, MAX_NAME_LEN))


def add_note(platform, user_id, note):
    """Append one short fact about this person.

    Capped at MAX_NOTES, oldest dropped first. This is deliberately a small
    ring buffer and not long-term memory: memory.py is the notebook for
    facts about the *owner's* world, and letting an arbitrary stranger on
    the internet write unbounded entries into the same store that rides
    along in every prompt is not a thing to build on purpose.
    """
    note = _clean_text(note, MAX_NOTE_LEN)
    if not note:
        return get(platform, user_id) or _blank(platform, user_id)
    entry = get(platform, user_id) or _blank(platform, user_id)
    notes = [n for n in (entry.get("notes") or []) if isinstance(n, str)]
    if note not in notes:
        notes.append(note)
    return _update(platform, user_id, notes=notes[-MAX_NOTES:])


def set_follow(platform, user_id, status):
    if status not in FOLLOW_STATES:
        raise ValueError(f"unknown follow state '{status}' — expected one of: "
                         + ", ".join(FOLLOW_STATES))
    return _update(platform, user_id, follow=status)


def mark_notified(platform, user_id):
    """Remember that the owner has already been told about this person, so
    they're told once and not on every message."""
    return _update(platform, user_id, notified_at=time.time(),
                   follow=FOLLOW_PENDING)


def needs_owner_notice(entry):
    """Should the owner be pinged about this sender right now?

    Once per person, and never for the owner themselves or for someone
    already decided about. A record that somehow lost its notified_at but
    has an answered follow state is treated as already handled — the point
    is to ask once, and re-asking because of a bookkeeping gap is worse
    than not asking.
    """
    if not isinstance(entry, dict) or entry.get("is_owner"):
        return False
    if entry.get("follow") in (FOLLOW_APPROVED, FOLLOW_BLOCKED, FOLLOW_PENDING):
        return False
    return not entry.get("notified_at")


def all_people(platform=None, follow=None):
    """Every known person, most recently seen first. For `channels-people`."""
    items = []
    for k, v in _load().items():
        if not isinstance(v, dict):
            continue
        if platform is not None and not k.startswith(f"{platform}:"):
            continue
        if follow is not None and (v.get("follow") or FOLLOW_UNKNOWN) != follow:
            continue
        items.append(v)
    items.sort(key=lambda it: it.get("last_seen") or 0, reverse=True)
    return items


def resolve_user_id(platform, entry_text):
    """Accept an id, a @handle, or a bare handle and return a stored id.

    Checks this file's own records before falling back to directory.py, so
    `jarvis channels-follow discord @someguy` works off the person you were
    just told about rather than needing the snowflake pasted in.
    """
    raw = str(entry_text or "").strip().lstrip("@").lower()
    if not raw:
        return None
    if raw.isdigit() and get(platform, raw):
        return raw
    for entry in all_people(platform):
        if (entry.get("handle") or "").lower() == raw:
            return entry.get("user_id")
        if (entry.get("name") or "").lower() == raw:
            return entry.get("user_id")
    if raw.isdigit():
        return raw
    from . import directory
    resolved, _err = directory.resolve(platform, entry_text)
    return resolved


def describe(entry):
    """One human-readable line for `jarvis channels-people`."""
    if not isinstance(entry, dict):
        return ""
    who = entry.get("name") or entry.get("handle") or entry.get("user_id") or "?"
    bits = [f"{who}"]
    if entry.get("handle") and entry.get("name"):
        bits.append(f"(@{entry['handle']})")
    bits.append(f"id={entry.get('user_id')}")
    bits.append(f"follow={entry.get('follow') or FOLLOW_UNKNOWN}")
    bits.append(f"msgs={entry.get('messages') or 0}")
    if entry.get("is_owner"):
        bits.append("OWNER")
    return "  ".join(bits)


def prompt_block(entry, platform):
    """The identity block injected into the system prompt for one message.

    Kept to a few lines on purpose — it rides along on every single turn
    of a chat conversation, so it is exactly the kind of text that has to
    justify its tokens. What earns its place:

      * whether this is the owner (changes the entire register of the
        reply, and whether the owner's own long-term memory is theirs to
        talk about)
      * the person's name, if known, so it isn't asked for twice
      * an explicit instruction to ASK for the name when it isn't known,
        because otherwise the model cheerfully carries on forever calling
        a stranger "sir"

    Returns "" for the owner on the CLI/web path (no entry at all), which
    keeps every non-chat ask byte-identical to what it was before.
    """
    if not isinstance(entry, dict):
        return ""
    if entry.get("is_owner"):
        return (
            f"This message arrived over {platform}, from your owner's own "
            f"account. Treat it exactly like a message at the PC."
        )

    who = entry.get("name") or ""
    handle = entry.get("handle") or ""
    label = who or (f"@{handle}" if handle else "someone you don't know")
    lines = [
        f"IDENTITY: this message came over {platform} from {label} — "
        f"NOT your owner. Do not address them as your owner, do not use "
        f"your owner's name for them, and do not repeat anything from "
        f"long-term memory about your owner's life, files, schedule or "
        f"machine. Answer them as a polite stranger would be answered."
    ]
    if not who:
        lines.append(
            "You don't know this person's name yet. Ask what to call them "
            "early in the conversation, and once they tell you, call "
            "remember_sender to save it."
        )
    else:
        lines.append(f"They asked to be called {who}.")
    notes = [n for n in (entry.get("notes") or []) if isinstance(n, str)][:MAX_NOTES]
    if notes:
        lines.append("What you already know about them: " + "; ".join(notes) + ".")
    return " ".join(lines)
