"""Tests for the thinking-level fixes (reported 2026-10-06):

  * "Groq always has thinking on": Groq's gpt-oss models reason on every
    request and return the reasoning whether or not it was asked for, so
    `Thinking: Off` did nothing. reasoning.off_patch() now tells them to stop
    (or hide it) when the level is off, and ai_client no longer forwards live
    `thinking` stream events to the UI/CLI when the level is off.
  * "Gemini never shows its thinking trace": Gemini only returns thought
    summaries when the request carries a thinkingConfig, which Jarvis sends
    only when the level is not off. These tests pin that contract (off sends
    nothing to Gemini, on sends includeThoughts) so the asymmetry is explicit.

Run: python3 tests/test_thinking_off_groq.py
"""
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="jarvis-test-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import (  # noqa: E402
    ai_client, ai_config, ai_providers, conversations, key_health as kh, logs, reasoning,
)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    detail = str(detail)
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


# ---------------------------------------------------------------------------
# reasoning.off_patch
# ---------------------------------------------------------------------------
op = reasoning.off_patch
G = dict(provider_type="openai_compatible", provider_name="groq",
         base_url="https://api.groq.com/openai/v1/chat/completions")

check("groq gpt-oss: lowest effort and reasoning text not returned",
      op(model="openai/gpt-oss-120b", **G) == {"reasoning_effort": "low", "include_reasoning": False})
check("groq qwen3: reasoning_effort none",
      op(model="qwen/qwen3-32b", **G) == {"reasoning_effort": "none"})
check("groq deepseek r1 distill: reasoning hidden",
      op(model="deepseek-r1-distill-llama-70b", **G) == {"reasoning_format": "hidden"})
check("groq llama (no reasoning mode): nothing is added",
      op(model="llama-3.3-70b-versatile", **G) == {})
check("unknown groq model: nothing is added", op(model="something-new", **G) == {})
check("a groq host is recognised by base_url even under another name",
      op(provider_type="openai_compatible", provider_name="fast",
         base_url="https://api.groq.com/openai/v1/chat/completions",
         model="openai/gpt-oss-20b") != {})
check("openai (not groq) with a gpt-oss name is untouched",
      op(provider_type="openai_compatible", provider_name="openai",
         base_url="https://api.openai.com/v1/chat/completions", model="gpt-oss-120b") == {})
check("gemini / anthropic / ollama: nothing (off already means off)",
      all(op(provider_type=t, provider_name="groq", model="openai/gpt-oss-120b") == {}
          for t in ("gemini", "anthropic", "ollama", "cohere")))

check("include_reasoning is a thinking key (stripped by the rejection retry)",
      "include_reasoning" in reasoning._PATCH_KEYS)
_p = {"model": "m", "include_reasoning": False, "reasoning_effort": "low"}
check("strip_from_payload removes both off-switch keys",
      reasoning.strip_from_payload(_p) and "include_reasoning" not in _p and "reasoning_effort" not in _p, _p)
check("a 400 naming include_reasoning counts as a thinking rejection",
      reasoning.looks_like_thinking_rejected("400: include_reasoning is not supported for this model"))


# ---------------------------------------------------------------------------
# ai_providers._apply_thinking
# ---------------------------------------------------------------------------
GROQ = {"name": "groq", "model": "openai/gpt-oss-120b",
        "base_url": "https://api.groq.com/openai/v1/chat/completions"}
GEMINI = {"name": "gemini", "model": "gemini-2.5-flash"}

ai_providers.set_thinking("off")
pl = {"model": "x"}
ret = ai_providers._apply_thinking(pl, GROQ, "openai_compatible", 0, False, 0)
check("off + groq gpt-oss: off keys merged into the request",
      pl.get("reasoning_effort") == "low" and pl.get("include_reasoning") is False, pl)
check("...but the round is not counted as a thinking request",
      ret is False and ai_providers.get_thinking_trace()["requested"] == 0)

pl = {"model": "x", "reasoning_effort": "high"}
ai_providers._apply_thinking(pl, GROQ, "openai_compatible", 0, False, 0)
check("a key already in the payload is never overwritten", pl["reasoning_effort"] == "high", pl)

