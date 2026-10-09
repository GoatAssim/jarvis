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

import json
import os
import sys
import threading
import time
import traceback

from . import PLATFORMS
from . import config as channel_config
from . import dedupe, people, permissions, transcript, usage, user_perms

# Serializes ask() calls so the JARVIS_ALLOWED_TOOLS mutation below is safe.
_ASK_LOCK = threading.Lock()

# L.28 #5: an ask that never finished and left no row at all (an owner message
# went unanswered for 4+ minutes and the logs showed a request, then silence).
# The cause is unknown (a hung HTTP call with no overall deadline, a stuck
# tool, or a killed process all look identical), and because asks are
# serialized under _ASK_LOCK one stuck ask silently blocks every other chat.
# This does not guess at a cause or kill anything: it makes the stall
# visible, once per STALL_LOG_SECONDS, with what the ask was last doing.
STALL_LOG_SECONDS = 90.0
LOCK_WAIT_LOG_SECONDS = 5.0

# Who sent the message currently being answered, as JSON, for tools that
# need the *current* sender rather than one named in their arguments (see
# actions/channel_people.py). Same env-var-under-a-lock mechanism as
# JARVIS_ALLOWED_TOOLS — see this module's docstring for why that is the
# shape rather than a threaded-through parameter.
SENDER_ENV = "JARVIS_CHANNEL_SENDER"

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
        name = getattr(msg, "guild_name", "")
        bits.append(f"server {name} ({msg.guild_id})" if name else f"guild {msg.guild_id}")
    if msg.channel_id and msg.channel_id != msg.guild_id:
        name = getattr(msg, "channel_name", "")
        bits.append(f"channel #{name} ({msg.channel_id})" if name else f"channel {msg.channel_id}")
    who = msg.user_handle or msg.user_id
    if who:
        bits.append(f"with {who}")
    return " / ".join(bits)


