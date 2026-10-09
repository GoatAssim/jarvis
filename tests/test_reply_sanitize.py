"""Tests for reply_sanitize.py — the degenerate-output guard.

Regression for a Discord reply that was ~380 characters of zero-width spaces,
NBSPs, ellipses and "**" with one real sentence at the end (gpt-oss-120b on
Groq, straight after a successful send_dm). It used to be posted verbatim.

No network: requests.post is monkeypatched. Same no-framework convention as
test_ai_providers_fixes.py — run: python3 tests/test_reply_sanitize.py
"""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_providers  # noqa: E402
from jarvis.reply_sanitize import is_degenerate, sanitize_reply_text  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


# The exact content field of the logged bad reply (no personal data in it).
LOGGED_BAD_REPLY = 'Message\u202f\u200b\u200b\u200b\u200b\u200b\u200b\u200b\u200b\u200b\u200b\u200b\u200b\u200b\u200b\u200b\u200b\u200b\u200b\xa0\u200b\xa0\u200b\xa0\u200b\xa0\u200b\xa0\xa0\u200b\xa0\xa0\u200b\xa0\xa0\u200b\xa0\xa0\u200b\xa0\xa0\u200b **\u200b\xa0\u200b\u200b\u200b\xa0\u200b**\xa0\xa0\u200b**\xa0\xa0\u200b\xa0\u200b\xa0**\xa0\u200b\xa0\xa0\u200b\xa0\xa0\u2026\n\nIt appears\xa0\xa0\u2026\u2026...\u2026\u2026\u2026\u2026\u2026\n\n...\xa0\xa0\u2026\u2026\xa0\xa0\u2026\n\n...\xa0\xa0\xa0\u2026\n\n\xa0\xa0\u2026\xa0\u2026\u2026\n\n...\xa0\u2026\u2026\xa0We\xa0\xa0\u2026\u2026\n\n\xa0\u2026\n\n\u2026\u2026\xa0...\xa0\u2026\n\n\u2026\xa0\u2026\n\n...\xa0\u2026\u2026\u2026\u2026\n\n\u2026\n\n\u2026\xa0\u2026\n\n\u2026\n\n\xa0\u2026\u2026\n\n\u2026\n\n\u2026\n\n\n\n\n\nApologies\u2026\u2026\u2026\n\n...\xa0\n\n\n\n\u2026\xa0\n\n\n\n\xa0\u2026\n\n\u2026\xa0\u2026\xa0\u2026\n\n\xa0\n\n\n\n\u2026\n\n\u2026\n\n\n\n\n\n\u2026\n\n\n\n\u2026\xa0\u2026\n\n...\xa0...\u2026\n\n\n\n...\n\nIt looks like the message was sent successfully, sir. Let me know if there\u2019s anything else.'

GOOD_TAIL = "It looks like the message was sent successfully, sir. Let me know if there\u2019s anything else."


def test_normal_replies_are_returned_untouched():
    for t in [
        "Done, sir.",
        "**Bold** and *italic*, plus an ellipsis\u2026 and another\u2026 for flavour.",
        "Line one.\n\nLine two with a non\u00a0breaking space.",
        "Family: \U0001F468\u200d\U0001F469\u200d\U0001F467 (ZWJ emoji must survive).",
        "",
    ]:
        check("untouched: %r" % t[:30], sanitize_reply_text(t) == t and not is_degenerate(t), t)


def test_logged_bad_reply_is_detected_and_salvaged():
    check("logged reply is flagged degenerate", is_degenerate(LOGGED_BAD_REPLY))
    out = sanitize_reply_text(LOGGED_BAD_REPLY)
    check("only the real closing sentences survive", out == GOOD_TAIL, repr(out))
    check("no invisible characters remain", "\u200b" not in out and "\u00a0" not in out and "\u202f" not in out, repr(out))


def test_pure_noise_becomes_empty():
    noise = ("\u200b\u00a0\u2026 " * 40)
    check("pure noise is flagged", is_degenerate(noise))
    check("pure noise sanitizes to empty", sanitize_reply_text(noise) == "", repr(sanitize_reply_text(noise)))
    frag = "It appears \u2026\u2026\n\u200b" * 10 + "We \u2026" * 10
    check("fragments without a finished sentence are dropped", sanitize_reply_text(frag) == "", repr(sanitize_reply_text(frag)))


GROQ = {"name": "groq", "type": "openai_compatible",
        "base_url": "https://api.groq.com/openai/v1/chat/completions",
        "api_key": "k", "model": "openai/gpt-oss-120b"}


class FakeResponse:
    def __init__(self, body):
        self.status_code = 200
        self._body = body
        self.text = __import__("json").dumps(body)

    def json(self):
        return self._body


def _run_adapter(content):
    """One tool round (send_dm succeeds) then a final reply `content`."""
    calls = {"n": 0}
    first = {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "send_dm", "arguments": "{}"}}]}}]}
    second = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}]}
    queue = [FakeResponse(first), FakeResponse(second)]
    orig = ai_providers.requests.post
    ai_providers.requests.post = lambda *a, **k: queue.pop(0)
    try:
        return ai_providers.call_openai_compatible(
            GROQ, [{"role": "user", "content": "hi"}], timeout=5,
            tools=[{"name": "send_dm", "description": "d", "parameters": {"type": "object", "properties": {}}}],
            tool_executor=lambda name, args: {"ok": True}, round_budget=ai_providers.RoundBudget(),
        )
    finally:
        ai_providers.requests.post = orig


def test_adapter_posts_the_salvaged_sentence_not_the_noise():
    r = _run_adapter(LOGGED_BAD_REPLY)
    check("adapter result is ok", r.ok is True, (r.ok, r.error))
    check("adapter text is the clean sentence", r.text == GOOD_TAIL, repr(r.text))


def test_adapter_fails_over_on_pure_noise_keeping_tool_history():
    r = _run_adapter("\u200b\u00a0\u2026 " * 40)
    check("pure-noise reply is a failure, not a posted message", r.ok is False, (r.ok, r.text))
    check("it is classified as an empty reply", r.kind == ai_providers.KIND_EMPTY, r.kind)
    check("tool history is kept so the next provider does not re-run send_dm",
          bool(r.tool_history), r.tool_history)


def test_adapter_leaves_a_normal_reply_alone():
    r = _run_adapter("Sent, sir.")
    check("normal reply unchanged", r.ok is True and r.text == "Sent, sir.", (r.ok, r.text, r.error))


for fn in [
    test_normal_replies_are_returned_untouched,
    test_logged_bad_reply_is_detected_and_salvaged,
    test_pure_noise_becomes_empty,
    test_adapter_posts_the_salvaged_sentence_not_the_noise,
    test_adapter_fails_over_on_pure_noise_keeping_tool_history,
    test_adapter_leaves_a_normal_reply_alone,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
