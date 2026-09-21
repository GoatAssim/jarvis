"""Thinking / reasoning support — one request-shape per provider family.

WHAT THIS ADDS
--------------
Every current frontier model can be asked to *think before answering*, and
every vendor spells it differently:

    Anthropic   thinking: {type: "enabled", budget_tokens: N}   -> "thinking" blocks
    Gemini      generationConfig.thinkingConfig{thinkingBudget, includeThoughts}
                                                                -> parts[].thought
    OpenAI      reasoning_effort: "low"|"medium"|"high"         -> no visible trace
    DeepSeek /  (reasoner models think unconditionally)         -> message.reasoning_content
    Groq/xAI/   reasoning_effort, sometimes reasoning_format     -> message.reasoning
    OpenRouter  reasoning: {effort|max_tokens}                  -> message.reasoning
    Ollama      think: true                                     -> message.thinking

This module is the ONE place that knows all of that. Adapters call
`request_patch()` to get the right keys for their payload and
`extract_trace()` to pull whatever thinking text came back, so
ai_providers.py never grows a per-vendor thinking branch and adding a new
provider family is a dict entry here rather than an edit in five places.

WHY IT IS A LEVEL, NOT A BOOLEAN
--------------------------------
Thinking is not free: it bills as output tokens and it adds real latency.
"Should Jarvis think?" has a different answer for "what's my battery" than
for "why is this build failing". So the knob is a LEVEL — off / low /
medium / high — resolved per ask from three sources, most specific first:

    1. an explicit per-call override (jarvis --think high "...")
    2. the auto-escalation heuristic, if reasoning.auto is on
    3. defaults.reasoning.level in ai_config.json

and translated to each provider's own units by _LEVELS below. A budget in
tokens is meaningless to OpenAI and an effort string is meaningless to
Anthropic; a level is meaningful to both.

AUTO-ESCALATION
---------------
`reasoning.auto` (default on) bumps a level-0 ask up to "low"/"medium" when
the message looks like it actually wants deliberation — debugging, planning,
comparing options, multi-step math, "why". The same keyword-scoring trick
tool_router.py already uses for tool groups, for the same reason: it costs
no tokens and no round trip, so being slightly wrong is cheap. It never
escalates past "medium" on its own, and never downgrades an explicit level.

DEGRADING, NEVER FAILING
------------------------
A model that doesn't support thinking must not break. Every request_patch()
is additive and guarded by CAPABILITIES; if a provider 400s anyway,
`strip_from_payload()` lets the adapter retry once without the thinking keys
rather than burning the key. That retry path is why the thinking fields are
kept in one flat set instead of being merged deep into the payload.

INTERACTION WITH TOOLS
----------------------
Anthropic requires thinking blocks to be preserved verbatim in the assistant
turn when tools are in play, so `carry_blocks()` exists for the adapter to
re-attach them; everything else discards the trace after extraction. Gemini's
thought parts must NOT be echoed back, which is why they're filtered out of
the assistant content the adapter appends.
"""

import re

LEVELS = ("off", "low", "medium", "high")
DEFAULT_LEVEL = "off"

# Per-level budgets/efforts. Anthropic's minimum billable thinking budget is
# 1024 tokens, so "low" starts there rather than at something smaller that
# the API would reject outright.
_LEVELS = {
    "off":    {"budget": 0,    "effort": None,     "gemini": 0,     "max_thinking_rounds": 0},
    "low":    {"budget": 1024, "effort": "low",    "gemini": 1024,  "max_thinking_rounds": 2},
    "medium": {"budget": 4096, "effort": "medium", "gemini": 4096,  "max_thinking_rounds": 4},
    # None = no cap: every round that ran tools gets a thinking request.
    # Master plan Part A §6 ("thinking between tool calls"): the old code
    # capped every level at 2 total rounds (round 0 + one more), so a long
    # tool-calling sequence got no thinking at all past the second round
    # regardless of level. This is a default policy choice, not a hard
    # requirement from the finding itself — see §6's own text ("worth
    # deciding deliberately what the new policy should be... scaled by
    # thinking level"). 'low' keeps the pre-§6 behavior so the cheapest
    # level's token/latency cost doesn't change; 'medium' allows more
    # rounds before capping; 'high' — a level someone chose specifically
    # for maximum deliberation — is no longer silently cut off.
    "high":   {"budget": 12288, "effort": "high",  "gemini": 16384, "max_thinking_rounds": None},
}