def _ask_jarvis(text, conv_id, may_use_tools, on_tool_call=None, platform="",
                sender_context="", sender_env="", tool_scope=None, tool_log=None,
                collect_media=False, media_log=None):
    """One ask, with tools allowed or forbidden for this specific sender.

    Imported lazily: ai_client pulls in the whole tool catalog, and a
    gateway that fails to start should fail on its own missing SDK rather
    than on an unrelated import several layers down.

    `sender_context` is the identity block for whoever sent this message
    (see channels/people.prompt_block) and rides in the system prompt.
    `sender_env` is the same person as a JSON blob in JARVIS_CHANNEL_SENDER,
    for tools that need to act on the *current* sender — remember_sender
    has no other way to know whose name it was just told, since a tool
    handler receives only its own arguments. Both are set and restored
    under the same _ASK_LOCK as JARVIS_ALLOWED_TOOLS, for exactly the same
    reason: os.environ is process-global and this process handles several
    people's messages.

    `tool_scope` narrows a sender who MAY use tools to a named set of them
    (user_perms.effective_tool_scope): None leaves the ask exactly as it was,
    a frozenset becomes the JARVIS_ALLOWED_TOOLS allowlist for this ask. It is
    the same variable, set and restored under the same lock, as the
    tools-off case — one enforcement point (tools.run_tool refuses anything
    not in it), no second mechanism to drift. It can only ever SHRINK what
    `may_use_tools` allows: a sender with tools off stays at "none" whatever
    the scope says.

    `tool_log`, if a list, collects {"name", "arguments"} for every tool the
    model calls during this ask, in order, for the Conversation view. It is
    filled from the same callback the stall watchdog already uses, so it adds
    no new hook into ai_client.

    `collect_media` opens jarvis.media_out's collector for this ask (the
    gateway passes True for the OWNER only); pictures tools made are appended to
    `media_log` as (path, kind). Opened and closed inside the same lock as the
    environment above, so one person's screenshot can never be collected into
    the next person's reply.
    """
    from .. import ai_client, commands_config, logs

    try:
        commands = commands_config.load_commands_dict()
    except Exception:  # noqa: BLE001 — a broken commands file must not block a reply
        commands = {}

    def _note(message):
        """stderr + one error row in this conversation's log. Never raises."""
        try:
            _log(message)
            logs.log(conv_id, "error", {"error": message}, provider="watchdog")
        except Exception:  # noqa: BLE001 — a diagnostic must never break an ask
            pass

    queued_at = time.time()
    with _ASK_LOCK:
        queued = time.time() - queued_at
        if queued >= LOCK_WAIT_LOG_SECONDS:
            _note(f"this ask waited {queued:.0f}s for the ask lock - another "
                  "chat's ask was still running")
        previous = os.environ.get("JARVIS_ALLOWED_TOOLS")
        previous_source = os.environ.get("JARVIS_LOG_SOURCE")
        previous_sender = os.environ.get(SENDER_ENV)
        if sender_env:
            os.environ[SENDER_ENV] = sender_env
        else:
            os.environ.pop(SENDER_ENV, None)
        if platform:
            # Tags every log entry this ask writes, matching what the
            # scheduler does for job runs (see scheduler.py). Belt and
            # braces with the conversation-level origin above: the
            # conversation label survives even if this env var is lost,
            # and the entry label survives even if a chat ask somehow
            # lands in a pre-existing conversation.
            os.environ["JARVIS_LOG_SOURCE"] = platform
        # What to write into JARVIS_ALLOWED_TOOLS for this ask, or None to
        # leave it alone. Tools off always wins over any per-person scope.
        restrict = None
        if not may_use_tools:
            # Empty string -> empty allowlist -> no tool reaches the model.
            # Note this is NOT the same as unsetting it (None = unrestricted).
            restrict = ""
        elif tool_scope is not None:
            restrict = ",".join(sorted(str(t) for t in tool_scope))
        if restrict is not None:
            os.environ["JARVIS_ALLOWED_TOOLS"] = restrict
        progress = {"attempt": "", "tool": ""}
        finished = threading.Event()

        def _on_attempt(label):
            progress["attempt"] = str(label)

        def _on_tool_call(*args, **kwargs):
            progress["tool"] = str(args[0]) if args else ""
            if tool_log is not None and args:
                try:
                    tool_log.append({"name": str(args[0]),
                                     "arguments": args[1] if len(args) > 1 else None})
                except Exception:  # noqa: BLE001 -- a log must never break a tool call
                    pass
            if on_tool_call:
                return on_tool_call(*args, **kwargs)
            return None

        def _watch():
            started = time.time()
            while not finished.wait(STALL_LOG_SECONDS):
                _note(f"ask still running after {time.time() - started:.0f}s with "
                      f"no answer - last provider attempt: "
                      f"{progress['attempt'] or '(none yet)'}; last tool: "
                      f"{progress['tool'] or '(none)'}. Every other chat is "
                      "waiting behind it.")

        threading.Thread(target=_watch, name="ask-watchdog", daemon=True).start()
        from .. import media_out
        media_out.begin(allow=collect_media)
        try:
            result = ai_client.ask(
                text,
                commands=commands,
                conversation_id=conv_id,
                on_attempt=_on_attempt,
                on_tool_call=_on_tool_call,
                sender_context=sender_context,
            )
        finally:
            finished.set()
            collected = media_out.end()
            if media_log is not None:
                media_log.extend(collected)
            # Restore exactly, including the "wasn't set at all" case —
            # leaving an empty string behind would silently disable tools
            # for every later message in this process.
            if restrict is not None:
                if previous is None:
                    os.environ.pop("JARVIS_ALLOWED_TOOLS", None)
                else:
                    os.environ["JARVIS_ALLOWED_TOOLS"] = previous
            # Restored unconditionally, unlike the tools var above: leaving
            # one person's identity in the environment would hand the NEXT
            # sender's remember_sender call to the previous sender's record.
            if previous_sender is None:
                os.environ.pop(SENDER_ENV, None)
            else:
                os.environ[SENDER_ENV] = previous_sender
            if platform:
                if previous_source is None:
                    os.environ.pop("JARVIS_LOG_SOURCE", None)
                else:
                    os.environ["JARVIS_LOG_SOURCE"] = previous_source
    return result


