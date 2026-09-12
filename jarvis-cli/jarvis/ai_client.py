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

from . import ai_config, ai_providers, command_tools, conversations, memory, playnite_config, stats, tool_safety
from . import discovery_cache
from . import tool_result_shaping
from . import tool_router
from . import tools as system_tools

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_TOKENS = 1700
DEFAULT_ASSISTANT_NAME = "J.A.R.V.I.S"
DEFAULT_ADDRESS = "sir"
DEFAULT_TOOLS_ENABLED = True

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

    __slots__ = ("ok", "text", "provider", "attempts", "assistant_name", "address_user_as", "usage")

    def __init__(self, ok, text=None, provider=None, attempts=None,
                 assistant_name=DEFAULT_ASSISTANT_NAME, address_user_as=DEFAULT_ADDRESS,
                 usage=None):
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


def _provider_label(provider):
    return provider.get("name") or provider.get("type") or "provider"


def _eligible_providers(providers, defaults=None):
    """Enabled, and either local (ollama — no key needed) or actually has at
    least one real key (see ai_config.provider_keys — handles both the
    'api_keys' list and the older singular 'api_key'). This is the single
    point where an empty-key starter-template entry quietly gets skipped
    instead of being "tried and failed" every time.

    When defaults.provider_priority is set, eligible providers are sorted by
    that list (unknown names keep their relative array order at the end)."""
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


