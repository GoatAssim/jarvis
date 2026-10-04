"""Inline (ghost-text) code suggestions for the Tool Manager editor (L.33).

The Tool Manager's code editor shows a dimmed suggestion after the caret, VS
Code style; Tab accepts it. This module is the model half of that: given the
file text and the caret offset it asks one provider for the text that should
come next and returns it, cleaned.

What this is NOT:
  - It never writes a tool file. The editor shows the suggestion and the
    owner accepts it into the buffer; saving is still the owner's Save, which
    still runs ``custom_tools_store.validate_source``. The model can't create
    or change custom tools (AGENTS.md); a suggestion the owner has not
    accepted is just text on a screen.
  - It never raises. No provider, no key, a timeout or a malformed reply all
    come back as ``{"ok": False, "error": ...}`` and the editor goes quiet.
  - It is not an ask(): no tools, no conversation, no memory, no thinking.
    The prompt is the code around the caret and a short description of the
    custom-tool contract, so a call costs a few hundred tokens.

Provider choice mirrors ``history_summarizer`` (a cheap, fast model beats the
main one for pure overhead): the first enabled, keyed provider flagged
``"completion": true`` in ai_config.json, else one flagged ``"summarizer"``,
else the first eligible provider.
"""

from __future__ import annotations

import re

from . import ai_config
from . import ai_providers

SUGGEST_TIMEOUT = 15
SUGGEST_MAX_TOKENS = 200
# How much of the file around the caret is sent. The prefix matters far more
# than the suffix; both are clipped on a line boundary where possible.
PREFIX_CHARS = 4000
SUFFIX_CHARS = 1200
# Never accept an enormous file or caret from a hand-rolled request.
MAX_SOURCE_CHARS = 200_000
# A suggestion is a few lines, not a rewrite.
MAX_LINES = 12
MAX_CHARS = 900

_CURSOR = "<CURSOR>"

_SYSTEM_PROMPT = (
    "You are the code-completion engine inside Jarvis's custom-tool editor. "
    "The file is a Python module that adds tools to Jarvis. Its contract: "
    "functions named like tool_<name>(args) take a dict and return a JSON-able "
    "dict that includes \"ok\"; TOOL_SCHEMAS is a list of {name, description, "
    "parameters} dicts (JSON-schema parameters); TOOLS maps each schema name "
    "to its function; TOOL_GROUP is a string; TOOL_KEYWORDS maps a tool name to "
    "{phrase: weight}; an optional TEST_CHECKLIST dict says how to test each "
    "tool. Reply with ONLY the text to insert at the marker "
    f"{_CURSOR}. Do not repeat code that is already before the marker. Do not "
    "repeat code that already follows it. No markdown fences, no commentary, "
    f"no explanation. At most {MAX_LINES} lines. Match the file's indentation "
    "and style. If nothing useful comes next, reply with nothing."
)