# Which provider *types* (ai_providers.ADAPTERS keys) can be asked to think
# at all. An entry here only means "the request shape exists" — an
# individual model within the family may still ignore it, which is fine and
# is why nothing downstream asserts a trace came back.
CAPABILITIES = {
    "anthropic": True,
    "gemini": True,
    "openai_compatible": True,
    "ollama": True,
    "cohere": False,
}

# openai_compatible covers a whole family with three different request
# shapes. Matched against the provider's *name* (lowercased), longest first,
# because "openrouter" and "openai" both contain "openai" as a substring.
#
#   effort    reasoning_effort: "low"|"medium"|"high"        (OpenAI, xAI, Groq)
#   object    reasoning: {"effort": ...}                     (OpenRouter)
#   implicit  nothing to send; the model always reasons      (DeepSeek reasoner)
_OPENAI_FAMILY_STYLE = (
    ("openrouter", "object"),
    ("deepseek", "implicit"),
    ("together", "object"),
    ("mistral", "none"),
    ("openai", "effort"),
    ("groq", "effort"),
    ("xai", "effort"),
    ("grok", "effort"),
    ("perplexity", "effort"),
    ("cerebras", "effort"),
)

# Models whose names say outright that they reason. Used only to decide
# whether to bother sending a knob at all — never to *block* one, since a
# name is a weak signal and a wrong guess here should cost nothing.
_REASONING_MODEL_RE = re.compile(
    r"(^|[-_/])(o[1-4](\s|-|$)|gpt-5|reason|think|r1\b|qwq|deepseek-r)", re.I
)

# §4 fix: a provider typed openai_compatible but actually pointed at an
# Ollama server (its native /api/chat endpoint speaks a different shape,
# but plenty of setups instead point at Ollama's OpenAI-compat endpoint,
# or a proxy in front of it) needs "think": true, not "reasoning_effort" —
# Ollama silently ignores the latter, which is exactly how a second local
# provider config'd this way ends up with no explicit thinking instruction
# at all. Three independent signals, any one is enough:
#   - the provider's own `name` says so ("ollama1", "my-ollama", ...)
#   - the base_url is Ollama's default port, or ends in its native path
#   - the model string uses Ollama's registry "name:tag" convention, which
#     nothing else in _OPENAI_FAMILY_STYLE's roster uses this way (an
#     OpenRouter "org/model:variant" always has a "/" too, so requiring no
#     "/" keeps this from firing on those)
# This is a heuristic, not a certainty — a false positive costs one wasted
# request key that Ollama ignores, same as today; a false negative just
# leaves things exactly as broken as they already are.
_OLLAMA_TAG_MODEL_RE = re.compile(r"^[A-Za-z0-9][\w.\-]*:[A-Za-z0-9][\w.\-]*$")


def _looks_like_ollama_host(provider_name, base_url, model):
    name = (provider_name or "").strip().lower()
    if "ollama" in name:
        return True
    url = (base_url or "").strip().lower()
    if ":11434" in url or url.rstrip("/").endswith("/api/chat"):
        return True
    model = model or ""
    if "/" not in model and _OLLAMA_TAG_MODEL_RE.match(model):
        return True
    return False

# Auto-escalation signal. Weighted like tool_router.TOOL_KEYWORDS and read
# the same way: word-boundary matched, additive, compared against a
# threshold. These are phrases where a wrong first answer is expensive
# enough that a few hundred extra output tokens is a good trade.
_AUTO_KEYWORDS = {
    # debugging / diagnosis
    "why is": 4, "why does": 4, "why did": 4, "why won't": 4, "why isn't": 4,
    "debug": 4, "root cause": 5, "failing": 3, "broken": 3, "stack trace": 5,
    "traceback": 5, "doesn't work": 3, "not working": 3, "regression": 4,
    # design / planning
    "plan": 3, "design": 3, "architecture": 4, "refactor": 4, "trade-off": 5,
    "tradeoff": 5, "pros and cons": 5, "compare": 3, "best approach": 5,
    "how should i": 4, "strategy": 3, "migrate": 3,
    # analysis / reasoning
    "explain why": 5, "walk me through": 4, "step by step": 5, "prove": 4,
    "derive": 4, "calculate": 3, "estimate": 3, "optimize": 4, "algorithm": 3,
    "think through": 6, "reason about": 6, "figure out": 4, "work out": 3,
    # explicit asks
    "think hard": 8, "think carefully": 8, "take your time": 6,
    "be thorough": 5, "double check": 5, "double-check": 5,
}