def _tools_blurb(compact, ultra, has_playnite, has_spotify):
    """Only advertise tools that are actually in this session's schema.
    Groq 400s if the prompt names a tool that isn't in request.tools."""
    if compact:
        if ultra:
            # Ultra (50% Capacity): tool_schema_style is already "name_only"
            # here, so the model gets almost nothing about each tool up
            # front \u2014 this blurb is the only place the essential behavioral
            # rules (confirm-before-destructive, don't guess a command name,
            # screenshots are for the user not you) survive. Cut everything
            # that's just elaboration on top of those rules.
            parts = [
                "Tools listed by name only \u2014 call with no args first if unsure, "
                "you'll get its schema back. Confirm before install/delete/off/eval. "
                "Unsure of a saved command's exact name? search_commands first, never "
                "guess. Screenshots only give you ok/path \u2014 never describe the image. "
                "Web questions: web_search then web_fetch. Installs: package_search, "
                "ask, package_install confirm=true. Video: ytdl_info, then ytdl_formats "
                "if needed, then ytdl_download confirm=true."
            ]
            if has_spotify:
                parts.append("Spotify: spotify_search then spotify_play; never run_command.")
            if has_playnite:
                parts.append(
                    "Playnite: find_game/query_games then playnite_launch_game; "
                    "don't claim launch unless it succeeded."
                )
            return " ".join(parts)

        parts = [
            "Tools are listed by name only. Call one when you need it. "
            "If it needs arguments you don't know, call it with no arguments — "
            "you will get its schema, then call it again. "
            "Confirm before install/delete/off/eval. "
            "COMMANDS: only some are listed above — if you're not sure of the exact "
            "saved command name, call search_commands (with a keyword, or no query "
            "for the full list) before run_command/run_chain. Never guess a name. "
            "Screenshots: take_screenshot (image is for the user, not you). "
            "Web: web_search then web_fetch. Install: package_search, ask, then "
            "package_install confirm=true. "
            "Video/audio: ytdl_info for metadata, ytdl_formats for exact format ids, "
            "ytdl_download to fetch (confirm first; single video by default, playlist=true for more, capped)."
        ]
        if has_spotify:
            parts.append(
                "Spotify: spotify_search then spotify_play; never run_command."
            )
        if has_playnite:
            parts.append(
                "Playnite: find_game or query_games, then playnite_launch_game. "
                "Don't claim launch unless that tool succeeded."
            )
        return " ".join(parts)

    parts = [
        "Tools: commands; system info (get_*); radio_status, wifi_set, bluetooth_set; git_run; "
        "take_screenshot; web_search + web_fetch; packages "
        "(package_* for winget, choco, scoop, pip, pipx, npm); memory_*. "
        "ONLY call tools that appear in your tool list. Never invent a tool name. "
        "COMMANDS: the 'Saved commands' list above is only a partial preview. Before "
        "run_command or run_chain, if you aren't certain of the exact saved command "
        "name, call search_commands first — pass a keyword, or no query to list every "
        "saved command. Do this instead of guessing a name and hoping it resolves. "
        "RADIOS: wifi_set/bluetooth_set action on|off. Off requires confirm=true (may need Admin). "
        "GIT: git_run with an allowlisted command (status, log, diff, add, commit, pull, push, …). "
        "reset/clean/force-push/clone need confirm=true. Not a shell. "
        "SCREENSHOT: take_screenshot saves the desktop and shows it in the UI. "
        "You only get a tiny ok/path — never describe pixels or ask for the image. Confirm in one short line. "
        "VIDEO/AUDIO: ytdl_info gets metadata (title, duration, qualities, ffmpeg_available) for a URL with no "
        "download. ytdl_formats lists exact format_ids when the simple quality presets aren't specific enough. "
        "ytdl_download fetches it (mode='video' or 'audio', quality/container/codec/subs/thumbnail/metadata/"
        "SponsorBlock all optional, output_dir to save somewhere specific) and hands the file to the user in "
        "the UI — confirm first. Single video by default; playlist=true fetches more (hard-capped), still one "
        "confirm. "
        "You only get a tiny ok/path back — never claim details about the content you weren't told. "
        "WEB: For 'best X', news, prices, how-tos, or anything that may have changed, "
        "MUST web_search, then web_fetch 1–3 URLs, then summarize with markdown source links. "
        "SOFTWARE INSTALL: package_search, ASK user, package_install confirm=true. Never guess ids. "
        "MEMORY: only facts relevant to this message are injected. If you need others, "
        "memory_search. memory_save for durable facts (prefs, names, 'remember that'). "
        "Chat history is short-term. No passwords/API keys. memory_forget to delete. "
        "Confirm before launch/delete/install/eval."
    ]
    if has_spotify:
        parts.append(
            "SPOTIFY: Free-account friendly. Do NOT use run_command. "
            "Open app: spotify_open. Play: spotify_search then spotify_play (opens the desktop app — "
            "user may need one click to play; Spotify blocks remote start on Free). "
            "Pause/skip: spotify_control (media keys). Queue/volume remote needs Premium. "
            "Never claim music started unless the tool returned ok."
        )
    if has_playnite:
        parts.append(
            "PLAYNITE: ALWAYS playnite_query_games WITH filters or groupBy — never dump the library. "
            "find_game is a specific title lookup. 'Play X' → playnite_launch_game (same as Play in Playnite; "
            "Steam/Epic use a virtual LibraryPlugin action). Extra launchers: list_game_actions then "
            "launch_action. Never PUT LibraryPlugin into gameActions. Never say launched unless playnite_launch_* succeeded."
        )
    return " ".join(parts)


