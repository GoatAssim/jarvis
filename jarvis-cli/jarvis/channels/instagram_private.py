"""Unofficial live transport for Instagram GROUP chats — `jarvis instagram-group-serve`.

READ THIS BEFORE TURNING IT ON (channel spaces S9c, owner decision D1 = "both")
------------------------------------------------------------------------------
Meta documents group messaging as UNSUPPORTED on both Instagram messaging APIs, so
the official webhook (instagram_gateway.py) can never deliver a live group chat.
This module is the other route the owner chose: it logs in as an ordinary Instagram
account through Instagram's PRIVATE, undocumented mobile API and reads and answers
group threads by polling. That goes OUTSIDE Meta's official API and OUTSIDE
Instagram's terms of use. The account can be challenged, rate-limited, locked or
banned, and the private API can change or break without notice. Use a DEDICATED
account, never your main one. It is OFF by default and removable: delete this file,
its tests and the two commands and nothing else changes.

What it does, and what it does not
    * It is the ONLY file that is allowed to import a private-API client library,
      and it imports none at module level: the client comes from `make_client()`
      (see "THE CLIENT" below). `jarvis.channels` never imports this module, so a
      normal start loads no private client at all.
    * Everything it reads goes through the same pipeline as every other message:
      `base.gate` (permissions.decide), `base.handle_message`, speaker labels (S4),
      recall rules (S6), ambient room context (S6a), the per-room switches (S7) and
      `dedupe`. There is no second code path for permissions.
    * It polls ONLY the threads the owner listed in `allowed_channels` for Instagram,
      written as `pt-<thread id>`. An empty list polls NOTHING (the gate treats an
      empty list as "everywhere"; this transport deliberately does not). It never
      reads the whole inbox, never creates, joins or leaves a group, and never
      posts unless somebody mentioned the account.
    * It starts "now": the first time it sees a thread it records a cursor and
      processes nothing older, so enabling it does not answer old messages.
    * Ids from the private API are NOT the app-scoped ids the official webhook gives,
      so every id here carries a prefix and the two id spaces are never merged:
      threads are `pt-<id>`, people are `pk-<id>`. People should be allowed by
      HANDLE (`channels-allow instagram reply @name`; the gate matches handles too).
      This transport does NOT write the handle -> id directory (that would map a
      handle to a private-API id next to the webhook's ids).
    * Credentials: entered only at the terminal prompt of `jarvis instagram-group-login`.
      Only the SESSION the client returns is stored, in `~/.jarvis/instagram_private_
      session.json`, mode 0600 on POSIX (Windows does not enforce mode bits: the file
      sits in your user profile, so treat the account as exposed to anyone who can
      read that profile). The password is never written anywhere, never put in
      `channels.json`, never sent through the browser, never logged.
    * Careful by construction: a poll interval with jitter, a minimum gap between
      posts per thread, exponential backoff, and on ANY login challenge / checkpoint /
      "login required" it STOPS, tells the owner once through `notify_owner`, and
      writes a halt marker so a supervisor restarting it does not retry in a loop
      (`instagram-group-login` clears it). Kill switch:
      `jarvis channels-set instagram group_transport off` (re-read every cycle).
    * Confirmations are not available here (no safe way to reach the owner's DM
      through this account), so a tool call that would need one is denied.

THE CLIENT
----------
The client is `instagrapi` (MIT, synchronous), an OPTIONAL extra pinned to one exact
version: `pip install "jarvis-cli[instagram-private]"` (see pyproject.toml). It is
imported lazily, inside `_default_client_factory()`, and only when one of the commands
below runs; without it `make_client()` raises TransportUnavailable with the install
line. `InstagrapiClient` (below) is the adapter. A client is any object with:

    own_user_id: str            own_username: str
    resume(session: dict) -> None          # raises CheckpointRequired / TransportError
    login(username, password, code="") -> dict   # the session to store
    fetch_new(thread_id: str, after_ms: int) -> list[dict]
        # messages newer than after_ms, any order; each dict:
        # {"message_id", "user_id", "username", "name", "text", "ts_ms", "own": bool}
    send(thread_id: str, text: str) -> None
    thread_title(thread_id: str) -> str    # optional
    list_threads(amount: int) -> list[dict]   # optional, `instagram-group-threads` only

What the adapter does NOT settle (nothing here has run against real Instagram): how a
group `@mention` looks in a message, how long a saved session lives, the ban / lock
rate at any poll interval, the exact exception raised in each login situation (every
unknown error that looks like a login check is treated as one and STOPS the transport;
every other error counts against the error budget), and whether `DirectThread.users`
lists everyone who has spoken (a message from someone not listed has an empty handle,
so that person can only be allowed by their `pk-<id>`). Test by hand with a throwaway
account in a small group everyone knows about, before any real use.
"""

