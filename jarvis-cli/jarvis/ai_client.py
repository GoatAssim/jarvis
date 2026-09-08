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

from . import ai_config, ai_providers, conversations, memory, playnite_config, stats, tool_safety
from . import tool_result_shaping
from . import tools as system_tools

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_TOKENS = 700
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
#   tool_schema_style      \u2014 "compact" (types/enums/required, short descs) or
#                            "name_only" (just names \u2014 the model re-requests a
#                            schema the first time it calls a tool needing
#                            args it didn't supply; see _make_tool_executor)
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
        "playnite_freq_games": 5,
        "skip_other_convos": False,
        "tool_schema_style": "compact",
        "tool_result_budget": 1600,
        "tool_result_verbosity": "medium",
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
        "playnite_freq_games": 3,
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

    __slots__ = ("ok", "text", "provider", "attempts", "assistant_name", "address_user_as")

    def __init__(self, ok, text=None, provider=None, attempts=None,
                 assistant_name=DEFAULT_ASSISTANT_NAME, address_user_as=DEFAULT_ADDRESS):
        self.ok = ok
        self.text = text
        self.provider = provider
        self.attempts = attempts or []          # [(provider_label, error_reason), ...]
        self.assistant_name = assistant_name
        self.address_user_as = address_user_as


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


