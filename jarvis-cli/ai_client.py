"""The AI brain: builds Jarvis's system prompt, tries configured providers
in order until one actually answers, and remembers the exchange.

Kept deliberately separate from cli.py (which only knows how to print an
AskResult nicely) and from ai_providers.py (which only knows how to speak
one provider's wire format) \u2014 this module is the one place that knows
*policy*: which provider to try next, what "failed" means, what Jarvis is
told about itself, and what gets remembered.
"""

import json
import re
import threading
import os
import subprocess
import sys
import time

from . import ai_config, ai_providers, command_tools, conversations, memory, playnite_config, skill_stickiness, skills, stats, tool_safety
from . import dev_agent_events as _dev_agent_events
from . import discovery_cache
from . import key_health
from . import logs
from . import tool_diagnosis
from . import tool_result_shaping
from . import tool_router
from . import route_stickiness
from . import reasoning
from . import turn_trace
from . import tools as system_tools

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_TOKENS = 1700
DEFAULT_ASSISTANT_NAME = "J.A.R.V.I.S"
DEFAULT_ADDRESS = "sir"
DEFAULT_TOOLS_ENABLED = True
DEFAULT_ATTITUDE = "dry"

# Personality presets for the persona's tone, selectable from the web UI's
# Skin modal (persona.attitude in ai_config.json) or by hand-editing that
# file. Each entry drives two spots in _system_prompt(): "full" is the
# clause slotted into the full (uncompact) persona line ("...think a
# supremely capable, unflappable AI butler: {full}. Address the user
# as..."), and "compact" is the whole short sentence used in the 100%-
# capacity persona line in place of "Dry wit, concise." Deliberately NOT
# used in ultra (50% capacity) mode at all, regardless of which attitude is
# picked \u2014 see the "dry wit is flavor, not a rule" comment on the ultra
# branch below; that token-saving call applies no matter which attitude is
# selected, not just the default. "dry" is the original, unchanged
# personality this app always had \u2014 kept as the default so nobody's
# persona changes underneath them just because this feature shipped.
ATTITUDE_PRESETS = {
    "dry": {
        "label": "Dry Wit",
        "full": "dry wit, complete composure, quiet confidence, never groveling or over-apologizing",
        "compact": "Dry wit, concise.",
    },
    "cheerful": {
        "label": "Cheerful",
        "full": "upbeat and warmly enthusiastic, genuinely pleased to help, quick with encouragement",
        "compact": "Upbeat and encouraging, concise.",
    },
    "snarky": {
        "label": "Snarky",
        "full": "sharp and playfully sarcastic, quick with a dry jab, but never actually unhelpful or mean about it",
        "compact": "Playfully sarcastic, concise.",
    },
    "formal": {
        "label": "Formal Butler",
        "full": "old-world formal and impeccably polite, in the manner of a classic household butler, never casual or familiar",
        "compact": "Formal and polite, concise.",
    },
    "warm": {
        "label": "Warm & Encouraging",
        "full": "warm, patient, and encouraging, like a trusted mentor genuinely invested in things going well",
        "compact": "Warm and patient, concise.",
    },
    "blunt": {
        "label": "No-Nonsense",
        "full": "blunt and no-nonsense, skips the pleasantries and gets straight to the point, respects the user's time above all",
        "compact": "Blunt and direct, concise.",
    },
}

# Merge in any custom attitudes registered by a tool file's PERSONAS entry
# (see persona_registry.py's _resolve_attitude and tools.AUTO_ATTITUDES).
# Done here, right after the built-in dict literal, so a custom attitude id
# picked in the Skin modal and saved to persona.attitude actually changes
# what the model is told about its own personality — not just what the
# dropdown displays. dict.update() means a custom id that happens to match
# a built-in one (e.g. someone naming a custom attitude "dry") just wins
# for that id, same as any other last-write-wins merge — no crash, no
# special-casing needed.
try:
    ATTITUDE_PRESETS.update(system_tools.AUTO_ATTITUDES)
except Exception:
    pass

MAX_COMMANDS_LISTED = 12  # cap how many command names+descriptions go into every prompt
COMPACT_MAX_COMMANDS = 6
COMPACT_DESC_MAX_LEN = 50
COMPACT_HISTORY_CHAR_BUDGET = 4800
COMPACT_HISTORY_EXCHANGES = 10
COMPACT_RECAP_EXCHANGES = 16
COMPACT_RECAP_CHAR_BUDGET = 1400
DEFAULT_COMPACT_PROMPT = True
DEFAULT_COMPACT_PROMPT_PROVIDERS = ("groq",)


class OrderedSchemaSet:
    """Enhancement #6 (see jarvis-token-optimization-enhancements.md):
    an insertion-ordered, tool-name-keyed structure standing in for the
    plain `list[dict]` that `active_schemas`/`compact_schemas`/
    `name_only_schemas` used to be in ask().

    Before this, a duplicate schema was still *representable* in those
    intermediate lists — `_discover_sink()` guarded against re-adding a
    name via a separate `_discovered_names` set, and a defensive
    dedupe-by-name filter was applied once, right before the final
    `tool_schemas` selection reached the adapter. Both were correct, but
    anything that forgot to check `_discovered_names` before appending
    could still reintroduce a duplicate upstream of that filter. Keying
    the structure itself by name makes a duplicate structurally
    impossible instead of merely guarded-against: `.append()` is a no-op
    on a name already present, so "is this name already here" collapses
    to a plain `in` check against the set itself — no separate tracking
    set needed alongside it.

    Iterates in insertion order and supports `len()`/`in` for convenience
    (so most call sites that only ever read/iterate need no changes at
    all), but callers that hand this to something expecting a genuine
    `list` (e.g. `ai_providers.py`'s adapters, or anything doing
    list-specific things like slicing) should call `.to_list()` at that
    boundary rather than relying on duck-typing.
    """

    __slots__ = ("_by_name",)

    def __init__(self, schemas=None):
        self._by_name = {}
        self.extend(schemas)

    def append(self, schema):
        """No-op if a schema with this name is already present (first
        occurrence wins, same as the dedupe filter it replaces)."""
        if not isinstance(schema, dict):
            return
        name = schema.get("name")
        if not name or name in self._by_name:
            return
        self._by_name[name] = schema

    def extend(self, schemas):
        for schema in schemas or []:
            self.append(schema)

    def to_list(self):
        return list(self._by_name.values())

    def __iter__(self):
        return iter(self._by_name.values())

    def __len__(self):
        return len(self._by_name)

    def __contains__(self, name):
        return name in self._by_name

    def __bool__(self):
        return bool(self._by_name)

    def __repr__(self):
        return f"OrderedSchemaSet({list(self._by_name.keys())!r})"

# ===========================================================================
# Prompt "capacity" modes \u2014 a generic, table-driven registry.
#
# Each entry in PROMPT_MODE_DEFS is a complete, self-contained profile of
# prompt-size knobs (see _build_messages/ask() for how every key is used).
# PROMPT_MODES, MODE_LABELS, the mode cycle (next_mode), the "jarvis mode" /
# "mode-set" CLI commands, the web UI's capacity switch, and the
# get_capacity_mode/set_capacity_mode AI tools (mode_tools.py) are all
# derived from this one list \u2014 nothing else needs to change to add a mode.
#
# To add a new mode later: append one dict here with a unique "name" (used
# in defaults.prompt_mode / "jarvis mode-set <name>" / the tool's "mode"
# argument) and a "label" (shown in the web switch + get_capacity_mode), plus
# every knob below. Order matters only for the cycle (web switch click /
# set_capacity_mode's next=true) \u2014 it steps through this list in order and
# wraps around.
#
# Knobs, in the order they appear below:
#   max_commands        \u2014 how many saved commands get listed in the prompt
#   desc_max_len         \u2014 max chars of each command's description
#   history_char_budget  \u2014 total chars of prior-turn history included
#   history_exchanges    \u2014 max prior exchanges included
#   recap_exchanges       \u2014 how many older exchanges get folded into a recap
#   recap_budget          \u2014 max chars of that recap
#   include_freq          \u2014 include the "frequently used commands" context
#   compact_tools_blurb    \u2014 use the short "tools" explainer vs the long one
#   compact_persona        \u2014 use the short persona blurb vs the long one
#   playnite_freq_games    \u2014 how many frequent Playnite games get listed
#   skip_other_convos      \u2014 omit the "other recent conversations" context
#   tool_schema_style      \u2014 "compact" (types/enums/required, short descs),
#                            "name_only" (just names \u2014 the model re-requests a
#                            schema the first time it calls a tool needing
#                            args it didn't supply; see _make_tool_executor), or
#                            "raw" (the full, uncompacted schema exactly as
#                            each tool declares it \u2014 no description clipping,
#                            full property docs; most tokens per tool)
#   precise_persona        \u2014 optional; when True, _system_prompt appends an
#                            extra paragraph telling the model to be maximally
#                            precise/unambiguous (see _system_prompt). Absent
#                            or False is a no-op \u2014 only "precise" sets this.
#   tool_result_budget     \u2014 chars of already-ran tool results replayed to
#                            the next provider on failover
#   tool_result_verbosity  \u2014 "full" (every field a tool returns), "medium"
#                            (drop merely-nice-to-have fields), or "low"
#                            (only what a tool's author marked necessary) \u2014
#                            see tool_result_shaping.py, applied to every
#                            tool call's *result* (as opposed to
#                            tool_schema_style, which shapes what the model
#                            is told a tool accepts before it's even
#                            called). A tool not listed in
#                            tool_result_shaping.TOOL_RESULT_SPECS is
#                            unaffected at every level.
# ===========================================================================

PROMPT_MODE_DEFS = [
    {
        "name": "full",
        "label": "400% Capacity",
        "summary": "Fullest context and richest answers. Most tokens per ask.",
        "max_commands": MAX_COMMANDS_LISTED,
        "desc_max_len": COMPACT_DESC_MAX_LEN * 2,
        "history_char_budget": 6000,
        "history_exchanges": 12,
        "recap_exchanges": 20,
        "recap_budget": 1800,
        "include_freq": True,
        "compact_tools_blurb": False,
        "compact_persona": False,
        "playnite_freq_games": 8,
        "skip_other_convos": False,
        "tool_schema_style": "compact",
        "tool_result_budget": 3500,
        "tool_result_verbosity": "full",
    },
    {
        "name": "compact",
        "label": "100% Capacity",
        "summary": "The balanced default \u2014 trimmed history/commands, still full tool schemas.",
        "max_commands": COMPACT_MAX_COMMANDS,
        "desc_max_len": COMPACT_DESC_MAX_LEN,
        "history_char_budget": COMPACT_HISTORY_CHAR_BUDGET,
        "history_exchanges": COMPACT_HISTORY_EXCHANGES,
        "recap_exchanges": COMPACT_RECAP_EXCHANGES,
        "recap_budget": COMPACT_RECAP_CHAR_BUDGET,
        "include_freq": False,
        "compact_tools_blurb": True,
        "compact_persona": True,
        "playnite_freq_games": 0,
        "skip_other_convos": True,
        "tool_schema_style": "compact",
        "tool_result_budget": 1600,
        "tool_result_verbosity": "medium",
    },
    {
        "name": "precise",
        "label": "150% Capacity",
        "summary": (
            "Same history/command budgets as 100%, but full uncompacted tool "
            "schemas (every field, no description clipping) and a sharper, "
            "precision-focused system prompt. More tokens per ask than 100%, "
            "well under 400%."
        ),
        "max_commands": COMPACT_MAX_COMMANDS,
        "desc_max_len": COMPACT_DESC_MAX_LEN,
        "history_char_budget": COMPACT_HISTORY_CHAR_BUDGET,
        "history_exchanges": COMPACT_HISTORY_EXCHANGES,
        "recap_exchanges": COMPACT_RECAP_EXCHANGES,
        "recap_budget": COMPACT_RECAP_CHAR_BUDGET,
        "include_freq": False,
        "compact_tools_blurb": False,
        "compact_persona": False,
        "precise_persona": True,
        # 150% Capacity is the "spend tokens for fidelity" mode, so it opts
        # out of the hybrid catalog tier — every offered tool keeps its full
        # argument schema rather than any of them dropping to a catalog line.
        "catalog_tier": False,
        "playnite_freq_games": 5,
        "skip_other_convos": False,
        "tool_schema_style": "raw",
        "tool_result_budget": 1600,
        "tool_result_verbosity": "full",
    },
    {
        "name": "ultra",
        "label": "50% Capacity",
        "summary": (
            "Ultra compact \u2014 name-only tool schemas plus every other budget cut "
            "to the minimum that still works. Cheapest mode; a tool needing "
            "arguments may cost one extra round trip the first time it's called."
        ),
        "max_commands": 4,
        "desc_max_len": 30,
        "history_char_budget": 1800,
        "history_exchanges": 4,
        "recap_exchanges": 6,
        "recap_budget": 500,
        "include_freq": False,
        "compact_tools_blurb": True,
        "compact_persona": True,
        "playnite_freq_games": 0,
        "skip_other_convos": True,
        "tool_schema_style": "name_only",
        "tool_result_budget": 600,
        "tool_result_verbosity": "low",
    },
]

PROMPT_MODES = tuple(m["name"] for m in PROMPT_MODE_DEFS)
MODE_LABELS = {m["name"]: m["label"] for m in PROMPT_MODE_DEFS}
MODE_SUMMARIES = {m["name"]: m.get("summary", "") for m in PROMPT_MODE_DEFS}
_MODE_BY_NAME = {m["name"]: m for m in PROMPT_MODE_DEFS}
DEFAULT_PROMPT_MODE = "compact"

# Below this many tools in one round's offering, the hybrid catalog tier in
# ask() is a no-op and every schema is sent in full. Set from the measured
# per-group costs: at 10 tools a group is ~700-1,000 compacted tokens, which
# is cheaper to just send than to risk an extra round trip over. The three
# groups above it (playnite 31, desktop 16, system_control 11) are where the
# eager-loading cost actually lives.
CATALOG_TIER_MIN_TOOLS = 10


def mode_options():
    """[{"mode","label","summary"}, ...] in registry order \u2014 the one place
    that builds this shape, so cli.py's mode/mode-set commands, the web
    /api/mode responses, and get_capacity_mode/set_capacity_mode (mode_tools)
    all show the exact same list. Adding a mode to PROMPT_MODE_DEFS is
    everything needed for it to show up here."""
    return [
        {"mode": m, "label": MODE_LABELS[m], "summary": MODE_SUMMARIES.get(m, "")}
        for m in PROMPT_MODES
    ]

# Legacy per-key overrides, kept only for the three built-in modes so an
# older hand-edited ai_config.json (compact_max_commands, history_char_budget,
# etc.) keeps working exactly as before. A custom mode appended to
# PROMPT_MODE_DEFS above has no legacy keys to honor, so it's used as-is.
_LEGACY_OVERRIDE_KEYS = {
    "full": {
        "history_char_budget": ("history_char_budget",),
        "history_exchanges": ("history_exchanges",),
        "recap_exchanges": ("recap_exchanges",),
        "recap_budget": ("recap_char_budget",),
    },
    "compact": {
        "max_commands": ("compact_max_commands",),
        "history_char_budget": ("history_char_budget", "compact_history_char_budget"),
        "history_exchanges": ("history_exchanges", "compact_history_exchanges"),
        "recap_exchanges": ("recap_exchanges", "compact_recap_exchanges"),
        "recap_budget": ("recap_char_budget", "compact_recap_char_budget"),
    },
    "ultra": {
        "max_commands": ("ultra_max_commands",),
        "history_char_budget": ("ultra_history_char_budget",),
        "history_exchanges": ("ultra_history_exchanges",),
        "recap_exchanges": ("ultra_recap_exchanges",),
        "recap_budget": ("ultra_recap_char_budget",),
        "tool_result_budget": ("ultra_tool_result_budget",),
    },
}
_SIMPLE_OVERRIDE_KEYS = {"max_commands", "tool_result_budget"}  # not run through _history_int's floor check

# How often a conversation's AI-generated title + one-line gist get
# (re)computed: right after the very first exchange (so the sidebar has
# something useful immediately), then every Nth exchange after that so it
# stays roughly current without an extra AI round-trip on every single ask.
TITLE_REGEN_EVERY = 5
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.S)


class AskResult:
    """Everything cli.py (or, via the web console, server.js re-running the
    CLI) needs to present one 'jarvis <text>' call to a person."""

    __slots__ = ("ok", "text", "provider", "attempts", "assistant_name", "address_user_as", "usage", "degraded",
                 "ending")

    def __init__(self, ok, text=None, provider=None, attempts=None,
                 assistant_name=DEFAULT_ASSISTANT_NAME, address_user_as=DEFAULT_ADDRESS,
                 usage=None, degraded=False, ending=None):
        self.ok = ok
        self.text = text
        self.provider = provider
        self.attempts = attempts or []          # [(provider_label, error_reason), ...]
        self.assistant_name = assistant_name
        self.address_user_as = address_user_as
        # Phase 0 (new_plan.md): ai_providers.AIResult.usage for the attempt
        # that actually succeeded — {input_tokens, output_tokens, total_tokens,
        # rounds: [...], tool_calls: [...]}. None if tools/usage weren't tracked.
        self.usage = usage
        # True when `text` isn't a model-composed reply at all — every
        # provider failed on the closing text call, but real, mutating tool
        # calls had already completed first. See _completed_mutations()'s
        # docstring for why this exists: the alternative was reporting a
        # completed task as a hard failure just because nothing was left to
        # write the last sentence.
        self.degraded = degraded
        # Why the turn ended when it was NOT the model finishing on its own
        # (master plan §5 / F.1: forced endings are reported AS forced):
        #   None            a natural ending
        #   "forced"        the step budget ran out; the reply is the harness's
        #                   summary of what ran and what was pending
        #   "cutoff"        the model's reply hit the output limit while it was
        #                   asking for a tool; same harness summary
        #   "truncated"     the model's answer hit the output limit (the text
        #                   is the model's, and ends with a note saying so)
        #   "no_provider"   every provider failed on the closing call; the text
        #                   is a mechanical summary of completed actions
        #   "pending_action" the user said "go ahead" and the step Jarvis had
        #                   proposed last turn was run directly (F.11)
        self.ending = ending


def _provider_label(provider):
    return provider.get("name") or provider.get("type") or "provider"


def _host_of(provider):
    """host:port of a provider's endpoint, or None. Used to notice that two
    providers (both Ollama entries, say) share a server that just refused a
    connection."""
    from urllib.parse import urlparse
    try:
        return urlparse(str(provider.get("base_url") or "")).netloc or None
    except ValueError:
        return None