import getpass
import json
import os
import random
import re
import sys
import threading
import time

from . import INSTAGRAM
from . import config as channel_config
from . import base, permissions, transcript

THREAD_PREFIX = "pt-"
USER_PREFIX = "pk-"

SESSION_FILE = transcript.JARVIS_DIR / "instagram_private_session.json"
HALT_FILE = transcript.JARVIS_DIR / "instagram_private_halt.json"
CURSOR_FILE = transcript.CHANNELS_DIR / "instagram_private_cursors.json"

MAX_TEXT_CHARS = 2000
INSTAGRAPI_PIN = "3.0.25"   # exact version of the optional client; keep in step with pyproject.toml
FETCH_MESSAGES = 20         # newest messages read per poll of one thread
MAX_FAILURES = 5            # consecutive transport errors before it stops and tells the owner
BACKOFF_CAP_SECONDS = 900
JITTER = 0.3                # poll interval is stretched by up to 30%
_RAW_ID = re.compile(r"^[0-9a-z_.\-]{1,60}$")


class TransportError(Exception):
    """Anything that went wrong talking to Instagram (network, rate limit, bad reply)."""


class CheckpointRequired(TransportError):
    """Login challenge, checkpoint, 2FA, or "login required": STOP, never retry."""


class TransportUnavailable(TransportError):
    """The optional client library is not installed."""


# ---------------------------------------------------------------------------
# The client seam
# ---------------------------------------------------------------------------

_client_factory = None


def set_client_factory(factory):
    """Install the function that builds a client (tests, or a deliberate wiring)."""
    global _client_factory
    _client_factory = factory


def _default_client_factory():
    """The ONE place the private-API library is imported (lazily, never at module level)."""
    try:
        import instagrapi  # noqa: PLC0415 -- deliberate lazy import, see the module docstring
    except ImportError as exc:
        raise TransportUnavailable(
            "no Instagram private-API client is installed. Install the optional one "
            f"(pinned, unofficial): pip install \"instagrapi=={INSTAGRAPI_PIN}\"  "
            "(or: pip install \"jarvis-cli[instagram-private]\")") from exc
    return InstagrapiClient(instagrapi)


def make_client():
    return (_client_factory or _default_client_factory)()


# ---------------------------------------------------------------------------
# The instagrapi adapter
# ---------------------------------------------------------------------------

# Rate-style errors: slow down (backoff + error budget), do not stop at once.
_RATE_WORDS = ("pleasewait", "throttl", "ratelimit", "toomany")
# Login-style errors: STOP. Matched on the exception class name (lower-cased), so a
# library that renames or adds a class still lands on the safe side when the name says
# what it is. `forbidden` / `blocked` are included on purpose: a 403 or a blocked
# address is Instagram saying no, not something to retry in a loop.
_CHECKPOINT_WORDS = ("challenge", "checkpoint", "loginrequired", "twofactor", "badpassword",
                     "relogin", "feedbackrequired", "recaptcha", "selectcontactpoint",
                     "consent", "forbidden", "blocked", "suspend")
# The same situations as Instagram names them in a reply body.
_CHECKPOINT_TEXT = ("login_required", "challenge_required", "checkpoint_required",
                    "feedback_required", "two_factor_required")


