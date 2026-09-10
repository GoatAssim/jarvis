"""Phase 0 (see new_plan.md): token measurement helpers.

Two jobs, kept in one small module since both exist purely to answer
"how many tokens did that cost":

  extract_usage(provider_type, data) — pull the REAL, provider-reported
  prompt/completion token counts out of one raw API response body. Every
  provider names its fields differently; this is the one place that knows
  all of them. Returns None when a response carries no usage block at all
  (network/parse errors never reach here — see ai_providers._record_usage).

  estimate_tokens_for(obj) — a cheap, provider-agnostic ~estimate for
  things no provider ever reports token counts for on its own: a single
  tool call's arguments, or a single tool call's result. ~4 chars/token is
  the standard rough-and-ready heuristic for English text and JSON; it's
  an estimate, not a real count, so every caller that surfaces this
  labels it "estimated" (see ai_providers._call_tool_safely).
"""

import json

CHARS_PER_TOKEN = 4  # rough heuristic; good enough for relative comparisons


def _text_len_for(obj):
    if obj is None:
        return 0
    if isinstance(obj, str):
        return len(obj)
    try:
        return len(json.dumps(obj, default=str, ensure_ascii=False))
    except (TypeError, ValueError):
        return len(str(obj))


def estimate_tokens_for(obj):
    """~Estimated token count for arbitrary text/JSON-able data. Never
    raises — worst case (unserializable object) falls back to str()."""
    chars = _text_len_for(obj)
    if chars <= 0:
        return 0
    return max(1, (chars + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def extract_usage(provider_type, data):
    """Return {"input_tokens", "output_tokens", "source": "reported"} from
    one raw response body `data`, or None if this response didn't carry a
    usage block (e.g. an error body, or a provider that never sends one).
    `provider_type` is the same string used for ai_providers.ADAPTERS keys
    (openai_compatible, anthropic, gemini, cohere, ollama).
    """
    if not isinstance(data, dict):
        return None

    if provider_type == "openai_compatible":
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return None
        inp = _as_int(usage.get("prompt_tokens"))
        out = _as_int(usage.get("completion_tokens"))
        if inp is None and out is None:
            return None
        return {"input_tokens": inp or 0, "output_tokens": out or 0, "source": "reported"}

    if provider_type == "anthropic":
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return None
        inp = _as_int(usage.get("input_tokens"))
        out = _as_int(usage.get("output_tokens"))
        if inp is None and out is None:
            return None
        # Anthropic also breaks out cache_creation/cache_read input tokens;
        # fold those into "input" since they still count against context.
        cache_c = _as_int(usage.get("cache_creation_input_tokens")) or 0
        cache_r = _as_int(usage.get("cache_read_input_tokens")) or 0
        return {"input_tokens": (inp or 0) + cache_c + cache_r, "output_tokens": out or 0, "source": "reported"}

    if provider_type == "gemini":
        meta = data.get("usageMetadata")
        if not isinstance(meta, dict):
            return None
        inp = _as_int(meta.get("promptTokenCount"))
        out = _as_int(meta.get("candidatesTokenCount"))
        if inp is None and out is None:
            return None
        return {"input_tokens": inp or 0, "output_tokens": out or 0, "source": "reported"}

    if provider_type == "cohere":
        # Cohere v2 chat nests it under meta.billed_units (or meta.tokens on
        # older responses) — check both, oldest-format-last.
        meta = data.get("meta")
        if isinstance(meta, dict):
            billed = meta.get("billed_units")
            if isinstance(billed, dict) and (billed.get("input_tokens") is not None or billed.get("output_tokens") is not None):
                return {
                    "input_tokens": _as_int(billed.get("input_tokens")) or 0,
                    "output_tokens": _as_int(billed.get("output_tokens")) or 0,
                    "source": "reported",
                }
            tokens = meta.get("tokens")
            if isinstance(tokens, dict) and (tokens.get("input_tokens") is not None or tokens.get("output_tokens") is not None):
                return {
                    "input_tokens": _as_int(tokens.get("input_tokens")) or 0,
                    "output_tokens": _as_int(tokens.get("output_tokens")) or 0,
                    "source": "reported",
                }
        return None

    if provider_type == "ollama":
        # Ollama's native /api/chat reports prompt_eval_count / eval_count.
        inp = _as_int(data.get("prompt_eval_count"))
        out = _as_int(data.get("eval_count"))
        if inp is None and out is None:
            return None
        return {"input_tokens": inp or 0, "output_tokens": out or 0, "source": "reported"}

    return None