def _utc_stamp():
    """Same format raw_archive stamps its events with, so the two compare as
    plain strings."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _tool_calls_for_log(captured, conv_id, since):
    """The tool calls of ONE finished ask, shaped for the transcript.

    The model-facing callback only knows a call's name and arguments. The
    outcome lives in the raw event log (raw_archive.py, which keeps every tool
    run at full size), so when that log has runs for this conversation since
    `since` they are the source: name, arguments AND result. When it does not
    (archive switched off, no conversation id) the captured calls are used
    with no outcome rather than dropping them. Never raises."""
    try:
        events = []
        if conv_id:
            from .. import raw_archive
            events = [e for e in raw_archive.read(conv_id, ["tool_run"])
                      if str(e.get("ts") or "") >= since]
        if events:
            return [transcript.tool_call_record(e.get("name"), e.get("arguments"),
                                                e.get("result"), e.get("confirm"))
                    for e in events[:transcript.MAX_TOOL_CALLS]]
        return [transcript.tool_call_record(c.get("name"), c.get("arguments"))
                for c in (captured or [])[:transcript.MAX_TOOL_CALLS]]
    except Exception:  # noqa: BLE001 -- the audit log must never break a reply
        return []


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


def _notify_owner_new_sender(platform, msg, entry):
    """Tell the owner, once, that a new person has written in.

    Fired from the pipeline rather than offered as a tool on purpose. The
    model deciding whether to mention a stranger is exactly the decision
    you don't want delegated to the thing the stranger is talking to — a
    "please don't tell anyone about this conversation" in the first
    message is all it would take. This runs before the model sees the
    text at all.

    Best-effort in the strongest sense: it is wrapped whole, and a failure
    is logged and dropped. The owner not receiving a courtesy ping must
    never be the reason a message goes unanswered. notify_owner() also
    records to the local notifier inbox, so an undelivered ping is still
    visible in `jarvis notify-list`.

    Returns True if the owner was pinged (or at least attempted), so the
    caller knows whether to mark the person as notified.
    """
    try:
        from . import outbound

        who = entry.get("handle") or entry.get("user_id") or "someone"
        preview = " ".join((msg.text or "").split())[:160]
        where = "DM" if msg.context == permissions.CTX_DM else "a group chat"
        lines = [
            f"New person on {platform}: @{who} (id {entry.get('user_id')}) "
            f"just messaged you in {where}.",
        ]
        if preview:
            lines.append(f'They said: "{preview}"')
        lines.append(
            f"Want me to follow them? Approve with:  jarvis channels-follow "
            f"{platform} {entry.get('user_id')}   /   ignore with:  jarvis "
            f"channels-block {platform} {entry.get('user_id')}"
        )
        outbound.notify_owner("\n".join(lines), platforms=None,
                              first_success_only=True)
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"could not notify owner about new {platform} sender: {exc}")
        return False


def gate(platform, msg, cfg=None):
    """Run the permission gate ALONE, answering nothing.

    Exists so a gateway can find out whether a message is even for us
    *before* doing anything the sender can see. permissions.decide() is
    pure and cheap — no network, no SDK, no model, and one small
    last-accepted read — so asking first costs nothing.

    That matters because an acknowledgement is not free the way a log line
    is. A bot sitting in a busy server that reacts 👀 and opens a typing
    indicator on every message it can see, then goes silent when the gate
    denies it, is indistinguishable from a broken bot — and it does that
    to every unrelated conversation in the channel. See the note on stage
    ordering in permissions.py: if it wasn't addressed to us, we never saw
    it, and that has to include what other people can observe.

    Fails closed, and logs, exactly as handle_message's inline gate did.
    """
    cfg = cfg or channel_config.platform_config(platform)
    try:
        return permissions.decide(
            cfg, msg,
            last_seen_at=transcript.last_accepted(platform, msg.user_id),
        )
    except Exception as exc:  # noqa: BLE001 — a gate crash must fail closed
        _log(f"gate error, denying: {exc}")
        return permissions.Decision(False, "error", str(exc))


def addressed_to_us(decision):
    """Was this message aimed at us at all, however it was later judged?

    The three stages below mean "this was never ours to answer" — a
    message in a channel that didn't mention us, our own echo, a disabled
    platform. Everything else (dm_allowed, reply, cooldown, error) means
    someone did address us and was refused, which is worth logging and may
    be worth acknowledging.

    Hoisted out of handle_message so a gateway can apply the exact same
    rule to its own visible acknowledgements rather than re-deriving a
    second, subtly different list of stage names.
    """
    return decision.allowed or decision.stage not in ("reachable", "self", "enabled")


def handle_message(platform, msg, send, cfg=None, on_tool_call=None, decision=None,
                   send_file=None):
    """Run one inbound message all the way through.

    `send(text)` is the gateway's own sender, called once per chunk. It
    should return True on success. Returns the Decision, so a gateway can
    react to a denial (add a reaction, say nothing) without re-running the
    gate itself.

    `decision`, if given, is a Decision a caller already obtained from
    gate() for this same message — passed in rather than recomputed so a
    gateway that had to know the verdict early (to decide whether to show
    a read receipt at all) doesn't run the gate twice. Omitting it keeps
    the original behaviour exactly: the gate runs here.

    `send_file(path, name)`, if the gateway can attach files, sends one file to
    the thread this message came from (never to anyone else). Pictures a tool
    made during the ask -- a screenshot -- go through it AFTER the text, and only
    for the owner (channels/media_send.py). Omitted: nothing is attached, as before.

    Never raises: an exception anywhere in here is logged and swallowed, on
    the principle that one malformed message must not kill a gateway that
    is holding a WebSocket open for everyone else.
    """
    cfg = cfg or channel_config.platform_config(platform)

    if decision is None:
        decision = gate(platform, msg, cfg)

    # Only log what was actually addressed to us. A bot in a busy server
    # must not write a transcript of every message in every channel to the
    # owner's disk — see permissions.py's note on stage ordering.
    addressed = addressed_to_us(decision)
    conv_id = None
    if addressed and cfg.get("log_conversations", True):
        conv_id = _conv_id_for(platform, msg.thread_id,
                               detail=_scope_detail(msg)) if decision.allowed else None
        transcript.log_inbound(platform, msg, decision, conv_id=conv_id)

    if not decision.allowed:
        # Register anyone who actually addressed us (a DM or an @mention) even
        # though the gate refused to answer them. Without this, a person who
        # DMs the bot before the owner has allowed them never appears in the
        # Channels panel, so there is nothing to click to let them in. This
        # only creates/refreshes the record: it grants nothing, answers
        # nothing and does NOT ping the owner. Unaddressed chatter in a busy
        # server is excluded by addressed_to_us().
        if addressed:
            try:
                people.touch(platform, msg.user_id, handle=msg.user_handle,
                             is_owner=permissions.is_owner(cfg, msg),
                             avatar=getattr(msg, "avatar", ""))
            except Exception as exc:  # noqa: BLE001
                _log(f"could not register denied sender: {exc}")
        if decision.stage not in ("reachable", "self", "enabled"):
            _log(f"denied {platform} msg from {msg.user_handle or msg.user_id} "
                 f"at [{decision.stage}]: {decision.reason}")
            if cfg.get("notify_on_denied"):
                try:
                    send(f"Not authorized ({decision.stage}).")
                except Exception:  # noqa: BLE001
                    pass
        return decision

    # --- redelivery guard --------------------------------------------------
    # Placed AFTER the gate and BEFORE anything with a side effect. After,
    # because a denied message should not consume a dedupe slot; before,
    # because everything below this line either costs tokens or actually
    # does something. See channels/dedupe.py for why redelivery happens on
    # both platforms and why this is a file rather than a set.
    if dedupe.already_seen(platform, msg.message_id):
        _log(f"ignoring duplicate {platform} message {msg.message_id} "
             f"from {msg.user_handle or msg.user_id}")
        return decision

    transcript.note_accepted(platform, msg.user_id)

    # --- who is this? -----------------------------------------------------
    # Only for messages that got past the gate, so a stranger who was
    # denied never creates a record — the "if it wasn't for us, we never
    # saw it" rule from permissions.py applies to this store too.
    sender_ctx = ""
    sender_env = ""
    is_owner = False
    try:
        is_owner = permissions.is_owner(cfg, msg)
        entry = people.touch(platform, msg.user_id,
                             handle=msg.user_handle, is_owner=is_owner,
                             avatar=getattr(msg, "avatar", ""))
        sender_ctx = people.prompt_block(entry, platform)
        sender_env = json.dumps({
            "platform": platform,
            "user_id": entry.get("user_id") or msg.user_id,
            "handle": entry.get("handle") or msg.user_handle,
            "name": entry.get("name") or "",
            "is_owner": bool(is_owner),
        })
        if people.needs_owner_notice(entry):
            if _notify_owner_new_sender(platform, msg, entry):
                people.mark_notified(platform, msg.user_id)
    except Exception as exc:  # noqa: BLE001
        # Identity is an enhancement to the answer, never a precondition
        # for one. A broken people.json degrades to the old behaviour (no
        # identity block) rather than dropping the message.
        _log(f"sender identity unavailable: {exc}")

    # --- which tools, exactly? --------------------------------------------
    # The per-person narrowing from the Channels panel. It is looked up only
    # when tools are already permitted (there is nothing to narrow
    # otherwise), and it fails CLOSED: an unreadable user_perms.json means
    # "this person's limits are unknown", so this one message runs with no
    # tools rather than with the whole catalogue.
    # The decision itself lives in user_perms.resolve_tool_access so the
    # panel's dry run ("Test as this person") asks the same function.
    may_use_tools, tool_scope, tools_label, problem = (
        user_perms.resolve_tool_access(platform, msg.user_id,
                                       decision.may_use_tools))
    if problem:
        _log(problem)
    # L.36-P6: a time-limited grant that has run out. This message already
    # runs without tools (resolve_tool_access said so); now take the person
    # off the tool list so the panel and the next gate decision agree. The
    # dry run ("Test as this person") never gets here, so it still writes
    # nothing. Never raises: cleaning up is not why a reply fails.
    if tools_label == user_perms.LABEL_EXPIRED:
        try:
            from . import user_admin
            user_admin.expire_due(platform, msg.user_id)
        except Exception as exc:  # noqa: BLE001
            _log(f"could not end an expired tool grant: {exc}")

    _log(f"accepted {platform} msg from {msg.user_handle or msg.user_id} "
         f"({'owner' if is_owner else 'guest'}, tools={tools_label})")

    ask_ok, ask_error, result = True, "", None
    captured_tools, ask_since = [], _utc_stamp()
    media_items = []
    try:
        result = _ask_jarvis(msg.text, conv_id, may_use_tools,
                             on_tool_call=on_tool_call, platform=platform,
                             sender_context=sender_ctx, sender_env=sender_env,
                             tool_scope=tool_scope, tool_log=captured_tools,
                             collect_media=bool(send_file and is_owner),
                             media_log=media_items)
        text = (getattr(result, "text", "") or "").strip()
        provider = getattr(result, "provider", "") or ""
        if not text:
            error = getattr(result, "error", "") or ""
            text = f"{_EMPTY_REPLY} {error}".strip()
            ask_ok, ask_error = False, error or "empty answer"
    except Exception as exc:  # noqa: BLE001
        _log("ask failed:\n" + traceback.format_exc())
        text = f"Something broke while answering: {exc}"
        provider = ""
        ask_ok, ask_error = False, str(exc)

    # L.36-P2: one counts-only line per finished ask, attributed to the person
    # who sent it (see channels/usage.py). Never raises.
    usage.record(platform, msg.user_id, thread_id=msg.thread_id, conv_id=conv_id,
                 provider=provider, ok=ask_ok,
                 usage_total=getattr(result, "usage_total", None),
                 tool_names=usage.tools_from_result(result),
                 tools_mode=tools_label, error=ask_error)

    # The tool calls behind this reply ride on its FIRST logged line (see
    # transcript.py's "Tool calls shown in the Conversation view"). Only built
    # when conversations are being logged at all.
    pending_tools = (_tool_calls_for_log(captured_tools, conv_id, ask_since)
                     if cfg.get("log_conversations", True) else [])

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
                                        error=str(exc), provider=provider,
                                        to_user=msg.user_id,
                                        tool_calls=pending_tools)
                pending_tools = []
            break
        if cfg.get("log_conversations", True):
            transcript.log_outbound(platform, msg.thread_id, chunk,
                                    conv_id=conv_id, ok=True, provider=provider,
                                    to_user=msg.user_id,
                                    tool_calls=pending_tools)
            pending_tools = []

    # --- pictures the tools made (L.42.3) --------------------------------------
    # After the text, owner only, this thread only, nothing arbitrary: see
    # channels/media_send.py. A failure here must not turn a delivered reply
    # into an error, so it is logged and the person is told in words.
    if media_items and send_file and is_owner:
        _send_media(platform, msg, send, send_file, media_items, cfg, conv_id, provider)

    return decision


def _send_media(platform, msg, send, send_file, items, cfg, conv_id, provider):
    """Send collected files to the asking thread; say so for any that were not."""
    from . import media_send
    files, notes, temps = media_send.prepare(items, True, cfg, platform=platform,
                                                  thread_id=msg.thread_id)
    log_on = cfg.get("log_conversations", True)
    try:
        for path, name in files:
            try:
                send_file(path, name)
                if log_on:
                    transcript.log_outbound(platform, msg.thread_id, "[sent file: %s]" % name,
                                            conv_id=conv_id, ok=True, provider=provider,
                                            to_user=msg.user_id)
            except Exception as exc:  # noqa: BLE001
                _log("could not send %s: %s" % (name, exc))
                notes.append("%s could not be sent (%s)." % (name, exc))
        if notes:
            try:
                send(" ".join(notes)[:1900])
            except Exception as exc:  # noqa: BLE001
                _log("could not send the file notes: %s" % exc)
    finally:
        media_send.cleanup(temps)


def enabled_platforms():
    cfg = channel_config.load_config()
    return [p for p in PLATFORMS if (cfg.get(p) or {}).get("enabled")]
