"""Tests for two ai_providers.py fixes:

1. call_openai_compatible's one-shot retry when a provider (Groq,
   specifically) 400s because jarvis omitted `tools` on the forced-final
   round but the model tried to call one anyway.
2. call_gemini's GEMINI_DEFAULT_MAX_TOKENS bump and the clearer
   MAX_TOKENS/thinking-budget diagnostic, plus extra_params merging into
   generationConfig.

No real network calls — requests.post is monkeypatched with a queue of
canned responses.

Run: python3 tests/test_ai_providers_fixes.py   (from jarvis-cli/)
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_providers  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = __import__("json").dumps(body)

    def json(self):
        return self._body


class FakePostQueue:
    """Stands in for requests.post: returns each queued response in turn,
    recording the payload it was called with so tests can assert on what
    was actually sent."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers=None, json=None, timeout=None):
        # Deep-copy the payload: call_openai_compatible mutates the SAME
        # dict object in place for the retry (adding "tools"), so without
        # copying, calls[0]'s recorded payload would silently pick up the
        # retry's mutation too.
        import copy
        self.calls.append({"url": url, "payload": copy.deepcopy(json)})
        if not self.responses:
            raise AssertionError("FakePostQueue exhausted — more calls than expected")
        return self.responses.pop(0)


def _patch_post(responses):
    orig = ai_providers.requests.post
    fake = FakePostQueue(responses)
    ai_providers.requests.post = fake
    return fake, (lambda: setattr(ai_providers.requests, "post", orig))


GROQ_PROVIDER = {"name": "groq", "type": "openai_compatible",
                  "base_url": "https://api.groq.com/openai/v1/chat/completions",
                  "api_key": "k", "model": "llama-3.3-70b"}

TOOLS = [{"name": "web_search", "description": "search", "parameters": {"type": "object", "properties": {}}}]


def test_retries_once_with_tools_after_tool_choice_none_mismatch():
    # Round budget exhausted -> tools omitted -> Groq 400s because the
    # model called a tool anyway -> retry the SAME round with tools
    # included -> succeeds.
    error_body = {"error": {"message": "Tool choice is none, but model called a tool",
                             "type": "invalid_request_error", "code": "tool_use_failed"}}
    success_body = {"choices": [{"message": {"role": "assistant", "content": "all done"}}]}
    fake, restore = _patch_post([FakeResponse(400, error_body), FakeResponse(200, success_body)])
    try:
        budget = ai_providers.RoundBudget()
        budget.remaining = lambda: 0  # forces tools to be omitted this round
        budget.take = lambda: False
        result = ai_providers.call_openai_compatible(
            GROQ_PROVIDER, [{"role": "user", "content": "hi"}], timeout=5,
            tools=TOOLS, tool_executor=None, round_budget=budget,
        )
        check("two requests were made (original + retry)", len(fake.calls) == 2, fake.calls)
        check("the first request omitted tools", "tools" not in fake.calls[0]["payload"], fake.calls[0])
        check("the retry included tools", "tools" in fake.calls[1]["payload"], fake.calls[1])
        check("the retry's success is returned as ok", result.ok is True, (result.ok, result.error))
        check("the retried reply's text comes through", result.text == "all done", result.text)
    finally:
        restore()


def test_does_not_retry_for_an_unrelated_400():
    error_body = {"error": {"message": "invalid model name", "type": "invalid_request_error"}}
    fake, restore = _patch_post([FakeResponse(400, error_body)])
    try:
        budget = ai_providers.RoundBudget()
        budget.remaining = lambda: 0
        budget.take = lambda: False
        result = ai_providers.call_openai_compatible(
            GROQ_PROVIDER, [{"role": "user", "content": "hi"}], timeout=5,
            tools=TOOLS, tool_executor=None, round_budget=budget,
        )
        check("only one request was made — no spurious retry", len(fake.calls) == 1, fake.calls)
        check("the original error is surfaced unchanged", result.ok is False and "invalid model name" in result.error, result.error)
    finally:
        restore()


def test_does_not_retry_when_tools_were_already_included():
    # Fresh round budget -> tools ARE included -> if Groq still 400s this
    # way it's not the omitted-tools mismatch, so no retry should fire
    # (nothing to gain by resending the identical request).
    error_body = {"error": {"message": "Tool choice is none, but model called a tool"}}
    fake, restore = _patch_post([FakeResponse(400, error_body)])
    try:
        result = ai_providers.call_openai_compatible(
            GROQ_PROVIDER, [{"role": "user", "content": "hi"}], timeout=5,
            tools=TOOLS, tool_executor=None, round_budget=ai_providers.RoundBudget(),
        )
        check("only one request was made", len(fake.calls) == 1, fake.calls)
        check("tools were present on that one request", "tools" in fake.calls[0]["payload"], fake.calls[0])
        check("the error is reported as a normal failure", result.ok is False, result)
    finally:
        restore()


