"""Tests for prompt caching (jarvis/prompt_cache.py) and the system-prompt
split that makes it work (ai_client._system_prompt_parts).

Run: python tests/test_prompt_cache.py   (from jarvis-cli/, like the others)

The property worth protecting here is NOT "a cache_control key appears in the
payload" — it's that the cacheable prefix is genuinely stable across requests.
Prompt caching fails silently: a prefix that never matches still returns a
correct answer, at full price, with no error anywhere. So most of these tests
assert on prefix stability and on byte-identical fallbacks, which is what
actually breaks if someone later reorders the prompt.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis-cli"))

from jarvis import ai_client, prompt_cache  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"{'ok      ' if cond else 'FAILED  '} {name}{'' if cond else ': ' + detail}")


ANTHROPIC = {"type": "anthropic", "model": "claude-haiku-4-5-20251001"}
GEMINI = {"type": "gemini", "model": "gemini-2.5-flash"}
GROQ = {"type": "openai_compatible", "model": "llama-3.3-70b"}
OLLAMA = {"type": "ollama", "model": "qwen3:4b"}

# Sized to the MEASURED real cost of jarvis's compact tool catalog
# (~4,800 estimated tokens for the full session offering). An earlier version
# of this fixture was ~3,573 tokens, which sits under haiku-4-5's 4,096 floor
# — so it silently tested the rejection path twice instead of the acceptance
# path once. See test_ultra_mode_sized_prefix_is_honestly_rejected below for
# why the small case is worth keeping as its own explicit test.
BIG_TOOLS = [
    {"name": f"tool_{i}", "description": "x" * 400, "input_schema": {"type": "object"}}
    for i in range(48)
]
# ~2,400 tokens: what "ultra" (50% Capacity) mode's name-only schemas cost.
SMALL_TOOLS = BIG_TOOLS[:24]
SMALL_PARTS = ["You are a butler.", "MEM: user likes X"]


# --- model floors ----------------------------------------------------------

def test_model_floors():
    check("haiku-4-5 gets the 4096 floor, not bare haiku's 2048",
          prompt_cache.min_cacheable_tokens("anthropic", "claude-haiku-4-5-20251001") == 4096)
    check("sonnet-4-5 gets 1024",
          prompt_cache.min_cacheable_tokens("anthropic", "claude-sonnet-4-5") == 1024)
    check("unknown model falls back to the conservative floor",
          prompt_cache.min_cacheable_tokens("anthropic", "some-proxy-model-name")
          == prompt_cache.FALLBACK_MIN_TOKENS)


# --- the floor is what makes or breaks this ---------------------------------

def test_small_prefix_earns_no_breakpoint():
    """jarvis's system prompt alone is 160-830 tokens against a 1k-4k floor.
    Marking it without the tool schemas in front would be a write that can
    never be read, i.e. a pure 25% loss."""
    plan = prompt_cache.plan(SMALL_PARTS, None, ANTHROPIC)
    check("tiny prefix gets no breakpoint", plan["system_index"] is None, plan["reason"])
    check("and says why", "floor" in plan["reason"], plan["reason"])


def test_tools_lift_the_prefix_over_the_floor():
    """The cumulative tools -> system -> messages ordering is the only reason
    caching is viable here at all."""
    plan = prompt_cache.plan(SMALL_PARTS, BIG_TOOLS, ANTHROPIC)
    check("tools+system clears the floor", plan["system_index"] == 0, plan["reason"])


def test_ultra_mode_sized_prefix_is_honestly_rejected():
    """A real finding, pinned so it can't regress into a silent cost.

    haiku-4-5's floor is 4,096 tokens. jarvis's compact catalog (~4,800 tok)
    clears it; ultra mode's name-only schemas (~2,400 tok) do not. So on the
    DEFAULT model, ultra mode cannot use prompt caching at all — and the
    right behavior is to place no breakpoint and say so, rather than pay a
    1.25x write premium on a prefix that can never be read back."""
    plan = prompt_cache.plan(SMALL_PARTS, SMALL_TOOLS, ANTHROPIC)
    check("ultra-sized prefix gets no breakpoint on haiku-4-5",
          plan["system_index"] is None, plan["reason"])
    check("a 1024-floor model caches the same prefix fine",
          prompt_cache.plan(SMALL_PARTS, SMALL_TOOLS,
                            {"type": "anthropic", "model": "claude-sonnet-4-5"}
                            )["system_index"] == 0)


def test_breakpoint_is_on_the_static_block():
    plan = prompt_cache.plan(SMALL_PARTS, BIG_TOOLS, ANTHROPIC)
    blocks = prompt_cache.apply_to_system(SMALL_PARTS, plan["system_index"], plan["ttl"])
    check("breakpoint marks the static block", "cache_control" in blocks[0])
    check("per-request tail is NOT inside the cached prefix",
          "cache_control" not in blocks[1])


def test_tools_breakpoint_is_opt_in():
    on = prompt_cache.plan(SMALL_PARTS, BIG_TOOLS, ANTHROPIC, {"prompt_cache_tools": True})
    off = prompt_cache.plan(SMALL_PARTS, BIG_TOOLS, ANTHROPIC)
    check("tools breakpoint off by default", off["tools"] is False)
    check("tools breakpoint opt-in works", on["tools"] is True)


def test_apply_to_tools_does_not_mutate_the_catalog():
    """tools.TOOL_SCHEMAS is a process-wide singleton. A cache_control key
    leaking into it would end up in every OTHER provider's payload on the
    next failover."""
    original = [{"name": "a", "description": "d", "input_schema": {}}]
    marked = prompt_cache.apply_to_tools(original, "5m")
    check("marked copy has the key", "cache_control" in marked[-1])
    check("original dict untouched", "cache_control" not in original[-1])


# --- per-provider strategies ------------------------------------------------

def test_provider_strategies():
    g = prompt_cache.plan(SMALL_PARTS, BIG_TOOLS, GEMINI)
    check("gemini defaults to free implicit caching", g["explicit"] is False, g["reason"])
    g2 = prompt_cache.plan(SMALL_PARTS, BIG_TOOLS, GEMINI, {"gemini_explicit_cache": True})
    check("gemini explicit is opt-in", g2["explicit"] is True)

    q = prompt_cache.plan(SMALL_PARTS, BIG_TOOLS, GROQ)
    check("openai-family gets a routing hint, no marker",
          q["cache_key"] is True and q["system_index"] is None, q["reason"])
    q2 = prompt_cache.plan(SMALL_PARTS, BIG_TOOLS, GROQ, {"prompt_cache_key": False})
    check("routing hint is suppressible", q2["cache_key"] is False)

    o = prompt_cache.plan(SMALL_PARTS, None, OLLAMA)
    check("ollama is eligible regardless of prompt size (keep_alive isn't billed)",
          o["eligible"] is True and o["keep_alive"] == "30m", o["reason"])


def test_groq_never_gets_prompt_cache_key_by_default():
    # Groq's endpoint 400s outright on prompt_cache_key ("property
    # 'prompt_cache_key' is unsupported") — unlike the rest of the
    # openai_compatible family, which silently ignores it. Matched by
    # provider *name*, since "type" is shared with every other host in
    # this family (see prompt_cache.NO_CACHE_KEY_PROVIDER_NAMES).
    real_groq = {"type": "openai_compatible", "name": "groq", "model": "llama-3.3-70b"}
    r = prompt_cache.plan(SMALL_PARTS, BIG_TOOLS, real_groq)
    check("groq gets no routing hint by default", r["cache_key"] is False, r)
    check("groq is still otherwise eligible for (automatic) caching", r["eligible"] is True, r)

    r2 = prompt_cache.plan(SMALL_PARTS, BIG_TOOLS, real_groq, {"prompt_cache_key": True})
    check("an explicit prompt_cache_key: true still overrides the groq default",
          r2["cache_key"] is True, r2)

    other = {"type": "openai_compatible", "name": "xai", "model": "grok-4"}
    r3 = prompt_cache.plan(SMALL_PARTS, BIG_TOOLS, other)
    check("a different openai_compatible host (by name) is unaffected",
          r3["cache_key"] is True, r3)


def test_disabled_and_unknown_are_safe():
    off = prompt_cache.plan(SMALL_PARTS, BIG_TOOLS, ANTHROPIC, {"prompt_cache": False})
    check("master switch off = no caching", off["system_index"] is None and not off["eligible"])
    weird = prompt_cache.plan(SMALL_PARTS, BIG_TOOLS, {"type": "cohere", "model": "x"})
    check("unknown provider type degrades to no caching", weird["eligible"] is False)
    check("empty everything doesn't raise",
          prompt_cache.plan([], None, {}, None)["eligible"] is False)


# --- the load-bearing split -------------------------------------------------

PERSONA = {"assistant_name": "J", "address_user_as": "sir",
           "attitude": "dry", "extra_instructions": "Be terse."}
KW = dict(compact_tools=True, compact_persona=True, has_history=True,
          memory_ctx="MEM: query-specific", other_convos_ctx="OTHER",
          pack_instructions_ctx="PACK")


def test_split_is_byte_identical_to_the_old_single_string():
    """Non-Anthropic providers get these halves re-joined by
    ai_providers._merge_system. If the join ever stops reproducing the
    original text, every provider's prompt silently changes."""
    joined = ai_client._system_prompt(PERSONA, "CMDS", "FREQ", True, **KW)
    static, dynamic = ai_client._system_prompt_parts(PERSONA, "CMDS", "FREQ", True, **KW)
    check("rejoined halves == old single string",
          "\n\n".join(p for p in (static, dynamic) if p) == joined)


