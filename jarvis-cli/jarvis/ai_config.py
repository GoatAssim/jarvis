"""Loading and defaults for ~/.jarvis/ai_config.json.

Same philosophy as commands.json (see cli.py): a plain, hand-editable JSON
file that's created with a sensible starter template on first use and
re-read fresh on every invocation \u2014 no separate "apply" or reload step,
no code changes needed to add a key or change a model.

Every "famous" provider is listed in the starter template out of the box,
each with an empty api_keys list. A provider is only ever actually tried if
it's `"enabled": true` AND either it's type "ollama" (no key needed \u2014 it's
your own machine) or it has at least one non-empty key. So the file works
immediately: paste a key into any one block and that provider goes live,
nothing else to configure.

Provider try-order is controlled separately from API keys:

    "defaults": {
        "provider_priority": ["gemini", "groq", "openai", ...]
    }

List provider *names* in the order you want jarvis to try them. Providers
not named fall back to the order they appear in the "providers" array.
Leave the list empty (or omit it) to keep using array order only.

Rate-sensitive hosts (Groq by default) can get a leaner system prompt via
"defaults.compact_prompt_providers" — fewer commands listed, shorter
descriptions, and less conversation history folded in.

Each provider can hold *more than one* key:

    "api_keys": ["sk-first...", "sk-second...", "sk-third..."]

jarvis tries them in array order, treating a bad/rate-limited/out-of-credit
key the same way it treats a bad provider \u2014 log why, move to the next key,
and only fall through to the *next provider* once every key for this one
has been tried (see ai_client.py). The old singular field still works too:

    "api_key": "sk-only-one..."

Both can even be present at once (api_keys tried first, then api_key
appended if it isn't already in the list) \u2014 provider_keys() below is the
one place that normalizes this, so nothing else in the codebase needs to
care which form a given config file happens to use.

"defaults.tools_enabled" (default true) turns Jarvis's built-in system-
info tools (battery, wifi, location, date/time, disk, memory \u2014 see
tools.py) on or off globally. They're read-only and cost nothing extra
unless the model actually decides to call one, but if you'd rather every
ask be a single request no matter what, flip this to false.
"""

import json
import sys
from pathlib import Path

JARVIS_DIR = Path.home() / ".jarvis"
AI_CONFIG_FILE = JARVIS_DIR / "ai_config.json"
ENCODING = "utf-8"

DEFAULT_AI_CONFIG = {
    "persona": {
        "assistant_name": "J.A.R.V.I.S",
        "address_user_as": "sir",
        "extra_instructions": "",
    },
    "defaults": {
        "max_tokens": 700,
        "timeout": 30,
        "tools_enabled": True,
        "provider_priority": [
            "gemini",
            "groq",
            "openai",
            "anthropic",
            "xai",
            "mistral",
            "deepseek",
            "openrouter",
            "cohere",
            "ollama",
        ],
        "compact_prompt_providers": ["groq"],
        "compact_max_commands": 8,
        "compact_history_char_budget": 1800,
        "compact_history_exchanges": 4,
    },
    "providers": [
        {
            "name": "openai",
            "type": "openai_compatible",
            "enabled": True,
            "base_url": "https://api.openai.com/v1/chat/completions",
            "api_keys": [""],
            "model": "gpt-5-mini",
        },
        {
            "name": "anthropic",
            "type": "anthropic",
            "enabled": True,
            "base_url": "https://api.anthropic.com/v1/messages",
            "api_keys": [""],
            "model": "claude-haiku-4-5-20251001",
        },
        {
            "name": "gemini",
            "type": "gemini",
            "enabled": True,
            "base_url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            "api_keys": [""],
            "model": "gemini-2.5-flash",
        },
        {
            "name": "xai",
            "type": "openai_compatible",
            "enabled": True,
            "base_url": "https://api.x.ai/v1/chat/completions",
            "api_keys": [""],
            "model": "grok-4-0709",
        },
        {
            "name": "groq",
            "type": "openai_compatible",
            "enabled": True,
            "base_url": "https://api.groq.com/openai/v1/chat/completions",
            "api_keys": [""],
            "model": "llama-3.3-70b-versatile",
        },
        {
            "name": "mistral",
            "type": "openai_compatible",
            "enabled": True,
            "base_url": "https://api.mistral.ai/v1/chat/completions",
            "api_keys": [""],
            "model": "mistral-small-latest",
        },
        {
            "name": "deepseek",
            "type": "openai_compatible",
            "enabled": True,
            "base_url": "https://api.deepseek.com/v1/chat/completions",
            "api_keys": [""],
            "model": "deepseek-v4-flash",
        },
        {
            "name": "openrouter",
            "type": "openai_compatible",
            "enabled": True,
            "base_url": "https://openrouter.ai/api/v1/chat/completions",
            "api_keys": [""],
            "model": "openai/gpt-4o-mini",
        },
        {
            "name": "cohere",
            "type": "cohere",
            "enabled": True,
            "base_url": "https://api.cohere.com/v2/chat",
            "api_keys": [""],
            "model": "command-a-03-2025",
        },
        {
            "name": "ollama",
            "type": "ollama",
            "enabled": True,
            "base_url": "http://localhost:11434/api/chat",
            "api_keys": [],
            "model": "llama3.1",
        },
    ],
}

