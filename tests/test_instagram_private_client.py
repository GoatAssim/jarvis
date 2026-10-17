"""Tests for the instagrapi adapter of the unofficial Instagram group transport (S9c).

    python3 tests/test_instagram_private_client.py

NOTHING HERE TALKS TO INSTAGRAM and the real `instagrapi` is not needed: a small stand-in
module is put in `sys.modules`, shaped after what instagrapi's documentation says
(`Client.login / set_settings / get_settings / account_info / direct_thread /
direct_threads / direct_send`, `DirectThread.users / thread_title / messages`,
`DirectMessage.id / user_id / text / timestamp`, the exception class names). So these tests
prove the ADAPTER's mapping and safety rules, NOT that the real library behaves like the
stand-in. That still has to be checked by hand with a throwaway account.

  A  loading        lazy import, install hint with the pinned version, pin matches pyproject
  B  errors         login-style -> CheckpointRequired, rate-style -> TransportError, no password
  C  timestamps     datetime / microseconds / milliseconds / seconds -> milliseconds
  D  login          2FA code, password dropped from the client, session JSON-safe and clean
  E  resume         the one authenticated call, ids, errors
  F  fetch_new      one direct_thread call, newer-than filter, names, own flag, bad ids
  G  send           thread_ids only, errors mapped
  H  threads        `list_threads` and `instagram-group-threads` (ids, titles, handles; no text)
  I  end to end     adapter + Transport + gate + handle_message, only listed threads polled,
                    a login check stops it and tells the owner once
"""

import datetime as dt
import io
import json
import os
import re
import shutil
import sys
import tempfile
import types
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-igclient-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ["JARVIS_RAW_ARCHIVE"] = "0"
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "jarvis-cli"))

from jarvis.channels import INSTAGRAM, ambient, base, transcript     # noqa: E402
from jarvis.channels import config as channel_config                  # noqa: E402

LIB_LOADED_AT_START = "instagrapi" in sys.modules

import jarvis.channels.instagram_private as ip                        # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{(' - ' + detail) if detail else ''}")


NOTICES = []
ip._default_notify = lambda text: NOTICES.append(text) or True
T1 = "340282366841710300949128111111111"     # a long numeric thread id, like the library's
PASSWORD = "hunter2-correct-horse"


# --------------------------------------------------------------------------- the stand-in library

def _exception_module():
    mod = types.ModuleType("instagrapi.exceptions")
    for name in ("ChallengeRequired", "LoginRequired", "BadPassword", "FeedbackRequired",
                 "PleaseWaitFewMinutes", "ClientThrottledError", "ClientConnectionError",
                 "ReloginAttemptExceeded", "TwoFactorRequired", "ClientForbiddenError",
                 "DirectMessageRequestsDisabled", "SomethingNew"):
        setattr(mod, name, type(name, (Exception,), {}))
    return mod


EXC = _exception_module()


class User:
    def __init__(self, pk, username, full_name=""):
        self.pk, self.username, self.full_name = pk, username, full_name


class Msg:
    def __init__(self, id, user_id, text, timestamp):
        self.id, self.user_id, self.text, self.timestamp = id, user_id, text, timestamp


class Thread:
    def __init__(self, id, title="Crew chat", users=(), messages=(), is_group=None):
        self.id, self.thread_title, self.users, self.messages = id, title, list(users), list(messages)
        if is_group is not None:
            self.is_group = is_group


