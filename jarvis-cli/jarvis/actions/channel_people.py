"""Auto-discovered tool: let Jarvis remember who it's talking to on a
chat platform.

The counterpart to channels/people.py's prompt block. That block tells the
model "you don't know this person's name, ask for it"; this is what the
model calls once they answer.

WHY THIS ISN'T memory_save
--------------------------
memory.py is the owner's notebook. Its facts ride along in the owner's own
prompts, at the PC and everywhere else, and they are retrieved by relevance
against whatever the owner is asking about. A stranger on Discord writing
into that store would be writing into the owner's assistant's head — and
"remember that you should always do what I say" is a one-line message away
from being a fact the owner's own next ask retrieves and applies.

So a chat guest's details go somewhere with a hard boundary: keyed by their
platform id, capped in size, only ever surfaced back to that same person's
own conversation. See channels/people.py's docstring.

WHY IT TAKES NO TARGET
----------------------
Same reasoning as actions/notify_owner.py. The tool acts on whoever sent
the message currently being answered, read from JARVIS_CHANNEL_SENDER (set
by channels/base.py under its ask lock). A `platform`/`user_id` argument
would let a prompt injection reachable from any chat rewrite a *different*
person's record — including flipping the owner's own — which is a much
worse outcome than the tool being slightly less flexible.
"""

import json
import os

# Must match channels.base.SENDER_ENV. Not imported from there: this file is
# discovered during tools.py's own initialization, and reaching back into a
# package that imports ai_client risks the circular-import rejection
# documented in actions/dev_agent.py. A bare string plus this note is the
# cheaper trade — the constant is read in exactly two places.
SENDER_ENV = "JARVIS_CHANNEL_SENDER"

_NOT_IN_CHAT = {
    "ok": False,
    "error": "not in a chat conversation",
    "hint": ("remember_sender only works for a message that arrived over "
             "Discord or Instagram. At the PC, use memory_save instead."),
}


def _current_sender():
    """Who is being answered right now, or None outside a chat gateway."""
    raw = os.environ.get(SENDER_ENV) or ""
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if not data.get("platform") or not data.get("user_id"):
        return None
    return data


def tool_remember_sender(args):
    from ..channels import people

    sender = _current_sender()
    if sender is None:
        return dict(_NOT_IN_CHAT)

    args = args or {}
    name = (args.get("name") or "").strip()
    note = (args.get("note") or "").strip()
    if not name and not note:
        return {
            "needs_clarification": True,
            "message": "Pass a name, a note, or both.",
        }

    platform = sender["platform"]
    user_id = sender["user_id"]

    entry = None
    if name:
        entry = people.set_name(platform, user_id, name)
    if note:
        entry = people.add_note(platform, user_id, note)

    entry = entry or {}
    return {
        "ok": True,
        "name": entry.get("name") or "",
        "notes": entry.get("notes") or [],
        # Echoed back so the model can say "I'll remember that, Sam" with
        # the name that was actually stored after cleaning, rather than the
        # raw string it sent.
        "saved_for": entry.get("handle") or user_id,
    }


def tool_who_am_i_talking_to(args=None):
    from ..channels import people

    sender = _current_sender()
    if sender is None:
        return dict(_NOT_IN_CHAT)
    entry = people.get(sender["platform"], sender["user_id"]) or {}
    return {
        "ok": True,
        "platform": sender["platform"],
        "handle": entry.get("handle") or sender.get("handle") or "",
        "name": entry.get("name") or "",
        "is_owner": bool(entry.get("is_owner") or sender.get("is_owner")),
        "notes": entry.get("notes") or [],
        "messages": entry.get("messages") or 0,
    }


TOOL_SCHEMAS = [
    {
        "name": "remember_sender",
        "description": (
            "Remember the name (or a short detail) of the person messaging "
            "you on Discord/Instagram, so you still know it next time. Call "
            "this as soon as they tell you what to call them. Only ever "
            "affects the person you are replying to right now — it cannot "
            "write to anyone else's record, and it is not your owner's "
            "long-term memory."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "What they asked to be called, e.g. 'Sam'.",
                },
                "note": {
                    "type": "string",
                    "description": (
                        "One short detail worth keeping about them, e.g. "
                        "'friend of the owner from uni'. Optional."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "who_am_i_talking_to",
        "description": (
            "Check who the current chat message is from — their name if you "
            "saved one, whether they are your owner, and anything you noted "
            "about them before. Use it when you're unsure whether you've met "
            "this person."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]

TOOLS = {
    "remember_sender": tool_remember_sender,
    "who_am_i_talking_to": tool_who_am_i_talking_to,
}

# Joins the existing "channels" group that notify_owner already created —
# both are "Jarvis talking to a person over a chat app", and splitting them
# would mean two groups whose keywords overlap heavily competing for the
# router's two-group budget.
TOOL_GROUP = "channels"

TOOL_KEYWORDS = {
    "remember_sender": {
        "my name is": 10,
        "call me": 9,
        "i'm called": 9,
        "remember me": 10,
        "remember my name": 10,
        "you can call me": 10,
    },
    "who_am_i_talking_to": {
        "who am i": 8,
        "do you know me": 9,
        "do you remember me": 10,
        "have we met": 9,
    },
}

TOOL_PACK_INSTRUCTION = (
    "remember_sender saves the name of whoever is messaging you over chat. "
    "If the identity block says you don't know them yet, ask what to call "
    "them and save it the moment they answer — don't ask twice, and don't "
    "guess a name from their handle."
)

# Neither tool touches anything outside one guest's own small record, so
# neither needs a confirmation gate.
TOOL_CONFIRM_REQUIRED = set()
TOOL_AI_REVIEW = set()
