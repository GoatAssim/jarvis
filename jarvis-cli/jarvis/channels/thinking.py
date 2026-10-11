"""Per-person thinking switch (master plan L.44).

WHAT IT CONTROLS
----------------
How much reasoning Jarvis is asked for when it answers THIS person in chat.
It is the `think_override` the gateway passes to `ai_client.ask`:

    "off"   the provider is asked not to reason (cheapest, fastest)
    "on"    the configured reasoning level; if the owner configured none,
            "medium" (so choosing "on" always changes something)
    (none)  no setting: exactly what chat gave everyone before this existed

The level and the trace are two different things. The level is what this module
has always controlled. The TRACE (L.42.2) is Jarvis's reasoning text shown in chat
after the reply, and it is shown to the OWNER only, and only while their thread is
"on" (see `wants_trace`). A guest whose switch is "on" gets the higher level but
never sees the trace: it can quote the memory block, other people's records, tool
results and the sender block (Q-L42b), and Q-L44h (may a guest see one at all?) is
still open, so the safe reading is the one taken.

WHO DECIDES WHAT
----------------
  owner (in the panel / CLI)   a value (on / off) and a lock, per person
  the person (in chat)         `/thinking on|off|status` for the current
                               thread, unless the owner locked it
  the owner in chat            always free to use `/thinking`; the owner is
                               never limited by a stored value

The stored value lives with the person's other permissions
(`user_perms.json`) because it is a permission, and `user_perms` already
fails closed: an unreadable file means "off, locked".

The per-thread choice is kept in `thinking_threads.json` as
`platform:thread:user -> on|off`, so one person's choice in one DM does not
change another thread of theirs. In a group it is the SENDER's own setting
that applies to the answer to them.
"""

import re
import time

from .. import atomic_io
from .directory import CHANNELS_DIR
from . import user_perms

THREADS_FILE = CHANNELS_DIR / "thinking_threads.json"
MAX_THREADS = 500
FALLBACK_LEVEL = "medium"

_COMMAND = re.compile(r"^\s*/thinking(?:\s+(on|off|status))?\s*$", re.I)

LOCKED_NOTICE = "Thinking is set by the owner and can't be changed here."


def _tkey(platform, thread_id, user_id):
    return f"{platform}:{thread_id}:{user_id}"


def _load_threads():
    data = atomic_io.read_json(THREADS_FILE, default={}, expect=dict)
    return data if isinstance(data, dict) else {}


def thread_value(platform, thread_id, user_id):
    row = _load_threads().get(_tkey(platform, thread_id, user_id))
    value = row.get("value") if isinstance(row, dict) else None
    return value if value in user_perms.THINKING_VALUES else None


def set_thread(platform, thread_id, user_id, value):
    """Remember one person's choice for one thread. Returns True on success."""
    if value not in user_perms.THINKING_VALUES:
        return False
    data = _load_threads()
    data[_tkey(platform, thread_id, user_id)] = {"value": value, "at": time.time()}
    if len(data) > MAX_THREADS:
        oldest = sorted(data.items(), key=lambda kv: (kv[1] or {}).get("at") or 0)
        for k, _v in oldest[:len(data) - MAX_THREADS]:
            data.pop(k, None)
    return atomic_io.write_json(THREADS_FILE, data)


def effective(platform, thread_id, user_id, is_owner=False):
    """What applies to this person's next answer.

    Returns {"value": "on"|"off"|None, "locked": bool, "source": str}.
    The owner is never locked and has no stored value; only their own
    per-thread choice applies."""
    if is_owner:
        return {"value": thread_value(platform, thread_id, user_id),
                "locked": False, "source": "owner thread choice"}
    stored, locked = user_perms.thinking_setting(platform, user_id)
    if locked:
        return {"value": stored or "off", "locked": True, "source": "owner lock"}
    mine = thread_value(platform, thread_id, user_id)
    if mine is not None:
        return {"value": mine, "locked": False, "source": "their /thinking choice"}
    return {"value": stored, "locked": False,
            "source": "owner setting" if stored else "default"}


def _configured_level():
    """The reasoning level the owner configured ('off' when none)."""
    try:
        from .. import ai_config, reasoning
        defaults = ai_config.load_ai_config().get("defaults") or {}
        level = reasoning.resolve_config(defaults).get("level")
        return level if isinstance(level, str) and level else "off"
    except Exception:  # noqa: BLE001
        return "off"


