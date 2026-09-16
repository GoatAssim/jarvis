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
        entry = {"input_tokens": inp or 0, "output_tokens": out or 0, "source": "reported"}
        # OpenAI-family hosts cache prefixes AUTOMATICALLY above ~1024
        # tokens — there's no request-side field to set, which is why
        # prompt_cache.SUPPORTED_TYPES deliberately excludes this provider
        # type. It's already happening; it's just invisible unless the
        # cached-token count is read back out. Reported under the same key
        # the anthropic branch uses so anything downstream only learns one
        # name for it. Nested under prompt_tokens_details on hosts that
        # implement it; absent entirely on the many that don't, which is
        # handled as "no cache info" rather than "zero cache hits".
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached = _as_int(details.get("cached_tokens"))
            if cached:
                entry["cache_read_tokens"] = cached
                entry["cache_write_tokens"] = 0
        return entry

    if provider_type == "anthropic":
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return None
        inp = _as_int(usage.get("input_tokens"))
        out = _as_int(usage.get("output_tokens"))
        if inp is None and out is None:
            return None
        # Anthropic breaks out cache_creation/cache_read input tokens.
        # input_tokens stays the grand total (all three still occupy the
        # context window) so every existing caller — get_usage_summary, the
        # debug panel, the Logs viewer — reads the same number it always
        # did. The two components are reported ALONGSIDE it rather than
        # folded away, because prompt_cache.py's whole failure mode is
        # silent: a misplaced breakpoint doesn't error, it just quietly
        # never hits. Without cache_read_tokens surfacing somewhere, the
        # only symptom of a cache that never hits is a bill nobody reads.
        cache_c = _as_int(usage.get("cache_creation_input_tokens")) or 0
        cache_r = _as_int(usage.get("cache_read_input_tokens")) or 0
        entry = {
            "input_tokens": (inp or 0) + cache_c + cache_r,
            "output_tokens": out or 0,
            "source": "reported",
        }
        if cache_c or cache_r:
            entry["cache_write_tokens"] = cache_c
            entry["cache_read_tokens"] = cache_r
        return entry

    if provider_type == "gemini":
        meta = data.get("usageMetadata")
        if not isinstance(meta, dict):
            return None
        inp = _as_int(meta.get("promptTokenCount"))
        out = _as_int(meta.get("candidatesTokenCount"))
        if inp is None and out is None:
            return None
        entry = {"input_tokens": inp or 0, "output_tokens": out or 0, "source": "reported"}
        # cachedContentTokenCount covers BOTH of Gemini's caching mechanisms
        # — the free implicit cache and the explicit cachedContents API —
        # so this one field is how prompt_cache.py's Gemini strategy is
        # verified either way. It's already counted inside promptTokenCount;
        # this reports it as a component, not an addition. Gemini has no
        # separate write counter, so cache_write_tokens is not reported here
        # rather than being faked as 0.
        cached = _as_int(meta.get("cachedContentTokenCount"))
        if cached:
            entry["cache_read_tokens"] = cached
        return entry

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
        #
        # prompt_eval_count is NOT a cache-hit signal, despite looking like
        # one: it reports the size of the prompt that was sent, not the
        # tokens actually recomputed, so it stays flat whether the KV prefix
        # was reused or rebuilt from scratch. prompt_eval_duration is the
        # field that actually collapses on a hit, which is why it — and not
        # a bogus cache_read_tokens derived from the count — is what gets
        # surfaced for the keep_alive tuning in prompt_cache.py.
        inp = _as_int(data.get("prompt_eval_count"))
        out = _as_int(data.get("eval_count"))
        if inp is None and out is None:
            return None
        entry = {"input_tokens": inp or 0, "output_tokens": out or 0, "source": "reported"}
        prefill_ns = _as_int(data.get("prompt_eval_duration"))
        if prefill_ns:
            entry["prefill_ms"] = prefill_ns // 1_000_000
        return entry

    return None