def test_static_half_is_stable_across_turns():
    """The whole point. memory_ctx is keyed on the user's message, so it
    differs every turn; if it were in the static half the prefix would never
    match and no cache would ever hit."""
    a = ai_client._system_prompt_parts(PERSONA, "CMDS", "FREQ", True,
                                       **{**KW, "memory_ctx": "MEM: turn one"})[0]
    b = ai_client._system_prompt_parts(PERSONA, "CMDS", "FREQ", True,
                                       **{**KW, "memory_ctx": "MEM: totally different"})[0]
    check("static prefix identical across differing memory context", a == b)
    check("query-specific text is not in the static prefix", "turn one" not in a)


def test_skills_catalog_rides_in_the_cached_prefix():
    static, dynamic = ai_client._system_prompt_parts(
        PERSONA, "CMDS", "FREQ", True, skills_ctx="SKILLCATALOG", **KW)
    check("skills catalog is in the static (cached) half", "SKILLCATALOG" in static)
    check("and not in the per-request tail", "SKILLCATALOG" not in dynamic)


def test_pack_instructions_does_not_invalidate_the_static_cache():
    """Regression test for a real bug: pack_instructions_ctx is built from
    route.groups, which differs turn to turn as the router matches
    different things. It used to sit in the STATIC half, so a conversation
    whose messages route to different groups invalidated the entire cached
    block on every such turn — a marked block caches as a whole; any byte
    difference inside it is a miss for the whole block, not a partial hit."""
    static_a, dynamic_a = ai_client._system_prompt_parts(
        PERSONA, "CMDS", "FREQ", True, **{**KW, "pack_instructions_ctx": "GROUP-A-INSTRUCTIONS"})
    static_b, dynamic_b = ai_client._system_prompt_parts(
        PERSONA, "CMDS", "FREQ", True, **{**KW, "pack_instructions_ctx": "GROUP-B-INSTRUCTIONS"})
    check("static half is IDENTICAL regardless of which group routed this turn",
          static_a == static_b)
    check("pack instructions correctly live in the dynamic tail instead",
          "GROUP-A-INSTRUCTIONS" in dynamic_a and "GROUP-A-INSTRUCTIONS" not in static_a)