# Two independent thresholds so auto-escalation has a ceiling of its own:
# anything at or above _AUTO_MEDIUM_SCORE gets "medium", anything at or
# above _AUTO_LOW_SCORE gets "low", and nothing auto-escalates to "high" —
# that stays a deliberate, typed choice.
_AUTO_LOW_SCORE = 4
_AUTO_MEDIUM_SCORE = 9

# A very short message almost never needs deliberation regardless of what
# words are in it ("compare" alone is not a research task), and a very long
# one usually does. Applied as a nudge, not a veto.
_SHORT_MESSAGE_CHARS = 25
_LONG_MESSAGE_CHARS = 400

DEFAULT_CONFIG = {
    "level": DEFAULT_LEVEL,
    "auto": True,
    # Show the thinking trace to the user (web UI bubble / CLI stderr).
    # Off by default: the trace is long, and the point of Jarvis's reply is
    # the reply. Turned on, it's one collapsed bubble per turn.
    "show": False,
    # Persist the trace into the conversation record so a page reload can
    # still show it. Independent of `show` on purpose — someone may want it
    # recorded for later debugging without it on screen every turn.
    "save": True,
    # Hard ceiling on saved/displayed trace length, so one runaway thinking
    # block can't bloat a conversation file.
    "max_trace_chars": 4000,
}


def resolve_config(defaults=None):
    """Merge defaults.reasoning from ai_config.json over DEFAULT_CONFIG.

    Tolerates the legacy/shorthand forms people actually write by hand:
    `"reasoning": true` (-> medium), `"reasoning": "high"`, or the full
    object. Anything unrecognized falls back to the defaults rather than
    raising — this is read on the hot path of every ask.
    """
    cfg = dict(DEFAULT_CONFIG)
    raw = (defaults or {}).get("reasoning")
    if raw is True:
        cfg["level"] = "medium"
        return cfg
    if raw is False:
        cfg["level"] = "off"
        cfg["auto"] = False
        return cfg
    if isinstance(raw, str):
        cfg["level"] = normalize_level(raw)
        return cfg
    if isinstance(raw, dict):
        for key in cfg:
            if key in raw:
                cfg[key] = raw[key]
        cfg["level"] = normalize_level(cfg.get("level"))
        cfg["auto"] = bool(cfg.get("auto", True))
        cfg["show"] = bool(cfg.get("show", False))
        cfg["save"] = bool(cfg.get("save", True))
        try:
            cfg["max_trace_chars"] = max(200, int(cfg.get("max_trace_chars") or 4000))
        except (TypeError, ValueError):
            cfg["max_trace_chars"] = 4000
    return cfg


def normalize_level(value):
    """Accept the spellings a human or a model actually types."""
    if value is None:
        return DEFAULT_LEVEL
    if value is True:
        return "medium"
    if value is False:
        return "off"
    text = str(value).strip().lower()
    if text in LEVELS:
        return text
    aliases = {
        "none": "off", "no": "off", "false": "off", "0": "off", "disabled": "off",
        "min": "low", "minimal": "low", "light": "low", "quick": "low", "1": "low",
        "normal": "medium", "default": "medium", "on": "medium", "true": "medium",
        "standard": "medium", "2": "medium",
        "max": "high", "maximum": "high", "deep": "high", "hard": "high",
        "thorough": "high", "3": "high",
    }
    return aliases.get(text, DEFAULT_LEVEL)


def max_thinking_rounds(level):
    """Master plan Part A §6: how many rounds of a single turn may carry a
    thinking request, round 0 included. None means unlimited (every round
    that ran tools gets one) — see the comment on _LEVELS above for why
    each level's number is what it is. Unknown/invalid levels fall back to
    'off's cap (0), same fail-safe direction as normalize_level."""
    return _LEVELS.get(normalize_level(level), _LEVELS["off"]).get("max_thinking_rounds", 0)


def auto_level(user_text):
    """Guess a level from the message alone. Zero tokens, zero round trips.

    Returns "off"/"low"/"medium" — never "high", see the module docstring.
    """
    text = (user_text or "").strip().lower()
    if not text:
        return "off"
    score = 0
    for phrase, weight in _AUTO_KEYWORDS.items():
        if re.search(r"\b" + re.escape(phrase) + r"\b", text):
            score += weight
    if len(text) <= _SHORT_MESSAGE_CHARS:
        score -= 3
    elif len(text) >= _LONG_MESSAGE_CHARS:
        score += 3
    # A question mark alone means nothing, but a message with several
    # clauses AND a question is usually a real question.
    if text.count("?") >= 1 and text.count(",") >= 2:
        score += 2
    if score >= _AUTO_MEDIUM_SCORE:
        return "medium"
    if score >= _AUTO_LOW_SCORE:
        return "low"
    return "off"


