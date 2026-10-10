"""Per-person thinking switch (master plan L.44).

WHAT IT CONTROLS
----------------
How much reasoning Jarvis is asked for when it answers THIS person in chat.
It is the `think_override` the gateway passes to `ai_client.ask`:

    "off"   the provider is asked not to reason (cheapest, fastest)
    "on"    the configured reasoning level; if the owner configured none,
            "medium" (so choosing "on" always changes something)
    (none)  no setting: exactly what chat gave everyone before this existed

It is NOT a trace: nothing about Jarvis's reasoning is shown to the person.
(Showing a trace in chat is L.42 and is not built.)

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