def classify_error(exc, secret=""):
    """Map anything the client library raised to CheckpointRequired (stop) or
    TransportError (counts against the error budget). Never includes `secret` in the
    text, and never more than the class name and a short flattened message."""
    name = type(exc).__name__
    text = " ".join(str(exc).split())
    if secret:
        text = text.replace(secret, "***")
    text = text[:160]
    label = f"{name}: {text}" if text else name
    low_name = name.lower()
    low_text = text.lower()
    if any(word in low_name for word in _RATE_WORDS):
        return TransportError(label)
    if any(word in low_name for word in _CHECKPOINT_WORDS) \
            or any(word in low_text for word in _CHECKPOINT_TEXT):
        return CheckpointRequired(label)
    return TransportError(label)


def to_ms(value):
    """A timestamp -> integer milliseconds since the epoch, 0 when it cannot be read.

    instagrapi gives a `datetime` (`.timestamp()` is right for both a naive local time
    and an aware one); a bare number is taken as microseconds, milliseconds or seconds
    by its size."""
    try:
        if value is None or isinstance(value, bool):
            return 0
        if hasattr(value, "timestamp"):
            return int(value.timestamp() * 1000)
        number = float(value)
    except (TypeError, ValueError, OverflowError, OSError):
        return 0
    if number >= 1e14:      # microseconds
        return int(number / 1000)
    if number >= 1e11:      # milliseconds
        return int(number)
    return int(number * 1000)   # seconds


def _numeric_thread(thread_id):
    raw = str(thread_id or "").strip()
    if not raw.isdigit():
        raise TransportError("an Instagram thread id is a number; got something else")
    return int(raw)


