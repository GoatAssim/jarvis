"""Tests for the §4 fix (master plan): an `openai_compatible`-typed provider
that's actually pointed at an Ollama server must get Ollama's `"think": true`
request shape, not `"reasoning_effort"`.

Why this exists: the log showed a second local provider, `ollama1`
(deepseek-r1:14b-qwen-distill-q4_K_M), sending `reasoning_effort` — the
openai_compatible-family shape — because it's configured with provider type
`openai_compatible` rather than `ollama`, so it never hit the `ollama`-native
branch in reasoning.round_patch(). Ollama silently ignores an unrecognized
`reasoning_effort` field, so this provider got no explicit thinking
instruction at all.

`ai_providers.py`'s adapters aren't touched by this fix — `_apply_thinking()`
already forwards `provider.get("base_url")` into `reasoning.round_patch()`;
this only changes what `reasoning.py` does with it. Not verified against a
real Ollama server (no network in this sandbox) — this only confirms the
request shape reasoning.py now builds, not that Ollama's OpenAI-compatible
endpoint actually honors `think` or reports back under `message.thinking`
the way its native endpoint does.

Run: python3 tests/test_ollama_openai_compat_thinking.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import reasoning  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


def test_ollama_detection_by_provider_name():
    check(
        "name containing 'ollama' is detected",
        reasoning._looks_like_ollama_host("ollama1", "", "some-model"),
    )
    check(
        "name is case-insensitive",
        reasoning._looks_like_ollama_host("My-OLLAMA-box", "", "some-model"),
    )


def test_ollama_detection_by_base_url():
    check(
        "default Ollama port in base_url is detected",
        reasoning._looks_like_ollama_host("", "http://localhost:11434/v1/chat/completions", "some-model"),
    )
    check(
        "a base_url ending in /api/chat is detected",
        reasoning._looks_like_ollama_host("", "http://my-box.local/api/chat", "some-model"),
    )
    check(
        "an unrelated base_url is not detected",
        not reasoning._looks_like_ollama_host("", "https://api.openai.com/v1/chat/completions", "gpt-4o"),
    )


def test_ollama_detection_by_model_tag_shape():
    check(
        "an Ollama-style name:tag model is detected with no other signal",
        reasoning._looks_like_ollama_host("", "", "deepseek-r1:14b-qwen-distill-q4_K_M"),
    )
    check(
        "an OpenRouter org/model:variant is NOT mistaken for a name:tag model",
        not reasoning._looks_like_ollama_host("openrouter", "", "meta-llama/llama-3.1-8b-instruct:free"),
    )


def test_openai_style_returns_ollama_native():
    style = reasoning._openai_style(
        "ollama1", "deepseek-r1:14b-qwen-distill-q4_K_M",
        "http://localhost:11434/v1/chat/completions",
    )
    check("style is ollama_native for the logged ollama1 config", style == "ollama_native", style)


def test_existing_families_are_unaffected():
    check("groq is still 'effort'", reasoning._openai_style("groq", "llama-3.3-70b-versatile", "https://api.groq.com/openai/v1/chat/completions") == "effort")
    check("openrouter is still 'object'", reasoning._openai_style("openrouter", "meta-llama/llama-3.1-8b-instruct:free", "") == "object")
    check("deepseek is still 'implicit'", reasoning._openai_style("deepseek", "deepseek-reasoner", "") == "implicit")
    check("mistral is still 'none'", reasoning._openai_style("mistral", "mistral-large-latest", "") == "none")
    check(
        "an unknown host with a non-reasoning model is still 'none'",
        reasoning._openai_style("myhost", "gpt-mini", "https://example.com/v1") == "none",
    )
    check(
        "an unknown host with a reasoning-named model is still 'effort'",
        reasoning._openai_style("myhost", "custom-o3-clone", "https://example.com/v1") == "effort",
    )


def test_request_patch_sends_think_for_ollama_flavored_openai_compatible():
    patch = reasoning.request_patch(
        "openai_compatible", "medium",
        provider_name="ollama1", model="deepseek-r1:14b-qwen-distill-q4_K_M",
        base_url="http://localhost:11434/v1/chat/completions",
    )
    check("request_patch sends exactly {'think': True}", patch == {"think": True}, patch)


def test_round_patch_sends_think_every_thinking_round():
    # Round 0: always thinks at the configured level.
    patch0 = reasoning.round_patch(
        "openai_compatible", "medium", round_num=0,
        provider_name="ollama1", model="deepseek-r1:14b-qwen-distill-q4_K_M",
        base_url="http://localhost:11434/v1/chat/completions",
    )
    check("round 0 sends {'think': True}", patch0 == {"think": True}, patch0)

    # A later round that should still think (post-tool synthesis round) also
    # gets the boolean flag, not a degraded effort string — "think" has no
    # graduated levels the way "reasoning_effort" does.
    patch1 = reasoning.round_patch(
        "openai_compatible", "medium", round_num=1, is_final=True, ran_tools=True,
        provider_name="ollama1", model="deepseek-r1:14b-qwen-distill-q4_K_M",
        base_url="http://localhost:11434/v1/chat/completions",
    )
    check("a later thinking round also sends {'think': True}", patch1 == {"think": True}, patch1)


def test_round_patch_still_empty_when_level_is_off():
    patch = reasoning.round_patch(
        "openai_compatible", "off", round_num=0,
        provider_name="ollama1", model="deepseek-r1:14b-qwen-distill-q4_K_M",
        base_url="http://localhost:11434/v1/chat/completions",
    )
    check("level off still yields no patch at all", patch == {}, patch)


def test_extract_trace_reads_thinking_field_for_openai_compatible():
    data = {"choices": [{"message": {"content": "hi", "thinking": "reasoning text"}}]}
    check(
        "extract_trace reads message.thinking for openai_compatible responses",
        reasoning.extract_trace("openai_compatible", data) == "reasoning text",
    )
    # Existing fields keep working unchanged.
    data2 = {"choices": [{"message": {"content": "hi", "reasoning": "groq style"}}]}
    check(
        "extract_trace still reads message.reasoning",
        reasoning.extract_trace("openai_compatible", data2) == "groq style",
    )


for fn in [
    test_ollama_detection_by_provider_name,
    test_ollama_detection_by_base_url,
    test_ollama_detection_by_model_tag_shape,
    test_openai_style_returns_ollama_native,
    test_existing_families_are_unaffected,
    test_request_patch_sends_think_for_ollama_flavored_openai_compatible,
    test_round_patch_sends_think_every_thinking_round,
    test_round_patch_still_empty_when_level_is_off,
    test_extract_trace_reads_thinking_field_for_openai_compatible,
]:
    fn()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