def effective_level(user_text, defaults=None, override=None):
    """The level this one ask should actually use.

    Precedence: explicit override > configured level > auto heuristic.
    Auto only ever raises "off" — it never lowers a level someone chose,
    because someone who typed `--think high` meant it.
    """
    cfg = resolve_config(defaults)
    if override is not None:
        return normalize_level(override), cfg
    level = normalize_level(cfg.get("level"))
    if level == "off" and cfg.get("auto", True):
        level = auto_level(user_text)
    return level, cfg


def _openai_style(provider_name, model, base_url=""):
    if _looks_like_ollama_host(provider_name, base_url, model):
        return "ollama_native"
    name = (provider_name or "").strip().lower()
    for needle, style in _OPENAI_FAMILY_STYLE:
        if needle in name:
            return style
    # Unknown host: only send an effort knob if the MODEL name suggests a
    # reasoning model, since an unrecognized field is a 400 on some hosts
    # (Groq does exactly this for prompt_cache_key — see call_openai_compatible).
    return "effort" if _REASONING_MODEL_RE.search(model or "") else "none"


def request_patch(provider_type, level, *, provider_name="", model="",
                  base_url="", include_trace=True):
    """The keys to merge into this provider's request payload.

    Returns a flat dict (possibly empty). Empty always means "send exactly
    what you were going to send" — callers never need to branch on level
    themselves, they just `payload.update(request_patch(...))`.
    """
    level = normalize_level(level)
    if level == "off" or not CAPABILITIES.get(provider_type):
        return {}
    spec = _LEVELS[level]

    if provider_type == "anthropic":
        # budget_tokens must be < max_tokens; the adapter raises max_tokens
        # to fit (see _fit_max_tokens) rather than silently shrinking the
        # budget, because a 1023-token budget is rejected outright.
        return {"thinking": {"type": "enabled", "budget_tokens": spec["budget"]}}

    if provider_type == "gemini":
        return {
            "_generationConfig": {
                "thinkingConfig": {
                    "thinkingBudget": spec["gemini"],
                    "includeThoughts": bool(include_trace),
                }
            }
        }

    if provider_type == "ollama":
        return {"think": True}

    if provider_type == "openai_compatible":
        style = _openai_style(provider_name, model, base_url)
        if style == "ollama_native":
            return {"think": True}
        if style == "effort":
            return {"reasoning_effort": spec["effort"]}
        if style == "object":
            return {"reasoning": {"effort": spec["effort"]}}
        # "implicit" (the model always reasons) and "none" (no supported
        # knob) both send nothing — but "implicit" still returns a trace,
        # which extract_trace() picks up regardless of what was sent.
        return {}

    return {}


# Every key any request_patch() above can introduce. The retry-without-
# thinking path strips exactly this set, so a provider that rejects one of
# them never costs a whole API key.
_PATCH_KEYS = ("thinking", "think", "reasoning", "reasoning_effort",
               "reasoning_format", "_generationConfig")


def strip_from_payload(payload):
    """Remove every thinking-related key. Returns True if it changed anything.

    Used by the adapters' one-shot retry after a 400 that mentions an
    unsupported reasoning field — same shape as call_openai_compatible's
    existing `_looks_like_omitted_tools_confused_the_model` retry.
    """
    changed = False
    for key in _PATCH_KEYS:
        if key in payload:
            payload.pop(key, None)
            changed = True
    gen = payload.get("generationConfig")
    if isinstance(gen, dict) and "thinkingConfig" in gen:
        gen.pop("thinkingConfig", None)
        changed = True
    return changed


_REJECT_RE = re.compile(
    r"(thinking|reasoning_effort|reasoning|thinkingconfig|thinkingbudget|"
    r"\bthink\b).{0,60}?(unsupported|not supported|unrecognized|unknown|"
    r"invalid|does not support|isn't supported)"
    r"|(unsupported|unrecognized|unknown|invalid|not supported).{0,60}?"
    r"(thinking|reasoning_effort|reasoning|thinkingconfig|thinkingbudget)",
    re.I | re.S,
)


