"""Tests for the L.28 follow-ups (2026-10-03), the four items the first L.28
patch left alone:

  #3  send_dm ask cost 10,296 tokens: the model called recent_dms (limit 10)
      only to learn the handle behind "mariem" (the saved handle is
      maryem.not.maryam11_16812). send_dm's unknown-name error now lists
      CLOSE SPELLINGS as suggestions (did_you_mean) and orders known_contacts
      nearest-first, and the channels instruction says to call send_dm first.
      Invariant kept (AGENTS.md): send_dm never guesses a recipient. A near
      miss is a suggestion to confirm, never a match.

  #5  An owner message went unanswered for 4+ minutes with no row in the log.
      The cause is unknown, so nothing is guessed or killed: channels/base.py
      now writes a watchdog row (once per STALL_LOG_SECONDS) naming what the ask
      was last doing, and a row when an ask waited behind another on _ASK_LOCK.

  guests with tools  A chat guest who may use tools was still OFFERED send_dm
      and recent_dms (owner-only: every call is refused) and search_tools
      listed them. They are no longer offered to a non-owner sender. The
      handlers' own owner checks - the real gate - are untouched and re-asserted.

  Groq tool_use_failed  Groq's error code for an unusable tool call is now a
      request-shape failure: the other keys of that provider are skipped.

No network, no API keys; every store is redirected to a temp dir first.

Run: python3 tests/test_l28b_followups.py
"""

