"""Instagram gateway — `jarvis instagram-serve`.

READ THIS BEFORE CONFIGURING ANYTHING
-------------------------------------
Instagram is not Discord. Four platform rules shape everything here, and
none of them are things this code can work around:

1. **Personal accounts cannot be automated at all.** The Basic Display API
   is end-of-life and has no successor for personal accounts. The account
   must be a **Professional** account (Business or Creator). There is no
   supported path for "link my normal Instagram".

2. **The bot cannot start a conversation.** Meta only permits a reply
   *inside a 24-hour window* opened by the user's own most recent message.
   The clock resets every time they write again. This is why
   `outbound.py` treats an Instagram DM to the owner as best-effort and
   falls back to another channel — see `window_open_for()` below.

3. **The HUMAN_AGENT tag does not rescue that.** It extends the window to
   7 days, but Meta restricts it to messages typed by an actual human, and
   explicitly treats automated use as a policy violation that gets API
   access revoked. So this module never sends it. If you find a library
   that does, that is a liability, not a feature.

4. **Webhooks must be answered within ~5 seconds.** A provider round trip
   plus tool execution takes far longer, so the HTTP handler acknowledges
   immediately and does the real work on a background thread. Blocking the
   response until Jarvis has answered would make Meta retry and eventually
   disable the subscription.

WHAT THIS MEANS PRACTICALLY
    * Someone DMs your Professional account -> Jarvis can answer. Good.
    * Jarvis wants to tell you a task finished, and you last messaged it
      two days ago -> it cannot. Use Discord for outbound, or DM the bot
      to reopen the window. `outbound.py` does this fallback for you.

GROUP CHATS (EXPERIMENTAL, NOT PROMISED)
----------------------------------------
Meta documents the Instagram messaging API as one-to-one only: group threads are not a
supported surface. `_group_info` below still accepts a `thread_key` on an event if one
ever arrives (the shapes this code has always accepted, and nothing more), and such a
message is treated as a group space: the mention rule applies, speaker labels and the
room switches (`channels-space-set`) work, and the 24-hour window is keyed on the
thread. Whether live group events are ever delivered to a webhook is NOT verified, and
nothing here or in the docs claims they are. The reliable way to get a group chat into
Jarvis is an offline importer from Instagram's own data export (planned, S9b; not
in this module yet). An unofficial live transport is a separate, opt-in design (plan
S9c) and is not part of this module either.

TRANSPORT
---------
A stdlib http.server, deliberately: this project's only hard dependency is
`requests`, and adding Flask/FastAPI for one endpoint would be a large
dependency for a small job. Meta requires a public HTTPS URL with a valid
certificate, so in practice this sits behind a tunnel (Cloudflare Tunnel,
ngrok) or a reverse proxy that terminates TLS. It binds 127.0.0.1 by
default for exactly that reason — it is not meant to face the internet
directly.
"""

import hashlib
import hmac
import re
import json
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import INSTAGRAM
from . import config as channel_config
from . import base, permissions, transcript, directory, approvals

GRAPH_HOST = "https://graph.instagram.com"
# Meta's own docs say a webhook must be acknowledged quickly; 5s is the
# documented expectation, so all real work happens off the response path.
_WINDOW_SECONDS = 24 * 60 * 60

# Last inbound message time per user, which is what the 24-hour reply
# window is measured from. Persisted because the sender usually runs in a
# *different process* from the webhook receiver (a tool call or a scheduler
# tick), so an in-memory dict would always look empty to it.
_WINDOW_FILE = transcript.CHANNELS_DIR / "instagram_windows.json"


def _load_windows():
    from .. import atomic_io
    return atomic_io.read_json(_WINDOW_FILE, default={}, expect=dict)


def note_inbound(user_id, when=None):
    """Record that a user messaged us, opening/refreshing their window."""
    windows = _load_windows()
    windows[str(user_id)] = time.time() if when is None else when
    # Atomic: this file is read by a different process than the one that
    # writes it (the sender vs the webhook receiver), so a torn write here
    # reads as "no window open" and silently blocks every outbound DM.
    from .. import atomic_io
    atomic_io.write_json(_WINDOW_FILE, windows)
    return windows[str(user_id)]