class InstagrapiClient:
    """Adapter from `instagrapi` to the small client interface in the module docstring.

    Built from the imported package so a test can hand in a stand-in. Every call into the
    library goes through `classify_error`; a library error never escapes unmapped."""

    def __init__(self, module):
        self._mod = module
        self._cl = None
        self.own_user_id = ""
        self.own_username = ""
        self._users = {}     # user id (str) -> (username, full name)
        self._titles = {}    # thread id (str) -> title

    # -- the library object ----------------------------------------------
    def _new(self):
        client = self._mod.Client()
        try:
            client.delay_range = [2, 5]    # the library pauses 2-5 s between its calls
        except Exception:  # noqa: BLE001 -- cosmetic safety margin only
            pass
        return client

    def _need(self):
        if self._cl is None:
            raise TransportError("not signed in")
        return self._cl

    @staticmethod
    def _forget_password(client):
        """The library keeps the password on the client for its own relogin. This
        transport never relogs in by itself, so drop it."""
        for attr in ("password",):
            try:
                if getattr(client, attr, None):
                    setattr(client, attr, "")
            except Exception:  # noqa: BLE001
                pass

    # -- session ----------------------------------------------------------
    def resume(self, session):
        client = self._new()
        try:
            client.set_settings(dict(session))
            info = client.account_info()    # one cheap authenticated call: is the session alive?
        except Exception as exc:  # noqa: BLE001
            raise classify_error(exc) from exc
        self._cl = client
        self.own_user_id = str(getattr(info, "pk", "") or getattr(client, "user_id", "") or "")
        self.own_username = str(getattr(info, "username", "") or "")
        if not self.own_user_id:
            self._cl = None
            raise TransportError("the session did not say which account it is")

    def login(self, username, password, code=""):
        client = self._new()
        try:
            if code:
                client.login(username, password, verification_code=code)
            else:
                client.login(username, password)
        except Exception as exc:  # noqa: BLE001
            raise classify_error(exc, secret=password) from exc
        finally:
            self._forget_password(client)
        try:
            session = json.loads(json.dumps(client.get_settings(), default=str))
        except (TypeError, ValueError) as exc:
            raise TransportError("the client's session could not be read") from exc
        if not isinstance(session, dict) or not session:
            raise TransportError("the client returned no session")
        # The session file is the one place a credential must never land. If this
        # version of the library ever stores the password in its settings, save nothing.
        if password and len(password) >= 6 and password in json.dumps(session):
            raise TransportError("the client put your password in its session; nothing was saved")
        self._cl = client
        self.own_user_id = str(getattr(client, "user_id", "") or "")
        self.own_username = str(username or "").lstrip("@")
        return session

    # -- reading ----------------------------------------------------------
    def _learn(self, thread):
        title = str(getattr(thread, "thread_title", "") or "")
        tid = str(getattr(thread, "id", "") or getattr(thread, "pk", "") or "")
        if tid and title:
            self._titles[tid] = title
        for user in getattr(thread, "users", None) or []:
            pk = str(getattr(user, "pk", "") or "")
            if pk:
                self._users[pk] = (str(getattr(user, "username", "") or ""),
                                   str(getattr(user, "full_name", "") or ""))
        return title

    def fetch_new(self, thread_id, after_ms):
        client = self._need()
        numeric = _numeric_thread(thread_id)
        try:
            # One call returns the newest messages AND the thread's people and title.
            thread = client.direct_thread(numeric, amount=FETCH_MESSAGES)
        except Exception as exc:  # noqa: BLE001
            raise classify_error(exc) from exc
        title = self._learn(thread)
        if title:
            self._titles[str(thread_id)] = title
        out = []
        for item in getattr(thread, "messages", None) or []:
            stamp = to_ms(getattr(item, "timestamp", None))
            if stamp <= int(after_ms or 0):
                continue
            uid = str(getattr(item, "user_id", "") or "")
            handle, name = self._users.get(uid, ("", ""))
            out.append({
                "message_id": str(getattr(item, "id", "") or ""),
                "user_id": uid,
                "username": handle,
                "name": name,
                "text": str(getattr(item, "text", "") or ""),
                "ts_ms": stamp,
                "own": bool(uid and uid == self.own_user_id),
            })
        return out

    def thread_title(self, thread_id):
        return self._titles.get(str(thread_id), "")

    # -- writing ----------------------------------------------------------
    def send(self, thread_id, text):
        client = self._need()
        numeric = _numeric_thread(thread_id)
        try:
            # thread_ids only: the library wants exactly one of user_ids / thread_ids.
            client.direct_send(str(text), thread_ids=[numeric])
        except Exception as exc:  # noqa: BLE001
            raise classify_error(exc) from exc

    # -- `instagram-group-threads` ---------------------------------------
    def list_threads(self, amount=20):
        """[{id, title, people:[handle...], is_group}] for the newest inbox threads.
        Carries no message text."""
        client = self._need()
        try:
            threads = client.direct_threads(amount=int(amount))
        except Exception as exc:  # noqa: BLE001
            raise classify_error(exc) from exc
        rows = []
        for thread in threads or []:
            tid = str(getattr(thread, "id", "") or "")
            if not tid.isdigit():
                continue
            people = [str(getattr(u, "username", "") or getattr(u, "pk", "") or "")
                      for u in (getattr(thread, "users", None) or [])]
            flag = getattr(thread, "is_group", None)
            rows.append({"id": tid,
                         "title": str(getattr(thread, "thread_title", "") or "")[:80],
                         "people": [p for p in people if p][:12],
                         "is_group": flag if isinstance(flag, bool) else None})
        return rows


# ---------------------------------------------------------------------------
# Small files: session, halt marker, cursors (never raise on the read side)
# ---------------------------------------------------------------------------


def _write_private(path, data):
    """Atomic write, 0600 where the platform honours it."""
    tmp = path.with_name(path.name + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data))
    try:
        os.chmod(str(tmp), 0o600)
    except OSError:
        pass
    os.replace(str(tmp), str(path))


def save_session(session):
    _write_private(SESSION_FILE, session)


def load_session():
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data else None
    except (OSError, ValueError):
        return None


