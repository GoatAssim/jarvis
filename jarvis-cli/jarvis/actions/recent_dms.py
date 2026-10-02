"""Auto-discovered tool: L.1 — recent_dms, a compact summary of recent DM
thread activity across configured chat channels (Discord/Instagram).

Lets Jarvis answer "who DMed me recently", "did anyone message me lately",
"what did the last few DMs say", "show me my recent Discord/Instagram
conversations" — from the existing channels/transcript.py audit log,
never a second DM-history store.

WHY OWNER-ONLY
--------------
This reads across EVERY person who has ever DMed the bot, not just
whoever is asking right now — the opposite of actions/channel_people.py's
tools, which are scoped to the current sender's own record. Exposing that
to an ordinary chat guest would let any stranger's message ask Jarvis to
read out other people's private conversations. So the same
JARVIS_CHANNEL_SENDER / is_owner flag channel_people.py uses to find out
who's asking is used here to refuse anyone but the owner: at the PC (no
chat context at all, JARVIS_CHANNEL_SENDER unset) it's always allowed —
the PC is already the owner's own trusted context, same reasoning
actions/notify_owner.py's docstring gives for taking no target argument
at all — and inside a chat conversation it's allowed only when
channels/permissions.is_owner() said so for THIS sender (see
channels/base.py's own use of the same flag, which is where
JARVIS_CHANNEL_SENDER's is_owner value actually comes from).

WHY NO thread_id/user_id ARGUMENT
----------------------------------
Same reasoning again: an id argument would just be
list_threads()/read_thread() with extra steps, but callable by a model a
prompt injection reached. The only inputs accepted are a platform name
and two small limits (L.1.2's "safe high-level filters only") — never an
id, a path, or anything else that could redirect the read.
"""

import json
import os

# Must match channels.base.SENDER_ENV. Not imported from there — same
# reasoning actions/channel_people.py gives for the same duplication:
# this file is discovered during tools.py's own initialization, and
# reaching back into a package that imports ai_client risks the circular-
# import rejection actions/dev_agent.py documents. A bare string plus
# this note is the cheaper trade — the constant is read in one place.
SENDER_ENV = "JARVIS_CHANNEL_SENDER"

# L.1.4 — bound both axes independently: how many threads, and how much
# of each one. Neither limit argument may exceed these even if the model
# asks for more.
MAX_THREADS = 10
MAX_MESSAGES_PER_THREAD = 10
DEFAULT_THREADS = 5
DEFAULT_MESSAGES_PER_THREAD = 4
# Loop guard, independent of the two result-size limits above: a server
# with many active GROUP channels (which this tool skips — DMs only, see
# tool_recent_dms) could otherwise force scanning far more threads than
# it ever returns.
MAX_CANDIDATES_SCANNED = 60
# Trim any single message's text before it goes in the result — this is a
# recent-activity summary (L.1.3's "short bidirectional snippet"), not a
# transcript viewer.
MAX_MESSAGE_CHARS = 240


def _current_sender():
    """Mirrors actions/channel_people.py's helper of the same name — see
    that file's note on why this constant/helper is duplicated rather
    than imported."""
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


def _clamp_int(value, default, lo, hi):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _trim(text):
    text = (text or "").strip()
    if len(text) > MAX_MESSAGE_CHARS:
        return text[:MAX_MESSAGE_CHARS].rstrip() + "\u2026"
    return text


def tool_recent_dms(args):
    from ..channels import PLATFORMS, people, permissions, transcript

    sender = _current_sender()
    if sender is not None and not sender.get("is_owner"):
        return {
            "ok": False,
            "error": "recent_dms is owner-only",
            "hint": ("Only the owner can review other people's DM history "
                     "— this isn't available to whoever is messaging you "
                     "over chat right now."),
        }

    args = args or {}
    platform_arg = (args.get("platform") or "").strip().lower()
    if platform_arg in ("", "all", "any"):
        wanted_platform = None
    elif platform_arg in PLATFORMS:
        wanted_platform = platform_arg
    else:
        return {"ok": False,
                "error": f"unknown platform '{platform_arg}' — use discord, "
                         "instagram, or leave blank"}

    limit_threads = _clamp_int(args.get("limit"), DEFAULT_THREADS, 1, MAX_THREADS)
    limit_messages = _clamp_int(args.get("messages_per_thread"),
                                 DEFAULT_MESSAGES_PER_THREAD, 1, MAX_MESSAGES_PER_THREAD)

    candidates = transcript.list_threads(platform=wanted_platform)

    threads_out = []
    scanned = 0
    for cand in candidates:
        if len(threads_out) >= limit_threads or scanned >= MAX_CANDIDATES_SCANNED:
            break
        scanned += 1

        # Pull generously more than we'll keep — half a thread's records
        # can be denied-message noise or an outbound status line with no
        # text, and read_thread's `limit` counts raw records, not the
        # messages-with-text we actually want.
        records = transcript.read_thread(cand["platform"], cand["thread_id"],
                                          limit=max(limit_messages * 5, 20))
        if not records:
            continue

        # DMs only — a busy group/guild channel is not "who DMed me" and
        # would otherwise crowd out actual DM activity in the result. A
        # DM channel never becomes a group one (or vice versa) on either
        # platform, so the most recent inbound record's context is enough
        # to classify the whole thread.
        last_inbound = next((r for r in reversed(records) if r.get("dir") == "in"), None)
        if last_inbound is None or last_inbound.get("context") != permissions.CTX_DM:
            continue

        messages = [r for r in records
                    if r.get("dir") in ("in", "out") and (r.get("text") or "").strip()]
        if not messages:
            continue
        messages = messages[-limit_messages:]

        user_id = last_inbound.get("user_id") or cand["thread_id"]
        handle = last_inbound.get("user_handle") or ""
        saved = people.get(cand["platform"], user_id) or {}
        person = saved.get("name") or handle or user_id

        threads_out.append({
            "platform": cand["platform"],
            "thread_id": cand["thread_id"],
            "person": person,
            "user_id": user_id,
            "last_message_at": cand.get("modified") or "",
            "messages": [
                {"dir": m.get("dir"), "at": m.get("at") or "", "text": _trim(m.get("text"))}
                for m in messages
            ],
        })

    return {"ok": True, "threads": threads_out}