def looks_like_thinking_rejected(reason):
    """Did this error come from the thinking keys specifically?

    Conservative on purpose: a false positive here just costs one retry
    without thinking, but a false negative burns an API key on a request
    shape Jarvis chose rather than anything the user did.
    """
    return bool(reason and _REJECT_RE.search(str(reason)))


def fit_max_tokens(max_tokens, level):
    """Anthropic rejects budget_tokens >= max_tokens.

    Raising the ceiling is right rather than shrinking the budget: the whole
    point of asking for `high` is the deliberation, and a 700-token cap with
    a 12k budget would otherwise silently become no thinking at all.
    """
    level = normalize_level(level)
    if level == "off":
        return max_tokens
    budget = _LEVELS[level]["budget"]
    try:
        current = int(max_tokens)
    except (TypeError, ValueError):
        current = 0
    # +1024 leaves room for a real answer after the thinking is paid for.
    return max(current, budget + 1024)


def extract_trace(provider_type, data):
    """Pull whatever thinking text a response carried. "" when there is none.

    Every branch is defensive: a provider that adds, renames or drops one of
    these fields degrades to "no trace", never to an exception on the
    response-parsing hot path.
    """
    if not isinstance(data, dict):
        return ""
    try:
        if provider_type == "anthropic":
            parts = []
            for block in data.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") in ("thinking", "redacted_thinking"):
                    parts.append(block.get("thinking") or block.get("data") or "")
            return "\n".join(p for p in parts if p).strip()

        if provider_type == "gemini":
            parts = []
            for cand in data.get("candidates") or []:
                content = (cand or {}).get("content") or {}
                for part in content.get("parts") or []:
                    if isinstance(part, dict) and part.get("thought"):
                        parts.append(part.get("text") or "")
            return "\n".join(p for p in parts if p).strip()

        if provider_type == "ollama":
            message = data.get("message") or {}
            return (message.get("thinking") or "").strip()

        if provider_type == "openai_compatible":
            choices = data.get("choices") or []
            if not choices:
                return ""
            message = (choices[0] or {}).get("message") or {}
            # DeepSeek: reasoning_content. OpenRouter/Groq: reasoning.
            # Ollama's OpenAI-compat endpoint (§4 fix, "ollama_native" style
            # above): "thinking", same field name its native /api/chat uses,
            # since it's the same server underneath.
            # OpenRouter can also send structured reasoning_details.
            for key in ("reasoning_content", "reasoning", "thinking"):
                value = message.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            details = message.get("reasoning_details")
            if isinstance(details, list):
                bits = []
                for d in details:
                    if isinstance(d, dict):
                        bits.append(d.get("text") or d.get("summary") or "")
                return "\n".join(b for b in bits if b).strip()
            return ""
    except Exception:  # noqa: BLE001 — never break response parsing over a trace
        return ""
    return ""