import json
import os
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
for _k in ("JARVIS_ALLOWED_TOOLS", "JARVIS_CHANNEL_SENDER"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import (ai_client, ai_config, ai_providers, conversations,  # noqa: E402
                    key_health, logs, tools as system_tools)
from jarvis.actions import recent_dms, send_dm  # noqa: E402
from jarvis.channels import base, people  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


GUEST = json.dumps({"platform": "discord", "user_id": "42", "user_handle": "g", "is_owner": False})
OWNER = json.dumps({"platform": "discord", "user_id": "1", "user_handle": "o", "is_owner": True})


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
def _isolated_ask_env():
    orig = {
        "conv_dir": conversations.JARVIS_DIR, "conv_conv_dir": conversations.CONV_DIR,
        "conv_index": conversations.INDEX_FILE, "conv_current": conversations.CURRENT_FILE,
        "logs_dir": logs.JARVIS_DIR, "logs_log_dir": logs.LOG_DIR,
        "spawn": ai_client._spawn_title_update, "health_file": key_health.HEALTH_FILE,
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


# ---------------------------------------------------------------------------
# Groq tool_use_failed
# ---------------------------------------------------------------------------

TOOL_USE_FAILED_OTHER = ('HTTP 400: {"error":{"message":"Failed to call a function. Please adjust '
                         'your prompt. See \'failed_generation\' for more details.",'
                         '"type":"invalid_request_error","code":"tool_use_failed"}}')


def test_tool_use_failed_is_request_shaped():
    check("Groq tool_use_failed (generic wording) is request-shaped",
          ai_providers.is_request_shape_error(TOOL_USE_FAILED_OTHER))
    check("a 429 still rotates keys", not ai_providers.is_request_shape_error("rate limited or quota exceeded (HTTP 429)"))
    check("a bad key (Gemini 400) still rotates",
          not ai_providers.is_request_shape_error("HTTP 400: API key not valid. Please pass a valid API key."))
    check("an unrecognized 400 still rotates", not ai_providers.is_request_shape_error("HTTP 400: invalid model name"))


def test_tool_use_failed_skips_the_providers_other_keys():
    calls = []

    def adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        calls.append((resolved.get("name"), resolved.get("api_key")))
        if resolved.get("name") == "first":
            return ai_providers.AIResult(False, error=TOOL_USE_FAILED_OTHER)
        return ai_providers.AIResult(True, text="answered by second")

    plist = [{"name": "first", "type": "fake", "enabled": True,
              "api_keys": ["k1", "k2", "k3"], "model": "m"},
             {"name": "second", "type": "fake", "enabled": True, "api_keys": ["z"], "model": "m"}]
    with _isolated_ask_env() as conv, _providers(plist, adapter, tools_enabled=False):
        r = ai_client.ask("hello", commands=[], conversation_id=conv)
    check("one key tried on the failing provider, not three",
          len([c for c in calls if c[0] == "first"]) == 1, calls)
    check("the next provider answered", r.ok and r.text == "answered by second", (r.ok, r.text))


# ---------------------------------------------------------------------------
# Guests with tools: owner-only tools are not offered
# ---------------------------------------------------------------------------

def test_guest_is_not_offered_owner_only_tools():
    names = ["send_dm", "recent_dms", "notify_owner"]
    with _env(JARVIS_CHANNEL_SENDER=GUEST):
        got = [s["name"] for s in system_tools.schemas_for_tools(names)]
    check("guest: only notify_owner survives", got == ["notify_owner"], got)
    with _env(JARVIS_CHANNEL_SENDER=OWNER):
        got = [s["name"] for s in system_tools.schemas_for_tools(names)]
    check("owner: all three offered", got == names, got)
    with _env(JARVIS_CHANNEL_SENDER=None):
        got = [s["name"] for s in system_tools.schemas_for_tools(names)]
    check("no chat context (the PC): all three offered", got == names, got)
    with _env(JARVIS_CHANNEL_SENDER="not json"):
        got = [s["name"] for s in system_tools.schemas_for_tools(names)]
    check("an unparseable sender is not treated as a guest", got == names, got)


def test_guest_discovery_does_not_list_or_describe_owner_only_tools():
    with _env(JARVIS_CHANNEL_SENDER=GUEST):
        found = [m["name"] for m in system_tools.tool_search_tools({"query": "dm"}).get("matches", [])]
        schema = system_tools.tool_get_tool_schema({"name": "send_dm"})
    check("search_tools('dm') hides send_dm and recent_dms from a guest",
          "send_dm" not in found and "recent_dms" not in found, found)
    check("get_tool_schema('send_dm') says owner-only, hands back no schema",
          "owner-only" in (schema.get("error") or "") and "schema" not in schema, schema)
    with _env(JARVIS_CHANNEL_SENDER=OWNER):
        found = [m["name"] for m in system_tools.tool_search_tools({"query": "dm"}).get("matches", [])]
    check("the owner still finds send_dm", "send_dm" in found, found)


def test_the_handlers_still_refuse_a_non_owner():
    # The real gate. Hiding the schemas is only a token saving on top of it.
    with _env(JARVIS_CHANNEL_SENDER=GUEST):
        r1 = send_dm.tool_send_dm({"person": "x", "message": "hi"})
        r2 = recent_dms.tool_recent_dms({})
        r3 = system_tools.execute_tool("send_dm", {"person": "x", "message": "hi"})
    check("send_dm refuses a guest", r1.get("ok") is False and "owner" in json.dumps(r1).lower(), r1)
    check("recent_dms refuses a guest", r2.get("ok") is False and "owner" in json.dumps(r2).lower(), r2)
    check("execute_tool still routes to the handler (refusal, not 'unknown tool')",
          "no such tool" not in json.dumps(r3).lower(), r3)


def test_owner_only_set_is_exactly_what_the_handlers_gate():
    check("OWNER_ONLY_TOOLS names real tools",
          all(n in system_tools._tool_index() for n in system_tools.OWNER_ONLY_TOOLS))
    check("it is send_dm + recent_dms", system_tools.OWNER_ONLY_TOOLS == {"send_dm", "recent_dms"})


def test_ask_level_guest_offer_excludes_owner_only_tools():
    seen = []

    def adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        seen.append({t.get("name") for t in (tools or [])})
        return ai_providers.AIResult(True, text="ok")

    one = [{"name": "p", "type": "fake", "enabled": True, "api_keys": ["k"], "model": "m"}]
    with _isolated_ask_env() as conv, _providers(one, adapter), \
            _env(JARVIS_CHANNEL_SENDER=GUEST, JARVIS_ALLOWED_TOOLS=None):
        r = ai_client.ask("tell ur owner hi", commands=[], conversation_id=conv)
    check("the ask answered", r.ok is True)
    check("notify_owner IS offered to the guest", seen and "notify_owner" in seen[0], seen)
    check("send_dm / recent_dms are NOT",
          seen and not ({"send_dm", "recent_dms"} & seen[0]), seen)


# ---------------------------------------------------------------------------
# send_dm: close spellings are suggestions, never a pick
# ---------------------------------------------------------------------------

def _fake_people(records):
    orig = people.all_people
    people.all_people = lambda platform: [r for r in records if r.get("platform") == platform]
    return orig


MARYEM = {"platform": "discord", "user_id": "111", "handle": "maryem.not.maryam11_16812", "name": ""}
OTHERS = [
    {"platform": "discord", "user_id": "222", "handle": "zed", "name": "Zed"},
    {"platform": "discord", "user_id": "333", "handle": "bob", "name": "Bob Marley"},
    {"platform": "discord", "user_id": "444", "handle": "alice", "name": "Alice"},
]


def test_near_spelling_is_suggested_not_picked():
    orig = _fake_people(OTHERS + [MARYEM])
    try:
        rec, err = send_dm._resolve("mariem", "discord")
    finally:
        people.all_people = orig
    check("a near spelling is NOT resolved to a recipient", rec is None, rec)
    check("it is an error", err and err.get("ok") is False, err)
    dym = (err or {}).get("did_you_mean") or []
    check("did_you_mean offers the close contact", [d["user_id"] for d in dym] == ["111"], dym)
    check("the hint says confirm, never pick", "confirm" in err["hint"].lower() and "never pick" in err["hint"].lower())
    check("known_contacts lists the nearest first", err["known_contacts"][0]["user_id"] == "111",
          err["known_contacts"])


def test_exact_and_unrelated_names_behave_as_before():
    orig = _fake_people(OTHERS + [MARYEM])
    try:
        rec, err = send_dm._resolve("maryem.not.maryam11_16812", "discord")
        check("the full handle still resolves", rec and rec["user_id"] == "111" and err is None, (rec, err))
        rec, err = send_dm._resolve("@Maryem.Not.Maryam11_16812", "discord")
        check("case and a leading @ are still ignored", rec and rec["user_id"] == "111", (rec, err))
        # A bare WORD of a handle was never a match (only a word of a saved
        # NAME is, tier 2) and still is not: loosening that would be guessing.
        # It now gets the handle back as a suggestion in one call instead.
        rec, err = send_dm._resolve("maryem", "discord")
        check("a word inside a handle is still NOT a match", rec is None, rec)
        check("but it is suggested, so no recent_dms round is needed",
              [d["user_id"] for d in err.get("did_you_mean", [])] == ["111"], err)
        rec, err = send_dm._resolve("qqqqq", "discord")
        check("an unrelated name: no suggestions", rec is None and "did_you_mean" not in err, err)
        check("  and the original hint is kept", "only knows people who have messaged" in err["hint"], err["hint"])
        rec, err = send_dm._resolve("ma", "discord")
        check("a 2-letter name never produces suggestions", rec is None and "did_you_mean" not in err, err)
    finally:
        people.all_people = orig


def test_two_matches_is_still_an_error_listing_both():
    twins = [{"platform": "discord", "user_id": "1", "handle": "sam1", "name": "Sam Lee"},
             {"platform": "discord", "user_id": "2", "handle": "sam2", "name": "Sam Ray"}]
    orig = _fake_people(twins)
    try:
        rec, err = send_dm._resolve("sam", "discord")
    finally:
        people.all_people = orig
    check("two matches: never a pick", rec is None and "more than one person" in err["error"], err)
    check("both are listed", len(err.get("candidates", [])) == 2, err)


def test_the_channels_instruction_tells_the_model_to_call_send_dm_first():
    from jarvis.actions import channel_people
    text = channel_people.TOOL_PACK_INSTRUCTION
    check("says to call send_dm straight away", "send_dm straight away" in text)
    check("says not to call recent_dms just to find a handle", "don't call recent_dms first" in text)
    check("still says never guess a recipient", "never guess" in text)


# ---------------------------------------------------------------------------
# channels/base.py watchdog
# ---------------------------------------------------------------------------

@contextmanager
def _watch_env(stall=0.2, lock_wait=0.1):
    notes, rows = [], []
    orig = (base.STALL_LOG_SECONDS, base.LOCK_WAIT_LOG_SECONDS, base._log, logs.log, ai_client.ask)
    base.STALL_LOG_SECONDS, base.LOCK_WAIT_LOG_SECONDS = stall, lock_wait
    base._log = lambda m: notes.append(m)
    logs.log = lambda conv, direction, data, provider=None, round_num=None: rows.append(
        (conv, direction, data, provider))
    try:
        yield notes, rows
    finally:
        base.STALL_LOG_SECONDS, base.LOCK_WAIT_LOG_SECONDS, base._log, logs.log, ai_client.ask = orig


def test_a_stalled_ask_leaves_a_watchdog_row_naming_what_it_was_doing():
    class R:
        ok, text = True, "fine"

    def slow_ask(text, commands=None, conversation_id=None, on_attempt=None,
                 on_tool_call=None, sender_context="", **kw):
        on_attempt("groq (key 1/2)")
        on_tool_call("recent_dms")
        time.sleep(0.75)
        return R()

    with _watch_env() as (notes, rows):
        ai_client.ask = slow_ask
        seen_tool = []
        result = base._ask_jarvis("hi", "abc1234567", True, on_tool_call=lambda *a: seen_tool.append(a))
        stalled = [m for m in notes if "still running" in m]
        count_at_end = len(stalled)
        time.sleep(0.5)   # the watchdog must stop once the ask is over
        after = len([m for m in notes if "still running" in m])
    check("the ask's result is returned unchanged", result.ok and result.text == "fine")
    check("at least two stall notes in 0.75s at a 0.2s interval", count_at_end >= 2, notes)
    check("the note names the last provider attempt", "groq (key 1/2)" in stalled[0], stalled[0])
    check("the note names the last tool", "recent_dms" in stalled[0], stalled[0])
    check("the caller's own on_tool_call still fires", seen_tool == [("recent_dms",)], seen_tool)
    check("a row was written to the conversation log under 'watchdog'",
          any(r[1] == "error" and r[3] == "watchdog" and "still running" in r[2]["error"] for r in rows), rows)
    check("the watchdog stops when the ask finishes", after == count_at_end, (count_at_end, after))


def test_a_fast_ask_writes_no_watchdog_rows():
    class R:
        ok, text = True, "fine"

    with _watch_env(stall=5.0, lock_wait=5.0) as (notes, rows):
        ai_client.ask = lambda *a, **k: R()
        base._ask_jarvis("hi", "abc1234567", True)
    check("no notes", notes == [], notes)
    check("no log rows", rows == [], rows)


def test_waiting_behind_another_ask_is_logged():
    class R:
        ok, text = True, "fine"

    with _watch_env(stall=5.0, lock_wait=0.1) as (notes, rows):
        ai_client.ask = lambda *a, **k: R()
        holder_ready = threading.Event()

        def hold():
            with base._ASK_LOCK:
                holder_ready.set()
                time.sleep(0.4)

        t = threading.Thread(target=hold)
        t.start()
        holder_ready.wait(2)
        base._ask_jarvis("hi", "abc1234567", True)
        t.join()
    check("the wait on the ask lock is reported",
          any("waited" in m and "ask lock" in m for m in notes), notes)


def test_the_real_ask_accepts_on_attempt():
    one = [{"name": "p", "type": "fake", "enabled": True, "api_keys": ["k"], "model": "m"}]

    def adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
        return ai_providers.AIResult(True, text="hello back")

    with _isolated_ask_env() as conv, _providers(one, adapter, tools_enabled=False):
        r = base._ask_jarvis("hello", conv, False)
    check("a real ask() through _ask_jarvis answers", r.ok and r.text == "hello back", (r.ok, r.text))


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
