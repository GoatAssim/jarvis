"""Tests for F.1 + F.8: what happens when the tool budget is spent.

Before: the request kept the whole native tool-call transcript but dropped
`tools`, models kept trying to call one, and every adapter reported a dead key
and rotated. Now the adapters hand the ending to ai_providers._forced_ending():
an optional grace round (tools back, once), then a flattened, tool-less final
request; if the model STILL wants a tool that is KIND_BUDGET and ask() ends
through a harness-written reply instead of rotating keys.

No network: requests.post is a queue of canned responses.
Run: python3 tests/test_forced_ending.py
"""
import copy
import json
import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_client, ai_providers as ap  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, str(detail)))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + str(detail)}")


class Resp:
    def __init__(self, status, body):
        self.status_code, self._b, self.text = status, body, json.dumps(body)

    def json(self):
        return self._b


class Queue:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls.append(copy.deepcopy(json))
        return self.responses.pop(0)


def _call(name="web_search", args="{}"):
    return Resp(200, {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": name, "arguments": args}}]}}]})


def _text(t):
    return Resp(200, {"choices": [{"message": {"role": "assistant", "content": t}}]})


PROV = {"name": "openai", "type": "openai_compatible", "base_url": "https://x.example/v1/chat", "api_key": "k", "model": "m"}
TOOLS = [{"name": "web_search", "description": "s", "parameters": {"type": "object", "properties": {}}}]
MSGS = [{"role": "user", "content": "do the thing"}]


def run(responses, grace, limit=1):
    q = Queue(responses)
    orig = ap.requests.post
    ap.requests.post = q
    seen = []

    def executor(name, args):
        seen.append((name, args))
        return {"ok": True, "n": len(seen)}

    budget = ap.RoundBudget(limit=limit, grace=grace)
    try:
        r = ap.call_openai_compatible(PROV, MSGS, 5, tools=TOOLS, tool_executor=executor, round_budget=budget, cfg_defaults={})
    finally:
        ap.requests.post = orig
    return r, q, seen, budget


def _structure_free(payload):
    return all(m.get("role") != "tool" and "tool_calls" not in m for m in payload["messages"])


def test_grace_then_flat_final():
    r, q, seen, b = run([_call(), _call(args='{"q": "2"}'), _text("all done")], grace=True)
    check("grace: ok with the final text", r.ok and r.text == "all done", (r.ok, r.error))
    check("grace: 3 requests (tool, grace, final)", len(q.calls) == 3, len(q.calls))
    check("grace: executor ran the round-1 call and the grace call", len(seen) == 2, seen)
    check("grace request offers tools again, with no native structure", "tools" in q.calls[1] and _structure_free(q.calls[1]))
    check("final request: no tools, no native structure", "tools" not in q.calls[2] and _structure_free(q.calls[2]))
    last = q.calls[2]["messages"][-1]["content"]
    check("final request carries the shared notice", "Do not call any tool" in last, last)
    check("history is prose, not [called ...] syntax", "[called" not in json.dumps(q.calls[2]) and "I ran web_search" in json.dumps(q.calls[2]))
    check("grace is used up", b.grace_used and not b.take_grace())


def test_no_grace_when_disabled():
    r, q, seen, _ = run([_call(), _text("fine")], grace=False)
    check("no grace: 2 requests, executor ran once", r.ok and len(q.calls) == 2 and len(seen) == 1, (len(q.calls), seen))
    check("no grace: withheld request is flat with notice", "tools" not in q.calls[1] and _structure_free(q.calls[1]))


def test_grace_round_can_just_answer():
    r, q, seen, _ = run([_call(), _text("answer")], grace=True)
    check("grace round answering in text is returned as is", r.ok and r.text == "answer" and len(q.calls) == 2 and len(seen) == 1)


def test_still_wants_tool_is_budget_kind_with_pending():
    r, q, _, _ = run([_call(), _call(args='{"q": "x"}')], grace=False)
    check("still calling: not ok", not r.ok)
    check("still calling: KIND_BUDGET (never rotate)", r.kind == ap.KIND_BUDGET, r.kind)
    check("still calling: pending names the call", r.pending and r.pending[0][0] == "web_search" and r.pending[0][1] == {"q": "x"}, r.pending)


def test_reasoning_channel_call_is_recognised():
    reasoning = '{"name": "repo_browser.web_search", "arguments": {"q": "hi"}}'
    resp = Resp(200, {"choices": [{"message": {"role": "assistant", "content": "", "reasoning": reasoning}}]})
    r, _, _, _ = run([_call(), resp], grace=False)
    check("call hidden in reasoning -> KIND_BUDGET with prefix stripped", r.kind == ap.KIND_BUDGET and r.pending and r.pending[0][0] == "web_search", (r.kind, r.pending))


def test_fake_call_text_as_final_answer():
    r, _, _, _ = run([_call(), _text('[called web_search with {"q": "z"}]')], grace=False)
    check("a reply that is only a fake call is KIND_BUDGET, not an answer", not r.ok and r.kind == ap.KIND_BUDGET, (r.ok, r.kind))


def test_real_failure_in_grace_round_still_rotates():
    r, _, _, _ = run([_call(), Resp(429, {"error": {"message": "rate limit"}})], grace=True)
    check("429 during the grace round is a KEY failure", not r.ok and r.kind == ap.KIND_KEY, (r.kind, r.error))


def test_classifier_and_helpers():
    c = ap.classify_failure
    check("classify budget", c("gave up after 5 rounds of tool calls with no final answer") == ap.KIND_BUDGET)
    check("classify malformed", c("malformed tool call from the model (Gemini finishReason MALFORMED_FUNCTION_CALL)") == ap.KIND_MALFORMED)
    check("classify key/network/empty", c("rate limited or quota exceeded") == ap.KIND_KEY and c("request timed out") == ap.KIND_NETWORK
          and c("empty response content") == ap.KIND_EMPTY)
    check("classify shape", c("Tool choice is none, but model called a tool") == ap.KIND_SHAPE)
    check("AIResult derives kind from the error", ap.AIResult(False, error="empty response content").kind == ap.KIND_EMPTY)
    check("strips gpt-oss prefixes", ap._normalize_tool_name("repo_browser.list_dir") == "list_dir" and ap._normalize_tool_name("functions.x") == "x")
    b = ap.RoundBudget()
    check("grace off by default", not b.take_grace())
    check("existing tests' plain path unaffected: ended-budget + no grace + no history -> no forced ending",
          not ap._forced_ending_due(5, ap.RoundBudget(), False, None))


def test_ask_side_reply():
    runs = [{"name": "search_tools", "result": {"ok": True}},
            {"name": "list_dir", "result": {"ok": True, "summary": "3 files"}},
            {"name": "run_shell", "result": {"error": "denied"}}]
    reply = ai_client._forced_ending_reply(runs, [("run_custom_command", {"command": "Move-Item a b"})])
    low = reply.lower()
    check("reply lists what ran, skipping discovery", "list_dir" in reply and "search_tools" not in reply and "denied" in reply, reply)
    check("reply shows the command that was about to run", "Move-Item a b" in reply, reply)
    check("reply never mentions budget/tools/rounds", not any(w in low for w in ("budget", "exhausted", "tool call", "rounds")), reply)
    check("reply without a pending call still reads sensibly", "continue" in ai_client._forced_ending_reply([], None).lower())
    check("path tools count as mutating", all(ai_client._is_mutating_tool(n) for n in ("move_path", "copy_path", "rename_path", "make_dir", "delete_path")))


for fn in (test_grace_then_flat_final, test_no_grace_when_disabled, test_grace_round_can_just_answer,
           test_still_wants_tool_is_budget_kind_with_pending, test_reasoning_channel_call_is_recognised,
           test_fake_call_text_as_final_answer, test_real_failure_in_grace_round_still_rotates,
           test_classifier_and_helpers, test_ask_side_reply):
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    sys.exit(1)