def read_halt():
    try:
        data = json.loads(HALT_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_halt(reason):
    try:
        _write_private(HALT_FILE, {"reason": str(reason)[:300],
                                   "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    except OSError:
        pass


def clear_halt():
    try:
        HALT_FILE.unlink()
    except OSError:
        pass


def _load_cursors():
    try:
        data = json.loads(CURSOR_FILE.read_text(encoding="utf-8"))
        return {str(k): int(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_cursors(cursors):
    try:
        CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CURSOR_FILE.with_name(CURSOR_FILE.name + ".tmp")
        tmp.write_text(json.dumps(cursors), encoding="utf-8")
        os.replace(str(tmp), str(CURSOR_FILE))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def polled_threads(cfg):
    """[(raw thread id, 'pt-<id>')] for the threads this transport may read.

    Only `allowed_channels` entries written `pt-<id>` count. An empty list (or one with
    only webhook-style ids) is NOTHING, even though the gate reads an empty list as
    "anywhere": a transport that logs in as a person must never go looking."""
    out = []
    seen = set()
    for entry in channel_config.normalize_entries((cfg or {}).get("allowed_channels")):
        if not entry.startswith(THREAD_PREFIX):
            continue
        raw = entry[len(THREAD_PREFIX):]
        if not _RAW_ID.match(raw) or raw in seen:
            continue
        seen.add(raw)
        out.append((raw, THREAD_PREFIX + raw))
    return out


def _flat(text):
    return " ".join(str(text or "").split())[:MAX_TEXT_CHARS]


def to_incoming(raw_msg, thread_raw, own_username="", own_user_id="", thread_name="", cfg=None):
    """One client message dict -> IncomingMessage (a room message), or None for our own
    message / an empty one. Carries no raw payload."""
    if not isinstance(raw_msg, dict) or raw_msg.get("own"):
        return None
    uid = str(raw_msg.get("user_id") or "")
    if not uid or (own_user_id and uid == str(own_user_id)):
        return None
    text = _flat(raw_msg.get("text"))
    if not text:
        return None
    handle = str(raw_msg.get("username") or "").strip().lstrip("@")
    bot = str(own_username or (cfg or {}).get("bot_handle") or "").strip().lstrip("@")
    from .instagram_gateway import mentions_bot
    thread_id = THREAD_PREFIX + str(thread_raw)
    mid = str(raw_msg.get("message_id") or "")
    try:
        stamp = float(raw_msg.get("ts_ms") or 0) / 1000.0 or None
    except (TypeError, ValueError):
        stamp = None
    return permissions.IncomingMessage(
        platform=INSTAGRAM,
        context=permissions.CTX_GROUP,
        user_id=USER_PREFIX + uid,
        user_handle=handle,
        text=text,
        mentioned=mentions_bot(text, bot),
        message_id=(THREAD_PREFIX + mid) if mid else "",
        channel_id=thread_id,
        thread_id=thread_id,
        timestamp=stamp,
        author_name=str(raw_msg.get("name") or "")[:60],
        thread_name=str(thread_name or "")[:80],
    )


def _keep_room_context(cfg, msg):
    """Same rule as discord_gateway._keep_room_context, for this transport: hand one
    unaddressed message to the in-memory ambient buffer if permissions.ambient_ok
    admits it. Writes NOTHING. Never raises."""
    try:
        from . import ambient, people, space
        if not permissions.ambient_ok(cfg, msg):
            return
        entry = people.get(INSTAGRAM, msg.user_id)
        blocked = bool(entry and (entry.get("follow") or "") == people.FOLLOW_BLOCKED)
        if not permissions.ambient_ok(cfg, msg, blocked=blocked):
            return
        scoped = permissions.resolve_scope(cfg, msg)
        owner = permissions.is_owner(scoped, msg)
        ambient.add(INSTAGRAM, msg, space.speaker_record(entry, msg, owner), cfg)
    except Exception:  # noqa: BLE001 -- context is never worth a failure
        pass


def _default_notify(text):
    from . import outbound
    return outbound.notify_owner(text)


# ---------------------------------------------------------------------------
# The transport
# ---------------------------------------------------------------------------


class Transport:
    """Polls the allowed threads and feeds base.handle_message. Everything that touches
    time, threads or the owner is injectable so a test needs no sleeping and no network."""

    def __init__(self, client, cfg_loader=None, clock=time.time, sleeper=time.sleep,
                 notify=None, spawn=None, rng=random.random):
        self.client = client
        self._load_cfg = cfg_loader or (lambda: channel_config.platform_config(INSTAGRAM))
        self._clock = clock
        self._sleep = sleeper
        self._notify = notify or _default_notify
        self._spawn = spawn or self._spawn_thread
        self._rng = rng
        self.cursors = _load_cursors()
        self.failures = 0
        self.halted = False
        self._notified = False
        self._titles = {}
        self._last_send = {}
        self._lock = threading.Lock()

    @staticmethod
    def _spawn_thread(fn):
        threading.Thread(target=fn, daemon=True, name="instagram-private-reply").start()

    # -- switches ---------------------------------------------------------
    def _on(self, cfg):
        return cfg.get("group_transport") is True and bool(cfg.get("enabled"))

    # -- stopping ---------------------------------------------------------
    def halt(self, reason):
        """Stop for good until the owner logs in again. Tells the owner ONCE."""
        self.halted = True
        write_halt(reason)
        base._log(f"instagram group transport stopped: {reason}")
        if not self._notified:
            self._notified = True
            try:
                self._notify("Instagram group transport stopped: " + str(reason)[:200]
                             + ". It will not retry by itself. Run `jarvis instagram-group-login` "
                               "to sign in again, or leave it off.")
            except Exception:  # noqa: BLE001
                pass

    # -- sending ----------------------------------------------------------
    def _send(self, thread_raw, text):
        cfg = self._load_cfg()
        if not self._on(cfg):
            raise RuntimeError("the instagram group transport is switched off")
        gap = _gap_seconds(cfg)
        with self._lock:
            last = self._last_send.get(thread_raw)
            now = self._clock()
            if last is not None and now - last < gap:
                self._sleep(gap - (now - last))
            self._last_send[thread_raw] = self._clock()
        try:
            self.client.send(thread_raw, text)
        except CheckpointRequired as exc:
            self.halt(f"Instagram asked for a login check while sending ({exc})")
            raise RuntimeError("login check required") from exc
        except TransportError as exc:
            raise RuntimeError(str(exc)[:200]) from exc
        return True

    # -- one message ------------------------------------------------------
    def dispatch(self, cfg, thread_raw, raw_msg):
        """Run one polled message through the gate. Returns 'answered', 'context' or
        'ignored'."""
        title = self._titles.get(thread_raw)
        if title is None:
            try:
                title = str(self.client.thread_title(thread_raw) or "")
            except Exception:  # noqa: BLE001 -- the title is cosmetic
                title = ""
            self._titles[thread_raw] = title
        msg = to_incoming(raw_msg, thread_raw,
                          own_username=getattr(self.client, "own_username", ""),
                          own_user_id=getattr(self.client, "own_user_id", ""),
                          thread_name=title, cfg=cfg)
        if msg is None:
            return "ignored"
        decision = base.gate(INSTAGRAM, msg, cfg)
        if not base.addressed_to_us(decision):
            _keep_room_context(cfg, msg)
            return "context"

        def send(text, _thread=thread_raw):
            return self._send(_thread, text)

        def work():
            try:
                base.handle_message(INSTAGRAM, msg, send, cfg=cfg, decision=decision)
            except Exception as exc:  # noqa: BLE001 -- one bad message must not stop polling
                base._log(f"instagram group message failed: {type(exc).__name__}")

        self._spawn(work)
        return "answered"

    # -- one poll cycle ---------------------------------------------------
    def step(self):
        """One pass over the allowed threads. Returns 'off', 'idle', 'halted', 'error'
        or 'ok'."""
        cfg = self._load_cfg()
        if not self._on(cfg):
            return "off"
        threads = polled_threads(cfg)
        if not threads:
            return "idle"
        now_ms = int(self._clock() * 1000)
        failed = False
        for raw, _prefixed in threads:
            if self.halted:
                return "halted"
            cursor = self.cursors.get(raw)
            if cursor is None:
                # First sight of this thread: start from now, never replay history.
                self.cursors[raw] = now_ms
                _save_cursors(self.cursors)
                continue
            try:
                batch = self.client.fetch_new(raw, cursor)
            except CheckpointRequired as exc:
                self.halt(f"Instagram asked for a login check ({exc})")
                return "halted"
            except TransportError as exc:
                failed = True
                self.failures += 1
                base._log(f"instagram group poll failed ({self.failures}/{MAX_FAILURES}): "
                          f"{type(exc).__name__}")
                if self.failures >= MAX_FAILURES:
                    self.halt("too many failed polls in a row")
                    return "halted"
                continue
            batch = [m for m in (batch or []) if isinstance(m, dict)]
            batch.sort(key=lambda m: _ts(m))
            for m in batch:
                stamp = _ts(m)
                if stamp <= cursor:
                    continue
                cursor = stamp
                self.cursors[raw] = cursor
                self.dispatch(cfg, raw, m)
            _save_cursors(self.cursors)
        if not failed:
            self.failures = 0
        return "error" if failed else "ok"

    def delay(self):
        """Seconds to wait before the next cycle: the interval, stretched by jitter and
        by exponential backoff after failures."""
        cfg = self._load_cfg()
        base_delay = _poll_seconds(cfg)
        delay = base_delay * (1.0 + JITTER * self._rng())
        if self.failures:
            delay = min(BACKOFF_CAP_SECONDS, delay * (2 ** min(self.failures, 8)))
        return delay

    def run_forever(self, max_cycles=None):
        cycles = 0
        while True:
            result = self.step()
            cycles += 1
            if result in ("off", "halted"):
                return result
            if max_cycles is not None and cycles >= max_cycles:
                return result
            self._sleep(self.delay())


def _ts(message):
    try:
        return int(message.get("ts_ms") or 0)
    except (TypeError, ValueError):
        return 0


def _gap_seconds(cfg):
    try:
        return max(3, min(600, int(cfg.get("group_send_gap_seconds") or 10)))
    except (TypeError, ValueError):
        return 10


def _poll_seconds(cfg):
    try:
        return max(15, min(600, int(cfg.get("group_poll_seconds") or 45)))
    except (TypeError, ValueError):
        return 45


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

_WARNING = ("Instagram group transport: this goes outside Meta's official API and Instagram's "
            "terms. The account can be challenged or banned. Use a dedicated account.")


def run():
    """`jarvis instagram-group-serve` (foreground). Returns an exit code: 0 stopped by
    the owner or the kill switch, 1 not ready, 2 stopped on a login check (needs the
    owner)."""
    cfg = channel_config.platform_config(INSTAGRAM)
    if cfg.get("group_transport") is not True:
        print("the instagram group transport is OFF (the default). Turn it on only after "
              "reading channels/instagram_private.py:\n"
              "  jarvis channels-set instagram group_transport on", file=sys.stderr)
        return 1
    if not cfg.get("enabled"):
        print("instagram channel is disabled: jarvis channels-set instagram enabled true",
              file=sys.stderr)
        return 1
    halt = read_halt()
    if halt:
        print("stopped earlier: " + str(halt.get("reason") or "login check") + "\n"
              "It will not start again by itself. Run `jarvis instagram-group-login`.",
              file=sys.stderr)
        return 2
    session = load_session()
    if not session:
        print("no saved session. Run `jarvis instagram-group-login` first.", file=sys.stderr)
        return 1
    if not polled_threads(cfg):
        print("no thread is listed. Add one with:\n"
              "  jarvis channels-channels instagram add pt-<thread id> --yes\n"
              "(nothing is read until a thread is listed)", file=sys.stderr)
        return 1
    try:
        client = make_client()
        client.resume(session)
    except CheckpointRequired as exc:
        Transport(None).halt(f"Instagram asked for a login check on start ({exc})")
        return 2
    except TransportError as exc:
        print(f"could not start: {exc}", file=sys.stderr)
        return 1
    print(_WARNING, file=sys.stderr, flush=True)
    base._log(f"instagram group transport polling {len(polled_threads(cfg))} thread(s)")
    transport = Transport(client)
    try:
        transport.run_forever()
    except KeyboardInterrupt:
        base._log("stopping")
    return 2 if transport.halted else 0


def login_cli():
    """`jarvis instagram-group-login`: ask for the credentials at the terminal, keep ONLY
    the session. Returns an exit code."""
    if not sys.stdin or not sys.stdin.isatty():
        print("this asks for a password: run it in a terminal, not piped or scheduled.",
              file=sys.stderr)
        return 1
    print(_WARNING)
    try:
        client = make_client()
    except TransportUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    username = input("Instagram username (a dedicated account): ").strip().lstrip("@")
    password = getpass.getpass("Password (not shown, not saved): ")
    code = input("2FA code (blank if none): ").strip()
    try:
        session = client.login(username, password, code)
    except CheckpointRequired as exc:
        print(f"Instagram asked for a check and it was not completed: {exc}", file=sys.stderr)
        return 2
    except TransportError as exc:
        print(f"login failed: {exc}", file=sys.stderr)
        return 1
    finally:
        password = ""  # noqa: F841 -- drop the only copy we hold
    if not isinstance(session, dict) or not session:
        print("login returned no session; nothing saved.", file=sys.stderr)
        return 1
    save_session(session)
    clear_halt()
    print(f"signed in as @{username}; session saved to {SESSION_FILE}.\n"
          "Next: jarvis channels-set instagram group_transport on, list a thread with "
          "`channels-channels instagram add pt-<thread id> --yes`, then "
          "jarvis instagram-group-serve")
    return 0


def _looks_like_group(row):
    """is_group when the library says so; otherwise two or more other people (you plus
    them makes three). A guess: `--all` shows every thread."""
    if row.get("is_group") is not None:
        return bool(row["is_group"])
    return len(row.get("people") or []) >= 2


def threads_cli(argv=None):
    """`jarvis instagram-group-threads [--all] [--amount N]`: print the ids of your
    newest inbox threads so you can list one as `pt-<id>`. One read of the inbox list,
    run by you at a terminal; it prints thread ids, titles and handles, NEVER message
    text, and writes nothing. It refuses after a login check (the halt marker). The
    transport itself still never reads anything that is not on allowed_channels."""
    args = list(argv or [])
    show_all = "--all" in args
    amount = 20
    if "--amount" in args:
        try:
            amount = max(1, min(50, int(args[args.index("--amount") + 1])))
        except (IndexError, ValueError):
            print("--amount takes a number from 1 to 50", file=sys.stderr)
            return 1
    halt = read_halt()
    if halt:
        print("stopped earlier: " + str(halt.get("reason") or "login check") + "\n"
              "Run `jarvis instagram-group-login` first.", file=sys.stderr)
        return 2
    session = load_session()
    if not session:
        print("no saved session. Run `jarvis instagram-group-login` first.", file=sys.stderr)
        return 1
    try:
        client = make_client()
        client.resume(session)
        lister = getattr(client, "list_threads", None)
        if not callable(lister):
            print("this client cannot list threads.", file=sys.stderr)
            return 1
        rows = lister(amount)
    except CheckpointRequired as exc:
        write_halt(f"Instagram asked for a login check while listing threads ({exc})")
        print(f"Instagram asked for a login check: {exc}\n"
              "Nothing more was tried. Run `jarvis instagram-group-login`.", file=sys.stderr)
        return 2
    except TransportError as exc:
        print(f"could not list threads: {exc}", file=sys.stderr)
        return 1
    print(_WARNING, file=sys.stderr)
    shown = [r for r in rows if show_all or _looks_like_group(r)]
    if not shown:
        print("no group thread found among the newest " + str(amount) + ". "
              "Try --all (the group check is a guess) or --amount 50.")
        return 0
    for row in shown:
        title = row["title"] or "(no title)"
        people = ", ".join("@" + p for p in row["people"]) or "(people not listed)"
        print(f"{THREAD_PREFIX}{row['id']}  {title}\n    with: {people}")
    print("\nList one for polling (nothing is read until you do):\n"
          "  jarvis channels-channels instagram add pt-<thread id> --yes")
    return 0