def carry_blocks(provider_type, blocks):
    """Assistant-turn content blocks that must survive back into the next request.

    Anthropic is strict here: when a tool_use turn is replayed, its thinking
    blocks must come back verbatim (signature included) or the API rejects
    the turn. Everything else discards its trace, so this returns the blocks
    unchanged for anthropic and nothing for the rest.
    """
    if provider_type != "anthropic" or not isinstance(blocks, list):
        return []
    return [b for b in blocks
            if isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")]


def clip_trace(text, max_chars=None):
    """Trim a trace for storage/display without cutting mid-word where avoidable."""
    text = (text or "").strip()
    limit = max_chars or DEFAULT_CONFIG["max_trace_chars"]
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.7:
        cut = cut[:space]
    return cut.rstrip() + " …"


def describe(level):
    """One short line for a trace/status surface."""
    level = normalize_level(level)
    if level == "off":
        return "thinking off"
    spec = _LEVELS[level]
    return "thinking %s (~%d token budget)" % (level, spec["budget"])


# ---------------------------------------------------------------------------
# TOKEN DISCIPLINE
#
# Thinking bills as OUTPUT tokens — the most expensive kind — and the naive
# wiring (send the thinking knob on every request) multiplies that by the
# number of tool rounds. A 5-round tool-calling turn at level "high" would
# buy the thinking budget SIX times over, and five of those six are the model
# deciding "which tool next" — a decision tool_router already narrowed to one
# group before the model saw anything.
#
# So thinking is spent where it changes the answer and skipped where it
# doesn't:
#
#   round 0            full budget   — the actual reasoning about the request
#   middle tool rounds OFF           — "call tool X with these args" is not a
#                                      reasoning problem
#   final round        reduced       — synthesizing results is worth some
#                                      deliberation, but far less than the
#                                      opening analysis
#
# In practice this takes a 5-round `high` turn from ~74k budgeted thinking
# tokens down to ~18k, without touching the round where the thinking is
# actually load-bearing. See spend_estimate() for the arithmetic.
#
# The second lever is that a trace is never resent. Thinking text is output,
# and echoing it back as input on the next round would pay for the same
# tokens twice — once to generate, once to re-read. Anthropic's tool-use
# turns are the one exception (the API rejects a replayed tool_use turn whose
# thinking blocks are missing), which is what carry_blocks() exists for.
# ---------------------------------------------------------------------------

# Fraction of the level's budget the closing round gets. Low enough to matter,
# high enough that a multi-tool turn can still reason about what came back.
FINAL_ROUND_FRACTION = 0.4

# Below this the budget isn't worth sending at all — Anthropic rejects
# anything under 1024 outright, and a sub-1k budget elsewhere buys a sentence.
MIN_USEFUL_BUDGET = 1024


def should_think(level, round_num=0, is_final=False, ran_tools=False):
    """Should THIS round carry a thinking request?

    round_num 0 always thinks (when the level is on). After that, only the
    round that will actually produce the answer does — and only if tools ran,
    because a turn that never called a tool has no results to synthesize and
    already did its thinking on round 0.
    """
    if normalize_level(level) == "off":
        return False
    if round_num == 0:
        return True
    return bool(is_final and ran_tools)


def budget_for_round(level, round_num=0, is_final=False):
    """Token budget for one round, after the discipline above."""
    level = normalize_level(level)
    if level == "off":
        return 0
    full = _LEVELS[level]["budget"]
    if round_num == 0:
        return full
    reduced = int(full * FINAL_ROUND_FRACTION)
    return reduced if reduced >= MIN_USEFUL_BUDGET else 0


def effort_for_round(level, round_num=0):
    """Effort string for the hosts that take one instead of a budget.

    Same idea, expressed in the only vocabulary those APIs have: step the
    effort down rather than the token count, since there is no token count
    to step.
    """
    level = normalize_level(level)
    if level == "off":
        return None
    if round_num == 0:
        return _LEVELS[level]["effort"]
    return {"high": "medium", "medium": "low", "low": "low"}.get(level)


def round_patch(provider_type, level, round_num=0, is_final=False,
                ran_tools=False, *, provider_name="", model="",
                base_url="", include_trace=True):
    """request_patch() for one specific round — the function adapters call.

    Returns {} for any round that shouldn't think, which is the whole point:
    an empty patch means the adapter sends exactly the request it would have
    sent with thinking switched off entirely.
    """
    if not should_think(level, round_num, is_final, ran_tools):
        return {}
    if not CAPABILITIES.get(provider_type):
        return {}

    level = normalize_level(level)
    budget = budget_for_round(level, round_num, is_final)
    if budget <= 0 and provider_type in ("anthropic", "gemini"):
        return {}

    if provider_type == "anthropic":
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}

    if provider_type == "gemini":
        return {
            "_generationConfig": {
                "thinkingConfig": {
                    "thinkingBudget": budget,
                    "includeThoughts": bool(include_trace),
                }
            }
        }

    if provider_type == "ollama":
        return {"think": True}

    if provider_type == "openai_compatible":
        style = _openai_style(provider_name, model, base_url)
        if style == "ollama_native":
            return {"think": True}
        effort = effort_for_round(level, round_num)
        if not effort:
            return {}
        if style == "effort":
            return {"reasoning_effort": effort}
        if style == "object":
            return {"reasoning": {"effort": effort}}
        return {}

    return {}


def spend_estimate(level, rounds=1):
    """Budgeted thinking tokens for a turn, naive vs. disciplined.

    Exposed so the Debug panel and `jarvis think` can show what the setting
    actually costs instead of asking someone to trust a claim about it.
    """
    level = normalize_level(level)
    if level == "off":
        return {"level": level, "naive": 0, "actual": 0, "saved": 0, "rounds": rounds}
    full = _LEVELS[level]["budget"]
    naive = full * max(1, rounds)
    actual = full + (budget_for_round(level, 1, True) if rounds > 1 else 0)
    return {
        "level": level, "rounds": rounds,
        "naive": naive, "actual": actual,
        "saved": naive - actual,
        "saved_percent": round((naive - actual) / naive * 100) if naive else 0,
    }