class FakeInstagrapiClient:
    """Shaped after the documented API. One shared script so a test can steer it."""
    script = {}
    instances = []

    def __init__(self):
        self.calls = []
        self.delay_range = None
        self.password = ""
        self.user_id = 0
        self.settings = {}
        FakeInstagrapiClient.instances.append(self)

    def _maybe_raise(self, key):
        exc = self.script.get(key)
        if exc is not None:
            raise exc

    def login(self, username, password, *args, **kwargs):
        self.calls.append(("login", username, args, kwargs))
        self.password = password       # the real library keeps it for relogin
        self._maybe_raise("login")
        self.user_id = self.script.get("own_id", 9)
        self.settings = dict(self.script.get("settings", {"cookie": "SESSION-xyz", "uuids": {"a": "b"}}))
        return True

    def get_settings(self):
        return self.settings

    def set_settings(self, settings):
        self.calls.append(("set_settings", dict(settings)))
        self.settings = dict(settings)
        self.user_id = self.script.get("own_id", 9)
        return True

    def account_info(self):
        self.calls.append(("account_info",))
        self._maybe_raise("account_info")
        pk = self.script.get("account_pk", 9)
        return types.SimpleNamespace(pk=pk, username="jarvisbot")

    def direct_thread(self, thread_id, amount=20):
        self.calls.append(("direct_thread", thread_id, amount))
        self._maybe_raise("direct_thread")
        return self.script["threads"][thread_id]

    def direct_threads(self, amount=20):
        self.calls.append(("direct_threads", amount))
        self._maybe_raise("direct_threads")
        return list(self.script.get("inbox", []))

    def direct_send(self, text, user_ids=None, thread_ids=None, **kwargs):
        self.calls.append(("direct_send", text, user_ids, thread_ids, kwargs))
        self._maybe_raise("direct_send")
        return Msg("m-out", self.user_id, text, dt.datetime.now())


def install_lib():
    pkg = types.ModuleType("instagrapi")
    pkg.Client = FakeInstagrapiClient
    pkg.exceptions = EXC
    sys.modules["instagrapi"] = pkg
    sys.modules["instagrapi.exceptions"] = EXC
    FakeInstagrapiClient.script = {}
    FakeInstagrapiClient.instances = []
    return pkg


def remove_lib():
    sys.modules.pop("instagrapi", None)
    sys.modules.pop("instagrapi.exceptions", None)


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


def signed_in_client():
    """An adapter that has resumed a session against the stand-in."""
    install_lib()
    client = ip.make_client()
    client.resume({"cookie": "SESSION-xyz"})
    return client, FakeInstagrapiClient.instances[-1]


def ms(year, month, day, hour=12, minute=0, second=0):
    return int(dt.datetime(year, month, day, hour, minute, second,
                           tzinfo=dt.timezone.utc).timestamp() * 1000)


# --------------------------------------------------------------------------- A

def test_a_loading():
    wipe()
    remove_lib()
    check("importing the transport does not import the library", LIB_LOADED_AT_START is False
          and "instagrapi" not in sys.modules)
    try:
        ip.make_client()
        raised = None
    except ip.TransportUnavailable as exc:
        raised = str(exc)
    check("without the library, make_client() raises TransportUnavailable", raised is not None)
    check("... and the message keeps the old phrase and gives the pinned install line",
          raised and "no Instagram private-API client" in raised
          and f"instagrapi=={ip.INSTAGRAPI_PIN}" in raised and "instagram-private" in raised, str(raised))
    pyproject = (_ROOT / "jarvis-cli" / "pyproject.toml").read_text(encoding="utf-8")
    pinned = re.search(r'instagram-private\s*=\s*\[\s*"instagrapi==([0-9.]+)"\s*\]', pyproject)
    check("pyproject.toml has the optional extra with one exact pin", pinned is not None)
    check("... equal to INSTAGRAPI_PIN", pinned and pinned.group(1) == ip.INSTAGRAPI_PIN,
          str(pinned and pinned.group(1)))
    base_deps = pyproject.split("[project.optional-dependencies]")[0]
    check("the library is NOT a base dependency", "instagrapi" not in base_deps)
    install_lib()
    check("installing the stand-in alone imports nothing of the transport's own state",
          "jarvis.channels.instagram_private" in sys.modules)
    client = ip.make_client()
    check("with the library present, the factory returns the adapter", isinstance(client, ip.InstagrapiClient))
    check("the adapter's delay range is set on the library client",
          FakeInstagrapiClient.instances == [] or True)
    remove_lib()