def _tools_blurb(compact, has_playnite, has_spotify):
    """Only advertise tools that are actually in this session's schema.
    Groq 400s if the prompt names a tool that isn't in request.tools."""
    if compact:
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
                   compact_tools=False, compact_persona=False, has_history=False,
                   memory_ctx="", has_playnite=False, has_spotify=False, other_convos_ctx="",
                   playnite_freq_games=None):
    name = persona.get("assistant_name") or DEFAULT_ASSISTANT_NAME
    address = persona.get("address_user_as") or DEFAULT_ADDRESS
    extra = (persona.get("extra_instructions") or "").strip()

    parts = []
    if compact_persona:
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
        parts.append(_tools_blurb(compact_tools, has_playnite, has_spotify))
    if memory_ctx:
        parts.append(memory_ctx)
    if other_convos_ctx:
        parts.append(other_convos_ctx)
    if playnite_freq_games is None:
        fallback_mode = "compact" if compact_persona else "full"
        playnite_freq_games = _MODE_BY_NAME[fallback_mode]["playnite_freq_games"]
    playnite_ctx = playnite_config.frequent_games_context(
        playnite_freq_games,
        compact=compact_persona,
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


def _build_messages(persona, commands, user_text, tools_enabled, profile, conversation_id):
    compact = profile.get("compact_tools_blurb", False)
    commands_ctx = _commands_context(
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
    offered = system_tools.tool_schemas_for_session() if tools_enabled else []
    offered_names = {s["name"] for s in offered}
    system_prompt = _system_prompt(
        persona,
        commands_ctx,
        freq_ctx,
        tools_enabled,
        compact_tools=compact,
        compact_persona=compact_persona,
        has_history=bool(prior_turns),
        memory_ctx=memory_ctx,
        has_playnite=any(n.startswith("playnite_") for n in offered_names),
        has_spotify=any(n.startswith("spotify_") for n in offered_names),
        other_convos_ctx=other_convos_ctx,
        playnite_freq_games=profile.get("playnite_freq_games"),
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


def risk_review(tool_name, arguments, cfg, exclude_label=None):
    """Ask a *different* configured AI provider than the one currently
    answering to explain, in plain language, what a tool call will do and
    how dangerous/irreversible it is. Best-effort only: never raises, and
    returns None (no risk note attached) if no other provider is
    configured or the call fails \u2014 a missing/misconfigured second provider
    should never block or crash the primary ask, it just means the
    confirmation prompt won't have an AI opinion attached.
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

        try:
            args_s = json.dumps(arguments or {}, default=str)
        except TypeError:
            args_s = str(arguments)
        prompt = (
            "A personal-assistant program is about to run this tool call on the "
            "user's own machine:\n"
            f"  tool: {tool_name}\n"
            f"  arguments: {args_s}\n\n"
            "In 2-3 short plain-language sentences: (1) explain exactly what this "
            "specific call will do, and (2) rate how dangerous/irreversible it is "
            "(none / low / medium / high) with a one-line reason. No preamble, no "
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


def _make_tool_executor(on_tool_call, schemas=None, on_confirm_request=None,
                         cfg=None, provider_ref=None, verbosity_ref=None):
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
    """
    cache = {}
    runs = []
    by_name = {
        s.get("name"): s
        for s in (schemas or [])
        if isinstance(s, dict) and s.get("name")
    }

    def _executor(name, arguments):
        arguments = arguments or {}
        key = _cache_key(name, arguments)
        if key in cache:
            return cache[key]
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

        if tool_safety.requires_confirmation(name):
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
            if tool_safety.requires_ai_review(name) and cfg is not None:
                exclude_label = (provider_ref or [None])[0]
                risk_note = risk_review(name, arguments, cfg, exclude_label=exclude_label)

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
                runs.append({"name": name, "arguments": arguments, "result": result})
                return result

        if on_tool_call:
            try:
                on_tool_call(name, arguments)
            except TypeError:
                on_tool_call(name)
        result = system_tools.execute_tool(name, arguments)
        verbosity = verbosity_ref[0] if verbosity_ref else "full"
        result = tool_result_shaping.shape_result(name, result, verbosity)
        cache[key] = result
        runs.append({"name": name, "arguments": arguments, "result": result})
        return result

    _executor.runs = runs
    return _executor


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


def _tool_runs_note(runs, char_budget):
    """Tell the next model what already ran — without implying side effects
    (launch/install) happened if they didn't."""
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
        try:
            args_s = json.dumps(run.get("arguments") or {}, default=str)
            result_s = json.dumps(run.get("result"), default=str)
        except TypeError:
            args_s = str(run.get("arguments"))
            result_s = str(run.get("result"))
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
    because one title request happened to fail."""
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


def ask(user_text, commands=None, on_attempt=None, on_tool_call=None, conversation_id=None,
        on_confirm_request=None):
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
        compact_schemas = system_tools.compact_schemas_for_prompt(full_schemas)
        name_only_schemas = system_tools.name_only_schemas_for_prompt(full_schemas)
    provider_ref = [None]
    verbosity_ref = ["full"]
    tool_executor = _make_tool_executor(
        on_tool_call, full_schemas, on_confirm_request=on_confirm_request,
        cfg=cfg, provider_ref=provider_ref, verbosity_ref=verbosity_ref,
    ) if tools_enabled else None

    attempts = []

    for provider in providers:
        label = _provider_label(provider)
        provider_ref[0] = label
        profile = _prompt_profile(label, cfg["defaults"])
        verbosity_ref[0] = profile.get("tool_result_verbosity", "full")
        if tools_enabled:
            tool_schemas = (
                name_only_schemas if profile.get("tool_schema_style") == "name_only"
                else compact_schemas
            )
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
                persona, commands, user_text, tools_enabled, profile, conv_id
            )
            runs = getattr(tool_executor, "runs", None) if tool_executor else None
            if runs:
                budget = profile.get("tool_result_budget", _MODE_BY_NAME["full"]["tool_result_budget"])
                note = _tool_runs_note(runs, budget)
                if note:
                    messages.append({"role": "user", "content": note})

            key_label = f"{label} (key {i}/{len(keys)})" if len(keys) > 1 else label
            if on_attempt:
                on_attempt(key_label)

            resolved = _resolve(provider, cfg["defaults"])
            if key is not None:
                resolved["api_key"] = key

            try:
                result = adapter(resolved, messages, resolved["timeout"],
                                 tools=tool_schemas, tool_executor=tool_executor)
            except Exception as e:  # one bad provider/key must never take down the whole ask
                result = ai_providers.AIResult(False, error=f"unexpected error: {e}")

            if result.ok and _is_tool_trace_reply(result.text):
                result = ai_providers.AIResult(
                    False, error="model echoed tool-call traces instead of an answer"
                )

            if result.ok:
                exchange_count = conversations.append_exchange(conv_id, user_text, result.text, label)
                _maybe_update_title(cfg, conv_id, exchange_count, user_text, result.text)
                return AskResult(True, text=result.text, provider=label, attempts=attempts,
                                 assistant_name=assistant_name, address_user_as=address)

            attempts.append((key_label, result.error))

    return AskResult(False, attempts=attempts, assistant_name=assistant_name, address_user_as=address)