def override_for(state):
    """The `think_override` for ask(), from effective(): None keeps today's
    behaviour."""
    value = (state or {}).get("value")
    if value == "off":
        return "off"
    if value == "on":
        level = _configured_level()
        return level if level not in ("off", "", None) else FALLBACK_LEVEL
    return None


def wants_trace(state, is_owner=False):
    """Should the trace be sent after this person's reply (L.42.2)?

    Owner only, and only while the effective value is "on" (default off, per
    thread). A guest is never sent one whatever their switch says -- Q-L44h.
    Nothing is shown when the level is off, because there is nothing real to show
    (and on Groq the model may think regardless, L.39: the gateway simply does not
    send it)."""
    return bool(is_owner) and (state or {}).get("value") == "on"


TRACE_MAX_MESSAGES = 3
SPOILER_LIMIT = 1900
_TRUNCATED = "(thinking trace truncated)"


def format_trace(platform, text, level="", limit=SPOILER_LIMIT, max_messages=TRACE_MAX_MESSAGES):
    """The trace as the list of messages to send, each its own message AFTER the
    reply (Q-L42e').

    Discord: the text inside `||spoiler||`, split across at most `max_messages`
    messages of `limit` characters, with a plain "(thinking trace truncated)" note
    when there was more. A literal `||` in the trace is broken up so it cannot end
    the spoiler early. Everywhere else (Instagram has no spoiler syntax) one plain
    message labelled "Thinking:", cut to `limit` with the same note. Empty text or
    a non-positive limit -> no messages."""
    text = str(text or "").strip()
    if not text or int(limit or 0) <= 0:
        return []
    label = "Thinking" + (f" ({level})" if level and level != "off" else "") + ":"
    if platform == "discord":
        body = text.replace("||", "| |")
        room = max(50, int(limit) - 4 - len(label) - 1 - len(_TRUNCATED) - 1)
        pieces = []
        rest = body
        while rest and len(pieces) <= max_messages:
            if len(rest) <= room:
                pieces.append(rest)
                rest = ""
            else:
                cut = rest.rfind("\n", 0, room)
                if cut < room * 0.5:
                    cut = rest.rfind(" ", 0, room)
                if cut < room * 0.5:
                    cut = room
                pieces.append(rest[:cut].rstrip())
                rest = rest[cut:].lstrip()
        truncated = len(pieces) > max_messages or bool(rest)
        pieces = pieces[:max_messages]
        out = []
        for i, piece in enumerate(pieces):
            head = label + "\n" if i == 0 else ""
            out.append(f"{head}||{piece}||")
        if truncated:
            out[-1] = out[-1] + "\n" + _TRUNCATED
        return out
    room = max(50, int(limit) - len(label) - 1 - len(_TRUNCATED) - 5)
    if len(text) > room:
        cut = text[:room]
        space = cut.rfind(" ")
        if space > room * 0.7:
            cut = cut[:space]
        return [f"{label}\n{cut.rstrip()} ... {_TRUNCATED}"]
    return [f"{label}\n{text}"]


def parse_command(text):
    """'/thinking', '/thinking on' ... -> the word ('toggle' for a bare
    command), or None when the message is not this command."""
    match = _COMMAND.match(str(text or ""))
    if not match:
        return None
    return (match.group(1) or "status").lower()


def handle_command(platform, thread_id, user_id, text, is_owner=False):
    """Answer a /thinking message without asking the model. Returns the reply
    text, or None when `text` is not the command."""
    word = parse_command(text)
    if word is None:
        return None
    state = effective(platform, thread_id, user_id, is_owner=is_owner)
    if word == "status":
        value = state["value"] or "the default"
        return (f"Thinking is {value} here."
                + (" The owner set it." if state["locked"] else ""))
    if state["locked"]:
        return LOCKED_NOTICE
    if not set_thread(platform, thread_id, user_id, word):
        return "I couldn't save that, so thinking is unchanged."
    return f"Thinking is now {word} for this chat."


def forget_person(platform, user_id):
    """Drop every per-thread choice this person made (forget-this-person)."""
    data = _load_threads()
    suffix = f":{user_id}"
    keep = {k: v for k, v in data.items()
            if not (k.startswith(f"{platform}:") and k.endswith(suffix))}
    if len(keep) != len(data):
        atomic_io.write_json(THREADS_FILE, keep)
    return len(data) - len(keep)