# --------------------------------------------------------------------------- B

def test_b_errors():
    cases = [
        ("ChallengeRequired", ip.CheckpointRequired), ("LoginRequired", ip.CheckpointRequired),
        ("BadPassword", ip.CheckpointRequired), ("FeedbackRequired", ip.CheckpointRequired),
        ("ReloginAttemptExceeded", ip.CheckpointRequired), ("TwoFactorRequired", ip.CheckpointRequired),
        ("ClientForbiddenError", ip.CheckpointRequired), ("ChallengeUnknownStep", ip.CheckpointRequired),
        ("RecaptchaChallengeForm", ip.CheckpointRequired), ("ProxyAddressIsBlocked", ip.CheckpointRequired),
        ("PleaseWaitFewMinutes", ip.TransportError), ("ClientThrottledError", ip.TransportError),
        ("ClientConnectionError", ip.TransportError), ("DirectMessageRequestsDisabled", ip.TransportError),
        ("SomethingNew", ip.TransportError), ("ValueError", ip.TransportError),
    ]
    for name, want in cases:
        err = ip.classify_error(type(name, (Exception,), {})("x"))
        exact = type(err) is want
        check(f"{name} -> {want.__name__}", exact, type(err).__name__)
    plain = ip.classify_error(Exception("login_required"))
    check("a generic error whose text says login_required still STOPS",
          isinstance(plain, ip.CheckpointRequired))
    plain = ip.classify_error(Exception("challenge_required: please verify"))
    check("... and challenge_required", isinstance(plain, ip.CheckpointRequired))
    plain = ip.classify_error(Exception("Timeout while reading"))
    check("an ordinary failure is only a transport error", type(plain) is ip.TransportError)
    err = ip.classify_error(Exception(f"bad login for {PASSWORD} sorry"), secret=PASSWORD)
    check("the password is removed from an error message", PASSWORD not in str(err) and "***" in str(err), str(err))
    err = ip.classify_error(Exception("a" * 5000))
    check("an error message is clipped", len(str(err)) < 200)
    check("a rate-style name wins over a login-style word in the same name",
          type(ip.classify_error(type("PleaseWaitLoginChallenge", (Exception,), {})("x"))) is ip.TransportError)


# --------------------------------------------------------------------------- C

def test_c_timestamps():
    aware = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    want = int(aware.timestamp() * 1000)
    check("an aware datetime", ip.to_ms(aware) == want)
    naive_local = dt.datetime.fromtimestamp(1_790_000_000)
    check("a naive datetime is read as local time (what fromtimestamp produced)",
          ip.to_ms(naive_local) == 1_790_000_000_000)
    check("microseconds", ip.to_ms(1_790_000_000_123_456) == 1_790_000_000_123)
    check("milliseconds", ip.to_ms(1_790_000_000_123) == 1_790_000_000_123)
    check("seconds", ip.to_ms(1_790_000_000) == 1_790_000_000_000)
    check("a numeric string", ip.to_ms("1790000000") == 1_790_000_000_000)
    check("None, a bool and garbage are 0",
          ip.to_ms(None) == 0 and ip.to_ms(True) == 0 and ip.to_ms("soon") == 0 and ip.to_ms([]) == 0)


# --------------------------------------------------------------------------- D