# Provider "type"s jarvis knows how to speak to. openai_compatible covers
# any endpoint that mirrors OpenAI's /chat/completions shape (which, as of
# writing, is most of them \u2014 see ai_providers.py). To add a brand-new
# OpenAI-compatible host later, you don't need new code: just add a
# provider block with this type and the right base_url.
KNOWN_TYPES = {"openai_compatible", "anthropic", "gemini", "cohere", "ollama"}


def provider_keys(provider):
    """Every usable key for a provider, in try-order: the 'api_keys' list
    first (in the order given, duplicates dropped), then the legacy
    singular 'api_key' field appended if it's set and isn't already in the
    list \u2014 so an older hand-edited config, or a fresh single-key paste,
    keeps working exactly as before. Blank/whitespace-only entries are
    ignored. Returns [] for a keyless provider (e.g. an unfilled starter
    entry, or Ollama, which never needs one)."""
    keys = []
    for k in (provider.get("api_keys") or []):
        if isinstance(k, str) and k.strip() and k not in keys:
            keys.append(k)
    legacy = provider.get("api_key")
    if isinstance(legacy, str) and legacy.strip() and legacy not in keys:
        keys.append(legacy)
    return keys


def ensure_ai_config():
    JARVIS_DIR.mkdir(parents=True, exist_ok=True)
    if not AI_CONFIG_FILE.exists():
        AI_CONFIG_FILE.write_text(
            json.dumps(DEFAULT_AI_CONFIG, indent=2) + "\n", encoding=ENCODING
        )


def load_ai_config():
    """Always returns a dict with 'persona', 'defaults', and 'providers'
    keys, even if the file is missing, empty, or malformed \u2014 callers never
    need to guard against a half-shaped config."""
    ensure_ai_config()
    try:
        data = json.loads(AI_CONFIG_FILE.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(
            f"Warning: couldn't read {AI_CONFIG_FILE} ({e}) \u2014 treating it as empty for this "
            f"run. Fix the JSON, or delete the file to get a fresh starter template.",
            file=sys.stderr,
        )
        return {"persona": {}, "defaults": {}, "providers": []}

    if not isinstance(data, dict):
        return {"persona": {}, "defaults": {}, "providers": []}

    persona = data.get("persona")
    defaults = data.get("defaults")
    providers = data.get("providers")

    return {
        "persona": persona if isinstance(persona, dict) else {},
        "defaults": defaults if isinstance(defaults, dict) else {},
        "providers": providers if isinstance(providers, list) else [],
    }