# Replies that are prose rather than code. Cheap models sometimes ignore the
# "only code" instruction; showing a sentence as ghost text would be wrong.
_PROSE_START = re.compile(
    r"^\s*(here(?:'s| is| are)|sure\b|certainly\b|of course\b|i\b|i'(?:ll|d|m)\b|"
    r"the following\b|this (?:code|will|is)\b|note\b|you (?:can|could|should)\b|"
    r"to (?:complete|continue)\b)",
    re.IGNORECASE,
)
_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n?|\n?```\s*$")


def _pick_provider(cfg):
    providers = [
        p for p in (cfg.get("providers") or [])
        if isinstance(p, dict) and p.get("enabled", True)
    ]
    keyed = [p for p in providers
             if p.get("type") == "ollama" or ai_config.provider_keys(p)]
    if not keyed:
        return None
    for flag in ("completion", "summarizer"):
        flagged = [p for p in keyed if p.get(flag)]
        if flagged:
            return flagged[0]
    return keyed[0]


def _window(source, cursor):
    """(prefix, suffix) around the caret, clipped on line boundaries."""
    prefix = source[:cursor]
    suffix = source[cursor:]
    if len(prefix) > PREFIX_CHARS:
        prefix = prefix[-PREFIX_CHARS:]
        nl = prefix.find("\n")
        if 0 <= nl < 400:
            prefix = prefix[nl + 1:]
    if len(suffix) > SUFFIX_CHARS:
        suffix = suffix[:SUFFIX_CHARS]
        nl = suffix.rfind("\n")
        if nl > len(suffix) - 400 and nl > 0:
            suffix = suffix[:nl]
    return prefix, suffix


def build_messages(source, cursor, name="draft"):
    prefix, suffix = _window(source, cursor)
    user = (
        f"File: {name}.py\n\n"
        f"{prefix}{_CURSOR}{suffix}"
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def clean_completion(text, prefix="", suffix=""):
    """Turn a raw model reply into the text to show after the caret ("" =
    show nothing)."""
    if not isinstance(text, str):
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = t.replace(_CURSOR, "")
    t = _FENCE.sub("", t) if "```" in t else t
    if not t.strip():
        return ""
    if _PROSE_START.match(t):
        return ""
    # Drop trailing blank lines / trailing spaces; keep leading whitespace and
    # a leading newline, which are meaningful (an end-of-line suggestion often
    # begins on the next line).
    t = t.rstrip()
    lines = t.split("\n")
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES]
    t = "\n".join(lines)
    if len(t) > MAX_CHARS:
        t = t[:MAX_CHARS]
        t = t[: t.rfind("\n")] if "\n" in t else t
    # The model sometimes re-types what already follows the caret; trim the
    # echoed tail so accepting doesn't double it.
    tail = suffix.lstrip("\n")
    if tail:
        first = tail.split("\n", 1)[0].strip()
        if first and len(first) >= 3:
            last_line = t.rsplit("\n", 1)[-1].strip()
            if last_line == first:
                if "\n" not in t:
                    return ""
                t = t.rsplit("\n", 1)[0].rstrip()
            elif last_line.endswith(first):
                t = t[: t.rfind(first)].rstrip(" ")
    # Echoing the line the caret is on is not a suggestion.
    cur_line = prefix.rsplit("\n", 1)[-1]
    if cur_line.strip() and t.strip() == cur_line.strip():
        return ""
    return t if t.strip() else ""


def suggest(source, cursor, name="draft", cfg=None):
    """Ask one provider what comes next. Never raises.

    Returns {"ok": True, "text": str, "provider": str} (``text`` may be ""),
    or {"ok": False, "error": str}.
    """
    if not isinstance(source, str):
        return {"ok": False, "error": "source must be text"}
    if len(source) > MAX_SOURCE_CHARS:
        return {"ok": False, "error": "file is too large for suggestions"}
    try:
        cursor = int(cursor)
    except (TypeError, ValueError):
        return {"ok": False, "error": "cursor must be a number"}
    cursor = max(0, min(cursor, len(source)))

    try:
        cfg = cfg if cfg is not None else ai_config.load_ai_config()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "couldn't read the AI config: %s" % exc}
    provider = _pick_provider(cfg)
    if not provider:
        return {"ok": False, "error": "no AI provider with a key is configured"}
    adapter = ai_providers.ADAPTERS.get(provider.get("type"))
    if adapter is None:
        return {"ok": False, "error": "unsupported provider type"}

    resolved = dict(provider)
    resolved["timeout"] = min(resolved.get("timeout") or SUGGEST_TIMEOUT, SUGGEST_TIMEOUT)
    resolved["max_tokens"] = SUGGEST_MAX_TOKENS
    messages = build_messages(source, cursor, name)
    prefix, suffix = _window(source, cursor)

    last_error = "the provider didn't answer"
    for key_val in (ai_config.provider_keys(provider) or [None]):
        attempt = dict(resolved)
        if key_val is not None:
            attempt["api_key"] = key_val
        try:
            result = adapter(attempt, messages, attempt["timeout"],
                             tools=None, tool_executor=None)
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)[:200] or last_error
            continue
        if result is not None and getattr(result, "ok", False):
            return {
                "ok": True,
                "text": clean_completion(getattr(result, "text", "") or "", prefix, suffix),
                "provider": str(provider.get("name") or provider.get("type") or ""),
            }
        err = getattr(result, "error", None)
        if err:
            last_error = str(err)[:200]
    return {"ok": False, "error": last_error}