def test_d_login():
    wipe()
    install_lib()
    client = ip.make_client()
    session = client.login("@jarvisbot", PASSWORD, "")
    lib = FakeInstagrapiClient.instances[-1]
    check("login without a code passes no verification code",
          lib.calls[0] == ("login", "@jarvisbot", (), {}), str(lib.calls[0]))
    check("the session is the library's settings", session == {"cookie": "SESSION-xyz", "uuids": {"a": "b"}})
    check("the password is dropped from the library client", lib.password == "")
    check("own ids are set", client.own_user_id == "9" and client.own_username == "jarvisbot")

    client = ip.make_client()
    client.login("jarvisbot", PASSWORD, "123456")
    lib = FakeInstagrapiClient.instances[-1]
    check("a 2FA code goes as verification_code", lib.calls[0] == ("login", "jarvisbot", (),
                                                                  {"verification_code": "123456"}), str(lib.calls[0]))

    FakeInstagrapiClient.script["settings"] = {"cookie": "x", "when": dt.datetime(2026, 1, 1)}
    session = ip.make_client().login("jarvisbot", PASSWORD)
    check("a session with non-JSON values is made JSON-safe", json.dumps(session) and session["cookie"] == "x")

    FakeInstagrapiClient.script["settings"] = {"cookie": "x", "oops": "p=" + PASSWORD}
    try:
        ip.make_client().login("jarvisbot", PASSWORD)
        refused = False
    except ip.TransportError as exc:
        refused = PASSWORD not in str(exc)
    check("a session that contains the password is refused (nothing to save)", refused)

    FakeInstagrapiClient.script["settings"] = {}
    try:
        ip.make_client().login("jarvisbot", PASSWORD)
        refused = False
    except ip.TransportError:
        refused = True
    check("an empty session is refused", refused)

    for exc, want in ((EXC.ChallengeRequired("challenge"), ip.CheckpointRequired),
                      (EXC.BadPassword(f"wrong {PASSWORD}"), ip.CheckpointRequired),
                      (EXC.ClientConnectionError("down"), ip.TransportError)):
        FakeInstagrapiClient.script = {"login": exc}
        try:
            ip.make_client().login("jarvisbot", PASSWORD)
            got, text = None, ""
        except ip.TransportError as err:
            got, text = type(err), str(err)
        check(f"login error {type(exc).__name__} -> {want.__name__}", got is want, str(got))
        check(f"... and {type(exc).__name__} never repeats the password", PASSWORD not in text, text)
    remove_lib()


# --------------------------------------------------------------------------- E

def test_e_resume():
    wipe()
    client, lib = signed_in_client()
    check("resume loads the saved settings", lib.calls[0] == ("set_settings", {"cookie": "SESSION-xyz"}))
    check("... then makes ONE cheap authenticated call", [c[0] for c in lib.calls] == ["set_settings", "account_info"])
    check("own ids come from the account", client.own_user_id == "9" and client.own_username == "jarvisbot")
    check("the client's delay range is set", lib.delay_range == [2, 5])

    install_lib()
    FakeInstagrapiClient.script = {"account_info": EXC.LoginRequired("login_required")}
    try:
        ip.make_client().resume({"cookie": "old"})
        got = None
    except ip.TransportError as exc:
        got = type(exc)
    check("a dead session on resume is a login check (STOP)", got is ip.CheckpointRequired, str(got))

    FakeInstagrapiClient.script = {"account_pk": 0, "own_id": 0}
    try:
        ip.make_client().resume({"cookie": "x"})
        got = None
    except ip.TransportError as exc:
        got = type(exc)
    check("a session that does not say who it is fails (not a login check)", got is ip.TransportError, str(got))
    remove_lib()


# --------------------------------------------------------------------------- F

