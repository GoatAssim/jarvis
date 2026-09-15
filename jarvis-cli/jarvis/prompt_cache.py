"""Prompt caching policy for every provider — lever 3 of
skills-and-token-optimization-research.md.

The other two levers (progressive disclosure, lazy tool loading) cut what
gets *sent*. This one cuts what gets *re-billed* when the same bytes are
sent again: providers keep a server-side cache of prompt prefixes, so a
request sharing a prefix with a recent one is charged a fraction on the
shared part.

All the policy lives in this ONE module rather than inlined into
ai_providers.py, because the failure mode is silent. A misplaced breakpoint
doesn't error — it just quietly never hits, and costs a write premium
forever. One file means one place to read, test, and fix.

===========================================================================
Why this pays off in jarvis specifically
===========================================================================

jarvis is a brand-new OS process on every `jarvis ...` call (see history.py's
module docstring), so there is no in-process session for a cache to live in.
That does NOT make caching useless here — every cache below is *server-side*
and keyed on prompt bytes, not on a connection or a session id. A prefix
written by one jarvis process is still warm for the next one, as long as it
lands within the TTL and the bytes match.

Three places repeat identical prefixes:

  1. Tool rounds inside one ask() — up to MAX_TOOL_ROUNDS requests sharing
     the whole tools+system prefix, differing only in the growing tail of
     tool_use/tool_result turns.
  2. Provider/key failover inside one ask() — ask() retries the *same*
     messages against the next key. Every retry re-bills the full prefix.
  3. Consecutive turns in a conversation, within the TTL.

===========================================================================
The one rule that is universal
===========================================================================

Every provider below caches a *prefix*. Not a set of blocks, not a fuzzy
match — a literal byte-prefix of the rendered request. Anything that changes
early in the request invalidates everything after it.

That single fact is why ai_client._system_prompt_parts() splits the system
prompt into a static prefix and a per-request tail, and why every adapter
puts static content first. Without that split, jarvis's own memory context
(which is keyed on the user's message, so it changes every turn) sits near
the front of the system prompt and destroys the cache for all four providers
at once. The split is the load-bearing change; the per-provider markers
below are what let each one take advantage of it.

===========================================================================
Per-provider notes — what each one actually needs from us
===========================================================================

ANTHROPIC — explicit, opt-in markers.
    Mark a block with cache_control: {"type": "ephemeral"}. Prefixes are
    built in the order tools -> system -> messages, so a breakpoint on a
    system block caches the tool definitions AND the system prompt together.
    That cumulative behavior is what makes this work at all here: jarvis's
    static system prefix is only ~160-830 estimated tokens, well under every
    model's cacheable floor, and only clears the floor because the tool
    schemas (~2.4k-10.6k tokens) ride in front of it in the same prefix.
    Hence: mark system, size-check tools+system.
    Pricing: write 1.25x base input (5m ttl) or 2x (1h); read 0.10x. A write
    that is never read is a ~25% loss, which is why CACHE_TOOLS defaults off
    (the tools array is the part most likely to differ between two jarvis
    calls — a different router group means a different tool list).

GEMINI — two mechanisms, and the free one is better here.
    *Implicit* caching is on by default for 2.5+ models, costs nothing, needs
    no extra request, and just requires a stable prefix. *Explicit* caching
    (the cachedContents API) guarantees the discount but costs a separate
    POST before the first generateContent, plus storage billing for the TTL.
    jarvis previously used explicit caching exclusively, created per-ask and
    DELETED at the end of the same ask — so every fresh jarvis process paid
    a cache-creation round trip and storage for a cache it read at most a
    few times and then threw away. For single-round asks (the common case)
    that is strictly worse than sending inline. Default is now implicit;
    explicit is opt-in and, when on, persists its cache name across
    processes (see gemini_cache_store.py) so the write is actually amortized.

OPENAI-FAMILY (openai, groq, xai, mistral, deepseek, openrouter) —
    automatic, nothing to opt into. Caching kicks in above a host-specific
    floor (~1024 tokens typically; Groq caches automatically on supported
    models with no code changes and no extra fee). The only lever is
    prompt_cache_key / its vendor aliases: a stable per-conversation string
    that routes the request to the same backend node and raises the hit
    rate. It is a hint, never a guarantee. Cheap to send, so it's on by
    default; harmless on hosts that ignore unknown fields, and suppressible
    per provider for the rare host that 400s on them.

OLLAMA — local, and the lever is keep_alive, not a cache field.
    Ollama reuses a KV prefix automatically when the prompt prefix matches,
    but it dumps that cache when it unloads the model, which it does after
    5 minutes idle by default. For jarvis — a fresh process per call, often
    with minutes between calls — that default is exactly wrong: the model
    unloads between asks and every ask pays a cold prefill. Raising
    keep_alive is the whole optimization.
    Note for anyone adding metrics: prompt_eval_count is NOT a cache signal
    on Ollama. It reports the size of the prompt you sent, not the tokens
    actually recomputed, so it stays flat across a hit and a miss alike.
    prompt_eval_duration is the field that moves.
"""

