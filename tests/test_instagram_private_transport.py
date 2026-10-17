"""Tests for the unofficial Instagram group transport (channel spaces S9c).

    python3 tests/test_instagram_private_transport.py

A fake client stands in for the private API: no network, no library, no model, no
real ~/.jarvis (HOME is redirected to a temp dir BEFORE any jarvis module is imported).

  A  isolation        not imported by default, no third-party import, no client bundled
  B  switch + keys    off by default, Instagram-only keys, validation, change log, CLI
  C  what is polled   only `pt-<id>` entries of allowed_channels; empty = nothing
  D  the message      id prefixes, own / empty messages dropped, mention, no raw payload
  E  the pipeline     same gate and handle_message; speaker labels, ambient context,
                      nothing unmentioned on disk, cursor (no history replay, no repeats)
  F  stopping         kill switch, checkpoint -> halt + one notice, error budget, backoff,
                      per-thread send gap
  G  credentials      only the session is kept (0600 on POSIX); the password is nowhere
  H  commands         run() / login_cli() behaviour, policy path, source rules
"""

import ast
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-igprivate-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ["JARVIS_RAW_ARCHIVE"] = "0"
_CLI = str(Path(__file__).resolve().parent.parent / "jarvis-cli")
sys.path.insert(0, _CLI)

from jarvis.channels import (INSTAGRAM, DISCORD, ambient, base, changelog, instagram_gateway,  # noqa: E402,F401
                             discord_gateway, people, permissions, transcript)
from jarvis.channels import config as channel_config                                       # noqa: E402
from jarvis import channels_cli                                                             # noqa: E402,F401

# Captured BEFORE anything below imports the module under test.
IMPORTED_BY_DEFAULT = "jarvis.channels.instagram_private" in sys.modules

import jarvis.channels.instagram_private as ip  # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' - ' + detail) if detail else ''}")


T1, T2 = "111222333", "999888777"
ALICE, CAROL = "5001", "5002"
NOTICES = []
ip._default_notify = lambda text: NOTICES.append(text) or True


def wipe():
    root = Path(_HOME) / ".jarvis"
    for _ in range(3):
        shutil.rmtree(root, ignore_errors=True)
        if not root.exists():
            break
    transcript.reset_cooldowns()
    ambient.clear()
    ip.set_client_factory(None)
    del NOTICES[:]


def reset(extra=None):
    wipe()
    cfg = channel_config.load_config()
    cfg[INSTAGRAM].update({
        "enabled": True, "allow_tools": False, "cooldown_seconds": 0,
        "dm_allowlist": [], "reply_allowlist": ["alice"], "tool_allowlist": [],
        "require_mention": True, "group_transport": True,
        "allowed_channels": ["pt-" + T1],
    })
    cfg[INSTAGRAM].update(extra or {})
    channel_config.save_config(cfg)
    return channel_config.load_config()[INSTAGRAM]


class FakeClient:
    own_user_id = "9"
    own_username = "jarvisbot"

    def __init__(self):
        self.fetch_calls = []
        self.sent = []
        self.batch = {}
        self.raise_on_fetch = None
        self.raise_on_send = None
        self.resumed = None
        self.login_args = None

    def resume(self, session):
        self.resumed = session
        if isinstance(self.raise_on_fetch, ip.CheckpointRequired) and self.raise_on_fetch.args == ("resume",):
            raise self.raise_on_fetch

    def login(self, username, password, code=""):
        self.login_args = (username, password, code)
        return {"cookie": "SESSION-SECRET-xyz", "user": username}

    def fetch_new(self, thread_id, after_ms):
        self.fetch_calls.append((thread_id, after_ms))
        if self.raise_on_fetch is not None:
            raise self.raise_on_fetch
        return list(self.batch.get(thread_id, []))

    def send(self, thread_id, text):
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.sent.append((thread_id, text))

    def thread_title(self, thread_id):
        return "Crew chat"


def raw(mid, uid, username, text, ts, name="", own=False):
    return {"message_id": mid, "user_id": uid, "username": username, "name": name or username.title(),
            "text": text, "ts_ms": ts, "own": own}


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class _Result:
    def __init__(self, text="the answer"):
        self.text, self.provider, self.error, self.ok = text, "fake-provider", "", True
        self.usage_total = {"input_tokens": 1, "output_tokens": 1, "thinking_tokens": 0,
                            "total_tokens": 2, "requests": 1}
        self.usage = {"tool_calls": []}