TOOL_SCHEMAS = [
    {
        "name": "recent_dms",
        "description": (
            "List recent direct-message conversations across configured chat "
            "channels (Discord, Instagram), newest activity first, each with "
            "a short back-and-forth snippet — not a full transcript dump. "
            "Use this for 'who DMed me recently', 'did anyone message me "
            "lately', 'what did the last few DMs say', or 'show me my "
            "recent Discord/Instagram conversations'. Owner-only: refuses "
            "when it's answering someone else's chat message rather than "
            "the owner."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "platform": {
                    "type": "string",
                    "enum": ["discord", "instagram", "any"],
                    "description": (
                        "Which channel to look at. Omit or use 'any' for "
                        "every configured channel."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"How many recent threads to return "
                        f"(default {DEFAULT_THREADS}, max {MAX_THREADS})."
                    ),
                },
                "messages_per_thread": {
                    "type": "integer",
                    "description": (
                        f"How many recent messages to include per thread "
                        f"(default {DEFAULT_MESSAGES_PER_THREAD}, "
                        f"max {MAX_MESSAGES_PER_THREAD})."
                    ),
                },
            },
            "required": [],
        },
    },
]

TOOLS = {
    "recent_dms": tool_recent_dms,
}

# Joins the existing "channels" group actions/channel_people.py and
# actions/notify_owner.py already created — all three are "Jarvis and a
# person over a chat app", and splitting this into its own group would
# just compete with them for the router's two-group budget on largely the
# same trigger phrases.
TOOL_GROUP = "channels"

TOOL_KEYWORDS = {
    "recent_dms": {
        "recent dms": 10,
        "who messaged me": 10,
        "who dmed me": 10,
        "did anyone message me": 9,
        "messaged me lately": 9,
        "messages lately": 7,
        "recent conversations": 7,
        "recent instagram": 7,
        "recent discord": 7,
        "discord":9,
        # L.24 T1: "has anyone texted you on discord" has no phrase of its
        # own - the bare word above was the only thing routing it, and with
        # a platform-less "has anyone texted you" nothing routed at all.
        # These are read-the-inbox phrasings only: "text me" / "text X" are
        # sending, which this tool deliberately does not cover (see below).
        "texted you": 9,
        "anyone texted": 10,
        "has anyone texted": 10,
        "did anyone text": 10,
        "who texted": 10,
        "who texted me": 10,
        "anyone dmed": 10,
        "anyone dm'd": 10,
        "did anyone dm": 10,
        "who dm'd me": 10,
        "anyone messaged you": 9,
        "anyone message you": 9,
        "new dms": 9,
        "unread dms": 9,
        # Deliberately NOT "message me" / "reply to" / "send a message" —
        # those are actions/notify_owner.py's/outbound sending's territory,
        # not this read-only tool, and overlapping them would blur the two
        # for the router (L.1.5's "avoid hijacking ordinary message-sending").
    },
}

# TOOL_PACK_INSTRUCTION is a no-op here in practice: "channels" is an
# existing group and tool_registry.TOOL_PACK_INSTRUCTIONS.setdefault()
# means whichever actions/ file for this group loads first keeps its
# instruction (currently actions/channel_people.py, which this repo's
# discovery order reaches before this file alphabetically) — see that
# module's own TOOL_PACK_INSTRUCTION and the note added to it for recent_dms.
# Declared anyway so the guidance travels with this file if that ever
# changes, per _template.py's own advice not to rely on it either way.
TOOL_PACK_INSTRUCTION = (
    "recent_dms is read-only and owner-only — call it to answer questions "
    "about who has DMed the owner, never to look someone up on behalf of "
    "whoever is currently chatting."
)

# Read-only, and privacy-gated in the handler itself (owner-only check
# above) rather than through the destructive-action confirm mechanism —
# neither entry applies to a tool that only reads.
TOOL_CONFIRM_REQUIRED = set()
TOOL_AI_REVIEW = set()

# L.1.6 — no inline TEST_CHECKLIST here. This ships with jarvis, and per
# test-checklist-data.js's own header, that file (not the module-level
# TEST_CHECKLIST mechanism _template.py also documents) is "the source of
# truth" for a tool in that position; the module attribute exists for a
# tool a user wrote into ~/.jarvis/tools/, which can't be in this file at
# all. See its "channels" group entries for recent_dms's real entry.
