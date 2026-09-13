"""Summarizes older conversation turns via a lightweight round-trip to an
AI provider, instead of conversations.py's old mechanical text-truncation
recap.

Why this exists: conversation_messages()'s old recap built "earlier in
this conversation" by hard-truncating each older user/assistant pair to a
fixed char count (90/110 chars) and concatenating the results. That's
cheap but dumb-lossy — a long answer gets scissored mid-sentence rather
than actually condensed, and it still costs real prompt tokens for
whatever survives the cut. This module instead asks a small/cheap model
to write a short prose recap of the same older turns, once, and caches
it — so the *current* model's context gets a genuinely compressed summary
instead of a pile of truncated fragments, and unchanged history is never
re-summarized on every single ask().

Same best-effort philosophy as discovery_cache.py/route_stickiness.py: a
missing config, no eligible provider, a network error, or a malformed
response all degrade to "no summary" (returns None) rather than an
error — conversations.py must fall back to its own mechanical recap in
that case, never let this be the reason an ask() fails.
"""

import hashlib
import json
from pathlib import Path

from . import ai_config
from . import ai_providers

JARVIS_DIR = Path.home() / ".jarvis"
CACHE_FILE = JARVIS_DIR / "history_summary_cache.json"
ENCODING = "utf-8"

# Keep this fast and cheap — it's pure overhead on top of the real ask,
# not something the user is waiting on for its own sake.
SUMMARY_TIMEOUT = 12
SUMMARY_MAX_TOKENS = 220
SUMMARY_TARGET_CHARS = 600  # roughly what the old recap_budget aimed for
# Don't hand the summarizer an unbounded wall of old transcript text.
SUMMARY_INPUT_CHAR_CAP = 8000
# Best-effort cap so ~/.jarvis/history_summary_cache.json can't grow
# forever across a long-lived install.
MAX_CACHE_ENTRIES = 200

_SYSTEM_PROMPT = (
    "You compress old chat turns into a short factual recap for another "
    "AI assistant's context window. Write 2-6 terse bullet lines (no "
    "preamble, no markdown headers) covering what was asked and what was "
    "done or answered, including any concrete file names, paths, "
    "commands, or decisions made. Skip pleasantries and filler. Keep the "
    f"whole thing under {SUMMARY_TARGET_CHARS} characters."
)


def _load_cache():
    try:
        data = json.loads(CACHE_FILE.read_text(encoding=ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(data):
    try:
        JARVIS_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding=ENCODING)
    except OSError:
        # Best-effort only — a failed write just means the next call
        # re-summarizes instead of hitting cache, same as a miss.
        pass


def _hash_for(conv_id, raw_text):
    h = hashlib.sha256()
    h.update(str(conv_id).encode(ENCODING, "ignore"))
    h.update(b"\x00")
    h.update(raw_text.encode(ENCODING, "ignore"))
    return h.hexdigest()


def _raw_text_for(older_src):
    lines = []
    for ex in older_src:
        user_text = (ex.get("user") or "").strip()
        assistant_text = (ex.get("jarvis") or "").strip()
        if not user_text:
            continue
        lines.append(f"User: {user_text}\nAssistant: {assistant_text}")
    return "\n\n".join(lines)


def _pick_provider(cfg):
    """First enabled, keyed (or ollama) provider, preferring one flagged
    "summarizer": true in ai_config.json so a cheap/fast model can be
    dedicated to this instead of whichever model provider_priority puts
    first for the real ask(). Falls back to the first eligible provider
    outright if none are flagged — summarizing with *a* model beats not
    summarizing at all. Deliberately doesn't reuse ai_client's own
    provider-selection helpers to avoid a conversations.py -> this module
    -> ai_client -> conversations.py import cycle; this is a small enough
    subset of that logic to duplicate.
    """
    providers = [
        p for p in (cfg.get("providers") or [])
        if isinstance(p, dict) and p.get("enabled", True)
    ]
    keyed = [p for p in providers if p.get("type") == "ollama" or ai_config.provider_keys(p)]
    if not keyed:
        return None
    flagged = [p for p in keyed if p.get("summarizer")]
    return flagged[0] if flagged else keyed[0]


def summarize_older_exchanges(conv_id, older_src, cfg=None):
    """Returns a short AI-written recap string for `older_src` (oldest-
    first list of exchange dicts, same shape conversations.py stores in
    an exchange record), or None if summarization isn't possible or
    didn't succeed right now. Callers MUST treat None exactly like a
    cache miss and fall back to their own mechanical recap — this never
    raises.
    """
    if not older_src:
        return None

    raw_text = _raw_text_for(older_src)
    if not raw_text:
        return None

    key = _hash_for(conv_id, raw_text)
    cache = _load_cache()
    cached = cache.get(key)
    if isinstance(cached, str) and cached.strip():
        return cached

    cfg = cfg if cfg is not None else ai_config.load_ai_config()
    provider = _pick_provider(cfg)
    if not provider:
        return None

    adapter = ai_providers.ADAPTERS.get(provider.get("type"))
    if adapter is None:
        return None

    resolved = dict(provider)
    resolved["timeout"] = min(resolved.get("timeout") or SUMMARY_TIMEOUT, SUMMARY_TIMEOUT)
    resolved["max_tokens"] = SUMMARY_MAX_TOKENS

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": raw_text[-SUMMARY_INPUT_CHAR_CAP:]},
    ]

    keys = ai_config.provider_keys(provider) or [None]
    for key_val in keys:
        attempt = dict(resolved)
        if key_val is not None:
            attempt["api_key"] = key_val
        try:
            result = adapter(attempt, messages, attempt["timeout"], tools=None, tool_executor=None)
        except Exception:
            # One bad provider/key must never take down the real ask() —
            # just try the next key, then give up and let the caller fall
            # back to its mechanical recap.
            continue

        if result is not None and getattr(result, "ok", False):
            summary = (result.text or "").strip()
            if summary:
                if len(summary) > SUMMARY_TARGET_CHARS:
                    summary = summary[:SUMMARY_TARGET_CHARS].rstrip() + "\u2026"
                cache[key] = summary
                if len(cache) > MAX_CACHE_ENTRIES:
                    for old_key in list(cache.keys())[: len(cache) - MAX_CACHE_ENTRIES]:
                        cache.pop(old_key, None)
                _save_cache(cache)
                return summary

    return None
