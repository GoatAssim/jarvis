"""The pipeline every gateway runs, once a platform message is normalized.

A gateway's job is narrow: turn its SDK's event into an IncomingMessage,
call handle_message(), and send whatever comes back. Everything between
those two points — the gate, the transcript, the conversation mapping, the
tool gating, the chunking — lives here, once, so Discord and Instagram
cannot drift apart in the part that actually decides what happens.

TOOL GATING IS DONE WITH AN ENV VAR, UNDER A LOCK
-------------------------------------------------
tools.allowed_tools_from_env() already reads JARVIS_ALLOWED_TOOLS and
treats an empty set as "allow nothing" (None means unrestricted). That is
exactly the per-message switch this needs, and reusing it means tool gating
goes through the same single chokepoint every other caller uses rather than
a second, parallel mechanism that could disagree with it.

The catch is that os.environ is process-global while a gateway is a
long-running process handling messages concurrently. So asks are serialized
behind _ASK_LOCK: set the var, ask, restore, release. The cost is that two
users talking at once are answered one after the other instead of in
parallel.

That is a real tradeoff and worth naming. The alternative — spawning a
`jarvis` subprocess per message, the way web/server.js does — would give
true isolation and parallelism at the price of interpreter startup on every
single message. For a personal assistant with a handful of allowlisted
users, serialized-and-simple beats parallel-and-forked; a provider round
trip is seconds anyway, and serializing also stops five simultaneous
messages from burning five providers' worth of tokens at once. If this ever
needs to serve a busy server, the subprocess model is the upgrade path, not
a bigger lock.
"""

import os
import sys
import threading
import traceback

from . import PLATFORMS
from . import config as channel_config
from . import permissions, transcript

# Serializes ask() calls so the JARVIS_ALLOWED_TOOLS mutation below is safe.
_ASK_LOCK = threading.Lock()

# Sent when the model produced nothing at all. A silent bot is
# indistinguishable from a crashed one, which is the worst failure mode for
# something you interact with over chat.
_EMPTY_REPLY = "(no answer — every configured provider failed. Check `jarvis logs`.)"


def _log(message):
    """Always-on stderr trace, matching cli.py's convention — there is no
    debug/verbose flag anywhere in this project (see AGENTS.md) so new
    trace output is unconditional rather than gated behind one that would
    have to be invented here."""
    print(f"[channels] {message}", file=sys.stderr, flush=True)


def chunk_text(text, limit):
    """Split a reply into platform-sized pieces.

    Prefers paragraph breaks, then line breaks, then whitespace, and only
    hard-cuts a run of text with no break in it at all (a long URL, a
    base64 blob). Chat platforms reject an over-length message outright
    rather than truncating it, so a reply that doesn't fit must become
    several that do, not one that vanishes.
    """
    text = (text or "").strip()
    if not text:
        return []
    if limit <= 0 or len(text) <= limit:
        return [text]

    chunks = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = -1
        for sep in ("\n\n", "\n", ". ", " "):
            found = window.rfind(sep)
            # Only honour a break that isn't uselessly close to the start —
            # breaking at character 3 of a 1900-char budget would turn one
            # long message into hundreds of tiny ones.
            if found > limit // 4:
                cut = found + (len(sep) if sep.strip() == "" else len(sep))
                break
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return [c for c in chunks if c]


def _scope_detail(msg):
    """Short human-readable "where did this come from", for the Logs viewer.
    Ids rather than names because a gateway has the id for free and would
    need an extra API call per message to resolve a name."""
    bits = []
    if msg.context == permissions.CTX_DM:
        bits.append("DM")
    else:
        bits.append("group")
    if msg.guild_id:
        bits.append(f"guild {msg.guild_id}")
    if msg.channel_id and msg.channel_id != msg.guild_id:
        bits.append(f"channel {msg.channel_id}")
    who = msg.user_handle or msg.user_id
    if who:
        bits.append(f"with {who}")
    return " / ".join(bits)


def _ask_jarvis(text, conv_id, may_use_tools, on_tool_call=None, platform=""):
    """One ask, with tools allowed or forbidden for this specific sender.

    Imported lazily: ai_client pulls in the whole tool catalog, and a
    gateway that fails to start should fail on its own missing SDK rather
    than on an unrelated import several layers down.
    """
    from .. import ai_client, commands_config

    try:
        commands = commands_config.load_commands_dict()
    except Exception:  # noqa: BLE001 — a broken commands file must not block a reply
        commands = {}

    with _ASK_LOCK:
        previous = os.environ.get("JARVIS_ALLOWED_TOOLS")
        previous_source = os.environ.get("JARVIS_LOG_SOURCE")
        if platform:
            # Tags every log entry this ask writes, matching what the
            # scheduler does for job runs (see scheduler.py). Belt and
            # braces with the conversation-level origin above: the
            # conversation label survives even if this env var is lost,
            # and the entry label survives even if a chat ask somehow
            # lands in a pre-existing conversation.
            os.environ["JARVIS_LOG_SOURCE"] = platform
        if not may_use_tools:
            # Empty string -> empty allowlist -> no tool reaches the model.
            # Note this is NOT the same as unsetting it (None = unrestricted).
            os.environ["JARVIS_ALLOWED_TOOLS"] = ""
        try:
            result = ai_client.ask(
                text,
                commands=commands,
                conversation_id=conv_id,
                on_tool_call=on_tool_call,
            )
        finally:
            # Restore exactly, including the "wasn't set at all" case —
            # leaving an empty string behind would silently disable tools
            # for every later message in this process.
            if not may_use_tools:
                if previous is None:
                    os.environ.pop("JARVIS_ALLOWED_TOOLS", None)
                else:
                    os.environ["JARVIS_ALLOWED_TOOLS"] = previous
            if platform:
                if previous_source is None:
                    os.environ.pop("JARVIS_LOG_SOURCE", None)
                else:
                    os.environ["JARVIS_LOG_SOURCE"] = previous_source
    return result