class StubModel:
    """Replace the model call; record what the pipeline handed it."""

    def __enter__(self):
        self.saved = base._ask_jarvis, base._notify_owner_new_sender
        self.calls = []

        def fake(*args, **kwargs):
            self.calls.append((args, kwargs))
            return _Result()

        base._ask_jarvis = fake
        base._notify_owner_new_sender = lambda *a, **k: True
        return self

    def __exit__(self, *exc):
        base._ask_jarvis, base._notify_owner_new_sender = self.saved


def make_transport(client, clock=None, cfg_loader=None):
    clock = clock or Clock()
    return ip.Transport(client, clock=clock, sleeper=clock.sleep, notify=lambda t: NOTICES.append(t) or True,
                        spawn=lambda fn: fn(), rng=lambda: 0.0, cfg_loader=cfg_loader), clock


def tree_text():
    """Everything under HOME as one string, for 'this text is nowhere on disk'."""
    chunks = []
    for path in sorted(Path(_HOME).rglob("*")):
        if path.is_file():
            try:
                chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass
    return "\n".join(chunks)


# --------------------------------------------------------------------------- A

def test_a_isolation():
    check("jarvis.channels and the gateways do NOT import the transport", IMPORTED_BY_DEFAULT is False)
    check("a stock start loads no private client library",
          not any(name.split(".")[0] in {"instagrapi", "instagram_private_api", "instaloader"}
                  for name in sys.modules))
    src = Path(ip.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    stdlib_ok = {"getpass", "json", "os", "random", "re", "sys", "threading", "time"}
    bad = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            bad += [a.name for a in node.names if a.name.split(".")[0] not in stdlib_ok]
        elif isinstance(node, ast.ImportFrom) and not node.level and (node.module or "").split(".")[0] not in stdlib_ok:
            bad.append(node.module)
    check("module-level imports are the standard library and sibling modules only", not bad, str(bad))
    check("the default client factory refuses: nothing is bundled",
          _raises(ip.TransportUnavailable, ip.make_client))
    check("no `requests` / network module is used by the transport", "requests" not in src.split('"""', 2)[2])
    check("it never writes the handle -> id directory (two id spaces stay apart)",
          "directory.record" not in src)
    channels_init = Path(ip.__file__).with_name("__init__.py").read_text(encoding="utf-8")
    check("jarvis/channels/__init__.py does not mention it", "instagram_private" not in channels_init)


def _raises(exc_type, fn, *args):
    try:
        fn(*args)
    except exc_type:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


# --------------------------------------------------------------------------- B

def test_b_switch_and_keys():
    wipe()
    cfg = channel_config.load_config()[INSTAGRAM]
    check("the transport is OFF by default", cfg.get("group_transport") is False)
    check("defaults for the poll interval and the send gap",
          cfg.get("group_poll_seconds") == 45 and cfg.get("group_send_gap_seconds") == 10)
    ok, err = channel_config.set_value(INSTAGRAM, "group_transport", "on")
    check("channels-set instagram group_transport on", ok and channel_config.platform_config(INSTAGRAM)["group_transport"] is True, err)
    check("only the literal true counts", ip.Transport(FakeClient())._on({"group_transport": "true", "enabled": True}) is False)
    ok, err = channel_config.set_value(DISCORD, "group_transport", "on")
    check("Discord cannot be given the Instagram transport switch", not ok and "instagram" in err, err)
    check("a bad on/off is refused", not channel_config.set_value(INSTAGRAM, "group_transport", "maybe")[0])
    check("poll seconds are bounded (15..600)",
          not channel_config.set_value(INSTAGRAM, "group_poll_seconds", "5")[0]
          and not channel_config.set_value(INSTAGRAM, "group_poll_seconds", "601")[0]
          and channel_config.set_value(INSTAGRAM, "group_poll_seconds", "60")[0])
    check("the send gap is bounded (3..600)",
          not channel_config.set_value(INSTAGRAM, "group_send_gap_seconds", "1")[0]
          and channel_config.set_value(INSTAGRAM, "group_send_gap_seconds", "20")[0])
    check("a non-number is refused", not channel_config.set_value(INSTAGRAM, "group_poll_seconds", "soon")[0])
    lines = [e for e in changelog.read_all() if e.get("kind") == "platform" and e.get("key") == "group_transport"]
    on_lines = [e for e in lines if e.get("on") is True]
    check("turning it on is written to the change log, with the risk in the words",
          bool(on_lines) and "outside Meta" in changelog.describe(on_lines[-1]), str(lines))
    env = dict(os.environ, HOME=_HOME, USERPROFILE=_HOME, PYTHONPATH=_CLI, JARVIS_RAW_ARCHIVE="0")
    proc = subprocess.run([sys.executable, "-m", "jarvis", "channels-set", "instagram", "group_transport", "off"],
                          capture_output=True, text=True, env=env, timeout=60)
    check("the CLI kill switch works", proc.returncode == 0
          and channel_config.platform_config(INSTAGRAM)["group_transport"] is False, proc.stdout + proc.stderr)


# --------------------------------------------------------------------------- C

def test_c_polled_threads():
    cfg = {"allowed_channels": ["pt-" + T1, "PT-" + T2, "12345", "pt-", "pt-../x", "pt-" + T1, "discord-chan"]}
    got = ip.polled_threads(cfg)
    check("only pt-<id> entries are polled, once each", [raw_id for raw_id, _p in got] == [T1, T2], str(got))
    check("an empty list polls NOTHING (the gate would read it as everywhere)", ip.polled_threads({"allowed_channels": []}) == [])
    check("webhook-style ids are never polled", ip.polled_threads({"allowed_channels": ["12345", "abc"]}) == [])
    check("a bad config value polls nothing", ip.polled_threads({"allowed_channels": None}) == [] and ip.polled_threads(None) == [])
    check("a path-like id is dropped", ip.polled_threads({"allowed_channels": ["pt-a/b", "pt-a b"]}) == [])


# --------------------------------------------------------------------------- D

def test_d_to_incoming():
    m = ip.to_incoming(raw("m1", ALICE, "@Alice", "  @JarvisBot   hello  there ", 5000, name="Alice A"),
                       T1, own_username="jarvisbot", own_user_id="9", thread_name="Crew chat")
    check("a room message with prefixed ids", m is not None and m.context == permissions.CTX_GROUP
          and m.user_id == "pk-" + ALICE and m.channel_id == "pt-" + T1 and m.thread_id == "pt-" + T1
          and m.message_id == "pt-m1" and m.platform == INSTAGRAM)
    check("handle cleaned, names kept, text flattened", m.user_handle == "Alice" and m.author_name == "Alice A"
          and m.text == "@JarvisBot hello there" and m.thread_name == "Crew chat")
    check("a mention of the account counts", m.mentioned is True)
    check("no raw payload is carried", m.raw is None)
    check("the time comes from the message", abs(m.timestamp - 5.0) < 1e-6)
    check("someone else's @name is not a mention",
          ip.to_incoming(raw("m2", ALICE, "alice", "hi @someoneelse", 1), T1, own_username="jarvisbot").mentioned is False)
    check("our own message is dropped", ip.to_incoming(raw("m3", "9", "jarvisbot", "x", 1), T1, own_user_id="9") is None
          and ip.to_incoming(raw("m3", ALICE, "alice", "x", 1, own=True), T1) is None)
    check("an empty message is dropped", ip.to_incoming(raw("m4", ALICE, "alice", "   ", 1), T1) is None)
    check("garbage is dropped", ip.to_incoming(None, T1) is None and ip.to_incoming({"text": "x"}, T1) is None)
    long = ip.to_incoming(raw("m5", ALICE, "alice", "x" * 5000, 1), T1)
    check("text is clipped", len(long.text) == ip.MAX_TEXT_CHARS)


# --------------------------------------------------------------------------- E

def test_e_pipeline():
    cfg = reset()
    client = FakeClient()
    t, clock = make_transport(client)
    base_ms = int(clock() * 1000)
    with StubModel() as model:
        check("first cycle: the cursor is set and NOTHING is fetched or answered",
              t.step() == "ok" and client.fetch_calls == [] and client.sent == [] and model.calls == [])
        check("the cursor is saved to disk", json.loads(ip.CURSOR_FILE.read_text()) == {T1: base_ms})
        client.batch[T1] = [
            raw("a1", ALICE, "alice", "the vault code is 4242", base_ms + 1000),          # approved, not mentioned
            raw("c1", CAROL, "carol", "carol private chatter 777", base_ms + 2000),       # not approved, not mentioned
            raw("o1", "9", "jarvisbot", "my own earlier post", base_ms + 2500, own=True),  # our own
            raw("a2", ALICE, "alice", "@jarvisbot what did I just say?", base_ms + 3000, name="Alice"),
        ]
        check("second cycle runs", t.step() == "ok")
        check("only the allowed thread was fetched, from the cursor", client.fetch_calls == [(T1, base_ms)], str(client.fetch_calls))
        check("exactly ONE model call: the mention, nothing else", len(model.calls) == 1, str(len(model.calls)))
        ctx = model.calls[0][1].get("sender_context", "") if model.calls else ""
        check("the model was told it is in a shared room", "WHERE" in ctx or "group chat" in ctx, ctx[:300])
        check("the approved unmentioned message is context", "the vault code is 4242" in ctx)
        check("the unapproved one is NOT", "carol private chatter" not in ctx)
        check("it is framed as untrusted", "untrusted" in ctx)
        check("a reply was posted into the thread, by raw id", client.sent == [(T1, "the answer")], str(client.sent))
        lines = transcript.read_space(INSTAGRAM, "pt-" + T1)
        entries = lines.get("entries") or []
        check("the mention is saved as a group-space line with its speaker",
              lines.get("kind") == "group" and any(e.get("text", "").startswith("@jarvisbot") for e in entries)
              and any((e.get("author_name") or "") == "Alice" for e in entries), str(lines)[:300])
        disk = tree_text()
        check("unmentioned text is NOT on disk (approved or not)",
              "the vault code is 4242" not in disk and "carol private chatter" not in disk)
        check("nothing was registered for the unapproved sender", people.get(INSTAGRAM, "pk-" + CAROL) is None)
        check("the cursor moved to the newest handled message", json.loads(ip.CURSOR_FILE.read_text())[T1] == base_ms + 3000)
        # --- restart: a new transport must not replay what was handled ---------
        t2, _c2 = make_transport(client, clock)
        before = len(model.calls)
        t2.step()
        check("after a restart the cursor is read back and the same batch is not answered twice",
              len(model.calls) == before and client.fetch_calls[-1] == (T1, base_ms + 3000) and len(client.sent) == 1,
              str(client.fetch_calls[-1]))
    check("a thread NOT on allowed_channels is never polled, even when the client knows it",
          all(call[0] == T1 for call in client.fetch_calls))


def test_e2_gate_denials_stay_quiet():
    reset()
    client = FakeClient()
    t, clock = make_transport(client)
    base_ms = int(clock() * 1000)
    with StubModel() as model:
        t.step()
        client.batch[T1] = [raw("c9", CAROL, "carol", "@jarvisbot hello", base_ms + 1000)]
        t.step()
        check("a mention by someone not on the reply list gets no model call and no post",
              model.calls == [] and client.sent == [])
        # the same message can also be refused by the room switch
        cfg = channel_config.load_config()
        cfg[INSTAGRAM]["enabled"] = False
        channel_config.save_config(cfg)
        client.batch[T1] = [raw("a9", ALICE, "alice", "@jarvisbot hi", base_ms + 2000)]
        check("platform disabled: the cycle does not even look", t.step() == "off" and model.calls == [])


# --------------------------------------------------------------------------- F

def test_f_stopping():
    # kill switch
    reset()
    client = FakeClient()
    t, clock = make_transport(client)
    t.step()
    channel_config.set_value(INSTAGRAM, "group_transport", "off")
    calls = len(client.fetch_calls)
    check("switching it off stops the very next cycle, no fetch", t.step() == "off" and len(client.fetch_calls) == calls)
    check("a reply still in flight is dropped when the switch is off",
          _raises(RuntimeError, t._send, T1, "late reply") and client.sent == [])
    check("run_forever returns when switched off", t.run_forever(max_cycles=5) == "off")

    # checkpoint -> halt, notify once, no retry
    reset()
    client = FakeClient()
    t, clock = make_transport(client)
    t.step()
    client.raise_on_fetch = ip.CheckpointRequired("challenge_required")
    r1 = t.step()
    r2 = t.step()
    check("a login check halts the transport", r1 == "halted" and r2 == "halted" and t.halted)
    check("the owner is told ONCE", len(NOTICES) == 1 and "instagram-group-login" in NOTICES[0], str(NOTICES))
    check("it did not hammer the client after the halt", len(client.fetch_calls) == 1, str(client.fetch_calls))
    check("a halt marker is written (a supervisor restart cannot loop)", ip.read_halt() is not None
          and "challenge_required" in ip.read_halt()["reason"])
    check("run_forever returns 'halted', no sleeping", t.run_forever(max_cycles=3) == "halted")
    client.raise_on_send = ip.CheckpointRequired("login_required")
    _raises(RuntimeError, t._send, T1, "x")
    check("a second login check after the halt does not notify again", len(NOTICES) == 1, str(NOTICES))

    # a checkpoint while SENDING halts too
    reset()
    client = FakeClient()
    t, clock = make_transport(client)
    client.raise_on_send = ip.CheckpointRequired("login_required")
    check("a login check on send halts and fails the send", _raises(RuntimeError, t._send, T1, "x") and t.halted)
    check("and the owner is told once", len(NOTICES) == 1)

    # error budget + backoff
    reset()
    client = FakeClient()
    t, clock = make_transport(client)
    t.step()
    client.raise_on_fetch = ip.TransportError("HTTP 429")
    results = [t.step() for _ in range(ip.MAX_FAILURES)]
    check("transient errors are 'error' until the budget runs out, then it halts",
          results[:-1] == ["error"] * (ip.MAX_FAILURES - 1) and results[-1] == "halted", str(results))
    check("one notice for the whole run", len(NOTICES) == 1)
    reset()
    client = FakeClient()
    t, clock = make_transport(client)
    d0 = t.delay()
    t.failures = 3
    d3 = t.delay()
    check("the wait grows exponentially after failures and is capped",
          d3 > d0 * 4 and d3 <= ip.BACKOFF_CAP_SECONDS and d0 == 45.0, f"{d0} {d3}")
    channel_config.set_value(INSTAGRAM, "group_poll_seconds", "100")
    t.failures = 0
    check("the interval is read from the config", t.delay() == 100.0)
    t2 = ip.Transport(client, rng=lambda: 1.0)
    check("jitter stretches the interval by at most 30%", abs(t2.delay() - 130.0) < 1e-6)

    # per-thread send gap
    reset({"group_send_gap_seconds": 20})
    client = FakeClient()
    t, clock = make_transport(client)
    t._send(T1, "one")
    t._send(T1, "two")
    check("two posts in one thread are spaced by the gap", clock.slept and abs(clock.slept[-1] - 20) < 1e-6, str(clock.slept))
    n = len(clock.slept)
    t._send(T2, "other thread")
    check("another thread is not delayed by it", len(clock.slept) == n)
    check("a client error on send becomes a plain failure for the caller",
          _raises(RuntimeError, _send_failing, t))


def _send_failing(t):
    t.client.raise_on_send = ip.TransportError("boom")
    t._send(T1, "x")


# --------------------------------------------------------------------------- G

def test_g_credentials():
    reset()
    client = FakeClient()
    ip.set_client_factory(lambda: client)
    password = "hunter2-PASSWORD-sentinel"
    answers = iter(["@dedicated_account", "123456"])

    class FakeStdin:
        def isatty(self):
            return True

    saved_in, saved_input, saved_getpass = sys.stdin, __builtins__["input"] if isinstance(__builtins__, dict) else __builtins__.input, ip.getpass.getpass
    out, err = io.StringIO(), io.StringIO()
    try:
        sys.stdin = FakeStdin()
        import builtins
        builtins.input = lambda prompt="": next(answers)
        ip.getpass.getpass = lambda prompt="": password
        with redirect_stdout(out), redirect_stderr(err):
            code = ip.login_cli()
    finally:
        sys.stdin = saved_in
        import builtins
        builtins.input = saved_input
        ip.getpass.getpass = saved_getpass
    check("login succeeds", code == 0, out.getvalue() + err.getvalue())
    check("the client got the credentials from the prompt", client.login_args == ("dedicated_account", password, "123456"))
    saved = json.loads(ip.SESSION_FILE.read_text(encoding="utf-8"))
    check("ONLY the session was stored", saved == {"cookie": "SESSION-SECRET-xyz", "user": "dedicated_account"})
    if os.name == "posix":
        check("the session file is mode 0600", (ip.SESSION_FILE.stat().st_mode & 0o777) == 0o600,
              oct(ip.SESSION_FILE.stat().st_mode & 0o777))
    disk = tree_text()
    check("the password is nowhere on disk", password not in disk)
    check("the password is not in anything printed", password not in out.getvalue() + err.getvalue())
    cfg_text = channel_config.CONFIG_FILE.read_text(encoding="utf-8")
    check("channels.json holds no credential or session", "SESSION-SECRET" not in cfg_text
          and "dedicated_account" not in cfg_text and password not in cfg_text)
    check("a halt marker is cleared by a fresh login", ip.read_halt() is None)

    ip.write_halt("test")
    check("the marker exists before", ip.read_halt() is not None)
    # a non-terminal run refuses (never from a pipe or a scheduled job)
    sys.stdin = io.StringIO("")
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            refused = ip.login_cli()
    finally:
        sys.stdin = saved_in
    check("login refuses to run without a terminal", refused == 1)

    # nothing in the log lines of a full polling run mentions a secret
    reset()
    client = FakeClient()
    ip.save_session({"cookie": "SESSION-SECRET-xyz"})
    t, clock = make_transport(client)
    buf = io.StringIO()
    with redirect_stderr(buf), StubModel():
        t.step()
        client.batch[T1] = [raw("z1", ALICE, "alice", "@jarvisbot hi", int(clock() * 1000) + 5)]
        t.step()
        client.raise_on_fetch = ip.CheckpointRequired("challenge_required")
        t.step()
    check("no log line carries the session", "SESSION-SECRET" not in buf.getvalue())
    check("the session is not in the transcript or the cursor file", "SESSION-SECRET" not in tree_text().replace(
        ip.SESSION_FILE.read_text(encoding="utf-8"), ""))


# --------------------------------------------------------------------------- H

def test_h_commands_and_rules():
    reset({"group_transport": False})
    err = io.StringIO()
    with redirect_stderr(err):
        code = ip.run()
    check("run() refuses while the switch is off", code == 1 and "OFF" in err.getvalue())

    reset()
    err = io.StringIO()
    with redirect_stderr(err):
        code = ip.run()
    check("run() needs a saved session", code == 1 and "instagram-group-login" in err.getvalue())

    ip.save_session({"cookie": "SESSION-SECRET-xyz"})
    channel_config.set_value(INSTAGRAM, "allowed_channels", "[]")
    cfg = channel_config.load_config()
    cfg[INSTAGRAM]["allowed_channels"] = []
    channel_config.save_config(cfg)
    err = io.StringIO()
    with redirect_stderr(err):
        code = ip.run()
    check("run() with no listed thread reads nothing and says how to list one",
          code == 1 and "channels-channels instagram add pt-" in err.getvalue(), err.getvalue())

    reset()
    ip.save_session({"cookie": "SESSION-SECRET-xyz"})
    err = io.StringIO()
    with redirect_stderr(err):
        code = ip.run()
    check("with nothing wired in, run() stops cleanly (no client library is bundled)", code == 1
          and "no Instagram private-API client" in err.getvalue(), err.getvalue())

    ip.write_halt("challenge_required")
    err = io.StringIO()
    with redirect_stderr(err):
        code = ip.run()
    check("after a halt, run() refuses until a new login", code == 2 and "instagram-group-login" in err.getvalue())
    ip.clear_halt()

    client = FakeClient()
    client.raise_on_fetch = ip.CheckpointRequired("resume")
    ip.set_client_factory(lambda: client)
    err = io.StringIO()
    with redirect_stderr(err):
        code = ip.run()
    check("a login check on start halts, notifies once and exits 2",
          code == 2 and ip.read_halt() is not None and len(NOTICES) == 1, str((code, NOTICES)))
    check("the saved session was handed to resume(), not printed", client.resumed == {"cookie": "SESSION-SECRET-xyz"}
          and "SESSION-SECRET" not in err.getvalue())

    from jarvis import policy, reserved_names
    check("the session file is on the sensitive-path list",
          any("instagram_private_session" in p for p, _w, _l in policy._SENSITIVE_PATHS))
    check("both commands are reserved names", {"instagram-group-serve", "instagram-group-login"} <= set(reserved_names.RESERVED_NAMES))
    from jarvis import bridge
    check("the bridge never hosts them", {"instagram-group-serve", "instagram-group-login"} <= set(bridge.UNHOSTED))
    check("channels_cli lists them", {"instagram-group-serve", "instagram-group-login"} <= set(channels_cli.COMMANDS))


def main():
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            FAILED.append(f"{fn.__name__} raised {type(exc).__name__}: {exc}\n" + traceback.format_exc(limit=5))
    total = PASSED + len(FAILED)
    print(f"{PASSED}/{total} passed")
    for failure in FAILED:
        print("  FAIL:", failure)
    shutil.rmtree(_HOME, ignore_errors=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
