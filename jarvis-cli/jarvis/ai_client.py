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

from . import ai_config, ai_providers, history, memory, playnite_config, stats
from . import tools as system_tools

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_TOKENS = 700
DEFAULT_ASSISTANT_NAME = "J.A.R.V.I.S"
DEFAULT_ADDRESS = "sir"
DEFAULT_TOOLS_ENABLED = True

MAX_COMMANDS_LISTED = 30  # cap how many command names+descriptions go into every prompt
COMPACT_MAX_COMMANDS = 8
COMPACT_DESC_MAX_LEN = 60
COMPACT_HISTORY_CHAR_BUDGET = 1800
COMPACT_HISTORY_EXCHANGES = 4
DEFAULT_COMPACT_PROMPT_PROVIDERS = ("groq",)


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
    names = list(commands.items())[:max_listed]
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
    if compact:
        return "Commands:\n" + listing
    return "Saved commands:\n" + listing


def _prompt_profile(provider_name, defaults):
    """Return prompt-size knobs for a provider. Compact profiles trim context
    for rate-sensitive hosts (Groq by default)."""
    compact_names = defaults.get("compact_prompt_providers")
    if compact_names is None:
        compact_names = list(DEFAULT_COMPACT_PROMPT_PROVIDERS)
    if provider_name in compact_names:
        return {
            "max_commands": defaults.get("compact_max_commands", COMPACT_MAX_COMMANDS),
            "desc_max_len": COMPACT_DESC_MAX_LEN,
            "history_char_budget": defaults.get(
                "compact_history_char_budget", COMPACT_HISTORY_CHAR_BUDGET
            ),
            "history_exchanges": defaults.get(
                "compact_history_exchanges", COMPACT_HISTORY_EXCHANGES
            ),
            "include_freq": False,
            "compact_tools_blurb": True,
            "compact_persona": True,
        }
    return {
        "max_commands": MAX_COMMANDS_LISTED,
        "desc_max_len": None,
        "history_char_budget": None,
        "history_exchanges": None,
        "include_freq": True,
        "compact_tools_blurb": False,
        "compact_persona": False,
    }


def _tools_blurb(compact, has_playnite, has_spotify):
    """Only advertise tools that are actually in this session's schema.
    Groq 400s if the prompt names a tool that isn't in request.tools."""
    if compact:
        parts = [
            "Tools: commands; system info (get_*); wifi_set/bluetooth_set/radio_status; git_run; "
            "web_search/web_fetch; packages "
            "(winget/choco/scoop/pip/pipx/npm); memory_*. "
            "ONLY call tools in your tool list — never invent names. "
            "Wi-Fi/BT off needs confirm=true. git_run allowlisted only (status/log/pull/commit/…). "
            "For 'best X'/news: web_search then web_fetch. "
            "Install: package_search then ASK then package_install confirm=true. "
            "MEMORY: matching facts are injected; else memory_search. "
            "memory_save for durable facts; no passwords."
        ]
        if has_spotify:
            parts.append(
                "Spotify: NEVER run_command. Open: spotify_open. Play: spotify_search then spotify_play "
                "(desktop app; Free may need a click to play). Pause/skip: spotify_control."
            )
        if has_playnite:
            parts.append(
                "Playnite: query_games WITH filters; find_game is name lookup only. "
                "Actions: list then launch. Never claim launched unless playnite_launch_* succeeded."
            )
        return " ".join(parts)

    parts = [
        "Tools: commands; system info (get_*); radio_status, wifi_set, bluetooth_set; git_run; "
        "web_search + web_fetch; packages "
        "(package_* for winget, choco, scoop, pip, pipx, npm); memory_*. "
        "ONLY call tools that appear in your tool list. Never invent a tool name. "
        "RADIOS: wifi_set/bluetooth_set action on|off. Off requires confirm=true (may need Admin). "
        "GIT: git_run with an allowlisted command (status, log, diff, add, commit, pull, push, …). "
        "reset/clean/force-push/clone need confirm=true. Not a shell. "
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
            "find_game is a specific title lookup. Actions: playnite_list_game_actions then "
            "playnite_launch_action. Never say a game launched unless playnite_launch_* returned success."
        )
    return " ".join(parts)


def _system_prompt(persona, commands_ctx, freq_ctx, tools_enabled,
                   compact_tools=False, compact_persona=False, has_history=False,
                   memory_ctx="", has_playnite=False, has_spotify=False):
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
    playnite_ctx = playnite_config.frequent_games_context(
        8 if compact_persona else None
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


def _build_messages(persona, commands, user_text, tools_enabled, profile):
    compact = profile.get("compact_tools_blurb", False)
    commands_ctx = _commands_context(
        commands,
        max_listed=profile["max_commands"],
        desc_max_len=profile["desc_max_len"],
        compact=compact,
    )
    freq_ctx = stats.frequent_commands_context(commands) if profile["include_freq"] else ""
    prior_turns = history.recent_messages(
        max_exchanges=profile["history_exchanges"],
        char_budget=profile["history_char_budget"],
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


def _make_tool_executor(on_tool_call):
    """Shared across every provider/key in one ask() so a failover never
    re-runs the same command, Playnite action, or web fetch. Cache hits
    still return the original result (no second launch / install / HTTP)."""
    cache = {}
    runs = []

    def _executor(name, arguments):
        arguments = arguments or {}
        key = _cache_key(name, arguments)
        if key in cache:
            return cache[key]
        if on_tool_call:
            on_tool_call(name)
        result = system_tools.execute_tool(name, arguments)
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


def ask(user_text, commands=None, on_attempt=None, on_tool_call=None):
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
    """
    cfg = ai_config.load_ai_config()
    persona = cfg["persona"]
    assistant_name = persona.get("assistant_name") or DEFAULT_ASSISTANT_NAME
    address = persona.get("address_user_as") or DEFAULT_ADDRESS

    providers = _eligible_providers(cfg["providers"], cfg["defaults"])
    if not providers:
        return AskResult(False, assistant_name=assistant_name, address_user_as=address)

    tools_enabled = cfg["defaults"].get("tools_enabled", DEFAULT_TOOLS_ENABLED)
    tool_schemas = system_tools.tool_schemas_for_session() if tools_enabled else None
    tool_executor = _make_tool_executor(on_tool_call) if tools_enabled else None

    attempts = []

    for provider in providers:
        label = _provider_label(provider)
        profile = _prompt_profile(label, cfg["defaults"])
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
                persona, commands, user_text, tools_enabled, profile
            )
            runs = getattr(tool_executor, "runs", None) if tool_executor else None
            if runs:
                budget = 2500 if profile.get("compact_tools_blurb") else 8000
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
                history.append_exchange(user_text, result.text, label)
                return AskResult(True, text=result.text, provider=label, attempts=attempts,
                                 assistant_name=assistant_name, address_user_as=address)

            attempts.append((key_label, result.error))

    return AskResult(False, attempts=attempts, assistant_name=assistant_name, address_user_as=address)
