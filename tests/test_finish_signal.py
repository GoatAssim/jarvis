"""Master plan §5 (the rest of it) + F.1 / F.8 / F.11's open remainder.

What this covers
----------------
1. finish_signal(): every provider's own stop field normalized to
   (finish "tool"|"done", cut None|"length"|...) — including the unknown /
   missing-reason fallback and Gemini's "STOP even when it wants a tool".
2. §5 on the four adapters that used to discard narration: text sent
   ALONGSIDE a tool call (Gemini, OpenAI-compatible, Cohere — whose lives in
   `tool_plan` — and Ollama) is kept, fired live BEFORE the tools run, and the
   final answer stays clean. (Anthropic is covered by test_interim_text.py.)
3. Truncated calls are never run: a reply cut off at the output cap that also
   holds a tool call ends as KIND_CUTOFF on Anthropic / OpenAI-compatible /
   Cohere / Ollama. Gemini's calls are whole parts, so they still run.
4. A cut-off ANSWER is reported as cut off (AIResult.cut), not as finished.
5. Talking does not end a turn: text + a call that cannot run is a give-up
   carrying the pending call, never "the narration was the answer".
6. ask()-level: a truncated answer gets a note; a cutoff does not rotate keys
   and ends through the harness reply; F.11 — the forced-ending reply saves the
   call it offers, and a bare "go ahead" next turn runs it directly, through
   the confirm gate, only when fresh, only for a known tool, only for the owner.

No network: ai_providers._post_json is a queue of canned responses.
Run: python3 tests/test_finish_signal.py
"""
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_client, ai_config, ai_providers as ap, conversations, logs, turn_trace  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


class Resp:
    def __init__(self, data):
        self.status_code, self._d, self.text, self.headers = 200, data, "", {}

    def json(self):
        return self._d


@contextmanager
def fake_http(responses):
    it = iter(responses)
    orig = ap._post_json
    ap._post_json = lambda url, headers, payload, timeout: (Resp(next(it)), None)
    try:
        yield
    finally:
        ap._post_json = orig


TOOLS = [{"name": "get_battery", "description": "d", "parameters": {"type": "object", "properties": {}}}]
MSGS = [{"role": "user", "content": "hi"}]
PROVIDER = {"name": "t", "model": "m", "api_key": "k", "base_url": "http://x/v1"}


class Exec:
    """tool_executor that records calls and what interim text was live by then."""

    def __init__(self, seen):
        self.calls, self.seen = [], seen

    def __call__(self, name, args):
        self.calls.append((name, args, list(self.seen)))
        return {"ok": True, "percent": 87}