def _eligible_providers(providers, defaults=None):
    """Enabled, and either local (ollama — no key needed) or actually has at
    least one real key (see ai_config.provider_keys — handles both the
    'api_keys' list and the older singular 'api_key'). This is the single
    point where an empty-key starter-template entry quietly gets skipped
    instead of being "tried and failed" every time.

    When defaults.provider_priority is set, eligible providers are sorted by
    that list (unknown names keep their relative array order at the end)."""
    # Subagent key isolation, applied FIRST and unconditionally. When this
    # process was spawned as a subagent, the provider list is replaced
    # wholesale by that role's own pool — not appended to, not reordered.
    # Doing it here rather than at a call site is the whole guarantee: this
    # function is the single point where "which keys may this process spend"
    # is decided, so there is no path by which a subagent reaches the main
    # Jarvis key, including on failover. See subagents.providers_from_env.
    # BUGFIX: this used to be one try/except around the whole lookup, so any
    # exception inside providers_from_env() — including one this function's
    # author never anticipated — set `pinned = None`, which reads as "this
    # process isn't a subagent, carry on" and silently handed back the
    # AMBIENT provider list (main key included). For a boundary whose entire
    # job is "a subagent can never reach the main key", fail-open on an
    # unexpected exception is exactly backwards. Only the "am I even a
    # subagent" check (reading one env var) is allowed to fail open, since a
    # normal ask must never be affected by this; once we know the env var IS
    # set, any failure past that point fails CLOSED (empty list) instead.
    try:
        from . import subagents
        is_subagent = bool(os.environ.get(subagents.KEY_ENV))
    except Exception:  # noqa: BLE001 — never let this break an ordinary ask
        is_subagent = False
    if is_subagent:
        try:
            pinned = subagents.providers_from_env(providers)
        except Exception:  # noqa: BLE001 — fail closed: this process IS a subagent
            pinned = []
        providers = pinned

    out = []
    for p in providers:
        if not isinstance(p, dict) or not p.get("enabled", True):
            continue
        if p.get("type") == "ollama" or ai_config.provider_keys(p):
            out.append(p)
    return _sort_providers_by_priority(out, (defaults or {}).get("provider_priority"))


def _sort_providers_by_priority(providers, priority_list):
    """Order providers by defaults.provider_priority (provider names, not keys).
    Providers missing from the list keep their relative order and trail named ones."""
    if not priority_list:
        return providers
    rank = {name: i for i, name in enumerate(priority_list) if isinstance(name, str)}
    if not rank:
        return providers
    trailing = len(rank)

    def sort_key(item):
        index, provider = item
        name = provider.get("name") or ""
        return (rank.get(name, trailing + index), index)

    indexed = list(enumerate(providers))
    indexed.sort(key=sort_key)
    return [p for _, p in indexed]


def _order_providers_by_override(eligible_providers, wanted_names):
    """Reorder (and filter down to) `eligible_providers` per a caller-picked
    ordered list of names \u2014 the general form of provider_override. Matching
    is case-insensitive against each provider's "name" field; a name with no
    match among the eligible providers is silently skipped rather than
    erroring the whole call (see ask()'s provider_override docs for why).
    Duplicate names in wanted_names are collapsed to the name's first
    position \u2014 picking the same provider twice doesn't try it twice."""
    by_lower_name = {}
    for p in eligible_providers:
        key = (p.get("name") or "").strip().lower()
        if key and key not in by_lower_name:
            by_lower_name[key] = p

    ordered = []
    seen = set()
    for name in wanted_names:
        key = name.strip().lower()
        if key in seen:
            continue
        match = by_lower_name.get(key)
        if match is not None:
            ordered.append(match)
            seen.add(key)
    return ordered


def _resolve(provider, defaults):
    """Provider-specific fields win; anything unset falls back to the
    config's 'defaults' block, then a hardcoded default."""
    merged = {
        "timeout": defaults.get("timeout", DEFAULT_TIMEOUT),
        "max_tokens": defaults.get("max_tokens", DEFAULT_MAX_TOKENS),
    }
    merged.update(provider)
    return merged


def _format_var_summary(spec):
    parts = []
    for var_name, var_spec in (spec.get("vars") or {}).items():
        if not isinstance(var_spec, dict):
            continue
        if "default" in var_spec:
            parts.append(f"{var_name}={var_spec['default']}")
        else:
            parts.append(f"{var_name}*")
    return ", ".join(parts)


def _commands_context(commands, max_listed=MAX_COMMANDS_LISTED, desc_max_len=None, compact=False):
    if not commands:
        return ""
    all_names = list(commands.items())
    names = all_names[:max_listed]
    lines = []
    for name, spec in names:
        if not isinstance(spec, dict):
            continue
        desc = spec.get("description", "")
        if desc_max_len is not None and len(desc) > desc_max_len:
            desc = desc[: desc_max_len - 1].rstrip() + "…"
        var_part = _format_var_summary(spec)
        if var_part:
            lines.append(f"- {name} ({var_part}): {desc}")
        else:
            lines.append(f"- {name}: {desc}")
    listing = "\n".join(lines)
    header = "Commands:\n" if compact else "Saved commands:\n"
    ctx = header + listing
    remaining = len(all_names) - len(names)
    if remaining > 0:
        ctx += (
            f"\n…and {remaining} more not shown here. If the command the user means "
            "isn't in this list, call search_commands instead of guessing a name."
        )
    return ctx


def _history_int(defaults, keys, floor, fallback):
    """Prefer explicit history_* knobs. Ignore leftover compact_history_*
    values from older configs that were too small to keep a conversation."""
    for key in keys:
        value = defaults.get(key)
        if isinstance(value, int) and value >= floor:
            return value
    return fallback


def _resolve_prompt_mode(provider_name, defaults):
    """Which of PROMPT_MODES applies to this provider.

    defaults.prompt_mode (set by "jarvis mode-set", the web UI's capacity
    switch, or the set_capacity_mode AI tool) wins outright when it names a
    mode in the registry \u2014 that's a single, explicit, global choice. With
    no explicit mode, fall back to the older per-provider scheme so existing
    configs keep behaving exactly as before: defaults.compact_prompt
    (default True) plus defaults.compact_prompt_providers (default
    ["groq"]) pick "compact" vs "full"; a mode with no matching legacy
    behavior (like "ultra", or any custom mode appended later) is never
    reached by this fallback \u2014 nothing used to ask for it.
    """
    explicit = defaults.get("prompt_mode")
    if explicit in PROMPT_MODES:
        return explicit
    compact_all = defaults.get("compact_prompt", DEFAULT_COMPACT_PROMPT)
    compact_names = defaults.get("compact_prompt_providers")
    if compact_names is None:
        compact_names = list(DEFAULT_COMPACT_PROMPT_PROVIDERS)
    use_compact = bool(compact_all) or provider_name in compact_names
    return "compact" if use_compact else "full"


def _prompt_profile(provider_name, defaults):
    """Return prompt-size knobs for a provider: the matching entry from
    PROMPT_MODE_DEFS (resolved via _resolve_prompt_mode), with any legacy
    per-key config overrides applied on top for the three built-in modes
    (see _LEGACY_OVERRIDE_KEYS). A copy is returned so nothing here ever
    mutates the registry itself.
    """
    mode = _resolve_prompt_mode(provider_name, defaults)
    base = _MODE_BY_NAME.get(mode) or _MODE_BY_NAME[DEFAULT_PROMPT_MODE]
    profile = dict(base)
    profile["mode"] = base["name"]

    for key, config_keys in _LEGACY_OVERRIDE_KEYS.get(base["name"], {}).items():
        if key in _SIMPLE_OVERRIDE_KEYS:
            for config_key in config_keys:
                value = defaults.get(config_key)
                if value is not None:
                    profile[key] = value
                    break
        else:
            profile[key] = _history_int(defaults, config_keys, 1, profile[key])
    return profile


def current_mode(cfg=None):
    """The prompt_mode that's actually in effect right now \u2014 what the web
    UI's capacity switch, 'jarvis mode', and get_capacity_mode show.
    Resolved the same way ask() resolves it per-provider (see
    _resolve_prompt_mode); since a switch only has one indicator, this
    checks the first eligible provider (or falls back to the global default
    if none are configured yet)."""
    cfg = cfg or ai_config.load_ai_config()
    defaults = cfg.get("defaults") or {}
    explicit = defaults.get("prompt_mode")
    if explicit in PROMPT_MODES:
        return explicit
    providers = _eligible_providers(cfg.get("providers") or [], defaults)
    label = _provider_label(providers[0]) if providers else ""
    return _resolve_prompt_mode(label, defaults)


def next_mode(current):
    """The next mode after `current` in PROMPT_MODE_DEFS's order, wrapping
    around \u2014 used by both the web switch's click-to-cycle and
    set_capacity_mode's next=true. Adding a mode to the registry
    automatically slots it into this cycle; nothing here needs to change."""
    names = list(PROMPT_MODES)
    if current not in names:
        return names[0]
    return names[(names.index(current) + 1) % len(names)]