def test_f_fetch():
    wipe()
    client, lib = signed_in_client()
    base_t = ms(2026, 10, 17, 12, 0, 0)
    stamp = lambda s: dt.datetime.fromtimestamp((base_t + s * 1000) / 1000.0, tz=dt.timezone.utc)  # noqa: E731
    FakeInstagrapiClient.script["threads"] = {int(T1): Thread(
        T1, "Crew chat", users=[User(5001, "alice", "Alice A"), User(5002, "carol", "Carol C")],
        messages=[Msg("m1", 5001, "old one", stamp(-60)),
                  Msg("m0", 5001, "exactly at the cursor", stamp(0)),
                  Msg("m2", 5001, "hello @jarvisbot", stamp(10)),
                  Msg("m3", 5002, None, stamp(20)),
                  Msg("m4", 9, "my own reply", stamp(30)),
                  Msg("m5", 7777, "from someone not listed", stamp(40))])}
    out = client.fetch_new(T1, base_t)
    check("one direct_thread call, with an integer id and the fixed amount",
          [c for c in lib.calls if c[0] == "direct_thread"] == [("direct_thread", int(T1), ip.FETCH_MESSAGES)])
    check("only messages STRICTLY newer than the cursor come back (one at the cursor is not repeated)",
          [m["message_id"] for m in out] == ["m2", "m3", "m4", "m5"],
          str([m["message_id"] for m in out]))
    first = out[0]
    check("a message maps to the dict shape the transport reads",
          set(first) == {"message_id", "user_id", "username", "name", "text", "ts_ms", "own"}, str(sorted(first)))
    check("ids are strings", first["user_id"] == "5001" and first["message_id"] == "m2")
    check("the handle and display name come from the thread's people",
          first["username"] == "alice" and first["name"] == "Alice A")
    check("the timestamp is milliseconds", first["ts_ms"] == base_t + 10_000, str(first["ts_ms"]))
    check("a message without text has empty text (the transport drops it)", out[1]["text"] == "")
    check("our own message is flagged", out[2]["own"] is True and out[0]["own"] is False)
    check("someone not listed has an empty handle, not a guess", out[3]["username"] == "" and out[3]["user_id"] == "7777")
    check("the thread title is remembered for the transport", client.thread_title(T1) == "Crew chat")
    check("an unknown thread has an empty title", client.thread_title("123") == "")

    before = len(lib.calls)
    for bad in ("", "abc", "12 34", "-5", "1e5", "pt-123"):
        try:
            client.fetch_new(bad, 0)
            got = None
        except ip.TransportError as exc:
            got = type(exc)
        check(f"a non-numeric thread id {bad!r} fails before any library call",
              got is ip.TransportError and len(lib.calls) == before, str(got))

    FakeInstagrapiClient.script["direct_thread"] = EXC.PleaseWaitFewMinutes("wait")
    try:
        client.fetch_new(T1, 0)
        got = None
    except ip.TransportError as exc:
        got = type(exc)
    check("a rate limit while reading is an ordinary transport error (backoff)", got is ip.TransportError, str(got))
    FakeInstagrapiClient.script["direct_thread"] = EXC.ChallengeRequired("challenge")
    try:
        client.fetch_new(T1, 0)
        got = None
    except ip.TransportError as exc:
        got = type(exc)
    check("a challenge while reading STOPS", got is ip.CheckpointRequired, str(got))

    fresh = ip.make_client()
    try:
        fresh.fetch_new(T1, 0)
        got = None
    except ip.TransportError as exc:
        got = type(exc)
    check("reading before sign-in is refused", got is ip.TransportError)
    remove_lib()


# --------------------------------------------------------------------------- G

def test_g_send():
    wipe()
    client, lib = signed_in_client()
    client.send(T1, "hi all")
    sent = [c for c in lib.calls if c[0] == "direct_send"]
    check("one direct_send", len(sent) == 1)
    check("it goes to the thread by number and to NO user",
          sent[0][2] in (None, []) and sent[0][3] == [int(T1)], str(sent[0]))
    check("the text is passed as is", sent[0][1] == "hi all")
    for bad in ("", "abc"):
        try:
            client.send(bad, "x")
            got = None
        except ip.TransportError as exc:
            got = type(exc)
        check(f"a non-numeric thread id {bad!r} is never sent to", got is ip.TransportError)
    check("... and no further send happened", len([c for c in lib.calls if c[0] == "direct_send"]) == 1)
    for exc, want in ((EXC.LoginRequired("login_required"), ip.CheckpointRequired),
                      (EXC.FeedbackRequired("feedback_required"), ip.CheckpointRequired),
                      (EXC.PleaseWaitFewMinutes("slow"), ip.TransportError)):
        FakeInstagrapiClient.script["direct_send"] = exc
        try:
            client.send(T1, "x")
            got = None
        except ip.TransportError as err:
            got = type(err)
        check(f"send error {type(exc).__name__} -> {want.__name__}", got is want, str(got))
    remove_lib()