# Provider types (ai_providers.ADAPTERS keys) with explicit, request-side
# cache markers we must opt into. Everything else either caches
# automatically (openai_compatible) or is tuned some other way (ollama).
EXPLICIT_MARKER_TYPES = frozenset({"anthropic"})

DEFAULT_TTL = "5m"
VALID_TTLS = ("5m", "1h")

# --- Anthropic -------------------------------------------------------------
#
# Minimum cacheable prompt length in tokens, by model family. Content below
# the floor is processed normally and never cached, even with an explicit
# breakpoint on it. Matched as a lowercased substring against the configured
# model string, LONGEST pattern first, so "claude-haiku-4-5-20251001"
# matches "haiku-4-5" (4096) and not bare "haiku" (2048).
#
# Verified against Anthropic's prompt-caching docs (cache limitations),
# Sept 2026. If a floor changes or a family ships, this dict is the only
# thing that needs editing.
ANTHROPIC_MIN_TOKENS = {
    "opus-4-5": 4096,
    "opus-4-6": 4096,
    "opus-4-7": 4096,
    "opus-4-8": 4096,
    "haiku-4-5": 4096,
    "sonnet-4-6": 2048,
    "haiku-3-5": 2048,
    "haiku-3": 2048,
    "sonnet-4-5": 1024,
    "sonnet-4": 1024,
    "sonnet-3-7": 1024,
    "opus-4-1": 1024,
    "opus-4": 1024,
}

# --- Gemini ----------------------------------------------------------------
#
# Minimum input tokens before caching (implicit or explicit) engages.
# Google's own docs have published several different tables for these as
# models shipped, so these are the conservative end of what's documented —
# erring high only means caching stays off for content that might have
# qualified, which is never a correctness problem.
GEMINI_MIN_TOKENS = {
    "gemini-3": 4096,
    "2.5-pro": 4096,
    "2.5-flash-lite": 1024,
    "2.5-flash": 1024,
    "flash-latest": 1024,
}

# Used when the configured model matches no pattern — an unknown model
# string, or a proxy/gateway that renames models. The most conservative real
# floor, so an unknown model errs toward "don't bother marking it" rather
# than toward a write that can never be read.
FALLBACK_MIN_TOKENS = 4096

# Ollama unloads an idle model after 5 minutes by default, dumping its KV
# cache with it. jarvis's usage pattern is bursty with long gaps, so the
# default guarantees a cold prefill on most asks. 30 minutes costs only VRAM
# residency on the user's own machine, which is the resource they'd rather
# spend than wait.
DEFAULT_OLLAMA_KEEP_ALIVE = "30m"

# Anthropic allows at most 4 cache breakpoints per request. jarvis uses at
# most 2 (tools, system) — this is a guard against future edits, not a limit
# anything here is near.
MAX_BREAKPOINTS = 4


def _normalized_model(model):
    return (model or "").lower().replace(".", "-")


def _floor_from(table, model):
    """Longest-substring match of `model` against a {pattern: floor} table."""
    normalized = _normalized_model(model)
    best = None
    for pattern, floor in table.items():
        key = pattern.lower().replace(".", "-")
        if key in normalized and (best is None or len(key) > len(best[0])):
            best = (key, floor)
    return best[1] if best else FALLBACK_MIN_TOKENS


def min_cacheable_tokens(provider_type, model):
    """Token floor below which `model` won't cache a marked prefix."""
    if provider_type == "gemini":
        return _floor_from(GEMINI_MIN_TOKENS, model)
    if provider_type == "anthropic":
        return _floor_from(ANTHROPIC_MIN_TOKENS, model)
    # openai-family hosts document ~1024 as the point automatic caching
    # engages; ollama has no floor (it's a local KV prefix, not a billed
    # cache).
    return 1024