pl = {"generationConfig": {"maxOutputTokens": 100}}
ret = ai_providers._apply_thinking(pl, GEMINI, "gemini", 0, False, 0)
check("off + gemini: the request is left exactly as it was (no thinkingConfig, so no thoughts come back)",
      ret is False and pl == {"generationConfig": {"maxOutputTokens": 100}}, pl)

ai_providers.set_thinking("medium")
pl = {"generationConfig": {"maxOutputTokens": 100}}
ret = ai_providers._apply_thinking(pl, GEMINI, "gemini", 0, False, 0)
tc = (pl.get("generationConfig") or {}).get("thinkingConfig") or {}
check("medium + gemini: thinkingConfig asks for thoughts back",
      ret is True and tc.get("includeThoughts") is True and tc.get("thinkingBudget") == 4096, pl)

pl = {"model": "x"}
ret = ai_providers._apply_thinking(pl, GROQ, "openai_compatible", 0, False, 0)
check("medium + groq: asks for medium effort and does NOT hide the reasoning",
      ret is True and pl.get("reasoning_effort") == "medium" and "include_reasoning" not in pl, pl)
ai_providers.set_thinking("off")


# ---------------------------------------------------------------------------
# ask(): live thinking events are not forwarded when thinking is off
# ---------------------------------------------------------------------------

@contextmanager
def _env(adapter):
    orig = (ai_config.load_ai_config, dict(ai_providers.ADAPTERS),
            conversations.JARVIS_DIR, conversations.CONV_DIR, conversations.INDEX_FILE,
            conversations.CURRENT_FILE, logs.JARVIS_DIR, logs.LOG_DIR)
    cfg = {"persona": {}, "defaults": {"tools_enabled": False, "prompt_mode": "full"},
           "providers": [{"name": "fake1", "type": "fake", "enabled": True,
                         "api_keys": ["k"], "model": "m"}]}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conversations.JARVIS_DIR = tmp
        conversations.CONV_DIR = tmp / "conversations"
        conversations.INDEX_FILE = conversations.CONV_DIR / "index.json"
        conversations.CURRENT_FILE = tmp / "current_conversation.json"
        conversations.CONV_DIR.mkdir(parents=True, exist_ok=True)
        logs.JARVIS_DIR, logs.LOG_DIR = tmp, tmp / "logs"
        ai_config.load_ai_config = lambda: cfg
        ai_providers.ADAPTERS["fake"] = adapter
        try:
            yield conversations.get_current_id()
        finally:
            (ai_config.load_ai_config, _, conversations.JARVIS_DIR, conversations.CONV_DIR,
             conversations.INDEX_FILE, conversations.CURRENT_FILE, logs.JARVIS_DIR,
             logs.LOG_DIR) = orig
            ai_providers.ADAPTERS.clear()
            ai_providers.ADAPTERS.update(orig[1])


def _reasoning_adapter(resolved, messages, timeout, tools=None, tool_executor=None, **kw):
    """A host that sends its reasoning whether or not it was asked to."""
    ai_providers._emit_stream(ai_providers.EVENT_THINKING, delta="we must not mention tools")
    ai_providers._emit_stream(ai_providers.EVENT_TEXT, delta="hello")
    ai_providers._emit_stream(ai_providers.EVENT_ROUND_END, finish=ai_providers.FINISH_ROUND_DONE)
    return ai_providers.AIResult(True, text="hello")


def _ask(think_override):
    try:
        kh.HEALTH_FILE.unlink()
    except OSError:
        pass
    seen = []
    with _env(_reasoning_adapter) as conv:
        r = ai_client.ask("hi", commands=[], conversation_id=conv, think_override=think_override,
                          on_stream=lambda kind, **d: seen.append(kind))
    return r, seen


r, seen = _ask("off")
check("thinking off: the reply still streams", ai_providers.EVENT_TEXT in seen, seen)
check("thinking off: the host's unrequested reasoning is NOT forwarded",
      ai_providers.EVENT_THINKING not in seen, seen)

r, seen = _ask("medium")
check("thinking medium: reasoning IS forwarded", ai_providers.EVENT_THINKING in seen, seen)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name, detail in FAIL:
        print(f"  - {name}: {detail}")
    sys.exit(1)
