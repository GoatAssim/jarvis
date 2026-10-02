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
        # L.24 T6: reasoning models report how much of completion_tokens was
        # hidden thinking. It is a COMPONENT of completion_tokens (already in
        # output_tokens), so it is flagged and never added a second time.
        cdetails = usage.get("completion_tokens_details")
        if isinstance(cdetails, dict):
            reasoning = _as_int(cdetails.get("reasoning_tokens"))
            if reasoning:
                entry["thinking_tokens"] = reasoning
                entry["thinking_in_output"] = True
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
        # L.24 T6: Gemini bills thinking SEPARATELY from candidatesTokenCount
        # (totalTokenCount = promptTokenCount + candidatesTokenCount +
        # thoughtsTokenCount - checked on both real logs in tests/fixtures).
        # It used to be dropped here, so every Gemini total understated the
        # spend. Additive: it is NOT inside output_tokens.
        thoughts = _as_int(meta.get("thoughtsTokenCount"))
        if thoughts:
            entry["thinking_tokens"] = thoughts
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


# ---------------------------------------------------------------------------
# L.24 T6: totals that count everything that was billed.
# ---------------------------------------------------------------------------

def extra_thinking(entry):
    """Thinking tokens billed IN ADDITION to entry["output_tokens"].

    Gemini reports thoughtsTokenCount outside candidatesTokenCount, so it
    adds to the bill. OpenAI-style reasoning_tokens are already inside
    completion_tokens (the entry carries thinking_in_output) and add nothing.
    """
    if not isinstance(entry, dict) or entry.get("thinking_in_output"):
        return 0
    return _as_int(entry.get("thinking_tokens")) or 0


def entry_total(entry):
    """input + output + any thinking not already counted in output."""
    if not isinstance(entry, dict):
        return 0
    return ((_as_int(entry.get("input_tokens")) or 0)
            + (_as_int(entry.get("output_tokens")) or 0)
            + extra_thinking(entry))


class AskUsage:
    """Everything one ask() spent, across EVERY provider attempt.

    ai_providers.get_usage_summary() describes one attempt only: the
    thread-local round list is reset by set_log_context() at the start of each
    attempt (and again for a same-key 429 retry), and a failed attempt's
    AIResult carries no usage at all. So after a failover the figure shown for
    the turn was the winning attempt alone. Measured on the two real logs in
    tests/fixtures: 3,466 of 49,276 logged tokens (7%) for 81b52bb561796954 and
    1,514 of 35,444 (4%) for cb140e091ddc66e8, before thinking tokens.
    ask() calls add() after every adapter call, so nothing is lost.
    """

    def __init__(self):
        self._attempts = []

    def add(self, label, summary, ok=False):
        """Record one attempt. `summary` is ai_providers.get_usage_summary()'s
        dict (or None). An attempt that made no request is not recorded."""
        if not isinstance(summary, dict):
            return
        rounds = [r for r in (summary.get("rounds") or []) if isinstance(r, dict)]
        if not rounds:
            return
        self._attempts.append({
            "label": str(label or ""),
            "ok": bool(ok),
            "rounds": len(rounds),
            "input_tokens": sum(_as_int(r.get("input_tokens")) or 0 for r in rounds),
            "output_tokens": sum(_as_int(r.get("output_tokens")) or 0 for r in rounds),
            # Billed ON TOP of output_tokens (Gemini). Reasoning that is
            # already inside output_tokens is kept apart so no reader adds
            # it twice.
            "thinking_tokens": sum(extra_thinking(r) for r in rounds),
            "thinking_in_output_tokens": sum(
                (_as_int(r.get("thinking_tokens")) or 0) for r in rounds if r.get("thinking_in_output")),
            "total_tokens": sum(entry_total(r) for r in rounds),
            "cache_read_tokens": sum(_as_int(r.get("cache_read_tokens")) or 0 for r in rounds),
        })

    def __bool__(self):
        return bool(self._attempts)

    def to_dict(self):
        a = self._attempts
        out = {
            "input_tokens": sum(x["input_tokens"] for x in a),
            "output_tokens": sum(x["output_tokens"] for x in a),
            "thinking_tokens": sum(x["thinking_tokens"] for x in a),
            "thinking_in_output_tokens": sum(x["thinking_in_output_tokens"] for x in a),
            "total_tokens": sum(x["total_tokens"] for x in a),
            "rounds": sum(x["rounds"] for x in a),
            "attempt_count": len(a),
            "attempts": [dict(x) for x in a],
        }
        cache = sum(x["cache_read_tokens"] for x in a)
        if cache:
            out["cache_read_tokens"] = cache
        return out


def format_ask_total(ask_total):
    """One human-readable line for the per-ask total, or "" if there is none."""
    if not isinstance(ask_total, dict) or not ask_total.get("attempt_count"):
        return ""
    bits = ["in=%d" % (ask_total.get("input_tokens") or 0),
            "out=%d" % (ask_total.get("output_tokens") or 0)]
    if ask_total.get("thinking_tokens"):
        bits.append("thinking=%d" % ask_total["thinking_tokens"])
    if ask_total.get("thinking_in_output_tokens"):
        bits.append("reasoning-inside-out=%d" % ask_total["thinking_in_output_tokens"])
    n = ask_total["attempt_count"]
    return "ask total=%d (%s) across %d attempt%s, %d round%s" % (
        ask_total.get("total_tokens") or 0, " ".join(bits), n, "" if n == 1 else "s",
        ask_total.get("rounds") or 0, "" if ask_total.get("rounds") == 1 else "s")