def _conv_id_for(platform, thread_id, detail=""):
    """Stable conversation per chat thread, created on first contact."""
    from .. import conversations

    def create():
        try:
            # make_current=False matters: `jarvis ask` in a terminal
            # tracks a "current conversation", and a chat bot creating one
            # would silently redirect the owner's next CLI message into a
            # Discord thread's history. A gateway always addresses its
            # conversation by id, so it never needs to be current.
            record = conversations.new_conversation(
                title=f"{platform}:{thread_id}"[:60], make_current=False,
                # Labels this whole conversation in the Logs viewer. Unlike
                # the scheduler — which runs inside a conversation the user
                # already owns — a chat thread's conversation is created by
                # and belongs entirely to the bot, so tagging it once at
                # creation is both accurate and cheaper than tagging every
                # entry in it.
                origin=platform, origin_detail=detail)
            # new_conversation returns a record on some paths and an id on
            # others depending on version; accept either rather than pin to
            # one and break on the other.
            if isinstance(record, dict):
                return record.get("id")
            return record
        except Exception:  # noqa: BLE001
            return None

    return transcript.conv_id_for(platform, thread_id, create=create)


def handle_message(platform, msg, send, cfg=None, on_tool_call=None):
    """Run one inbound message all the way through.

    `send(text)` is the gateway's own sender, called once per chunk. It
    should return True on success. Returns the Decision, so a gateway can
    react to a denial (add a reaction, say nothing) without re-running the
    gate itself.

    Never raises: an exception anywhere in here is logged and swallowed, on
    the principle that one malformed message must not kill a gateway that
    is holding a WebSocket open for everyone else.
    """
    cfg = cfg or channel_config.platform_config(platform)

    try:
        decision = permissions.decide(
            cfg, msg,
            last_seen_at=transcript.last_accepted(platform, msg.user_id),
        )
    except Exception as exc:  # noqa: BLE001 — a gate crash must fail closed
        _log(f"gate error, denying: {exc}")
        decision = permissions.Decision(False, "error", str(exc))

    # Only log what was actually addressed to us. A bot in a busy server
    # must not write a transcript of every message in every channel to the
    # owner's disk — see permissions.py's note on stage ordering.
    addressed = decision.allowed or decision.stage not in ("reachable", "self", "enabled")
    conv_id = None
    if addressed and cfg.get("log_conversations", True):
        conv_id = _conv_id_for(platform, msg.thread_id,
                               detail=_scope_detail(msg)) if decision.allowed else None
        transcript.log_inbound(platform, msg, decision, conv_id=conv_id)

    if not decision.allowed:
        if decision.stage not in ("reachable", "self", "enabled"):
            _log(f"denied {platform} msg from {msg.user_handle or msg.user_id} "
                 f"at [{decision.stage}]: {decision.reason}")
            if cfg.get("notify_on_denied"):
                try:
                    send(f"Not authorized ({decision.stage}).")
                except Exception:  # noqa: BLE001
                    pass
        return decision

    transcript.note_accepted(platform, msg.user_id)
    _log(f"accepted {platform} msg from {msg.user_handle or msg.user_id} "
         f"(tools={'on' if decision.may_use_tools else 'off'})")

    try:
        result = _ask_jarvis(msg.text, conv_id, decision.may_use_tools,
                             on_tool_call=on_tool_call, platform=platform)
        text = (getattr(result, "text", "") or "").strip()
        provider = getattr(result, "provider", "") or ""
        if not text:
            error = getattr(result, "error", "") or ""
            text = f"{_EMPTY_REPLY} {error}".strip()
    except Exception as exc:  # noqa: BLE001
        _log("ask failed:\n" + traceback.format_exc())
        text = f"Something broke while answering: {exc}"
        provider = ""

    limit = int(cfg.get("max_reply_chars") or 1900)
    sent_ok = True
    for chunk in chunk_text(text, limit):
        try:
            ok = send(chunk)
            sent_ok = sent_ok and (ok is not False)
        except Exception as exc:  # noqa: BLE001
            _log(f"send failed: {exc}")
            sent_ok = False
            if cfg.get("log_conversations", True):
                transcript.log_outbound(platform, msg.thread_id, chunk,
                                        conv_id=conv_id, ok=False,
                                        error=str(exc), provider=provider)
            break
        if cfg.get("log_conversations", True):
            transcript.log_outbound(platform, msg.thread_id, chunk,
                                    conv_id=conv_id, ok=True, provider=provider)

    return decision


def enabled_platforms():
    cfg = channel_config.load_config()
    return [p for p in PLATFORMS if (cfg.get(p) or {}).get("enabled")]
