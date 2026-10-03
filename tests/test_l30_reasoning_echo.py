"""Tests for the Groq "property 'reasoning_content' is unsupported" 400
(owner log 7b5013b522d1fe40, 2026-10-03).

What the log showed: on Groq, the first round of a tool call worked. The
SECOND round of the same ask always came back 400 on `messages.N`, because the
assistant message appended after a tool call still carried `reasoning_content`
(the streaming accumulator adds that key). jarvis then took the 400 for a
rejected thinking knob, stripped `reasoning_effort` from the payload (which
was never the cause), failed again, and moved to the next key - four keys
spent on one question. DeepSeek is the one host that wants reasoning handed
back during a tool loop, so it keeps it.

No network, no API keys.   Run: python3 tests/test_l30_reasoning_echo.py
"""

import copy
import os
import sys
import tempfile
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_providers  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


GROQ = {"name": "groq", "type": "openai_compatible",
        "base_url": "https://api.groq.com/openai/v1/chat/completions",
        "api_key": "k", "model": "openai/gpt-oss-120b", "max_tokens": 200}
DEEPSEEK = {"name": "deepseek", "type": "openai_compatible",
            "base_url": "https://api.deepseek.com/chat/completions",
            "api_key": "k", "model": "deepseek-reasoner", "max_tokens": 200}
TOOLS = [{"name": "ping", "description": "d", "parameters": {"type": "object", "properties": {}}}]

TOOL_ROUND = {"choices": [{"finish_reason": "tool_calls", "message": {
    "role": "assistant", "content": "", "reasoning_content": "need ping",
    "tool_calls": [{"id": "c1", "type": "function",
                    "function": {"name": "ping", "arguments": "{}"}}]}}]}
FINAL = {"choices": [{"finish_reason": "stop", "message": {
    "role": "assistant", "content": "done", "reasoning_content": "ok"}}]}


class Resp:
    def __init__(self, status, body, text=""):
        self.status_code, self._b, self.text, self.headers = status, body, text, {}

    def json(self):
        return self._b

    def close(self):
        pass


def _run(provider, scripted):
    """scripted: list of (status, body, text) answered in order. Returns
    (result, payload snapshots in the order they were sent)."""
    sent, queue = [], list(scripted)
    old_post, old_parse = ai_providers._post_json, ai_providers._parse_json

    def fake_post(url, headers, payload, timeout):
        sent.append(copy.deepcopy(payload))
        st, body, txt = queue.pop(0)
        return Resp(st, body, txt), None

    ai_providers._post_json = fake_post
    ai_providers._parse_json = lambda r: (r.json(), None)
    try:
        res = ai_providers.call_openai_compatible(
            provider, [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
            5, tools=TOOLS, tool_executor=lambda n, a: {"ok": True})
    finally:
        ai_providers._post_json, ai_providers._parse_json = old_post, old_parse
    return res, sent


def _assistants(payload):
    return [m for m in payload["messages"] if m.get("role") == "assistant"]


def test_groq_second_round_does_not_echo_reasoning():
    res, sent = _run(GROQ, [(200, copy.deepcopy(TOOL_ROUND), ""), (200, copy.deepcopy(FINAL), "")])
    check("two requests were sent", len(sent) == 2, len(sent))
    a = _assistants(sent[1])
    check("the tool-call assistant message is in the second request", len(a) == 1 and a[0].get("tool_calls"), a)
    check("...without reasoning_content", all("reasoning_content" not in m and "reasoning" not in m for m in a), a)
    check("...and the call itself is intact", a[0]["tool_calls"][0]["function"]["name"] == "ping")
    check("the ask succeeded", res.ok and res.text == "done", getattr(res, "error", ""))


def test_deepseek_keeps_reasoning_for_its_tool_loop():
    _, sent = _run(DEEPSEEK, [(200, copy.deepcopy(TOOL_ROUND), ""), (200, copy.deepcopy(FINAL), "")])
    a = _assistants(sent[1])
    check("DeepSeek still gets reasoning_content back", a and a[0].get("reasoning_content") == "need ping", a)


def test_helper_leaves_the_original_message_alone():
    msg = copy.deepcopy(TOOL_ROUND["choices"][0]["message"])
    out = ai_providers._history_message(msg, GROQ)
    check("returns a copy without the trace", "reasoning_content" not in out and out is not msg)
    check("the original keeps its trace (the log and trace read it)", msg.get("reasoning_content") == "need ping")
    plain = {"role": "assistant", "content": "x"}
    check("a message with no trace is returned as is", ai_providers._history_message(plain, GROQ) is plain)
    check("non-dict input is passed through", ai_providers._history_message(None, GROQ) is None)


def test_rejection_naming_reasoning_content_is_repaired_not_misattributed():
    # A transcript that already carries the key (a failover hand-over, an older
    # build). The host rejects it; the retry must fix THAT and keep the knob.
    bad = ("HTTP 400: 'messages.2' : for 'role:assistant' the following must be satisfied"
           "[('messages.2' : property 'reasoning_content' is unsupported)]")
    err = {"error": {"message": bad}}
    sent, queue = [], [(400, err, bad), (200, copy.deepcopy(FINAL), "")]
    old_post, old_parse = ai_providers._post_json, ai_providers._parse_json

    def fake_post(url, headers, payload, timeout):
        sent.append(copy.deepcopy(payload))
        st, body, txt = queue.pop(0)
        return Resp(st, body, txt), None

    ai_providers._post_json = fake_post
    ai_providers._parse_json = lambda r: (r.json(), None)
    try:
        res = ai_providers.call_openai_compatible(
            GROQ, [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"},
                   {"role": "assistant", "content": "earlier", "reasoning_content": "stale trace"},
                   {"role": "user", "content": "again"}],
            5, tools=None, tool_executor=None)
    finally:
        ai_providers._post_json, ai_providers._parse_json = old_post, old_parse
    check("exactly one retry, not a key rotation", len(sent) == 2 and res.ok, (len(sent), getattr(res, "error", "")))
    first = [m for m in sent[0]["messages"] if m.get("role") == "assistant"]
    second = [m for m in sent[1]["messages"] if m.get("role") == "assistant"]
    check("the first request did carry the key (the scenario is real)", first and "reasoning_content" in first[0])
    check("the retry no longer carries it", second and "reasoning_content" not in second[0], second)

    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "reasoning_content": "t", "tool_calls": []}]
    check("repair removes the key in place", ai_providers._strip_reasoning_from_messages(msgs)
          and "reasoning_content" not in msgs[1])
    check("repair reports nothing to do the second time", not ai_providers._strip_reasoning_from_messages(msgs))


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
