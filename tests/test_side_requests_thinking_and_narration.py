"""A.1d / Gemini thinking / AI-overview isolation.

1. The AI overview (ai_client.risk_review) makes a request to a SECOND provider
   in the middle of the main turn. It used to share the main attempt's thinking
   state and stream sink, so thinking on made the second provider think, glued
   its reasoning onto the main trace and (streaming) put it in the main bubble.
   ai_providers.isolated_call() fences that off.
2. Gemini 3+ takes `thinkingLevel`, not `thinkingBudget`; a provider block's
   extra_params must not wipe `includeThoughts`; a thought-less reply after
   thinking tokens were spent leaves a diagnostic line.
3. A local model that announces a tool call instead of making it (A.1d) is
   nudged, twice at most.

No network. Run: python3 tests/test_side_requests_thinking_and_narration.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_client, ai_providers as ap, narration_guard, reasoning  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


class Resp:
    status_code = 200
    text = ""
    headers = {}

    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d

    def close(self):
        pass


# ---------------------------------------------------------------- 1. isolation
def test_isolated_call_restores_everything():
    events = []
    ap.set_log_context(None, "gemini (key 1/2)", stream=True)
    ap.set_stream_sink(lambda kind, **d: events.append((kind, d)))
    ap.set_thinking("medium")
    ap._thinking["trace"] = "main reasoning"
    ap._thinking["rounds"] = 1
    ap._thinking["requested"] = 1
    ap._collect_interim_text(0, "main narration")
    ap._note_finish(0, "tool", None)

    with ap.isolated_call():
        check("inside: streaming is off", ap.stream_enabled() is False)
        check("inside: no stream sink", ap._stream_sink() is None)
        check("inside: thinking is off", ap._thinking["level"] == "off")
        ap._emit_stream(ap.EVENT_THINKING, delta="second provider thinking")
        ap._thinking["trace"] = "second provider reasoning"
        ap._collect_interim_text(0, "second narration")

    check("a delta emitted inside never reached the main sink", events == [], events)
    check("after: the main thinking state is back", ap._thinking == {
        "level": "medium", "trace": "main reasoning", "rounds": 1, "requested": 1,
        "floor_noted": False}, ap._thinking)   # floor_noted: the once-per-ask Groq "Off is the floor" note (10-11t)
    check("after: streaming and the sink are back", ap.stream_enabled() is True and ap._stream_sink() is not None)
    check("after: main interim text is back, the second's is gone",
          [it["text"] for it in ap.get_interim_text()] == ["main narration"], ap.get_interim_text())
    check("after: main finish log is back", len(ap.get_finish_log()) == 1, ap.get_finish_log())
    ap._emit_stream(ap.EVENT_TEXT, delta="main")
    check("after: the main sink works again", events and events[0][0] == ap.EVENT_TEXT, events)
    ap.clear_log_context()


def test_restores_on_exception_and_missing_fields():
    ap.clear_log_context()
    try:
        del ap._log_local.stream_sink
    except AttributeError:
        pass
    ap.set_thinking("high")
    try:
        with ap.isolated_call():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    check("an exception inside still restores thinking", ap._thinking["level"] == "high")
    check("a field that did not exist before does not exist after",
          not hasattr(ap._log_local, "stream_sink"))
    ap.set_thinking("off")


def test_risk_review_is_isolated():
    seen = {}
    events = []

    def fake_adapter(provider, messages, timeout, tools=None, tool_executor=None, **kw):
        seen["thinking_level"] = ap._thinking["level"]
        seen["stream"] = ap.stream_enabled()
        ap._emit_stream(ap.EVENT_TEXT, delta="risk note text")
        ap._emit_stream(ap.EVENT_THINKING, delta="second provider thinking")
        ap._collect_thinking("gemini", {"candidates": [{"content": {"parts": [
            {"thought": True, "text": "SECOND PROVIDER THINKING"}]}}]})
        return ap.AIResult(True, text="Deletes a file. Risk: medium.")

    second = {"name": "second", "type": "fake_side", "model": "m", "api_key": "k"}
    orig_adapters = dict(ap.ADAPTERS)
    orig_elig = ai_client._eligible_providers
    ap.ADAPTERS["fake_side"] = fake_adapter
    ai_client._eligible_providers = lambda providers, defaults: [second]
    try:
        ap.set_log_context(None, "gemini (key 1/2)", stream=True)
        ap.set_stream_sink(lambda kind, **d: events.append(kind))
        ap.set_thinking("medium")
        ap._thinking["trace"] = "MAIN THINKING"
        note = ai_client.risk_review("delete_file", {"path": "x"},
                                      {"providers": [second], "defaults": {}}, exclude_label="gemini")
        trace = ap.get_thinking_trace()
        check("the note still comes back", note and "Risk: medium" in note["note"], note)
        check("the second provider was NOT asked to think", seen.get("thinking_level") == "off", seen)
        check("the second provider did NOT stream", seen.get("stream") is False, seen)
        check("none of its deltas reached the main bubble", events == [], events)
        check("its thinking is not in the main trace", trace["text"] == "MAIN THINKING", trace)
        check("the main level is untouched", trace["level"] == "medium", trace)
    finally:
        ap.ADAPTERS.clear()
        ap.ADAPTERS.update(orig_adapters)
        ai_client._eligible_providers = orig_elig
        ap.clear_log_context()
        ap.set_thinking("off")


# ------------------------------------------------------------ 2. Gemini thinking
def test_gemini_levels_vs_budgets():
    f = reasoning.gemini_uses_levels
    check("gemini-3.6-flash uses levels", f("gemini-3.6-flash"))
    check("gemini-3-flash-preview uses levels", f("gemini-3-flash-preview"))
    check("gemini-3.1-pro uses levels", f("gemini-3.1-pro"))
    check("gemini-2.5-flash keeps budgets", not f("gemini-2.5-flash"))
    check("an unknown or empty model keeps budgets", not f("") and not f(None) and not f("flash"))

    def cfg(model, level, round_num=0, ran_tools=False):
        p = reasoning.round_patch("gemini", level, round_num, is_final=bool(ran_tools and round_num > 0),
                                  ran_tools=ran_tools, model=model)
        return (p.get("_generationConfig") or {}).get("thinkingConfig")

    c = cfg("gemini-3.6-flash", "medium")
    check("3.x medium: thinkingLevel + includeThoughts, no budget",
          c == {"thinkingLevel": "medium", "includeThoughts": True}, c)
    check("3.x low", cfg("gemini-3.6-flash", "low")["thinkingLevel"] == "low")
    check("3.x high", cfg("gemini-3.6-flash", "high")["thinkingLevel"] == "high")
    c = cfg("gemini-2.5-flash", "medium")
    check("2.5 medium: unchanged budget form", c == {"thinkingBudget": 4096, "includeThoughts": True}, c)
    check("off sends nothing on either", cfg("gemini-3.6-flash", "off") is None and cfg("gemini-2.5-flash", "off") is None)
    c = cfg("gemini-3.6-flash", "high", round_num=2, ran_tools=True)
    check("3.x final round steps the level down", c and c["thinkingLevel"] == "medium", c)
    check("request_patch agrees (3.x)", reasoning.request_patch("gemini", "high", model="gemini-3.6-flash")
          ["_generationConfig"]["thinkingConfig"]["thinkingLevel"] == "high")
    check("request_patch agrees (2.5)", reasoning.request_patch("gemini", "high", model="gemini-2.5-pro")
          ["_generationConfig"]["thinkingConfig"]["thinkingBudget"] == 16384)
    check("a 400 naming thinkingLevel is recognised as a thinking rejection",
          reasoning.looks_like_thinking_rejected("Unknown name \"thinkingLevel\": Cannot find field"))


def _run_gemini(model, level, response):
    sent = []
    orig_post, orig_status = ap._post_json, ap._status_reason

    def fake_post(url, headers, payload, timeout):
        sent.append(json.loads(json.dumps(payload)))
        return Resp(response), None

    ap._post_json, ap._status_reason = fake_post, lambda r: None
    try:
        ap.set_log_context("conv-x", "gemini (key 1/1)", base_url="https://generativelanguage.googleapis.com/v1beta/models/m")
        ap.set_thinking(level)
        prov = {"name": "gemini", "type": "gemini", "model": model, "api_key": "k",
                "base_url": f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"}
        extra = response.pop("_extra", None)
        if extra:
            prov["extra_params"] = extra
        res = ap.ADAPTERS["gemini"](prov, [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
                                     30, tools=None, tool_executor=None)
        return res, sent, ap.get_thinking_trace()
    finally:
        ap._post_json, ap._status_reason = orig_post, orig_status
        ap.clear_log_context()
        ap.set_thinking("off")


_THOUGHT_REPLY = {"candidates": [{"content": {"role": "model", "parts": [
    {"thought": True, "text": "weighing it"}, {"text": "answer"}]}, "finishReason": "STOP"}],
    "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2, "thoughtsTokenCount": 40}}


def test_gemini_adapter_requests_and_keeps_thoughts():
    res, sent, trace = _run_gemini("gemini-3.6-flash", "medium", json.loads(json.dumps(_THOUGHT_REPLY)))
    tc = sent[0]["generationConfig"]["thinkingConfig"]
    check("3.6 request carries thinkingLevel and includeThoughts", tc == {"thinkingLevel": "medium", "includeThoughts": True}, tc)
    check("the thought text is collected and not in the answer", trace["text"] == "weighing it" and res.text == "answer", (trace, res.text))
    res, sent, trace = _run_gemini("gemini-2.5-flash", "low", json.loads(json.dumps(_THOUGHT_REPLY)))
    check("2.5 request is unchanged", sent[0]["generationConfig"]["thinkingConfig"]
          == {"thinkingBudget": 1024, "includeThoughts": True}, sent[0]["generationConfig"])


def test_extra_params_do_not_wipe_include_thoughts():
    gen = {"maxOutputTokens": 3072, "thinkingConfig": {"thinkingLevel": "medium", "includeThoughts": True}}
    ap._merge_gemini_extra(gen, {"thinkingConfig": {"thinkingBudget": 2048}, "topP": 0.9})
    check("includeThoughts survives a provider thinkingConfig", gen["thinkingConfig"].get("includeThoughts") is True, gen)
    check("naming a budget removes the level jarvis chose (never both)",
          gen["thinkingConfig"] == {"thinkingBudget": 2048, "includeThoughts": True}, gen)
    check("other generationConfig keys still merge", gen["topP"] == 0.9 and gen["maxOutputTokens"] == 3072)
    gen = {"thinkingConfig": {"thinkingBudget": 4096, "includeThoughts": True}}
    ap._merge_gemini_extra(gen, {"thinkingConfig": {"thinkingLevel": "low"}})
    check("naming a level removes the budget", gen["thinkingConfig"] == {"thinkingLevel": "low", "includeThoughts": True}, gen)
    gen = {"thinkingConfig": {"thinkingBudget": 4096, "includeThoughts": True}}
    ap._merge_gemini_extra(gen, {"thinkingConfig": {"includeThoughts": False}})
    check("the provider block wins where it names a key", gen["thinkingConfig"]["includeThoughts"] is False, gen)
    gen = {"maxOutputTokens": 10}
    ap._merge_gemini_extra(gen, {"thinkingConfig": {"thinkingBudget": 0}})
    check("no jarvis thinkingConfig: the provider's is used as written", gen["thinkingConfig"] == {"thinkingBudget": 0}, gen)
    gen = {"maxOutputTokens": 10}
    ap._merge_gemini_extra(gen, {"topK": 3})
    check("no thinkingConfig anywhere: plain update", gen == {"maxOutputTokens": 10, "topK": 3}, gen)


def test_thought_gap_is_logged():
    lines = []
    orig = ap.logs.log
    ap.logs.log = lambda conv, kind, data, provider=None, **k: lines.append((kind, data))
    try:
        silent = {"candidates": [{"content": {"parts": [{"text": "answer"}]}, "finishReason": "STOP"}],
                  "usageMetadata": {"thoughtsTokenCount": 77}}
        _run_gemini("gemini-3.6-flash", "medium", silent)
        gaps = [d for k, d in lines if k == "info" and "thinking" in d]
        check("thinking requested, tokens spent, no summary: one info line", len(gaps) == 1, lines)
        check("it records what was sent and what was spent",
              gaps and gaps[0]["thoughts_token_count"] == 77 and gaps[0]["sent"].get("thinkingLevel") == "medium", gaps)
        lines.clear()
        _run_gemini("gemini-3.6-flash", "medium", json.loads(json.dumps(_THOUGHT_REPLY)))
        check("a summary came back: no line", not [1 for k, d in lines if k == "info" and "thinking" in d], lines)
        lines.clear()
        _run_gemini("gemini-3.6-flash", "off", json.loads(json.dumps(silent)))
        check("thinking off: no line", not [1 for k, d in lines if k == "info" and "thinking" in d], lines)
    finally:
        ap.logs.log = orig


# ----------------------------------------------------------------- 3. narration
def test_detector():
    names = ["search_files", "read_file"]
    yes = [
        "I will now search for `cli.py` in D:\\projects\\jarvis. Let's proceed with that.",
        "Let me use search_files to find it.",
        "I'll call read_file on that path now.",
        "I am going to look for the file.",
        "Now I will check the folder.",
    ]
    no = [
        "",
        "Done. I found the file at D:\\x\\cli.py and it has 4,000 lines.",
        "Let me know if you want me to check anything else.",
        "Do you want me to search for it?",
        "Here are the results: three files matched.",
        "I couldn't find it, so I will need your help with the exact path.",
        "The weather in Tunis is sunny, 24 degrees.",
        "I searched for cli.py and it does not exist there.",
        "x" * 500,
        None,
    ]
    for t in yes:
        check(f"announcement: {str(t)[:46]!r}", narration_guard.looks_like_narrated_call(t, names), t)
    for t in no:
        check(f"not an announcement: {str(t)[:46]!r}", not narration_guard.looks_like_narrated_call(t, names), t)
    long_answer = ("The build failed because the config is missing. " * 3
                   + "I will check the logs next.")
    check("a longer reply ending in intent counts", narration_guard.looks_like_narrated_call(long_answer, names))
    mid = "First I will check the logs. The file is missing and the build fails. That is the whole story here."
    check("a plan mentioned mid-way in a finished reply does not", not narration_guard.looks_like_narrated_call(mid, names), mid)


def _ollama(responses, provider_extra=None, executor=None):
    sent = []
    queue = list(responses)
    orig_post, orig_status = ap._post_json, ap._status_reason

    def fake_post(url, headers, payload, timeout):
        sent.append(json.loads(json.dumps(payload)))
        return Resp(queue.pop(0)), None

    ap._post_json, ap._status_reason = fake_post, lambda r: None
    ran = []
    try:
        ap.set_log_context(None, "ollama")
        prov = {"name": "ollama", "type": "ollama", "model": "qwen2.5:14b", "base_url": "http://localhost:11434/api/chat"}
        prov.update(provider_extra or {})
        tools = [{"name": "search_files", "description": "d", "parameters": {"type": "object", "properties": {}}}]

        def default_exec(name, args):
            ran.append(name)
            return {"ok": True, "matches": ["a.py"]}

        res = ap.ADAPTERS["ollama"](prov, [{"role": "system", "content": "s"}, {"role": "user", "content": "find cli.py"}],
                                    30, tools=tools, tool_executor=executor or default_exec)
        return res, sent, ran
    finally:
        ap._post_json, ap._status_reason = orig_post, orig_status
        ap.clear_log_context()


def _say(text):
    return {"message": {"role": "assistant", "content": text}, "done_reason": "stop"}


_CALL = {"message": {"role": "assistant", "content": "", "tool_calls": [
    {"function": {"name": "search_files", "arguments": {"q": "cli.py"}}}]}, "done_reason": "stop"}


def test_ollama_is_nudged_into_calling_the_tool():
    res, sent, ran = _ollama([_say("I will now search for cli.py. Let's proceed."), _CALL, _say("Found it: a.py")])
    check("the model ends up calling the tool", ran == ["search_files"], ran)
    check("the final answer is the real one, not the announcement", res.ok and res.text == "Found it: a.py", res.text)
    second = sent[1]["messages"]
    check("the announcement and the nudge were sent back",
          second[-2]["role"] == "assistant" and "search for cli.py" in second[-2]["content"]
          and second[-1]["role"] == "user" and second[-1]["content"] == narration_guard.NUDGE, second[-2:])
    check("exactly three requests were made", len(sent) == 3, len(sent))


def test_ollama_nudge_is_capped():
    res, sent, ran = _ollama([_say("I will now search for cli.py.")] * 3)
    check("after two nudges the text is accepted as the answer", res.ok and "search for cli.py" in res.text, (res.ok, res.text))
    check("two nudges means three requests", len(sent) == 3, len(sent))
    check("no tool was ever run", ran == [], ran)


def test_ollama_nudge_can_be_turned_off_and_leaves_answers_alone():
    res, sent, ran = _ollama([_say("I will now search for cli.py.")], {"narration_retry": False})
    check("narration_retry false: old behaviour, one request", res.ok and len(sent) == 1, len(sent))
    res, sent, ran = _ollama([_say("Found it. The file is at D:\\x\\cli.py.")])
    check("a real answer is never nudged", res.ok and len(sent) == 1, len(sent))


test_isolated_call_restores_everything()
test_restores_on_exception_and_missing_fields()
test_risk_review_is_isolated()
test_gemini_levels_vs_budgets()
test_gemini_adapter_requests_and_keeps_thoughts()
test_extra_params_do_not_wipe_include_thoughts()
test_thought_gap_is_logged()
test_detector()
test_ollama_is_nudged_into_calling_the_tool()
test_ollama_nudge_is_capped()
test_ollama_nudge_can_be_turned_off_and_leaves_answers_alone()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