def window_open_for(user_id, now=None):
    """(is_open, seconds_remaining). A closed window is not an error state —
    it is the normal condition most of the time, and callers are expected
    to degrade gracefully rather than treat it as a failure."""
    now = time.time() if now is None else now
    last = _load_windows().get(str(user_id))
    if not last:
        return False, 0
    remaining = _WINDOW_SECONDS - (now - last)
    return (remaining > 0), max(0, int(remaining))


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def send_message(user_id, text, cfg=None, thread_key=""):
    """POST one message to the Send API. Returns (ok, error).

    With `thread_key`, addresses a GROUP thread instead of a person: Meta's
    Send API takes either `recipient: {id}` (1:1) or `recipient:
    {thread_key}` (group). The 24-hour window still applies and is still
    measured from the last message *by a participant*, so the window check
    below is keyed on whichever of the two identifies the conversation.

    Never attaches a message tag — see rule 3 in the module docstring.
    """
    cfg = cfg or channel_config.platform_config(INSTAGRAM)
    token = (cfg.get("access_token") or "").strip()
    ig_user_id = (cfg.get("ig_user_id") or "").strip()
    if not token or not ig_user_id:
        return False, "instagram access_token / ig_user_id not configured"

    window_key = thread_key or user_id
    open_now, _ = window_open_for(window_key)
    if not open_now:
        return False, (
            "24-hour messaging window is closed for this user. Instagram only "
            "allows a reply within 24h of their last message, and the bot "
            "cannot open a conversation itself.")

    try:
        import requests
    except ImportError:
        return False, "the 'requests' package is required"

    version = cfg.get("graph_version") or "v21.0"
    url = f"{GRAPH_HOST}/{version}/{ig_user_id}/messages"
    try:
        resp = requests.post(
            url,
            params={"access_token": token},
            json={"recipient": ({"thread_key": str(thread_key)} if thread_key
                                else {"id": str(user_id)}),
                  "message": {"text": text[:900]}},
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"network error: {exc}"

    if resp.status_code >= 400:
        detail = resp.text[:400]
        try:
            payload = resp.json().get("error", {})
            detail = payload.get("message", detail)
        except Exception:  # noqa: BLE001
            pass
        return False, f"HTTP {resp.status_code}: {detail}"
    return True, ""


# ---------------------------------------------------------------------------
# Webhook payload parsing
# ---------------------------------------------------------------------------


def verify_signature(app_secret, raw_body, header_value):
    """Check Meta's X-Hub-Signature-256.

    Returns True when it matches. If no app_secret is configured this
    returns False — an unverified webhook endpoint is an open door that
    anyone who learns the URL can POST arbitrary "messages" to, and those
    messages would be gated only by an allowlist keyed on a sender id the
    caller also controls. Failing closed is the only safe default.
    """
    if not app_secret:
        return False
    if not header_value:
        return False
    header_value = header_value.strip()
    if header_value.startswith("sha256="):
        header_value = header_value[len("sha256="):]
    expected = hmac.new(
        app_secret.encode("utf-8"),
        raw_body if isinstance(raw_body, bytes) else raw_body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    # compare_digest, not ==, so a wrong signature can't be discovered one
    # byte at a time by timing the response.
    return hmac.compare_digest(expected, header_value)


def mentions_bot(text, bot_handle):
    """Did this text @mention us?

    Instagram sends a mention as plain text, not a structured entity the
    way Discord does — there is no mentions array to consult, so this is a
    string match and is honest about being one. Word-boundary anchored so
    "@jarvisbot" does not match a bot handle of "@jarvis", which would let
    an unrelated account's name activate the bot.
    """
    handle = str(bot_handle or "").strip().lstrip("@")
    if not handle or not text:
        return False
    return re.search(r"@" + re.escape(handle) + r"\b", text, re.IGNORECASE) is not None


def _attachment_descriptors(message):
    """Plain descriptors of the pictures a DM carries (never the bytes), capped at 10.

    Meta puts them at ``message.attachments[]`` as ``{"type": "image", "payload": {"url": ...}}``.
    Only ``image`` entries are reported (a story reply, a reel share, audio or a sticker is not a
    picture Jarvis can read); media_in.py decides what, if anything, is fetched -- it will only
    fetch https URLs on Meta's own CDN hosts. Never raises."""
    out = []
    try:
        for i, a in enumerate(list((message or {}).get("attachments") or [])[:10], 1):
            if not isinstance(a, dict) or str(a.get("type") or "").lower() != "image":
                continue
            url = str((a.get("payload") or {}).get("url") or "")
            if url:
                out.append({"url": url, "filename": "instagram-%d.jpg" % i,
                            "content_type": "image/jpeg", "size": 0})
    except Exception:  # noqa: BLE001
        pass
    return out


_GROUP_NOTED = False


def _group_info(event, entry, sender):
    """(thread_key, is_group) for one `messaging` event.

    Meta marks a group thread with a thread_key on the event, on a `thread` object, or
    on the entry; a 1:1 has none and the sender alone identifies the conversation. The
    shapes accepted are exactly the ones the inline code accepted before it was moved
    here, and nothing new. A missing key, or a key equal to the sender, degrades to "treat
    it as a DM", the safe direction: DMs are gated by dm_allowlist, so an unexpected shape
    fails closed rather than skipping the mention rule.

    The first group event this process sees writes ONE log line saying so: Meta documents
    group messaging as unsupported, so a group payload is news worth noticing."""
    global _GROUP_NOTED
    event = event if isinstance(event, dict) else {}
    entry = entry if isinstance(entry, dict) else {}
    thread = event.get("thread")
    thread_key = str(
        event.get("thread_key")
        or (thread.get("id") if isinstance(thread, dict) else "")
        or entry.get("thread_key")
        or "")
    is_group = bool(thread_key) and thread_key != str(sender or "")
    if is_group and not _GROUP_NOTED:
        _GROUP_NOTED = True
        try:
            base._log("instagram group payload received; Meta documents group "
                      "messaging as unsupported, treating as experimental")
        except Exception:  # noqa: BLE001 -- a log line is never worth a failure
            pass
    return thread_key, is_group


def parse_events(payload, bot_id="", bot_handle=""):
    """Turn one webhook body into IncomingMessages.

    Handles the `messages` field (a DM) and `mentions` (a comment or story
    that @-mentions the account). Everything else — reactions, read
    receipts, echoes of our own sends — is ignored rather than guessed at.
    """
    out = []
    if not isinstance(payload, dict):
        return out

    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue

        # --- DMs -----------------------------------------------------
        for event in entry.get("messaging") or []:
            if not isinstance(event, dict):
                continue
            message = event.get("message") or {}
            # is_echo marks our own outbound message being reflected back.
            # Without this check the bot answers itself in a loop.
            if message.get("is_echo"):
                continue
            sender = str((event.get("sender") or {}).get("id") or "")
            if not sender or (bot_id and sender == str(bot_id)):
                continue
            text = (message.get("text") or "").strip()
            attachments = _attachment_descriptors(message)
            if not text and attachments:
                # A picture on its own is not nothing: say what it is, so the transcript and the
                # model see something true (media_in adds the description, if the sender may).
                text = "(sent a picture)"
            if not text:
                continue  # a sticker / reaction with no caption — nothing to answer

            # Group vs 1:1: see _group_info (experimental; fails closed to a DM).
            thread_key, is_group = _group_info(event, entry, sender)

            out.append(permissions.IncomingMessage(
                platform=INSTAGRAM,
                context=permissions.CTX_GROUP if is_group else permissions.CTX_DM,
                user_id=sender,
                user_handle=str((event.get("sender") or {}).get("username") or ""),
                text=text,
                # A 1:1 DM is inherently addressed to us. In a group it is
                # not — the same @mention rule Discord uses applies, via a
                # text match since Instagram has no mention entities.
                mentioned=(mentions_bot(text, bot_handle) if is_group else True),
                message_id=str(message.get("mid") or ""),
                channel_id=thread_key,
                thread_id=thread_key or sender,
                raw=event,
                attachments=attachments,
            ))

        # --- @mentions in comments / stories --------------------------
        for change in entry.get("changes") or []:
            if not isinstance(change, dict) or change.get("field") != "mentions":
                continue
            value = change.get("value") or {}
            sender = str(value.get("from", {}).get("id") or value.get("user_id") or "")
            text = (value.get("text") or "").strip()
            if not sender or not text:
                continue
            out.append(permissions.IncomingMessage(
                platform=INSTAGRAM,
                context=permissions.CTX_GROUP,
                user_id=sender,
                user_handle=str(value.get("from", {}).get("username") or ""),
                text=text,
                mentioned=True,
                message_id=str(value.get("comment_id") or value.get("media_id") or ""),
                # Its own thread: a comment/story mention used to share the
                # sender's DM transcript file and conversation. Only the
                # STORAGE key changes -- see _window_and_target for where
                # the reply goes.
                thread_id=MENTION_THREAD_PREFIX + sender,
                raw=change,
            ))
    return out


MENTION_THREAD_PREFIX = "mention-"


def _window_and_target(msg):
    """(24h-window key, thread_key to send into) for one parsed message.

    A DM is keyed on the person and sent by id (no thread_key). A group is
    keyed and sent by its thread. A comment/story mention is `context=group`
    with a storage-only thread id (`mention-<sender>`, no channel id); its
    window and send target are exactly what they were before that id
    existed: the sender. Keeping this in one place is what stops the storage
    key from leaking into what actually gets posted."""
    if msg.context != permissions.CTX_GROUP:
        return msg.user_id, ""
    if not msg.channel_id and str(msg.thread_id).startswith(MENTION_THREAD_PREFIX):
        return msg.user_id, msg.user_id
    return msg.thread_id, msg.thread_id


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = "JarvisInstagram/1.0"

    # BaseHTTPRequestHandler logs every request to stderr in Apache format,
    # which buries the [channels] trace lines. Routed through base._log so
    # everything this package prints has one shape.
    def log_message(self, fmt, *args):  # noqa: A003
        base._log("http " + (fmt % args))

    def _respond(self, code, body=b"", content_type="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):  # noqa: N802 — stdlib naming
        """Meta's subscription handshake: echo hub.challenge back if the
        verify token matches the one we configured."""
        cfg = channel_config.platform_config(INSTAGRAM)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != (cfg.get("webhook_path") or "/webhook"):
            return self._respond(404, b"not found")

        params = urllib.parse.parse_qs(parsed.query)
        mode = (params.get("hub.mode") or [""])[0]
        token = (params.get("hub.verify_token") or [""])[0]
        challenge = (params.get("hub.challenge") or [""])[0]
        expected = (cfg.get("verify_token") or "").strip()

        if mode == "subscribe" and expected and hmac.compare_digest(token, expected):
            base._log("webhook verification succeeded")
            return self._respond(200, challenge.encode("utf-8"))
        base._log("webhook verification FAILED (verify_token mismatch)")
        return self._respond(403, b"verification failed")

    def do_POST(self):  # noqa: N802
        cfg = channel_config.platform_config(INSTAGRAM)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != (cfg.get("webhook_path") or "/webhook"):
            return self._respond(404, b"not found")

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b""

        if not verify_signature(cfg.get("app_secret"),
                                raw,
                                self.headers.get("X-Hub-Signature-256")):
            base._log("rejected webhook POST: bad or missing X-Hub-Signature-256")
            return self._respond(403, b"bad signature")

        # Acknowledge FIRST. Meta expects a fast 200 and retries otherwise;
        # answering a message takes seconds to minutes.
        self._respond(200, b"ok")

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            base._log(f"unparseable webhook body: {exc}")
            return

        threading.Thread(target=_process_payload, args=(payload, cfg),
                         daemon=True).start()


def _fetch_username(user_id, cfg):
    """Look up a sender's current username via the Graph API.

    Necessary because Instagram's DM webhook usually sends ONLY
    `sender: {id}` — no username at all — unlike Discord, where
    discord.py hands you the author's name for free on every event. So
    "@handle resolves to an id" only works on Instagram if something
    actively fetches the handle; parse_events() alone reads a field Meta
    typically never populates, which is why a handle-based allowlist
    entry never matched anything even though messages were arriving.

    Best-effort: a failure here must never block answering the message
    itself, so it returns "" rather than raising, and channels-directory
    simply won't know this person's handle until a later message succeeds.
    """
    token = str(cfg.get("access_token") or "").strip()
    if not token or not user_id:
        return ""
    try:
        import requests
    except ImportError:
        return ""
    version = cfg.get("graph_version") or "v21.0"
    try:
        resp = requests.get(
            f"{GRAPH_HOST}/{version}/{user_id}",
            params={"fields": "username", "access_token": token},
            timeout=10,
        )
        if resp.status_code >= 400:
            return ""
        return str(resp.json().get("username") or "")
    except Exception:  # noqa: BLE001
        return ""


def _fetch_profile_pic(user_id, cfg):
    """Best-effort profile-picture URL for the Channels panel ("" on any
    failure). Separate from _fetch_username so a missing picture field or
    permission can never break username lookup. people.py re-validates the
    host against its image-CDN allow-list before storing it."""
    token = str(cfg.get("access_token") or "").strip()
    if not token or not user_id:
        return ""
    try:
        import requests
        version = cfg.get("graph_version") or "v21.0"
        resp = requests.get(
            f"{GRAPH_HOST}/{version}/{user_id}",
            params={"fields": "profile_pic", "access_token": token},
            timeout=10,
        )
        if resp.status_code >= 400:
            return ""
        return str(resp.json().get("profile_pic") or "")
    except Exception:  # noqa: BLE001
        return ""


def _process_payload(payload, cfg):
    """Handle one webhook body off the response path.

    Traces every parsed message with its sender id — this is the ONLY
    place that id is ever printed, and it is how `jarvis channels-whoami`
    tells someone to find their IGSID (there is no username lookup on
    Instagram; the id only exists once they've messaged the bot). Without
    this line, "jarvis instagram-serve" showing `POST /webhook 200` with
    nothing else looks identical whether the message was received and
    silently dropped, or never reached parse_events at all — that
    ambiguity is exactly what made this undiagnosable before.
    """
    try:
        messages = parse_events(payload, bot_id=cfg.get("ig_user_id"),
                                bot_handle=cfg.get("bot_handle"))
    except Exception as exc:  # noqa: BLE001
        base._log(f"could not parse webhook payload: {exc}")
        return

    if not messages:
        # The single most common cause: Meta delivered something other than
        # a message (a read receipt, a reaction, an echo of our own send) —
        # all of which parse_events correctly ignores. Rarer cause: the
        # payload shape didn't match what parse_events expects at all. This
        # line is what tells the two apart instead of both looking like
        # nothing happened.
        base._log("webhook delivered no actionable message "
                  "(a read receipt/reaction/echo, most likely — "
                  "raw payload logged at debug level below)")
        base._log(f"raw payload: {json.dumps(payload)[:500]}")
        return

    for msg in messages:
        if not msg.user_handle:
            # This is the fix: the webhook itself almost never carries a
            # username, so without an active fetch here `@handle` would
            # never resolve to anything no matter how many messages came in.
            msg.user_handle = _fetch_username(msg.user_id, cfg)
        if msg.user_handle:
            directory.record(INSTAGRAM, msg.user_handle, msg.user_id)
        if not msg.avatar:
            msg.avatar = _fetch_profile_pic(msg.user_id, cfg)
        base._log(f"instagram message from user_id={msg.user_id!r} "
                  f"handle={msg.user_handle!r} context={msg.context} "
                  f"thread={msg.thread_id!r} text={msg.text[:80]!r}")
        # Record the window BEFORE gating: their message opens the reply
        # window regardless of whether we are allowed to answer it, and a
        # denied sender who is later allowlisted should not have lost it.
        # The window is keyed on the conversation, not the person: in a
        # group thread any participant's message reopens it for the thread.
        window_key, send_thread = _window_and_target(msg)
        note_inbound(window_key)

        def send(text, _user=msg.user_id, _thread=send_thread):
            ok, err = send_message(_user, text, cfg=cfg, thread_key=_thread)
            if not ok:
                raise RuntimeError(err)
            return True

        def ask_approval(request_id, text, seconds, _user=msg.user_id):
            """Post the exact action and ask for a whole-message Y or N (L.29 / L.42.1).

            Always to the owner's own DM (never into a group thread). The 24-hour
            window applies: when it is closed send_message refuses, and raising here
            is a denial that says why."""
            body = (text + "\n\nReply Y to run it or N to cancel. Only a reply of just Y or N "
                    "counts; %d seconds, then it is denied." % int(seconds))
            ok, err = send_message(_user, body, cfg=cfg)
            if not ok:
                raise RuntimeError(err)
            return None

        base.handle_message(INSTAGRAM, msg, send, cfg=cfg,
                            ask_approval=ask_approval, approval_by_text=True)


def run():
    """Entry point for `jarvis instagram-serve`. Returns an exit code."""
    cfg = channel_config.platform_config(INSTAGRAM)
    if not cfg.get("enabled"):
        print("instagram channel is disabled. Enable it with:\n"
              "  jarvis channels-set instagram enabled true", file=sys.stderr)
        return 1

    missing = [key for key in ("access_token", "ig_user_id", "app_secret", "verify_token")
               if not (cfg.get(key) or "").strip()]
    if missing:
        print("instagram is not fully configured — missing: "
              + ", ".join(missing)
              + f"\nEdit {channel_config.CONFIG_FILE}, or open the web UI's "
                "Guides panel for the full setup walkthrough.", file=sys.stderr)
        return 1

    # A restart cancels every pending approval (Q-L42h).
    try:
        dropped = approvals.cancel_pending(INSTAGRAM)
        if dropped:
            base._log(f"cancelled {dropped} pending approval(s) left from before the restart")
    except Exception as exc:  # noqa: BLE001
        base._log(f"could not clear pending approvals: {exc}")

    host = cfg.get("webhook_host") or "127.0.0.1"
    port = int(cfg.get("webhook_port") or 19824)
    path = cfg.get("webhook_path") or "/webhook"

    try:
        server = HTTPServer((host, port), _Handler)
    except OSError as exc:
        print(f"could not bind {host}:{port} — {exc}", file=sys.stderr)
        return 1

    base._log(f"instagram webhook listening on http://{host}:{port}{path}")
    base._log("Meta requires a public HTTPS callback — point a tunnel or "
              "reverse proxy at this address.")
    print(permissions.describe(cfg), file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        base._log("stopping")
    finally:
        server.server_close()
    return 0