def _system_prompt(persona, commands_ctx, freq_ctx, tools_enabled,
                   compact_tools=False, compact_persona=False, ultra=False, has_history=False,
                   memory_ctx="", has_playnite=False, has_spotify=False, other_convos_ctx="",
                   playnite_freq_games=None, precise=False, pack_instructions_ctx=""):
    name = persona.get("assistant_name") or DEFAULT_ASSISTANT_NAME
    address = persona.get("address_user_as") or DEFAULT_ADDRESS
    extra = (persona.get("extra_instructions") or "").strip()

    parts = []
    if compact_persona:
        if ultra:
            # Ultra (50% Capacity): trims the compact persona line further —
            # "dry wit" is flavor, not a rule the model needs to be told
            # explicitly to follow; everything else here is load-bearing
            # (name, how to address the user, don't claim unconfirmed actions).
            parts.append(
                f"You are {name}, a local AI butler. Address the user as "
                f'"{address}" sometimes. Never claim you did something unless a tool confirmed it.'
            )
        else:
            parts.append(
                f"You are {name}, a local AI butler. Dry wit, concise. Address the user as "
                f'"{address}" sometimes. Never claim you did something unless a tool confirmed it.'
            )
    else:
        parts.append(
            f"You are {name}, a private AI assistant running locally for one user on their own "
            f"computer \u2014 think a supremely capable, unflappable AI butler: dry wit, complete "
            f"composure, quiet confidence, never groveling or over-apologizing. Address the user as "
            f'"{address}" sometimes, naturally \u2014 not in every single sentence. Keep replies '
            f"conversational and to the point: a sentence or two for anything simple, more only when "
            f"the question genuinely calls for it. Be honest about your limits. Never claim "
            f"to have taken an action you didn't actually take."
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
        parts.append(_tools_blurb(compact_tools, ultra, has_playnite, has_spotify))
        if pack_instructions_ctx:
            parts.append(pack_instructions_ctx)
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
    return "\n\n".join(parts)


def _build_messages(persona, commands, user_text, tools_enabled, profile, conversation_id, route=None):
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
    system_prompt = _system_prompt(
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
    )
    messages = [{"role": "system", "content": system_prompt}]
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
                         discover_sink=None, cache_query=None):
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
        if (tool_safety.requires_confirmation(name)
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
        result = system_tools.execute_tool(name, arguments)
        verbosity = verbosity_ref[0] if verbosity_ref else "full"
        result = tool_result_shaping.shape_result(name, result, verbosity)
        cache[key] = result
        run_entry = {"name": name, "arguments": arguments, "result": result}
        if confirm_meta is not None:
            run_entry["confirm"] = confirm_meta
        runs.append(run_entry)

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
    return extras


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


def _tool_runs_note(runs, char_budget, verbosity="full"):
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
    mutated = [n for n in ran if _is_mutating_tool(n)]
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
        parts.append(
            "No launch/install/command has run yet. If the user asked to play/launch/"
            "install something, you MUST call the real tool now (playnite_launch_action, "
            "package_install, run_command, …). Do not claim it already launched. "
            "Do not write tool calls as plain text."
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


_TOOL_TRACE_LINE = re.compile(r"^\[(called |tool result)", re.I)
_TOOL_TRACE_ANY = re.compile(r"\[called\s+[A-Za-z0-9_]+\s+with\s+\{", re.I)


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
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(args, **kwargs)
    except Exception:
        pass  # title generation is cosmetic — never let it break an ask


def ask(user_text, commands=None, on_attempt=None, on_tool_call=None, on_tool_result=None, conversation_id=None,
        on_confirm_request=None, on_route=None):
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

    on_tool_call(name), if given, fires right before each tool call Jarvis
    makes while answering (battery, wifi, location, ...) \u2014 same idea, live
    trace of what's actually happening. Tools are looked up fresh from
    ai_config.json's defaults.tools_enabled on every call, same as
    everything else here; set it to false to turn tool calling off
    entirely (e.g. to keep every ask to a single request).

    conversation_id picks which conversation (see conversations.py) this
    exchange belongs to and gets appended to. When omitted, the CLI's
    on-disk "current" conversation is used (auto-created on first ever
    use) — the web UI instead always passes one explicitly, since each
    browser tab tracks its own active conversation.

    on_confirm_request(name, arguments, risk_note), if given, gates any
    tool call flagged confirm_required in tool_safety.json (see
    _make_tool_executor). Tools requiring confirmation fail closed (never
    run) if this isn't supplied.

    on_route(route), if given, fires once right after the local router
    (tool_router.route()) decides what this message plausibly needs \u2014
    same live-trace idea as on_attempt, but for the routing decision
    itself (route.tools/route.groups/route.matches) rather than a
    provider attempt. Only called when tools are enabled, since routing
    only happens on that branch.
    """
    cfg = ai_config.load_ai_config()
    persona = cfg["persona"]
    assistant_name = persona.get("assistant_name") or DEFAULT_ASSISTANT_NAME
    address = persona.get("address_user_as") or DEFAULT_ADDRESS

    conv_id = conversation_id if conversations.is_valid_id(conversation_id) else conversations.get_current_id()

    providers = _eligible_providers(cfg["providers"], cfg["defaults"])
    if not providers:
        return AskResult(False, assistant_name=assistant_name, address_user_as=address)

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
        route = tool_router.route(user_text)
        if on_route:
            on_route(route)
        # Phase 5 of the token-optimization plan (see new_plan.md): when the
        # router has no opinion, don't fall all the way back to the full
        # catalog — offer only the small always-available search_tools
        # discovery tool instead. A real tool call the model needs is still
        # reachable (search_tools -> _make_tool_executor's discover_sink
        # below grows active/compact/name_only_schemas in place, so a match
        # becomes callable on the very next round without a second full
        # prompt resend), it's just not offered up front on spec.
        active_schemas = OrderedSchemaSet(
            system_tools.schemas_for_tools(route.tools)
            if route.confident else system_tools.DISCOVERY_AND_COMMANDS_SCHEMAS
        )

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

    tool_executor = _make_tool_executor(
        on_tool_call, full_schemas, on_confirm_request=on_confirm_request,
        cfg=cfg, provider_ref=provider_ref, verbosity_ref=verbosity_ref,
        discover_sink=_discover_sink if tools_enabled else None,
        cache_query=cache_query if tools_enabled else None,
    ) if tools_enabled else None

    attempts = []

    for provider in providers:
        label = _provider_label(provider)
        provider_ref[0] = label
        profile = _prompt_profile(label, cfg["defaults"])
        verbosity_ref[0] = profile.get("tool_result_verbosity", "full")
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
        else:
            tool_schemas = None
        adapter = ai_providers.ADAPTERS.get(provider.get("type"))
        if adapter is None:
            attempts.append((label, f"unknown provider type '{provider.get('type')}'"))
            continue

        # Ollama (or anything else with no configured keys but still
        # eligible \u2014 i.e. local, no auth needed) gets exactly one pass with
        # no key substituted, same as before multi-key support existed.
        keys = ai_config.provider_keys(provider) or [None]

        for i, key in enumerate(keys, start=1):
            messages = _build_messages(
                persona, commands, user_text, tools_enabled, profile, conv_id,
                route=route if tools_enabled else None,
            )
            runs = getattr(tool_executor, "runs", None) if tool_executor else None
            if runs:
                budget = profile.get("tool_result_budget", _MODE_BY_NAME["full"]["tool_result_budget"])
                note = _tool_runs_note(runs, budget, verbosity_ref[0] if verbosity_ref else "full")
                if note:
                    messages.append({"role": "user", "content": note})

            key_label = f"{label} (key {i}/{len(keys)})" if len(keys) > 1 else label
            if on_attempt:
                on_attempt(key_label)

            resolved = _resolve(provider, cfg["defaults"])
            if key is not None:
                resolved["api_key"] = key

            ai_providers.set_log_context(conv_id, key_label, on_tool_usage=on_tool_result)
            try:
                result = adapter(resolved, messages, resolved["timeout"],
                                 tools=tool_schemas, tool_executor=tool_executor)
            except Exception as e:  # one bad provider/key must never take down the whole ask
                result = ai_providers.AIResult(False, error=f"unexpected error: {e}")
            finally:
                ai_providers.clear_log_context()

            if result.ok and _is_tool_trace_reply(result.text):
                result = ai_providers.AIResult(
                    False, error="model echoed tool-call traces instead of an answer"
                )

            if result.ok:
                turn_runs = getattr(tool_executor, "runs", None) if tool_executor else None
                extras = _extras_from_runs(turn_runs)
                exchange_count = conversations.append_exchange(
                    conv_id, user_text, result.text, label, extras=extras
                )
                _spawn_title_update(conv_id, exchange_count)
                return AskResult(True, text=result.text, provider=label, attempts=attempts,
                                 assistant_name=assistant_name, address_user_as=address,
                                 usage=result.usage)

            attempts.append((key_label, result.error))

    return AskResult(False, attempts=attempts, assistant_name=assistant_name, address_user_as=address)