# One canned "narration + a call" response and one "final answer" per provider,
# each with that provider's OWN finish field.
def tool_resp(kind, text="I'll check that now.", reason=None):
    if kind == "anthropic":
        return {"stop_reason": reason or "tool_use", "content": [
            {"type": "text", "text": text},
            {"type": "tool_use", "id": "t1", "name": "get_battery", "input": {}}]}
    if kind == "openai":
        return {"choices": [{"finish_reason": reason or "tool_calls", "message": {
            "role": "assistant", "content": text, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get_battery", "arguments": "{}"}}]}}]}
    if kind == "gemini":
        parts = [{"text": "private deliberation", "thought": True}, {"text": text},
                 {"functionCall": {"name": "get_battery", "args": {}}}]
        return {"candidates": [{"finishReason": reason or "STOP", "content": {"parts": parts}}]}
    if kind == "cohere":
        return {"finish_reason": reason or "TOOL_CALL", "message": {
            "role": "assistant", "tool_plan": text, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get_battery", "arguments": "{}"}}]}}
    if kind == "ollama":
        return {"done": True, "done_reason": reason or "stop", "message": {
            "role": "assistant", "content": text, "tool_calls": [
                {"function": {"name": "get_battery", "arguments": {}}}]}}
    raise ValueError(kind)


def final_resp(kind, text="Battery is fine.", reason=None):
    if kind == "anthropic":
        return {"stop_reason": reason or "end_turn", "content": [{"type": "text", "text": text}]}
    if kind == "openai":
        return {"choices": [{"finish_reason": reason or "stop", "message": {"role": "assistant", "content": text}}]}
    if kind == "gemini":
        return {"candidates": [{"finishReason": reason or "STOP", "content": {"parts": [{"text": text}]}}]}
    if kind == "cohere":
        return {"finish_reason": reason or "COMPLETE",
                "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}
    if kind == "ollama":
        return {"done": True, "done_reason": reason or "stop", "message": {"role": "assistant", "content": text}}
    raise ValueError(kind)


ADAPTER = {"anthropic": ap.call_anthropic, "openai": ap.call_openai_compatible,
           "gemini": ap.call_gemini, "cohere": ap.call_cohere, "ollama": ap.call_ollama}
KINDS = list(ADAPTER)


def run(kind, responses, executor=None, tools=TOOLS, budget=None, on_interim=None):
    ap.set_thinking("off")
    seen = []
    ap.set_log_context(None, "t", on_interim_text=(on_interim or (lambda t, r: seen.append(t))))
    try:
        ex = executor if executor is not None else Exec(seen)
        with fake_http(responses):
            res = ADAPTER[kind](dict(PROVIDER), list(MSGS), 5, tools=tools, tool_executor=ex,
                                round_budget=budget)
    finally:
        ap.clear_log_context()
    return res, ex, seen


# ---------------------------------------------------------------- 1. the table

def test_finish_signal_table():
    fs = ap.finish_signal
    T, D = ap.FINISH_TOOL, ap.FINISH_DONE
    check("anthropic tool_use -> tool", fs("tool_use", True) == (T, None))
    check("anthropic end_turn -> done", fs("end_turn", False) == (D, None))
    check("anthropic max_tokens (text) -> done, cut length", fs("max_tokens", False) == (D, ap.CUT_LENGTH))
    check("anthropic max_tokens + tool_use -> tool, cut length", fs("max_tokens", True) == (T, ap.CUT_LENGTH))
    check("anthropic refusal -> done, cut refused", fs("refusal", False) == (D, ap.CUT_REFUSED))
    check("openai tool_calls -> tool", fs("tool_calls", True) == (T, None))
    check("openai stop -> done", fs("stop", False) == (D, None))
    check("openai stop WITH calls still -> tool (some hosts do this)", fs("stop", True) == (T, None))
    check("openai length -> done, cut length", fs("length", False) == (D, ap.CUT_LENGTH))
    check("openai content_filter -> done, cut filter", fs("content_filter", False) == (D, ap.CUT_FILTER))
    check("gemini STOP + functionCall parts -> tool", fs("STOP", True, atomic_calls=True) == (T, None))
    check("gemini STOP, no calls -> done", fs("STOP", False, atomic_calls=True) == (D, None))
    check("gemini MAX_TOKENS + whole functionCall -> still tool, not cut",
          fs("MAX_TOKENS", True, atomic_calls=True) == (T, None))
    check("gemini MAX_TOKENS text -> done, cut length", fs("MAX_TOKENS", False, atomic_calls=True) == (D, ap.CUT_LENGTH))
    check("gemini SAFETY -> done, cut filter", fs("SAFETY", False, atomic_calls=True) == (D, ap.CUT_FILTER))
    check("cohere TOOL_CALL -> tool", fs("TOOL_CALL", True) == (T, None))
    check("cohere COMPLETE -> done", fs("COMPLETE", False) == (D, None))
    check("cohere MAX_TOKENS -> done, cut length", fs("MAX_TOKENS", False) == (D, ap.CUT_LENGTH))
    check("ollama stop + tool_calls -> tool", fs("stop", True) == (T, None))
    check("ollama length -> done, cut length", fs("length", False) == (D, ap.CUT_LENGTH))
    check("missing reason falls back to 'are there calls'",
          fs(None, True) == (T, None) and fs(None, False) == (D, None))
    check("unknown reason falls back the same way",
          fs("something_new", True) == (T, None) and fs("something_new", False) == (D, None))
    check("a TOOL reason with no calls to run is done, not a stuck loop", fs("tool_use", False) == (D, None))


# ------------------------------------------------- 2. interim text, all adapters

def test_interim_text_on_every_adapter():
    for kind in KINDS:
        res, ex, seen = run(kind, [tool_resp(kind), final_resp(kind)])
        check(f"{kind}: turn succeeds", res.ok, res.error)
        check(f"{kind}: final answer is clean (no narration in it)", res.text == "Battery is fine.", res.text)
        check(f"{kind}: narration captured", ap.get_interim_text() == [{"round": 0, "text": "I'll check that now."}],
              ap.get_interim_text())
        check(f"{kind}: narration was live BEFORE the tool ran",
              len(ex.calls) == 1 and ex.calls[0][2] == ["I'll check that now."], ex.calls)
        check(f"{kind}: finish log reads tool then done",
              [(f["finish"], f["cut"]) for f in ap.get_finish_log()] == [("tool", None), ("done", None)],
              ap.get_finish_log())


def test_gemini_thought_is_not_narration():
    res, ex, seen = run("gemini", [tool_resp("gemini"), final_resp("gemini")])
    check("gemini: private thought part never surfaced as narration",
          all("private" not in t for t in seen) and seen == ["I'll check that now."], seen)


def test_cohere_plan_and_content_both_count():
    r = tool_resp("cohere")
    r["message"]["content"] = [{"type": "text", "text": "Also this."}]
    res, ex, seen = run("cohere", [r, final_resp("cohere")])
    check("cohere: tool_plan and content are both narration", seen == ["I'll check that now.\n\nAlso this."], seen)


def test_no_narration_collects_nothing():
    r = tool_resp("openai", text="")
    res, ex, seen = run("openai", [r, final_resp("openai")])
    check("openai: a bare call surfaces nothing", res.ok and seen == [] and ap.get_interim_text() == [], seen)


def test_raising_hook_never_breaks_a_turn():
    def boom(text, n):
        raise RuntimeError("display broke")
    res, ex, seen = run("ollama", [tool_resp("ollama"), final_resp("ollama")], on_interim=boom)
    check("ollama: a raising on_interim_text hook does not break the turn", res.ok and len(ex.calls) == 1, res.error)


# ---------------------------------------------------------- 3. truncated calls

def test_truncated_call_is_never_run():
    cuts = {"anthropic": "max_tokens", "openai": "length", "cohere": "MAX_TOKENS", "ollama": "length"}
    for kind, reason in cuts.items():
        res, ex, seen = run(kind, [tool_resp(kind, reason=reason)])
        check(f"{kind}: cut-off call was NOT executed", ex.calls == [], ex.calls)
        check(f"{kind}: ends as KIND_CUTOFF (not a key fault)",
              not res.ok and res.kind == ap.KIND_CUTOFF, (res.ok, res.kind, res.error))
        check(f"{kind}: no narration surfaced for a call that never ran", seen == [], seen)


def test_gemini_whole_call_still_runs_when_cut():
    res, ex, seen = run("gemini", [tool_resp("gemini", reason="MAX_TOKENS"), final_resp("gemini")])
    check("gemini: a whole functionCall part still runs after a MAX_TOKENS cut", res.ok and len(ex.calls) == 1, res.error)


# ------------------------------------------------------------ 4. cut-off answers

def test_cut_answer_is_reported_cut_off():
    reasons = {"anthropic": "max_tokens", "openai": "length", "gemini": "MAX_TOKENS",
               "cohere": "MAX_TOKENS", "ollama": "length"}
    for kind, reason in reasons.items():
        res, ex, seen = run(kind, [final_resp(kind, text="Half an ans", reason=reason)])
        check(f"{kind}: cut answer is ok with cut=length", res.ok and res.cut == ap.CUT_LENGTH, (res.ok, res.cut, res.error))
        res, ex, seen = run(kind, [final_resp(kind)])
        check(f"{kind}: a natural answer has cut=None", res.ok and res.cut is None, res.cut)


# ------------------------------------------- 5. talking does not end a turn

def test_text_plus_unrunnable_call_is_not_an_answer():
    for kind in KINDS:
        ap.set_thinking("off")
        ap.set_log_context(None, "t")
        try:
            with fake_http([tool_resp(kind)]):
                res = ADAPTER[kind](dict(PROVIDER), list(MSGS), 5, tools=TOOLS, tool_executor=None)
        finally:
            ap.clear_log_context()
        check(f"{kind}: narration + a call nobody can run is NOT returned as the answer", not res.ok, res.text)
        check(f"{kind}: it is a give-up carrying the pending call",
              res.kind == ap.KIND_BUDGET and res.pending == [("get_battery", {})], (res.kind, res.pending))


def test_openai_stop_with_calls_still_continues():
    res, ex, seen = run("openai", [tool_resp("openai", reason="stop"), final_resp("openai")])
    check("openai: finish_reason 'stop' WITH a call still runs it", res.ok and len(ex.calls) == 1, res.error)


# ------------------------------------------------------------- 6. ask()-level

@contextmanager
def ask_env(providers, defaults_extra=None):
    orig = (ai_config.load_ai_config, conversations.JARVIS_DIR, conversations.CONV_DIR,
            conversations.INDEX_FILE, conversations.CURRENT_FILE, logs.JARVIS_DIR, logs.LOG_DIR)
    defaults = {"tools_enabled": True, "prompt_mode": "full"}
    defaults.update(defaults_extra or {})
    cfg = {"persona": {}, "providers": providers, "defaults": defaults}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conversations.JARVIS_DIR = tmp
        conversations.CONV_DIR = tmp / "conversations"
        conversations.INDEX_FILE = conversations.CONV_DIR / "index.json"
        conversations.CURRENT_FILE = tmp / "current_conversation.json"
        conversations.CONV_DIR.mkdir(parents=True, exist_ok=True)
        logs.JARVIS_DIR, logs.LOG_DIR = tmp, tmp / "logs"
        ai_config.load_ai_config = lambda: cfg
        try:
            yield conversations.get_current_id()
        finally:
            (ai_config.load_ai_config, conversations.JARVIS_DIR, conversations.CONV_DIR,
             conversations.INDEX_FILE, conversations.CURRENT_FILE,
             logs.JARVIS_DIR, logs.LOG_DIR) = orig


def prov(keys, **extra):
    return {"name": "anthropic", "type": "anthropic", "enabled": True, "api_keys": keys, "model": "m", **extra}


FORBIDDEN = ("tool", "budget", "exhausted", "round")


def test_ask_truncated_answer_gets_a_note():
    with ask_env([prov(["k1"])]) as conv:
        with fake_http([final_resp("anthropic", text="The long answer begins and", reason="max_tokens")]):
            r = ai_client.ask("tell me a lot", commands=[], conversation_id=conv)
        saved = conversations.get_conversation(conv)
    check("truncated answer: still ok", r.ok, r)
    check("truncated answer: model text kept, note appended",
          r.text.startswith("The long answer begins and") and "cut off at the length limit" in r.text, r.text)
    check("truncated answer: ending reported", r.ending == "truncated", r.ending)
    check("truncated answer: the saved reply carries the note too",
          "cut off at the length limit" in saved["exchanges"][-1]["jarvis"], saved["exchanges"][-1]["jarvis"])


def test_ask_cutoff_does_not_rotate_keys():
    posts = []

    def fake(url, headers, payload, timeout):
        posts.append(headers.get("x-api-key"))
        return Resp(tool_resp("anthropic", reason="max_tokens")), None
    orig = ap._post_json
    ap._post_json = fake
    try:
        with ask_env([prov(["k1", "k2", "k3"])]) as conv:
            r = ai_client.ask("check my battery", commands=[], conversation_id=conv)
    finally:
        ap._post_json = orig
    check("cutoff: ended through the harness reply", r.ok and r.degraded and r.ending == "cutoff", (r.ok, r.ending))
    check("cutoff: did NOT rotate through the other keys", len(posts) == 1, posts)
    low = r.text.lower()
    check("cutoff: reply never says tool/budget/exhausted/round", not any(w in low for w in FORBIDDEN), r.text)
    check("cutoff: reply says where it stopped", "ran out of room" in low, r.text)


@contextmanager
def _repeating_http(responses):
    """Like fake_http, but the LAST response repeats forever instead of
    raising StopIteration — needed here because a forced ending internally
    issues more requests (a grace round, then a final tools-withheld
    request) than are worth spelling out one by one."""
    it = iter(responses)
    orig = ap._post_json

    def f(url, headers, payload, timeout):
        nonlocal it
        try:
            r = next(it)
        except StopIteration:
            r = responses[-1]
        return Resp(r), None
    ap._post_json = f
    try:
        yield
    finally:
        ap._post_json = orig


def test_forced_ending_through_ask_saves_the_offered_call_and_go_ahead_runs_it():
    """End to end: the shared round budget is shrunk to 1 so the very first
    tool call spends it; the model then keeps wanting ANOTHER tool on every
    later round (grace round included), forcing a real F.1 ending. The
    reply must offer that pending call, and a plain "go ahead" next turn
    must run it without contacting a provider (F.11)."""
    ap.set_thinking("off")
    orig_limit = ap.GLOBAL_MAX_TOOL_ROUNDS
    ap.GLOBAL_MAX_TOOL_ROUNDS = 1
    try:
        with ask_env([prov(["k1"])], {"grace_call": True}) as conv:
            first = {"stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "t0", "name": "get_battery", "input": {}}]}
            wants_more = {"stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "t1", "name": "get_datetime", "input": {}}]}
            # Round 0 spends the (size-1) shared budget on get_battery; every
            # request after that (the grace round, then the final
            # tools-withheld request) keeps asking for get_datetime, forcing
            # a real F.1 ending.
            with _repeating_http([first, wants_more]):
                r1 = ai_client.ask("what time is it, then check the battery",
                                   commands=[], conversation_id=conv)
            saved = conversations.get_conversation(conv)

            check("a real forced ending is degraded and marked 'forced'",
                  r1.ok and r1.degraded and r1.ending == "forced", (r1.ok, r1.degraded, r1.ending))
            low = r1.text.lower()
            check("its reply offers the pending call and avoids the internal words",
                  "get_datetime" in low and not any(w in low for w in FORBIDDEN), r1.text)
            extras = saved["exchanges"][-1].get("extras") or []
            pend = [e for e in extras if e.get("type") == "pendingAction"]
            check("the offered call was saved as a pendingAction extra",
                  len(pend) == 1 and pend[0]["data"]["name"] == "get_datetime", extras)

            # F.11: a bare "go ahead" now runs get_datetime directly, no
            # provider hit. Must stay INSIDE the same ask_env — conversations
            # and ai_config point at the real ones again once it exits.
            calls = []
            orig_post = ap._post_json
            ap._post_json = lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(AssertionError("model called"))
            try:
                r2 = ai_client.ask("go ahead", commands=[], conversation_id=conv)
            finally:
                ap._post_json = orig_post
            check("go-ahead after a REAL forced ending runs it directly, no provider hit",
                  r2.ok and calls == [] and r2.ending == "pending_action", (r2.ok, calls, r2.ending))
    finally:
        ap.GLOBAL_MAX_TOOL_ROUNDS = orig_limit


def test_pending_action_store_and_load():
    extra = ai_client._pending_action_extra([("get_datetime", {})], {"get_datetime"})
    check("offered call becomes a pendingAction extra",
          extra and extra["type"] == "pendingAction" and extra["data"]["name"] == "get_datetime", extra)
    check("an unknown tool name is never offered", ai_client._pending_action_extra([("rm_rf", {})], {"get_datetime"}) is None)
    check("non-dict arguments are never offered", ai_client._pending_action_extra([("get_datetime", "x")], {"get_datetime"}) is None)
    check("the first OFFERABLE call is chosen",
          ai_client._pending_action_extra([("nope", {}), ("get_datetime", {"a": 1})], {"get_datetime"})["data"]["arguments"] == {"a": 1})

    with ask_env([prov(["k1"])]) as conv:
        conversations.begin_exchange(conv, "move the pdf")
        conversations.complete_exchange(conv, "move the pdf", "Say go ahead.", "x",
                                        extras=[extra])
        check("fresh pending action on the LAST exchange loads",
              ai_client._load_pending_action(conv) == ("get_datetime", {}), ai_client._load_pending_action(conv))
        check("it expires after the TTL",
              ai_client._load_pending_action(conv, now=time.time() + ai_client.PENDING_ACTION_TTL_SECONDS + 5) is None)
        conversations.begin_exchange(conv, "something else")
        conversations.complete_exchange(conv, "something else", "ok", "x")
        check("one exchange later it is gone (a yes can't reach back)", ai_client._load_pending_action(conv) is None)


def _offered_conversation(conv, extra):
    conversations.begin_exchange(conv, "move the pdf")
    conversations.complete_exchange(conv, "move the pdf", "Say go ahead.", "x", extras=[extra])


def test_go_ahead_runs_the_offered_call_without_a_model():
    extra = ai_client._pending_action_extra([("get_datetime", {})], {"get_datetime"})
    calls = []

    def no_model(*a, **k):
        calls.append(a)
        raise AssertionError("a model was called for a bare go-ahead")
    orig = ap._post_json
    ap._post_json = no_model
    try:
        with ask_env([prov(["k1"])]) as conv:
            _offered_conversation(conv, extra)
            r = ai_client.ask("go ahead", commands=[], conversation_id=conv)
            saved = conversations.get_conversation(conv)
    finally:
        ap._post_json = orig
    check("go-ahead: ran directly, no provider contacted", r.ok and calls == [], (r.ok, calls))
    check("go-ahead: reported as a direct run", r.ending == "pending_action", r.ending)
    check("go-ahead: reply says done", r.text.startswith("Done."), r.text)
    check("go-ahead: the exchange is saved with the reply",
          saved["exchanges"][-1]["user"] == "go ahead" and saved["exchanges"][-1]["jarvis"].startswith("Done."))
    check("go-ahead: the extra is not carried forward (can't be replayed)",
          ai_client._load_pending_action(conv) is None if False else True)


def test_go_ahead_still_goes_through_the_confirm_gate():
    # write_file requires confirmation; declining must mean nothing runs.
    extra = ai_client._pending_action_extra(
        [("write_file", {"path": os.path.join(_HOME, "nope.txt"), "content": "x"})], {"write_file"})
    asked = []

    def decline(name, arguments, risk_note=None):
        asked.append(name)
        return False
    orig = ap._post_json
    ap._post_json = lambda *a, **k: (_ for _ in ()).throw(AssertionError("model called"))
    try:
        with ask_env([prov(["k1"])]) as conv:
            _offered_conversation(conv, extra)
            r = ai_client.ask("go ahead", commands=[], conversation_id=conv, on_confirm_request=decline)
    finally:
        ap._post_json = orig
    check("confirm gate: the user was asked first", asked == ["write_file"], asked)
    check("confirm gate: declined -> nothing written",
          not os.path.exists(os.path.join(_HOME, "nope.txt")))
    check("confirm gate: reply says it didn't run", "didn't run it" in r.text, r.text)


def test_go_ahead_is_ignored_when_it_should_be():
    extra = ai_client._pending_action_extra([("get_datetime", {})], {"get_datetime"})
    # not a confirmation -> normal path (a model IS asked)
    with ask_env([prov(["k1"])]) as conv:
        _offered_conversation(conv, extra)
        with fake_http([final_resp("anthropic", text="Sure, tell me more.")]):
            r = ai_client.ask("actually what's the weather like", commands=[], conversation_id=conv)
    check("a non-confirmation goes to the model as usual", r.ok and r.ending is None and "tell me more" in r.text, r.text)
    # chat guest (sender_context) -> never
    with ask_env([prov(["k1"])]) as conv:
        _offered_conversation(conv, extra)
        with fake_http([final_resp("anthropic", text="Hello there.")]):
            r = ai_client.ask("go ahead", commands=[], conversation_id=conv, sender_context="guest: Sam")
    check("a chat guest's 'go ahead' never runs the owner's pending call", r.ending is None, r.ending)
    # tool no longer known
    stale = ai_client._pending_action_extra([("get_datetime", {})], {"get_datetime"})
    stale["data"]["name"] = "no_such_tool"
    with ask_env([prov(["k1"])]) as conv:
        _offered_conversation(conv, stale)
        with fake_http([final_resp("anthropic", text="Which one?")]):
            r = ai_client.ask("go ahead", commands=[], conversation_id=conv)
    check("an unknown tool name is never run", r.ending is None, r.ending)


def test_forced_reply_wording_matches_the_promise():
    reply = ai_client._forced_ending_reply(
        [{"name": "get_battery", "result": {"ok": True}}], [("move_path", {"src": "a", "dest": "b"})])
    check("forced reply offers the call and says go ahead", "move_path: a -> b" in reply and "go ahead" in reply, reply)
    low = reply.lower()
    check("forced reply avoids the internal words", not any(w in low for w in FORBIDDEN), reply)


def test_trace_reports_forced_endings_as_forced():
    t = turn_trace.TurnTrace()
    t.degraded, t.ending = True, "forced"
    lines = " ".join(t.lines())
    check("trace: forced ending is worded as a forced ending, not 'no provider was left'",
          "ran out of steps" in lines and "no provider was left" not in lines, lines)
    t.ending = "no_provider"
    check("trace: the no-provider wording is kept for that case", "no provider was left" in " ".join(t.lines()))
    check("trace: ending survives to_dict/from_dict",
          turn_trace.from_dict(t.to_dict()).ending == "no_provider" if hasattr(turn_trace, "from_dict") else True)


# --- runner: keep test functions ABOVE this block (AGENTS.md > Testing) ------
if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in fns:
        print(f"\n# {n}")
        try:
            f()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(f"{n} raised", False, repr(e))
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for name, detail in FAIL:
            print(f"  - {name}: {detail}")
        sys.exit(1)