def cache_control(ttl=DEFAULT_TTL):
    """The Anthropic marker dict. "ephemeral" is the only supported type.

    ttl is omitted when it's the 5m default, so the rendered bytes match
    what a caller that never passed a ttl would produce — a gratuitous
    "ttl": "5m" would still be a valid request, it would just make two call
    sites that meant the same thing hash differently, which is the one thing
    prefix caching cannot tolerate.
    """
    marker = {"type": "ephemeral"}
    if ttl and ttl != DEFAULT_TTL:
        marker["ttl"] = ttl
    return marker


def resolve_settings(provider, defaults=None):
    """Merge the global defaults block with this provider's own overrides.

    Provider-level keys win, so one expensive or flaky provider can turn
    caching off without affecting the rest. Keys (all optional):

        prompt_cache            master switch (default True)
        prompt_cache_ttl        "5m" or "1h"; anything else falls back to 5m
        prompt_cache_tools      Anthropic: second breakpoint on the tools
                                array (default False — see module docstring
                                on why a write never read is a 25% loss)
        prompt_cache_min_tokens override the model-derived floor
        prompt_cache_key        openai-family: send a routing hint (default
                                True)
        gemini_explicit_cache   Gemini: use the cachedContents API instead of
                                relying on free implicit caching (default
                                False)
        ollama_keep_alive       Ollama: how long to keep the model resident
    """
    defaults = defaults or {}
    provider = provider or {}

    def pick(key, fallback):
        if key in provider:
            return provider[key]
        return defaults.get(key, fallback)

    ttl = pick("prompt_cache_ttl", DEFAULT_TTL)
    if ttl not in VALID_TTLS:
        ttl = DEFAULT_TTL

    min_tokens = pick("prompt_cache_min_tokens", None)
    try:
        min_tokens = int(min_tokens) if min_tokens is not None else None
    except (TypeError, ValueError):
        min_tokens = None

    keep_alive = pick("ollama_keep_alive", DEFAULT_OLLAMA_KEEP_ALIVE)
    if not isinstance(keep_alive, (str, int)) or keep_alive == "":
        keep_alive = DEFAULT_OLLAMA_KEEP_ALIVE

    return {
        "enabled": bool(pick("prompt_cache", True)),
        "ttl": ttl,
        "cache_tools": bool(pick("prompt_cache_tools", False)),
        "min_tokens": min_tokens,
        "send_cache_key": bool(pick("prompt_cache_key", True)),
        "gemini_explicit": bool(pick("gemini_explicit_cache", False)),
        "ollama_keep_alive": keep_alive,
    }


def _estimate(obj):
    # Imported lazily so this module stays importable in isolation — the
    # tests exercise the policy without pulling in the rest of jarvis.
    from . import token_usage

    return token_usage.estimate_tokens_for(obj)


def prefix_tokens(system_parts, tools_payload):
    """Estimated size of the cacheable prefix: tools + the STATIC system
    block only.

    Counting the whole system prompt would overstate it — parts[1:] is the
    per-request tail that sits after the breakpoint and is never cached.
    Counting the system block alone would understate it badly enough to
    reject every real jarvis prompt, since tools are 10-40x its size.
    """
    parts = [p for p in (system_parts or []) if p]
    static = parts[0] if parts else ""
    return _estimate(tools_payload or []) + _estimate(static)