def set_mode(mode):
    """Persist defaults.prompt_mode to ~/.jarvis/ai_config.json, leaving
    every other key (including hand-edited ones this module doesn't know
    about) untouched. Raises ValueError for anything not in PROMPT_MODES."""
    if mode not in PROMPT_MODES:
        raise ValueError(f"unknown mode '{mode}' \u2014 expected one of: {', '.join(PROMPT_MODES)}")
    ai_config.ensure_ai_config()
    try:
        raw = json.loads(ai_config.AI_CONFIG_FILE.read_text(encoding=ai_config.ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        raw = None
    if not isinstance(raw, dict):
        raw = json.loads(json.dumps(ai_config.DEFAULT_AI_CONFIG))  # deep copy of the starter template
    if not isinstance(raw.get("defaults"), dict):
        raw["defaults"] = {}
    raw["defaults"]["prompt_mode"] = mode
    ai_config.AI_CONFIG_FILE.write_text(
        json.dumps(raw, indent=2) + "\n", encoding=ai_config.ENCODING
    )
    return mode


def _tools_blurb(compact, ultra):
    """The tools explainer that is true on EVERY turn, whatever routed.

    Split in two as of the per-clause work below. What stays here is the
    part that doesn't depend on which tools are being offered this round:
    how to call a tool you don't have the schema for, and the
    confirm-before-destructive rule. That makes this function's output a
    pure function of the capacity mode — which is what lets it stay in the
    cached static prefix (see _system_prompt_parts' breakpoint note).

    Everything that names a *specific* tool moved to
    _tool_workflow_notes(), because it varies with the router's decision
    and therefore belongs in the per-request tail. Keeping it here meant
    two separate problems:

      1. Tokens. A message about Spotify still paid for the yt-dlp
         workflow, the package-install workflow, the git workflow and the
         screenshot rule — every turn, forever. That's the bulk of what
         made the compact prompt as big as it was.
      2. Cache. has_playnite/has_spotify were already derived from the
         router's offered set, so this "static" block silently differed
         between turns that routed to different groups — invalidating the
         whole cached prefix exactly the way pack_instructions_ctx used
         to (see tests/test_prompt_cache.py).
    """
    if compact:
        if ultra:
            # Ultra (50% Capacity): tool_schema_style is already "name_only"
            # here, so the model gets almost nothing about each tool up
            # front \u2014 this blurb is the only place the essential behavioral
            # rules (confirm-before-destructive, don't guess a command name,
            # screenshots are for the user not you) survive. Cut everything
            # that's just elaboration on top of those rules.
            return (
                "Tools listed by name only \u2014 call with no args first if unsure, "
                "you'll get its schema back. Never invent a tool name \u2014 "
                "search_tools first if you don't see one you need. "
                "Confirm before install/delete/off/eval."
            )
        return (
            "Tools are listed by name only. Call one when you need it. "
            "If it needs arguments you don't know, call it with no arguments — "
            "you will get its schema, then call it again. "
            "Never invent a tool name — if you don't see one for what you need, "
            "call search_tools first. "
            "Confirm before install/delete/off/eval."
        )
    return (
        "ONLY call tools that appear in your tool list. Never invent a tool name. "
        "Confirm before launch/delete/install/eval."
    )


# Per-tool workflow guidance, keyed by the tools each clause actually talks
# about. A clause is emitted ONLY when at least one of its tools is in the
# set the router decided to offer this round.
#
# That gate is not only a token saving. This function's original docstring
# said it outright — "Groq 400s if the prompt names a tool that isn't in
# request.tools" — and the old unconditional text named a dozen tools the
# router had usually just filtered out. has_playnite/has_spotify were the
# only two clauses that ever honoured it.
#
# Each entry is (tool names, full text, compact text, ultra text). An empty
# string means "this clause doesn't exist at that verbosity", which is how
# ultra stays as lean as it was before.
_TOOL_WORKFLOW_CLAUSES = (
    (
        ("search_commands", "run_command", "run_chain"),
        "COMMANDS: the 'Saved commands' list above is only a partial preview. Before "
        "run_command or run_chain, if you aren't certain of the exact saved command "
        "name, call search_commands first — pass a keyword, or no query to list every "
        "saved command. Do this instead of guessing a name and hoping it resolves.",
        "COMMANDS: only some are listed above — if you're not sure of the exact "
        "saved command name, call search_commands (with a keyword, or no query "
        "for the full list) before run_command/run_chain. Never guess a name.",
        "Unsure of a saved command's exact name? search_commands first, never guess.",
    ),
    (
        ("radio_status", "wifi_set", "bluetooth_set"),
        "RADIOS: wifi_set/bluetooth_set action on|off. Off requires confirm=true (may need Admin).",
        "",
        "",
    ),
    (
        ("git_run", "git_commit_all"),
        "GIT: 'commit everything' / 'stage and commit' -> git_commit_all in ONE call (it stages "
        "+ commits together). Do not check status or diff first unless the user asked you to "
        "review changes or write a message based on their content — each extra git_run round "
        "resends the whole growing conversation, so status->diff->add->commit as four separate "
        "calls is expensive and usually unnecessary. For anything else, git_run with an "
        "allowlisted command (status, log, diff, add, commit, pull, push, …). "
        "reset/clean/force-push/clone need confirm=true. Not a shell.",
        "",
        "",
    ),
    (
        ("take_screenshot",),
        "SCREENSHOT: take_screenshot saves the desktop and shows it in the UI. "
        "You only get a tiny ok/path — never describe pixels or ask for the image. "
        "Confirm in one short line.",
        "Screenshots: take_screenshot (image is for the user, not you).",
        "Screenshots only give you ok/path — never describe the image.",
    ),
    (
        ("ytdl_info", "ytdl_formats", "ytdl_download"),
        "VIDEO/AUDIO: ytdl_info gets metadata (title, duration, qualities, ffmpeg_available) for a URL with no "
        "download. ytdl_formats lists exact format_ids when the simple quality presets aren't specific enough. "
        "ytdl_download fetches it (mode='video' or 'audio', quality/container/codec/subs/thumbnail/metadata/"
        "SponsorBlock all optional, output_dir to save somewhere specific) and hands the file to the user in "
        "the UI — confirm first. Single video by default; playlist=true fetches more (hard-capped), still one "
        "confirm. "
        "You only get a tiny ok/path back — never claim details about the content you weren't told.",
        "Video/audio: ytdl_info for metadata, ytdl_formats for exact format ids, "
        "ytdl_download to fetch (confirm first; single video by default, playlist=true for more, capped).",
        "Video: ytdl_info, then ytdl_formats if needed, then ytdl_download confirm=true.",
    ),
    (
        ("web_search", "web_fetch"),
        "WEB: For 'best X', news, prices, how-tos, or anything that may have changed, "
        "MUST web_search, then web_fetch 1–3 URLs, then summarize with markdown source links.",
        "Web: web_search then web_fetch.",
        "Web questions: web_search then web_fetch.",
    ),
    (
        ("package_search", "package_install"),
        "SOFTWARE INSTALL: package_search, ASK user, package_install confirm=true. Never guess ids.",
        "Install: package_search, ask, then package_install confirm=true.",
        "Installs: package_search, ask, package_install confirm=true.",
    ),
    (
        ("memory_search", "memory_save", "memory_forget"),
        "MEMORY: only facts relevant to this message are injected. If you need others, "
        "memory_search. memory_save for durable facts (prefs, names, 'remember that'). "
        "Chat history is short-term. No passwords/API keys. memory_forget to delete.",
        "",
        "",
    ),
    (
        ("spotify_search", "spotify_play", "spotify_open", "spotify_control"),
        "SPOTIFY: Free-account friendly. Do NOT use run_command. "
        "Open app: spotify_open. Play: spotify_search then spotify_play (opens the desktop app — "
        "user may need one click to play; Spotify blocks remote start on Free). "
        "Pause/skip: spotify_control (media keys). Queue/volume remote needs Premium. "
        "Never claim music started unless the tool returned ok.",
        "Spotify: spotify_search then spotify_play; never run_command.",
        "Spotify: spotify_search then spotify_play; never run_command.",
    ),
    (
        ("playnite_launch_game", "playnite_query_games", "find_game",
         "query_games", "list_game_actions", "launch_action"),
        "PLAYNITE: ALWAYS playnite_query_games WITH filters or groupBy — never dump the library. "
        "find_game is a specific title lookup. 'Play X' → playnite_launch_game (same as Play in Playnite; "
        "Steam/Epic use a virtual LibraryPlugin action). Extra launchers: list_game_actions then "
        "launch_action. Never PUT LibraryPlugin into gameActions. Never say launched unless playnite_launch_* succeeded.",
        "Playnite: find_game or query_games, then playnite_launch_game. "
        "Don't claim launch unless that tool succeeded.",
        "Playnite: find_game/query_games then playnite_launch_game; "
        "don't claim launch unless it succeeded.",
    ),
)

# Prefix matches, for tool families whose members are generated rather than
# listed (playnite_*, spotify_*). Mirrors what has_playnite/has_spotify did
# with startswith() before this table existed.
_WORKFLOW_PREFIXES = ("playnite_", "spotify_")


def _clause_applies(tool_names, offered_names):
    """Is any tool this clause talks about actually on offer this round?"""
    for name in tool_names:
        if name in offered_names:
            return True
    # A prefix family counts as present if ANY of its members is offered,
    # so a clause listing three playnite tools still fires when the router
    # offered a fourth one this table doesn't name.
    for prefix in _WORKFLOW_PREFIXES:
        if any(n.startswith(prefix) for n in tool_names):
            if any(n.startswith(prefix) for n in offered_names):
                return True
    return False


def _tool_workflow_notes(offered_names, compact, ultra,
                         has_playnite=False, has_spotify=False):
    """Per-tool workflow guidance for THIS round's offered tools only.

    Goes in the per-request tail next to pack_instructions_ctx, not the
    cached static prefix, because it varies with the router's decision —
    same rule, same reason. See _tools_blurb's docstring.

    `offered_names` is the set of tool names ai_client is actually sending
    this round. Passing None restores the old unconditional behaviour
    exactly (every clause, with Playnite/Spotify still gated on the two
    legacy flags), so a caller that doesn't know its offered set — a test,
    an external caller of _system_prompt() — reads the same text it always
    did rather than silently losing guidance.
    """
    index = 1 if not compact else (3 if ultra else 2)
    legacy = offered_names is None
    parts = []
    for tool_names, *texts in _TOOL_WORKFLOW_CLAUSES:
        text = texts[index - 1]
        if not text:
            continue
        if legacy:
            is_spotify = any(n.startswith("spotify_") for n in tool_names)
            is_playnite = any(n.startswith("playnite_") or n in
                              ("find_game", "query_games", "list_game_actions",
                               "launch_action")
                              for n in tool_names)
            if is_spotify and not has_spotify:
                continue
            if is_playnite and not has_playnite:
                continue
        elif not _clause_applies(tool_names, offered_names):
            continue
        parts.append(text)
    return " ".join(parts)


def _system_prompt(*args, **kwargs):
    """The whole system prompt as one string — unchanged public behavior.

    Kept as a thin join over _system_prompt_parts() so every existing caller
    and test reads the same text it always did, while _build_messages() can
    reach for the two halves separately.
    """
    static, dynamic = _system_prompt_parts(*args, **kwargs)
    return "\n\n".join(p for p in (static, dynamic) if p)


def _system_prompt_parts(persona, commands_ctx, freq_ctx, tools_enabled,
                         compact_tools=False, compact_persona=False, ultra=False, has_history=False,
                         memory_ctx="", has_playnite=False, has_spotify=False, other_convos_ctx="",
                         playnite_freq_games=None, precise=False, pack_instructions_ctx="",
                         skills_ctx="", loaded_skills_ctx="", sender_ctx="",
                         offered_names=None):
    """Return (static_prefix, per_request_tail) instead of one joined string.

    This split is the load-bearing half of prompt caching (see
    prompt_cache.py). Every provider jarvis talks to caches a byte-PREFIX of
    the request, so anything that changes early in the prompt invalidates
    everything after it. jarvis's system prompt mixes both kinds of content:

      STATIC — persona, the history nudge, the tools blurb, the router's
        pack instructions, the precision directive. Identical across every
        turn that lands on the same capacity mode and router group.

      PER-REQUEST — memory context (keyed on the user's message, so it is
        different on literally every turn), the other-conversations context,
        the Playnite frequent-games block, saved-commands listing, and
        frequency stats.

    Before this split they were interleaved and joined, which put
    query-dependent text in front of static text and made the prefix differ
    on every single turn. No amount of cache_control markers can rescue that
    — the bytes genuinely differ. Separating them lets the breakpoint sit at
    the end of the static run, so the tail changes freely without touching
    what is cached in front of it.

    Ordering note: the parts are emitted in the SAME order as before, so the
    joined text is byte-identical to what _system_prompt() used to return.
    `extra` (the persona's extra_instructions) is static and would cache
    slightly better if hoisted into the prefix, but moving it would change
    the prompt the model sees, and a token optimization is not worth an
    unmeasured behavior change. It stays in the tail.
    """
    name = persona.get("assistant_name") or DEFAULT_ASSISTANT_NAME
    address = persona.get("address_user_as") or DEFAULT_ADDRESS
    extra = (persona.get("extra_instructions") or "").strip()
    attitude = ATTITUDE_PRESETS.get(persona.get("attitude"), ATTITUDE_PRESETS[DEFAULT_ATTITUDE])

    parts = []
    if compact_persona:
        if ultra:
            # Ultra (50% Capacity): trims the compact persona line further —
            # attitude flavor is not a rule the model needs to be told
            # explicitly to follow; everything else here is load-bearing
            # (name, how to address the user, don't claim unconfirmed
            # actions). This holds regardless of which attitude preset is
            # selected — not just for the "dry" default.
            parts.append(
                f"You are {name}, a local AI butler. Address the user as "
                f'"{address}" sometimes. Never claim you did something unless a tool confirmed it.'
            )
        else:
            parts.append(
                f"You are {name}, a local AI butler. {attitude['compact']} Address the user as "
                f'"{address}" sometimes. Never claim you did something unless a tool confirmed it.'
            )
    else:
        parts.append(
            f"You are {name}, a private AI assistant running locally for one user on their own "
            f"computer \u2014 think a supremely capable, unflappable AI butler: {attitude['full']}. "
            f'Address the user as "{address}" sometimes, naturally \u2014 not in every single '
            f"sentence. Keep replies conversational and to the point: a sentence or two for "
            f"anything simple, more only when the question genuinely calls for it. Be honest "
            f"about your limits. Never claim to have taken an action you didn't actually take."
        )
    if has_history:
        if compact_persona:
            if ultra:
                parts.append("Earlier messages are real — continue that thread.")
            else:
                parts.append(
                    "Earlier messages in this chat are real — continue that thread. "
                    "If the user is answering your questions, proceed with what they asked for."
                )
        else:
            parts.append(
                "The conversation history before the latest user message is real — continue that "
                "thread naturally. If their latest message answers questions you asked, use those "
                "answers and move forward with their original request. Do not pretend the prior "
                "turns never happened."
            )
    if tools_enabled:
        # Static half only — the per-tool workflow clauses moved to the
        # tail below, since they depend on what the router offered this
        # turn. See _tools_blurb / _tool_workflow_notes.
        parts.append(_tools_blurb(compact_tools, ultra))
        # Tier 1 of the skills system (see skills.py). Deliberately inside
        # the STATIC run: the catalog is identical on every turn and only
        # changes when a skill is added or removed, which makes it exactly
        # the kind of content the cached prefix is for. Putting it in the
        # per-request tail instead would cost its full price on every ask.
        if skills_ctx:
            parts.append(skills_ctx)
        # pack_instructions_ctx is NOT appended here — see the boundary
        # below. It's built from route.groups, which is per-turn, so it
        # belongs in the dynamic tail. It used to sit here, which meant a
        # conversation whose messages route to different tool groups from
        # turn to turn invalidated the ENTIRE static block's cache on every
        # such turn — a marked content block caches as a whole; one byte
        # difference anywhere inside it is a miss for the whole block, not
        # a partial hit (see prompt_cache.py's module docstring).
    if precise:
        # 150% Capacity only: an extra directive on top of the normal
        # persona/tools text \u2014 not a replacement for either.
        parts.append(
            "Precision mode: state exact values, file paths, and tool results "
            "verbatim rather than paraphrasing them. If a request is ambiguous "
            "or you're not certain of a fact, say so explicitly instead of "
            "guessing \u2014 verify with a tool when one is available rather than "
            "assuming. Prefer being exactly right over being quick."
        )
    # ---- end of the static prefix -------------------------------------
    # Everything appended above is stable for a given capacity mode — it no
    # longer includes anything keyed to which router group matched THIS
    # turn (see the note above pack_instructions_ctx's old spot). Everything
    # below varies per request or per conversation. The breakpoint goes here.
    static_parts = list(parts)
    parts = []

    # WHO IS TYPING. First in the tail, ahead of memory_ctx, deliberately:
    # everything below is written as though the owner is on the other end,
    # and this is the line that says they might not be (see
    # channels/people.py). A model that reads "remember: their sister's
    # birthday is Tuesday" before being told it's talking to a stranger has
    # already been primed with the wrong frame.
    #
    # Tail rather than static prefix even though it's stable within one
    # chat thread: it differs per conversation, and the static run is
    # shared across every conversation in a capacity mode (see the
    # breakpoint note above). Empty for the CLI and the web UI, which keeps
    # every non-chat ask byte-identical to before this existed.
    if sender_ctx:
        parts.append(sender_ctx)
    # Per-tool workflow guidance for the tools actually on offer this round.
    # Sits with pack_instructions_ctx because it has exactly the same
    # property: derived from the router's per-turn decision, so it must not
    # touch the cached prefix.
    if tools_enabled:
        workflow_ctx = _tool_workflow_notes(
            offered_names, compact_tools, ultra,
            has_playnite=has_playnite, has_spotify=has_spotify)
        if workflow_ctx:
            parts.append(workflow_ctx)
    if pack_instructions_ctx:
        parts.append(pack_instructions_ctx)
    # Manually-loaded skills (see skill_stickiness.py — the "/skillload
    # <name>" chat command or `jarvis skillload`). Conversation-scoped, not
    # turn-scoped: stable across every ask in one chat until unloaded, but
    # not identical across different chats, so it belongs alongside
    # memory_ctx here rather than in the globally-static run above.
    if loaded_skills_ctx:
        parts.append(loaded_skills_ctx)
    if memory_ctx:
        parts.append(memory_ctx)
    if other_convos_ctx:
        parts.append(other_convos_ctx)
    if playnite_freq_games is None:
        fallback_mode = "compact" if compact_persona else "full"
        playnite_freq_games = _MODE_BY_NAME[fallback_mode]["playnite_freq_games"]
    # playnite_freq_games == 0 means "omit this block entirely" for the
    # current mode (100%/50% capacity) — frequent_games_context treats a
    # falsy max_games as "use the configured default", so we must not call
    # it at all in that case rather than passing 0 through.
    playnite_ctx = (
        playnite_config.frequent_games_context(
            playnite_freq_games,
            compact=compact_persona,
        )
        if playnite_freq_games
        else ""
    )
    if playnite_ctx:
        parts.append(playnite_ctx)
    if extra:
        parts.append(extra)
    if commands_ctx:
        parts.append(commands_ctx)
    if freq_ctx:
        parts.append(freq_ctx)
    return "\n\n".join(static_parts), "\n\n".join(parts)


def _build_messages(persona, commands, user_text, tools_enabled, profile, conversation_id, route=None,
                    sender_ctx=""):
    compact = profile.get("compact_tools_blurb", False)
    # Phase 8 of the token-optimization plan (see new_plan.md): the full
    # saved-commands listing only earns its tokens when the "commands"
    # tool group is actually in play. When the router is confident about a
    # *different* group, search_commands/run_command/etc. aren't even
    # being offered this round (see ask()'s active_schemas) — so a dozen
    # inlined command names+descriptions would be pure overhead with no
    # tool available to act on them anyway. Stay unconditional (the exact
    # prior behavior) whenever the router has no opinion or "commands" is
    # itself one of the matched groups, since that's the safe/no-regression
    # case this phase must not touch.
    skip_commands_listing = bool(route) and route.confident and "commands" not in route.groups
    commands_ctx = "" if skip_commands_listing else _commands_context(
        commands,
        max_listed=profile["max_commands"],
        desc_max_len=profile["desc_max_len"],
        compact=compact,
    )
    freq_ctx = stats.frequent_commands_context(commands) if profile["include_freq"] else ""
    prior_turns = conversations.conversation_messages(
        conversation_id,
        max_exchanges=profile["history_exchanges"],
        char_budget=profile["history_char_budget"],
        recap_exchanges=profile.get("recap_exchanges"),
        recap_budget=profile.get("recap_budget"),
    )
    compact_persona = profile.get("compact_persona", False)
    # tool_schema_style is only "name_only" for the ultra profile today, so
    # it doubles as the ultra flag here rather than adding a new knob just
    # for this — see PROMPT_MODE_DEFS' knob list.
    ultra = profile.get("tool_schema_style") == "name_only"
    prior_user = [
        (m.get("content") or "")
        for m in prior_turns
        if (m.get("role") == "user" and (m.get("content") or "").strip())
    ][-2:]
    memory_ctx = memory.prompt_context(
        compact=compact_persona,
        query=user_text or "",
        extra_texts=prior_user,
    )
    other_convos_ctx = (
        "" if profile.get("skip_other_convos")
        else conversations.other_conversations_context(conversation_id)
    )
    # Bugfix: this used to always call tool_schemas_for_session() (the
    # full per-session catalog) regardless of `route`, so has_playnite/
    # has_spotify below were computed from what *could* be offered rather
    # than what this round's router-filtered active_schemas actually
    # offers (see ask()'s active_schemas selection, which this mirrors).
    # Net effect of the bug: whenever the router narrowed to some other
    # group (e.g. "desktop"), the playnite/spotify blurb text still got
    # included every time, since tool_schemas_for_session() always
    # includes those — quietly defeating the token-optimization this
    # phase is for.
    if tools_enabled:
        if route is not None and route.confident:
            offered = system_tools.schemas_for_tools(route.tools)
        elif route is not None:
            offered = list(system_tools.DISCOVERY_AND_COMMANDS_SCHEMAS)
        else:
            offered = system_tools.tool_schemas_for_session()
    else:
        offered = []
    offered_names = {s["name"] for s in offered}
    # Phase 6 of the token-optimization plan (see new_plan.md): inject
    # TOOL_PACK_INSTRUCTIONS only for the groups the router actually
    # activated this turn, instead of _tools_blurb() baking every
    # subsystem's workflow guidance into every prompt unconditionally.
    # Only meaningful once the router is confident (route.groups is empty
    # otherwise) — when it isn't, active_schemas already fell back to the
    # small search_tools-only offering (Phase 5), so there's no group-
    # specific guidance to add here either way.
    pack_instructions_ctx = ""
    if route is not None and route.confident:
        from . import tool_registry
        pack_lines = [
            tool_registry.pack_instruction(group)
            for group in route.groups
            if tool_registry.pack_instruction(group)
        ]
        pack_instructions_ctx = " ".join(pack_lines)
    elif route is not None and tools_enabled:
        # Phase 5 fallback (see new_plan.md): the router had no opinion, so
        # only the tiny search_tools discovery schema is being offered this
        # round (see active_schemas below) instead of the full catalog.
        # Without an explicit nudge here, a model that doesn't already
        # "know" search_tools exists tends to just say it lacks whatever
        # capability was asked for, rather than calling search_tools to
        # check first — which is the whole point of this discovery tool.
        pack_instructions_ctx = (
            "Before telling the user you don't have a tool for something, "
            "call search_tools with a keyword for it — many tools aren't "
            "listed above and only appear after that search. If the "
            "request sounds like running something the user already set "
            "up (a saved routine/command) rather than a built-in "
            "capability, try search_commands instead — don't just keep "
            "retrying search_tools with different keywords."
        )
    # The skills catalog is one line per installed skill and nothing else —
    # the instructions themselves stay on disk until load_skill is called.
    # Wrapped because a skills directory that can't be read must degrade to
    # "no skills" rather than take down every ask.
    try:
        skills_ctx = skills.catalog_text()
    except Exception:
        skills_ctx = ""

    # Manually-loaded skills for this conversation (see skill_stickiness.py)
    # — the "/skillload <name>" chat command / `jarvis skillload` CLI
    # command. Same wrapping reasoning as skills_ctx above.
    try:
        loaded_skills_ctx = skill_stickiness.loaded_context(conversation_id)
    except Exception:
        loaded_skills_ctx = ""

    static_system, dynamic_system = _system_prompt_parts(
        persona,
        commands_ctx,
        freq_ctx,
        tools_enabled,
        compact_tools=compact,
        compact_persona=compact_persona,
        ultra=ultra,
        has_history=bool(prior_turns),
        memory_ctx=memory_ctx,
        has_playnite=any(n.startswith("playnite_") for n in offered_names),
        has_spotify=any(n.startswith("spotify_") for n in offered_names),
        other_convos_ctx=other_convos_ctx,
        playnite_freq_games=profile.get("playnite_freq_games"),
        precise=profile.get("precise_persona", False),
        pack_instructions_ctx=pack_instructions_ctx,
        skills_ctx=skills_ctx,
        loaded_skills_ctx=loaded_skills_ctx,
        sender_ctx=sender_ctx,
        offered_names=offered_names if tools_enabled else None,
    )
    # Two system messages, not one: index 0 is the cacheable static prefix,
    # index 1 the per-request tail (see _system_prompt_parts). Providers that
    # can act on the boundary read it via ai_providers._system_parts();
    # every other provider gets them folded back into a single system message
    # by ai_providers._merge_system(), which joins with the same "\n\n" this
    # function's parts already use — so the text those providers receive is
    # byte-identical to the single-block prompt this replaced.
    messages = [{"role": "system", "content": static_system}]
    if dynamic_system:
        messages.append({"role": "system", "content": dynamic_system})
    messages.extend(prior_turns)
    messages.append({"role": "user", "content": user_text})
    return messages


def _cache_key(name, arguments):
    try:
        args_s = json.dumps(arguments or {}, sort_keys=True, default=str)
    except TypeError:
        args_s = str(arguments)
    return f"{name}:{args_s}"


def _schema_required(schema):
    params = (schema or {}).get("parameters") or {}
    required = params.get("required") or []
    return [k for k in required if isinstance(k, str)]


def _missing_required(schema, arguments):
    arguments = arguments or {}
    missing = []
    for key in _schema_required(schema):
        value = arguments.get(key)
        if value is None or value == "":
            missing.append(key)
    return missing


# Risk-note length/detail and its token ceiling, keyed by the same
# tool_result_verbosity a mode's PROMPT_MODE_DEFS entry already declares
# (full/medium/low) \u2014 reuses the existing capacity-mode vocabulary instead
# of inventing a parallel one, without touching how any *other* prompt in
# this file is built.
_RISK_REVIEW_VERBOSITY_PROFILES = {
    "full": {
        "sentence_count": "4-6",
        "extra_instruction": " Include any edge cases or side effects worth knowing about.",
        "max_tokens": 400,
    },
    "medium": {
        "sentence_count": "2-3",
        "extra_instruction": "",
        "max_tokens": 200,
    },
    "low": {
        "sentence_count": "1",
        "extra_instruction": " Be as brief as possible \u2014 the rating and nothing else.",
        "max_tokens": 80,
    },
}


def risk_review(tool_name, arguments, cfg, exclude_label=None, mode=None):
    """Ask a *different* configured AI provider than the one currently
    answering to explain, in plain language, what a tool call will do and
    how dangerous/irreversible it is. Best-effort only: never raises, and
    returns None (no risk note attached) if no other provider is
    configured or the call fails \u2014 a missing/misconfigured second provider
    should never block or crash the primary ask, it just means the
    confirmation prompt won't have an AI opinion attached.

    `mode`, if given, is one of PROMPT_MODES (e.g. from a one-off local
    override, not necessarily the config's real defaults.prompt_mode) and
    only ever scales *this* prompt's requested length and token ceiling via
    _RISK_REVIEW_VERBOSITY_PROFILES above \u2014 it never reads or writes
    defaults.prompt_mode, so it can't affect the real global capacity mode
    or any other prompt built elsewhere in this file. Anything not in
    PROMPT_MODES (including None) falls back to the same "medium" profile
    this function always used before `mode` existed.
    """
    try:
        providers = _eligible_providers(cfg["providers"], cfg["defaults"])
        candidates = [p for p in providers if exclude_label is None or _provider_label(p) != exclude_label]
        if not candidates:
            return None
        provider = candidates[0]
        adapter = ai_providers.ADAPTERS.get(provider.get("type"))
        if adapter is None:
            return None
        keys = ai_config.provider_keys(provider) or [None]
        resolved = _resolve(provider, cfg["defaults"])
        if keys[0] is not None:
            resolved["api_key"] = keys[0]

        mode_profile = _MODE_BY_NAME.get(mode) if mode in PROMPT_MODES else None
        verbosity = (mode_profile or {}).get("tool_result_verbosity", "medium")
        review_profile = _RISK_REVIEW_VERBOSITY_PROFILES.get(verbosity, _RISK_REVIEW_VERBOSITY_PROFILES["medium"])
        if mode_profile is not None:
            resolved["max_tokens"] = review_profile["max_tokens"]

        try:
            args_s = json.dumps(arguments or {}, default=str)
        except TypeError:
            args_s = str(arguments)
        prompt = (
            "A personal-assistant program is about to run this tool call on the "
            "user's own machine:\n"
            f"  tool: {tool_name}\n"
            f"  arguments: {args_s}\n\n"
            f"In {review_profile['sentence_count']} short plain-language sentence(s): "
            "(1) explain exactly what this specific call will do, and (2) rate how "
            "dangerous/irreversible it is (none / low / medium / high) with a "
            f"one-line reason.{review_profile['extra_instruction']} No preamble, no "
            "markdown, just the assessment."
        )
        messages = [{"role": "user", "content": prompt}]
        result = adapter(resolved, messages, resolved.get("timeout", DEFAULT_TIMEOUT),
                          tools=None, tool_executor=None)
        if result.ok and result.text:
            return {"provider": _provider_label(provider), "note": result.text.strip()}
        return None
    except Exception:
        return None


def _command_flags_for_call(name, arguments):
    """create_command/update_command let the AI set a saved command's own
    confirm_required/ai_review flags (see command_tools.py's schemas) —
    this surfaces those two booleans, exactly as the AI is about to save
    them, so on_confirm_request's caller can show "Flags: confirm_required=
    True, ai_review=False" on the confirmation prompt instead of the user
    only finding out by opening the Debug dashboard afterward. Returns
    None for any other tool name (nothing to attach)."""
    if name not in ("create_command", "update_command"):
        return None
    arguments = arguments or {}

    base_confirm, base_review = False, False
    if name == "update_command":
        # Start from the command's *current* flags — update_command only
        # touches confirm_required/ai_review when the AI explicitly passes
        # them (see command_tools.tool_update_command), so a call that
        # only changes e.g. `run` must still reflect the flags the command
        # already has, not silently report them as False.
        try:
            from . import commands_config
            existing = commands_config.load_commands_dict().get(arguments.get("name"))
        except Exception:
            existing = None
        if isinstance(existing, dict):
            base_confirm = bool(existing.get("confirm_required"))
            base_review = bool(existing.get("ai_review"))

    confirm_required = arguments.get("confirm_required")
    ai_review_flag = arguments.get("ai_review")
    return {
        "confirm_required": base_confirm if confirm_required is None else bool(confirm_required),
        "ai_review": base_review if ai_review_flag is None else bool(ai_review_flag),
    }


# See _make_tool_executor's repeat-failure tracking below: a search_tools
# miss or a run_command/run_chain that couldn't resolve the name both count
# as one "failed lookup". After this many in a single ask(), search_commands
# gets forced into the offered set even if the router was confidently
# pointed at the wrong group the whole time (Part A's DISCOVERY_AND_
# COMMANDS_SCHEMAS fallback only helps when the router had no opinion at
# all — a *wrong but confident* route needs this separate safety net).
_FAILED_LOOKUP_THRESHOLD = 3

# Enhancement #5 (see jarvis-token-optimization-enhancements.md): the
# tool/command lookup case above used to be the only repeat-failure pattern
# tracked, via a single-purpose (_failed_lookups, _commands_surfaced) pair.
# This registry generalizes that into an ordered list of detectors so other
# repeat-failure patterns (e.g. click_on_text missing the same text three
# times in a row) get the same safety net without a bespoke counter each.
#
# Each entry is (predicate, kind_fn, threshold, corrective):
#   predicate(name, arguments, result) -> bool
#       True if this tool call, given its result, counts as one instance
#       of this failure kind.
#   kind_fn(name, arguments) -> str
#       The counter key for this instance. Usually a constant label, but
#       can fold in an argument (e.g. the `text` click_on_text was given)
#       so unrelated instances of a pattern don't share one counter.
#   threshold -> int
#       How many instances of this kind (in this single ask()) before the
#       corrective fires.
#   corrective -> list[str]
#       Tool names handed to discover_sink once threshold is hit — same
#       plumbing search_tools hits already use, so the corrective group is
#       actually callable next round, not just implied.
#
# The tool/command entry below preserves the exact existing behavior (same
# threshold, same corrective) as one entry in this list.
_REPEAT_FAILURE_DETECTORS = [
    (
        lambda name, arguments, result: (
            (name == "search_tools" and not result.get("matches"))
            or (name in ("run_command", "run_chain") and result.get("needs_clarification"))
        ),
        lambda name, arguments: "tool_or_command",
        _FAILED_LOOKUP_THRESHOLD,
        ["search_commands"],
    ),
    (
        # click_on_text found nothing (no "clicked" key at all) or found an
        # ambiguous/inspect-only match (explicit "clicked": False) for the
        # *same* text three times running — surface list_windows/
        # focus_window as a corrective path (a no-op if the desktop group,
        # which already includes them alongside click_on_text, is active).
        lambda name, arguments, result: (
            name == "click_on_text" and result.get("clicked") is False
        ),
        lambda name, arguments: f"click_on_text_miss:{(arguments or {}).get('text')}",
        3,
        ["list_windows"],
    ),
]


def _make_tool_executor(on_tool_call, schemas=None, on_confirm_request=None,
                         cfg=None, provider_ref=None, verbosity_ref=None,
                         discover_sink=None, cache_query=None,
                         round_budget=None, conv_id=None):
    """Shared across every provider/key in one ask() so a failover never
    re-runs the same command, Playnite action, or web fetch. Cache hits
    still return the original result (no second launch / install / HTTP).

    First call with missing required args returns the compact schema
    instead of running the tool (lazy tool summaries).

    Before a tool flagged confirm_required (see tool_safety.py) actually
    runs, on_confirm_request(name, arguments, risk_note) is called and must
    return True/False \u2014 the tool is only executed on True. If ai_review is
    also on for that tool, risk_note is a {"provider", "note"} dict from a
    *different* configured provider (see risk_review) explaining what the
    call does and how dangerous it is; otherwise risk_note is None. With no
    on_confirm_request supplied at all, a tool requiring confirmation fails
    closed (never silently runs unconfirmed).

    verbosity_ref, if given, is a one-element list read fresh on every call
    (["full"] by default) whose current value picks how much of a
    successful result survives before it's cached/returned \u2014 see
    tool_result_shaping.shape_result(). A mutable holder (not a plain
    argument) because one executor is shared across every provider in a
    single ask() for failover, and each provider attempt resolves its own
    prompt profile (see ask()'s main loop), same pattern as provider_ref.

    cache_query, if given, is the normalized user_text for this ask() call
    (see discovery_cache.py) — a successful search_tools/search_commands
    hit is stored under it so a similar query in a *later* jarvis process
    can pre-seed active_schemas without repeating the same discovery round
    trip. Purely additive: with cache_query left None, nothing is stored.

    round_budget, if given, is the ask()-level ai_providers.RoundBudget
    shared across every provider/key attempt this turn (see ask()) — it's
    wrapped in a ToolContext and handed to any tool handler whose
    signature accepts one (tools.py's _accepts_context), so a bounded,
    self-correcting tool like dev_agent can size its own internal retry
    loop against what's actually left in the shared pool instead of a
    hardcoded constant that can outlive the budget. conv_id is likewise
    threaded through so a handler can scope its own output/bookkeeping to
    the active conversation. Both default to a safe fallback (a
    remaining()-like lambda returning 1, and None) when omitted, so every
    existing caller of _make_tool_executor keeps working unchanged.
    """
    cache = {}
    runs = []
    by_name = {
        s.get("name"): s
        for s in (schemas or [])
        if isinstance(s, dict) and s.get("name")
    }
    # Generalized repeat-failure tracking (enhancement #5) — replaces the
    # old single-purpose (_failed_lookups int, _commands_surfaced bool)
    # pair with a per-kind counter dict and a per-kind surfaced set, so one
    # failure pattern hitting its threshold doesn't interfere with another's
    # count. See _REPEAT_FAILURE_DETECTORS above for the active patterns.
    _repeat_failures = {}
    _surfaced = set()

    def _executor(name, arguments):
        arguments = arguments or {}
        key = _cache_key(name, arguments)
        if key in cache:
            # Signal the cache hit to ai_providers._call_tool_safely so it
            # doesn't re-log/re-estimate token usage or append a duplicate
            # tool_usage entry for a tool that didn't actually run again —
            # this executor is shared across every provider/key failover
            # in one ask() specifically so cached results are reused
            # instead of re-run; the usage accounting needs to honor that
            # same reuse, not just the side effects.
            _executor._cache_hit = True
            return cache[key]
        _executor._cache_hit = False
        schema = by_name.get(name)
        if schema is not None:
            missing = _missing_required(schema, arguments)
            if missing:
                compact = system_tools.compact_schemas_for_prompt([schema])
                result = {
                    "need_args": True,
                    "missing": missing,
                    "schema": compact[0] if compact else {"name": name},
                    "hint": "Call this tool again with the parameters in schema.",
                }
                cache[key] = result
                runs.append({"name": name, "arguments": arguments, "result": result})
                return result

        # Tool-level gate (tool_safety.json, keyed by tool name) is OR'd with
        # the per-*saved-command* flags on run_command/run_chain (see
        # command_tools.command_call_requires_confirmation) so "warn on
        # deploy-prod but not on list-files" works even though both go
        # through the same run_command tool.
        # A command can be flagged ai_review=True with confirm_required
        # left False (the user wants a heads-up note, not a hard gate).
        # Previously this whole block — including the ai_review check
        # inside it — only ran when confirm was independently required,
        # so an ai_review-only command silently ran with no note shown
        # anywhere (chat bubble or debug popup) whenever the tool-level
        # confirm_required also happened to be off. OR'ing ai_review in
        # here means "needs review" now reliably routes through the same
        # notification channel as "needs confirmation".
        confirm_meta = None
        # D6: a small, fixed allow-list of genuinely read-only run_shell
        # commands (dir, type, where, echo — see tool_safety's
        # is_allowlisted_read_only_shell docstring) skips confirm/ai_review
        # entirely, so the same 'dir' doesn't cost a prompt AND a Groq
        # risk-review call every single time it's asked for (F.12 evidence:
        # one such confirm cost 32s of the user's time). Deliberately its
        # own top-of-block check, isolated from the general gate below —
        # confirm gating is AGENTS.md-protected, so this stays a narrow,
        # exact allow-list rather than folding into any broader condition.
        if name == "run_shell" and tool_safety.is_allowlisted_read_only_shell(
                arguments.get("command") if isinstance(arguments, dict) else None):
            pass
        elif (tool_safety.requires_confirmation(name)
                or command_tools.command_call_requires_confirmation(name, arguments)
                or tool_safety.requires_ai_review(name)
                or command_tools.command_call_requires_ai_review(name, arguments)):
            if on_confirm_request is None:
                result = {
                    "ok": False, "cancelled": True,
                    "message": f"'{name}' requires user confirmation, but no confirmation "
                               f"channel is available here \u2014 not run.",
                }
                cache[key] = result
                runs.append({"name": name, "arguments": arguments, "result": result})
                return result

            risk_note = None
            if (tool_safety.requires_ai_review(name)
                    or command_tools.command_call_requires_ai_review(name, arguments)) and cfg is not None:
                exclude_label = (provider_ref or [None])[0]
                # For run_command/run_chain, hand risk_review the resolved
                # shell steps (vars expanded) instead of just the command
                # label, so the second AI's opinion is about the real
                # commands being run, not a guess based on the name.
                review_arguments = arguments
                if name in ("run_command", "run_chain"):
                    expanded = command_tools.resolved_run_for_review(name, arguments)
                    if expanded is not None:
                        review_arguments = expanded
                risk_note = risk_review(name, review_arguments, cfg, exclude_label=exclude_label)

            # Always show the actual resolved command content for
            # run_command/run_chain — not just the tool name and the
            # saved-command name/vars the AI passed — regardless of
            # whether ai_review produced a plain-language note above, so
            # the user sees the real shell steps before approving, same
            # as the direct-run popup (see cli.py's confirm_direct_command).
            if name in ("run_command", "run_chain"):
                command_run = command_tools.resolved_run_for_review(name, arguments)
                if command_run is not None:
                    risk_note = dict(risk_note) if isinstance(risk_note, dict) else (
                        {"note": risk_note} if risk_note else {}
                    )
                    risk_note["command_run"] = command_run

            # For create_command/update_command specifically, always show
            # the user the resulting command's own confirm_required/
            # ai_review flags on the confirmation prompt — regardless of
            # whether ai_review produced a risk note above — so they see
            # exactly what safety behavior the command they're about to
            # create/change will have going forward, not just a generic
            # danger rating for the create/update call itself.
            command_flags = _command_flags_for_call(name, arguments)
            if command_flags is not None:
                risk_note = dict(risk_note) if isinstance(risk_note, dict) else (
                    {"note": risk_note} if risk_note else {}
                )
                risk_note["command_flags"] = command_flags

            if name == "dev_agent":
                # §3.6 plan §8. Can't resolve the actual plan (files/deps/
                # run command) at confirm-time — planning hasn't run yet,
                # it only runs after this approval. So unlike run_command/
                # run_chain above, there's no "resolved content" to attach
                # here; the confirmation prompt necessarily just describes
                # the raw ask itself. A second, lighter-weight confirmation
                # between plan and write could be added later (see the
                # plan's §11 open questions), but the first cut asks once,
                # up front. No risk_note augmentation beyond whatever the
                # default ai_review path above already produced.
                pass

            approved = False
            try:
                approved = bool(on_confirm_request(name, arguments, risk_note))
            except TypeError:
                approved = bool(on_confirm_request(name, arguments))
            if not approved:
                result = {"ok": False, "cancelled": True,
                          "message": f"The user declined to run '{name}' \u2014 do not retry it "
                                     f"this turn, and don't claim it happened."}
                cache[key] = result
                # Keep the risk_note + decision so the web UI can persist
                # and replay this exact confirmation prompt later (see
                # conversations.append_exchange's `extras` and
                # web/public/app.js's renderResolvedConfirmBubble) instead
                # of it only existing for as long as the browser tab does.
                runs.append({"name": name, "arguments": arguments, "result": result,
                             "confirm": {"risk_note": risk_note, "approved": False}})
                return result
            # Approved — recorded below alongside the actual tool result so
            # a single `runs` entry carries both the confirmation and what
            # it let through.
            confirm_meta = {"risk_note": risk_note, "approved": True}

        if on_tool_call:
            try:
                on_tool_call(name, arguments)
            except TypeError:
                on_tool_call(name)
        context = system_tools.ToolContext(
            conv_id=conv_id,
            round_budget_remaining=(round_budget.remaining if round_budget else (lambda: 1)),
            emit_event=_dev_agent_events.emit,
            ui=os.environ.get("JARVIS_UI", "cli"),
        )
        result = system_tools.execute_tool(name, arguments, context=context)
        verbosity = verbosity_ref[0] if verbosity_ref else "full"
        # §3.6 plan §7's flagged checkpoint, resolved: run_entry (which
        # feeds _extras_from_runs -> conversations.append_exchange's
        # persisted `extras`, i.e. what a reloaded page replays) must keep
        # the FULL, pre-shaping result -- shape_result trims fields like
        # dev_agent's per-step preview/stdout_tail/stderr_tail that the UI
        # still needs for replay even though the model doesn't need them
        # repeated back into its own context every round. Only
        # cache[key] (reused on a same-turn repeat call) and the
        # model-facing return value get the shaped copy.
        shaped_result = tool_result_shaping.shape_result(name, result, verbosity)
        # "Explain what broke": a failed tool gets a plain-language cause
        # and the actual fix attached, matched against the same dependency
        # tables `jarvis doctor` uses (see tool_diagnosis.py). Applied to
        # the SHAPED copy only — the model is the audience for the
        # explanation, while run_entry below deliberately keeps the
        # untouched result for UI replay. Returns its input unchanged when
        # the call succeeded or nothing is known about the error, so the
        # happy path is byte-identical to before.
        shaped_result = tool_diagnosis.annotate(name, shaped_result)
        cache[key] = shaped_result
        run_entry = {"name": name, "arguments": arguments, "result": result}
        if confirm_meta is not None:
            run_entry["confirm"] = confirm_meta
        runs.append(run_entry)
        result = shaped_result

        # Phase 5 handoff (see new_plan.md): a successful search_tools call
        # hands its matches to discover_sink so ask() can grow this round's
        # active/compact/name_only_schemas in place \u2014 the match is then
        # actually offered (and callable) on the *next* round, rather than
        # just described in this tool's own reply and then unreachable.
        if name == "search_tools" and discover_sink and isinstance(result, dict):
            matches = result.get("matches") or []
            found = [m.get("name") for m in matches if isinstance(m, dict) and m.get("name")]
            if found:
                discover_sink(found)
                # Phase 2 of the enhancements doc: remember this hit under
                # the message that triggered it, so a similar message in a
                # *later* jarvis process (a fresh OS process every time —
                # see history.py) can skip the round trip entirely.
                if cache_query:
                    discovery_cache.cache_store(cache_query, "tools", found)

        # search_commands's own matches are SAVED COMMAND names (e.g.
        # "deploy-prod"), not tool names — they can't be fed to discover_sink
        # directly the way search_tools' matches are. But finding at least
        # one means the model is about to want run_command/run_chain next,
        # and those weren't necessarily offered yet (search_commands can now
        # be called directly from turn one — see DISCOVERY_AND_COMMANDS_
        # SCHEMAS — with no search_tools call in between to trigger the hook
        # above). Passing any one commands-group tool name activates the
        # whole group the same way search_tools' hits do, immediately
        # making run_command/etc. callable next round instead of costing a
        # whole extra round trip to find that out.
        if name == "search_commands" and discover_sink and isinstance(result, dict):
            if result.get("matches"):
                discover_sink(["run_command"])
                # Commands are already found — mark the tool/command
                # repeat-failure kind surfaced too, so the detector below
                # doesn't keep counting (and eventually re-fire) toward a
                # corrective that already happened via this direct hit.
                _surfaced.add("tool_or_command")
                # Same idea as the search_tools case above, kind="commands"
                # so a cache_lookup for a plain tool hit never gets handed
                # the commands group by mistake.
                if cache_query:
                    discovery_cache.cache_store(cache_query, "commands", ["run_command"])

        # Generalized repeat-failure tracking (enhancement #5 — see
        # _REPEAT_FAILURE_DETECTORS above). Each detector counts its own
        # kind independently, so an unrelated failure type never pushes a
        # different kind over its own threshold. Once a kind's threshold is
        # hit, its corrective is fired exactly once (the kind is marked
        # surfaced) — same discover_sink path search_tools hits use, so the
        # corrective is actually callable immediately, not just mentioned.
        if discover_sink and isinstance(result, dict):
            for predicate, kind_fn, threshold, corrective in _REPEAT_FAILURE_DETECTORS:
                if not predicate(name, arguments, result):
                    continue
                kind = kind_fn(name, arguments)
                if kind in _surfaced:
                    continue
                count = _repeat_failures.get(kind, 0) + 1
                _repeat_failures[kind] = count
                if count >= threshold:
                    _surfaced.add(kind)
                    discover_sink(corrective)

        return result

    _executor.runs = runs
    return _executor


def _extras_from_runs(runs):
    """Turns this turn's tool_executor.runs (see _make_tool_executor) into
    the same lightweight 'screenshot / download / organizeJson / confirm'
    shape the web UI already builds client-side for a live ask (see
    web/public/app.js's pushThreadExtra) so conversations.append_exchange
    can save them alongside the exchange. That's what lets the browser
    replay a screenshot, download card, organize-json result, or a
    resolved confirmation after a genuine page reload/reconnect — not just
    for as long as that browser tab's in-memory state happens to survive.
    """
    extras = []
    for run in (runs or []):
        name = run.get("name")
        result = run.get("result") if isinstance(run.get("result"), dict) else {}
        confirm = run.get("confirm")
        if isinstance(confirm, dict):
            extras.append({
                "type": "confirm",
                "data": {
                    "tool": name,
                    "arguments": run.get("arguments") or {},
                    "risk_note": confirm.get("risk_note"),
                    "resolved": bool(confirm.get("approved")),
                },
            })
        if name == "take_screenshot" and result.get("ok") and result.get("file"):
            extras.append({"type": "screenshot", "data": {"filename": result["file"]}})
        elif name == "organize_json" and result.get("ok") and result.get("path"):
            extras.append({"type": "organizeJson", "data": {"targetPath": result["path"], "payload": None}})
        elif name == "ytdl_download" and result.get("ok") and result.get("job_id"):
            # Mirror ytdl_tools.py's own MAX_MEDIA_EMITTED cap — only the
            # first few files ever got an inline player card live (see its
            # _emit_media loop), so a saved/replayed turn shouldn't show
            # more cards than the user actually saw at the time.
            for f in (result.get("files") or [])[:5]:
                if isinstance(f, dict) and f.get("file"):
                    extras.append({
                        "type": "download",
                        "data": {
                            "jobId": result["job_id"],
                            "filename": f["file"],
                            "title": f.get("title") or f["file"],
                        },
                    })
        elif name == "present_file" and result.get("ok"):
            # The gap that made present_file cards disappear on reload: every
            # other media tool had an entry here, this one never did, so its
            # card lived only in the live JARVIS_MEDIA stream. The field
            # names match app.js's showAskPresentFile(info) argument exactly
            # so the replay path can hand this straight to the same renderer
            # the live path uses, rather than a second near-copy of it.
            extras.append({
                "type": "presentFile",
                "data": {
                    "jobId": result.get("job_id"),
                    "filename": result.get("download_filename"),
                    "name": result.get("name"),
                    "type": result.get("type") or "file",
                    "sizeBytes": result.get("size_bytes"),
                    "path": result.get("path"),
                },
            })
        elif name == "dev_agent" and isinstance(result.get("steps"), list):
            # §3.6 plan §6. `result` here is run["result"] — the FULL,
            # pre-shaping copy (see the ordering fix in _executor above) —
            # so `steps` still has every preview/stdout_tail/stderr_tail
            # field a live view showed, not whatever verbosity trimmed for
            # the model. These are the exact same event dicts the live
            # stream already displayed (see dev_agent_events.emit's
            # docstring: dev_agent.py's own `steps` list is built from
            # emit()'s return value, never reconstructed separately), so a
            # reload replays provably the same trace, not an approximation
            # of it.
            extras.append({
                "type": "devAgent",
                "data": {
                    "jobId": result.get("job_id"),
                    "ok": result.get("ok"),
                    "projectDir": result.get("project_dir"),
                    "steps": result["steps"],
                },
            })
    return extras


def _completed_mutations(runs):
    """Which of this turn's tool calls actually changed something in the
    real world, and didn't themselves report an error.

    This is what lets ask() tell "every provider failed before doing
    anything" apart from "every provider failed AFTER the real work was
    already done" — see the "every provider failed" tail of ask() for why
    that distinction matters. A run counts as a completed mutation only if
    _is_mutating_tool() already agrees it's the kind of tool that isn't a
    read-only lookup, and its own result doesn't carry an "error" key (a
    mutating tool that itself failed obviously didn't complete anything).
    """
    done = []
    for run in (runs or []):
        name = run.get("name") or ""
        if not _is_mutating_tool(name):
            continue
        result = run.get("result")
        if isinstance(result, dict) and result.get("error"):
            continue
        done.append(run)
    return done


def _summarize_completed_mutations(runs):
    """Plain-language fallback reply for when real work finished but no
    provider survived to write the closing sentence about it.

    Deliberately terse and mechanical (tool name + a one-line gloss of its
    result) rather than trying to sound like a normal assistant reply —
    this text was never reviewed by a model, and pretending otherwise
    would be worse than admitting a provider dropped out partway through.
    """
    lines = ["The requested action(s) completed, but every configured "
             "provider failed before a closing reply could be written:"]
    for run in runs:
        name = run.get("name") or "tool"
        result = run.get("result") if isinstance(run.get("result"), dict) else {}
        gloss = result.get("summary") or result.get("message") or result.get("path") \
            or result.get("file") or result.get("job_id")
        if gloss:
            lines.append("- %s: %s" % (name, gloss))
        else:
            lines.append("- %s: done" % name)
    return "\n".join(lines)


def _is_mutating_tool(name):
    name = name or ""
    if name in {
        "run_command", "run_chain", "create_command", "update_command",
        "package_install", "package_uninstall",
        "spotify_play", "spotify_control", "spotify_queue", "spotify_like",
        "memory_save", "memory_forget",
        "spotify_open",
        "wifi_set", "bluetooth_set", "git_run",
        "write_file", "run_custom_command",
        "ytdl_download",
        # actions/path_tools.py (F.3): these change the disk, so a completed one
        # must show up in the recap and the degraded reply.
        "move_path", "copy_path", "rename_path", "make_dir", "delete_path",
    }:
        return True
    return name.startswith((
        "playnite_launch",
        "playnite_install",
        "playnite_uninstall",
        "playnite_delete",
        "playnite_update",
        "playnite_eval",
        "playnite_notify",
        "playnite_create",
        "playnite_manage",
        "playnite_auto",
        "playnite_view",
        "playnite_rotate",
        "playnite_fetch_all",
    ))


_DISCOVERY_ONLY_TOOLS = {"search_tools", "get_tool_schema", "load_skill"}


def _describe_run(run):
    name = run.get("name") or "a step"
    result = run.get("result")
    if isinstance(result, dict):
        if result.get("error"):
            return f"{name} did not work: {str(result['error'])[:140]}"
        if result.get("from") and result.get("to"):
            return f"{name}: {result['from']} -> {result['to']}"
        for key in ("summary", "message", "path"):
            if isinstance(result.get(key), str) and result[key]:
                return f"{name}: {result[key][:140]}"
    return f"{name}: done"


def _describe_pending(name, args):
    args = args if isinstance(args, dict) else {}
    for key in ("command", "cmd"):
        if isinstance(args.get(key), str) and args[key].strip():
            return f"{name}: {args[key].strip()[:400]}"
    if name in ("move_path", "copy_path") and args.get("src") and args.get("dest"):
        return f"{name}: {args['src']} -> {args['dest']}"
    shown = ", ".join(f"{k}={str(v)[:80]}" for k, v in list(args.items())[:4])
    return f"{name}({shown})"


def _forced_ending_reply(runs, pending, cutoff=False):
    """Harness-written reply for a turn the model couldn't finish (F.1/F.11).
    Built from what actually ran, never asks the model to explain limits, and
    never uses the words 'tool', 'budget', 'exhausted' or 'round'.

    `cutoff` is the §5 variant: the model's reply hit the output limit while
    it was writing its next step, so that step is unusable and there is no
    pending call to offer — the reply says where it stopped instead."""
    done = [r for r in (runs or []) if (r.get("name") or "") not in _DISCOVERY_ONLY_TOOLS][-6:]
    lines = []
    if done:
        lines.append("Here is where things stand.\n\nDone so far:")
        lines += [f"- {_describe_run(r)}" for r in done]
    else:
        lines.append("I couldn't finish this one.")
    if cutoff:
        lines.append("\nI ran out of room partway through working out the next step, so I stopped there.")
    if pending:
        name, args = pending[0]
        lines.append("\nStill to do — I was about to run:\n" + f"- {_describe_pending(name, args)}")
        lines.append("\nSay \"go ahead\" and I'll run that (you'll get the usual confirmation first).")
    else:
        lines.append("\nAsk me to continue and I'll pick up from there.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# F.11 — a pending proposed action, so a bare "go ahead" runs it.
#
# When a turn ends by force, the harness reply offers the one call the model
# was about to make ("Say \"go ahead\" and I'll run that"). Until now that was
# only words: the next message went to a model that had to rediscover the
# call from the recap, and in the logs it often didn't (Case 2b's Move-Item
# was proposed twice and never run). Now the offered call is saved on that
# exchange as a `pendingAction` extra, and the very next message, if it is a
# bare confirmation, runs it directly.
#
# Safety, deliberately conservative:
#   - it goes through the SAME tool_executor as any model call, so the normal
#     confirm gate (tool_safety.py) still asks first — nothing here bypasses it;
#   - only the LAST exchange's action counts, so it can never be "a yes to
#     something from yesterday": one message later it is gone;
#   - it expires after PENDING_ACTION_TTL_SECONDS even if nothing else was said;
#   - the tool name must be one this session actually has, and the arguments
#     must be a plain dict;
#   - only the owner's own surfaces (CLI / web) use it, never a chat guest.
# ---------------------------------------------------------------------------
PENDING_ACTION_TTL_SECONDS = 30 * 60


def _pending_action_extra(pending, known_names):
    """The `pendingAction` extra for the first offerable call in `pending`
    (the same call _forced_ending_reply names), or None."""
    for name, args in pending or []:
        if name not in known_names or not isinstance(args, dict):
            continue
        try:
            json.dumps(args)
        except (TypeError, ValueError):
            continue
        return {"type": "pendingAction", "data": {
            "name": name, "arguments": args,
            "summary": _describe_pending(name, args), "ts": time.time(),
        }}
    return None


def _load_pending_action(conv_id, now=None):
    """(name, arguments) proposed by the conversation's LAST exchange and
    still fresh, else None."""
    if not conversations.is_valid_id(conv_id):
        return None
    record = conversations.get_conversation(conv_id) or {}
    exchanges = record.get("exchanges") or []
    if not exchanges:
        return None
    last = exchanges[-1]
    if last.get("pending") or not (last.get("jarvis") or "").strip():
        return None
    for extra in last.get("extras") or []:
        if not isinstance(extra, dict) or extra.get("type") != "pendingAction":
            continue
        data = extra.get("data") or {}
        name, args, ts = data.get("name"), data.get("arguments"), data.get("ts")
        if not isinstance(name, str) or not isinstance(args, dict):
            continue
        try:
            age = (time.time() if now is None else now) - float(ts)
        except (TypeError, ValueError):
            continue
        if 0 <= age <= PENDING_ACTION_TTL_SECONDS:
            return name, args
    return None


def _pending_action_reply(name, args, result):
    """Harness-written reply after a direct run: says what happened, in the
    user's terms, from the result alone."""
    if isinstance(result, dict) and (result.get("cancelled") or result.get("blocked")):
        return "Okay, I didn't run it. Nothing has changed."
    if isinstance(result, dict) and (result.get("error") or result.get("ok") is False):
        why = str(result.get("error") or "it reported a failure")[:200]
        return f"That didn't go through: {why}"
    return "Done. " + _describe_run({"name": name, "result": result})


# §5: appended to an answer the provider cut off at its output cap.
_CUT_NOTE = ("\n\n(That reply was cut off at the length limit. "
             "Say \"continue\" and I'll pick up from there.)")


def _run_pending_action(pending_action, tool_executor, conv_id, user_text,
                        assistant_name, address, trace):
    """Run the call the last turn offered, straight away. Goes through
    `tool_executor`, i.e. the normal confirm gate. Never raises."""
    name, args = pending_action
    conversations.begin_exchange(conv_id, user_text)
    _pending_turn[0] = (conv_id, user_text)
    ai_providers.set_log_context(conv_id, "pending action")
    try:
        result = ai_providers._call_tool_safely(tool_executor, name, args)
    except Exception as e:  # noqa: BLE001 — never take the ask down
        result = {"error": f"{name} failed: {e}"}
    finally:
        ai_providers.clear_log_context()
    runs = getattr(tool_executor, "runs", None)
    reply = _pending_action_reply(name, args, result)
    trace.ending = "pending_action"
    for run in (runs or []):
        trace.note_step(run.get("name"), run.get("arguments"), run.get("result"))
    exchange_count = conversations.complete_exchange(
        conv_id, user_text, reply, "(ran the step I'd proposed)",
        extras=_extras_from_runs(runs),
    )
    _spawn_title_update(conv_id, exchange_count)
    _pending_turn[0] = None
    return AskResult(True, text=reply, provider=None, attempts=[],
                     assistant_name=assistant_name, address_user_as=address,
                     ending="pending_action")


# F.7: the recap's own notion of "something real happened". Deliberately NOT
# _is_mutating_tool(): that one also feeds _completed_mutations(), i.e. the
# "the requested action completed" degraded reply, and a `run_shell` that only
# ran `dir` must never be reported as a completed action. Here the only effect
# of the wider set is the recap wording — after a failed coding job the next
# model was told "No launch/install/command has run yet... you MUST call the
# real tool now" and went looking for a tool instead of reporting.
_RECAP_SIDE_EFFECT_TOOLS = {"code_agent", "run_shell", "edit_file"}


def _ran_something_real(name):
    return _is_mutating_tool(name) or (name or "") in _RECAP_SIDE_EFFECT_TOOLS


def _tool_runs_note(runs, char_budget, verbosity="full", can_call_tools=True):
    """Tell the next model what already ran — without implying side effects
    (launch/install) happened if they didn't.

    Each run's cached result is re-shaped at the *current* verbosity before
    being serialized here, rather than resent at whatever verbosity was in
    effect when it was first produced. shape_result() is an opt-in allowlist
    that returns unclassified tools unchanged, so re-shaping an
    already-shaped result is idempotent-or-further-trimming, never wrong.
    """
    if not runs:
        return None
    ran = [r.get("name") or "" for r in runs]
    mutated = [n for n in ran if _ran_something_real(n)]
    parts = [
        "Some tools already ran this turn. Reuse those results — do not repeat "
        "the same read-only call (search, list, fetch, query).",
    ]
    if mutated:
        parts.append(
            "These actions DID run (only claim they happened because of the results below): "
            + ", ".join(mutated) + "."
        )
    else:
        # Neutral on purpose (F.7 item 3). The old text told every model, on
        # every non-launch task, that it MUST call a tool; the launch/install
        # case it was written for is still covered by "if the request needs one".
        parts.append(
            "Nothing that launches, installs, edits or runs a command has run yet — "
            "do not claim otherwise. "
            + ("If the user's request needs one, call the real tool for it now "
               "(don't write a tool call as plain text); if it doesn't, answer from "
               "the results above."
               if can_call_tools else
               "Answer from the results above and say plainly what is still undone.")
        )
    used = sum(len(p) for p in parts)
    for run in runs:
        shaped_result = tool_result_shaping.shape_result(
            run.get("name"), run.get("result"), verbosity
        )
        try:
            args_s = json.dumps(run.get("arguments") or {}, default=str)
            result_s = json.dumps(shaped_result, default=str)
        except TypeError:
            args_s = str(run.get("arguments"))
            result_s = str(shaped_result)
        block = f"\n{run.get('name')}({args_s})\n{result_s}"
        room = char_budget - used
        if room <= 80:
            parts.append("\n…(further tool results omitted)")
            break
        if len(block) > room:
            block = block[:room] + "\n…(truncated)"
        parts.append(block)
        used += len(block)
    return "\n".join(parts)


def _carried_scaffold(history):
    """The tool round-trips out of a failed attempt's `AIResult.tool_history`,
    ready to append to the NEXT attempt's fresh messages.

    `tool_history` is the failed attempt's whole generic transcript (system +
    the user turn + every "[called x with {...}]" / "[tool result] ..." pair).
    The head of it is rebuilt fresh for each provider (its system prompt and
    profile differ), so only the tail from the first tool call on is kept. That
    is found by the FIRST assistant message containing a call — never by a user
    message that merely looks like a result, which a pasted trace could fake."""
    for i, m in enumerate(history or []):
        if (isinstance(m, dict) and m.get("role") == "assistant"
                and isinstance(m.get("content"), str) and "[called " in m["content"]):
            return [dict(x) for x in history[i:] if isinstance(x, dict)]
    return []


def _truncate_middle(text, limit):
    """Shorten `text` to about `limit` chars by cutting the MIDDLE, keeping the
    head (what it is) and the tail (how it ended). The old recap cut the tail
    off everything after the first big result — F.7's 'never drop the whole
    tail'."""
    if not isinstance(text, str) or len(text) <= limit or limit < 120:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    return text[:head] + f"\n…[{len(text) - head - tail} characters omitted]…\n" + text[-tail:]


def _carried_messages(history, char_budget):
    """`_carried_scaffold`, with each tool result middle-truncated so the whole
    carry stays near `char_budget` (the active capacity mode's own limit, but
    never less than ~600 chars per result — that floor is what keeps a single
    large result like code_agent's from squeezing out the ones after it)."""
    scaffold = _carried_scaffold(history)
    results = [m for m in scaffold if m.get("role") == "user"
               and str(m.get("content", "")).lstrip().startswith("[tool result")]
    per_result = max(600, int(char_budget) // max(1, len(results)))
    out = []
    for m in scaffold:
        if m in results:
            m = {**m, "content": _truncate_middle(m["content"], per_result)}
        out.append(m)
    return out


_TOOL_TRACE_LINE = re.compile(r"^\[(called |tool result)", re.I)
_TOOL_TRACE_ANY = re.compile(r"\[called\s+[A-Za-z0-9_]+\s+with\s+\{", re.I)


def _short_429_wait_seconds(kind, failure, cap):
    """Decision D5 (master plan F.9/F.16): seconds to wait before retrying
    the SAME key once, or None if this failure doesn't qualify.

    Only a rate-limit/quota failure (KIND_KEY, matching the same
    "rate limited"/"quota" text key_health.record_failure keys off of) that
    stated an actual delay of at most `cap` seconds qualifies — a
    bad/rejected key (401/403, still KIND_KEY but nothing to wait out), an
    unstated delay, or a delay longer than `cap` all return None and rotate
    to the next key immediately, exactly as before D5. `cap` <= 0 disables
    this entirely (defaults.max_429_wait_seconds = 0).
    """
    if cap <= 0 or kind != ai_providers.KIND_KEY:
        return None
    err = str(failure or "").lower()
    if "rate limited" not in err and "quota" not in err:
        return None
    delay = key_health.parse_retry_delay(failure)
    if delay is None or delay > cap:
        return None
    return max(delay, 0.0)


def _is_tool_trace_reply(text):
    """True when the model echoed internal tool-call scaffolding instead of
    answering the user — treat as a failed attempt and keep failing over."""
    if not text or not str(text).strip():
        return False
    stripped = str(text).strip()
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    if not lines:
        return False
    if all(_TOOL_TRACE_LINE.match(ln) for ln in lines):
        return True
    # Whole reply is basically one fake tool call (maybe with a short prefix).
    if _TOOL_TRACE_ANY.search(stripped) and len(stripped) < 800:
        prose = _TOOL_TRACE_ANY.sub("", stripped).strip(" \n:-")
        if len(prose) < 40:
            return True
    return False


# Mirrors web/public/app.js's splitConsoleDump()/NAME_PREFIX_LINE exactly —
# same two passes, same regexes translated 1:1 (JS's case-sensitive
# `[^\n:]{1,40}` and INLINE_TOOL_TRACE_LINE == this module's own
# _TOOL_TRACE_LINE, just reused instead of re-declared). This is the "raw
# reply glued to a console dump" split the browser already does live while
# streaming stdout for a fresh ask; ask() below applies it once more,
# server-side, purely so the *saved* exchange matches what was shown live
# (see the call site's comment). If splitConsoleDump ever changes, update
# this to match or the two will drift — there's no shared source of truth,
# same caveat AGENTS.md calls out for tests/interactive_inspector.py's
# router mirror.
_NAME_PREFIX_LINE = re.compile(r"^([^\n:]{1,40}):\s(.*)$")


def _split_console_dump(text):
    """Splits `text` into (clean_reply_text, dump_lines) the same way the
    web UI splits a live stdout stream into a normal reply bubble plus a
    separate "Console" bubble: everything before the first "<Name>: <rest>"
    -looking line is a console dump (raw command output some providers glue
    in ahead of their real answer), and any line anywhere else that looks
    like an echoed tool-call/tool-result trace (_TOOL_TRACE_LINE) is pulled
    out into the dump too. Returns (text, []) unchanged when there's
    nothing to split out, so callers can apply this unconditionally."""
    if not text:
        return text, []
    lines = str(text).split("\n")
    split_at = -1
    first_reply_line = None
    for i, ln in enumerate(lines):
        m = _NAME_PREFIX_LINE.match(ln)
        if m:
            split_at = i
            first_reply_line = m.group(2)
            break
    if split_at == -1:
        dump = []
        candidate_reply = list(lines)
    else:
        dump = lines[:split_at]
        candidate_reply = [first_reply_line] + lines[split_at + 1:]
    reply = []
    for ln in candidate_reply:
        if _TOOL_TRACE_LINE.match(ln.strip()):
            dump.append(ln.strip())
        else:
            reply.append(ln)
    return "\n".join(reply), dump



def _extract_json_object(text):
    if not text:
        return None
    match = _JSON_OBJECT_RE.search(text)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _quick_title_completion(cfg, user_text, jarvis_text):
    """One cheap, history-free completion asking for {"title", "soft_context"}
    JSON describing this exchange. Tries at most the first two eligible
    providers — but, same as the main ask() failover, every configured key
    for each of those providers before moving on — and gives up quietly on
    failure; this is cosmetic, never allowed to block or break an actual
    ask. (Previously this only ever tried a provider's first key, so a
    single blocked/rate-limited key could skip a provider's other working
    keys entirely instead of failing over to them.)"""
    providers = _eligible_providers(cfg["providers"], cfg["defaults"])
    if not providers:
        return None
    prompt = (
        "Give this exchange a short conversation title (3-6 words, title case, no quotes, "
        "no trailing punctuation) and a one-sentence gist (under 20 words, third person) "
        "suitable for a chat-history sidebar. Respond with ONLY compact JSON like "
        '{"title": "...", "soft_context": "..."} and nothing else \u2014 no markdown, no '
        "commentary.\n\n"
        f"User: {conversations._truncate(user_text or '', 400)}\n"
        f"Assistant: {conversations._truncate(jarvis_text or '', 400)}"
    )
    messages = [{"role": "user", "content": prompt}]
    for provider in providers[:2]:
        adapter = ai_providers.ADAPTERS.get(provider.get("type"))
        if adapter is None:
            continue
        keys = ai_config.provider_keys(provider) or [None]
        for key in keys:
            resolved = _resolve(provider, cfg["defaults"])
            if key is not None:
                resolved["api_key"] = key
            try:
                result = adapter(
                    resolved, messages, min(resolved["timeout"], 15),
                    tools=None, tool_executor=None,
                )
            except Exception:
                continue
            if result.ok and result.text:
                return result.text
    return None


def _maybe_update_title(cfg, conversation_id, exchange_count, user_text, jarvis_text):
    """(Re)titles a conversation right after its first exchange, then every
    TITLE_REGEN_EVERY exchanges after that. Falls back to a heuristic title
    (a truncated first line of the user's message) if the AI call fails, so
    a conversation is never stuck saying "New Conversation" forever just
    because one title request happened to fail.

    Purely cosmetic and, critically, NOT on the path to the user's answer
    (see _spawn_title_update below) \u2014 this can take up to ~30s (two
    provider attempts at a 15s timeout each) when a title provider is slow
    or flaky, and that used to stall the real reply for just as long since
    ask() called this inline before returning."""
    if exchange_count != 1 and exchange_count % TITLE_REGEN_EVERY != 0:
        return
    title = None
    soft_context = None
    try:
        raw = _quick_title_completion(cfg, user_text, jarvis_text)
        obj = _extract_json_object(raw) if raw else None
        if obj:
            t = obj.get("title")
            s = obj.get("soft_context")
            if isinstance(t, str) and t.strip():
                title = t.strip().strip("\"'")
            if isinstance(s, str) and s.strip():
                soft_context = s.strip()
    except Exception:
        pass  # title generation is cosmetic — never let it break an ask
    if title is None:
        title = conversations._truncate((user_text or "").strip().splitlines()[0], 42) if user_text else None
    if soft_context is None:
        soft_context = conversations._truncate((user_text or "").strip(), 140) if user_text else None
    if title or soft_context:
        try:
            conversations.update_meta(conversation_id, title=title, soft_context=soft_context)
        except Exception:
            pass


def run_internal_retitle(conversation_id, exchange_count):
    """Entry point for the detached `jarvis _internal_retitle <id> <n>`
    subprocess (see _spawn_title_update below). Re-reads the just-appended
    exchange straight from the conversation's on-disk record — which
    conversations.append_exchange() already wrote before this process was
    ever launched — instead of needing user_text/jarvis_text passed on the
    command line (avoids OS argv-length limits for a long exchange, and
    keeps this callable with nothing but an id). Purely cosmetic and safe
    to fail silently, same as the code it replaces.
    """
    try:
        cfg = ai_config.load_ai_config()
        record = conversations._load_conv(conversation_id)
        exchanges = (record or {}).get("exchanges") or []
        if not exchanges:
            return
        last = exchanges[-1]
        _maybe_update_title(
            cfg, conversation_id, exchange_count,
            last.get("user"), last.get("jarvis"),
        )
    except Exception:
        pass  # title generation is cosmetic — never let it break anything


def _spawn_title_update(conversation_id, exchange_count):
    """Fire-and-forget (re)titling, launched as a fully **detached OS
    process** rather than a background thread.

    Why not a thread: jarvis is a brand-new OS process on every invocation
    (see history.py's docstring) that exits almost immediately after
    printing the reply — cli.py's top-level call is `sys.exit(handle_ai_
    prompt(...))`, and Python kills daemon threads outright on interpreter
    shutdown rather than waiting for them. A title/soft_context completion
    is a real network round trip (up to ~30s across two provider attempts
    at a 15s timeout each — see _quick_title_completion), which is
    essentially always still in flight when the parent process exits a few
    milliseconds after spawning it. The old daemon-thread version lost that
    race on effectively every call, which is why brand-new conversations
    were never actually getting titled/described (see bug log) even though
    the logic that computes the title was itself correct.

    The fix: spawn a fully independent `jarvis _internal_retitle <id> <n>`
    process (own session/process group, stdio detached) that keeps running
    after this parent exits, and re-reads the exchange it needs from disk
    (see run_internal_retitle) rather than depending on anything held in
    this process's memory. This preserves the original design intent
    exactly — the real reply is never delayed by this — while actually
    letting the update complete.
    """
    args = [sys.executable, "-m", "jarvis", "_internal_retitle",
            conversation_id, str(exchange_count)]
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL)
    if os.name == "nt":
        # CREATE_NO_WINDOW (not DETACHED_PROCESS) is the flag that actually
        # suppresses window creation for a console-subsystem child like
        # `python -m jarvis`: DETACHED_PROCESS still lets Windows briefly
        # allocate/flash a console for a console-subsystem executable
        # before it exits, which is exactly the "a cmd window pops up and
        # dies" symptom on every single ask — CREATE_NO_WINDOW never
        # allocates one at all. (The two are mutually exclusive per the
        # Win32 CreateProcess docs, so this replaces DETACHED_PROCESS
        # rather than adding to it; CREATE_NEW_PROCESS_GROUP isn't needed
        # either — that's for signal isolation, not window visibility.)
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(args, **kwargs)
    except Exception:
        pass  # title generation is cosmetic — never let it break an ask


# The turn currently in flight, as (conv_id, user_text), or None. A
# one-element list rather than a module global reassigned from inside ask()
# purely so the interrupt handler below and ask() itself are provably
# looking at the same object.
#
# This exists because the only reliable moment to record "the user asked
# this and never got an answer" is from a signal handler, which has no
# access to ask()'s locals. Killing a jarvis process mid-ask is a completely
# ordinary thing to do (the web console's Stop button does it on every
# abort), so that path needs to leave the conversation in an honest state,
# not an empty one.
_pending_turn = [None]


def abandon_pending_turn(reason="interrupted"):
    """Flush the in-flight turn as unanswered. Safe to call at any time,
    including from a signal handler and including when nothing is pending.

    Never raises: it runs on the way out of a dying process, where an
    exception would just replace one lost message with a confusing
    traceback.
    """
    pending = _pending_turn[0]
    _pending_turn[0] = None
    if not pending:
        return False
    conv_id, user_text = pending
    try:
        return conversations.abandon_exchange(conv_id, user_text, reason=reason)
    except Exception:  # noqa: BLE001
        return False


# Mirrors the wrapper web/server.js builds around a highlighted excerpt
# (`prompt = "The user highlighted this excerpt ... :\n\"\"\"\n" + quote +
# "\n\"\"\"\n\n" + text`). The excerpt is text the user is pointing AT, not
# asking for, so it must not vote in the router (F.10 cause 3). If server.js's
# wording ever changes this simply stops matching and routing falls back to the
# old whole-message behaviour — same no-shared-source-of-truth caveat as the
# other mirrors AGENTS.md lists. tests/test_highlight_wrapper_drift.py reads the
# wrapper's literals out of server.js and fails if they stop matching this.
_HIGHLIGHT_WRAPPER = re.compile(
    r'\AThe user highlighted this excerpt from the conversation and wants you to address it specifically:\n"""\n.*?\n"""\n\n(?P<own>.*)\Z',
    re.S,
)
_HIGHLIGHT_DEFAULT_ASK = "Please respond about the quoted excerpt."


def _strip_highlight_excerpt(user_text):
    """The user's own words from a highlight-quote prompt, or `user_text`
    unchanged when it isn't one. The stock "Please respond about the quoted
    excerpt." (what the UI sends when the user typed nothing) routes as empty."""
    m = _HIGHLIGHT_WRAPPER.match(user_text or "")
    if not m:
        return user_text
    own = m.group("own").strip()
    return "" if own == _HIGHLIGHT_DEFAULT_ASK else own


def _strip_pasted_previous_reply(user_text, conv_id):
    """F.10: when user_text contains Jarvis's own previous reply pasted
    back verbatim (the raw paste case — no `The user highlighted this
    excerpt…` wrapper, which is the web UI's separate highlight-quote
    path and isn't touched here), remove that reply text before handing
    the message to tool_router.route(). Otherwise the router scores the
    ASSISTANT's own words too — a closing line like "Let me know if
    you'd like me to handle it..." can out-score the user's actual
    instruction sitting right next to it (Case 2b: it beat "run this
    custom command" outright and replaced the sticky `files` group with
    `channels`+`scheduling`).

    Deliberately conservative: only strips an EXACT match of the whole
    previous reply (stripped of surrounding whitespace), and only when
    it's long enough (20+ chars) to be a real paste rather than a short
    phrase that could legitimately also be something the user typed
    themselves. A smaller or inexact overlap is left alone — worst case
    that just falls back to the pre-fix behavior for that message, never
    something worse.

    Only affects what gets ROUTED; the original user_text (paste
    included) is still what's sent to the model and saved to history —
    this never changes what Jarvis actually sees or remembers.
    """
    if not user_text or not conv_id:
        return user_text
    prev = conversations.last_assistant_reply(conv_id)
    if not prev:
        return user_text
    prev = prev.strip()
    if len(prev) < 20 or prev not in user_text:
        return user_text
    stripped = user_text.replace(prev, " ", 1).strip()
    return stripped or user_text


# F.10 item 2: a short confirmation ("yes", "do it", "run this", "go
# ahead", ...) that ALSO happens to confidently match a group (e.g. "yes
# plz run this" matching `commands` via "run"/"command") shouldn't
# outright replace a broader sticky context the way a genuine topic
# change should (route_stickiness.py's normal "a new toolset drops the
# old one" behavior) — it should ADD to it.
#
# Anchored across the WHOLE message, not just a leading prefix: a
# prefix-only match let "please install ffmpeg for me" false-positive as
# a confirmation, since "please" is both a common confirmation word AND
# a common way to start a completely new, unrelated instruction. Anchoring
# end-to-end means every word has to be either a bare filler
# (yes/plz/please/...) or part of a fixed confirmation phrase — "install
# ffmpeg for me" isn't either, so the whole match fails, exactly as it
# should. "run this <whatever the model/user names>" is still allowed to
# carry up to 3 trailing words (the actual real-world Case 2b case, "run
# this custom command") since that's the one place a confirmation
# legitimately names its own object.
_CONFIRMATION_FILLER = r"(?:yes|yep|yeah|yup|sure|ok(?:ay)?|please|plz|confirmed?|proceed)"
_CONFIRMATION_CORE = r"(?:go ahead|go for it|do it|do that|sounds good|run (?:it|this|that)(?:\s+\w+){0,3})"
_CONFIRMATION_TOKEN = rf"(?:{_CONFIRMATION_FILLER}|{_CONFIRMATION_CORE})"
_CONFIRMATION_RE = re.compile(
    rf"^\s*{_CONFIRMATION_TOKEN}(?:[\s!.,]+{_CONFIRMATION_TOKEN})*[\s!.,]*$",
    re.IGNORECASE,
)


def _looks_like_short_confirmation(text):
    text = (text or "").strip()
    if not text or len(text.split()) > 8:
        return False
    return bool(_CONFIRMATION_RE.match(text))


def _merged_sticky_groups_for_confirmation(route, existing_sticky_groups, user_text):
    """The decision half of F.10 item 2, pulled out as a pure function so
    it's testable without going through the whole ask() attempt loop.

    Returns the merged group list (existing sticky groups, in order, plus
    any newly-matched group not already in it) when this message is BOTH
    a confident route AND a short confirmation with a live sticky context
    to merge into; returns None otherwise, meaning "normal replace
    behavior" (ask() then does route_stickiness.set_sticky(conv_id,
    route.groups) exactly as before this fix).
    """
    if not route.confident or not existing_sticky_groups:
        return None
    if not _looks_like_short_confirmation(user_text):
        return None
    merged = list(existing_sticky_groups)
    for group in route.groups:
        if group not in merged:
            merged.append(group)
    return merged


def ask(user_text, commands=None, on_attempt=None, on_tool_call=None, on_tool_result=None, conversation_id=None,
        on_confirm_request=None, on_route=None, provider_override=None,
        think_override=None, on_trace=None, sender_context="", on_interim_text=None):
    """Ask Jarvis something, trying every configured, enabled provider in
    order until one answers \u2014 and within each provider, every one of its
    configured keys in order before moving on to the next provider. Always
    returns an AskResult \u2014 never raises, so a single flaky provider (or
    key) can't take down the whole CLI call.

    on_attempt(label), if given, fires right before each attempt (cli.py
    uses this to print live "asking X..." trace to stderr). label includes
    a "(key i/N)" suffix when a provider has more than one key configured,
    so the trace makes it obvious which key failed \u2014 useful when e.g. only
    your second OpenAI key has run out of credit.

    on_tool_result(name, input_tokens, output_tokens), if given, fires right
    after each tool call finishes — once the ~estimated input AND output
    token counts (see token_usage.estimate_tokens_for) are both known.

    on_tool_call(name), if given, fires right before each tool call Jarvis
    makes while answering (battery, wifi, location, ...) \u2014 same idea, live
    trace of what's actually happening. Tools are looked up fresh from
    ai_config.json's defaults.tools_enabled on every call, same as
    everything else here; set it to false to turn tool calling off
    entirely (e.g. to keep every ask to a single request).

    on_interim_text(text, round_num), if given, fires when a provider sends
    text ALONGSIDE a tool call in the same response \u2014 "I'll check that
    now" right before it actually calls something \u2014 rather than that text
    being silently dropped the way it was before master plan Part A \u00a75.
    Fires before the tool(s) that came with it are run. All five adapters
    report it (Anthropic, Gemini, OpenAI-compatible, Cohere, Ollama).

    conversation_id picks which conversation (see conversations.py) this
    exchange belongs to and gets appended to. When omitted, the CLI's
    on-disk "current" conversation is used (auto-created on first ever
    use) — the web UI instead always passes one explicitly, since each
    browser tab tracks its own active conversation.

    on_confirm_request(name, arguments, risk_note), if given, gates any
    tool call flagged confirm_required in tool_safety.json (see
    _make_tool_executor). Tools requiring confirmation fail closed (never
    run) if this isn't supplied.

    sender_context, if given, is a short identity block naming who sent
    this message and whether they are the owner (see
    channels/people.prompt_block). It is injected at the head of the
    per-request system tail. Only the chat gateways pass it — over Discord
    or Instagram the person typing is frequently NOT the owner, and the
    rest of the prompt is written on the assumption that they are. The CLI
    and web UI leave it empty, which keeps their prompts byte-identical to
    what they were before this parameter existed.

    on_route(route), if given, fires once right after the local router
    (tool_router.route()) decides what this message plausibly needs \u2014
    same live-trace idea as on_attempt, but for the routing decision
    itself (route.tools/route.groups/route.matches) rather than a
    provider attempt. Only called when tools are enabled, since routing
    only happens on that branch.

    provider_override, if given, restricts this single ask() call to a
    custom, caller-picked subset/order of providers instead of the
    configured defaults.provider_priority \u2014 skipping every other
    configured provider entirely. Accepts either:

      - a single provider name (string) \u2014 the original one-provider form,
        kept working as-is for the plain-CLI '--provider NAME' flag; or
      - an ordered list/tuple of names, e.g. ["anthropic", "gemini"] \u2014
        try anthropic first, then gemini, in exactly that order, ignoring
        every other configured provider and defaults.provider_priority.
        This is what powers the Ask panel's multi-pick provider-order
        picker (see _order_providers_by_override() below and
        jarvis-provider-override-ranked.patch): the *order the person
        clicked providers in* becomes the try-order for that one ask.

    Matching is case-insensitive against each provider's "name" field
    (same lookup provider_priority uses). It's a one-off, per-call knob
    (like the "mode" argument to jarvis tool-run/tool-preview): it never
    touches ai_config.json or any persisted default, and normal multi-key
    failover *within* each named provider still applies. Omitted (the
    default) means "no override" \u2014 exact previous behavior, trying every
    eligible provider in priority order. Names that don't match any
    eligible provider (wrong name, disabled, or no usable key) are simply
    dropped from the try-order rather than failing the whole call \u2014
    unless *every* name given fails to match, in which case ask() fails
    the same way it would if no providers were configured at all, except
    result.attempts names every requested provider so the caller can say
    why.
    """
    cfg = ai_config.load_ai_config()
    persona = cfg["persona"]
    assistant_name = persona.get("assistant_name") or DEFAULT_ASSISTANT_NAME
    address = persona.get("address_user_as") or DEFAULT_ADDRESS

    conv_id = conversation_id if conversations.is_valid_id(conversation_id) else conversations.get_current_id()

    providers = _eligible_providers(cfg["providers"], cfg["defaults"])
    if provider_override:
        wanted_names = (
            [provider_override] if isinstance(provider_override, str)
            else [n for n in provider_override if isinstance(n, str) and n.strip()]
        )
        matched = _order_providers_by_override(providers, wanted_names)
        if not matched:
            return AskResult(
                False, assistant_name=assistant_name, address_user_as=address,
                attempts=[(", ".join(wanted_names) or str(provider_override),
                           "not configured, not enabled, or has no API key")],
            )
        providers = matched
    if not providers:
        return AskResult(False, assistant_name=assistant_name, address_user_as=address)

    # Per-turn explanation (turn_trace.py). Built here, filled by the same
    # callbacks that already drive the stderr trace, so it can never describe
    # something different from what actually ran.
    trace = turn_trace.TurnTrace()

    # Thinking level for this one ask: explicit override > configured level >
    # the zero-cost keyword heuristic. Resolved once and held in
    # ai_providers' thinking context for the whole attempt loop, because the
    # adapters read it per round (see reasoning.round_patch's token-discipline
    # comment for why per-round rather than per-request).
    think_level, think_cfg = reasoning.effective_level(
        user_text, cfg["defaults"], think_override)
    trace.thinking_level = think_level

    tools_enabled = cfg["defaults"].get("tools_enabled", DEFAULT_TOOLS_ENABLED)
    full_schemas = []
    if tools_enabled:
        full_schemas = system_tools.tool_schemas_for_session()

        # Phase 4 of the token-optimization plan (see new_plan.md): ask the
        # local router (Phase 3, tool_router.py — no model round trip) what
        # this message plausibly needs before deciding what to *offer*.
        # Deliberately conservative and safe-by-default: the router only
        # ever narrows the catalog when it has real keyword signal
        # (route.confident); anything ambiguous — including "hi", which is
        # exactly the case this phase targets — still falls back to the
        # exact full_schemas behavior from before this phase, so there's no
        # regression risk for messages the router doesn't recognize yet.
        # tool_executor below is still built from full_schemas regardless
        # (not active_schemas) so a tool call the router didn't anticipate
        # still validates/executes normally rather than failing closed.
        route = tool_router.route(_strip_pasted_previous_reply(_strip_highlight_excerpt(user_text), conv_id))
        if on_route:
            on_route(route)
        trace.note_route(route)

        from . import tool_registry

        # Stickiness: a confident match here normally wins outright and
        # replaces whatever group was previously sticky for this
        # conversation (never merges with it — see route_stickiness.py's
        # module docstring on why "a new toolset drops the old one"). The
        # one exception (master plan F.10 item 2): when the message is
        # both confident AND a short confirmation ("yes plz run this"),
        # it's continuing the previous task, not switching to a new one —
        # merge its matched group(s) into the still-live sticky set
        # instead of dropping the rest.
        # A non-confident result instead checks whether an earlier turn in
        # this same conversation left a still-live sticky group behind, and
        # if so offers *that* instead of falling all the way back to the
        # bare discovery pair — this is what keeps a same-task follow-up
        # ("no just say hello", "it's open now") from losing every tool it
        # actually needs just because the follow-up text itself doesn't
        # repeat the original keywords.
        sticky_tools = []
        existing_sticky_groups = route_stickiness.get_sticky(conv_id)
        if route.confident:
            merged_groups = _merged_sticky_groups_for_confirmation(route, existing_sticky_groups, user_text)
            if merged_groups is not None:
                route_stickiness.set_sticky(conv_id, merged_groups)
                trace.sticky_groups = list(existing_sticky_groups)
                # Mutate route in place (RouteResult's fields are plain
                # attributes, not read-only) so every other consumer of
                # this same route object downstream — _build_messages'
                # pack_instructions_ctx/offered_names, the other
                # route.tools/route.groups reads later in this function —
                # sees the merged view too, not just this one
                # active_schemas assignment. turn_trace already took its
                # own copy via note_route() above, so the raw pre-merge
                # decision is still what shows up in the trace/log.
                seen = set()
                merged_tools = []
                for group in merged_groups:
                    for name in tool_registry.tools_in_group(group):
                        if name not in seen:
                            seen.add(name)
                            merged_tools.append(name)
                route.groups = merged_groups
                route.tools = merged_tools
            else:
                route_stickiness.set_sticky(conv_id, route.groups)
        else:
            sticky_groups = existing_sticky_groups
            if sticky_groups:
                seen = set()
                for group in sticky_groups:
                    for name in tool_registry.tools_in_group(group):
                        if name not in seen:
                            seen.add(name)
                            sticky_tools.append(name)
                if sticky_tools:
                    route_stickiness.touch_sticky(conv_id)
                    trace.sticky_groups = list(sticky_groups)

        # Phase 5 of the token-optimization plan (see new_plan.md): when the
        # router has no opinion, don't fall all the way back to the full
        # catalog — offer only the small always-available search_tools
        # discovery tool instead. A real tool call the model needs is still
        # reachable (search_tools -> _make_tool_executor's discover_sink
        # below grows active/compact/name_only_schemas in place, so a match
        # becomes callable on the very next round without a second full
        # prompt resend), it's just not offered up front on spec. A live
        # sticky group (see above) is offered in place of that bare pair.
        if route.confident:
            active_schemas = OrderedSchemaSet(system_tools.schemas_for_tools(route.tools))
        elif sticky_tools:
            active_schemas = OrderedSchemaSet(system_tools.schemas_for_tools(sticky_tools))
        else:
            active_schemas = OrderedSchemaSet(system_tools.DISCOVERY_AND_COMMANDS_SCHEMAS)

        # Phase 2 of the enhancements doc: when the router has no opinion,
        # check whether a similar message already paid for a search_tools/
        # search_commands round trip recently (in this process or an
        # earlier one — jarvis is a fresh OS process every call, see
        # history.py, so this is the only thing that survives between
        # them). A hit pre-seeds active_schemas with the previously-found
        # tools/commands-group names via the same schemas_for_tools() path
        # discover_sink uses below, so the very first round already has
        # what the last similar query needed. A miss (missing/corrupt/
        # stale cache file, or just no prior match) falls straight through
        # to the exact DISCOVERY_AND_COMMANDS_SCHEMAS behavior above — no
        # regression risk either way.
        cache_query = (user_text or "").strip().lower()
        if not route.confident:
            from . import tool_registry

            cached_names = []
            for kind in ("tools", "commands"):
                hit = discovery_cache.cache_lookup(cache_query, kind)
                if hit:
                    cached_names.extend(hit)
            if cached_names:
                seeded = {s.get("name") for s in active_schemas}
                for name in cached_names:
                    if name in seeded:
                        continue
                    group = tool_registry.group_of(name)
                    group_names = tool_registry.tools_in_group(group) if group else [name]
                    for gname in group_names:
                        if gname and gname not in seeded:
                            seeded.add(gname)
                            full = system_tools.schemas_for_tools([gname])
                            active_schemas.extend(full)
                            trace.cache_seeded.append(gname)

        # Real (description-stripped) argument schemas, not name-only stubs,
        # are the default (full/compact modes) because jarvis is a brand-new
        # process every "jarvis ..." call (see history.py's module
        # docstring) — there's no running session for a model to "learn" a
        # tool's shape in, so name-only-then-relearn-on-first-use pays a
        # full extra tool round trip (i.e. resending the *entire* prompt
        # again) on essentially every argument-taking tool, every single
        # invocation. A few hundred extra bytes of schema up front is far
        # cheaper than that guaranteed second round trip — UNLESS the prompt
        # itself is the thing being minimized, which is exactly what "ultra"
        # (50% Capacity) mode is for: see tool_schema_style per-provider
        # below, resolved fresh each attempt since mode can still vary by
        # provider under the legacy compact_prompt_providers config.
        # ---- Hybrid catalog tier ---------------------------------------
        # (Lever 1 of skills-and-token-optimization-research.md, applied to
        # tool schemas.)
        #
        # The router activates whole GROUPS, which is right for workflow
        # completeness but expensive at the top end: the playnite group is 31
        # tools / ~2,700 compacted tokens, desktop is 16, system_control 11.
        # Sending all of that to call one tool is the eager-loading
        # antipattern the research is about.
        #
        # The obvious fix — stub everything and make the model re-request —
        # is wrong here, and the comment above says why: jarvis is a fresh
        # process per call, so a re-request costs a full extra round trip
        # (the entire prompt, resent) rather than a cheap in-session lookup.
        # Pure stubs would trade ~2,700 tokens for a guaranteed second round.
        #
        # So: hybrid. route.matches records which tool each qualifying
        # keyword actually hit, so the tools the user plausibly meant get
        # their FULL schema and stay callable with no extra round trip, while
        # the rest of the group drops to a ~10-token catalog line plus
        # get_tool_schema. The common case costs nothing extra; only the
        # genuinely unanticipated sibling pays a round trip, which is exactly
        # the trade search_tools already makes.
        #
        # Below CATALOG_TIER_MIN_TOOLS this is a no-op, so small groups and
        # every non-confident path behave precisely as before.
        #
        # Bug fix: this used to read `profile.get("catalog_tier", ...)`, but
        # `profile` isn't assigned until INSIDE the `for provider in
        # providers:` loop below (it's resolved per-attempt, since mode can
        # vary by provider — see that loop's own comment). Reading it here,
        # before the loop, raised UnboundLocalError on every single ask
        # once a route was confident and a group had 10+ tools — i.e. on
        # exactly the routes this tier exists to help. active_schemas/
        # compact_schemas/name_only_schemas are built ONCE and shared
        # across every attempt (not per-provider), so the catalog-tier
        # decision has to be made here too — resolved from the first
        # eligible provider's profile, same representative-provider
        # approach current_mode() already uses for the same reason.
        _catalog_tier_profile = (
            _prompt_profile(_provider_label(providers[0]), cfg["defaults"]) if providers else {}
        )
        if (
            _catalog_tier_profile.get("catalog_tier", True)
            and route is not None
            and route.confident
            and len(active_schemas) >= CATALOG_TIER_MIN_TOOLS
        ):
            hot = {name for _group, name, _phrase in (route.matches or [])}
            # Keep the discovery pair callable: demoting get_tool_schema
            # itself to a catalog entry would strand every demoted tool.
            hot.update({"search_tools", "get_tool_schema", "search_commands"})
            full_set, cold = [], []
            for schema in active_schemas.to_list():
                (full_set if schema.get("name") in hot else cold).append(schema)
            if cold:
                active_schemas = OrderedSchemaSet(full_set)
                active_schemas.extend(system_tools.schemas_for_tools(["get_tool_schema"]))
                catalog_entries = system_tools.catalog_schemas_for_prompt(cold)
                compact_schemas = OrderedSchemaSet(
                    system_tools.compact_schemas_for_prompt(active_schemas)
                )
                compact_schemas.extend(catalog_entries)
                name_only_schemas = OrderedSchemaSet(
                    system_tools.name_only_schemas_for_prompt(active_schemas)
                )
                name_only_schemas.extend(
                    system_tools.name_only_schemas_for_prompt(cold)
                )
                # active_schemas is the "raw" (precise mode) offering AND the
                # set discover_sink checks membership against. Catalog
                # entries go in so a promoted tool isn't added twice, but
                # they carry no argument schema — precise mode deliberately
                # opts out of this tier instead (catalog_tier False), since
                # its whole point is maximum schema fidelity.
                active_schemas.extend(catalog_entries)
            else:
                compact_schemas = OrderedSchemaSet(
                    system_tools.compact_schemas_for_prompt(active_schemas)
                )
                name_only_schemas = OrderedSchemaSet(
                    system_tools.name_only_schemas_for_prompt(active_schemas)
                )
        else:
            compact_schemas = OrderedSchemaSet(system_tools.compact_schemas_for_prompt(active_schemas))
            name_only_schemas = OrderedSchemaSet(system_tools.name_only_schemas_for_prompt(active_schemas))
    provider_ref = [None]
    verbosity_ref = ["full"]

    def _discover_sink(names):
        """Phase 5 handoff: called by the tool executor right after a
        search_tools call returns matches. Grows active_schemas (raw),
        compact_schemas, and name_only_schemas *in place* (all three, since
        which one is actually sent depends on the per-provider
        tool_schema_style resolved below) so a matched tool is really
        callable on the model's very next round in this same ask() \u2014 not
        just described in the search_tools reply and then unreachable.

        Enhancement #6: active_schemas/compact_schemas/name_only_schemas
        are OrderedSchemaSet instances now, so "already discovered" is a
        plain `in` check against active_schemas itself \u2014 the separate
        `_discovered_names` set this used to need is gone; a duplicate
        name is structurally impossible to add twice regardless of which
        of the three sets .append()/.extend() is called on.
        """
        from . import tool_registry

        to_add = []
        for name in names or []:
            if not name or name in active_schemas:
                continue
            # Activate the tool's whole group, not just the single matched
            # name — mirrors tool_router.route()'s behavior (Phase 3) so a
            # search_tools hit is just as workflow-complete as a router hit.
            # Without this, finding e.g. spotify_search via search_tools
            # left spotify_play/spotify_control unreachable, forcing a
            # second search_tools call mid-workflow for every sibling tool.
            group = tool_registry.group_of(name)
            group_names = tool_registry.tools_in_group(group) if group else [name]
            for gname in group_names:
                if gname and gname not in active_schemas:
                    to_add.append(gname)

        for name in to_add:
            full = system_tools.schemas_for_tools([name])
            if not full:
                continue
            active_schemas.extend(full)
            compact_schemas.extend(system_tools.compact_schemas_for_prompt(full))
            name_only_schemas.extend(system_tools.name_only_schemas_for_prompt(full))

    # Shared across every provider/key attempt below — see RoundBudget's docstring.
    # Each adapter still gets its own local MAX_TOOL_ROUNDS, but a failover to the
    # next key draws from this same pool instead of getting a fresh 5 rounds on top
    # of whatever the failed key already burned. Built *before* _make_tool_executor
    # now, so it can be handed into the executor and wrapped into every tool call's
    # ToolContext (see tools.ToolContext / _make_tool_executor's docstring).
    # grace=True lets the model make ONE last-chance call after the budget is
    # spent (decision D1; switch off with defaults.grace_call = false).
    # discovery_limit gives search_tools/get_tool_schema/load_skill calls (and
    # made-up tool names) their own small separate pool instead of eating the
    # real work budget (decision D1's other half, F.2; switch off with
    # defaults.discovery_call_budget = 0). Default of 3 covers the F.2
    # evidence's worst case (six straight discovery calls) with room to
    # spare without materially raising how much total work a turn can do.
    _defaults = cfg.get("defaults") or {}
    round_budget = ai_providers.RoundBudget(
        grace=bool(_defaults.get("grace_call", True)),
        discovery_limit=int(_defaults.get("discovery_call_budget", 3) or 0),
    )
    # D5: cap (seconds) on waiting out a 429's OWN stated retry delay before
    # rotating keys. 30 by default; 0 disables (see _short_429_wait_seconds).
    max_429_wait = float(_defaults.get("max_429_wait_seconds", 30) or 0)
    forced_end = None

    tool_executor = _make_tool_executor(
        on_tool_call, full_schemas, on_confirm_request=on_confirm_request,
        cfg=cfg, provider_ref=provider_ref, verbosity_ref=verbosity_ref,
        discover_sink=_discover_sink if tools_enabled else None,
        cache_query=cache_query if tools_enabled else None,
        round_budget=round_budget, conv_id=conversation_id,
    ) if tools_enabled else None

    attempts = []

    # F.11: the previous turn ended by force and offered one call ("say go
    # ahead and I'll run that"). If this message is that go-ahead, run it
    # directly instead of asking a model to rediscover it. Owner surfaces only.
    if tools_enabled and tool_executor and conv_id and not sender_context \
            and _looks_like_short_confirmation(user_text):
        pending_action = _load_pending_action(conv_id)
        known = {sch.get("name") for sch in full_schemas if isinstance(sch, dict)}
        if pending_action and pending_action[0] in known:
            return _run_pending_action(
                pending_action, tool_executor, conv_id, user_text,
                assistant_name, address, trace)

    # Persist the user's half of this turn NOW, before a single provider is
    # contacted. Everything downstream of here can be killed mid-flight —
    # the web UI's Stop button does exactly that (server.js killTree()s the
    # child process, which on Windows is an unconditional taskkill /F and on
    # POSIX a SIGTERM that skips every finally block) — and until this call
    # existed, that killed the user's message with it. See
    # conversations.begin_exchange's docstring.
    #
    # Registering the interrupt handler is a separate step in cli.py, not
    # here: ask() is also called from contexts with no signal handling to
    # own (the scheduler's spawned process, tests), and installing a
    # process-wide handler from a library function would be reaching well
    # outside this function's remit.
    _pending_turn[0] = (conv_id, user_text) if conv_id else None
    if conv_id:
        conversations.begin_exchange(conv_id, user_text)

    # F.7: the tool round-trips of the most recent attempt that got far enough
    # to have any. The next key/provider continues from them instead of being
    # handed a 1600-character recap and repeating read_file / run_shell.
    carried = []

    # F.9: a provider whose model just returned a 503 goes to the back of the
    # line for a short while (never out of it — it is still tried if nothing
    # else answers). sorted() is stable, so healthy providers keep the user's
    # own priority order.
    def _model_cooling(p):
        try:
            return key_health.model_cooling(_provider_label(p), _resolve(p, cfg["defaults"]).get("model"))
        except Exception:  # noqa: BLE001 — health is advice, never a reason to fail
            return False

    providers = sorted(providers, key=_model_cooling)
    dead_hosts = set()   # endpoints that refused a connection during THIS ask

    for provider in providers:
        label = _provider_label(provider)
        provider_ref[0] = label
        profile = _prompt_profile(label, cfg["defaults"])
        verbosity_ref[0] = profile.get("tool_result_verbosity", "full")
        trace.mode = MODE_LABELS.get(profile.get("mode"), profile.get("mode"))
        if tools_enabled:
            style = profile.get("tool_schema_style")
            tool_schemas = (
                name_only_schemas if style == "name_only"
                else active_schemas if style == "raw"
                else compact_schemas
            )
            # Boundary: everything downstream of ai_client.py (adapters,
            # provider-payload builders) expects a plain list, not an
            # OrderedSchemaSet — .to_list() is the one conversion point.
            #
            # Enhancement #6: duplicates are now structurally impossible in
            # active_schemas/compact_schemas/name_only_schemas themselves
            # (OrderedSchemaSet.append() is a no-op on a name already
            # present), so the belt-and-suspenders re-dedupe that used to
            # live here — guarding against every way those three could
            # theoretically grow a duplicate — is redundant and has been
            # removed. Gemini's HTTP 400 "Duplicate function declaration
            # found: X" on a slipped-through duplicate (see
            # jarvis-token-optimization-handoff.md's live-test log) is the
            # reason this mattered; that failure mode is now prevented one
            # layer upstream instead of filtered right before the adapter.
            tool_schemas = tool_schemas.to_list()
            trace.tool_count = len(tool_schemas)
        else:
            tool_schemas = None
        adapter = ai_providers.ADAPTERS.get(provider.get("type"))
        if adapter is None:
            attempts.append((label, f"unknown provider type '{provider.get('type')}'"))
            continue
        host = _host_of(provider)
        if host and host in dead_hosts:
            # Two Ollama entries share localhost:11434 — the second failure was
            # guaranteed (F.8's table). Don't spend a request finding that out.
            attempts.append((label, f"skipped: {host} already refused a connection this turn"))
            continue

        # Ollama (or anything else with no configured keys but still
        # eligible \u2014 i.e. local, no auth needed) gets exactly one pass with
        # no key substituted, same as before multi-key support existed.
        keys = ai_config.provider_keys(provider) or [None]
        # F.9: start at the key that last worked; keys cooling down from a 429
        # go last (still tried if nothing else is left).
        keys = key_health.order_keys(_provider_label(provider), keys)

        for i, key in enumerate(keys, start=1):
            messages = _build_messages(
                persona, commands, user_text, tools_enabled, profile, conv_id,
                route=route if tools_enabled else None,
                sender_ctx=sender_context,
            )
            runs = getattr(tool_executor, "runs", None) if tool_executor else None
            budget = profile.get("tool_result_budget", _MODE_BY_NAME["full"]["tool_result_budget"])
            if carried:
                # The real transcript. No recap on top of it — the results are
                # already there, and the recap's wording only ever confused things.
                messages.extend(_carried_messages(carried, budget))
            elif runs:
                # No transcript to carry (the failed attempt never got a
                # response back), but tools did run: fall back to the recap.
                can_call = round_budget.remaining() > 0 or round_budget.grace_available()
                note = _tool_runs_note(runs, budget, verbosity_ref[0] if verbosity_ref else "full",
                                       can_call_tools=can_call)
                if note:
                    messages.append({"role": "user", "content": note})

            key_label = f"{label} (key {i}/{len(keys)})" if len(keys) > 1 else label
            if on_attempt:
                on_attempt(key_label)

            # "Info" log entry (see logs.py's long-documented-but-never-used
            # "info" direction) — one per attempt, tagged with the same
            # key_label as this attempt's request/response/usage entries
            # below, so the Logs viewer can group "which capacity mode was
            # this API key run under" right alongside its token usage.
            # Purely informational: nothing in this file ever reads it back.
            if conv_id:
                logs.log(
                    conv_id, "info",
                    {
                        "capacity_mode": profile.get("mode"),
                        "capacity_label": MODE_LABELS.get(profile.get("mode"), profile.get("mode")),
                    },
                    provider=key_label,
                )

            resolved = _resolve(provider, cfg["defaults"])
            if key is not None:
                resolved["api_key"] = key

            ai_providers.set_log_context(conv_id, key_label, on_tool_usage=on_tool_result,
                                        base_url=resolved.get("base_url"), on_interim_text=on_interim_text)
            # Reset per attempt, not per ask: a failover to the next key
            # starts a fresh set of rounds, so its round-0 thinking is a new
            # spend and its trace shouldn't be glued onto the failed
            # attempt's.
            ai_providers.set_thinking(think_level)
            try:
                result = adapter(resolved, messages, resolved["timeout"],
                                 tools=tool_schemas, tool_executor=tool_executor,
                                 round_budget=round_budget,
                                 # Prompt-cache knobs live in defaults (and
                                 # can be overridden per provider) — see
                                 # prompt_cache.resolve_settings. Passed
                                 # here rather than read from disk inside
                                 # the adapter so a test can drive caching
                                 # behavior without touching ~/.jarvis.
                                 cfg_defaults=cfg["defaults"])
            except Exception as e:  # one bad provider/key must never take down the whole ask
                result = ai_providers.AIResult(False, error=f"unexpected error: {e}")
            finally:
                ai_providers.clear_log_context()

            # D5 (master plan F.9/F.16): a 429 that stated a short retry
            # delay is worth waiting out ONCE, on this SAME key, rather than
            # abandoning the attempt and rotating — a failed attempt here
            # cost the transcript nothing (no request/response was appended
            # to `messages`), so re-trying loses nothing but the wait
            # itself. Only fires for a genuine rate-limit/quota failure with
            # a stated delay <= max_429_wait; anything else (a bad key, an
            # unstated or long delay) rotates immediately as before.
            if not result.ok:
                wait_s = _short_429_wait_seconds(result.kind, result.error, max_429_wait)
                if wait_s is not None:
                    if on_attempt:
                        on_attempt(f"{key_label} [rate limited \u2014 waiting {wait_s:.0f}s before retrying]")
                    time.sleep(wait_s)
                    ai_providers.set_log_context(conv_id, key_label, on_tool_usage=on_tool_result,
                                                base_url=resolved.get("base_url"), on_interim_text=on_interim_text)
                    ai_providers.set_thinking(think_level)
                    try:
                        result = adapter(resolved, messages, resolved["timeout"],
                                         tools=tool_schemas, tool_executor=tool_executor,
                                         round_budget=round_budget,
                                         cfg_defaults=cfg["defaults"])
                    except Exception as e:
                        result = ai_providers.AIResult(False, error=f"unexpected error: {e}")
                    finally:
                        ai_providers.clear_log_context()
                    if not result.ok:
                        result.error = f"{result.error} [waited {wait_s:.0f}s for the stated rate limit, still failed]"
                    if on_attempt:
                        on_attempt(key_label)

            if result.ok and _is_tool_trace_reply(result.text):
                result = ai_providers.AIResult(
                    False, error="model echoed tool-call traces instead of an answer"
                )

            if result.ok:
                key_health.record_success(_provider_label(provider), resolved.get("model"), key)
                turn_runs = getattr(tool_executor, "runs", None) if tool_executor else None
                extras = _extras_from_runs(turn_runs)

                # --- thinking + trace -------------------------------------
                thinking = ai_providers.get_thinking_trace()
                trace.provider = label
                trace.thinking_chars = len(thinking.get("text") or "")
                for run in (turn_runs or []):
                    trace.note_step(run.get("name"), run.get("arguments"), run.get("result"))
                try:
                    trace.skills = list(skill_stickiness.get_loaded(conv_id) or [])
                except Exception:  # noqa: BLE001 — a trace never fails a turn
                    trace.skills = []

                if thinking.get("text") and think_cfg.get("save", True):
                    extras.append({"type": "thinking", "data": {
                        "text": reasoning.clip_trace(
                            thinking["text"], think_cfg.get("max_trace_chars")),
                        "level": think_level,
                        "rounds": thinking.get("rounds", 0),
                        "requested": thinking.get("requested", 0),
                    }})
                # Part A §5: narration the model sent alongside a tool call
                # ("I'll check that now") \u2014 previously discarded entirely,
                # now saved as its own extra so a page reload/reconnect (or
                # a plain conversation replay) still shows it, the same
                # reasoning the "thinking" extra just above exists for.
                # All five adapters feed this (see on_interim_text's docstring).
                interim_items = ai_providers.get_interim_text()
                if interim_items:
                    extras.append({"type": "interimText", "data": {"items": interim_items}})
                extras.append({"type": "trace", "data": trace.to_dict()})
                if on_trace:
                    try:
                        on_trace(trace, thinking)
                    except Exception:  # noqa: BLE001
                        pass
                # Split the same way the web UI's live view does (see
                # _split_console_dump) so the *saved* exchange matches what
                # was actually shown: a clean reply bubble plus, if there
                # was one, a separate "console" extra the web UI already
                # knows how to replay (renderThreadExtra's "console" case).
                # Only the saved copy is split — the raw, unsplit
                # result.text below is still what's returned/printed live,
                # since the browser (or a plain terminal) needs the whole
                # stream exactly as before; this doesn't change live output
                # at all, only what a page reload sees afterward.
                reply_text = result.text
                ending = None
                if getattr(result, "cut", None) == ai_providers.CUT_LENGTH:
                    # §5: the provider stopped this answer at its output cap;
                    # the model did not finish on its own. Say so rather than
                    # let a cut-off answer pass for a complete one.
                    reply_text = result.text + _CUT_NOTE
                    ending = "truncated"
                    trace.ending = ending
                clean_text, dump_lines = _split_console_dump(reply_text)
                if dump_lines:
                    extras.append({"type": "console", "data": {"dumpLines": dump_lines}})
                    # Same "info" direction as the capacity-mode entry above,
                    # tagged with this (the successful) attempt's key_label —
                    # lets the Logs viewer show a per-API-key console dump
                    # without having to re-derive the split from raw
                    # response bodies itself.
                    if conv_id:
                        logs.log(conv_id, "info", {"console_dump": dump_lines}, provider=key_label)
                exchange_count = conversations.complete_exchange(
                    conv_id, user_text, clean_text, label, extras=extras
                )
                _pending_turn[0] = None
                _spawn_title_update(conv_id, exchange_count)
                return AskResult(True, text=reply_text, provider=label, attempts=attempts,
                                 assistant_name=assistant_name, address_user_as=address,
                                 usage=result.usage, ending=ending)

            # A rejection of the REQUEST itself (Groq's "Tool choice is none,
            # but model called a tool", a tool-argument schema mismatch, ...)
            # is not a key problem: the identical payload gets the identical
            # answer from every other key on this provider. Move on to the
            # next provider instead of burning the rest of this one's keys.
            # Only a whitelist of observed shapes qualifies — see
            # ai_providers.is_request_shape_error — so a bad/rate-limited key
            # still rotates exactly as before.
            failure = result.error
            if result.kind in (ai_providers.KIND_BUDGET, ai_providers.KIND_CUTOFF):
                # Not a key problem: the budget is spent and the model still
                # wanted a tool (KIND_BUDGET), or its reply hit the output cap
                # while writing a call (KIND_CUTOFF, §5). Every other key would
                # do the same — stop rotating and report what ran instead.
                forced_end = result
                attempts.append((key_label, failure))
                trace.note_attempt_failed(key_label, failure)
                break
            key_health.record_failure(_provider_label(provider), resolved.get("model"), key,
                                      result.kind, failure)
            skip_remaining_keys = ai_providers.is_request_shape_error(failure) and i < len(keys)
            if skip_remaining_keys:
                failure = (f"{failure} [request rejected on its shape, not because of the key - "
                           f"skipping this provider's other {len(keys) - i} key(s)]")
            elif result.kind == ai_providers.KIND_NETWORK and "couldn't connect" in str(failure):
                # Same endpoint for every key AND for any sibling provider.
                if host:
                    dead_hosts.add(host)
                if i < len(keys):
                    skip_remaining_keys = True
                    failure = (f"{failure} [the service refused the connection - "
                               f"skipping this provider's other {len(keys) - i} key(s)]")
            elif result.kind == ai_providers.KIND_OVERLOAD and i < len(keys):
                # A model-wide condition (503 "high demand"): another key on the
                # same model has little better odds and each hop costs the turn
                # its state (F.9).
                skip_remaining_keys = True
                failure = (f"{failure} [the model is overloaded, not the key - "
                           f"skipping this provider's other {len(keys) - i} key(s)]")
            attempts.append((key_label, failure))
            trace.note_attempt_failed(key_label, failure)
            # Remember how far this attempt got. An attempt that failed before
            # any response came back has no history: keep what we had.
            new_carry = _carried_scaffold(getattr(result, "tool_history", None))
            if new_carry:
                carried = new_carry
            if skip_remaining_keys:
                break
        if forced_end is not None:
            break

    if forced_end is not None:
        turn_runs = getattr(tool_executor, "runs", None) if tool_executor else None
        is_cutoff = forced_end.kind == ai_providers.KIND_CUTOFF
        reply = _forced_ending_reply(turn_runs, forced_end.pending, cutoff=is_cutoff)
        ending = "cutoff" if is_cutoff else "forced"
        trace.degraded = True
        trace.ending = ending
        if conv_id:
            forced_extras = _extras_from_runs(turn_runs)
            # F.11: keep the call the reply just offered, so a bare "go ahead"
            # next turn runs it. Nothing to keep after a cutoff (no usable call).
            offered = None if is_cutoff else _pending_action_extra(
                forced_end.pending, {sch.get("name") for sch in full_schemas if isinstance(sch, dict)})
            if offered:
                forced_extras.append(offered)
            exchange_count = conversations.complete_exchange(
                conv_id, user_text, reply,
                "(reporting what ran — the model couldn't finish)",
                extras=forced_extras,
            )
            _spawn_title_update(conv_id, exchange_count)
        _pending_turn[0] = None
        return AskResult(True, text=reply, provider=None, attempts=attempts,
                         assistant_name=assistant_name, address_user_as=address,
                         degraded=True, ending=ending)

    # Every provider failed on the closing text call. That used to always
    # mean "no provider answered" and get reported as a hard failure — but
    # if a mutating tool call already completed cleanly earlier this turn
    # (see _completed_mutations), the actual requested action DID happen;
    # only the follow-up "compose a nice reply about it" call failed on
    # every remaining provider (all keys genuinely exhausted after a long
    # tool-calling turn is the common case). That's exactly the bug
    # reported against the scheduler: a scheduled task would run its tools,
    # complete the real work, then still get reported as an error because
    # ask() kept cycling every remaining provider for a closing sentence
    # that never came — "cycled thru every token provider and gave an
    # error EVEN THOUGH the task is done." Recording that as an abandoned
    # exchange and returning ok=False was true to what happened to the
    # LAST attempt, but false to what happened to the ask() call as a
    # whole. Synthesize a plain summary of what ran instead.
    turn_runs = getattr(tool_executor, "runs", None) if tool_executor else None
    completed = _completed_mutations(turn_runs)
    if completed:
        summary = _summarize_completed_mutations(completed)
        trace.degraded = True
        trace.ending = "no_provider"
        if conv_id:
            exchange_count = conversations.complete_exchange(
                conv_id, user_text, summary,
                "(no provider — reporting completed actions)",
                extras=_extras_from_runs(turn_runs),
            )
            _spawn_title_update(conv_id, exchange_count)
        _pending_turn[0] = None
        return AskResult(True, text=summary, provider=None, attempts=attempts,
                         assistant_name=assistant_name, address_user_as=address,
                         degraded=True, ending="no_provider")

    # The turn is still real — the user asked something and got nothing at
    # all, not even a completed side effect — so it's recorded as such
    # rather than left pending forever (a pending turn would otherwise be
    # re-abandoned by the next process's interrupt handler and look like it
    # was cancelled).
    if conv_id:
        conversations.abandon_exchange(
            conv_id, user_text, reason="no provider answered",
            extras=_extras_from_runs(turn_runs),
        )
    _pending_turn[0] = None
    return AskResult(False, attempts=attempts, assistant_name=assistant_name, address_user_as=address)