def test_pattern_matcher_recognizes_both_observed_message_shapes():
    check("'Tool choice is none...called a tool' matches",
          ai_providers._looks_like_omitted_tools_confused_the_model(
              "HTTP 400: Tool choice is none, but model called a tool"))
    check("'which was not in request.tools' matches",
          ai_providers._looks_like_omitted_tools_confused_the_model(
              "HTTP 400: attempted to call tool 'web_search' which was not in request.tools"))
    check("an unrelated 400 does not match",
          not ai_providers._looks_like_omitted_tools_confused_the_model("HTTP 400: invalid model name"))
    check("None/empty reason does not match",
          not ai_providers._looks_like_omitted_tools_confused_the_model(None))


GEMINI_PROVIDER = {"name": "gemini", "type": "gemini", "api_key": "k", "model": "gemini-2.5-flash",
                    "base_url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"}


def test_gemini_default_max_tokens_is_raised_from_the_old_700():
    body = {"candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}]}
    fake, restore = _patch_post([FakeResponse(200, body)])
    try:
        ai_providers.call_gemini(GEMINI_PROVIDER, [{"role": "user", "content": "hi"}], timeout=5)
        sent = fake.calls[0]["payload"]
        check("the default maxOutputTokens is well above the old 700",
              sent["generationConfig"]["maxOutputTokens"] == ai_providers.GEMINI_DEFAULT_MAX_TOKENS,
              sent["generationConfig"])
        check("the new default is meaningfully higher than the old one",
              ai_providers.GEMINI_DEFAULT_MAX_TOKENS > 700)
    finally:
        restore()


def test_gemini_explicit_max_tokens_still_wins():
    body = {"candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}]}
    fake, restore = _patch_post([FakeResponse(200, body)])
    try:
        provider = dict(GEMINI_PROVIDER, max_tokens=222)
        ai_providers.call_gemini(provider, [{"role": "user", "content": "hi"}], timeout=5)
        check("an explicit max_tokens overrides the new default",
              fake.calls[0]["payload"]["generationConfig"]["maxOutputTokens"] == 222)
    finally:
        restore()


def test_gemini_max_tokens_with_no_text_gets_a_diagnostic_not_a_generic_error():
    body = {"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}],
            "usageMetadata": {"thoughtsTokenCount": 3000}}
    fake, restore = _patch_post([FakeResponse(200, body)])
    try:
        result = ai_providers.call_gemini(GEMINI_PROVIDER, [{"role": "user", "content": "hi"}], timeout=5)
        check("reports failure", result.ok is False)
        check("the error explains it hit max_tokens, not a generic empty response",
              "max_tokens" in result.error.lower(), result.error)
        check("the thinking-token count is surfaced when available",
              "3000" in result.error, result.error)
        check("actionable config advice is included",
              "thinkingBudget" in result.error or "max_tokens" in result.error, result.error)
    finally:
        restore()


def test_gemini_genuinely_empty_response_keeps_the_old_message():
    # No MAX_TOKENS finish reason and no text at all — the generic message
    # is still correct for this case (nothing new to diagnose).
    body = {"candidates": [{"content": {"parts": []}, "finishReason": "STOP"}]}
    fake, restore = _patch_post([FakeResponse(200, body)])
    try:
        result = ai_providers.call_gemini(GEMINI_PROVIDER, [{"role": "user", "content": "hi"}], timeout=5)
        check("falls back to the plain 'empty response content' message",
              result.error == "empty response content", result.error)
    finally:
        restore()


def test_gemini_extra_params_merge_into_generation_config():
    body = {"candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}]}
    fake, restore = _patch_post([FakeResponse(200, body)])
    try:
        provider = dict(GEMINI_PROVIDER, extra_params={"thinkingConfig": {"thinkingBudget": 0}})
        ai_providers.call_gemini(provider, [{"role": "user", "content": "hi"}], timeout=5)
        gen_cfg = fake.calls[0]["payload"]["generationConfig"]
        check("extra_params lands inside generationConfig",
              gen_cfg.get("thinkingConfig") == {"thinkingBudget": 0}, gen_cfg)
        check("maxOutputTokens is still present alongside it",
              "maxOutputTokens" in gen_cfg, gen_cfg)
    finally:
        restore()


for fn in [
    test_retries_once_with_tools_after_tool_choice_none_mismatch,
    test_does_not_retry_for_an_unrelated_400,
    test_does_not_retry_when_tools_were_already_included,
    test_pattern_matcher_recognizes_both_observed_message_shapes,
    test_gemini_default_max_tokens_is_raised_from_the_old_700,
    test_gemini_explicit_max_tokens_still_wins,
    test_gemini_max_tokens_with_no_text_gets_a_diagnostic_not_a_generic_error,
    test_gemini_genuinely_empty_response_keeps_the_old_message,
    test_gemini_extra_params_merge_into_generation_config,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