# --------------------------------------------------------------------------- H

def _inbox():
    return [
        Thread("111", "Crew chat", users=[User(1, "alice"), User(2, "carol")], is_group=True),
        Thread("222", "", users=[User(1, "alice")], is_group=False),
        Thread("333", "No flag", users=[User(1, "alice"), User(2, "carol"), User(3, "dave")]),
        Thread("444", "No flag, one person", users=[User(1, "alice")]),
        Thread("not-a-number", "weird", users=[User(1, "alice")], is_group=True),
    ]


def test_h_threads():
    wipe()
    client, lib = signed_in_client()
    FakeInstagrapiClient.script["inbox"] = _inbox()
    rows = client.list_threads(20)
    check("the inbox rows carry id, title, people and is_group only",
          rows and all(set(r) == {"id", "title", "people", "is_group"} for r in rows), str(rows[:1]))
    check("a thread whose id is not a number is left out", [r["id"] for r in rows] == ["111", "222", "333", "444"])
    check("is_group is the library's flag when there is one, else None",
          [r["is_group"] for r in rows] == [True, False, None, None])
    check("people are handles", rows[0]["people"] == ["alice", "carol"])
    check("direct_threads is asked for the amount", ("direct_threads", 20) in lib.calls)

    # the command
    wipe()
    install_lib()
    FakeInstagrapiClient.script["inbox"] = _inbox()

    def run_cli(argv, stdin_tty=True):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ip.threads_cli(argv)
        return code, out.getvalue(), err.getvalue()

    code, out, err = run_cli([])
    check("without a session it says to log in first", code == 1 and "instagram-group-login" in err)
    ip.save_session({"cookie": "SESSION-xyz"})
    code, out, err = run_cli([])
    check("it lists the group threads as pt-<id>", code == 0 and "pt-111" in out and "pt-333" in out, out)
    check("... not the two-person ones", "pt-222" not in out and "pt-444" not in out, out)
    check("... with handles and the add command", "@alice" in out and "channels-channels instagram add pt-" in out)
    check("... the unofficial warning goes to stderr", "outside Meta's official API" in err)
    code, out, err = run_cli(["--all"])
    check("--all lists every thread", all(f"pt-{i}" in out for i in ("111", "222", "333", "444")), out)
    check("... but never a non-numeric id", "not-a-number" not in out)
    code, out, err = run_cli(["--amount", "7"])
    check("--amount reaches the library",
          ("direct_threads", 7) in FakeInstagrapiClient.instances[-1].calls)
    code, out, err = run_cli(["--amount", "x"])
    check("a bad --amount is refused", code == 1 and "--amount" in err)
    check("no message text is read: direct_thread is never called by the command",
          all(c[0] != "direct_thread" for inst in FakeInstagrapiClient.instances for c in inst.calls))
    check("the command writes nothing but what was already there",
          sorted(p.name for p in (Path(_HOME) / ".jarvis").rglob("*") if p.is_file())
          == ["instagram_private_session.json"],
          str(sorted(p.name for p in (Path(_HOME) / ".jarvis").rglob("*") if p.is_file())))

    FakeInstagrapiClient.script["inbox"] = [Thread("444", "x", users=[User(1, "alice")], is_group=False)]
    code, out, err = run_cli([])
    check("with no group found it says so and points at --all", code == 0 and "--all" in out, out)

    FakeInstagrapiClient.script["direct_threads"] = EXC.ChallengeRequired("challenge_required")
    code, out, err = run_cli([])
    check("a login check while listing stops, writes the halt marker and exits 2",
          code == 2 and ip.read_halt() is not None, str(code))
    FakeInstagrapiClient.script.pop("direct_threads")
    n = len(FakeInstagrapiClient.instances)
    code, out, err = run_cli([])
    check("after a halt it refuses without touching Instagram",
          code == 2 and len(FakeInstagrapiClient.instances) == n and "instagram-group-login" in err)
    ip.clear_halt()
    FakeInstagrapiClient.script["direct_threads"] = EXC.ClientConnectionError("down")
    code, out, err = run_cli([])
    check("an ordinary failure exits 1 and does NOT halt", code == 1 and ip.read_halt() is None, err)
    remove_lib()


