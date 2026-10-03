"""Tests for L.28 — token waste measured from two Discord logs, 2026-10-02.

Three fixes are covered here:

  #2 (11,750 tokens in one day) A chat sender with no tool permission runs
     with JARVIS_ALLOWED_TOOLS="" — an EXPLICITLY EMPTY allowlist. ask() still
     offered search_tools / get_tool_schema / search_commands, every call to
     them was answered "tool not permitted", and the system prompt still told
     the model to "call search_tools first". Now an empty allowlist runs the
     ask as a plain no-tools ask. Unset (None) is still unrestricted.

  #4 (failover churn) An Ollama that is not running cost a ~4 s connect
     failure on every ask that reached it. A LOCAL endpoint that refused a
     connection is now skipped for 60 s across asks (never when it is the only
     provider, never for a remote host, ended early by a success).

  #1 (27,631 tokens for one greeting) A guest's "tell ur owner hi" matched none
     of notify_owner's owner-voice keywords ("notify me", "dm me" ...), so the
     router offered only the discovery tools. Guest phrasings now route to it.

No network, no API keys: adapters are fakes in ai_providers.ADAPTERS and every
store is redirected to a temp dir before any jarvis module is imported.

Run: python3 tests/test_l28_token_waste.py
"""

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ.pop("JARVIS_ALLOWED_TOOLS", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import (ai_client, ai_config, ai_providers, conversations,  # noqa: E402
                    key_health, logs, tool_router)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


@contextmanager
def _isolated_ask_env():
    orig = {
        "conv_dir": conversations.JARVIS_DIR, "conv_conv_dir": conversations.CONV_DIR,
        "conv_index": conversations.INDEX_FILE, "conv_current": conversations.CURRENT_FILE,
        "logs_dir": logs.JARVIS_DIR, "logs_log_dir": logs.LOG_DIR,
        "spawn": ai_client._spawn_title_update,
        "health_file": key_health.HEALTH_FILE,
    }
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conversations.JARVIS_DIR = tmp
        conversations.CONV_DIR = tmp / "conversations"
        conversations.INDEX_FILE = conversations.CONV_DIR / "index.json"
        conversations.CURRENT_FILE = tmp / "current_conversation.json"
        conversations.CONV_DIR.mkdir(parents=True, exist_ok=True)
        logs.JARVIS_DIR = tmp
        logs.LOG_DIR = tmp / "logs"
        key_health.HEALTH_FILE = tmp / "key_health.json"
        ai_client._spawn_title_update = lambda *a, **k: None
        try:
            yield conversations.get_current_id()
        finally:
            conversations.JARVIS_DIR = orig["conv_dir"]
            conversations.CONV_DIR = orig["conv_conv_dir"]
            conversations.INDEX_FILE = orig["conv_index"]
            conversations.CURRENT_FILE = orig["conv_current"]
            logs.JARVIS_DIR = orig["logs_dir"]
            logs.LOG_DIR = orig["logs_log_dir"]
            key_health.HEALTH_FILE = orig["health_file"]
            ai_client._spawn_title_update = orig["spawn"]


@contextmanager
def _env(**values):
    old = {k: os.environ.get(k) for k in values}
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _providers(provider_list, adapter, tools_enabled=True):
    orig_load = ai_config.load_ai_config
    orig_adapters = dict(ai_providers.ADAPTERS)
    cfg = {"persona": {}, "providers": provider_list,
           "defaults": {"tools_enabled": tools_enabled, "prompt_mode": "compact"}}
    ai_config.load_ai_config = lambda: cfg
    ai_providers.ADAPTERS["fake"] = adapter
    try:
        yield
    finally:
        ai_config.load_ai_config = orig_load
        ai_providers.ADAPTERS.clear()
        ai_providers.ADAPTERS.update(orig_adapters)


def _flatten(messages):
    """All text the model would see, however the messages are shaped."""
    out = []
    for m in messages or []:
        c = m.get("content") if isinstance(m, dict) else m
        out.append(c if isinstance(c, str) else repr(c))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# #2 — empty allowlist == no tools offered, no discovery instructions
# ---------------------------------------------------------------------------

def _capture_adapter(seen):
    def adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        seen.append({"tools": tools, "executor": tool_executor,
                     "prompt": _flatten(messages)})
        return ai_providers.AIResult(True, text="hi")
    return adapter


ONE = [{"name": "p", "type": "fake", "enabled": True, "api_keys": ["k1"], "model": "m"}]


def test_empty_allowlist_offers_no_tools_and_no_discovery_prompt():
    seen = []
    with _isolated_ask_env() as conv, _providers(ONE, _capture_adapter(seen)), \
            _env(JARVIS_ALLOWED_TOOLS=""):
        r = ai_client.ask("hello there", commands=[], conversation_id=conv)
    check("the ask answered", r.ok is True, r.attempts)
    check("no tool schemas reached the model", seen and not seen[0]["tools"], seen and seen[0]["tools"])
    check("no tool executor was built", seen and seen[0]["executor"] is None)
    check("the prompt does not tell the model to call search_tools",
          "search_tools" not in seen[0]["prompt"], "search_tools" in seen[0]["prompt"])
    check("nor mention search_commands", "search_commands" not in seen[0]["prompt"])


def test_unset_allowlist_is_still_unrestricted():
    seen = []
    with _isolated_ask_env() as conv, _providers(ONE, _capture_adapter(seen)), \
            _env(JARVIS_ALLOWED_TOOLS=None):
        r = ai_client.ask("hello there", commands=[], conversation_id=conv)
    check("the ask answered", r.ok is True)
    names = {t.get("name") for t in (seen[0]["tools"] or [])}
    check("discovery tools are still offered with no allowlist", "search_tools" in names, names)
    check("a tool executor exists", seen[0]["executor"] is not None)


def test_nonempty_allowlist_is_not_treated_as_empty():
    seen = []
    with _isolated_ask_env() as conv, _providers(ONE, _capture_adapter(seen)), \
            _env(JARVIS_ALLOWED_TOOLS="get_time"):
        r = ai_client.ask("hello there", commands=[], conversation_id=conv)
    check("the ask answered", r.ok is True)
    check("tools are still wired up for a non-empty allowlist", seen[0]["executor"] is not None)


def test_allowlist_probe():
    with _env(JARVIS_ALLOWED_TOOLS=""):
        check("'' is empty", ai_client._allowlist_is_empty() is True)
    with _env(JARVIS_ALLOWED_TOOLS=" , "):
        check("only separators is empty", ai_client._allowlist_is_empty() is True)
    with _env(JARVIS_ALLOWED_TOOLS="a,b"):
        check("a list is not empty", ai_client._allowlist_is_empty() is False)
    with _env(JARVIS_ALLOWED_TOOLS=None):
        check("unset is not empty", ai_client._allowlist_is_empty() is False)


# ---------------------------------------------------------------------------
# #4 — a refused LOCAL endpoint is skipped across asks
# ---------------------------------------------------------------------------

REFUSED = "couldn't connect to http://localhost:11434 - is it running?"


def _two_providers(local_url="http://localhost:11434", remote=True):
    lst = [{"name": "ollama-ish", "type": "fake", "enabled": True, "api_keys": ["x"],
            "model": "m", "base_url": local_url}]
    if remote:
        lst.append({"name": "cloud", "type": "fake", "enabled": True, "api_keys": ["z"],
                    "model": "m", "base_url": "https://api.example.com/v1"})
    return lst


def _routing_adapter(calls, fail_name="ollama-ish"):
    def adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        calls.append(resolved.get("name"))
        if resolved.get("name") == fail_name:
            return ai_providers.AIResult(False, error=REFUSED, kind=ai_providers.KIND_NETWORK)
        return ai_providers.AIResult(True, text="cloud answer")
    return adapter


def test_refused_local_host_is_skipped_on_the_next_ask():
    calls = []
    with _isolated_ask_env() as conv, _providers(_two_providers(), _routing_adapter(calls), False):
        r1 = ai_client.ask("hello", commands=[], conversation_id=conv)
        r2 = ai_client.ask("hello again", commands=[], conversation_id=conv)
    check("ask 1 tried the local provider first, then the cloud one",
          calls[:2] == ["ollama-ish", "cloud"], calls)
    check("ask 2 did NOT call the local provider again", calls[2:] == ["cloud"], calls)
    check("both asks were answered", r1.ok and r2.ok)
    note = [a for a in r2.attempts if a[0] == "ollama-ish"]
    check("ask 2's attempt list says why it was skipped",
          note and "refused a connection recently" in note[0][1], r2.attempts)


def test_window_expires():
    calls = []
    with _isolated_ask_env() as conv, _providers(_two_providers(), _routing_adapter(calls), False):
        ai_client.ask("hello", commands=[], conversation_id=conv)
        # Age the refusal past its window.
        state = key_health._load()
        for k in state["hosts"]:
            state["hosts"][k]["refused_until"] = 1.0
        key_health._save(state, 2.0)
        ai_client.ask("hello again", commands=[], conversation_id=conv)
    check("after the window the local provider is tried again",
          calls.count("ollama-ish") == 2, calls)


def test_a_lone_local_provider_is_never_skipped():
    # The owner may have just started Ollama; with nothing else to try, a
    # cached refusal must not turn into a guaranteed failure.
    calls = []
    with _isolated_ask_env() as conv, \
            _providers(_two_providers(remote=False), _routing_adapter(calls), False):
        ai_client.ask("hello", commands=[], conversation_id=conv)
        ai_client.ask("hello again", commands=[], conversation_id=conv)
    check("both asks reached the only provider", calls == ["ollama-ish", "ollama-ish"], calls)


def test_a_remote_host_is_never_parked():
    calls = []
    providers = _two_providers(local_url="https://flaky.example.com/v1")
    with _isolated_ask_env() as conv, _providers(providers, _routing_adapter(calls), False):
        ai_client.ask("hello", commands=[], conversation_id=conv)
        ai_client.ask("hello again", commands=[], conversation_id=conv)
        remembered = key_health._load()["hosts"]
    check("a refusing REMOTE provider is retried on the next ask",
          calls.count("ollama-ish") == 2, calls)
    check("and nothing was written for it", not remembered, remembered)


def test_success_ends_the_window_early():
    with _isolated_ask_env():
        key_health.record_host_refused("localhost:11434")
        check("window open", key_health.host_recently_refused("localhost:11434") > 0)
        key_health.clear_host_refused("localhost:11434")
        check("window closed by clear", key_health.host_recently_refused("localhost:11434") == 0)


def test_key_health_host_api_and_pruning():
    with _isolated_ask_env():
        check("unknown host -> 0", key_health.host_recently_refused("nope:1") == 0)
        check("empty host is a no-op", key_health.record_host_refused("") is None)
        key_health.record_host_refused("localhost:11434", now=1000.0)
        left = key_health.host_recently_refused("localhost:11434", now=1010.0)
        check("window length is HOST_REFUSED_COOLDOWN",
              abs(left - (key_health.HOST_REFUSED_COOLDOWN - 10.0)) < 1e-6, left)
        check("expired -> 0", key_health.host_recently_refused(
            "localhost:11434", now=1000.0 + key_health.HOST_REFUSED_COOLDOWN + 1) == 0)
        key_health.record_failure("p", "m", None, "overload", "x", now=5000.0)  # any save prunes
        check("expired host entries are pruned on save",
              "localhost:11434" not in key_health._load()["hosts"])
        # An old file with no "hosts" section must still load.
        key_health.HEALTH_FILE.write_text('{"keys": {}, "models": {}, "last_good": {}}', encoding="utf-8")
        check("a pre-L.28 health file still loads", key_health._load()["hosts"] == {})


def test_loopback_detection():
    f = ai_client._is_loopback_host
    check("localhost:11434", f("localhost:11434"))
    check("127.0.0.1:11434", f("127.0.0.1:11434"))
    check("[::1]:11434", f("[::1]:11434"))
    check("bare localhost", f("localhost"))
    check("LAN address is not loopback", not f("192.168.1.20:11434"))
    check("a cloud host is not loopback", not f("api.groq.com"))
    check("a hostname merely containing 127 is not loopback", not f("my127.example.com"))
    check("empty is not loopback", not f(""))


# ---------------------------------------------------------------------------
# #1 — guest phrasings reach notify_owner without a discovery round trip
# ---------------------------------------------------------------------------

def test_guest_phrasings_route_to_the_channels_group():
    for text in ("Tell ur owner hi", "tell your owner i said hi", "let ur owner know its urgent",
                 "can you message the owner?", "dm your owner please", "notify the owner"):
        r = tool_router.route(text)
        check(f"routes: {text!r}", r.confident and "channels" in r.groups, (r.confident, r.groups))
        check(f"  to notify_owner specifically: {text!r}",
              any(m[1] == "notify_owner" for m in (r.matches or [])), r.matches)


def test_ordinary_mentions_of_an_owner_do_not_route_there():
    for text in ("hello there", "what's the owner of that file", "my owner told me to say hi",
                 "who owns this repo", "the owner is away"):
        r = tool_router.route(text)
        check(f"does not route to channels: {text!r}", "channels" not in (r.groups or []), r.groups)


def test_the_original_owner_voice_phrases_still_work():
    for text in ("dm me when it's done", "notify me", "let me know when the build finishes"):
        r = tool_router.route(text)
        check(f"still routes: {text!r}", "channels" in (r.groups or []), r.groups)


# --- runner (keep BELOW every test: it reads globals() when it executes) ----

if __name__ == "__main__":
    for name in sorted(n for n in list(globals()) if n.startswith("test_")):
        print(f"\n== {name}")
        try:
            globals()[name]()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAIL.append((name, f"raised {exc!r}"))
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for n, d in FAIL:
        print(f"  FAILED: {n}: {d}")
    sys.exit(1 if FAIL else 0)