def plan(system_parts, tools_payload, provider, defaults=None, model=None):
    """Decide the caching strategy for one request, for any provider type.

    system_parts is the ordered list of system strings as
    ai_client._build_messages emitted them: index 0 is the static prefix,
    anything after it is the per-request tail. tools_payload is the
    provider-shaped tools array (or None).

    Returns a dict every adapter can read, using only the keys it cares
    about:

        system_index  anthropic: index into system_parts to mark, or None
        tools         anthropic: True to mark the last tool
        ttl           anthropic: ttl string for both markers
        cache_key     openai-family: True to send a routing hint
        explicit      gemini: True to use the cachedContents API
        keep_alive    ollama: model residency string
        eligible      False when the prefix is too small to cache anywhere
        reason        short human-readable string, surfaced in logs so a
                      cache that never hits is diagnosable without a debugger

    Never raises: a bad config or an odd payload degrades to "no caching",
    which is exactly the pre-caching behavior.
    """
    settings = resolve_settings(provider, defaults)
    provider_type = (provider or {}).get("type")
    model = model or (provider or {}).get("model") or ""

    out = {
        "system_index": None,
        "tools": False,
        "ttl": settings["ttl"],
        "cache_key": False,
        "explicit": False,
        "keep_alive": settings["ollama_keep_alive"],
        "eligible": False,
        "reason": "",
    }

    if not settings["enabled"]:
        out["reason"] = "disabled by config"
        return out

    floor = settings["min_tokens"]
    if floor is None:
        floor = min_cacheable_tokens(provider_type, model)
    size = prefix_tokens(system_parts, tools_payload)

    # Ollama's keep_alive isn't a billed cache and has no token floor — it's
    # about whether the model is still resident on the user's own GPU. It
    # applies regardless of prompt size, so it's decided before the floor
    # check below.
    if provider_type == "ollama":
        out["eligible"] = True
        out["reason"] = f"keep_alive={settings['ollama_keep_alive']} (local KV prefix reuse)"
        return out

    if size < floor:
        out["reason"] = f"prefix ~{size} tok < {floor} tok floor for {model or 'unknown model'}"
        return out
    out["eligible"] = True

    if provider_type == "openai_compatible":
        # Automatic above the host's floor — nothing to opt into. The one
        # lever is the routing hint, which is a hint and not a guarantee.
        out["cache_key"] = settings["send_cache_key"]
        out["reason"] = f"automatic caching; prefix ~{size} tok"
        return out

    if provider_type == "gemini":
        out["explicit"] = settings["gemini_explicit"]
        out["reason"] = (
            f"explicit cachedContents; prefix ~{size} tok" if out["explicit"]
            else f"implicit caching (free, no extra request); prefix ~{size} tok"
        )
        return out

    if provider_type in EXPLICIT_MARKER_TYPES:
        parts = [p for p in (system_parts or []) if p]
        if not parts:
            out["eligible"] = False
            out["reason"] = "no system prompt to mark"
            return out
        # Mark the FIRST system block, never the last. parts[0] is the
        # static prefix; parts[1:] hold query-dependent text (memory context
        # keyed on the user's message, saved-commands listing, frequency
        # stats) that changes between turns. A breakpoint after the tail
        # would hash the tail in and miss every single time.
        out["system_index"] = 0
        out["tools"] = bool(settings["cache_tools"]) and bool(tools_payload)
        out["reason"] = f"prefix ~{size} tok >= {floor} tok floor"
        return out

    out["eligible"] = False
    out["reason"] = f"no caching strategy for provider type {provider_type!r}"
    return out


def apply_to_system(system_parts, breakpoint_index, ttl=DEFAULT_TTL):
    """Render system_parts as Anthropic content blocks, marking one.

    Returns [{"type": "text", "text": ...}, ...] with cache_control on the
    block at breakpoint_index; pass None to get the same blocks unmarked.

    The caller can always fall back to "\\n\\n".join(system_parts) for a
    provider wanting a plain string — both forms carry identical text, which
    is what lets _merge_system() in ai_providers.py hand non-Anthropic
    providers byte-identical prompts to what they got before this existed.
    """
    parts = [p for p in (system_parts or []) if p]
    blocks = [{"type": "text", "text": p} for p in parts]
    if breakpoint_index is not None and 0 <= breakpoint_index < len(blocks):
        blocks[breakpoint_index]["cache_control"] = cache_control(ttl)
    return blocks


def apply_to_tools(tools_payload, ttl=DEFAULT_TTL):
    """Mark the last tool in an Anthropic-shaped tools array.

    Returns a NEW list with a shallow-copied final entry. Mutating the
    caller's schema dicts in place would leak a cache_control key back into
    tools.TOOL_SCHEMAS — a process-wide singleton — and from there into
    every other provider's payload on the next failover.
    """
    if not tools_payload:
        return tools_payload
    out = list(tools_payload)
    last = dict(out[-1])
    last["cache_control"] = cache_control(ttl)
    out[-1] = last
    return out


def ttl_seconds(ttl):
    """"5m"/"1h" -> seconds. Used for Gemini, whose cachedContents API wants
    a duration string in seconds ("300s") rather than Anthropic's enum."""
    return 3600 if ttl == "1h" else 300