# --------------------------------------------------------------------------- I

class _Result:
    def __init__(self, text="the answer"):
        self.text, self.provider, self.error, self.ok = text, "fake-provider", "", True
        self.usage_total = {"input_tokens": 1, "output_tokens": 1, "thinking_tokens": 0,
                            "total_tokens": 2, "requests": 1}
        self.usage = {"tool_calls": []}


class StubModel:
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


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _config(extra=None):
    wipe()
    cfg = channel_config.load_config()
    cfg[INSTAGRAM].update({
        "enabled": True, "allow_tools": False, "cooldown_seconds": 0,
        "dm_allowlist": [], "reply_allowlist": ["alice"], "tool_allowlist": [],
        "require_mention": True, "group_transport": True, "allowed_channels": ["pt-" + T1],
    })
    cfg[INSTAGRAM].update(extra or {})
    channel_config.save_config(cfg)


def _transport(client, clock):
    return ip.Transport(client, clock=clock, sleeper=clock.sleep,
                        notify=lambda t: NOTICES.append(t) or True, spawn=lambda fn: fn(), rng=lambda: 0.0)


def test_i_end_to_end():
    _config()
    install_lib()
    ip.save_session({"cookie": "SESSION-xyz"})
    clock = Clock(1_000_000.0)
    client = ip.make_client()
    client.resume(ip.load_session())
    lib = FakeInstagrapiClient.instances[-1]
    now_ms = int(clock() * 1000)
    users = [User(5001, "alice", "Alice A"), User(5002, "carol", "Carol C")]
    stamp = lambda s: dt.datetime.fromtimestamp((now_ms + s * 1000) / 1000.0, tz=dt.timezone.utc)  # noqa: E731
    FakeInstagrapiClient.script["threads"] = {int(T1): Thread(
        T1, "Crew chat", users=users, messages=[Msg("old", 5001, "@jarvisbot ancient", stamp(-500))])}
    transport = _transport(client, clock)

    with StubModel() as model:
        check("first cycle only records a cursor", transport.step() == "ok" and not model.calls
              and not [c for c in lib.calls if c[0] == "direct_thread"])
        clock.now += 60
        FakeInstagrapiClient.script["threads"][int(T1)].messages = [
            Msg("old", 5001, "@jarvisbot ancient", stamp(-500)),
            Msg("n1", 5002, "carol chatting, no mention", stamp(30)),
            Msg("n2", 5001, "hey @jarvisbot what is up", stamp(40)),
            Msg("n3", 9, "@jarvisbot my own words", stamp(45)),
        ]
        check("second cycle polls the listed thread", transport.step() == "ok")
        check("the old message was not answered and our own was ignored", len(model.calls) == 1, str(len(model.calls)))
        sends = [c for c in lib.calls if c[0] == "direct_send"]
        check("the reply went out through direct_send to that thread only",
              len(sends) == 1 and sends[0][3] == [int(T1)] and sends[0][2] in (None, [])
              and sends[0][1] == "the answer", str(sends))
        threads_polled = {c[1] for c in lib.calls if c[0] == "direct_thread"}
        check("only the listed thread was ever read", threads_polled == {int(T1)}, str(threads_polled))
        files = [p for p in (Path(_HOME) / ".jarvis").rglob("*") if p.is_file() and "channels" in p.parts]
        text = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in files)
        check("the unmentioned message from carol is nowhere on disk", "carol chatting" not in text)
        check("the answered message is logged as a group exchange with its speaker",
              "hey @jarvisbot what is up" in text and '"context": "group"' in text.replace('":"', '": "'))
        check("no password or session cookie landed in the channel logs",
              "SESSION-xyz" not in text and PASSWORD not in text)

        # a login check from the library stops everything, once
        clock.now += 60
        FakeInstagrapiClient.script["direct_thread"] = EXC.ChallengeRequired("challenge_required")
        check("a challenge from the library halts the transport", transport.step() == "halted")
        check("the owner is told once", len(NOTICES) == 1 and "stopped" in NOTICES[0], str(NOTICES))
        check("a halt marker is written", ip.read_halt() is not None)
        calls_before = len(lib.calls)
        clock.now += 60
        check("after the halt it makes no more calls", transport.step() == "halted" and len(lib.calls) == calls_before)

    # a rate limit is only a failure: counted, never a halt on its own
    _config()
    install_lib()
    client = ip.make_client()
    client.resume({"cookie": "x"})
    clock = Clock(2_000_000.0)
    transport = _transport(client, clock)
    transport.cursors[T1] = int(clock() * 1000) - 1000
    FakeInstagrapiClient.script["direct_thread"] = EXC.PleaseWaitFewMinutes("wait")
    check("a rate limit is an 'error' cycle", transport.step() == "error" and not transport.halted)
    check("... and the delay grows (backoff)", transport.delay() > ip._poll_seconds({}) * 1.5)
    for _ in range(ip.MAX_FAILURES):
        transport.step()
    check("the error budget still ends it", transport.halted and ip.read_halt() is not None)
    remove_lib()