def test_loaded_skills_ctx_is_dynamic_not_static():
    """A manually-loaded skill (see skill_stickiness.py) is stable within
    one conversation but not identical across different ones, so it belongs
    in the per-request tail alongside memory_ctx, not the globally-static
    prefix that's meant to be identical for everyone on this capacity mode."""
    static, dynamic = ai_client._system_prompt_parts(
        PERSONA, "CMDS", "FREQ", True, loaded_skills_ctx="FORCED-SKILL-BODY", **KW)
    check("loaded-skill content is in the dynamic tail", "FORCED-SKILL-BODY" in dynamic)
    check("and not in the static prefix", "FORCED-SKILL-BODY" not in static)


def test_build_messages_emits_two_system_messages():
    msgs = ai_client._build_messages(
        PERSONA, {}, "hello there", True,
        ai_client._MODE_BY_NAME["compact"], None, route=None)
    systems = [m for m in msgs if m["role"] == "system"]
    check("two system messages emitted", len(systems) == 2, f"got {len(systems)}")
    check("last message is still the user turn", msgs[-1]["role"] == "user")


def test_merge_system_restores_a_single_block():
    from jarvis import ai_providers
    msgs = [{"role": "system", "content": "STATIC"},
            {"role": "system", "content": "TAIL"},
            {"role": "user", "content": "hi"}]
    merged = ai_providers._merge_system(msgs)
    check("merged back to one system message",
          len([m for m in merged if m["role"] == "system"]) == 1)
    check("joined with the same separator the prompt uses",
          merged[0]["content"] == "STATIC\n\nTAIL")
    parts, turns = ai_providers._system_parts(msgs)
    check("_system_parts keeps the halves apart for anthropic",
          parts == ["STATIC", "TAIL"] and len(turns) == 1)


def test_usage_summary_surfaces_cache_tokens():
    from jarvis import token_usage
    hit = token_usage.extract_usage("anthropic", {"usage": {
        "input_tokens": 12, "output_tokens": 5,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 4000}})
    check("cache reads reported separately", hit.get("cache_read_tokens") == 4000)
    check("input_tokens still the grand total (back-compat)",
          hit["input_tokens"] == 4012)
    plain = token_usage.extract_usage("anthropic", {"usage": {
        "input_tokens": 12, "output_tokens": 5}})
    check("no cache activity = no cache keys (not a misleading zero)",
          "cache_read_tokens" not in plain)
    gem = token_usage.extract_usage("gemini", {"usageMetadata": {
        "promptTokenCount": 900, "candidatesTokenCount": 10,
        "cachedContentTokenCount": 800}})
    check("gemini cachedContentTokenCount surfaces", gem.get("cache_read_tokens") == 800)


for t in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
    t()

print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} passed")
sys.exit(1 if FAIL else 0)