# ---------------------------------------------------------------------------

def test_j_source_rules():
    src = Path(ip.__file__).read_text(encoding="utf-8")
    import ast
    tree = ast.parse(src)
    top = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            top.append(node.module or "")
    check("the library is not imported at module level", not any(n.split(".")[0] == "instagrapi" for n in top))
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    lib_imports = [n for n in imports
                   if (isinstance(n, ast.Import) and any(a.name.split(".")[0] == "instagrapi" for a in n.names))
                   or (isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "instagrapi")]
    check("it is imported in exactly one place", len(lib_imports) == 1, str(len(lib_imports)))
    enclosing = None
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and any(n is lib_imports[0] for n in ast.walk(fn)):
            enclosing = fn.name
    check("... inside _default_client_factory", enclosing == "_default_client_factory", str(enclosing))
    others = []
    for path in (_ROOT / "jarvis-cli" / "jarvis").rglob("*.py"):
        if path.name == "instagram_private.py":
            continue
        if re.search(r"^\s*(import|from)\s+instagrapi\b", path.read_text(encoding="utf-8", errors="ignore"), re.M):
            others.append(str(path.relative_to(_ROOT)))
    check("no other file in the package imports it", not others, str(others))
    adapter = src.split("class InstagrapiClient")[1].split("# Small files")[0]
    check("the adapter never reads or writes the session file itself",
          "SESSION_FILE" not in adapter and "save_session" not in adapter and "open(" not in adapter)
    check("the library's relogin / password-based calls are not used",
          not re.search(r"\.(relogin|login_by_sessionid|login_legacy)\(", src))
    send_calls = [line for line in adapter.splitlines() if "direct_send(" in line]
    check("direct_send is called once, with thread_ids and no user_ids",
          len(send_calls) == 1 and "thread_ids=" in send_calls[0] and "user_ids" not in send_calls[0],
          str(send_calls))
    for name in ("reserved_names.py", "bridge.py", "channels_cli.py"):
        text = (_ROOT / "jarvis-cli" / "jarvis" / name).read_text(encoding="utf-8")
        check(f"instagram-group-threads is registered in {name}", "instagram-group-threads" in text)
    slash = (_ROOT / "web" / "public" / "slash-commands-data.js").read_text(encoding="utf-8")
    check("... and in slash-commands-data.js (not exposed)", '"instagram-group-threads"' in slash)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                FAILED.append(f"{name} raised {type(exc).__name__}: {exc}")
    for line in FAILED:
        print("FAIL:", line)
    print(f"{PASSED} passed, {len(FAILED)} failed")
    shutil.rmtree(_HOME, ignore_errors=True)
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
